"""tc/netem injection on host-side veths.

Applies failures (loss, delay, blackhole) to the host-side end of a veth
pair from the docker host's perspective. Runs `tc` inside the target
container's network namespace via `nsenter`, which keeps SONiC's view of
the fabric clean and avoids re-pushing ConfigDB.

Target forms (parsed from scenario YAML `target:` strings):

    plane <N>                          all green+yellow host NICs in plane N
                                       (32 NICs: 16 green ethN + 16 yellow ethN)
    host <NAME>                        all 4 plane uplinks of one host
    host <NAME> plane <N>              single NIC: <NAME>:eth(N+1)

Spec strings (`spec:`) are passed straight through to `tc qdisc add ... netem`:

    loss 5%
    loss 5% 25%                        Markov-correlated loss
    delay 50ms 10ms 25%                mean delay 50ms, jitter 10ms, corr 25%
    delay 50ms loss 1%                 combined
    blackhole                          sugar for `loss 100%`

The module is structurally side-effect-free in import; concrete execution
goes through `Netem.apply()` / `revert()` which call out via the configurable
`runner` callable (default: `subprocess.run`). Tests can pass a mock.
"""

from __future__ import annotations

import re
import shlex
import subprocess
from dataclasses import dataclass, field
from typing import Callable, Sequence

from .topo import (
    NUM_PLANES, NUM_LEAVES, TENANTS, CLAB_TOPOLOGY_NAME,
    PLANE_NIC, host_name,
)

# Containerlab topology name; used only when a container can't be found
# by its short name (matches routes.py:container() fallback). Sourced
# from the active topo.yaml.
TOPO_DEFAULT = CLAB_TOPOLOGY_NAME

RunResult = subprocess.CompletedProcess
Runner = Callable[[Sequence[str]], RunResult]


# --- target parsing ---------------------------------------------------------

@dataclass(frozen=True)
class NICTarget:
    """One concrete veth endpoint: (container, ifname)."""
    container: str
    ifname: str

    def __str__(self) -> str:
        return f"{self.container}:{self.ifname}"


def _all_hosts() -> list[str]:
    return [host_name(t, i) for t in TENANTS for i in range(NUM_LEAVES)]


def parse_target(target: str) -> list[NICTarget]:
    """Expand a target string into the concrete NICs it covers.

    Whitespace-tolerant. Raises ValueError on bad input.
    """
    if not isinstance(target, str):
        raise ValueError(f"target must be a string, got {type(target).__name__}")
    s = target.strip()

    # "plane N"
    m = re.fullmatch(r"plane\s+(\d+)", s)
    if m:
        plane = int(m.group(1))
        if not 0 <= plane < NUM_PLANES:
            raise ValueError(
                f"target 'plane {plane}': plane out of range 0..{NUM_PLANES - 1}"
            )
        return [NICTarget(host, PLANE_NIC(plane)) for host in _all_hosts()]

    # "host NAME plane N"
    m = re.fullmatch(r"host\s+(\S+)\s+plane\s+(\d+)", s)
    if m:
        host = m.group(1)
        plane = int(m.group(2))
        _validate_host(host)
        if not 0 <= plane < NUM_PLANES:
            raise ValueError(
                f"target '{s}': plane out of range 0..{NUM_PLANES - 1}"
            )
        return [NICTarget(host, PLANE_NIC(plane))]

    # "host NAME"
    m = re.fullmatch(r"host\s+(\S+)", s)
    if m:
        host = m.group(1)
        _validate_host(host)
        return [NICTarget(host, PLANE_NIC(p)) for p in range(NUM_PLANES)]

    raise ValueError(
        f"unrecognized target {target!r}; expected "
        "'plane N', 'host NAME', or 'host NAME plane N'"
    )


def _validate_host(host: str) -> None:
    """The hostname must look like '<tenant>-host<NN>' with NN < NUM_LEAVES."""
    m = re.fullmatch(r"(green|yellow)-host(\d{2})", host)
    if not m:
        raise ValueError(
            f"host name {host!r} must match '<green|yellow>-host<NN>'"
        )
    idx = int(m.group(2))
    if not 0 <= idx < NUM_LEAVES:
        raise ValueError(
            f"host name {host!r}: id {idx} out of range 0..{NUM_LEAVES - 1}"
        )


# --- spec parsing -----------------------------------------------------------

# tc netem accepts a small grammar; rather than reimplementing it, we
# whitelist the tokens we expect to see and let tc reject anything else.
_NETEM_TOKEN_RE = re.compile(
    r"^[A-Za-z0-9.%]+$"
)

# Tokens that legitimately appear in a netem spec. Anything else is rejected
# at parse time as a typo guard. (`tc` itself will of course reject anything
# malformed; this pre-check catches shell-injection-shaped inputs early.)
_NETEM_ALLOWED_KEYWORDS = {
    "loss", "delay", "duplicate", "corrupt", "reorder", "rate", "gap",
    "limit", "distribution", "normal", "pareto", "paretonormal",
    "blackhole",
}


def normalize_spec(spec: str) -> list[str]:
    """Validate and convert a netem spec into argv tokens.

    `blackhole` is sugar for `loss 100%`. Everything else passes through
    verbatim after token-by-token validation.
    """
    if not isinstance(spec, str) or not spec.strip():
        raise ValueError(f"netem spec must be a non-empty string, got {spec!r}")
    tokens = spec.strip().split()
    if tokens == ["blackhole"]:
        return ["loss", "100%"]
    for tok in tokens:
        if not _NETEM_TOKEN_RE.match(tok):
            raise ValueError(f"netem spec: bad token {tok!r}")
    # First token should be a recognized keyword. The grammar is loose
    # (`delay 50ms 10ms loss 1%` is valid), so we only check the very first
    # token strictly.
    first = tokens[0]
    if first not in _NETEM_ALLOWED_KEYWORDS:
        raise ValueError(
            f"netem spec: leading token {first!r} not in "
            f"{sorted(_NETEM_ALLOWED_KEYWORDS)}"
        )
    return tokens


# --- nsenter command builder ------------------------------------------------

def _docker_pid_cmd(container: str) -> list[str]:
    """argv that prints the container's main PID. Used to resolve netns."""
    return ["docker", "inspect", "-f", "{{.State.Pid}}", container]


def _nsenter_tc_argv_add(pid: int, ifname: str,
                         netem_tokens: Sequence[str]) -> list[str]:
    """`tc qdisc add dev IF root netem <tokens>` inside the container netns."""
    return ["nsenter", "-t", str(pid), "-n",
            "tc", "qdisc", "add", "dev", ifname, "root", "netem",
            *netem_tokens]


def _nsenter_tc_argv_del(pid: int, ifname: str) -> list[str]:
    """`tc qdisc del dev IF root` inside the container netns. No netem tokens
    needed (tc only cares about the root qdisc handle)."""
    return ["nsenter", "-t", str(pid), "-n",
            "tc", "qdisc", "del", "dev", ifname, "root"]


# --- runner abstraction -----------------------------------------------------

def _default_runner(argv: Sequence[str]) -> RunResult:
    return subprocess.run(
        list(argv),
        check=False,
        capture_output=True,
        text=True,
    )


def resolve_container_pid(container: str, *,
                          runner: Runner = _default_runner,
                          topo: str = TOPO_DEFAULT) -> int:
    """Return the PID of the container's init process. Tries the bare name
    first, falls back to clab-<topo>-<name>. Mirrors routes.py:container()."""
    for name in (container, f"clab-{topo}-{container}"):
        res = runner(_docker_pid_cmd(name))
        if res.returncode == 0:
            pid_str = res.stdout.strip()
            if pid_str.isdigit() and int(pid_str) > 0:
                return int(pid_str)
    raise RuntimeError(
        f"netem: cannot resolve PID for container {container!r} "
        f"(tried bare and clab-{topo}- prefixes). Is the lab up?"
    )


# --- the public surface -----------------------------------------------------

@dataclass
class Fault:
    """One scenario `faults:` entry, post-validation."""
    target: str
    spec: str

    # Cached after .resolve(). `tokens` is the post-normalize argv tail.
    nics: tuple[NICTarget, ...] = field(default=(), init=False)
    tokens: tuple[str, ...] = field(default=(), init=False)
    resolved: bool = field(default=False, init=False)

    def resolve(self) -> None:
        nics = tuple(parse_target(self.target))
        tokens = tuple(normalize_spec(self.spec))
        # dataclass is mutable here on purpose; cache the parsed view.
        self.nics = nics
        self.tokens = tokens
        self.resolved = True


@dataclass
class Netem:
    """Apply/revert a list of faults against a running lab.

    Usage:
        nm = Netem(faults=[Fault("plane 2", "loss 5%")])
        nm.apply()
        try:
            ...
        finally:
            nm.revert()
    """
    faults: list[Fault]
    runner: Runner = _default_runner
    topo: str = TOPO_DEFAULT

    # populated as faults are applied; consulted by revert() so we only
    # tear down what we successfully put up.
    _applied: list[tuple[int, str]] = field(default_factory=list, init=False)

    def apply(self, *, dry_run: bool = False) -> list[list[str]]:
        """Apply every fault. Returns the list of argvs invoked (useful for
        dry-run preview and test assertions).

        If `dry_run` is True, no commands are run, but the argvs are still
        built (and target/spec parsing is still validated)."""
        invoked: list[list[str]] = []
        for fault in self.faults:
            if not fault.resolved:
                fault.resolve()
            for nic in fault.nics:
                pid = (0 if dry_run
                       else resolve_container_pid(nic.container,
                                                  runner=self.runner,
                                                  topo=self.topo))
                argv = _nsenter_tc_argv_add(pid, nic.ifname, fault.tokens)
                invoked.append(argv)
                if dry_run:
                    continue
                res = self.runner(argv)
                if res.returncode != 0:
                    # Best-effort cleanup of what we already applied, then re-raise.
                    self.revert(quiet=True)
                    raise RuntimeError(
                        f"netem apply failed on {nic}: "
                        f"{_format_err(argv, res)}"
                    )
                self._applied.append((pid, nic.ifname))
        return invoked

    def revert(self, *, quiet: bool = False) -> list[list[str]]:
        """Remove every qdisc this Netem successfully applied. Idempotent.

        With quiet=True, errors from `tc qdisc del` are swallowed (used by
        the apply() unwind path and as a final scenario teardown safety net).
        """
        invoked: list[list[str]] = []
        # Pop in reverse so the apply log mirrors the revert order.
        while self._applied:
            pid, ifname = self._applied.pop()
            argv = _nsenter_tc_argv_del(pid, ifname)
            invoked.append(argv)
            res = self.runner(argv)
            if res.returncode != 0 and not quiet:
                raise RuntimeError(
                    f"netem revert failed on pid={pid} dev={ifname}: "
                    f"{_format_err(argv, res)}"
                )
        return invoked

    def __enter__(self) -> "Netem":
        self.apply()
        return self

    def __exit__(self, *exc) -> None:
        self.revert(quiet=True)


# --- helpers ----------------------------------------------------------------

def _format_err(argv: Sequence[str], res: RunResult) -> str:
    cmd = " ".join(shlex.quote(a) for a in argv)
    err = (res.stderr or "").strip()
    out = (res.stdout or "").strip()
    return (
        f"\n  cmd: {cmd}\n  rc:  {res.returncode}"
        + (f"\n  err: {err}" if err else "")
        + (f"\n  out: {out}" if out else "")
    )
