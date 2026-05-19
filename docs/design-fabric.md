# 4-Plane SRv6 Fabric (8-spine × 16-leaf × 4 planes)

A SRv6 (uSID) lab built on top of `docker-sonic-vs` +
Containerlab. Models a small slice of a hyperscale AI backend fabric: 4
independent network planes, each an 8 × 16 Clos, with multi-homed tenant
hosts uplinked into every plane.


- **96 SONiC switches** (32 spines + 64 leaves)
- **32 Alpine hosts** (16 green + 16 yellow), each with 4 NIC uplinks
- **640 veth pairs** (512 fabric + 128 host)
- **No BGP, no IGP** — every transit FIB entry is a static route or an SRv6 uA
  SID; the controller installs end-to-end SR policies for tenant traffic.

## Quickstart
Instructions to quickly deploy and play with the topology can be found in the [quickstart.md](./quickstart.md) guide

## Fabric design

The lab demonstrates several patterns that recur in hyperscale GPU fabrics:

1. **Multi-plane Clos** — each plane is an independent failure / scheduling
   domain. Hosts have one NIC into each plane; flows are pinned to a plane by
   the controller, not by ECMP.
2. **Per-plane uSID block** — each plane gets its own `/32` so plane identity
   is part of the destination prefix, not buried in node bits. Aggregates
   cleanly at the WAN: one `/30` per cluster.
3. **Function-bit conventions across the fabric** — `f00<S>` always means
   "this leaf's uA toward spine S", `e00<L>` always means "this spine's uA
   toward leaf L", `d000`/`d001` are tenant-ID uDT6 SIDs. A controller
   reading any SID list can tell what each label does without per-node state.
4. **Three SRv6 multi-tenancy models**:
    - **Network-based** (blue): leaf-encap, leaf-decap. *Removed in this lab.*
    - **Hybrid** (green): host-encap, leaf-decap into `Vrf-green` via uDT6.
    - **Host-based** (yellow): host-encap, host-decap. Leaves are pure transit;
      yellow hosts run `seg6local End.DT6` on every plane NIC.

## Addressing

### IPv6 layout

| Element | Pattern | Example |
|---|---|---|
| Cluster aggregate | `fc00:0000::/30` | covers all 4 planes |
| Plane block | `fc00:000<P>::/32` | plane 2 → `fc00:0002::/32` |
| Spine locator | `fc00:000<P>:1<S>::/48` | p2-spine03 → `fc00:0002:13::/48` |
| Leaf locator | `fc00:000<P>:2<L>::/48` | p2-leaf10 → `fc00:0002:2a::/48` |
| Leaf uA → spine | `fc00:000<P>:f00<S>::/48` | p2 leaf, toward spine03 → `fc00:0002:f003::/48` |
| Spine uA → leaf | `fc00:000<P>:e00<L>::/48` | p2 spine, toward leaf10 → `fc00:0002:e00a::/48` |
| Green tenant uDT6 | `fc00:000<P>:d000::/48` | per-plane on every leaf, decap into `sonic Vrf-green` |
| Yellow tenant uDT6 | `fc00:000<P>:d001::/48` | per-plane on every yellow host, `linux End.DT6 table 0` |
| Fabric P2P | `2001:db8:fab:<S*16+L>::/127` | reused per plane (planes are L2-isolated) |
| Green tenant address | `2001:db8:bbbb:<NN>::2` | **anycast** on all 4 host NICs (`nodad`); identical leaf-side `::1/64` on every plane's Ethernet32 in `Vrf-green` |
| Yellow tenant address | `2001:db8:cccc:<NN>::2` | **anycast** on all 4 host NICs + `lo` (`nodad`); identical leaf-side `::1/64` on every plane's Ethernet36 (Phase 1a: mirrors green's plan with `bbbb`→`cccc`) |
| Host side (anycast) | `...::2/64` | identical address on `eth1..eth4` (and `lo` for yellow) — nodad |
| Leaf gateway (anycast) | `...::1/64` | identical address on every plane's host-facing leaf port |

`<P>` = plane 0–3 (hex), `<S>` = spine 0–7, `<L>` = leaf 0–f, `<NN>` = host 00–15 (hex byte).

### IPv4 (loopback only — for FRR router-id)

| Element | Pattern |
|---|---|
| Spine loopback | `10.0.<P>.<S+1>` |
| Leaf loopback | `10.1.<P>.<L+1>` |

### Reading a SR-policy SID list

A path "deliver to p2-leaf10, choose plane 2, egress toward p2-spine03,
then decap into green at the next hop" encodes as a single uSID-compressed
IPv6 destination:

```
fc00:0002:f003:e00a:d000::
└──┬───┘ └┬──┘ └┬─┘ └┬─┘
   │      │     │    └─ d000  : tenant-ID green → Vrf-green at egress leaf
   │      │     └────── e00a  : spine03 uA toward leaf10 (in plane 2)
   │      └──────────── f003  : leaf uA toward spine03 
   └─────────────────── 0002  : plane 2 block
```


## Files

| File | Purpose |
|---|---|
| `topologies/<name>/topo.yaml` | Declarative single source of truth for one variant (planes, spines, leaves, images, clab name) |
| `generators/fabric.py` | Parameterized generator: reads `topo.yaml` and emits `topology.clab.yaml` + `config/<node>/{config_db.json,frr.conf}` in the same dir |
| `topologies/<name>/topology.clab.yaml` | Containerlab topology (generated) |
| `topologies/<name>/config/<node>/` | Per-node SONiC `config_db.json` and FRR `frr.conf` (generated) |
| `scripts/config.sh` | Pushes generated configs into running SONiC containers |
| `srv6_mrc/cli/routes.py` (CLI: `routes`) | Declarative SRv6 host-route manager (kubectl-style: `apply`, `delete`, `list`) |
| `topologies/<name>/routes/*.yaml` | Ready-made route specs: reference pairs, full mesh, host00 fanout |
| `srv6_mrc/cli/spray.py` (CLI: `spray`) | Userspace SRv6 packet sprayer (sender + receiver). MRC/SRv6 demo. See `spray-protocol.md`. |
| `host-image/Dockerfile` | Builds `alpine-srv6-scapy:1.0` (host image: alpine + scapy + pip-installed `srv6_mrc`) |
| `spray-protocol.md` | Tool writeup: SID lists the sprayer builds, run instructions, manual tcpdump checkpoints |


## Tenant models in this lab

### Green (hybrid SRv6)

```
green-host00 NICs eth1..eth4   (anycast 2001:db8:bbbb:00::2 on all four)
   │ (encap by host or upstream controller; one of 4 NICs picked per packet)
   │  outer dst: fc00:000<P>:f00<S>:e00<L>:d000::      <P> = chosen plane
   ▼
   ─►  fabric (uA hops)  ─►  egress p<P>-leaf<L>.Ethernet32 (Vrf-green)
                              uDT6 d000 → decap → connected /64 → host
```

Every leaf in every plane has `fc00:000<P>:d000::/48 uDT6 vrf Vrf-green`, and
every plane's leaf carries the **same** `2001:db8:bbbb:<NN>::1/64` on its
green-facing Ethernet32. The host's tenant address `bbbb:<NN>::2` is anycast
across all 4 NICs, so a sprayed flow's inner dst is plane-independent — the
controller picks `<P>` in the outer SID list per packet, and the receiver
sees one socket regardless of which plane delivered it.

### Yellow (host-based SRv6)

```
yellow-host00 NICs eth1..eth4    (inner anycast 2001:db8:cccc:00::2 on
                                  all 4 NICs + lo nodad — Phase 1a)
   │  encap; outer dst: fc00:000<P>:f00<S>:e00<L>:e009:d001::  <P> = chosen plane
   ▼
   ─►  fabric (uA hops)  ─►  egress p<P>-leaf<NN>.Ethernet36 (default VRF)
                               ─►  yellow-host<NN>.eth(P+1) [anycast cccc:<NN>::2]
                                    seg6local End.DT6 table 0 → decap →
                                    table-0 lookup hits anycast 2001:db8:cccc:<NN>::2
                                    (present on eth1..eth4 + lo, nodad)
```

Each yellow host has 4 `seg6local` entries — one per plane — bound to the
respective plane NIC; that didn't change. What changed in Phase 1a: the
inner tenant destination is now anycast `cccc:<NN>::2`, present on all 4
NICs and on `lo` (mirroring green's `bbbb:<NN>::2` plan with `bbbb`→`cccc`).
The address present on `lo` (nodad) guarantees table-0 lookup resolves
locally even when no NIC is the egress interface. So a sprayed flow's
inner dst is plane-independent; plane identity stays in the outer SID
list and in which NIC the host's seg6local fires on. The leaf is a pure
transit hop; no `Vrf-yellow` exists.

### Why anycast for green, loopback for yellow

Both designs satisfy the same MRC/SRv6 invariant: **plane identity lives only
in the outer SID list, never in the inner/tenant address**. Without this,
spraying a single flow across planes would look like 4 different flows to the
receiver's stack — fatal for reorder. The mechanism differs because of where
each tenant's decap happens:

- **Green** decaps at the egress leaf (uDT6 → Vrf-green). The leaf's connected
  `/64` *is* the tenant address space, so we make it identical across planes
  (anycast `bbbb:<NN>::1/64` on every leaf's Ethernet32). The host's anycast
  `bbbb:<NN>::2` on all 4 NICs is the natural complement.
- **Yellow** decaps at the host (seg6local). The decap action delivers into
  table 0; making the post-decap dst plane-independent only requires a single
  `/128` on `lo`. The 4 per-plane `End.DT6` entries are an artifact of the
  per-plane uSID block (`d001` lives inside `fc00:000<P>::/32`), and they all
  point at the same inner address.


## Reducing scale

If your host can't accommodate 96 SONiC nodes, edit
`topologies/4p-8x16/topo.yaml` (or copy it to
`topologies/<smaller>/topo.yaml` to keep both):

```yaml
planes: 2                # 24 SONiC + 16 hosts, 96 veth pairs
spines_per_plane: 4      # halve again per plane
leaves_per_plane: 8
```

Hosts will reduce to the new `leaves_per_plane` count. Re-run
`make regen` (or `make TOPO=<smaller> regen`) and redeploy.

## What this lab is *not*

- **Not a performance benchmark.** `docker-sonic-vs` runs a software ASIC; you
  will not see line-rate. The point is correctness of the SRv6 control plane
  and forwarding behavior.
- **Not a full controller.** No PCEP/BGP-LS/path-computation engine is
  included. The static SIDs and routes give you a substrate; programming
  end-to-end SR policies is left to whatever controller you wire up
  (e.g. `jalapeno`, an OpenConfig+gRPC actor, or hand-rolled `vtysh`/iproute2).
- **Not multi-cluster.** A single cluster lives at `fc00:0000::/30`. The
  scheme extends naturally — the next cluster would be `fc00:0004::/30`,
  etc. — but no WAN gear is modeled here.

## See also

- `./spray-protocol.md` — userspace SRv6 sprayer: round-robin a single flow across
  all 4 planes to one anycast/loopback dst, count per-NIC arrivals on the
  receiver. The MRC/SRv6 demo this lab was built for.
- `./design-mrc.md` — MRC behavior layer on top of the spray substrate:
  policies, per-flow reorder measurement, fault injection scenarios,
  orchestration.
- `./running.md` — how to run MRC unit tests, manual two-host
  spray, and scenario-driven runs end-to-end.
- `./results-format.md` — how to read the per-flow ASCII summary
  and JSON reports `run-scenario` (`srv6_mrc/mrc/run.py`) produces.
- `./design-appendix.md` — rationale for the major design decisions.
- `./architecture.md` §2 — the plane-independent inner addressing
  invariant that makes spray work.


