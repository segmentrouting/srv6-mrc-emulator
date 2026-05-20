"""srctl — the SR-fabric emulator control CLI.

Kubectl-shaped command surface for inspecting topology, listing the EV
grid for a host pair, and running scenarios. Subcommands are dispatched
by argparse subparsers; output format is human-readable by default with
`-o json|yaml` available on the `get` family.

Subcommand surface (v1):

    srctl get topology
    srctl get hosts [--tenant T]
    srctl get evs <src-host> <dst-host> [-n N] [-o table|json|yaml|sid]
    srctl run <scenario> [--verbose] [--dry-run]
    srctl run --list

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


def _infer_srv6_topo_from_argv() -> None:
    """Set SRV6_TOPO from a scenario reference on argv, before importing
    srv6_mrc.topo. Idempotent: does nothing if SRV6_TOPO is already set.

    Recognized argv shapes for `srctl run`:
      - bare scenario name: `srctl run <name>` — search topologies/*/scenarios/<name>.yaml
      - explicit path:      `srctl run path/to/foo.yaml`
    For both forms, once a unique topology is identified, SRV6_TOPO is
    set to that topology's topo.yaml so module-level constants in
    srv6_mrc.topo bind to the right NUM_LEAVES/NUM_PLANES at import.
    """
    if os.environ.get("SRV6_TOPO"):
        return
    argv = sys.argv[1:]
    if not argv or argv[0] != "run":
        return
    # Find the first non-flag positional after "run".
    scen_arg: str | None = None
    for a in argv[1:]:
        if a.startswith("-"):
            continue
        scen_arg = a
        break
    if not scen_arg:
        return

    here = Path(__file__).resolve()
    repo_root = here.parent.parent.parent
    topos_dir = repo_root / "topologies"

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

    # Bare name: scan topologies/*/scenarios/<scen>.yaml. If exactly
    # one topology has it, use that. If multiple do, narrow down by
    # checking which topology is currently deployed (clab topology_name
    # appears in `docker ps`). Last resort: leave SRV6_TOPO unset and
    # let the user disambiguate.
    if not topos_dir.is_dir():
        return
    hits = list(topos_dir.glob(f"*/scenarios/{scen_arg}.yaml"))
    if not hits:
        return
    if len(hits) == 1:
        os.environ["SRV6_TOPO"] = str(hits[0].resolve().parents[1] / "topo.yaml")
        return

    # Multiple topologies have this scenario name. Use docker ps to
    # find the deployed one. Best-effort: any failure leaves SRV6_TOPO
    # unset (preserves the previous behaviour for users without docker).
    try:
        import subprocess
        out = subprocess.run(
            ["docker", "ps", "--format", "{{.Names}}"],
            capture_output=True, text=True, timeout=2, check=False,
        )
        running = out.stdout.split()
    except Exception:
        return
    if not running:
        return
    try:
        import yaml  # type: ignore[import-not-found]
    except ImportError:
        return
    live_hits: list[Path] = []
    for h in hits:
        topo_yaml = h.resolve().parents[1] / "topo.yaml"
        try:
            with open(topo_yaml) as f:
                t = yaml.safe_load(f)
            clab_name = t.get("clab", {}).get("topology_name")
            if not clab_name:
                continue
            prefix = f"clab-{clab_name}-"
            if any(n.startswith(prefix) for n in running):
                live_hits.append(topo_yaml)
        except Exception:
            continue
    if len(live_hits) == 1:
        os.environ["SRV6_TOPO"] = str(live_hits[0])


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
      2. <repo>/topologies/4p-8x16/ (development default)

    Returns None if neither path resolves to a directory; callers must
    handle (e.g., `srctl run --list` falls back to a clear error).
    """
    env = os.environ.get("SRV6_TOPO")
    if env:
        p = Path(env).resolve().parent
        return p if p.is_dir() else None
    here = Path(__file__).resolve()
    p = here.parent.parent.parent / "topologies" / "4p-8x16"
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
    computed via `usid_outer_dst(tenant, plane, spine, dst_id)`.

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
                sid = _topo.usid_outer_dst(tenant, plane, spine, dst_id)
                print(f"      P{plane}:S{spine}  {sid}")
        return 0

    rows: list[dict[str, Any]] = []
    for spine in spines:
        for plane in range(_topo.NUM_PLANES):
            sid = _topo.usid_outer_dst(tenant, plane, spine, dst_id)
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
    return run_main(forwarded)


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
    r.set_defaults(func=_cmd_run)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":   # pragma: no cover
    sys.exit(main())
