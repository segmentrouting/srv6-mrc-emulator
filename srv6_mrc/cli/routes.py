#!/usr/bin/env python3
"""
routes.py — declarative SRv6 route management for the 4-plane lab.

A small kubectl-style tool that installs/removes the per-host kernel routes
needed for SRv6 traffic between tenant hosts. The lab fabric only forwards;
the hosts have to know "for inner dst X, encap with this SID list and send
out NIC ethN". Writing those `ip -6 route` invocations by hand is tedious,
and once you want a full mesh (e.g. host00 talking to host01..15 across all
4 planes) it's hundreds of routes. This tool reads a YAML spec describing
pairs or meshes and expands them into the underlying route operations.

The address scheme and SID-list shape are identical to spray.py and to the
generator (generate_fabric.py / former test-routes.sh) — keep those in
sync if you change any of the helpers below.

Subcommands:

    routes.py apply  -f spec.yaml             # idempotent install (ip route replace)
    routes.py delete -f spec.yaml             # remove exactly what the spec describes
    routes.py delete --all                    # wipe every `encap seg6` route everywhere
    routes.py list   [--host h1,h2] [--tenant green|yellow] [-o wide|raw]

Spec format (YAML):

    apiVersion: srv6-lab/v1
    kind: RouteSet
    metadata:
      name: my-routes
    spec:
      pairs:               # explicit endpoint pairs
        - {tenant: green, src: 0, dst: 15}             # spine auto
        - {tenant: green, src: 1, dst: 14, spine: 3}   # spine forced
      mesh:                # cartesian expansion; self-pairs and dups dropped
        - tenant: yellow
          src: [0, 1, 2, 3]
          dst: [12, 13, 14, 15]
          # planes: [0, 1, 2, 3]    # optional; default all 4
          # spine: auto              # optional; default auto

`apply -f spec.yaml` installs 4 routes per pair per direction (4 planes x
forward + reverse = 8 routes per pair). Plane selection at run-time is
done by the sender choosing the source NIC (eth1..eth4 == plane 0..3).
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import os
import re
import signal
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

try:
    import yaml
except ImportError:
    sys.exit("routes.py: PyYAML not installed. Try: pip3 install pyyaml")


# ----------------------------------------------------------------------------
# topology selection (must run before srv6_mrc.topo import)
# ----------------------------------------------------------------------------

def _infer_srv6_topo_from_argv(argv: list[str]) -> str | None:
    """If argv contains `-f topologies/<X>/routes/<Y>.yaml` (or `--file=...`),
    return the matching `topologies/<X>/topo.yaml`. Otherwise None.

    This lets `python3 -m srv6_mrc.cli.routes apply -f
    topologies/2p-4x8/routes/full-mesh.yaml` automatically run against the
    2p-4x8 topology even if the operator forgot to export $SRV6_TOPO on
    the deploy host. Without this, srv6_mrc.topo falls back to the
    default (4p-8x16) and the route generator iterates the wrong number
    of planes, attempting `eth3`/`eth4` on hosts that only have
    `eth1`/`eth2` and producing hundreds of spurious failures.

    We only look at the spec path; we do NOT read the YAML here (that's
    later, in load_spec_file). Pure string inspection, no I/O.
    """
    spec_path: str | None = None
    it = iter(range(len(argv)))
    for i in it:
        a = argv[i]
        if a in ("-f", "--file") and i + 1 < len(argv):
            spec_path = argv[i + 1]
            break
        if a.startswith("--file="):
            spec_path = a[len("--file="):]
            break
        if a.startswith("-f="):
            spec_path = a[len("-f="):]
            break
    if not spec_path:
        return None

    # Expect `<...>/topologies/<NAME>/routes/<spec>.yaml`. Resolve to absolute
    # so relative invocations (the normal case via the Makefile) still work.
    p = Path(spec_path).resolve()
    parts = p.parts
    try:
        i = len(parts) - 1 - list(reversed(parts)).index("topologies")
    except ValueError:
        return None
    # Need parts after `topologies`: [<NAME>, "routes", ...]
    if i + 2 >= len(parts) or parts[i + 2] != "routes":
        return None
    topo_name = parts[i + 1]
    topo_yaml = Path(*parts[: i + 1]) / topo_name / "topo.yaml"
    return str(topo_yaml) if topo_yaml.exists() else None


# Set $SRV6_TOPO from argv path before importing srv6_mrc.topo (which reads
# the env at *its* import time). Operator-supplied $SRV6_TOPO always wins.
if not os.environ.get("SRV6_TOPO"):
    _inferred = _infer_srv6_topo_from_argv(sys.argv[1:])
    if _inferred:
        os.environ["SRV6_TOPO"] = _inferred

from srv6_mrc import topo as _topo


# ----------------------------------------------------------------------------
# constants
# ----------------------------------------------------------------------------

# Sourced from the active topo.yaml via srv6_mrc.topo (env: SRV6_TOPO).
# Do not hardcode values here — every topology supplies its own.
TOPO = _topo.CLAB_TOPOLOGY_NAME
NUM_PLANES = _topo.NUM_PLANES
PLANES_ALL = list(range(NUM_PLANES))
NUM_LEAVES = _topo.NUM_LEAVES   # host ids 0..NUM_LEAVES-1
NUM_SPINES = _topo.NUM_SPINES
APIVERSION = "srv6-lab/v1"
KIND = "RouteSet"
WORKERS = 16      # parallel docker exec for apply/delete/list

# Reference (lo, hi) pair -> chosen transit spine. Imported from
# srv6_mrc.topo so the table tracks the active topology. Topologies
# without an entry fall through to a hash in spine_for() below.
REFERENCE_PAIRS_SPINES = _topo.REFERENCE_PAIRS_SPINES


# ----------------------------------------------------------------------------
# address / SID helpers (must match generate_fabric.py + spray.py)
# ----------------------------------------------------------------------------

def inner_addr(tenant: str, host_id: int) -> str:
    """Plane-independent inner tenant address.

    Phase 1a: yellow flipped from `cccd:<NN>::1` (on lo) to anycast
    `cccc:<NN>::2` (mirroring green's `bbbb:<NN>::2`). Inner-DA semantics
    are now uniform across tenants; only the tenant-tag hextet differs.
    """
    if tenant == "green":
        return f"2001:db8:bbbb:{host_id:02x}::2"
    return f"2001:db8:cccc:{host_id:02x}::2"


def inner_route_dst(tenant: str, host_id: int) -> str:
    """The /128 we install on the sender. The same /128 is replicated across
    all 4 planes; entries are distinguished by `dev ethN metric 100+P`."""
    return f"{inner_addr(tenant, host_id)}/128"


def build_segs(tenant: str, plane: int, spine: int, dst_leaf: int) -> str:
    """Outer uSID list. encap.red — kernel will place this in the outer dst."""
    seg = f"fc00:000{plane:x}:f00{spine:x}:e00{dst_leaf:x}"
    return f"{seg}:d000::" if tenant == "green" else f"{seg}:e009:d001::"


def spine_for(lo: int, hi: int) -> int:
    """Spine selection. Reference pairs use the table for reproducibility;
    everything else falls through to a deterministic hash."""
    a, b = (lo, hi) if lo < hi else (hi, lo)
    if (a, b) in REFERENCE_PAIRS_SPINES:
        return REFERENCE_PAIRS_SPINES[(a, b)]
    # Hash: must be deterministic, must spread across 0..7. (a + b * 16) % 8
    # gives uneven distribution for small samples but is fine for full mesh.
    return (a * NUM_LEAVES + b) % NUM_SPINES


def host_name(tenant: str, host_id: int) -> str:
    return f"{tenant}-host{host_id:02d}"


def container(node: str) -> str:
    """Resolve clab short-name to docker container name. Tries the short
    name first (works if user re-tagged); falls back to clab- prefix."""
    rc = subprocess.run(
        ["docker", "inspect", node],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    return node if rc.returncode == 0 else f"clab-{TOPO}-{node}"


# ----------------------------------------------------------------------------
# spec model
# ----------------------------------------------------------------------------

@dataclass(frozen=True)
class Pair:
    """One logical endpoint pair. Expands to 8 routes (4 planes x 2 dirs)."""
    tenant: str
    a: int          # canonicalized so a < b (dedup key)
    b: int
    spine: int
    planes: tuple[int, ...]

    def routes(self) -> list["Route"]:
        """Emit one Route per (direction, plane)."""
        out: list[Route] = []
        for src_id, dst_id in ((self.a, self.b), (self.b, self.a)):
            for p in self.planes:
                out.append(Route(
                    on_host=host_name(self.tenant, src_id),
                    dst=inner_route_dst(self.tenant, dst_id),
                    segs=build_segs(self.tenant, p, self.spine,
                                    dst_leaf=dst_id),
                    dev=f"eth{p + 1}",
                    metric=100 + p,
                ))
        return out


@dataclass(frozen=True)
class Route:
    """One ip -6 route invocation."""
    on_host: str
    dst: str
    segs: str
    dev: str
    metric: int


def _validate_host_id(v, ctx: str) -> int:
    if not isinstance(v, int) or not (0 <= v < NUM_LEAVES):
        raise ValueError(f"{ctx}: expected int 0..{NUM_LEAVES - 1}, got {v!r}")
    return v


def _validate_tenant(v, ctx: str) -> str:
    if v not in ("green", "yellow"):
        raise ValueError(f"{ctx}: tenant must be 'green' or 'yellow', got {v!r}")
    return v


def _validate_planes(v, ctx: str) -> tuple[int, ...]:
    if not isinstance(v, list) or not all(isinstance(p, int) for p in v):
        raise ValueError(f"{ctx}: planes must be a list of ints, got {v!r}")
    bad = [p for p in v if not (0 <= p < NUM_PLANES)]
    if bad:
        raise ValueError(f"{ctx}: plane(s) out of range 0..{NUM_PLANES - 1}: {bad}")
    return tuple(sorted(set(v)))


def _validate_spine(v, ctx: str) -> int | str:
    if v == "auto":
        return "auto"
    if not isinstance(v, int) or not (0 <= v < NUM_SPINES):
        raise ValueError(f"{ctx}: spine must be 'auto' or int 0..{NUM_SPINES - 1}, got {v!r}")
    return v


def _as_id_list(v, ctx: str) -> list[int]:
    """Accept a single int, a list of ints, or the string "all" (= every
    host id 0..NUM_LEAVES-1); return list of ints. The "all" form makes
    `mesh:` blocks topology-agnostic — the same YAML works on any
    topology size without hardcoding host IDs.
    """
    if v == "all":
        return list(range(NUM_LEAVES))
    if isinstance(v, int):
        return [_validate_host_id(v, ctx)]
    if isinstance(v, list):
        return [_validate_host_id(x, f"{ctx}[{i}]") for i, x in enumerate(v)]
    raise ValueError(f"{ctx}: expected int, list of ints, or \"all\", got {v!r}")


def expand_spec(spec_doc: dict) -> list[Pair]:
    """Parse a loaded YAML doc and return a deduped list of Pairs.

    Returns Pairs sorted by (tenant, a, b) for stable output ordering.
    """
    if spec_doc.get("apiVersion") != APIVERSION:
        raise ValueError(f"unsupported apiVersion: {spec_doc.get('apiVersion')!r} "
                         f"(want {APIVERSION!r})")
    if spec_doc.get("kind") != KIND:
        raise ValueError(f"unsupported kind: {spec_doc.get('kind')!r} (want {KIND!r})")

    spec = spec_doc.get("spec") or {}
    if not isinstance(spec, dict):
        raise ValueError("spec: must be a mapping")

    raw_pairs: list[tuple[str, int, int, int | str, tuple[int, ...]]] = []

    # explicit pairs
    for i, p in enumerate(spec.get("pairs", []) or []):
        ctx = f"spec.pairs[{i}]"
        if not isinstance(p, dict):
            raise ValueError(f"{ctx}: expected mapping")
        tenant = _validate_tenant(p.get("tenant"), f"{ctx}.tenant")
        src = _validate_host_id(p.get("src"), f"{ctx}.src")
        dst = _validate_host_id(p.get("dst"), f"{ctx}.dst")
        spine = _validate_spine(p.get("spine", "auto"), f"{ctx}.spine")
        planes = _validate_planes(p.get("planes", PLANES_ALL), f"{ctx}.planes")
        if src == dst:
            continue  # silently drop self-pairs
        raw_pairs.append((tenant, src, dst, spine, planes))

    # mesh: cartesian expansion
    for i, m in enumerate(spec.get("mesh", []) or []):
        ctx = f"spec.mesh[{i}]"
        if not isinstance(m, dict):
            raise ValueError(f"{ctx}: expected mapping")
        tenant = _validate_tenant(m.get("tenant"), f"{ctx}.tenant")
        srcs = _as_id_list(m.get("src"), f"{ctx}.src")
        dsts = _as_id_list(m.get("dst"), f"{ctx}.dst")
        spine = _validate_spine(m.get("spine", "auto"), f"{ctx}.spine")
        planes = _validate_planes(m.get("planes", PLANES_ALL), f"{ctx}.planes")
        for s in srcs:
            for d in dsts:
                if s == d:
                    continue
                raw_pairs.append((tenant, s, d, spine, planes))

    if not raw_pairs:
        raise ValueError("spec contained no pairs (after self-pair removal)")

    # canonicalize (a < b) and dedup. If the same (tenant, a, b) shows up
    # with different spine/planes, the last one wins; could be stricter
    # but kubectl-ish leniency feels right.
    canonical: dict[tuple[str, int, int], tuple[int | str, tuple[int, ...]]] = {}
    for tenant, src, dst, spine, planes in raw_pairs:
        a, b = (src, dst) if src < dst else (dst, src)
        canonical[(tenant, a, b)] = (spine, planes)

    pairs: list[Pair] = []
    for (tenant, a, b), (spine, planes) in sorted(canonical.items()):
        if spine == "auto":
            spine = spine_for(a, b)
        pairs.append(Pair(tenant=tenant, a=a, b=b, spine=int(spine), planes=planes))
    return pairs


def load_spec_file(path: str) -> list[Pair]:
    try:
        with open(path) as f:
            doc = yaml.safe_load(f)
    except FileNotFoundError:
        sys.exit(f"routes.py: spec file not found: {path}")
    except yaml.YAMLError as e:
        sys.exit(f"routes.py: YAML parse error in {path}:\n{e}")
    if doc is None:
        sys.exit(f"routes.py: spec file is empty: {path}")
    try:
        return expand_spec(doc)
    except ValueError as e:
        sys.exit(f"routes.py: spec error in {path}: {e}")


# ----------------------------------------------------------------------------
# kernel ops (docker exec into host containers)
# ----------------------------------------------------------------------------

def _docker_exec(host: str, cmd: list[str]) -> tuple[int, str]:
    """Run a command inside a host container; return (rc, combined output)."""
    full = ["docker", "exec", container(host)] + cmd
    p = subprocess.run(full, capture_output=True, text=True)
    return p.returncode, (p.stdout + p.stderr).strip()


def route_replace(r: Route) -> tuple[int, str]:
    return _docker_exec(r.on_host, [
        "ip", "-6", "route", "replace", r.dst,
        "encap", "seg6", "mode", "encap.red", "segs", r.segs,
        "dev", r.dev, "metric", str(r.metric),
    ])


def route_delete(r: Route) -> tuple[int, str]:
    # `ip route del` errors if the route is absent; we treat that as success
    # so delete is idempotent like apply.
    rc, out = _docker_exec(r.on_host, [
        "ip", "-6", "route", "del", r.dst,
        "dev", r.dev, "metric", str(r.metric),
    ])
    if rc != 0 and "No such" in out:
        return 0, ""
    return rc, out


def list_srv6_routes_on_host(host: str) -> list[str]:
    """Return raw `ip -6 route` lines that include `encap seg6`."""
    rc, out = _docker_exec(host, ["ip", "-6", "route", "show"])
    if rc != 0:
        return [f"!! error: {out}"]
    return [ln for ln in out.splitlines() if "encap seg6" in ln]


# ----------------------------------------------------------------------------
# host enumeration / filtering
# ----------------------------------------------------------------------------

def all_host_names(tenant_filter: str | None = None,
                   host_filter: list[str] | None = None) -> list[str]:
    hosts: list[str] = []
    tenants = [tenant_filter] if tenant_filter else ["green", "yellow"]
    for t in tenants:
        for i in range(NUM_LEAVES):
            hosts.append(host_name(t, i))
    if host_filter:
        wanted = set(host_filter)
        unknown = wanted - set(hosts)
        if unknown:
            sys.exit(f"routes.py: unknown host(s): {sorted(unknown)}")
        hosts = [h for h in hosts if h in wanted]
    return hosts


# ----------------------------------------------------------------------------
# parallel runner
# ----------------------------------------------------------------------------

def _run_parallel(label: str, items: list, fn, key=lambda x: x) -> int:
    """Run fn over items in a thread pool. fn returns (rc, msg). Returns
    count of failures."""
    failures = 0
    with cf.ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futures = {ex.submit(fn, it): it for it in items}
        for fut in cf.as_completed(futures):
            it = futures[fut]
            try:
                rc, msg = fut.result()
            except Exception as e:
                rc, msg = 1, f"exception: {e}"
            if rc != 0:
                failures += 1
                print(f"  ! {label} failed on {key(it)}: {msg}", file=sys.stderr)
    return failures


# ----------------------------------------------------------------------------
# subcommands
# ----------------------------------------------------------------------------

def cmd_apply(args) -> int:
    pairs = load_spec_file(args.file)
    routes = [r for p in pairs for r in p.routes()]
    print(f"  apply: {len(pairs)} pair(s) -> {len(routes)} route(s) "
          f"across {sum(len(p.planes) for p in pairs) * 2} (plane x dir) slots")
    fails = _run_parallel("replace", routes, route_replace,
                          key=lambda r: f"{r.on_host} {r.dst} via {r.dev}")
    if fails:
        print(f"  done with {fails} failure(s)", file=sys.stderr)
        return 1
    print(f"  ok")
    return 0


def cmd_delete(args) -> int:
    if args.all:
        return _delete_all()
    if not args.file:
        sys.exit("routes.py: delete needs -f <spec.yaml> or --all")
    pairs = load_spec_file(args.file)
    routes = [r for p in pairs for r in p.routes()]
    print(f"  delete: {len(routes)} route(s)")
    fails = _run_parallel("del", routes, route_delete,
                          key=lambda r: f"{r.on_host} {r.dst} via {r.dev}")
    if fails:
        print(f"  done with {fails} failure(s)", file=sys.stderr)
        return 1
    print(f"  ok")
    return 0


# Match `ip -6 route show` output; capture dst, segs, dev, metric for delete.
# Alpine iproute2 emits the segs field in bracketed form with a count:
#     2001:db8:bbbb:f::2  encap seg6 mode encap.red segs 1 [ fc00:0:f000:e00f:d000:: ] dev eth1 metric 100 pref medium
# Some older / other iproute2 builds emit `segs <addr>` without brackets. We
# accept both shapes; the only field we actually extract here is the *first*
# segs entry, which is what kernel `ip route del` doesn't need anyway (delete
# only matches on dst+dev+metric). Note dst lacks an explicit /128 even when
# installed as /128 — iproute2 omits it for natural-width host routes.
_RE_SEG6 = re.compile(
    r"^(?P<dst>\S+)\s+.*?\bencap seg6\b.*?\bdev (?P<dev>\S+).*?\bmetric (?P<metric>\d+)"
)


def _delete_all() -> int:
    """Remove every encap-seg6 route on every host. Filters strictly on
    `encap seg6` so connected/default routes are untouched."""
    hosts = all_host_names()
    print(f"  delete --all: scanning {len(hosts)} host(s)")

    # Step 1: discover routes per host (parallel reads).
    discovered: list[tuple[str, str, str, int]] = []

    def scan(host: str) -> tuple[int, str]:
        lines = list_srv6_routes_on_host(host)
        n = 0
        for ln in lines:
            m = _RE_SEG6.match(ln)
            if not m:
                continue
            dst = m.group("dst")
            if "/" not in dst:
                dst += "/128"
            discovered.append((host, dst, m.group("dev"), int(m.group("metric"))))
            n += 1
        return 0, f"{n} routes"

    _run_parallel("scan", hosts, scan, key=lambda h: h)

    if not discovered:
        print("  nothing to delete")
        return 0
    print(f"  found {len(discovered)} encap-seg6 route(s); removing")

    # Step 2: delete them. Use a synthetic Route record so we reuse route_delete.
    def do(item: tuple[str, str, str, int]) -> tuple[int, str]:
        host, dst, dev, metric = item
        r = Route(on_host=host, dst=dst, segs="", dev=dev, metric=metric)
        return route_delete(r)

    fails = _run_parallel("del", discovered, do,
                          key=lambda it: f"{it[0]} {it[1]} via {it[2]}")
    if fails:
        print(f"  done with {fails} failure(s)", file=sys.stderr)
        return 1
    print("  ok")
    return 0


def cmd_list(args) -> int:
    # Resolve output mode. --raw is a back-compat alias for -o raw.
    mode = args.output
    if args.raw:
        mode = "raw"
    if mode is None:
        mode = "default"

    host_filter: list[str] | None = None
    if args.host:
        host_filter = [h.strip() for h in args.host.split(",") if h.strip()]
    hosts = all_host_names(tenant_filter=args.tenant, host_filter=host_filter)

    # parallel fetch
    per_host_lines: dict[str, list[str]] = {}
    def fetch(h: str) -> tuple[int, str]:
        per_host_lines[h] = list_srv6_routes_on_host(h)
        return 0, ""
    _run_parallel("list", hosts, fetch)

    total = 0
    for h in hosts:
        lines = per_host_lines.get(h, [])
        if not lines:
            continue
        if mode == "raw":
            print(f"\n{h}:")
            for ln in lines:
                print(f"  {ln}")
            total += len(lines)
            continue

        # Parse all routes for this host into structured per-plane records,
        # grouped by (tenant, dst_id, spine). Each record carries the plane
        # number, the underlying dev (eth1..eth4) and the kernel metric so
        # that -o wide can render the full per-plane path.
        groups: dict[tuple[str, int, int], list[tuple[int, str, int]]] = {}
        unknown = False
        for ln in lines:
            m = _RE_SEG6.match(ln)
            if not m:
                continue
            dst = m.group("dst")
            dev = m.group("dev")
            metric = int(m.group("metric"))
            segs = _extract_segs(ln)
            tenant, dst_id = _decode_inner_dst(dst)
            spine = _decode_spine_from_segs(segs)
            try:
                plane = int(dev.removeprefix("eth")) - 1
            except ValueError:
                plane = -1
            if tenant is None or dst_id is None or spine is None:
                unknown = True
                continue
            groups.setdefault((tenant, dst_id, spine), []).append(
                (plane, dev, metric)
            )

        if not groups and not unknown:
            continue

        # Source-leaf id is derived from the host name. Every host attaches
        # to its same-indexed leaf on every plane (hostNN -> leafNN).
        try:
            src_id = int(h.split("-host")[1])
        except (IndexError, ValueError):
            src_id = -1

        print(f"\n{h}:")
        if unknown:
            print("  (unrecognized SRv6 route shape — use -o raw to inspect)")
        for (tenant, dst_id, spine), recs in sorted(groups.items()):
            recs.sort()  # by plane
            planes = sorted({p for p, _, _ in recs if p >= 0})
            if mode == "wide":
                print(f"  -> {tenant}-host{dst_id:02d}  "
                      f"via spine{spine:02d}  planes {planes}")
                for plane, dev, metric in recs:
                    if plane < 0:
                        continue
                    print(f"     plane {plane}: "
                          f"p{plane}-leaf{src_id:02d} -> "
                          f"p{plane}-spine{spine:02d} -> "
                          f"p{plane}-leaf{dst_id:02d}  "
                          f"({dev} metric {metric})")
            else:
                # default: collapsed one-liner per (dst, spine)
                print(f"  -> {tenant}-host{dst_id:02d}  "
                      f"via spine{spine:02d}  planes {planes}")
            total += len(planes)
    print(f"\n  total: {total} route(s) across {len(hosts)} host(s)")
    return 0


# Small parsers for collapsed list view ---------------------------------------

# Two valid `segs` field shapes from iproute2:
#   bracketed (alpine):  "segs 1 [ fc00:0:f000:e00f:d000:: ]"
#   bare (older builds): "segs fc00:0:f000:e00f:d000::"
# Try bracketed first; fall back to bare. We only want the first SID since
# the spine/dst-leaf are encoded in its f-hextet.
_RE_SEGS_BRACKETED = re.compile(r"\bsegs\s+\d+\s+\[\s*(\S+?)\s*(?:\]|\s)")
_RE_SEGS_BARE = re.compile(r"\bsegs\s+([0-9a-fA-F:]+)(?:\s|$)")

def _extract_segs(line: str) -> str | None:
    m = _RE_SEGS_BRACKETED.search(line)
    if m:
        return m.group(1)
    m = _RE_SEGS_BARE.search(line)
    return m.group(1) if m else None


_RE_INNER_GREEN = re.compile(r"^2001:db8:bbbb:([0-9a-fA-F]+)::2(?:/128)?$")
_RE_INNER_YELLOW = re.compile(r"^2001:db8:cccc:([0-9a-fA-F]+)::2(?:/128)?$")

def _decode_inner_dst(dst: str) -> tuple[str | None, int | None]:
    for rx, t in ((_RE_INNER_GREEN, "green"), (_RE_INNER_YELLOW, "yellow")):
        m = rx.match(dst)
        if m:
            return t, int(m.group(1), 16)
    return None, None


# Extract the spine number from a uSID list. iproute2 prints addresses in
# their canonical (collapsed) form, so we see "fc00:0:f000:e00f:d000::"
# rather than the zero-padded "fc00:0000:f000:e00f:d000::" we'd write by
# hand. The structure is:
#   fc00 : <plane>  : f00<spine>  : e00<leaf> : ... : ...
# i.e. the f-hextet (spine) is hextet index 2 (0-indexed). We match it
# tolerantly: any hex value at hextet[1] (plane), then `f00<digit>` at
# hextet[2].
_RE_SEGS_SHAPE = re.compile(
    r"^fc00:[0-9a-f]+:f00([0-9a-f]):", re.IGNORECASE
)

def _decode_spine_from_segs(segs: str | None) -> int | None:
    if not segs:
        return None
    m = _RE_SEGS_SHAPE.match(segs)
    return int(m.group(1), 16) if m else None


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(
        prog="routes.py",
        description="declarative SRv6 host route management",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_apply = sub.add_parser("apply", help="install routes from a spec file")
    p_apply.add_argument("-f", "--file", required=True, help="YAML spec file")
    p_apply.set_defaults(func=cmd_apply)

    p_delete = sub.add_parser("delete", help="remove routes")
    p_delete.add_argument("-f", "--file", help="YAML spec file")
    p_delete.add_argument("--all", action="store_true",
                          help="remove every encap-seg6 route on every host")
    p_delete.set_defaults(func=cmd_delete)

    p_list = sub.add_parser("list", help="show installed SRv6 routes")
    p_list.add_argument("--host", help="comma-separated host names to limit to")
    p_list.add_argument("--tenant", choices=("green", "yellow"),
                        help="limit to one tenant")
    p_list.add_argument("-o", "--output", choices=("wide", "raw"),
                        default=None,
                        help="output mode: 'wide' shows per-plane path detail; "
                             "'raw' prints literal `ip -6 route` lines")
    p_list.add_argument("--raw", action="store_true",
                        help="alias for -o raw")
    p_list.set_defaults(func=cmd_list)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    # Make piping to head/less behave like normal CLI tools (no traceback
    # on EPIPE when the consumer closes early).
    try:
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    except (AttributeError, ValueError):
        pass  # not available on Windows; harmless
    sys.exit(main())
