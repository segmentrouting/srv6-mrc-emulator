# MRC layer

The static SRv6 substrate in the parent directory is the *fabric*. This
subdirectory adds the **MRC behaviors** that ride on top of it: spray policy,
reorder measurement, multi-pair flow generation, plane failure injection, and
plane-health signaling back to the senders.

Read this in the context of:

- `../README.md` — the 4-plane Clos and uSID address scheme
- `../design-appendix.md` §10 — the plane-independent inner addressing
  invariant MRC relies on
- `./spray-protocol.md` — the round-robin sprayer this layer extends
- The OpenAI MRC + SRv6 paper:
  https://cdn.openai.com/pdf/resilient-ai-supercomputer-networking-using-mrc-and-srv6.pdf
- The OCP Multipath Reliable Connection spec:
  https://github.com/opencomputeproject/OCP-Multipath-Reliable-Connection
  (see §"OCP mapping" below for what we faithfully reproduce and what we
  approximate)

## What MRC needs that the fabric doesn't provide

| Requirement | Where it lives |
|---|---|
| One logical flow → many planes (spray) | `srv6_fabric/cli/spray.py` (CLI: `spray`) |
| Plane choice per packet by **policy** (hash / weighted / health-aware) | `srv6_fabric/policy.py` |
| Many concurrent flows in one test run | `srv6_fabric/runner.py` |
| Per-flow reorder distance measurement at receiver | `srv6_fabric/reorder.py` |
| Plane failure injection (loss, delay, blackhole) | `topologies/<name>/scenarios/*.yaml` driving `tc netem` via `srv6_fabric/netem.py` |
| Plane-health signal from fabric → host | EV Probes + receiver loss feedback (see *Detection & re-spray* below) |
| Run orchestration (start recv on N hosts, drive senders, collect) | `srv6_fabric/mrc/run.py` (CLI: `run-scenario`) |
| Result collection / per-scenario reports | `srv6_fabric/report.py` |

## Module layout

```
srv6_fabric/
├── topo.py              # fabric constants + addressing (reads topo.yaml)
├── policy.py            # SprayPolicy: round_robin | hash5tuple | weighted | health_aware
├── runner.py            # multi-flow send/recv core (engine behind `spray`)
├── reorder.py           # per-flow reorder distance histograms
├── health.py            # legacy: per-plane ICMPv6 probe (superseded by mrc/ev_state.py)
├── netem.py             # tc/netem injection helpers (runs on the docker host)
├── report.py            # write JSON + ascii summary
├── cli/
│   ├── spray.py         # userspace SRv6 spray CLI
│   └── routes.py        # kubectl-style static SRv6 route manager
└── mrc/
    ├── run.py           # orchestrator: parse scenario, drive senders, merge
    ├── scenario.py      # YAML schema + executor
    ├── ev_state.py      # per-(tenant,plane) EV state machine (GOOD/ASSUMED_BAD/UNKNOWN)
    ├── probe.py         # PROBE / PROBE_REPLY / LOSS_REPORT packet build/parse
    └── policy.py        # health_aware policy wired to EVStateTable

topologies/<name>/scenarios/
├── baseline.yaml        # 16 flows, round-robin, no failure
├── hash5tuple.yaml      # 16 flows, 5-tuple hash policy, no failure
├── plane-loss.yaml      # 16 flows + 5% loss on plane 2
├── plane-blackhole.yaml # 16 flows + plane 2 blackhole
└── plane-latency.yaml   # 16 flows + 50ms added latency on plane 2
```

## Data model

### Scenario (YAML, consumed by `run-scenario`)

```yaml
name: plane-loss-5pct
description: 16 concurrent green flows, round-robin spray, 5% loss on plane 2

flows:
  - pairs: green-pairs-8       # named set in topo.py (8 pairs, 16 hosts)
    policy: round_robin
    rate: 1000pps
    duration: 30s

faults:                        # optional; applied before flows start
  - kind: netem
    target: plane 2
    spec: loss 5%

report:
  out: results/plane-loss-5pct.json
```

### Per-flow record (in result JSON)

```json
{
  "src": "green-host00",
  "dst": "green-host15",
  "policy": "round_robin",
  "sent": 30000,
  "received": 28500,
  "loss": 1500,
  "per_plane_sent":  {"0": 7500, "1": 7500, "2": 7500, "3": 7500},
  "per_plane_recv":  {"0": 7500, "1": 7500, "2": 6000, "3": 7500},
  "per_plane_loss":  {"0": 0,    "1": 0,    "2": 1500, "3": 0},
  "reorder_hist": {"0": 26880, "1": 1200, "2": 320, "3": 80, "...": "..."},
  "reorder_max": 41,
  "reorder_mean": 0.62
}
```

`reorder_hist[k]` = number of packets whose sequence number arrived `k`
positions earlier than the maximum seq seen so far in this flow. `k=0` =
in-order. The aggregate `reorder_max` and `reorder_mean` summarize the same
distribution for quick eyeballing.

## Spray policies

| Name | Behavior | Notes |
|---|---|---|
| `round_robin` | `plane = seq % 4` | Current `spray.py` behavior; trivially balanced; ignores plane health. |
| `hash5tuple` | `plane = hash(src, dst, sport, dport, proto) % 4` | Per-flow plane affinity; mimics ECMP. Single flow → single plane unless tuple varies. |
| `weighted` | Plane probabilities from scenario YAML | E.g. `[0.4, 0.3, 0.2, 0.1]`; used to model TE / congestion bias. |
| `health_aware_mrc` | Weighted RR with plane weights derived from EV state (`GOOD=1.0`, `UNKNOWN=0.5`, `ASSUMED_BAD=0.0`), subject to `min_active_evs` floor | Reads from `mrc/ev_state.py` `EVStateTable`, which is fed by EV Probes and receiver loss-feedback. See *Detection & re-spray* below. |

Policies share a tiny interface in `policy.py`:

```python
class SprayPolicy:
    def pick(self, seq: int, flow: FlowKey) -> int: ...
```

## Reorder metric

Per-flow at the receiver. For each (src, dst, sport, dport) tuple:

1. Maintain `max_seq_seen`.
2. For each arriving packet with seq `s`:
   - if `s > max_seq_seen`: bin 0 (in-order); update max.
   - else: bin `max_seq_seen - s` (how far behind it arrived).
3. At end of flow, emit histogram + `max` + `mean` + `p99`.

This matches the OpenAI paper's reorder definition closely enough for
comparison. The metric is computed entirely in the receiver process — no
extra protocol additions, no time sync needed.

## Failure injection — `tc/netem` on host veths

Choice rationale (from the scoping discussion): `tc netem` on the host-side
veth is the cleanest place because

1. SONiC's view of the fabric is untouched (no need to reload configs).
2. Failures are scoped to one host's view of one plane — closer to a real
   NIC/optic failure than a fabric link drop.
3. Easy to compose: loss + latency + reorder are all `tc qdisc` options.
4. Trivially reversible: `tc qdisc del`.

Injection runs on the **container host**, not inside the container, because
`tc` on a veth peer needs root in the host netns. `srv6_fabric/netem.py` shells
out to `ip netns` and `tc` via `docker inspect`-resolved netns paths, with
the same per-plane mapping the senders already use.

Targets supported:

```
target: plane N                 # all 32 host-side NICs in plane N
target: host green-host00       # all 4 NICs of one host
target: host green-host00 plane 2   # one (host, plane) pair
```

## Detection & re-spray (MRC core)

This is the brain of the MRC layer: how a sender decides a plane is
unhealthy and how it re-weights spray in response. Two independent signal
sources feed a shared per-(tenant, plane) state machine; a policy
(`health_aware`) reads that state and biases plane selection.

### OCP mapping

The OCP MRC API
(https://github.com/opencomputeproject/OCP-Multipath-Reliable-Connection)
is a NIC-side libibverbs-style API. Its core abstractions:

| OCP concept | What it is | Our analogue |
|---|---|---|
| **EV** (Entropy Value) | A bit-string that selects a network path. In `MRC_CTL_EV_FMT_MODE_SRV6`, the first 16B is outer IPv6 DA and the second 16B is a single-segment SRH segment. | One of our N per-plane SID-lists. Tenant-aware: `(tenant, plane) → SID-list`. |
| **EV Profile** | The set of EVs the NIC may spray across, plus mode (AUTO / EXPLICIT / GEN). | The full per-tenant SID-list table. We are always EXPLICIT mode. |
| **EV State** | `GOOD` / `ASSUMED_BAD` / `DENIED` / `UNKNOWN`. | We use `GOOD` / `ASSUMED_BAD` / `UNKNOWN`. `DENIED` is fabric-admin-only and not modeled. |
| **EV Event** | NIC firmware → controller async notification of EV state transitions, drained from a dedicated CQ. | A callback on the sender's `EVStateTable` whenever a transition crosses thresholds. Logged + counted in the report. |
| **EV Probe** (`MRC_CTL_EP_OP_EV_PROBE`) | NIC sends a small out-of-band probe along an explicit EV; responder echoes; NIC measures RTT or times out. | Implemented faithfully in `mrc/probe.py` — see *Probe wire format* below. |
| **TRIM NACK** (`MRC_DEVICE_CAP_TRIM_NACK`) | Fabric trims a congestion-dropped packet to its header; responder NIC generates a NACK to the requester. | **Not modeled.** docker-sonic-vs does not support packet trimming. We substitute receiver-side loss-feedback (below), which is a coarser but trim-free analogue. |
| **NSCC CC** (`uet-1.0-nscc`) | NIC-resident congestion control consuming per-EV RTT and queueing delay; produces per-EV rate adjustments and `ASSUMED_BAD` demotions. | Deferred. The current `EVStateTable` does on/off demotion only; per-EV rate control is a future extension (see roadmap). |
| **`ev_min_active`** | Minimum number of EVs that must remain `GOOD`; firmware refuses to demote past this floor. | Honored: when fewer than `mrc_min_active_evs` are `GOOD`, the state machine logs a warning and lets the otherwise-doomed plane stay nominally up. Spray continues to spread across whatever is left rather than collapsing onto one plane. |

What this simulator does **not** attempt to be: a wire-faithful
reimplementation of the OCP RDMA transport. We share the control-plane
*model* (EVs, EV state, EV Events, EV Probes, ev_min_active); we do not
share the data-plane (RDMA, WIMM, MPR, retry counters). Our spray runs
over plain UDP/SRv6.

### Signal sources

**1. EV Probes (active, OCP-faithful).** Every `probe_interval_ms` the
sender emits one `PROBE` packet per (tenant, plane) along that plane's
SID-list. The receiver echoes a `PROBE_REPLY` immediately. Sender
measures RTT.

- `probe_fail_threshold` consecutive timeouts (no reply within
  `probe_timeout_ms`) → plane transitions to `ASSUMED_BAD`.
- `probe_recover_threshold` consecutive successful probes → `GOOD`.
- Probe RTT samples are kept in a small ring buffer per plane and
  reported in the per-flow JSON (`probe_rtt_ns_p50`, `_p99`) for
  latency-fault scenarios.

Probes consume bandwidth and CPU. Cadence is tunable because at 1000pps
spray on docker-sonic-vs the receiver scapy loop is already CPU-bound;
`probe_interval_ms=100` works on the lab but should be raised
(e.g. `250` or `500`) on slower hosts.

**2. Receiver loss feedback (passive, in-band).** The receiver already
tracks per-(flow, plane) sequence-number gaps for the reorder histogram.
Every `loss_report_interval_ms` the receiver emits one `LOSS_REPORT`
packet back to each active sender, summarizing `(seen, expected, max_gap)`
per plane over the window. The sender:

- Computes `loss_ratio = (expected - seen) / expected` per plane.
- `loss_ratio > loss_threshold` for two consecutive windows →
  `ASSUMED_BAD`.
- One window of `loss_ratio ≤ loss_threshold / 2` → recovery contributes
  toward `GOOD` (still gated by `probe_recover_threshold`).

This signal only fires when user traffic is flowing on the plane in
question. It catches in-stream loss that probes might miss between
ticks, and it costs zero additional probe bandwidth. It is our
substitute for the fabric's trim-NACK signal.

**3. Fusion.** The two signal sources vote independently into the same
per-(tenant, plane) state. Either path can demote; recovery requires
both to be quiet (no consecutive timeouts AND no recent loss-report
flag). This matches how real NICs combine in-band (trim NACK / NSCC
telemetry) with out-of-band (EV Probe) signals.

### State machine

```
                 probe_recover_threshold successes
                 AND no recent loss-report demote
              ┌──────────────────────────────────┐
              │                                  │
              ▼                                  │
        ┌─────────┐                          ┌──────────────┐
        │  GOOD   │                          │ ASSUMED_BAD  │
        └────┬────┘                          └──────▲───────┘
             │                                      │
             │ probe_fail_threshold timeouts        │
             │ OR loss_ratio > loss_threshold ×2    │
             └──────────────────────────────────────┘

   UNKNOWN is the initial state until the first probe round completes.
   ev_min_active floor: if demoting would push GOOD count below
   mrc_min_active_evs, transition is suppressed and a warning is
   logged in the per-flow report.
```

### Probe wire format

All three new packet types ride in UDP/IPv6 with the **same outer SRH
encap** as user spray for the targeted plane, so they exercise the
exact same forwarding path. Distinct UDP destination ports let the
receiver demux without disturbing the existing spray RX filter.

| Type | UDP dport | Direction | Payload (after UDP header) |
|---|---|---|---|
| `PROBE` | `SPRAY_PROBE_PORT = 9998` | sender → receiver | `!HBQQ`: `req_id` (u16), `plane_id` (u8), `tx_ns` (u64), `pad` (u64) |
| `PROBE_REPLY` | `SPRAY_PROBE_PORT = 9998` | receiver → sender | `!HBQQ`: same layout; `tx_ns` echoed from request, `pad` carries receiver-side service-time in ns |
| `LOSS_REPORT` | `SPRAY_REPORT_PORT = 9997` | receiver → sender | `!HH` header (`window_id` (u16), `num_planes` (u16)) then `num_planes ×` `!BIII`: `plane_id` (u8), `seen` (u32), `expected` (u32), `max_gap` (u32) |

`PROBE_REPLY` is sent into the **inbound** SID-list for the responding
plane (i.e. the sender's plane-N SID-list rewritten to point at the
sender), so the path is symmetric and the measured RTT covers both
legs. `LOSS_REPORT` is sent via kernel routing (no SRH) — the sender
just needs to receive it; we don't care which path it took.

Sequence numbers in `PROBE`/`PROBE_REPLY` are per-(sender, plane) and
independent from the spray `seq` field; that prevents probe traffic
from polluting the reorder histogram.

### Tunables

All MRC knobs live under a single optional top-level `mrc:` block in
the scenario YAML. Absence of the block keeps the baseline wire
unchanged — senders use the non-MRC policy code path and receivers
don't open probe sockets. The presence of the block (even an empty
`mrc: {}`) is what flips the orchestrator into MRC mode: it passes
`--mrc` to every receiver and sets `SRV6_MRC_CONFIG_JSON=<blob>` on
every sender's `docker exec`.

Every subkey is optional; omitted keys fall through to the dataclass
default in `agent.py` / `ev_state.py`. The split below mirrors the
two configs the env-blob populates: `AgentConfig` (wall-clock cadence,
read by `Sender/ReceiverMrcAgent`) and `EVStateConfig` (the state
machine thresholds, read by `EVStateTable`).

```yaml
mrc:
  # AgentConfig — cadence + windowing (all milliseconds, ints, > 0)
  probe_interval_ms: 200        # how often the sender emits a probe per plane
  probe_timeout_ms:  100        # in-flight probe is considered lost after this
  loss_window_ms:    200        # receiver's loss-window length
  max_window_skew_ms: 1000      # sender-side window-ring tolerance

  # EVStateConfig — state-machine thresholds
  probe_fail_threshold:    3    # consecutive probe timeouts → demote
  probe_recover_threshold: 5    # consecutive probe successes → recover
  loss_threshold:        0.05   # per-window loss ratio that flags a plane (float in [0,1])
  loss_demote_consecutive: 2    # consecutive windows over threshold → demote
  min_active_evs:       2    # floor matching OCP's ev_min_active (default = max(1, num_planes//2))
  rtt_ring_size:         128    # per-plane RTT-sample ring length
```

| Knob | Default | Notes |
|---|---|---|
| `probe_interval_ms` | `200` | Lower = faster detection, more CPU. Lab handles 100ms; a laptop running 32 hosts may not. |
| `probe_timeout_ms` | `100` | Must be > worst-case observed RTT; on docker-sonic-vs unburdened RTT is ~1–5ms. |
| `loss_window_ms` | `200` | Receiver-side window size for loss accounting. Don't go below `probe_interval_ms` or signals interleave badly. |
| `max_window_skew_ms` | `1000` | How far back the sender's `SentWindowRing` will accept a report. |
| `probe_fail_threshold` | `3` | Consecutive timeouts before demote. With defaults: `3 × 200ms = 600ms` detection. |
| `probe_recover_threshold` | `5` | Consecutive successes before recovery (asymmetric: demote fast, recover slow). |
| `loss_threshold` | `0.05` | 5% per-plane loss in a window flags it. |
| `loss_demote_consecutive` | `2` | Two flagged windows back-to-back → demote. |
| `min_active_evs` | `max(1, num_planes // 2)` | Floor matching OCP's `ev_min_active`. On 4 planes this is 2. |
| `rtt_ring_size` | `128` | RTT samples retained per plane for diagnostics (no policy effect today). |

Validation lives in `srv6_fabric/mrc/scenario.py` (`_validate_mrc`):
unknown subkeys, negative or zero ints, out-of-range loss ratios, and
booleans-where-ints-expected all raise `ScenarioError` at load time.
The orchestrator serialises only the *set* fields into
`SRV6_MRC_CONFIG_JSON` (via `MrcSpec.to_env_json`), so the env blob
stays small and a missing key in the container always means "use the
code default", never "fell back silently from a typo upstream".

### Sender architecture

The existing `runner.py` sender hot-loop is single-threaded (build →
sendto). MRC adds:

- A **probe timer** (timerfd or simple monotonic-clock check inside the
  hot loop) that emits probe batches every `probe_interval_ms`.
- A **second RX socket** on `SPRAY_PROBE_PORT` to consume
  `PROBE_REPLY`s. Drained by the same scapy receive loop the spray RX
  already uses, with a port-based demux.
- A **third RX socket** on `SPRAY_REPORT_PORT` for `LOSS_REPORT`s.
- A shared `EVStateTable` mutated from RX callbacks and consulted by
  the `health_aware` policy on every `pick()`.

Threading: the EVStateTable is mutated by the RX thread and read by
the TX hot loop. We use a single `threading.Lock` around state
transitions; reads are lock-free (slightly stale state is fine, we're
voting on plane health over hundreds of ms).

### Receiver architecture

- Existing reorder bookkeeping already tracks per-plane gaps; we just
  expose them to a new `LossReporter` that emits `LOSS_REPORT` every
  `loss_report_interval_ms` to each sender it has heard from.
- `PROBE` RX handler echoes a `PROBE_REPLY` immediately, with the
  responder-service-time field set from a monotonic clock delta.
- Both new responsibilities are in the same scapy receive loop as
  spray RX; we add port-based dispatch.

## Orchestration

`run.py` is a thin orchestrator:

1. Parse scenario YAML.
2. Apply `faults:` (calls `lib/netem.py`).
3. For each unique receiver, `docker exec` a `runner.py --role recv` in
   background; pipe its result JSON back over the exec stream.
4. For each flow, `docker exec` `runner.py --role send` with the matching
   policy / rate / duration.
5. Wait for sends to finish + per-recv idle timeout.
6. Reverse `faults:`.
7. Merge per-flow JSON into one report and write to `report.out`.

It does **not** speak to SONiC at all. Everything MRC-level is host-side.

## What this is still not

- **Not a controller.** No PCEP/BGP-LS/SR-policy programming. The MRC
  layer assumes the static SRv6 substrate from the parent dir is up and
  uses it as-is.
- **Not the OCP RDMA transport.** We share the OCP MRC *control-plane
  model* (EVs, EV state, EV Events, EV Probes, ev_min_active); we do
  not share the data-plane (RDMA, WIMM, MPR, retry counters, trim
  NACK). Spray runs over plain UDP/SRv6.
- **No NSCC.** Per-EV rate / cwnd / target-delay control from
  `uet-1.0-nscc` is not implemented; the EVStateTable does on/off
  demotion only.
- **Not at scale.** We're testing correctness and qualitative behavior on
  docker-sonic-vs. Throughput numbers are meaningless; reorder/loss
  patterns and detection-latency-under-fault are not.

## Roadmap and status

| Item | Status |
|---|---|
| Design doc (this file) | done |
| `srv6_fabric/topo.py`, `policy.py`, `reorder.py` | done |
| `srv6_fabric/runner.py` (send/recv core; `spray` is a thin CLI shim) | done |
| `srv6_fabric/netem.py` | done |
| `srv6_fabric/health.py` (legacy ICMPv6 probe; superseded) | done, deprecated |
| `srv6_fabric/mrc/run.py` orchestrator (CLI: `run-scenario`) | done |
| `topologies/4p-8x16/scenarios/baseline.yaml` (smoke test) | done |
| `topologies/4p-8x16/scenarios/yellow-baseline.yaml` | done |
| `topologies/4p-8x16/scenarios/plane-{loss,blackhole,latency}.yaml` (green) | done |
| Yellow fault scenarios: `yellow-plane-{loss,blackhole,latency}.yaml` | TODO |
| `srv6_fabric/mrc/ev_state.py` EVStateTable + state machine | done |
| `srv6_fabric/mrc/probe.py` PROBE / PROBE_REPLY / LOSS_REPORT wire format | done |
| `srv6_fabric/policy.py` `health_aware_mrc` wired to EVStateTable | done |
| Sender agent: probe emit + RX demux + state mutation (`mrc/agent.py` `SenderMrcAgent`) | done |
| Receiver agent: probe-reply emit + LOSS_REPORT emit (`mrc/agent.py` `ReceiverMrcAgent`) | done |
| Scenario YAML schema: `mrc:` block (enabled + tunables) | done |
| `green-mrc-{baseline,plane-loss,plane-latency}.yaml` | done, **lab-validated** |
| Yellow MRC scenarios (`yellow-mrc-*.yaml`) | done, **blocked** (see "Yellow MRC: known-blocked" below) |
| Single-process loopback integration test (sender ↔ receiver ↔ EVStateTable) | done |
| Per-host MRC agent w/ IPC (deduplicate probes across N flows on one host) | future |
| Compare round-robin vs hash5tuple vs health_aware_mrc under fault | TODO |
| NSCC-style per-EV rate control (deferred) | future |
| Tenant encap model as first-class `topo.yaml` property (replace `if tenant == "green"` literals) | future |
| RTT-aware MRC weighting (plane-latency is the regression fixture for when this lands) | future |
| `make image` sanity rail (line-count / SHA check inside the built image) | future |

### First lab validation (commit `1e100f3` + prior MRC commits)

End-to-end run on the docker-sonic-vs 4-plane fabric confirms the three
headline green-tenant scenarios behave as designed:

| Scenario | Fault | Expected | Observed |
|---|---|---|---|
| `green-mrc-baseline` | none | uniform spray, all planes GOOD | per-plane sent within ±2 of mean across 8 flows; 0% loss |
| `green-mrc-plane-loss` | 5% loss on green-host00 plane 2 | demote plane 2, others uniform, total loss ≪ round-robin baseline | plane 2 `ASSUMED_BAD` via the loss path (`consecutive_loss_demote_windows = 3`, weight 0); planes 0/1/3 `GOOD` at weight 1/3; 0.07% total loss vs ~1.25% under round-robin |
| `green-mrc-plane-latency` | 10ms delay on green-host00 plane 3 | uniform spray, all planes GOOD (loss-only MRC ignores latency) | all four planes `GOOD` at weight 0.25; plane 3 RTT p50 ≈ 21ms vs 5-8ms on the other three, visible in the EV-state snapshot but not acted on |

EV-state and loss-fusion snapshots are surfaced on the sender's `--json`
output under a top-level `mrc` key (and passed through to the
ScenarioReport JSON as `flows[].mrc`). Use:

```bash
jq '.flows[].mrc' results/green-mrc-plane-loss.json
```

to inspect per-plane state, consecutive-demote/recover counters,
last-loss-ratio, RTT percentiles, and the LossFusionStats counters
(`paired_with_sent_window` vs `fell_back_to_receiver_expected`).

### Yellow MRC: known-blocked

Yellow MRC scenarios were committed (`2e10f56`) and the addressing
half of the regression has been fixed in `topo.py` —
`host_probe_peer_addr` now returns the inner (plane-independent) host
address for both tenants, and plane selection is purely a
`SO_BINDTODEVICE` concern on the sender. See `docs/architecture.md` §2
for the addressing rule this enforces.

Two further socket-layout issues remain before yellow MRC scenarios can
be lab-validated end to end:

1. **Receiver probe RX**: receiver probe sockets are bound with
   `SO_BINDTODEVICE` on `eth(P+1)`. After yellow's
   `seg6local End.DT6 table 0` action decaps the inner packet,
   the inner DA (anycast `cccc:<NN>::2`, Phase 1a) resolves on `lo`
   for the table-0 lookup. The kernel will not deliver that packet
   to a socket bound to `eth(P+1)`. Plane attribution at the
   receiver does not actually require the socket binding — the
   probe payload carries `plane_id` — so the fix is to bind the
   yellow receiver's probe socket to `::` without
   `SO_BINDTODEVICE`. Green is unaffected.
2. **Receiver → sender reply destination**: the receiver currently
   replies to `peer[0]`, which is the source address from
   `recvfrom`. For yellow that is the per-plane underlay
   (`cccc:<P><A>::2`) which is not a valid workload-layer
   destination. The fix is to derive the reply destination from
   `(tenant_id, src_id)` in the probe payload via
   `inner_addr(tenant, src_id)`, which produces an address with valid
   `seg6 encap` routes. The sender's reply-RX socket layout will also
   need to change for yellow (post-decap replies land on `lo`, not
   `eth(P+1)`).

These changes deliberately scope-creep beyond Phase 0. They are
tracked as Phase 1 NIC-layer work in `docs/architecture.md` §5 and
are best done as part of the `nic/` module extraction so the
tenant-specific socket-layout logic lands in one place. See also
the discussion of where the workload/NIC boundary really sits — in
production, the NIC builds *every* header from a raw payload + a QP
context, which argues for an even thinner emulator-workload analog
than the current code presents.
