"""srctl — the SR-fabric emulator control CLI.

Kubectl-shaped command surface for inspecting topology, listing the EV
grid for a host pair, and running scenarios. Subcommands are dispatched
by argparse subparsers; output format is human-readable by default with
`-o json|yaml` available on the `get` family.

Subcommand surface (v1):

    srctl get topology
    srctl get hosts [--tenant T]
    srctl get evs <src-host> <dst-host> [-n N] [-o table|json|yaml|sid] [--sid uA|uN]
    srctl run <scenario> [--verbose] [--dry-run] [--sid uA|uN]
    srctl run --list

`--sid` selects the outer uSID construction: `uA` (default) uses
per-adjacency SIDs that force each fabric hop onto one specific
physical link; `uN` uses each hop's own node locator instead, letting
the underlay's (already-provisioned) static routes pick the link. See
`srv6_mrc.topo.usid_outer_dst` for the addressing detail.

`<scenario>` is resolved by name against the active topology's
`scenarios/` directory: `srctl run green-mrc-ev-spray` finds
`topologies/<topo>/scenarios/green-mrc-ev-spray.yaml`. The active
topology is inferred from `$SRV6_TOPO` (same precedence as
`srv6_mrc.topo._load_topo`).

Console-script entry point: `srctl = srv6_mrc.cli.srctl:main` in
pyproject.toml.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Iterable

from srv6_mrc import fault as fault_module
from srv6_mrc.netem import Netem, Fault as NetemFault


def _infer_srv6_topo_from_argv() -> None:
    """Set SRV6_TOPO before importing srv6_mrc.topo. Idempotent: does
    nothing if SRV6_TOPO is already set.

    Strategy depends on the subcommand:
      - `srctl run <scenario>`: derive topology from the scenario path
        or name (see implementation below).
      - everything else (`srctl get ...`): fall back to host-count
        sentinel detection only — pick the unique deployed topology
        whose `leaves_per_plane` matches the running container set.

    The fallback ensures `srctl get evs <a> <b>` shows the correct
    NUM_SPINES / NUM_PLANES for the deployed topology, not the
    hardcoded dev default.
    """
    if os.environ.get("SRV6_TOPO"):
        return
    argv = sys.argv[1:]

    here = Path(__file__).resolve()
    repo_root = here.parent.parent.parent
    topos_dir = repo_root / "topologies"

    # `srctl run`: try scenario-path / scenario-name disambiguation first.
    if argv and argv[0] == "run":
        scen_arg: str | None = None
        for a in argv[1:]:
            if a.startswith("-"):
                continue
            scen_arg = a
            break
        if scen_arg:
            # Explicit path: derive topo from <topo>/scenarios/<scen>.yaml.
            p = Path(scen_arg)
            if p.is_file():
                try:
                    topo_yaml = p.resolve().parents[1] / "topo.yaml"
                    if topo_yaml.is_file():
                        os.environ["SRV6_TOPO"] = str(topo_yaml)
                        return
                except IndexError:
                    pass
            # Bare name: scan topologies/*/scenarios/<scen>.yaml.
            if topos_dir.is_dir():
                hits = list(topos_dir.glob(f"*/scenarios/{scen_arg}.yaml"))
                if len(hits) == 1:
                    os.environ["SRV6_TOPO"] = str(
                        hits[0].resolve().parents[1] / "topo.yaml"
                    )
                    return
                # If multiple match, fall through to host-count sentinel.

    # Fall through (any subcommand including `get`): identify the
    # deployed topology by host-count sentinel. This makes `srctl get
    # ...` reflect the live topology size automatically.
    _select_topo_by_host_sentinel(topos_dir)


def _select_topo_by_host_sentinel(topos_dir: Path) -> None:
    """Pick the unique deployed topology among all topologies/*/topo.yaml
    by checking which one's expected highest-numbered host container
    exists (and the next one above does not).

    Containerlab in this repo is configured with prefix:"" so container
    names are short-form (yellow-host00, not clab-<topo>-yellow-host00).
    The host-count sentinel is the cheapest way to discriminate.
    """
    if not topos_dir.is_dir():
        return
    try:
        import subprocess
        out = subprocess.run(
            ["docker", "ps", "--format", "{{.Names}}"],
            capture_output=True, text=True, timeout=2, check=False,
        )
        running = set(out.stdout.split())
    except Exception:
        return
    if not running:
        return
    try:
        import yaml  # type: ignore[import-not-found]
    except ImportError:
        return
    # Two-axis sentinel: a candidate matches iff
     #   (a) yellow-host<L-1> exists and yellow-host<L> does NOT
     #       (uniquely identifies leaves_per_plane), AND
     #   (b) p<P-1>-spine0 exists and p<P>-spine0 does NOT
     #       (uniquely identifies num_planes).
    # Sharing leaves_per_plane (e.g. 2p-4x8 vs 4p-4x8 both have 8
    # leaves) is disambiguated by axis (b); sharing num_planes alone
    # by axis (a).
    live: list[Path] = []
    skip_reasons: list[str] = []
    for cand in topos_dir.glob("*/topo.yaml"):
        try:
            with open(cand) as f:
                t = yaml.safe_load(f)
            n = int(t["leaves_per_plane"])
            p = int(t["planes"])
            host_in = f"yellow-host{n - 1:02d}"
            host_out = f"yellow-host{n:02d}"
            plane_in = f"p{p - 1}-spine00"
            plane_out = f"p{p}-spine00"
            if (
                host_in in running and host_out not in running
                and plane_in in running and plane_out not in running
            ):
                live.append(cand)
        except Exception as e:  # noqa: BLE001 - sentinel must never crash CLI
            skip_reasons.append(f"{cand}: {type(e).__name__}: {e}")
            continue
    if len(live) == 1:
        os.environ["SRV6_TOPO"] = str(live[0])
        return
    # Diagnostic: if we got here without setting SRV6_TOPO and any
    # candidate raised, surface the reasons. Schema drift in topo.yaml
    # otherwise gets silently swallowed (this exact scenario hid a
    # KeyError on 'num_planes' through two commits). One-line warning
    # to stderr is loud enough to notice without breaking scripts.
    if skip_reasons and not os.environ.get("SRV6_TOPO"):
        sys.stderr.write(
            "srctl: topology auto-detect skipped candidates ("
            + "; ".join(skip_reasons)
            + "); falling back to default. Set SRV6_TOPO to override.\n"
        )


_infer_srv6_topo_from_argv()

from srv6_mrc import topo as _topo


# --- helpers ----------------------------------------------------------------


_HOST_RE = re.compile(r"^([a-z]+)-host(\d{1,3})$")


def _parse_host(name: str) -> tuple[str, int]:
    """Parse "<tenant>-host<NN>" -> (tenant, host_id).

    Raises ValueError with a user-readable message on malformed input
    or out-of-range host id. Tenant validity is checked against the
    topology's TENANTS registry.
    """
    m = _HOST_RE.match(name)
    if not m:
        raise ValueError(
            f"host name {name!r} not in form '<tenant>-host<NN>' "
            f"(e.g. green-host00, yellow-host15)"
        )
    tenant, hid_str = m.group(1), m.group(2)
    if tenant not in _topo.TENANTS:
        raise ValueError(
            f"tenant {tenant!r} in {name!r} not in topology registry "
            f"{sorted(_topo.TENANTS)}"
        )
    hid = int(hid_str)
    if not (0 <= hid < _topo.NUM_LEAVES):
        raise ValueError(
            f"host id {hid} in {name!r} out of range "
            f"0..{_topo.NUM_LEAVES - 1}"
        )
    return tenant, hid


def _active_topo_dir() -> Path | None:
    """Locate the topology directory (the one containing topo.yaml + scenarios/).

    Order of precedence mirrors srv6_mrc.topo._load_topo:
      1. $SRV6_TOPO/.. (parent of the topo.yaml file)
      2. <repo>/topologies/4p-4x8/ (development default)

    Returns None if neither path resolves to a directory; callers must
    handle (e.g., `srctl run --list` falls back to a clear error).
    """
    env = os.environ.get("SRV6_TOPO")
    if env:
        p = Path(env).resolve().parent
        return p if p.is_dir() else None
    here = Path(__file__).resolve()
    p = here.parent.parent.parent / "topologies" / "4p-4x8"
    return p if p.is_dir() else None


def _render_table(rows: list[dict[str, Any]],
                  columns: list[str]) -> str:
    """Render a list of dict rows as a fixed-width ASCII table.

    Columns is the ordered list of dict keys to project. Each column's
    width is sized to the wider of the header or the longest value in
    that column. Right-padded by two spaces between columns (kubectl
    convention).
    """
    if not rows:
        return "  ".join(columns) + "\n  (no rows)"
    widths = {c: len(c) for c in columns}
    for r in rows:
        for c in columns:
            v = str(r.get(c, ""))
            if len(v) > widths[c]:
                widths[c] = len(v)
    lines = []
    lines.append("  ".join(c.upper().ljust(widths[c]) for c in columns))
    for r in rows:
        lines.append("  ".join(
            str(r.get(c, "")).ljust(widths[c]) for c in columns))
    return "\n".join(lines)


def _emit(rows: Iterable[dict[str, Any]] | dict[str, Any],
          columns: list[str] | None,
          fmt: str) -> str:
    """Serialize a result set in the requested format.

    `rows` may be a list of dicts (tabular) or a single dict (scalar
    facts like `get topology`). `columns` only matters for fmt='table'.
    """
    if fmt == "json":
        return json.dumps(rows, indent=2, default=str)
    if fmt == "yaml":
        try:
            import yaml  # type: ignore[import-not-found]
        except ImportError:
            print("srctl: PyYAML not installed; falling back to JSON",
                  file=sys.stderr)
            return json.dumps(rows, indent=2, default=str)
        return yaml.safe_dump(rows, sort_keys=False)
    # table / default
    if isinstance(rows, dict):
        # Single scalar record: render as key: value pairs.
        return "\n".join(f"{k}: {v}" for k, v in rows.items())
    assert columns is not None, "_emit(table) requires columns"
    return _render_table(list(rows), columns)


# --- subcommands: get -------------------------------------------------------


def _cmd_get_topology(args: argparse.Namespace) -> int:
    """`srctl get topology` — print the active topology's dimensions."""
    info: dict[str, Any] = {
        "name": _topo._TOPO.get("name", "<unknown>"),
        "planes": _topo.NUM_PLANES,
        "spines_per_plane": _topo.NUM_SPINES,
        "leaves_per_plane": _topo.NUM_LEAVES,
        "tenants": list(_topo.TENANTS),
        "clab_topology_name": _topo.CLAB_TOPOLOGY_NAME,
    }
    if args.output in ("json", "yaml"):
        print(_emit(info, None, args.output))
    else:
        print(_emit(info, None, "table"))
    return 0


def _cmd_get_hosts(args: argparse.Namespace) -> int:
    """`srctl get hosts [--tenant T]` — list all hosts for the topology."""
    tenants = (args.tenant,) if args.tenant else _topo.TENANTS
    for t in tenants:
        if t not in _topo.TENANTS:
            print(f"srctl: unknown tenant {t!r}; "
                  f"known: {sorted(_topo.TENANTS)}", file=sys.stderr)
            return 2

    rows: list[dict[str, Any]] = []
    for t in tenants:
        for hid in range(_topo.NUM_LEAVES):
            rows.append({
                "host": _topo.host_name(t, hid),
                "tenant": t,
                "inner_addr": _topo.inner_addr(t, hid),
            })
    print(_emit(rows, ["host", "tenant", "inner_addr"], args.output))
    return 0


def _cmd_get_evs(args: argparse.Namespace) -> int:
    """`srctl get evs <src> <dst> [-n N] [-o ...]` — list EVs for a pair.

    EVs are derived from `select_spines(src_id, dst_id, n)` × all planes,
    so the result is `NUM_PLANES * n` entries. Each EV's outer-DA SID is
    computed via `usid_outer_dst(tenant, plane, spine, dst_id, sid_mode)`.

    Cross-tenant pairs are rejected (each tenant has its own SID space;
    a green→yellow flow has no defined outer SID).
    """
    try:
        src_tenant, src_id = _parse_host(args.src_host)
        dst_tenant, dst_id = _parse_host(args.dst_host)
    except ValueError as e:
        print(f"srctl: {e}", file=sys.stderr)
        return 2

    if src_tenant != dst_tenant:
        print(
            f"srctl: cross-tenant pair not supported "
            f"({src_tenant} -> {dst_tenant}); EVs are scoped to one tenant",
            file=sys.stderr,
        )
        return 2

    tenant = src_tenant
    n = args.n if args.n is not None else _topo.NUM_SPINES
    if not (1 <= n <= _topo.NUM_SPINES):
        print(f"srctl: -n must be 1..{_topo.NUM_SPINES}, got {n}",
              file=sys.stderr)
        return 2

    spines = _topo.select_spines(src_id, dst_id, n)

    if args.output == "sid":
        # Legacy "P<plane>:S<spine>  <sid>" indented format (matches the
        # ev-spray scenario printout the user already knows).
        print(f"{args.src_host} -> {args.dst_host}")
        for spine in spines:
            for plane in range(_topo.NUM_PLANES):
                sid = _topo.usid_outer_dst(tenant, plane, spine, dst_id,
                                           sid_mode=args.sid)
                print(f"      P{plane}:S{spine}  {sid}")
        return 0

    rows: list[dict[str, Any]] = []
    for spine in spines:
        for plane in range(_topo.NUM_PLANES):
            sid = _topo.usid_outer_dst(tenant, plane, spine, dst_id,
                                       sid_mode=args.sid)
            rows.append({
                "plane": plane,
                "path": spine,
                "ev": f"P{plane}:S{spine}",
                "sid": sid,
            })
    # Sort by (plane, path) for stable kubectl-like output. The native
    # `select_spines` order is the round-robin walk order, which is
    # useful in -o sid mode but jarring in a sorted table.
    rows.sort(key=lambda r: (r["plane"], r["path"]))
    print(_emit(rows, ["plane", "path", "ev", "sid"], args.output))
    return 0


# --- subcommands: run -------------------------------------------------------


def _list_scenarios() -> list[Path]:
    """Enumerate scenario YAMLs in the active topology directory."""
    topo_dir = _active_topo_dir()
    if topo_dir is None:
        return []
    scen_dir = topo_dir / "scenarios"
    if not scen_dir.is_dir():
        return []
    return sorted(scen_dir.glob("*.yaml"))


def _resolve_scenario(name_or_path: str) -> Path | None:
    """Resolve a scenario reference to a YAML path.

    Accepts:
      - bare name: 'green-mrc-baseline' → '<topo>/scenarios/green-mrc-baseline.yaml'
      - explicit path: 'topologies/foo/scenarios/bar.yaml' (must exist)
    Returns None if nothing resolves.
    """
    # Explicit path wins if the user pointed at a file directly.
    p = Path(name_or_path)
    if p.is_file():
        return p
    # Bare name: look in active topology's scenarios/.
    topo_dir = _active_topo_dir()
    if topo_dir is None:
        return None
    candidate = topo_dir / "scenarios" / f"{name_or_path}.yaml"
    return candidate if candidate.is_file() else None


def _cmd_run(args: argparse.Namespace) -> int:
    """`srctl run <scenario>` — execute a scenario by name.

    `srctl run --list` prints available scenarios and exits 0 without
    running anything. Otherwise the scenario name is resolved to a YAML
    path and `srv6_mrc.mrc.run.main` is invoked with the resolved
    arguments, returning its exit code.
    """
    if args.list:
        scens = _list_scenarios()
        if not scens:
            topo_dir = _active_topo_dir()
            hint = (f"no scenarios under {topo_dir}/scenarios"
                    if topo_dir else "could not locate topology directory")
            print(f"srctl: {hint}", file=sys.stderr)
            return 1
        for p in scens:
            print(p.stem)
        return 0

    if args.scenario is None:
        print("srctl: 'run' requires <scenario> or --list", file=sys.stderr)
        return 2

    yaml_path = _resolve_scenario(args.scenario)
    if yaml_path is None:
        avail = ", ".join(p.stem for p in _list_scenarios())
        print(f"srctl: scenario {args.scenario!r} not found; "
              f"available: {avail or '(none)'}", file=sys.stderr)
        return 2

    # Delegate to srv6_mrc.mrc.run.main. Import lazily so `srctl get …`
    # doesn't pay the cost of importing the orchestrator.
    from srv6_mrc.mrc.run import main as run_main
    forwarded = [str(yaml_path)]
    if args.verbose:
        forwarded.append("--verbose")
    if args.dry_run:
        forwarded.append("--dry-run")
    if args.duration is not None:
        forwarded.extend(["--duration", args.duration])
    if args.sid is not None:
        forwarded.extend(["--sid", args.sid])
    return run_main(forwarded)


# --- fault commands ---------------------------------------------------------


def _cmd_fault_shutdown(args: argparse.Namespace) -> int:
    """Handle 'srctl fault shutdown' command."""
    node = args.node
    interfaces = args.interfaces
    unidirectional = args.unidirectional
    
    # Load fault state
    state = fault_module.FaultState.load()
    topo_links = fault_module.TopologyLinks()
    
    # Handle "all" keyword
    if len(interfaces) == 1 and interfaces[0].lower() == "all":
        interfaces = topo_links.get_all_interfaces(node)
        if not interfaces:
            print(f"Error: No interfaces found for node {node}", file=sys.stderr)
            return 1
        print(f"Shutting down all {len(interfaces)} interfaces on {node}")
    
    # Validate interfaces
    for iface in interfaces:
        if not iface.startswith("Ethernet"):
            print(f"Error: Interface must be in SONiC format (EthernetN), got {iface!r}", file=sys.stderr)
            return 1
    
    # Build target list (with peers if bidirectional)
    targets = []
    for iface in interfaces:
        targets.append(fault_module.InterfaceEndpoint(node, iface))
        
        if not unidirectional:
            peer = topo_links.get_peer(node, iface)
            if peer:
                peer_node, peer_iface = peer
                targets.append(fault_module.InterfaceEndpoint(peer_node, peer_iface))
                print(f"  {node}:{iface} ↔ {peer_node}:{peer_iface} (bidirectional)")
            else:
                print(f"  {node}:{iface} (no peer found, unidirectional)")
        else:
            print(f"  {node}:{iface} (unidirectional)")
    
    # Apply shutdowns
    for target in targets:
        try:
            fault_module.shutdown_interface(target.node, target.interface)
        except RuntimeError as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1
    
    # Record fault
    fault_id = fault_module.generate_fault_id(state)
    fault = fault_module.Fault(
        id=fault_id,
        type="shutdown",
        targets=targets,
        spec="down",
        bidirectional=not unidirectional,
    )
    state.add_fault(fault)
    state.save()
    
    print(f"Fault {fault_id} applied: shutdown {len(targets)} interface(s)")
    return 0


def _cmd_fault_netem(args: argparse.Namespace) -> int:
    """Handle 'srctl fault netem' command."""
    target_str = args.target
    spec = args.spec
    
    # Delegate to existing netem module
    try:
        netem_fault = NetemFault(target=target_str, spec=spec)
        netem = Netem(faults=[netem_fault])
        netem.apply()
    except Exception as e:
        print(f"Error applying netem fault: {e}", file=sys.stderr)
        return 1
    
    # Record fault in state
    state = fault_module.FaultState.load()
    fault_id = fault_module.generate_fault_id(state)
    
    # netem targets are host-side veths, not fabric interfaces
    # Store as a single pseudo-target with the target string
    targets = [fault_module.InterfaceEndpoint(node="netem", interface=target_str)]
    
    fault = fault_module.Fault(
        id=fault_id,
        type="netem",
        targets=targets,
        spec=spec,
        bidirectional=False,  # netem is host-side only
    )
    state.add_fault(fault)
    state.save()
    
    print(f"Fault {fault_id} applied: netem '{target_str}' with spec '{spec}'")
    return 0


def _cmd_fault_clear(args: argparse.Namespace) -> int:
    """Handle 'srctl fault clear' command."""
    state = fault_module.FaultState.load()
    
    if args.all:
        # Clear all faults
        if not state.faults:
            print("No active faults to clear")
            return 0
        
        for fault in state.faults:
            _revert_fault(fault)
        
        count = state.clear_all()
        state.save()
        print(f"Cleared {count} fault(s)")
        return 0
    
    if args.node:
        # Clear faults on specific node (and optionally specific interface)
        if args.interface:
            faults = state.find_by_interface(args.node, args.interface)
        else:
            faults = state.find_by_node(args.node)
        
        if not faults:
            print(f"No faults found for {args.node}" + (f":{args.interface}" if args.interface else ""))
            return 0
        
        for fault in faults:
            _revert_fault(fault)
            state.remove_fault(fault.id)
        
        state.save()
        print(f"Cleared {len(faults)} fault(s)")
        return 0
    
    print("Error: Must specify --all or a node name", file=sys.stderr)
    return 1


def _revert_fault(fault: fault_module.Fault) -> None:
    """Revert a single fault (bring interfaces back up or remove netem)."""
    if fault.type == "shutdown":
        for target in fault.targets:
            try:
                fault_module.startup_interface(target.node, target.interface)
                print(f"  Brought up {target.node}:{target.interface}")
            except RuntimeError as e:
                print(f"  Warning: Failed to bring up {target}: {e}", file=sys.stderr)
    
    elif fault.type == "netem":
        # Revert netem via the netem module
        target_str = fault.targets[0].interface  # stored as pseudo-target
        try:
            netem_fault = NetemFault(target=target_str, spec=fault.spec or "")
            netem = Netem(faults=[netem_fault])
            netem.revert()
            print(f"  Reverted netem on '{target_str}'")
        except Exception as e:
            print(f"  Warning: Failed to revert netem: {e}", file=sys.stderr)


def _cmd_fault_list(args: argparse.Namespace) -> int:
    """Handle 'srctl fault list' command."""
    state = fault_module.FaultState.load()
    
    if not state.faults:
        print("No active faults")
        return 0
    
    if args.output == "json":
        print(json.dumps(state.to_dict(), indent=2))
        return 0
    
    # Table format
    print(f"{'ID':<12} {'TYPE':<10} {'TARGET':<40} {'SPEC':<20} {'APPLIED':<20}")
    print("-" * 102)
    
    for fault in state.faults:
        # Format targets
        if fault.type == "shutdown":
            target_strs = [f"{t.node}:{t.interface}" for t in fault.targets]
            target_display = ", ".join(target_strs[:2])
            if len(target_strs) > 2:
                target_display += f" (+{len(target_strs) - 2} more)"
        else:  # netem
            target_display = fault.targets[0].interface  # stored as pseudo-target
        
        # Parse applied_at timestamp
        applied_display = fault.applied_at.split("T")[1][:8] if "T" in fault.applied_at else fault.applied_at
        
        print(f"{fault.id:<12} {fault.type:<10} {target_display:<40} {fault.spec or '':<20} {applied_display:<20}")
    
    return 0


# --- argument parser --------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="srctl",
        description="SRv6 fabric emulator control CLI",
    )
    sub = p.add_subparsers(dest="command", required=True)

    # --- get ----------------------------------------------------------
    get = sub.add_parser("get", help="inspect topology / hosts / EVs")
    get_sub = get.add_subparsers(dest="resource", required=True)

    g_topo = get_sub.add_parser("topology",
                                help="show fabric dimensions + tenants")
    g_topo.add_argument("-o", "--output",
                        choices=("table", "json", "yaml"),
                        default="table")
    g_topo.set_defaults(func=_cmd_get_topology)

    g_hosts = get_sub.add_parser("hosts", help="list hosts")
    g_hosts.add_argument("--tenant", default=None,
                         help="filter by tenant (default: all)")
    g_hosts.add_argument("-o", "--output",
                         choices=("table", "json", "yaml"),
                         default="table")
    g_hosts.set_defaults(func=_cmd_get_hosts)

    g_evs = get_sub.add_parser(
        "evs",
        help="list EVs (plane, path, SID) for a (src, dst) host pair",
    )
    g_evs.add_argument("src_host", help="e.g. green-host00")
    g_evs.add_argument("dst_host", help="e.g. green-host15")
    g_evs.add_argument("-n", type=int, default=None,
                       help="paths_per_plane subset size "
                            f"(1..{_topo.NUM_SPINES}; default = NUM_SPINES)")
    g_evs.add_argument("-o", "--output",
                       choices=("table", "json", "yaml", "sid"),
                       default="table",
                       help="output format ('sid' = scenario-style indented")
    g_evs.add_argument("--sid", choices=("uA", "uN"), default="uA",
                       help="outer uSID construction: uA (default) per-"
                            "adjacency, forces each fabric hop onto one "
                            "specific physical link; uN uses each hop's "
                            "own node locator instead")
    g_evs.set_defaults(func=_cmd_get_evs)

    # --- run ----------------------------------------------------------
    r = sub.add_parser("run",
                       help="execute a scenario by name (or --list)")
    r.add_argument("scenario", nargs="?", default=None,
                   help="scenario name (e.g. green-mrc-baseline) or YAML path")
    r.add_argument("--list", action="store_true",
                   help="list available scenarios for the active topology")
    r.add_argument("--verbose", "-v", action="store_true")
    r.add_argument("--dry-run", action="store_true")
    r.add_argument("--duration", default=None, metavar="DUR",
                   help="override every flow's duration (e.g. '5s', '500ms'); "
                        "forwarded to run-scenario")
    r.add_argument("--sid", choices=("uA", "uN"), default=None,
                   help="override the scenario's outer uSID construction "
                        "(uA=per-adjacency default, uN=node-locator); "
                        "forwarded to run-scenario")
    r.set_defaults(func=_cmd_run)

    # --- fault --------------------------------------------------------
    fault = sub.add_parser("fault", help="inject/clear/list faults in the fabric")
    fault_sub = fault.add_subparsers(dest="fault_command", required=True)

    # srctl fault shutdown
    f_shutdown = fault_sub.add_parser(
        "shutdown",
        help="shutdown interface(s) on a fabric node",
    )
    f_shutdown.add_argument("node", help="node name (e.g. p0-spine01)")
    f_shutdown.add_argument(
        "interfaces",
        nargs="+",
        help="interface(s) to shutdown (e.g. Ethernet0, Ethernet4, or 'all')",
    )
    f_shutdown.add_argument(
        "--unidirectional",
        action="store_true",
        help="only shutdown this side (default: bidirectional)",
    )
    f_shutdown.set_defaults(func=_cmd_fault_shutdown)

    # srctl fault netem
    f_netem = fault_sub.add_parser(
        "netem",
        help="inject tc/netem fault on host-side veth",
    )
    f_netem.add_argument("target", help="target string (e.g. 'host yellow-host00 plane 2')")
    f_netem.add_argument("spec", help="netem spec (e.g. 'loss 5%%' or 'delay 10ms')")
    f_netem.set_defaults(func=_cmd_fault_netem)

    # srctl fault clear
    f_clear = fault_sub.add_parser(
        "clear",
        help="clear injected faults",
    )
    f_clear.add_argument(
        "--all",
        action="store_true",
        help="clear all tracked faults",
    )
    f_clear.add_argument(
        "node",
        nargs="?",
        help="clear faults on specific node (optional if --all)",
    )
    f_clear.add_argument(
        "interface",
        nargs="?",
        help="clear fault on specific interface (requires node)",
    )
    f_clear.set_defaults(func=_cmd_fault_clear)

    # srctl fault list
    f_list = fault_sub.add_parser(
        "list",
        help="list active faults",
    )
    f_list.add_argument(
        "-o", "--output",
        choices=("table", "json"),
        default="table",
    )
    f_list.set_defaults(func=_cmd_fault_list)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":   # pragma: no cover
    sys.exit(main())
