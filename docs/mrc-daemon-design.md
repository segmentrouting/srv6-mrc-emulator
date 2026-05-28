# MRC Daemon Design — Stateless Probes (v4)

Status: **implemented and deployed** (2026-05-25)
Current design: v4 stateless probes with sliding-window health model
See also: `docs/stateless-probes-validation.md`, `AGENTS.md` Hard Invariants

## Problem (Historical — Resolved in v4)

**Original v3 issue (SO_REUSEPORT cascade, resolved in PR #1):**
At all-to-all scale (8 hosts → 7 sender processes per host), every sender
process bound `("::", SPRAY_REPORT_PORT=9997)` with `SO_REUSEPORT`. The
Linux kernel hashed inbound replies by 4-tuple across the REUSEPORT
group. Each peer's reply stream got pinned to **one** of the 7 sender
processes — the chance that pinned process was the correct owner was ~14%.

This manifested as:
- `probe_clock.emit ≈ 244 per EV` (probe TX healthy)
- `probe_clock.reply: {}` per EV (zero matched replies)
- `probe_clock.timeout ≈ 242 per EV` (every probe timed out)
- `probe_clock.stale_replies = 343–512` (replies delivered to wrong process)

Wire-level tcpdump showed clean RTT and balanced TX/RX on every plane —
the issue was purely host-side architectural.

**The v4 cure (stateless probes, 2026-05-25):** Removed per-probe
correspondence state entirely. Probes round-trip via a 6-slot uSID list
ending in `End.DT6` on the sender's own leaf, bypassing the peer's
userland. The daemon's dispatcher demuxes returning probes by inner-dst
(per-EV /128). EV health became a sliding-window recv/sent ratio instead
of timeout counters. No match table, no req_id, no stale-reply hazard.

## Architecture (v4 Stateless Probes)

**One MRC daemon process per src_host**, implemented in `srv6_mrc/mrc/daemon.py`.

The `MrcDaemon` class:

- Owns ONE `Srv6RawTransport(is_sender=True)` that binds `("::", SPRAY_REPORT_PORT=9997)` exactly once
- Holds a registry of `SenderMrcAgent` instances, one per `(tenant, dst_id)` flow
- Runs a `_dispatch_loop` thread on the shared recv socket that demuxes inbound packets by magic byte:
  - `0xA5` = returning stateless PROBE → `agent.record_probe_recv(plane, path)`
  - `0xA7` = LOSS_REPORT from peer receiver → `agent._handle_loss_report(payload)`
- Runs a `_snapshot_loop` thread that writes per-flow snapshots to `/dev/shm/srv6-mrc/<host>/<tenant>_<dst_id>.json` every `probe_interval_ms` (default 200ms)

Each `SenderMrcAgent` (owned by the daemon, one per flow):
- Runs TWO daemon threads:
  - `mrc-emit`: sends one PROBE per (plane, path) EV every `probe_interval_ms`, paced across the interval
  - `mrc-window`: rotates sent-window for loss fusion + calls `EVStateTable.tick(tenant)` to advance the sliding window
- No longer has `_reply_rx_loop` or `_sweep_loop` (v3 artifacts; removed in v4)
- Shares the daemon's transport (no per-agent socket binds)

Data sender processes consume EV health via the `mrc_snapshot` policy, which:
- Reads `/dev/shm/srv6-mrc/<host>/<tenant>_<dst>.json` at startup and every `loss_window_ms`
- Builds a weighted CDF from the snapshot's per-EV weights (cached; invalidated on weight changes)
- Picks EVs identically to the live `health_aware_mrc` but from frozen snapshot data

Staleness ≤ 200ms is well under any demote threshold (probe or loss path), so steering decisions remain timely.

## Why a separate daemon (not one all-in-one process)

Putting all N data-spray workers + the MRC probe/reply loops in one
Python process would create GIL starvation: scapy packet construction
at high pps (N flows × rate/flow) would starve the RX thread. Splitting
MRC out of the data path keeps each process single-purpose and bounds
GIL contention.

The v4 fast-path byte templates (probe-emit + formerly probe-reply-build)
reduced scapy overhead to ~10-20µs per operation, but the separation
remains valuable for isolation and debuggability.

## Stateless-Probe Demux (v4)

The v4 design removed per-probe correspondence state. There is no
`probe_clock` module, no match table, no `req_id`, no `tx_ns`, no
per-probe timeout sweep.

**Probe round-trip model:**

1. Sender emits a PROBE with outer DA = 6-slot uSID list:
   `f<spine> e<dst_leaf> e009 f<spine> e<src_leaf> dfff`
2. Probe traverses the fabric to the peer host's leaf, then forward-hops
   back to the sender's leaf (peer host's role is pure IPv6 forwarding
   in the kernel — no userland involvement)
3. Sender's leaf decaps via its static `End.DT6` on `dfff` into table main
4. Inner packet (dst = sender's own per-EV /128, e.g. `cccc::100` for
   yellow host00 eth1 spine0) routes back to the sender via the leaf's
   connected route
5. Returning probe lands on the daemon's `(::, SPRAY_REPORT_PORT)` listener
6. Daemon dispatcher extracts:
   - Magic byte `0xA5` → this is a returning probe
   - Payload `dst_id` byte → which per-flow agent owns this EV
   - Payload `(plane_id, path_id)` → which EV within that flow
7. Daemon calls `agent.record_probe_recv(plane, path)` to bump the EV's
   in-progress recv bucket

**Key invariants (see AGENTS.md for full list):**

- Each per-EV /128 (e.g. `cccc::100`) is configured on EXACTLY ONE host
  (the owner). If another host has it, that host's kernel claims the
  returning probe and silently kills the round trip.
- The inner-src placeholder (e.g. `cccc::ffff`) is NEVER configured
  anywhere. It's unrouted by design.
- Peer-host kernel forwarding must be ON (`net.ipv6.conf.all.forwarding=1`)
  or the peer drops every probe at FIB lookup time.
- The leaf's `dfff End.DT6` entry targets `table main` (not a tenant VRF).

**EV health derivation:**

Per-EV sliding window tracks `(sent, recv)` counts over the last
`probe_window_ticks` ticks (default 5 × 200ms = 1 second). The agent's
`_window_loop` calls `EVStateTable.tick(tenant)` once per
`probe_interval_ms` to rotate buckets. Health decision:

- Demote (GOOD/UNKNOWN → ASSUMED_BAD) when `window_sent >= probe_min_samples`
  AND `window_recv / window_sent < probe_fail_ratio` (default 0.5)
- Recover (ASSUMED_BAD → GOOD) when `window_sent >= probe_min_samples`
  AND `window_recv / window_sent >= probe_recover_ratio` (default 0.9)
  for `probe_recover_ticks` consecutive windows (default 5)

Loss-report signal (receiver → sender feedback) is unchanged and remains
a parallel demotion path independent of probe success.

See `docs/stateless-probes-validation.md` for the hand-validation runbook.

## Process model (v4)

```
src_host (e.g. yellow-host00):
  ├── spray --role mrc-daemon --flows-json '[{"tenant":"yellow","dst_id":1},...]'
  │    └── MrcDaemon owns:
  │         - 1 Srv6RawTransport(is_sender=True), binds (::, 9997) once
  │         - N SenderMrcAgent instances (one per peer dst_id)
  │         - N EVStateTable instances (per-flow, not per-tenant)
  │         - 2 daemon threads: dispatcher + snapshot publisher
  │         - Per-agent: 2 threads each (mrc-emit + mrc-window)
  │         - Snapshot publisher: writes wrapped envelope to
  │           /dev/shm/srv6-mrc/<host>/<tenant>_<dst>.json every 200ms
  │
  ├── spray --role send --dst-id 01 --policy mrc_snapshot:/dev/shm/srv6-mrc/<host>
  ├── spray --role send --dst-id 02 ...
  └── ... (N data sender processes, one per dst)

dst_host (e.g. yellow-host01):
  └── spray --role recv --mrc
       └── ReceiverMrcAgent: ONE agent per host, owns LossWindowTable,
           runs mrc-report-emit thread. No probe RX (probes are kernel-only
           forwarding in v4). Data-RX path calls
           `agent.record_data(flow_key, plane, path, seq)` to feed loss
           windows AND learn per-sender `(last_plane, last_path)` cache
           for loss-report steering.
```

**Snapshot file format (wrapped envelope):**

```json
{
  "src_host": "yellow-host00",
  "src_id": 0,
  "tenant": "yellow",
  "dst_id": 1,
  "captured_ns": 1234567890123456789,
  "ev_state": {
    "config": { "probe_window_ticks": 5, ... },
    "num_planes": 4,
    "num_paths": 4,
    "tenants": {
      "yellow": [
        {
          "plane": 0, "path": 0, "state": "good",
          "window_sent": 5, "window_recv": 5,
          "total_sent": 123, "total_recv": 121,
          "last_probe_ratio": 1.0,
          "consecutive_healthy_windows": 5,
          "consecutive_loss_demote_windows": 0,
          "last_loss_ratio": 0.0,
          "transitions": 0,
          "demotes_suppressed_by_floor": 0,
          "weight": 0.0625
        },
        ...
      ]
    }
  },
  "transport_stats": { "probe_fast_path_misses": 0 },
  "dispatch_stats": { "packets_received": 456, ... },
  ...
}
```

Consumers MUST unwrap `.ev_state` before reading `num_planes` etc.
The `mrc_snapshot` policy and `report.py` both accept this wrapped shape.

## What data senders run

Data senders use `--policy mrc_snapshot:/dev/shm/srv6-mrc/<host>`, which:
- Loads the wrapped snapshot from the daemon's published path at startup
- Re-loads it every `loss_window_ms` (default 200ms)
- Unwraps `.ev_state` and builds a per-EV weighted grid
- Drives EV picks identically to `health_aware_mrc.choose_ev()` but
  using the snapshot's frozen view instead of a live `EVStateTable`
- Caches the CDF keyed by `(id(wgrid), spines)` to avoid per-packet
  allocations (invalidated on wgrid swap)
- Does NOT report per-EV sent counters into a ring (v3 design artifact;
  removed in v4 — the loss-feedback path uses the daemon's own
  `SenderMrcAgent.sent_ring`)

Backward compatibility: the `health_aware_mrc` policy (live in-process
EVStateTable) continues to work for single-flow / non-daemon scenarios.
Tests use it; production all-to-all / ring scenarios use the daemon +
`mrc_snapshot` split.

## Loss-feedback path (sender-side, v4)

The daemon's per-flow `SenderMrcAgent` owns a `SentWindowRing` (indexed
`[plane][path]`) that tracks per-EV sent counts for loss fusion. The
agent's `_window_loop` thread calls `_rotate_window()` every
`loss_window_ms` to snapshot and reset counters.

When a receiver's LOSS_REPORT arrives (magic `0xA7`), the daemon's
dispatcher calls `agent._handle_loss_report(payload)` → `apply_loss_report`
→ `EVStateTable.record_loss_window(tenant, plane, path, seen, expected)`.

Data senders do NOT write per-EV sent counters to `/dev/shm`. The v3
design's "sender writes `.sent.json`, daemon reads it" flow was removed
in v4 because the daemon already owns the sent-window ring through its
per-flow agents. The only cross-process snapshot is the EV health
(`.ev_state`), not the sent counts.

## Lifecycle (v4)

`srv6_mrc/mrc/run.py` (scenario orchestrator):
- Spawns one `spray --role mrc-daemon` per src_host before data senders start
- Passes `--flows-json '[{"tenant":"yellow","dst_id":1}, ...]'` with the
  full per-host flow list
- Waits a short settle interval (default 1s) for daemons to initialize
  and write initial snapshots
- Spawns data senders with `--policy mrc_snapshot:/dev/shm/srv6-mrc/<host>`,
  which find their snapshots already on disk
- On scenario end: stops senders first (drain in-flight), then sends
  SIGTERM **inside the container** via
  `docker exec <host> pkill -TERM -x spray` (not just `Popen.terminate()`,
  which doesn't cross the `docker exec` boundary)
- Daemon writes its final per-flow report to
  `/dev/shm/srv6-mrc/<host>/final_report.json` on exit
- Orchestrator retrieves via `docker exec <host> cat ...` (with retry
  on rc=1 to handle dockerd exec-session teardown race) and merges into
  `ScenarioReport`

**Critical detail:** `Popen.terminate()` on the local `docker exec` client
does NOT propagate SIGTERM into the container. The orchestrator MUST use
`docker exec <host> pkill -TERM -x spray` to kill the in-container daemon
process. On `TimeoutExpired`, escalates to `pkill -KILL`. This prevents
orphan daemons accumulating across runs (AGENTS.md "SO_REUSEPORT cascade"
resolved issue).

## Backward compat / migration notes

- **Tests using `LoopbackUdpTransport`** continue to work. The daemon is
  a new entry point; tests that call `SenderMrcAgent` / `ReceiverMrcAgent`
  directly are unaffected.
- **Single-flow baseline runs** work unchanged through the daemon (N=1
  flow → daemon owns one EVStateTable, one agent).
- **Receiver side completely unchanged** at the wire-format level. Loss
  reports remain v2 (per-EV shape); only the sender-side probe changed
  from v3 stateful to v4 stateless.
- **No v3 → v4 wire-compat.** PROBE v4 is NOT backward-compatible with
  v3. v3 probes are rejected by the v4 decoder (`ProbeDecodeError` on
  version mismatch). This is a lab tool with no in-flight upgrades;
  lockstep deployment is assumed.
- **CLI / env-var names unchanged.** `SRV6_MRC_CONFIG_JSON` payload shape
  is backward-compatible (v4 added `probe_window_ticks`,
  `probe_min_samples`, `probe_fail_ratio`, `probe_recover_ratio`,
  `probe_recover_ticks` but tolerates their absence and falls back to
  defaults). Scenarios that only set `loss_threshold`,
  `loss_demote_consecutive`, etc. keep working.

## Out of scope / future work

- **Cross-host coordination of MRC state** — still purely per-host today.
  Each sender independently decides per-EV health for its own flows.
- **Structured IPC** (UDS dgram, mmap'd shared memory) to replace
  `/dev/shm` JSON snapshots. The file-based approach is simple, works,
  and is trivial to debug (`cat /dev/shm/srv6-mrc/.../yellow_01.json | jq`).
  More sophisticated IPC would reduce snapshot read/write overhead but
  is not a bottleneck at current scales.
- **Per-host MRC agent** (one agent shared across all flows on a src_host,
  instead of one per flow). Would deduplicate probe work in N-to-M
  all-to-all scenarios (each src_host currently runs N emit threads, one
  per peer dst). Deferred because the current per-flow model is easier
  to reason about and test, and probe pacing + fast-path templates already
  reduced emit cost to negligible.
- **RTT-aware EV weighting.** The `rtt_ring` is collected per EV but not
  yet consulted by the weight builder. `green-mrc-plane-latency` scenario
  observes latency deltas in diagnostics but correctly does NOT demote
  (by design — latency-only faults shouldn't cause traffic shift until
  RTT-aware weighting lands).
- **Adaptive probe interval** (back off when all EVs are stable, increase
  frequency under churn). Current design uses fixed 200ms. OCP MRC spec
  mentions adaptive intervals; not implemented here.

## Key implementation files (v4)

- **`srv6_mrc/mrc/daemon.py`** (676 lines): `MrcDaemon` class, dispatcher,
  snapshot publisher, final-report writer. Entry point:
  `spray --role mrc-daemon`.
- **`srv6_mrc/mrc/agent.py`** (662 lines): `SenderMrcAgent` (emit + window
  threads) and `ReceiverMrcAgent` (report-emit thread). Owned by the
  daemon (sender) or by `spray --role recv --mrc` (receiver).
- **`srv6_mrc/mrc/transport.py`** (523 lines): `MrcTransport` ABC,
  `Srv6RawTransport` (lab, with probe-template fast path), and
  `LoopbackUdpTransport` (tests). No `send_probe_reply` method (removed
  in v4).
- **`srv6_mrc/mrc/ev_state.py`** (611 lines): `EVStateTable` (per-EV
  sliding-window health model), `EVStateConfig` (tunables). No
  `consecutive_probe_timeouts` / `consecutive_probe_successes` counters
  (replaced by `buckets` deque + window ratio in v4).
- **`srv6_mrc/mrc/probe.py`** (315 lines): PROBE v4 codec (10 bytes:
  `magic=0xA5, version=4, plane_id, path_id, tenant_id, src_id, dst_id,
  _reserved`). LOSS_REPORT v2 codec (unchanged). No PROBE_REPLY type.
- **`srv6_mrc/mrc/loss_compute.py`** / **`loss_window.py`**: Loss-fusion
  logic (unchanged from v3).
- **`srv6_mrc/mrc/run.py`** (scenario orchestrator): daemon lifecycle
  (spawn, pkill teardown), final-report retrieval from `/dev/shm`.

**Removed modules (v3 → v4):**
- `probe_clock.py` — per-probe match table, timeout sweep, `req_id`,
  `tx_ns`. Entire module deleted in v4.

**Probe fast-path templates (`Srv6RawTransport._prewarm_probe_templates`):**
Pre-builds byte templates for every `(plane, path, dst_leaf)` triple at
`__init__` (4×4×7 = 112 on 4p-4x8). Emit hot path: splice 10-byte payload
at `PAYLOAD_OFFSET`, fix UDP6 checksum via `udp6_checksum_inplace`, send.
Cost per probe: ~10-20µs (vs ~2-3ms scapy slow path). Regression test:
`tests/test_mrc_transport.py::FastPathByteIdentityTests` pins byte-identity
against scapy reference.

## Acceptance / validation (v4)

**Lab validation status (4p-4x8, 2026-05-26):**

- `yellow-all-to-all` (56 flows × 30s): **0% loss** on all flows, 16/16
  EVs GOOD on every flow, per-plane balance tight (within 5% across
  planes). Consecutive runs show no orphan daemons accumulating
  (`pgrep -x spray` returns 0 between runs).
- `green-all-to-all` (56 flows × 30s): **0% loss**, 16/16 EVs on 55/56
  flows; one transient demote on `green-host05 → host07` (15/16 EVs,
  p1-spine00 unused for ~2s then recovered).
- `yellow-mrc-ev-spray` (8 senders → 8 different receivers): **0% loss**,
  16/16 EVs, clean window recv/sent ratios (1.0 ± 0.01).
- `yellow-baseline` (round-robin, no MRC): **0% loss**, plane-balanced.

**Metrics confirming the v4 design goals:**

- `probe_fast_path_misses = 0` (template cache hit rate 100%)
- `window_recv / window_sent ≈ 0.98–1.0` on healthy EVs
- `consecutive_healthy_windows >= 5` on all GOOD EVs
- `demotes_suppressed_by_floor = 0` (no spurious floor hits)
- `dispatch_rx_gap_buckets["lt_1ms"] >> ["ge_1s"]` (dispatcher not starved)
- `kernel_rx_dwell_buckets["lt_1ms"] ≈ 95%` (no recvbuf queueing)

**Regression checks (must stay clean on every commit):**

- All 564 unit tests pass (0 skips, 0 expected failures)
- Baseline scenarios (`*-baseline.yaml`) show 0% loss, 16/16 EVs
- Fault-injection scenarios (`*-plane-loss.yaml`) demote affected EVs
  within `probe_window_ticks * probe_interval_ms` (1 second) and recover
  after `probe_recover_ticks` consecutive healthy windows (also 1 second)
- Snapshots are valid JSON and contain the wrapped envelope with
  `.ev_state` key
- `cat /dev/shm/srv6-mrc/.../yellow_01.json | jq '.ev_state.tenants.yellow | length'`
  returns 16 (4 planes × 4 spines)

See `AGENTS.md` "RESOLVED: SO_REUSEPORT cascade" and "RESOLVED: Phantom
EV demotions" for the lab-validated bug fixes that v4 depends on.
