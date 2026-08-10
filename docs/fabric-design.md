# 4-Plane SRv6 Fabric 

A SRv6 (uSID) lab built on top of `docker-sonic-vs` +
Containerlab. Models a small slice of a hyperscale AI backend fabric: 4
independent network planes, each a **4 × 8 Clos** (with larger 8 × 16 variant
available), with multi-homed tenant hosts uplinked into every plane.

### Default topology
- **48 SONiC switches** (16 spines + 32 leaves) in the default 4p-4x8 topology
- **16 Alpine hosts** (8 green tenant + 8 yellow tenant), each with 4 NIC uplinks
- **No BGP, no IGP** — every transit FIB entry is a static route or an SRv6 uA
  SID; the controller installs end-to-end SR policies for tenant traffic.

## Quickstart
Instructions to quickly deploy and play with the topology can be found in the [quickstart.md](./quickstart.md) guide

## Fabric design

High level architecture:

1. **Multi-plane Clos** — each plane is an independent failure / scheduling
   domain. Hosts have one NIC into each plane. In a production deployment the GPU-NIC
   is broken out across 4 or 8 planes. With containerlab we emulate the breakout 
   by simply using more veths and assigning the same ipv6 address to each.
2. **Per-plane uSID block** — each plane gets its own IPv6 `/32` so plane identity
   is part of the destination prefix, not buried in node bits. This can also aggregate
   cleanly at the WAN: one `/30` per cluster.
3. **Function-bit conventions across the fabric**:
    — `f000 - f0ff`: reserved for northbound uA allocation (leaf up to spine, spine up to super-spine, etc.)
    - `e000 - e0ff`: reserved for southbound uA allocation (spine down to leaf, leaf down to host, etc.)
    - `d000 - dfff`: reserved for tenant-ID uDT6 SIDs. 
    - These allocations are a reference design and could certainly be adjusted depending on the deployment
4. **Two SRv6 multi-tenancy models**:
    - **Hybrid** (green): host-encap, leaf-decap into `Vrf-green` via uDT6. Note Aug. 10, 2026: As deployments and operational models mature we may see this option deprecated
    - **Host-based** (yellow): host-encap, host-decap. Leaves are pure transit;
      yellow hosts run their own `seg6local End.DT6` on every plane NIC.

## Addressing

### IPv6 layout

| Element | Pattern | Example |
|---|---|---|
| Cluster aggregate | `fc00:0000::/30` | covers all 4 planes |
| Plane block | `fc00:000<P>::/32` | plane 2 → `fc00:0002::/32` |
| Spine locator | `fc00:000<P>:1<S>::/48` | p2-spine03 → `fc00:0002:13::/48` [Link](../topologies/4p-4x8/config/p2-spine03/frr.conf#L22) |
| Leaf locator | `fc00:000<P>:2<L>::/48` | p2-leaf07 → `fc00:0002:7::/48` [Link](../topologies/4p-4x8/config/p2-leaf07/frr.conf#L56)|
| Leaf uA → spine | `fc00:000<P>:f00<S>::/48` | p2 leaf, toward spine03 → `fc00:0002:f003::/48` [Link](../topologies/4p-4x8/config/p2-leaf07/frr.conf#L60) |
| Spine uA → leaf | `fc00:000<P>:e00<L>::/48` | p2 spine, toward leaf06 → `fc00:0002:e006::/48` [Link](../topologies/4p-4x8/config/p2-spine03/frr.conf#L29) |
| Green tenant uDT6 | `fc00:000<P>:d000::/48` | per-plane on every leaf, decap into `sonic Vrf-green` [Link](../topologies/4p-4x8/config/p0-leaf00/frr.conf#L61) |
| Yellow tenant uDT6 | `fc00:000<P>:d001::/48` | per-plane on every yellow host, `linux End.DT6 table 0` [Note: see host config, not FRR] |
| Fabric P2P | `2001:db8:fab:<S*16+L>::/127` | reused per plane (planes are L2-isolated) |
| Green tenant address | `2001:db8:bbbb:<NN>::2` | **anycast** on all 4 host NICs (`nodad`); identical leaf-side `::1/64` on every plane's Ethernet32 in `Vrf-green` |
| Yellow tenant address | `2001:db8:cccc:<NN>::2` | **anycast** on all 4 host NICs + `lo` (`nodad`); identical leaf-side `::1/64` on every plane's Ethernet36 (Phase 1a: mirrors green's plan with `bbbb`→`cccc`) |

`<P>` = plane 0–3 (hex), `<S>` = spine 0–7, `<L>` = leaf 0–f, `<NN>` = host 00–15 (hex byte).

### IPv4 (loopback only — for FRR router-id)

| Element | Pattern |
|---|---|
| Spine loopback | `10.0.<P>.<S+1>` |
| Leaf loopback | `10.1.<P>.<L+1>` |

### Reading a SR-policy SID list

A path "deliver to green-host06, via EV plane-2, spine03, leaf06,
then decap into green VRF" encodes as a single uSID-compressed
IPv6 destination:

```
fc00:0002:f003:e006:d000::
└──┬───┘ └┬──┘ └┬─┘ └┬─┘
   │      │     │    └─ d000  : tenant-ID green → Vrf-green at egress leaf
   │      │     └────── e006  : spine03 uA toward p2-leaf06 
   │      └──────────── f003  : p2-leaf uA toward p2-spine03 
   └─────────────────── 0002  : plane 2 block
```

## Tenant models in this lab

### Green (hybrid: host-encap, egress-leaf-decap)

```
green-host00 NICs eth1..eth4   (anycast 2001:db8:bbbb:<NN>::2 on all four)
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
yellow-host00 NICs eth1..eth4    (inner anycast 2001:db8:cccc:<NN>::2 on all four)                        
   │  encap; outer dst: fc00:000<P>:f00<S>:e00<L>:e009:d001::  <P> = chosen plane
   │  (e009 is the leaf's uA toward yellow host port [config](../topologies/4p-4x8/config/p0-leaf00/frr.conf#L62))
   ▼
   ─►  fabric (uA hops)  ─►  egress p<P>-leaf<NN>.Ethernet36 (default VRF)
                               ─►  yellow-host<NN>.eth(P+1) [inner 2001:db8:cccc:<NN>::2]
                                    seg6local End.DT6 table 0 → decap →
                                    table-0 lookup hits 2001:db8:cccc:<NN>::2
                                    (present on eth1..eth4 + lo, nodad)
```

Each yellow host has 4 `seg6local` entries — one per plane — bound to the
respective plane NIC. The address present on `lo` guarantees table-0 lookup resolves
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


## What this lab is *not*

- **Not a performance benchmark.** `docker-sonic-vs` runs a software ASIC; expect a
  maximum of a few hundred pps per EV. The goal is to demonstrate MRC-style per-EV packet spray and SRv6 forwarding behavior.
- **Not a full controller.** No PCEP/BGP-LS/path-computation engine is
  included. The static SIDs and routes provide a template; a controller for programming
  end-to-end SR policies (per-EV SID calculation) has yet to be developed



