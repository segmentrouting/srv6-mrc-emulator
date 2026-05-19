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
| Green tenant uDT6 | `fc00:000<P>:d000::/48` | per-plane on every leaf, decap into `Vrf-green` |
| Yellow tenant uDT6 | `fc00:000<P>:d001::/48` | per-plane on every yellow host, `End.DT6 table 0` |
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
fc00:0002:2a:f003:d000::
└──┬───┘ └┬┘ └┬─┘ └┬─┘
   │      │   │    └─ d000  : tenant-ID green → Vrf-green at egress leaf
   │      │   └────── f003  : leaf uA toward spine 03 (in plane 2)
   │      └────────── 2a    : leaf locator (leaf 0a = leaf 10)
   └───────────────── 0002  : plane 2 block
```

Every label is unambiguous in isolation.

## Topology counts

| | Count |
|---|---|
| Planes | 4 |
| Spines per plane | 8 |
| Leaves per plane | 16 |
| Hosts per color | 16 |
| Total SONiC nodes | 96 |
| Total host nodes | 32 |
| Fabric links | 4 × 8 × 16 = 512 |
| Host links | 4 × 16 × 2 = 128 |
| **Total veth pairs** | **640** |

Each `docker-sonic-vs` container needs roughly 1–1.5 GB resident memory and a
portion of one CPU. Plan on **~150 GB RAM** and a multi-socket host (or scale
the lab down — see "Reducing scale" below).

## Files

| File | Purpose |
|---|---|
| `topologies/<name>/topo.yaml` | Declarative single source of truth for one variant (planes, spines, leaves, images, clab name) |
| `generators/fabric.py` | Parameterized generator: reads `topo.yaml` and emits `topology.clab.yaml` + `config/<node>/{config_db.json,frr.conf}` in the same dir |
| `topologies/<name>/topology.clab.yaml` | Containerlab topology (generated) |
| `topologies/<name>/config/<node>/` | Per-node SONiC `config_db.json` and FRR `frr.conf` (generated) |
| `scripts/config.sh` | Pushes generated configs into running SONiC containers |
| `srv6_fabric/cli/routes.py` (CLI: `routes`) | Declarative SRv6 host-route manager (kubectl-style: `apply`, `delete`, `list`) |
| `topologies/<name>/routes/*.yaml` | Ready-made route specs: reference pairs, full mesh, host00 fanout |
| `srv6_fabric/cli/spray.py` (CLI: `spray`) | Userspace SRv6 packet sprayer (sender + receiver). MRC/SRv6 demo. See `spray-protocol.md`. |
| `host-image/Dockerfile` | Builds `alpine-srv6-scapy:1.0` (host image: alpine + scapy + pip-installed `srv6_fabric`) |
| `spray-protocol.md` | Tool writeup: SID lists the sprayer builds, run instructions, manual tcpdump checkpoints |

## Deployment

### 1. Pull / build required images

```bash
docker pull docker-sonic-vs:latest                # SONiC VS (build or pull)
docker pull iejalapeno/alpine-srv6:1.0            # base host image
make image                                        # build alpine-srv6-scapy:1.0
                                                  # (equivalent to: docker build
                                                  #  -f host-image/Dockerfile
                                                  #  -t alpine-srv6-scapy:1.0 .)
                                                  # One image serves every topology;
                                                  # topo.yaml is bind-mounted at runtime.
```

### 2. Generate configs — already committed under `topologies/4p-8x16/config/`; re-run only if you change `topo.yaml`

```bash
make regen
# or directly:
python3 generators/fabric.py --topo topologies/4p-8x16/topo.yaml
```

Writes the topology and 96 sets of node configs into
`topologies/4p-8x16/`.

### 3. Bring up the lab

```bash
make deploy
# or directly:
sudo containerlab deploy -t topologies/4p-8x16/topology.clab.yaml
```

This stage:

- Boots 96 SONiC + 32 Alpine containers in dependency order
- Creates 640 veth pairs
- Applies the host `exec:` blocks (host IP addresses, plane routes, yellow
  `seg6local`)

Expect 5–15 minutes on a well-provisioned host.

### 4. Push SONiC configs

```bash
make config
# or directly:
scripts/config.sh all
```

This iterates every SONiC node and:

1. Creates `Loopback0` if missing
2. Copies `config_db.json` to `/etc/sonic/` and runs `sonic-cfggen --write-to-db`
3. Restarts SONiC services (`supervisorctl restart all`)
4. Sets up `vrfdefault`, `sr0`, IPv6 forwarding sysctls
5. Brings every port up (`config interface startup`)
6. Strips any default BGP instance (this lab has no BGP)
7. Loads `frr.conf` via `vtysh -f`

Other targets:

```bash
scripts/config.sh gen           # regenerate (same as `make regen`)
scripts/config.sh leaf          # leaf tier only
scripts/config.sh spine         # spine tier only
scripts/config.sh p2-leaf0a     # one node
```

### 5. Verify

```bash
# Pick any leaf
docker exec p2-leaf10 vtysh -c 'show segment-routing srv6 sid'
docker exec p2-leaf10 vtysh -c 'show ipv6 route summary'

# A green host: anycast tenant addr on all 4 NICs (same address each time)
docker exec green-host00 ip -6 addr show | grep bbbb

# A green host's plane-aggregate routes (one per NIC, anycast gateway)
docker exec green-host00 ip -6 route | grep fc00

# A yellow host: anycast cccc: address on eth1..eth4 + lo (Phase 1a)
docker exec yellow-host00 ip -6 addr show | grep cccc

# A yellow host should have 4 seg6local entries (one per plane NIC)
docker exec yellow-host00 ip -6 route | grep seg6local
```

To see SRv6 packet spraying across all 4 planes, see [`spray-protocol.md`](./spray-protocol.md). Two terminals:

```bash
# Receiver
docker exec -it green-host15 spray --role recv

# Sender (round-robin spray across 4 planes to the same anycast dst)
docker exec -it green-host00 spray --role send \
    --dst-id 15 --rate 1000pps --duration 5s
```

Spot-check any hop by running `tcpdump -nn -i <Ethernet…> 'ip6 proto 41'`
inside the relevant SONiC container.

### Tear down

```bash
make teardown
# or directly:
sudo containerlab destroy -t topologies/4p-8x16/topology.clab.yaml -c
```

## Routing model: no BGP, no IGP

Every node's `frr.conf` carries:

- A single SRv6 locator (`MAIN`) with `behavior usid`
- Static uA SIDs for each connected neighbor (in `f00<S>` / `e00<L>` form)
- Static uDT6 SID `d000` on leaves → `Vrf-green` (green decap)
- Static IPv6 routes for every other locator in the same plane, via the
  appropriate connected `/127`. Leaves install 8-way ECMP per remote leaf
  (one route via each spine); spines install one route per leaf.

This is the **minimum data plane** an SRv6 controller needs:

- Connected reachability so the outer-IPv6 destination of any encapsulated
  packet has a FIB entry.
- The full uA matrix so packets can hop spine ↔ leaf via uSID.
- `d000` on every leaf so any path landing there can decap into `Vrf-green`.

The controller layers on top:

- Tenant-prefix routes inside `Vrf-green` (host /64 reachability)
- SR policies / per-flow steering (e.g. plane affinity, congestion-aware
  scheduling)
- Yellow tenant routes (host-encap targets, plane selection)

## Tenant models in this lab

### Green (hybrid SRv6)

```
green-host00 NICs eth1..eth4   (anycast 2001:db8:bbbb:00::2 on all four)
   │ (encap by host or upstream controller; one of 4 NICs picked per packet)
   │  outer dst: fc00:000<P>:2<L>:f<S>:d000::         <P> = chosen plane
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
   │  encap; outer dst: fc00:000<P>:f<S>:e<L>:e009:d001::    <P> = chosen plane
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
planes: 2                # 48 SONiC + 32 hosts, 320 veth pairs
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
  and JSON reports `run-scenario` (`srv6_fabric/mrc/run.py`) produces.
- `./design-appendix.md` — rationale for the major design decisions, including
  §10 on the plane-independent inner addressing that makes spray work.


