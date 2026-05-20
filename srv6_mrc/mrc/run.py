#!/usr/bin/env python3
"""mrc/run.py — scenario orchestrator.

Drives a single MRC experiment end-to-end:

    1. Load + validate a scenario YAML.
    2. Apply tc/netem faults to the lab via lib/netem.
    3. For each FlowSpec, in parallel:
        a. docker exec <dst_host> spray --role recv --json
           (background, captures JSON to stdout on idle-exit).
        b. brief settle delay so all receivers are sniffing.
        c. docker exec <src_host> spray --role send --json
           (foreground, prints SenderResult JSON on stdout).
    4. Wait for all receivers to drain (idle-timeout).
    5. Revert faults (always — even on failure).
    6. Merge JSON records via lib/report.ScenarioReport.
    7. Print ASCII summary; optionally write JSON to `report.out`.

Run:
    python3 -m mrc.run scenarios/green-mrc-baseline.yaml
    python3 -m mrc.run scenarios/green-mrc-plane-loss.yaml --dry-run
    python3 -m mrc.run scenarios/green-mrc-baseline.yaml --report out.json

This file is *not* designed to be imported by the host containers — it runs
on the docker host (no scapy required, no raw sockets used here).
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import os
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Allow `python3 mrc/run.py ...` and `python3 -m mrc.run ...` both to work.
# When invoked as a script (no package context), prepend project root so
# `from srv6_mrc...` succeeds.
if __package__ in (None, ""):
    _ROOT = Path(__file__).resolve().parent.parent
    if str(_ROOT) not in sys.path:
        sys.path.insert(0, str(_ROOT))


def _infer_srv6_topo_from_argv() -> None:
    """Set SRV6_TOPO from the scenario path on argv, before any srv6_mrc
    import. Idempotent: does nothing if SRV6_TOPO is already set.

    Scenario paths look like topologies/<name>/scenarios/<scen>.yaml,
    so the grandparent is the topology directory and its `topo.yaml`
    drives the run. Setting SRV6_TOPO here ensures srv6_mrc.topo
    binds NUM_LEAVES (and friends) to the correct topology before the
    scenario module evaluates auto-sized pair-sets at import time.
    """
    if os.environ.get("SRV6_TOPO"):
        return
    # Walk argv for the first arg that exists and looks like a scenario
    # YAML under a topologies/<name>/ tree.
    for arg in sys.argv[1:]:
        if not arg or arg.startswith("-"):
            continue
        p = Path(arg)
        if not p.is_file():
            continue
        # topologies/<name>/scenarios/<scen>.yaml -> topo at p.parents[1]/topo.yaml
        try:
            topo_yaml = p.resolve().parents[1] / "topo.yaml"
        except IndexError:
            continue
        if topo_yaml.is_file():
            os.environ["SRV6_TOPO"] = str(topo_yaml)
            return


_infer_srv6_topo_from_argv()

from srv6_mrc.netem import Fault, Netem
from srv6_mrc.report import ScenarioReport
from srv6_mrc.mrc.scenario import MrcSpec, Scenario, from_yaml_file
from srv6_mrc.topo import (NUM_PLANES, NUM_SPINES, inner_addr,
                              select_spines_for_addrs, usid_outer_dst)


# --- defaults ---------------------------------------------------------------

# How long to wait after spawning all receivers before sending starts. Keeps
# the first few packets from racing the AsyncSniffer init in scapy.
RECEIVER_SETTLE_S = 1.0

# How long after applying faults before sending starts. Gives netem qdiscs
# and the health-aware policy probes time to converge.
FAULT_SETTLE_S = 1.0

# Per-receiver `--idle-timeout` (seconds). Receivers self-exit this many
# seconds after their last packet. Must be larger than any tolerable
# pause in the send stream; reasonable default = 2× the longest interval
# between bursts. Default of 6s matches the `spray` CLI default.
RECV_IDLE_TIMEOUT_S = 6.0


# --- subprocess helpers -----------------------------------------------------

@dataclass
class ExecResult:
    cmd: list[str]
    rc: int
    stdout: str
    stderr: str
    elapsed_s: float


def docker_exec(container: str, argv: list[str],
                *, timeout_s: float | None = None,
                env: dict[str, str] | None = None) -> ExecResult:
    """`docker exec <container> <argv...>`, capture stdout/stderr.

    `timeout_s=None` blocks until exit; a positive value kills on timeout.
    `env` adds -e KEY=VALUE flags to docker exec so the in-container
    process sees those env vars. Useful for SRV6_MRC_CONFIG_JSON.
    """
    cmd = ["docker", "exec"]
    if env:
        for k, v in sorted(env.items()):
            cmd += ["-e", f"{k}={v}"]
    cmd += [container] + argv
    t0 = time.monotonic()
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout_s,
        )
        return ExecResult(
            cmd=cmd, rc=proc.returncode,
            stdout=proc.stdout, stderr=proc.stderr,
            elapsed_s=time.monotonic() - t0,
        )
    except subprocess.TimeoutExpired as e:
        return ExecResult(
            cmd=cmd, rc=-1,
            stdout=e.stdout or "", stderr=(e.stderr or "") + "\n[timed out]\n",
            elapsed_s=time.monotonic() - t0,
        )


def docker_exec_async(container: str, argv: list[str],
                      *, env: dict[str, str] | None = None) -> subprocess.Popen:
    """Fire-and-forget; caller waits + reads stdout via `.communicate()`.

    `env` adds -e KEY=VALUE flags to docker exec (see docker_exec).
    """
    cmd = ["docker", "exec"]
    if env:
        for k, v in sorted(env.items()):
            cmd += ["-e", f"{k}={v}"]
    cmd += [container] + argv
    return subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )


# --- policy spec → CLI flag -------------------------------------------------

def policy_to_cli(spec: Any) -> str:
    """Convert a validated scenario `policy:` spec into a `--policy` string.

    Mirrors lib/policy.policy_from_spec accepted shapes.
    """
    if isinstance(spec, str):
        # Bare strings pass straight through: round_robin, hash5tuple,
        # ev_spray, health_aware_mrc. The spray.py side resolves
        # health_aware_mrc into a bound HealthAwareMrc with its local
        # EVStateTable; ev_spray (bare) defaults to full fan-out and
        # the scenario-level paths_per_plane override (if any) reaches
        # the sender via the SRV6_PATHS_PER_PLANE env var rather than a
        # CLI flag, so the bare policy string here is sufficient.
        return spec
    if isinstance(spec, dict) and len(spec) == 1:
        key, value = next(iter(spec.items()))
        if key == "weighted":
            return "weighted:" + ",".join(str(w) for w in value)
        if key == "ev_spray":
            return f"ev_spray:{int(value)}"
    raise ValueError(f"unsupported policy spec for CLI: {spec!r}")


# --- per-flow orchestration -------------------------------------------------

@dataclass
class FlowRun:
    """One (src, dst, policy) triple resolved from a FlowSpec pair list."""
    src_host: str
    dst_host: str
    tenant: str
    src_id: int
    dst_id: int
    policy_cli: str
    rate_pps: int
    duration_s: float


def expand_flows(scenario: Scenario) -> list[FlowRun]:
    """Cartesian explode FlowSpec → one FlowRun per pair."""
    out: list[FlowRun] = []
    for fs in scenario.flows:
        try:
            cli_policy = policy_to_cli(fs.policy_spec)
        except (ValueError, NotImplementedError) as e:
            raise SystemExit(
                f"mrc/run.py: cannot translate policy {fs.policy_spec!r}: {e}"
            )
        for pair in fs.pairs:
            out.append(FlowRun(
                src_host=pair.src_host(),
                dst_host=pair.dst_host(),
                tenant=pair.tenant,
                src_id=pair.src,
                dst_id=pair.dst,
                policy_cli=cli_policy,
                rate_pps=fs.rate_pps,
                duration_s=fs.duration_s,
            ))
    return out


def _send_argv(flow: FlowRun) -> list[str]:
    return [
        "spray", "--role", "send",
        "--dst-id", str(flow.dst_id),
        "--rate", f"{flow.rate_pps}pps",
        "--duration", f"{flow.duration_s}s",
        "--policy", flow.policy_cli,
        "--json",
    ]


def _recv_argv(idle_timeout_s: float, *, mrc: bool) -> list[str]:
    argv = [
        "spray", "--role", "recv",
        "--idle-timeout", f"{idle_timeout_s}s",
        "--json",
    ]
    if mrc:
        argv.append("--mrc")
    return argv


def _scenario_env(mrc: MrcSpec | None,
                  paths_per_plane: int | None) -> dict[str, str] | None:
    """Build the env dict passed to docker exec for a scenario run.

    Bundles both MRC tunables (SRV6_MRC_CONFIG_JSON) and the EV-spray
    fan-out override (SRV6_PATHS_PER_PLANE). Returns None when neither
    knob is set, so non-MRC scenarios without EV-spray see no -e flags
    and the on-wire behavior is identical to pre-EV runs.

    paths_per_plane is propagated even when MRC is disabled, because
    EV-spray is a sender-side concern independent of the MRC probe/EV
    state machine.
    """
    env: dict[str, str] = {}
    if mrc is not None:
        env["SRV6_MRC_CONFIG_JSON"] = mrc.to_env_json()
    if paths_per_plane is not None:
        env["SRV6_PATHS_PER_PLANE"] = str(paths_per_plane)
    return env or None


# Backward-compat shim so external code (tests, etc.) that imports
# _mrc_env still works. New code should call _scenario_env directly.
def _mrc_env(mrc: MrcSpec | None) -> dict[str, str] | None:
    return _scenario_env(mrc, paths_per_plane=None)


def run_flows(flows: list[FlowRun], *,
              idle_timeout_s: float = RECV_IDLE_TIMEOUT_S,
              settle_s: float = RECEIVER_SETTLE_S,
              mrc: MrcSpec | None = None,
              paths_per_plane: int | None = None,
              verbose: bool = False) -> tuple[list[dict], list[dict]]:
    """Run all flows concurrently. Returns (sender_records, receiver_records).

    One receiver process per unique dst_host (multiple flows to the same
    host share a receiver). Senders are launched in parallel after a
    short settle delay.

    When `mrc` is set, receivers are launched with --mrc and both sides
    get SRV6_MRC_CONFIG_JSON set so the AgentConfig + EVStateConfig
    overrides from the scenario yaml take effect.
    """
    # Group flows by dst_host so we spawn exactly one receiver per host.
    dsts = sorted({f.dst_host for f in flows})

    # Total receiver lifetime upper bound: max flow duration + idle timeout
    # + generous slack. Used as the subprocess wait timeout.
    max_dur = max((f.duration_s for f in flows), default=0.0)
    recv_max_wait = max_dur + idle_timeout_s + 30.0

    env = _scenario_env(mrc, paths_per_plane)
    mrc_enabled = mrc is not None

    if verbose:
        suffix = "  (mrc enabled)" if mrc_enabled else ""
        print(f"  spawning {len(dsts)} receiver(s): {', '.join(dsts)}{suffix}")

    # Spawn all receivers first.
    recv_procs: dict[str, subprocess.Popen] = {}
    for dst in dsts:
        recv_procs[dst] = docker_exec_async(
            dst, _recv_argv(idle_timeout_s, mrc=mrc_enabled), env=env,
        )

    time.sleep(settle_s)

    # Launch senders in parallel.
    sender_records: list[dict] = []
    send_failures: list[str] = []

    def _do_send(flow: FlowRun) -> tuple[FlowRun, ExecResult]:
        return flow, docker_exec(flow.src_host, _send_argv(flow),
                                 timeout_s=flow.duration_s + 30.0,
                                 env=env)

    if verbose:
        print(f"  spawning {len(flows)} sender(s)")
    with cf.ThreadPoolExecutor(max_workers=max(1, len(flows))) as pool:
        for flow, res in pool.map(_do_send, flows):
            if res.rc != 0:
                send_failures.append(
                    f"sender {flow.src_host}->{flow.dst_host} rc={res.rc} "
                    f"stderr={res.stderr.strip()[:200]}"
                )
                continue
            try:
                sender_records.append(json.loads(res.stdout))
            except json.JSONDecodeError as e:
                send_failures.append(
                    f"sender {flow.src_host}->{flow.dst_host} bad JSON: {e}: "
                    f"stdout={res.stdout[:200]!r}"
                )

    # Wait for all receivers to self-exit on idle-timeout, then collect.
    receiver_records: list[dict] = []
    recv_failures: list[str] = []
    for dst, proc in recv_procs.items():
        try:
            out, err = proc.communicate(timeout=recv_max_wait)
        except subprocess.TimeoutExpired:
            proc.kill()
            out, err = proc.communicate()
            recv_failures.append(
                f"receiver on {dst} timed out (killed); stderr={err.strip()[:200]}"
            )
            continue
        if proc.returncode != 0:
            recv_failures.append(
                f"receiver on {dst} rc={proc.returncode} "
                f"stderr={err.strip()[:200]}"
            )
            continue
        # Receiver stdout is one JSON object on its own line.
        line = out.strip()
        if not line:
            recv_failures.append(f"receiver on {dst}: empty stdout")
            continue
        try:
            receiver_records.append(json.loads(line))
        except json.JSONDecodeError as e:
            recv_failures.append(
                f"receiver on {dst} bad JSON: {e}: stdout={line[:200]!r}"
            )

    if send_failures or recv_failures:
        for msg in send_failures + recv_failures:
            print(f"  ! {msg}", file=sys.stderr)
        # Continue rather than abort: partial results are useful.

    return sender_records, receiver_records


# --- fault application ------------------------------------------------------

def faults_for_netem(scenario: Scenario) -> list[Fault]:
    return [Fault(target=f.target, spec=f.spec) for f in scenario.faults]


# --- main pipeline ----------------------------------------------------------

def _ev_spray_n(policy_cli: str,
                scenario_ppp: int | None) -> int | None:
    """Resolve the effective paths_per_plane for an EV-spray flow.

    Returns None if the flow isn't EV-spray. Precedence matches the
    sender CLI: `ev_spray:N` > scenario.paths_per_plane > NUM_SPINES.
    """
    s = policy_cli.strip()
    if s.startswith("ev_spray:"):
        try:
            return int(s.split(":", 1)[1])
        except ValueError:
            return None
    if s == "ev_spray":
        return scenario_ppp if scenario_ppp is not None else NUM_SPINES
    return None


def _print_ev_preview(flows: list[FlowRun],
                      scenario_ppp: int | None) -> None:
    """Verbose-mode preview: for each EV-spray flow, print the spine
    subset and the resolved outer-DA per (plane, spine).

    Read-only: this just calls the same helpers the runner uses
    inside the sender. Useful for verifying the per-packet wire form
    BEFORE traffic flows, especially for yellow (where the 2-uSID
    outer is easy to miscount visually) and for confirming the
    hash-derived per-pair spine subset under paths_per_plane < N.

    No output for non-EV-spray scenarios (round_robin etc.).
    """
    ev_flows = [(f, _ev_spray_n(f.policy_cli, scenario_ppp))
                for f in flows]
    ev_flows = [(f, n) for f, n in ev_flows if n is not None]
    if not ev_flows:
        return

    print(f"  ev preview (paths_per_plane="
          f"{scenario_ppp if scenario_ppp is not None else NUM_SPINES}):")
    for flow, n in ev_flows:
        src_addr = inner_addr(flow.tenant, flow.src_id)
        dst_addr = inner_addr(flow.tenant, flow.dst_id)
        spines = select_spines_for_addrs(src_addr, dst_addr, n)
        ev_count = NUM_PLANES * len(spines)
        print(f"    {flow.src_host} -> {flow.dst_host}  "
              f"spines={list(spines)}  ev_count={ev_count}")
        # Spine-major round-robin: plane = seq % NUM_PLANES;
        # spine_idx = (seq // NUM_PLANES) % len(spines). Print one row
        # per (plane, spine) — the full EV set this flow will rotate
        # through, in transmit order so seq=k maps to the k-th row.
        for spine in spines:
            for plane in range(NUM_PLANES):
                outer = usid_outer_dst(flow.tenant, plane, spine,
                                       flow.dst_id)
                print(f"      P{plane}:S{spine}  {outer}")


def run_scenario(scenario: Scenario, *,
                 dry_run: bool = False,
                 verbose: bool = False) -> ScenarioReport:
    flows = expand_flows(scenario)

    if dry_run:
        print(f"DRY RUN scenario: {scenario.name}")
        print(f"  description: {scenario.description}")
        print(f"  mrc:")
        if scenario.mrc is None:
            print("    (disabled)")
        else:
            print(f"    enabled, env=SRV6_MRC_CONFIG_JSON={scenario.mrc.to_env_json()}")
        print(f"  flows:")
        for fr in flows:
            print(f"    {fr.src_host} -> {fr.dst_host}  "
                  f"policy={fr.policy_cli}  rate={fr.rate_pps}pps  "
                  f"dur={fr.duration_s}s")
        _print_ev_preview(flows, scenario.paths_per_plane)
        print(f"  faults:")
        if not scenario.faults:
            print("    (none)")
        for f in scenario.faults:
            print(f"    target={f.target!r}  spec={f.spec!r}")
        # Show the netem argv preview for free.
        nm = Netem(faults=faults_for_netem(scenario))
        try:
            argvs = nm.apply(dry_run=True)
            print(f"  netem argvs that would run:")
            for av in argvs:
                print(f"    {' '.join(shlex.quote(a) for a in av)}")
        except Exception as e:
            print(f"  netem dry-run failed: {e}")
        return ScenarioReport(scenario=scenario.name)

    # --- live run ---
    nm = Netem(faults=faults_for_netem(scenario))
    sender_records: list[dict] = []
    receiver_records: list[dict] = []

    if verbose:
        print(f"scenario: {scenario.name}")
        # Show the expected run wall-clock so users know whether to
        # wait or whether to start poking at the lab in another shell.
        # When flows have heterogeneous durations we report the max
        # (the run blocks until the longest flow finishes).
        if flows:
            max_dur = max(f.duration_s for f in flows)
            n_flows = len(flows)
            if all(f.duration_s == max_dur for f in flows):
                print(f"  duration: {max_dur:g}s ({n_flows} flow(s))")
            else:
                print(f"  duration: up to {max_dur:g}s "
                      f"({n_flows} flow(s), mixed durations)")
        if scenario.mrc is not None:
            print(f"  mrc: enabled (env={scenario.mrc.to_env_json()})")
        _print_ev_preview(flows, scenario.paths_per_plane)
        if scenario.faults:
            print(f"  applying {len(scenario.faults)} fault(s)...")

    nm.apply()
    try:
        if scenario.faults:
            time.sleep(FAULT_SETTLE_S)
        sender_records, receiver_records = run_flows(
            flows, mrc=scenario.mrc,
            paths_per_plane=scenario.paths_per_plane,
            verbose=verbose,
        )
    finally:
        try:
            nm.revert()
        except Exception as e:
            print(f"  ! revert failed: {e}", file=sys.stderr)

    paths_per_plane = (
        scenario.paths_per_plane
        if scenario.paths_per_plane is not None
        else NUM_SPINES
    )
    return ScenarioReport.from_records(
        scenario.name, sender_records, receiver_records,
        topology_dims=(NUM_PLANES, paths_per_plane),
    )


# --- CLI --------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="run an MRC scenario end-to-end",
    )
    p.add_argument("scenario", type=Path,
                   help="path to scenario YAML")
    p.add_argument("--dry-run", action="store_true",
                   help="print plan + netem argvs without touching lab")
    p.add_argument("--report", type=Path, default=None,
                   help="write JSON report to this path "
                        "(overrides scenario.report.out)")
    p.add_argument("--verbose", "-v", action="store_true",
                   help="extra progress prints during the run")
    args = p.parse_args(argv)

    try:
        scenario = from_yaml_file(args.scenario)
    except FileNotFoundError:
        print(f"mrc/run.py: scenario file not found: {args.scenario}",
              file=sys.stderr)
        return 2
    except Exception as e:
        print(f"mrc/run.py: failed to load scenario: {e}", file=sys.stderr)
        return 2

    try:
        report = run_scenario(scenario, dry_run=args.dry_run,
                              verbose=args.verbose)
    except KeyboardInterrupt:
        print("\nmrc/run.py: interrupted; faults may need manual revert.",
              file=sys.stderr)
        return 130

    if args.dry_run:
        return 0

    print(report.render_ascii())

    out_path = args.report
    if out_path is None and scenario.report.out:
        out_path = Path(scenario.report.out)
    if out_path is not None:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(report.to_json())
        print(f"  json report: {out_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
