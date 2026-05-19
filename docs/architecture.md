# Architecture — three-role decomposition

This document is the architectural reference for the emulator. It is the
companion to `design-fabric.md` (the SRv6 substrate), `design-mrc.md` (the
spray + EV state-machine layer) and `design-multi-tenant.md` (green vs yellow
tenancy). Where those documents describe **what** each subsystem does, this
one describes **why the layers are structured the way they are** and how
that structure maps to the production system in the OpenAI/Microsoft MRC +
SRv6 paper (`resilient-ai-supercomputer-networking-using-mrc-and-srv6.md`).

In a production MRC deployment there are three clearly separated systems or roles,
which the MRC emulator tries to capture or model:

| Role            | Production analog                          | Emulator analog                                   |
|-----------------|--------------------------------------------|---------------------------------------------------|
| **Workload**    | CCL / NCCL / `ib_write_bw` on the GPU host | Spray runner's traffic generator                  |
| **NIC**         | MRC firmware on CX-8 / Pollara / Thor Ultra| Per-host MRC agent + raw-socket encap fast-path   |
| **Fabric**      | T0 / T1 switches running SRv6 (uN)         | docker-sonic-vs nodes with static SRv6 (uA) routes|

These roles have very different addressing concerns:

- **Workload only sees inner addresses.** The application calls
  `ibv_post_send` against a remote NIC's inner address. It has no concept
  of plane, EV, T0 uplink, or uSID. In the emulator the analog is "the
  workload addresses a remote *host*, not a remote *plane on a host*."
- **The NIC owns encap.** It picks an EV per packet, derives the outer
  SRv6 destination address from a per-QP template, stamps the EV across
  UDP source port + IPv6 flow label, and emits the packet on the chosen
  plane's port. It also owns the EV state machine (active set, backup set,
  probes, ECN echo, SACK/NACK processing).
- **The fabric is dumb.** Switches consume uSIDs hop-by-hop. Their
  forwarding tables are static and do not change in response to failures.

In our current code these three roles are interleaved across `runner.py`,
`mrc/agent.py`, `cli/spray.py` and `cli/routes.py`. Module restructure to
match the three-role boundary is **Phase 1** work tracked in
`design-mrc.md`.

## 2. Addressing model — the rule we keep getting wrong

Every host has two address classes. Confusing them is the cause of the
yellow-tenant probe regression that triggered this document.

### 2.1 Inner addresses (what the workload sees)

Inner addresses are **plane-independent identities** for a host's NIC.
The workload uses these as packet destinations.

| Tenant | Inner address      | Where it lives                                     |
|--------|--------------------|----------------------------------------------------|
| green  | `bbbb:<NN>::2`     | `eth1..eth4` anycast (no-DAD) on the host          |
| yellow | `cccc:<NN>::2`     | `eth1..eth4` + `lo` anycast (no-DAD) on the host   |

`<NN>` is the host ordinal. For both tenants the same address is
reachable on every plane interface — the addressing plan is symmetric
(`bbbb`→`cccc`). The tenants differ only in *where decap happens*:
green decaps on the egress leaf (uDT6 into `Vrf-green`); yellow decaps
on the host itself (`seg6local End.DT6` on each `eth1..eth4`, with the
anycast also present on `lo` so the table-0 lookup resolves locally
regardless of which NIC the inner packet arrived on).

**This is the only address the workload should ever name as a
destination.** If a workload-layer call site reaches for a per-plane
underlay address, that is a bug.

### 2.2 Underlay / SRv6 destination addresses (what the NIC writes on the wire)

The NIC layer derives the outer IPv6 destination address from:

1. an inner address (which identifies *which host*), and
2. an EV (which selects *which plane and which path through that plane*).

In our emulator the EV granularity is **one EV per plane** (4 EVs per QP),
so "select EV" reduces to "select plane". For each `(inner_address, plane)`
pair the NIC emits a packet whose outer destination is a uSID list
encoding the chosen plane's path to the destination T0:

```
fc00:<P>:f00<S>:e00<L>:e009:d001::
       │   │      │      │
       │   │      │      └── End.X to host downlink on leaf L
       │   │      └── leaf uSID
       │   └── spine uSID
       └── plane index
```

The per-host underlay addresses (`cccc:<P><NN>::2` on yellow eth(P+1),
`bbbb:<NN>::2` on green eth(P+1)) are **NIC-internal**. They are the
source addresses that the kernel stamps onto post-encap packets when those
packets egress the per-plane interface. They are not destinations a
workload, probe, or MRC control packet should target.

### 2.3 The probe-addressing bug, stated as a rule violation

The yellow-tenant probe regression (pre-Phase-1a) was a workload-layer
call (the MRC probe sender) using a NIC-internal source address
(`cccc:<P><NN>::2` — the legacy per-plane underlay) as its destination.
Kernel `seg6 encap` routes were only installed for the inner address
(historically `cccd:<NN>::1/128` with four metrics, one per plane).
Targeting `cccc:<P><NN>::2` found no encap route, fell back to the
underlay default, and was dropped at the leaf.

The Phase-0 fix was to name the inner address. Phase 1a removes the
ambiguity entirely: the per-plane underlay address is retired for
yellow, and the inner address itself moves to anycast `cccc:<NN>::2`
on `eth1..eth4` + `lo` — the same plan green has used since the
original redesign.

The longer-term structural fix is to make `host_underlay_addr` private
to the NIC layer (`_host_underlay_addr`) so workload-layer code
physically cannot reach for it.

### 2.4 Addressing summary

```
  Workload says:   "send to host NN"
                          │
                          ▼
                   inner address
                  (bbbb:NN::2  or  cccc:NN::2)
                          │
                          ▼
  NIC says:       "EV[k] picks plane P;
                   template[plane=P, dst=NN]
                   becomes outer DA"
                          │
                          ▼
                   uSID list
                  (fc00:P:f00S:e00L:e009:d001::)
                          │
                          ▼
  Fabric says:    "consume uSID, left-shift, forward"
```

## 3. Mapping to the paper (Fig. 3)

The paper's Fig. 3 (Sec. 2.3, "Creating the SRv6 address from an EV and
template") describes a two-step specialization:

```
  QP startup                         each data packet
  ┌─────────────────────────┐        ┌─────────────────────────┐
  │ Load row template:      │        │ Pick EV[k] from QP set  │
  │ - chain: T0│T1│T0│dst   │   ──▶  │ - plane bits  → every   │
  │ - specialize dst uSID   │        │   uSID (same plane)     │
  │   with last-hop downlink│        │ - T0 uplink # → T1 uSID │
  └─────────────────────────┘        └─────────────────────────┘
                                                │
                                                ▼
                                         outer dst IPv6
```

Our emulator does the analog of this in two places. `srv6_mrc/cli/routes.py`
(`build_segs`, `inner_addr`) installs kernel `seg6 encap` routes
keyed by inner address with one route per plane (metrics 100..103) —
this is the *kernel-encap* data path, used by simple connectivity
tests (ping, etc.) and as a debugging fallback. The MRC data and
control paths (EV-spray, probes, loss reports) bypass these routes
and build the outer packet in user space via
`srv6_mrc.encap.build_outer_packet`, so the "EV[k] picks plane"
step happens in the sender process — not in the FIB. Plane selection
on the wire is still NIC-bound via `SO_BINDTODEVICE` on `eth(P+1)`
(invariant 8); spine entropy comes from per-packet outer-DA rotation
(invariant 10), not kernel ECMP.

Differences worth flagging:

- **uA vs uN.** The paper uses uN (each switch named, End behavior). We
  use uA (segments encode adjacencies, End.X behavior). This is a
  customer preference; it changes the *encoding* of the uSID list but
  not the *control loop*.
- **Per-EV path granularity.** The paper has hundreds of EVs per QP,
  each a distinct path. Until Phase 1b we had one EV per plane and
  relied on the leaf to ECMP across spines internally; as of Phase 1b
  step 1 the EV-spray data path varies the spine per packet, and as
  of step 2 the MRC control plane (probes + loss reports) tracks the
  same per-`(plane, path)` EV. Per-EV state attribution is the
  prerequisite for Phase 1b step 3 (per-EV health-aware policy).
- **NIC-firmware vs user-space encap.** The paper does the EV→template
  specialization in NIC firmware on every packet. Our emulator does
  the equivalent specialization in user space (scapy / raw socket)
  inside the sender process. The simple-ping path still uses
  pre-installed kernel `seg6 encap` routes as a fallback. Both
  produce the same bits on the wire.

## 4. What we faithfully reproduce vs what we approximate

This is the canonical "where we diverge" table. It supersedes scattered
remarks in `design-mrc.md` and `design-multi-tenant.md`.

| Concern                          | Paper                                  | Emulator                              | Why                                              |
|----------------------------------|----------------------------------------|---------------------------------------|--------------------------------------------------|
| Topology                         | 2-tier multi-plane Clos                | 2-tier 4-plane Clos                   | Match                                            |
| Switch fabric                    | Hardware, line rate                    | docker-sonic-vs                       | Lab constraint                                   |
| Routing                          | Static SRv6 uN                         | Static SRv6 uA                        | Customer preference                              |
| PFC                              | Disabled                               | N/A (no RDMA)                         | Match in spirit                                  |
| Transport                        | RoCEv2 + MRC extensions                | Plain UDP spray                       | Verbs unavailable in emulator                    |
| EV granularity                   | 100–256 EVs per QP                     | 4 EVs per QP (1 per plane)            | Plane-level fidelity is enough for the scenarios |
| Spray policy                     | EV[k] rotated per packet               | Round-robin / hash / health-aware     | Match in spirit at plane granularity             |
| Loss signal — congestion         | Packet trimming → NACK → fast retx     | **Not available**                     | docker-sonic-vs cannot trim                      |
| Loss signal — failure            | Untrimmed loss → demote EV             | Receiver loss-fusion → demote plane   | The only loss signal we have                     |
| Load-balance signal              | ECN → migrate EV within plane          | **Not implemented**                   | Plane granularity makes this not directly useful |
| Probes                           | Background EV resurrection probes      | Per-plane MRC probes                  | Match in spirit                                  |
| Fabric health mapping            | Clustermapper (1 ms/link)              | **Not implemented**                   | Out of scope for current phases                  |
| Reverse-path EV management       | Small reverse EV set + EV probes       | Stateless replies                     | Plane granularity makes this not directly useful |
| EV state machine                 | active / backup / inactive             | UNKNOWN / GOOD / ASSUMED_BAD          | Equivalent at plane granularity                  |
| EV demotion threshold            | Binary (first untrimmed loss)          | Configurable per scenario             | We need knobs because we have one signal         |

The single largest fidelity gap is the loss-signal collapse: the paper
distinguishes three signals (trim, untrimmed-loss, ECN) that drive three
different responses (retransmit, demote, rebalance). The emulator has
only the middle one, so it conflates congestion with failure. This is
acceptable for scenarios that inject *explicit* path failures (the
existing `plane-loss`, `plane-blackhole`, `plane-latency` scenarios) but
will produce false positives under genuine congestion. Scenarios are
designed accordingly: we never run incast against MRC.

## 5. Where the code is going (Phase 1)

The Phase 1 module restructure tracked in `design-mrc.md` will physically
separate the three roles:

```
srv6_mrc/
├── workload/       # generators that name inner addresses only
│   ├── spray.py    # current cli/spray.py minus the encap fast-path
│   └── patterns.py # collective-shaped traffic (Phase 2)
├── nic/            # everything that names a plane, EV, or uSID
│   ├── encap.py    # current runner.py raw-socket fast-path
│   ├── mrc/        # current mrc/* (agent, ev_state, probe, policy)
│   └── plane.py    # SO_BINDTODEVICE plane selection
├── fabric/         # everything that touches SONiC ConfigDB / kernel routes
│   └── routes.py   # current cli/routes.py
└── netem.py        # fault injection, unchanged
```

The yellow-tenant probe fix is the smallest possible Phase 0 change that
respects the addressing rule: have the probe sender name the inner
address. The architectural cleanup (making `host_underlay_addr` private,
introducing dedicated MRC anycast for yellow if needed) is Phase 1.
