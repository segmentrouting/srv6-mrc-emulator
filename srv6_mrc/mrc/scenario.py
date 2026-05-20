"""Scenario YAML validator.

Validates the full scenario shape laid out in mrc/README.md:

    name: <string>
    description: <string>            # optional
    flows:
      - pairs: <named-set | list of {tenant,src,dst}>
        policy: <policy-spec>        # passed to policy.policy_from_spec
        rate: <int|str>              # e.g. 1000 or "1000pps"
        duration: <str>              # e.g. "30s", "500ms"
    mrc:                             # optional; presence enables MRC
      probe_interval_ms: <int>       # all subkeys optional
      probe_timeout_ms: <int>
      loss_window_ms: <int>
      max_window_skew_ms: <int>
      probe_fail_threshold: <int>
      probe_recover_threshold: <int>
      loss_threshold: <float 0..1>
      loss_demote_consecutive: <int>
      min_active_evs: <int>
      rtt_ring_size: <int>
    faults:                          # optional
      - kind: netem
        target: <target-string>
        spec: <netem-spec-string>
    report:                          # optional
      out: <path>

The validator is intentionally strict — unknown keys raise. This catches
typos like `paris:` vs `pairs:` before a long lab run.

When the optional `mrc:` block is present, the orchestrator passes
--mrc to the receiver spray.py invocation and sets the
SRV6_MRC_CONFIG_JSON env var so spray.py picks up tunable overrides
(see scenario.MrcSpec.to_env_json). Senders auto-enable when their
per-flow `policy:` resolves to health_aware_mrc; the MrcSpec tunables
are additionally applied to the sender's AgentConfig + EVStateConfig
via the same env var. An empty mrc block (`mrc: {}`) is valid and
means "enable MRC, use all defaults" — the most common shape.

Output is a `Scenario` dataclass tree. Importing this module does NOT need
PyYAML; only `from_yaml_file()` / `from_yaml_string()` do, and they raise
a clean error if it's missing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..netem import normalize_spec, parse_target
from ..policy import policy_from_spec
from ..topo import NUM_LEAVES, NUM_SPINES, TENANTS, host_name


# --- public dataclasses -----------------------------------------------------

@dataclass(frozen=True)
class FlowPair:
    """One src/dst pair to be run as a flow."""
    tenant: str
    src: int
    dst: int

    def src_host(self) -> str:
        return host_name(self.tenant, self.src)

    def dst_host(self) -> str:
        return host_name(self.tenant, self.dst)


@dataclass(frozen=True)
class FlowSpec:
    """One `flows:` entry, expanded to its concrete pair list."""
    pairs: tuple[FlowPair, ...]
    policy_spec: Any                  # raw spec; runner calls policy_from_spec
    rate_pps: int
    duration_s: float
    # The original `policy:` value, kept verbatim so reports can show it
    # without re-encoding.
    policy_label: str = field(default="")


@dataclass(frozen=True)
class FaultSpec:
    """One `faults:` entry. Resolved-but-not-applied."""
    kind: str                          # currently only "netem"
    target: str
    spec: str


@dataclass(frozen=True)
class ReportSpec:
    out: str | None = None


# All MrcSpec fields are optional; absent values mean "use spray.py's
# built-in defaults" (DEFAULT_PROBE_INTERVAL_MS etc. in mrc/agent.py,
# and EVStateConfig() defaults in mrc/ev_state.py).
#
# These are deliberately split into two groups so the runner can build
# the right object on each side: AgentConfig is socket/thread cadence,
# EVStateConfig is the EV state-machine behavior. spray.py merges both
# from a single SRV6_MRC_CONFIG_JSON blob via env, so users author one
# `mrc:` block and don't need to know the split.
@dataclass(frozen=True)
class MrcSpec:
    # AgentConfig tunables (socket/thread cadence; milliseconds).
    probe_interval_ms: int | None = None
    probe_timeout_ms: int | None = None
    loss_window_ms: int | None = None
    max_window_skew_ms: int | None = None
    # EVStateConfig tunables (EV state machine).
    probe_fail_threshold: int | None = None
    probe_recover_threshold: int | None = None
    loss_threshold: float | None = None
    loss_demote_consecutive: int | None = None
    min_active_evs: int | None = None
    rtt_ring_size: int | None = None

    def to_env_json(self) -> str:
        """Encode for the SRV6_MRC_CONFIG_JSON env var consumed by
        spray.py. Only set fields are emitted so spray.py can layer
        them onto its dataclass defaults via field-by-field overrides.
        """
        import json
        payload: dict[str, Any] = {}
        for fname in (
            "probe_interval_ms", "probe_timeout_ms", "loss_window_ms",
            "max_window_skew_ms", "probe_fail_threshold",
            "probe_recover_threshold", "loss_threshold",
            "loss_demote_consecutive", "min_active_evs",
            "rtt_ring_size",
        ):
            v = getattr(self, fname)
            if v is not None:
                payload[fname] = v
        return json.dumps(payload, sort_keys=True)


@dataclass(frozen=True)
class Scenario:
    name: str
    description: str
    flows: tuple[FlowSpec, ...]
    faults: tuple[FaultSpec, ...]
    report: ReportSpec
    # None = MRC disabled for this scenario; an MrcSpec instance (even
    # with all-None fields) means "enable MRC, layer these tunables
    # over the defaults on both sender + receiver sides".
    mrc: MrcSpec | None = None
    # Override the default fan-out for EV-spray policies (ev_spray and
    # any future EV-aware variant). None = "use NUM_SPINES from topo"
    # (full fan-out). Otherwise 1..NUM_SPINES. Non-EV policies ignore
    # this field. Plumbed through to the spray policy at sender startup
    # by the spray CLI (via SRV6_PATHS_PER_PLANE env var or the
    # --paths-per-plane CLI flag).
    paths_per_plane: int | None = None


# --- named pair sets --------------------------------------------------------

# Mirrors the reference pairs in topo.REFERENCE_PAIRS_SPINES — same 8 pairs,
# laid out as 16 hosts. Useful as a one-liner `pairs: green-pairs-8` in YAMLs.
_REFERENCE_PAIRS = [
    (0, 15), (1, 14), (2, 13), (3, 12),
    (4, 11), (5, 10), (6, 9),  (7, 8),
]

# Same pattern for the smaller 2p-4x8 topology: 4 pairs out of 8 hosts.
# Spine assignment for these falls through to topo.spine_for()'s hash
# branch (the REFERENCE_PAIRS_SPINES table is 4p-8x16-specific).
_REFERENCE_PAIRS_4 = [
    (0, 7), (1, 6), (2, 5), (3, 4),
]

NAMED_PAIR_SETS: dict[str, list[FlowPair]] = {
    "green-pairs-8": [FlowPair("green", a, b) for a, b in _REFERENCE_PAIRS],
    "yellow-pairs-8": [FlowPair("yellow", a, b) for a, b in _REFERENCE_PAIRS],
    "green-pairs-4": [FlowPair("green", a, b) for a, b in _REFERENCE_PAIRS_4],
    "yellow-pairs-4": [FlowPair("yellow", a, b) for a, b in _REFERENCE_PAIRS_4],
    # One pair only — handy for smoke tests.
    "green-00-15": [FlowPair("green", 0, 15)],
    "yellow-00-15": [FlowPair("yellow", 0, 15)],
}


# --- collective-communication pair sets -------------------------------------

# Pair-set generators for emulating AI-collective traffic patterns. These
# are computed once at import time from the topology's host count
# (`NUM_LEAVES`), so they auto-size between 2p-4x8 (8 hosts) and 4p-8x16
# (16 hosts) without needing a per-topology alias name.
#
# all-to-all: every host sends to every other host (N*(N-1) ordered pairs).
#   Models the most fabric-stressful collective; every receiver sees N-1
#   concurrent senders (incast at the destination leaf's downlink).
#   16 hosts -> 240 flows; 8 hosts -> 56 flows.
#
# ring: each host i sends to host (i+1) mod N. Models one step of NCCL's
#   ring all-reduce (the dominant pattern in AI training). All N hosts
#   send simultaneously — this is the bandwidth-optimal collective
#   pattern, not a serial chain. A real all-reduce is 2(N-1) such steps
#   with the ring "rotated" so different chunks traverse different links;
#   we model one step with sustained traffic, which captures the per-link
#   load characteristic without the orchestration overhead.

def _all_to_all_pairs(tenant: str) -> list[FlowPair]:
    """Every ordered (src, dst) with src != dst, in (src, dst) order."""
    n = NUM_LEAVES
    return [
        FlowPair(tenant, i, j)
        for i in range(n)
        for j in range(n)
        if i != j
    ]


def _ring_pairs(tenant: str) -> list[FlowPair]:
    """Each host i sends to host (i+1) mod N. NCCL-style unidirectional ring."""
    n = NUM_LEAVES
    return [FlowPair(tenant, i, (i + 1) % n) for i in range(n)]


def _reference_pairs(tenant: str) -> list[FlowPair]:
    """Mirror-pattern pairs i <-> (N-1-i), N/2 pairs total.

    Auto-sizes from NUM_LEAVES so the same alias `<tenant>-pairs` works
    on every topology size:
      8 hosts  -> 4 pairs:  (0,7), (1,6), (2,5), (3,4)
      16 hosts -> 8 pairs:  (0,15), (1,14), ..., (7,8)
    On 4p-8x16 the resulting spine assignments line up with
    topo.REFERENCE_PAIRS_SPINES (so per-pair spine selection is
    deterministic and matches the reference design).
    """
    n = NUM_LEAVES
    return [FlowPair(tenant, i, n - 1 - i) for i in range(n // 2)]


for _tenant in TENANTS:
    NAMED_PAIR_SETS[f"{_tenant}-all-to-all"] = _all_to_all_pairs(_tenant)
    NAMED_PAIR_SETS[f"{_tenant}-ring"] = _ring_pairs(_tenant)
    NAMED_PAIR_SETS[f"{_tenant}-pairs"] = _reference_pairs(_tenant)
del _tenant


# --- error type -------------------------------------------------------------

class ScenarioError(ValueError):
    """Raised for any malformed scenario. Always includes the dotted path."""

    def __init__(self, path: str, msg: str) -> None:
        super().__init__(f"{path}: {msg}")
        self.path = path


# --- top-level entry --------------------------------------------------------

def validate(doc: Any) -> Scenario:
    """Validate a parsed YAML document and return a Scenario tree.

    Raises ScenarioError on the first problem.
    """
    if not isinstance(doc, dict):
        raise ScenarioError("$", "scenario must be a mapping at top level")

    _require_keys(doc, "$", required={"name", "flows"},
                  optional={"description", "faults", "report", "mrc",
                            "paths_per_plane"})

    name = _require_str(doc, "$.name")
    description = _opt_str(doc, "$.description", default="")

    flows_raw = doc["flows"]
    if not isinstance(flows_raw, list) or not flows_raw:
        raise ScenarioError("$.flows", "must be a non-empty list")
    flows = tuple(_validate_flow(item, f"$.flows[{i}]")
                  for i, item in enumerate(flows_raw))

    faults_raw = doc.get("faults") or []
    if not isinstance(faults_raw, list):
        raise ScenarioError("$.faults", "must be a list if present")
    faults = tuple(_validate_fault(item, f"$.faults[{i}]")
                   for i, item in enumerate(faults_raw))

    report = _validate_report(doc.get("report"), "$.report")
    mrc = _validate_mrc(doc.get("mrc"), "$.mrc") if "mrc" in doc else None

    paths_per_plane = _validate_paths_per_plane(
        doc.get("paths_per_plane"), "$.paths_per_plane"
    )

    return Scenario(
        name=name,
        description=description,
        flows=flows,
        faults=faults,
        report=report,
        mrc=mrc,
        paths_per_plane=paths_per_plane,
    )


def from_yaml_string(s: str) -> Scenario:
    yaml = _load_pyyaml()
    return validate(yaml.safe_load(s))


def from_yaml_file(path: str | Path) -> Scenario:
    yaml = _load_pyyaml()
    with open(path, "r") as f:
        return validate(yaml.safe_load(f))


# --- flow ------------------------------------------------------------------

def _validate_flow(item: Any, path: str) -> FlowSpec:
    if not isinstance(item, dict):
        raise ScenarioError(path, "must be a mapping")
    _require_keys(item, path,
                  required={"pairs", "policy", "rate", "duration"})

    pairs = _resolve_pairs(item["pairs"], f"{path}.pairs")
    policy_raw = item["policy"]
    # Validate policy spec by attempting to build it — but don't keep
    # the instance (FlowSpec stores the raw spec for runner-side rebuild).
    try:
        policy_from_spec(policy_raw)
    except ValueError as e:
        raise ScenarioError(f"{path}.policy", str(e)) from None

    rate_pps = _parse_rate(item["rate"], f"{path}.rate")
    duration_s = _parse_duration(item["duration"], f"{path}.duration")

    return FlowSpec(
        pairs=pairs,
        policy_spec=policy_raw,
        rate_pps=rate_pps,
        duration_s=duration_s,
        policy_label=_policy_label(policy_raw),
    )


def _resolve_pairs(value: Any, path: str) -> tuple[FlowPair, ...]:
    if isinstance(value, str):
        named = NAMED_PAIR_SETS.get(value)
        if named is None:
            raise ScenarioError(
                path,
                f"unknown named pair set {value!r}; known: "
                f"{sorted(NAMED_PAIR_SETS)}",
            )
        return tuple(named)
    if isinstance(value, list):
        if not value:
            raise ScenarioError(path, "pair list is empty")
        out: list[FlowPair] = []
        for i, entry in enumerate(value):
            ep = f"{path}[{i}]"
            if not isinstance(entry, dict):
                raise ScenarioError(ep, "pair entry must be a mapping")
            _require_keys(entry, ep, required={"tenant", "src", "dst"})
            tenant = _require_choice(entry, f"{ep}.tenant", TENANTS)
            src = _require_host_id(entry, f"{ep}.src")
            dst = _require_host_id(entry, f"{ep}.dst")
            if src == dst:
                raise ScenarioError(ep, "src and dst must differ")
            out.append(FlowPair(tenant=tenant, src=src, dst=dst))
        return tuple(out)
    raise ScenarioError(path, "must be a named set string or list of {tenant,src,dst}")


def _policy_label(spec: Any) -> str:
    if isinstance(spec, str):
        return spec
    if isinstance(spec, dict) and len(spec) == 1:
        (k, v), = spec.items()
        return f"{k}({_policy_label(v) if not isinstance(v, list) else 'weights'})"
    return repr(spec)


# --- fault -----------------------------------------------------------------

def _validate_fault(item: Any, path: str) -> FaultSpec:
    if not isinstance(item, dict):
        raise ScenarioError(path, "must be a mapping")
    _require_keys(item, path, required={"kind", "target", "spec"})
    kind = _require_str(item, f"{path}.kind")
    if kind != "netem":
        raise ScenarioError(f"{path}.kind",
                            f"unsupported fault kind {kind!r}; only 'netem' is implemented")
    target = _require_str(item, f"{path}.target")
    spec = _require_str(item, f"{path}.spec")
    # Validate by parsing — surfaces typos at scenario-load time.
    try:
        parse_target(target)
    except ValueError as e:
        raise ScenarioError(f"{path}.target", str(e)) from None
    try:
        normalize_spec(spec)
    except ValueError as e:
        raise ScenarioError(f"{path}.spec", str(e)) from None
    return FaultSpec(kind=kind, target=target, spec=spec)


# --- report ----------------------------------------------------------------

def _validate_report(value: Any, path: str) -> ReportSpec:
    if value is None:
        return ReportSpec()
    if not isinstance(value, dict):
        raise ScenarioError(path, "must be a mapping if present")
    _require_keys(value, path, required=set(), optional={"out"})
    out = value.get("out")
    if out is not None and not isinstance(out, str):
        raise ScenarioError(f"{path}.out", "must be a string path")
    return ReportSpec(out=out)


def _validate_paths_per_plane(value: Any, path: str) -> int | None:
    """Validate top-level scenario `paths_per_plane: <int>` if present.

    Returns None when absent (means "use NUM_SPINES at runtime"); raises
    ScenarioError on a malformed value. Range is checked against the
    *current* NUM_SPINES — a scenario with paths_per_plane=8 will fail
    to load on a 2p-4x8 topology (NUM_SPINES=4), which is the right
    behavior: the YAML is topology-specific.
    """
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ScenarioError(path, "must be an integer")
    if not (1 <= value <= NUM_SPINES):
        raise ScenarioError(
            path,
            f"must be 1..{NUM_SPINES} (NUM_SPINES on this topology), "
            f"got {value}"
        )
    return value


# --- mrc -------------------------------------------------------------------

# Allowed mrc: subkeys + their per-field validator. Order kept stable so
# error messages list them deterministically.
_MRC_POSITIVE_INT_FIELDS = (
    "probe_interval_ms", "probe_timeout_ms", "loss_window_ms",
    "max_window_skew_ms", "probe_fail_threshold",
    "probe_recover_threshold", "loss_demote_consecutive",
    "min_active_evs", "rtt_ring_size",
)
_MRC_RATIO_FIELDS = ("loss_threshold",)
_MRC_OPTIONAL = set(_MRC_POSITIVE_INT_FIELDS) | set(_MRC_RATIO_FIELDS)


def _validate_mrc(value: Any, path: str) -> MrcSpec:
    """Validate the optional `mrc:` block.

    None: caller decides (we never see None — the top-level validator
    only calls us when the key is present).
    Empty dict (`mrc: {}`): valid; all-None MrcSpec means "enable MRC
    using the spray.py-side defaults".
    Otherwise: every present subkey must be a known tunable with a
    type-appropriate value.
    """
    if value is None:
        # `mrc:` with no value -> YAML gives us None; treat as empty
        # block (enabled, all defaults).
        return MrcSpec()
    if not isinstance(value, dict):
        raise ScenarioError(path, "must be a mapping if present")
    _require_keys(value, path, required=set(), optional=_MRC_OPTIONAL)

    kwargs: dict[str, Any] = {}
    for fname in _MRC_POSITIVE_INT_FIELDS:
        if fname in value:
            v = value[fname]
            if not isinstance(v, int) or isinstance(v, bool) or v <= 0:
                raise ScenarioError(
                    f"{path}.{fname}",
                    f"must be a positive int, got {v!r}",
                )
            kwargs[fname] = v
    for fname in _MRC_RATIO_FIELDS:
        if fname in value:
            v = value[fname]
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                raise ScenarioError(
                    f"{path}.{fname}",
                    f"must be a number in [0.0, 1.0], got {v!r}",
                )
            if not 0.0 <= float(v) <= 1.0:
                raise ScenarioError(
                    f"{path}.{fname}",
                    f"must be in [0.0, 1.0], got {v}",
                )
            kwargs[fname] = float(v)
    return MrcSpec(**kwargs)


# --- primitive helpers ------------------------------------------------------

def _require_keys(d: dict, path: str, *,
                  required: set[str],
                  optional: set[str] | None = None) -> None:
    keys = set(d.keys())
    missing = required - keys
    if missing:
        raise ScenarioError(path, f"missing required key(s): {sorted(missing)}")
    allowed = required | (optional or set())
    extra = keys - allowed
    if extra:
        raise ScenarioError(path, f"unknown key(s): {sorted(extra)}")


def _require_str(d: dict, path: str) -> str:
    leaf = path.rsplit(".", 1)[-1]
    v = d[leaf]
    if not isinstance(v, str) or not v:
        raise ScenarioError(path, "must be a non-empty string")
    return v


def _opt_str(d: dict, path: str, *, default: str) -> str:
    leaf = path.rsplit(".", 1)[-1]
    v = d.get(leaf, default)
    if not isinstance(v, str):
        raise ScenarioError(path, "must be a string if present")
    return v


def _require_choice(d: dict, path: str, choices) -> str:
    leaf = path.rsplit(".", 1)[-1]
    v = d[leaf]
    if v not in choices:
        raise ScenarioError(path, f"must be one of {tuple(choices)}, got {v!r}")
    return v


def _require_host_id(d: dict, path: str) -> int:
    leaf = path.rsplit(".", 1)[-1]
    v = d[leaf]
    if not isinstance(v, int) or not 0 <= v < NUM_LEAVES:
        raise ScenarioError(
            path, f"must be int 0..{NUM_LEAVES - 1}, got {v!r}"
        )
    return v


_RATE_RE = re.compile(r"^\s*(\d+)\s*(?:pps?)?\s*$", re.I)


def _parse_rate(v: Any, path: str) -> int:
    if isinstance(v, int) and v > 0:
        return v
    if isinstance(v, str):
        m = _RATE_RE.match(v)
        if m:
            return int(m.group(1))
    raise ScenarioError(path, f"must be a positive int or '<N>pps', got {v!r}")


_DUR_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(ms|s)?\s*$", re.I)


def _parse_duration(v: Any, path: str) -> float:
    if isinstance(v, (int, float)) and v > 0:
        return float(v)
    if isinstance(v, str):
        m = _DUR_RE.match(v)
        if m:
            val = float(m.group(1))
            return val / 1000.0 if (m.group(2) or "").lower() == "ms" else val
    raise ScenarioError(path, f"must be a duration like '30s' or '500ms', got {v!r}")


def _load_pyyaml():
    try:
        import yaml  # type: ignore
    except ImportError as e:
        raise RuntimeError(
            "PyYAML not installed; needed by scenario.from_yaml_*. "
            "Install with: pip3 install pyyaml"
        ) from e
    return yaml
