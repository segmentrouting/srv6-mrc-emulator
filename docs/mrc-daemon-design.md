# MRC Daemon Refactor — Design Note

Status: draft, pre-implementation
Branch: `mrc-daemon-refactor`

## Problem

At all-to-all scale (8 hosts → 7 sender processes per host), every sender
process binds `("::", SPRAY_REPORT_PORT=9997)` with `SO_REUSEPORT`. The
Linux kernel hashes inbound replies by 4-tuple across the REUSEPORT
group. Each peer's reply stream therefore gets pinned to **one** of the 7
sender processes — and the chance that pinned process is the one
actually owning the host00→peer flow is 1/7 ≈ 14 %.

JSON evidence from latest yellow all-to-all run (host00→host01):
- `probe_clock.emit ≈ 244 per EV` (probe TX is healthy)
- `probe_clock.reply: {}` per EV (zero matched replies)
- `probe_clock.timeout ≈ 242 per EV` (every probe times out)
- `probe_clock.stale_replies = 343–512` (some replies arrive but req_id
  doesn't match any outstanding entry — they belong to a different
  process's flow)
- Wire-level tcpdump shows clean RTT and balanced TX/RX on every plane

User reports green all-to-all reproduces identically — confirms this
is host-side architectural (process model + REUSEPORT splay), not a
yellow-decap quirk.

## Decision

**One MRC daemon process per src_host.**

After reading `agent.py:SenderMrcAgent` end-to-end, the refactor is
narrower than it first looked. Per-flow `SenderMrcAgent` is already
correct as an object — its only architectural problem is that each
instance binds its own reply socket. We can keep `SenderMrcAgent`
mostly intact and introduce a new `MrcDaemon` class that:

- Owns one `Srv6RawTransport(is_sender=True)` (binds `("::", 9997)`
  exactly once).
- Holds a registry of `SenderMrcAgent` instances keyed by
  `peer_inner_addr_str` (= the peer's `inner_addr(tenant, dst_id)`).
- Runs a single `_dispatch_loop` thread on the shared reply socket
  that decodes replies, looks up the target agent by `peer_addr`,
  and calls `agent._handle_probe_reply()` / `agent._handle_loss_report()`
  directly (these methods are already pure-logic — no socket I/O).
- Publishes per-flow EV state snapshots to `/dev/shm/srv6-mrc/<host>/<dst_id>.json`
  every `probe_interval_ms`.

Required changes to `SenderMrcAgent`:
- Remove `_reply_rx_loop` (dispatcher takes over).
- Remove default-construction of `Srv6RawTransport` — the daemon
  injects a shared transport. Tests are unaffected because they
  already pass `transport=LoopbackUdpTransport(...)` explicitly.
- Make `_handle_probe_reply` and `_handle_loss_report` public-ish
  (single underscore is fine; the daemon dispatcher is in the same
  package).

Data sender processes (one per flow, unchanged process count) stop
running their own `SenderMrcAgent`. They consume EV-health state by
mapping a snapshot file under `/dev/shm/srv6-mrc/<host>/<dst_id>.json`
that the daemon refreshes once per `probe_interval_ms` (200 ms).
Senders cache the snapshot in-memory and re-read it on a periodic
tick aligned to `loss_window_ms` (200 ms). Staleness ≤ 200 ms is well
under any demote threshold (`probe_fail_threshold * probe_interval`
= 600 ms minimum), so steering decisions remain timely.

## Why not a single all-in-one process

Putting all 56 data-spray workers + the MRC reply RX loop in one
Python process would re-create the GIL starvation we just diagnosed,
in a different costume: scapy packet construction at ~56 k pps would
starve the reply RX thread. Splitting MRC out of the data path keeps
each process single-purpose and bounds GIL contention.

## Reply demux

The probe wire format (`mrc/probe.py`) echoes `tenant_id`, `src_id`
(= the sending host), and `reply_port` in `ProbeReply`. It does **not**
encode `dst_id` — but the daemon doesn't need that field on the wire
because:

- `recvfrom()` on the listener returns `(payload, peer_addr)`.
- `peer_addr[0]` is the peer's inner anycast for the tenant — i.e.
  `inner_addr(tenant, dst_id_from_our_view)`.
- The daemon already knows the tenant→dst_id→inner_addr map (built at
  startup from the flow list). It reverses the lookup once per
  reply: `peer_addr[0]` → `dst_id` → `(EVStateTable, ProbeClock)` for
  that flow.

This works identically for green and yellow because both colors deliver
the same inner UDP packet to the kernel after decap; only the upstream
encap path differs.

## Process model

```
src_host (e.g. green-host00):
  ├── spray --role mrc-daemon --flows-json '[{tenant,dst_id},...]'
  │    └── owns: 1 Srv6RawTransport (binds 9997)
  │              N EVStateTables, N ProbeClocks
  │              probe TX threads, sweep thread, reply RX dispatcher
  │              snapshot publisher: writes /dev/shm/srv6-mrc/<host>/<dst>.json every 200ms
  │
  ├── spray --role send --dst-id 01 --policy ev_spray --snapshot-from /dev/shm/srv6-mrc/<host>
  ├── spray --role send --dst-id 02 ...
  └── ... (N data sender processes, one per dst, unchanged in count)

dst_host (e.g. green-host01):
  └── spray --role recv --mrc
       └── unchanged from today; one receiver per host with own
           ReceiverMrcAgent + probe RX listener (already correct).
```

## What `--policy` data senders run

Today data senders use `health_aware_mrc` which auto-starts a
`SenderMrcAgent`. After the refactor, data senders use a new policy
`mrc_snapshot:<path>` which:
- Loads the snapshot from path at startup.
- Re-loads it every `loss_window_ms`.
- Drives EV picks identically to `health_aware_mrc.choose_ev` but
  using the snapshot's frozen EV-health view instead of a live
  `EVStateTable`.
- Reports per-EV sent counters into a snapshot-publish ring the
  daemon also reads (see "loss-feedback path" below).

## Loss-feedback path (sender-side)

Today `SenderMrcAgent.record_sent(plane, path)` increments per-EV
counters that feed into `SentWindowRing` for sender-driven loss
attribution. After the refactor:
- Data senders write per-EV sent counters to a second `/dev/shm`
  snapshot (`/dev/shm/srv6-mrc/<host>/<dst_id>.sent.json`) once per
  loss window.
- The daemon reads those snapshots and feeds them into the per-flow
  `LossFusion` exactly as the in-process callback does today.

This keeps the daemon as the sole owner of `EVStateTable` and avoids
multi-writer races on the EV state.

## Lifecycle

`mrc/run.py`:
- Spawns one daemon per src_host before any data senders start.
- Waits a short settle interval for the daemon to initialize and write
  initial snapshots.
- Spawns data senders, which find their snapshots already on disk.
- On scenario end: stops senders first (drain in flight), then signals
  daemon to flush stats + exit.
- Daemon writes its final per-flow `mrc.ev_state` + `mrc.probe_clock`
  diagnostics to stdout as JSON, picked up by `run.py` and merged
  into the `ScenarioReport`.

## Backward compat

- Tests that use `LoopbackUdpTransport` and call `SenderMrcAgent`
  directly continue to work. The daemon is a new entry point built
  on top of the same `agent.py` primitives; it does not replace them.
- Single-flow runs (baseline, allreduce-ring under 4 hosts) work
  unchanged through the daemon: N=1 flow → daemon owns one
  EVStateTable, snapshot is trivial.
- Receiver side completely unchanged; receiver bug never existed.

## Out of scope (later branches)

- Cross-host coordination of MRC state (still purely per-host today).
- Replacing snapshot files with a structured IPC (UDS dgram, mmap'd
  shared memory) — `/dev/shm` JSON is enough for V1 and is trivial
  to replace later.
- Yellow vs green parity tests for the daemon — daemon is color-agnostic
  by construction (Srv6RawTransport already is).

## Acceptance

- All-to-all scenarios show `probe_clock.reply` populated for every EV
  (matched count comparable to `probe_clock.emit`).
- `stale_replies` drops to ~0 (no cross-process misdelivery anymore).
- EV state stays `up` for healthy planes; demotes only happen on real
  fabric loss.
- Existing baseline + allreduce-ring scenarios pass unchanged.
