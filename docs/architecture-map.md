# Architecture Map

A factual snapshot of the codebase as it exists today. Companion to
`architecture.md` (which describes intent and paper mapping). This
document describes:

1. Components and where they live
2. Process model — what runs where, how many copies
3. State ownership and lifecycle
4. Wire formats and communication paths
5. The seams (structural problems with evidence)
6. Test coverage map

This is descriptive, not prescriptive. Decisions about what to keep,
refactor, or rewrite belong in a separate Audit document.

---

## 1. Code inventory

```
srv6_mrc/                                         8325 LOC total
├── __init__.py                          7
├── topo.py                            489    fabric dims, uSID build, EV listing
├── encap.py                           131    raw-socket SRv6 outer packet builder
├── policy.py                          432    SprayPolicy: round_robin | hash5tuple
│                                             | weighted | ev_spray | health_aware_mrc
├── runner.py                          467    multi-flow sender/receiver core
├── reorder.py                         154    per-flow reorder histograms
├── netem.py                           328    tc/netem injection helpers
├── report.py                          448    JSON + ASCII report writer
├── cli/
│   ├── spray.py                       439    spray CLI entrypoint
│   ├── routes.py                      727    kubectl-style static SRv6 route manager
│   └── srctl.py                       517    top-level CLI (run / get evs / ...)
└── mrc/
    ├── run.py                         588    scenario orchestrator
    ├── scenario.py                    588    scenario YAML schema + executor
    ├── agent.py                       861    SenderMrcAgent + ReceiverMrcAgent
    ├── transport.py                   372    MrcTransport ABC + Srv6Raw + LoopbackUdp
    ├── ev_state.py                    550    EVStateTable: per-(tenant,plane,path) FSM
    ├── probe.py                       423    PROBE / PROBE_REPLY / LOSS_REPORT framing
    ├── probe_clock.py                 277    in-flight probe bookkeeping + sweep
    ├── loss_window.py                 246    receiver-side per-EV loss accounting
    └── loss_compute.py                266    sender-side SentWindowRing for loss correlation

tests/                                            5252 LOC, 394 tests
generators/                                       SONiC config + clab template
topologies/{2p-4x8,4p-4x8,4p-8x16}/               three deployable fabrics
```

LOC ratio test:source = 0.63. Higher than typical for this kind of
networking code, which reflects the integration-test heaviness of the
suite (transport-level loopback fakes, real-ish EV state machines under
unit test).

---

## 2. Process model

The system at runtime is **not** monolithic. A scenario run spawns and
coordinates processes across multiple containers.

```
┌────────────────────────────────────────────────────────────────┐
│  Topology host (Docker host running clab)                       │
│                                                                 │
│   srctl run <scenario>                                          │
│      │                                                          │
│      └─> srv6_mrc.mrc.run.main()  (one process)                 │
│             │                                                   │
│             ├─> docker exec yellow-host<R>  python -m           │
│             │     srv6_mrc.cli.spray --role recv ...            │
│             │   (one process per receiver host)                 │
│             │                                                   │
│             ├─> docker exec yellow-host<S>  python -m           │
│             │     srv6_mrc.cli.spray --role send ...            │
│             │   (one process per sender host; auto-spawns       │
│             │    SenderMrcAgent thread internally if            │
│             │    policy=health_aware_mrc)                       │
│             │                                                   │
│             ├─> [optional] docker exec spine/leaf  config       │
│             │     interface shutdown / tc netem ...             │
│             │                                                   │
│             └─> collect stdout JSON from each spray process     │
│                 → merge into results/<scenario>.json            │
└────────────────────────────────────────────────────────────────┘
```

Per-process responsibilities:

| Process            | What it owns                                    | Lives where         |
|--------------------|------------------------------------------------|---------------------|
| `srctl`            | argv parsing, topology inference, output       | topology host       |
| `mrc.run`          | scenario YAML, fork senders/recvs, merge       | topology host       |
| `spray --role recv`| listen on UDP/raw, count, reorder histogram, write JSON to stdout | container |
| `spray --role send`| send packets per policy, optionally embed `SenderMrcAgent` | container |
| `SenderMrcAgent` (thread inside sender) | emit probes, receive replies + LOSS_REPORTs, drive `EVStateTable` | container |
| `ReceiverMrcAgent` (in recv process)    | reply to PROBEs, accumulate per-EV loss windows, send LOSS_REPORT | container |

Key consequences:

- `EVStateTable` is **per-sender-process state**. Two senders on the
  same host (different tenants) have two independent tables. Across
  hosts there is no shared MRC state.
- The agent threads run inside the sender/receiver processes. They
  share Python module state (including `srv6_mrc.topo`'s
  module-level constants) with the spray loop.
- Receiver state (loss windows) **outlives any single flow** within
  a process but does not persist across scenario runs.

---

## 3. State ownership and lifecycle

Mutable state, ranked by scope:

### 3.1 Fabric-global, persistent across runs

| State | Owner | Lifetime | How modified |
|---|---|---|---|
| ConfigDB on each SONiC node | `redis` inside `p<P>-spine<S>` / `<tenant>-leaf<L>` containers | until container destroyed | `routes.py apply` writes via `sonic-cfggen`; `make destroy` clears |
| Kernel routes / addresses on host containers | Linux kernel inside `<tenant>-host<N>` | until container destroyed | `routes.py host-routes` via `nsenter` |
| Containerlab nodes themselves | clab + docker | until `make destroy` | `make deploy` |

### 3.2 Process-scoped, runtime-only

| State | Owner | Lifetime | How modified |
|---|---|---|---|
| `srv6_mrc.topo.NUM_PLANES` etc. | module globals | duration of process | **bound at first import**; SRV6_TOPO env-var read once |
| `EVStateTable` | `SenderMrcAgent` | duration of sender process | probe results + LOSS_REPORTs |
| `SentWindowRing` | sender's `loss_compute.py` | duration of sender process | every emitted data packet |
| Per-EV `LossWindow` | `ReceiverMrcAgent` | duration of receiver process | every received data packet |
| In-flight probes | `probe_clock.py` | until reply or timeout | probe emit / reply / sweep |
| Reorder histograms | `runner.py`'s recv loop | duration of receiver | every received data packet |

### 3.3 Communication patterns

```
sender process                                 receiver process
┌──────────────────────────┐                  ┌──────────────────────────┐
│ runner.py                │                  │ runner.py                │
│   for seq in range(N):   │  data packet     │   while time < end:      │
│     ev = policy.pick_ev  │ ───────────────> │     recv → reorder       │
│     emit(ev, payload)    │  raw SRv6 / UDP  │     LossWindow.observe   │
│       │                  │                  │       │                  │
│       └─ embeds (plane,  │                  │       │                  │
│          path) in pkt    │                  │       │                  │
└──────────────────────────┘                  └────────┬─────────────────┘
                                                       │
       ┌───────────────────────────────────────────────┘
       │  PROBE_REPLY (on each PROBE)
       │  LOSS_REPORT (periodic, summarizes recent windows)
       ▼
┌──────────────────────────┐                  ┌──────────────────────────┐
│ SenderMrcAgent           │  PROBE on EV     │ ReceiverMrcAgent         │
│   _emit_loop:            │ ───────────────> │   on PROBE: reply        │
│     for ev in evs:       │                  │   on LOSS_TIMER: send    │
│       send PROBE         │ <─────────────── │     LOSS_REPORT          │
│   _rx_loop:              │   PROBE_REPLY    │                          │
│     PROBE_REPLY -> good  │                  │                          │
│     LOSS_REPORT -> bad   │                  │                          │
│   updates EVStateTable   │                  │                          │
└──────────────────────────┘                  └──────────────────────────┘
       │
       ▼
   EVStateTable
   (per-EV state + weight)
       │
       ▼
   policy.pick_ev reads weights for next emit
```

---

## 4. Wire formats

| Frame | Direction | Layer 4 | Carrier | Where defined |
|---|---|---|---|---|
| Data packet | sender → receiver | UDP/9000 inside SRv6 outer | `eth(P+1)` per plane | `encap.py` builds outer; `runner.py` builds inner |
| PROBE | sender → receiver | UDP/9999 inside SRv6 outer | same as data | `mrc/probe.py::PROBE` |
| PROBE_REPLY | receiver → sender | UDP/9998 inside SRv6 outer | reply on same (plane,path) as inbound | `mrc/probe.py::PROBE_REPLY` |
| LOSS_REPORT | receiver → sender | UDP/9998 | one reverse plane | `mrc/probe.py::LOSS_REPORT` |
| Kernel encap (fallback) | host → host | per `seg6 encap` route | metric 100..103 per plane | `cli/routes.py::build_segs` |

The separation of UDP ports for control vs data is **historical**, not
load-bearing. PROBE/REPLY/LOSS_REPORT all use the same custom binary
framing in `mrc/probe.py`.

Important: PROBE_REPLY and LOSS_REPORT both target UDP/9998 on the
sender. There is one observed open issue (ICMPv6 port-unreachable on
:9999, parked) where reverse-direction probes hit a sender that hasn't
opened the port — race condition at startup or socket-bind issue.

---

## 5. Seams (with evidence)

These are structural problems where bugs cluster. Evidence is from the
last week of debugging.

### Seam 1: Topology dimensions as a global side-effect

**The structure:** `srv6_mrc.topo` reads `SRV6_TOPO` env-var at first
import and freezes `NUM_PLANES`, `NUM_SPINES`, `NUM_LEAVES` as module
constants. Every CLI entrypoint must set `SRV6_TOPO` before importing
anything from `srv6_mrc`.

**The consequence:** Topology inference logic is duplicated across:
- `Makefile` (sets SRV6_TOPO inline for `scenario` target)
- `srv6_mrc/mrc/run.py` (`_infer_srv6_topo_from_argv`)
- `srv6_mrc/cli/srctl.py` (`_infer_srv6_topo_from_argv`)

**Bugs traced to this seam:**
- `b2728b2` — `make scenario` was reading wrong topology (manual fix)
- `1099536` — sentinel-by-`clab-` prefix never worked (clab uses `prefix:""`)
- `d2aea0e` — `srctl get` subcommands missed inference entirely
- `0e94344` — leaves-only sentinel collided 2p-4x8 with 4p-4x8
- `953f4a6` — sentinel KeyError on wrong dict key, hidden by bare `except`
- `90b6f4d` — added stderr warning to surface future schema drift

Six commits in two weeks, all working around the same architectural
choice. Each band-aid was correct given the constraint; the constraint
itself is the problem.

**Cost of the seam:** ~150 LOC of inference + try/except across three
entrypoints. Schema-drift fragility (unique to bare `except` in
`_infer_srv6_topo_from_argv`). Cognitive load: every new CLI command
needs to remember to do the SRV6_TOPO dance before importing anything.

### Seam 2: Sender/receiver state divergence

**The structure:** EV health is computed from two independent signals:
- Probes (sender-driven; sender knows when reply is late)
- Per-EV loss windows (receiver-driven; receiver computes loss ratio
  from observed gaps in per-EV sequence)

These are merged in `EVStateTable` via implicit rules
(`consecutive_loss_demote_windows >= 2` AND `last_loss_ratio >=
loss_threshold` → demote). The sender has full visibility into the
probe signal and only delayed/aggregated visibility into the loss
signal (via LOSS_REPORT messages from the receiver).

**Bugs traced to this seam:**
- Bug #1 (ongoing): probe thundering-herd at 4p-8x16 ring scale →
  295 consecutive_probe_timeouts/EV → cascading demotion of healthy
  EVs. The pacing patch was the band-aid.
- Bug #4 (observed once, not yet reproduced): `last_loss_ratio: 0.4`
  on EVs whose flow shows `sent == rx`. Receiver believes loss
  occurred; data path delivered everything. Sender demoted based
  on receiver's wrong belief.
- Open question (today): why are 8/16 EVs unused in the green
  baseline? `_weighted_pick` is per-packet; equal weights should
  hit every EV. Either `weights_ev` returns asymmetric weights for
  reasons we don't yet understand, or the unused-EV detector in
  the report is broken.

**Cost of the seam:** Hard to debug — the divergence only matters at
scale (16 bidirectional flows on 4p-8x16, not 4 on 4p-4x8). Hard to
test — the unit tests use loopback transports that don't reproduce
the timing races.

### Seam 3: Convenience accumulation

**The structure:** Several quality-of-life features were added
incrementally:
- `<tenant>-pairs` auto-sized aliases
- `src/dst: all` keyword in routes
- Bare-name scenario lookup via topology scan
- Host-count sentinel for live-topology detection

**The consequence:** "What is actually being run?" is non-obvious. A
scenario name like `yellow-mrc-baseline` triggers:
1. Topology inference (which topology?)
2. Pairs alias resolution (which pairs?)
3. Routes keyword expansion (which src→dst pairs?)
4. Policy resolution (which policy class with which args?)

Each is debuggable in isolation; the chain is not. When a result looks
wrong, narrowing it down requires walking the chain.

**Bugs traced to this seam:** None *correctness* bugs, but several
"wait, what scenario am I actually running?" debug detours.

**Cost:** Medium. Removable with explicit "show me the resolved plan"
output. Not architectural rot, but accumulated friction.

---

## 6. Test coverage map

394 tests, 5252 LOC. Coverage is **uneven** by component:

| Component | Test file(s) | Coverage shape |
|---|---|---|
| `topo.py` | `test_topo.py` | Strong: addressing, EV enumeration, sentinel selection |
| `policy.py` | `test_policy.py` | Strong: all 5 policies, weight derivation, picker determinism |
| `runner.py` | `test_runner.py` | Medium: send/recv loop with loopback transport |
| `reorder.py` | `test_reorder.py` | Strong: histogram math, edge cases |
| `report.py` | `test_report.py` | Medium: JSON shape, summary formatting |
| `netem.py` | `test_netem.py` | Strong: command construction (no real tc) |
| `mrc/ev_state.py` | `test_ev_state.py` | Strong: state transitions, floor, weights |
| `mrc/probe.py` | `test_probe.py` | Strong: framing, parsing |
| `mrc/agent.py` | `test_mrc_agent_io.py`, `test_mrc_agent_logic.py` | **Mixed**: io tests use loopback transport; logic tests stub out the world. Pacing tests added recently but timing races at scale not exercised. |
| `mrc/run.py` | `tests/mrc/test_run.py` | Medium: scenario parse + dispatch, no real subprocesses |
| `mrc/scenario.py` | `tests/mrc/test_scenario.py` | Strong: YAML schema, alias resolution |
| Integration | `test_mrc_integration.py` | Medium: in-process end-to-end with fake transport. **One known order-flake**: `PlaneLossShiftsDistributionTests` |
| `cli/srctl.py` | `test_srctl.py` | **Weak**: argv parsing only; topology inference NOT tested (the bug source) |
| `cli/routes.py` | `test_routes_topo_inference.py` | Medium: dry-run scenarios across topologies |
| `cli/spray.py` | none directly | **Untested**: relies on integration tests |

**Where the tests don't reach:**
- Topology inference logic (Seam 1) — three implementations, zero tests.
  Every bug found here was found in production.
- Real timing races at scale (Seam 2) — the loopback transport collapses
  network-induced timing, hiding probe thundering-herd until it surfaces
  on real fabric.
- Sender↔receiver process boundaries — all integration tests run
  in-process with shared state. The actual process model from §2 is
  exercised only by manual `srctl run` invocations.

This explains the bug pattern. The tests verify per-module logic; the
bugs live in cross-module coordination and at-scale timing.

---

## 7. Summary observations

These are observations, not recommendations. Recommendations belong in
the Audit pass.

1. **Two seams account for ~80% of recent bugs.** Seam 1 (topology
   globals) and Seam 2 (sender/receiver divergence). Seam 3
   (convenience accumulation) contributes friction but not correctness
   bugs.

2. **The codebase is more tested than typical, but the tests don't
   cover the bug surface.** Tests are concentrated in pure-logic
   modules (policy, reorder, ev_state, probe parsing). Cross-process
   coordination and scale-dependent timing are essentially untested.

3. **Process model is not reflected in module structure.** Sender,
   receiver, and orchestrator code intermix in `runner.py`,
   `mrc/agent.py`, `cli/spray.py`. This is fine when the boundaries
   match what they should be, but it makes "which process owns this
   state" require reading multiple files.

4. **Bug density is not uniform across components.** Topology
   inference, EV state at scale, and report-side derived counts
   (unused-EV warnings, orphan-flow detection) are bug-dense. Encap,
   probe framing, EV state machine logic, and reorder accounting are
   essentially bug-free.

5. **There is no documented "what happens when I type srctl run X"
   sequence diagram.** The Map in §2-§3 of this document is the
   first complete one. Future contributors (including future me)
   would benefit from this being kept current.
