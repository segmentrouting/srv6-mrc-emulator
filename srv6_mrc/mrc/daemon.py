"""MRC daemon — single per-host process owning shared reply RX + per-flow agents.

Background
----------
At all-to-all scale (8 hosts, 56 sender flows total, 7 sender processes
per host) the previous design — one `spray --role send` process per
flow, each binding `("::", SPRAY_REPORT_PORT=9997)` with `SO_REUSEPORT`
for its own reply listener — produced catastrophic reply misdelivery.
The Linux kernel hashes inbound UDP packets across the REUSEPORT group
by 4-tuple. With ~7 sender processes per host, a peer's reply stream
gets pinned to one specific process; that process is the *correct*
owner of the host→peer flow only ~1/7 of the time.

The cure is a single MRC daemon process per src_host that owns the one
shared reply listener and dispatches inbound replies to the right
per-flow `SenderMrcAgent` by inspecting `recvfrom()`'s peer source
address.

Process model
-------------
For src_host green-host00 with N flows (host00→host01..hostN):

    spray --role mrc-daemon --flows-json '[{"tenant":"green","dst_id":1},...]'
        └─ MrcDaemon owns:
             - 1 Srv6RawTransport(is_sender=True), binds 9997 once
             - N EVStateTables, N SenderMrcAgent instances (no own RX loop)
             - 1 dispatcher thread on the shared reply socket
             - 1 snapshot publisher thread
             - per-flow snapshots written to /dev/shm/srv6-mrc/<host>/<dst_id>.json

Data sender processes (the existing one-per-flow `spray --role send`
processes) read those snapshots via the `mrc_snapshot:<path>` policy
(added in step b of the refactor) instead of running their own MRC
agent. This keeps each process single-purpose: daemon = MRC control
plane, senders = data plane.

Reply demux
-----------
The probe wire format (`mrc/probe.py`) echoes the originating sender's
`tenant_id` + `src_id` in `ProbeReply`, but does NOT carry `dst_id`
(the replying host's id from the sender's perspective). That field is
unnecessary because `recvfrom()` returns `(payload, peer_addr)` where
`peer_addr[0]` is the IPv6 source of the inbound UDP packet — i.e. the
peer's tenant-inner anycast `inner_addr(tenant, dst_id)`. The daemon
maintains a `peer_inner_addr -> SenderMrcAgent` map at startup (built
from the flow list) and uses it as the dispatch key.

This works identically for green and yellow because both colors deliver
the same inner UDP packet to the kernel after decap; only the encap
path on the wire differs.

Threading model
---------------
Daemon threads (3 total):
  - dispatcher: reads `transport.recv_reply_socket()`, decodes magic
    byte, dispatches to the right agent's `_handle_probe_reply` or
    `_handle_loss_report`. Single thread by design — funnels ALL
    inbound replies through one drain loop, eliminating the GIL race
    with multiple per-flow RX loops.
  - snapshot publisher: every `probe_interval_ms`, writes per-flow
    `EVStateTable.snapshot()` JSON to `/dev/shm/srv6-mrc/<host>/<dst>.json`.
  - signal/sigterm reaper: blocks on `_stop` Event; exposed via
    `MrcDaemon.stop()`.

Each per-flow `SenderMrcAgent` still spawns its own emit / sweep /
window-rotate threads (1 + 1 + 1 = 3 per flow). With N=7 flows on a
host that's 21 thread + 3 daemon threads = 24 threads — comfortable.

Lifecycle
---------
  d = MrcDaemon(src_host, flows, ...)
  d.start()           # spawn dispatcher + publisher + per-flow agents
  d.wait_for_stop()   # block until SIGTERM or stop()
  d.stop()            # joins, closes transport, prints final JSON
"""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import socket
import struct
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

# SIOCINQ (FIONREAD) ioctl probes the kernel UDP recv-buf depth at
# recv()-time so we can localize whether replies are stacking up in
# the kernel between our recvfrom() calls (sender-RX starvation) or
# arriving cleanly (then the seconds are upstream — on the wire, in
# the receiver, or in the kernel's tx path back to us). Mirror the
# guard pattern from `srv6_mrc/mrc/agent.py:81-93`: macOS dev laptops
# without `termios.FIONREAD` degrade to a no-op so unit tests run on
# any OS. Lab containers are linux/alpine so this is always available.
try:
    import fcntl  # type: ignore[import-untyped]
    import termios  # type: ignore[import-untyped]
    _SIOCINQ = getattr(termios, "FIONREAD", None)
    _HAVE_SIOCINQ = _SIOCINQ is not None
except ImportError:
    _HAVE_SIOCINQ = False
    _SIOCINQ = None

from ..topo import (
    NUM_PLANES,
    NUM_SPINES,
    inner_addr,
    tenant_id as topo_tenant_id,
)
from .agent import AgentConfig, SenderMrcAgent
from .ev_state import EVStateConfig, EVStateTable
from .probe import ProbeDecodeError, decode_loss_report, decode_probe_reply
from .transport import (
    DEFAULT_RECV_BUFSIZE,
    MrcTransport,
    Srv6RawTransport,
)


log = logging.getLogger(__name__)


def _canon_ipv6(addr: str) -> str:
    """RFC 5952 canonical form, matching `socket.recvfrom`'s peer_addr[0].

    Used to key/lookup `MrcDaemon._demux`. Both `inner_addr()`-produced
    strings (which zero-pad hextets, e.g. "2001:db8:bbbb:07::2") and
    kernel-canonical strings (zero-stripped, e.g. "2001:db8:bbbb:7::2")
    must map to the same key. A scope-id suffix (`%ifname`) on
    link-local addresses is stripped before parsing — the daemon only
    ever talks to global addresses, but defensive normalization avoids
    a future linkylocal-related foot-gun.

    Returns the input string unchanged on parse failure so a malformed
    `peer_addr` from recvfrom() (shouldn't happen) doesn't crash the
    dispatcher's hot loop; the lookup just misses and the packet is
    counted as `unknown peer`.
    """
    s = addr.split("%", 1)[0]
    try:
        return ipaddress.IPv6Address(s).compressed
    except (ValueError, ipaddress.AddressValueError):
        return addr


DEFAULT_SNAPSHOT_DIR = "/dev/shm/srv6-mrc"

# Filename used by `write_final_report_file()` for the on-exit dump.
# Reading this back from disk is the orchestrator's authoritative path
# for retrieving the daemon's final report — `docker exec` stdout is
# unreliable for payloads of this size (dockerd's stdout multiplex
# stream drops trailing frames if the container exits before drain;
# we've observed 7-of-8 daemons producing empty stdout and 1 producing
# stdout truncated at ~16 KiB on a `green-all-to-all` run). The file
# is the source of truth; stdout is kept as a fallback for humans who
# run `spray --role mrc-daemon` directly.
FINAL_REPORT_FILENAME = "final_report.json"


@dataclass(frozen=True)
class DaemonFlow:
    """One flow owned by the daemon: (tenant, dst_id).

    `src_id` is the daemon's own host id and is shared by all flows.
    Future versions may add per-flow probe-rate overrides etc.; for
    now the AgentConfig is shared.
    """
    tenant: str
    dst_id: int


class MrcDaemon:
    """Per-src_host MRC daemon: shared reply RX + per-flow agents.

    Constructs ONE Srv6RawTransport(is_sender=True) and shares it
    across all per-flow SenderMrcAgent instances. The agents'
    `_reply_rx_loop` is suppressed via `start(own_reply_rx=False)` and
    the daemon's dispatcher thread takes over reading from the shared
    reply socket, dispatching by peer source address.

    Snapshot publisher writes per-flow `EVStateTable.snapshot()` JSON
    to `<snapshot_dir>/<src_host>/<dst_id>.json` every
    `probe_interval_ms`. Data sender processes read those files via
    the (forthcoming) `mrc_snapshot:<path>` policy.

    Tests should pass `transport=LoopbackUdpTransport(...)` so the
    daemon doesn't try to open AF_INET6/SOCK_RAW (which requires
    CAP_NET_RAW). The loopback transport's recv socket is bound to
    `("::1", peer_rx_port)` and `recvfrom()` returns peer addresses
    as `("::1", ...)` — so the demux's address-based lookup works
    only if the test arranges for distinct peer addresses. For
    single-flow tests the demux is trivial (one entry).
    """

    def __init__(
        self,
        *,
        src_host: str,
        src_id: int,
        flows: List[DaemonFlow],
        agent_cfg: AgentConfig,
        ev_cfg: Optional[EVStateConfig] = None,
        transport: Optional[MrcTransport] = None,
        snapshot_dir: str = DEFAULT_SNAPSHOT_DIR,
        clock_ns: Callable[[], int] = time.monotonic_ns,
    ) -> None:
        if not flows:
            raise ValueError("MrcDaemon requires at least one flow")
        self.src_host = src_host
        self.src_id = src_id
        self.flows = list(flows)
        self.cfg = agent_cfg
        self.ev_cfg = ev_cfg
        self.snapshot_dir = Path(snapshot_dir) / src_host
        self.clock_ns = clock_ns

        self._stop = threading.Event()
        self._threads: List[threading.Thread] = []

        # Shared transport — exactly one Srv6RawTransport per daemon.
        # Tests inject a LoopbackUdpTransport here.
        if transport is None:
            transport = Srv6RawTransport(
                tenant=flows[0].tenant,  # tenant is per-flow but the
                # raw send sockets are tenant-agnostic; we pass
                # flows[0].tenant only because Srv6RawTransport's
                # constructor wants one for inner-anycast caching.
                # The transport's per-plane raw sockets are NOT
                # tenant-bound. Multi-tenant daemons would need a
                # transport refactor; out of scope for V1.
                my_id=src_id,
                is_sender=True,
            )
        self.transport = transport

        # Per-flow state. Construction order:
        #   one EVStateTable per (tenant, dst_id) flow, NOT per tenant.
        #
        # Why per-flow: probe outcomes against the same EV from
        # different destinations would otherwise race destructively
        # in a shared counter. With 7 flows on a host all writing to
        # one (tenant, plane, path) record, every successful probe
        # from flow A would be erased by the next millisecond's
        # timeout from flow B's sweep, leaving consecutive_probe_
        # successes pegged at 0 even when probes were succeeding
        # (rtt_ring populated but the state-machine counter
        # trampled). This matches the MRC paper's per-endpoint-pair
        # EV model: host00 has 16 EVs to host01, a separate 16 to
        # host02, etc. Each pair is independently monitored.
        #
        # Memory: 16 EV records x N flows is small (N=7 on 4p-4x8
        # all-to-all = 112 records per host per tenant). The
        # snapshot already publishes per-(tenant, dst_id), so the
        # on-disk file layout is unchanged; only the in-memory
        # dict key changes from `tenant` to `(tenant, dst_id)`.
        self.tables: Dict[Tuple[str, int], EVStateTable] = {
            (flow.tenant, flow.dst_id): EVStateTable(
                num_planes=NUM_PLANES,
                num_paths=NUM_SPINES,
                tenants=(flow.tenant,),
                cfg=ev_cfg,
            )
            for flow in self.flows
        }

        # Per-flow agent. `transport=self.transport` shares the daemon's
        # transport; the agent does NOT construct its own.
        self.agents: Dict[Tuple[str, int], SenderMrcAgent] = {}
        # Demux key: peer's inner anycast string -> agent. Built once
        # at construction so the dispatcher's hot path is just a dict
        # lookup.
        #
        # We MUST canonicalize the key on both insert and lookup. Two
        # different string forms of the same IPv6 address exist:
        #   - `inner_addr(green, 7)` builds "2001:db8:bbbb:07::2"
        #     (zero-padded hextet from f"{host_id:02x}").
        #   - `socket.recvfrom()`'s peer_addr[0] returns the kernel's
        #     RFC 5952 canonical form "2001:db8:bbbb:7::2" (leading
        #     zeros within each hextet are suppressed).
        # Pre-fix, the dict was keyed by the unpadded inner_addr() form
        # while the dispatcher looked up with the canonical form, so
        # EVERY probe reply was silently dropped at the "unknown peer"
        # branch — the bug was invisible because dispatch fell through
        # to `continue` rather than logging at INFO. Normalizing through
        # ipaddress.IPv6Address(...).compressed (the same form recvfrom
        # returns) makes both sides agree. See AGENTS.md gotchas:
        # "IPv6 string canonicalization" / `_canon_addr` in report.py.
        self._demux: Dict[str, SenderMrcAgent] = {}

        # Dispatch instrumentation. Counters expose where replies go
        # to die between recv() on the shared socket and
        # record_probe_result() on the per-flow EVStateTable. After
        # `11df16c` (per-flow tables) the lab saw ~12,840 replies
        # arrive at the kernel socket while only ~200 made it to
        # record_probe_result — these counters localize which
        # transition in the dispatch chain is dropping the rest.
        #
        # Updated WITHOUT a lock because Python's int += 1 is atomic
        # under the GIL for the read-modify-write pattern we use
        # (single _dispatch_loop thread writes; readers are snapshot
        # publishers that don't care about a torn read of one tick).
        self._dispatch_counters: Dict[str, int] = {
            "replies_received": 0,        # every recvfrom() w/ payload
            "replies_no_peer": 0,         # peer addr not in _demux
            "replies_unknown_magic": 0,   # not 0xA6 / 0xA7
            "replies_dispatched_probe": 0,   # routed to _handle_probe_reply
            "replies_dispatched_loss": 0,    # routed to _handle_loss_report
        }

        # Sender reply-RX instrumentation. Symmetric to the receiver's
        # probe_rx_gap_buckets / probe_rx_backlog_buckets (see
        # agent.py:832-845): localizes whether the multi-second
        # reply-latency tail observed in cycle 7 lives in the kernel's
        # reply path TO the sender (replies sitting in the UDP recvbuf
        # while we're GIL-blocked by emit/sweep/window threads) or
        # upstream of recvfrom() (wire / receiver / kernel TX).
        #
        # dispatch_rx_gap_buckets: monotonic_ns gap between successive
        # recvfrom() returns on the shared reply socket. Heavy lt_100ms+
        # tail means we ARE the queue — dispatch thread is starved.
        # Mostly lt_1ms means recvfrom is idle and the seconds vanish
        # before they reach us.
        #
        # dispatch_rx_backlog_buckets: SIOCINQ BYTES queued on the
        # reply socket AFTER the recvfrom that just returned. Bucketed
        # on RAW BYTES (not record count) because the shared reply
        # socket carries BOTH probe replies (0xA6, ~28B) AND loss
        # reports (0xA7, variable / larger) — dividing by any fixed
        # record size would mis-bin loss reports against probe
        # replies. Buckets: inq_0 (caught up), le_512 (~<18 probe
        # replies queued), le_4k (a probe round backlog), le_32k
        # (heavy), gt_32k (very heavy / near rcvbuf limit).
        #
        # Single-writer (dispatcher thread) — int += 1 is GIL-atomic
        # for the read-modify-write pattern, same justification as
        # `_dispatch_counters` above.
        self._dispatch_rx_gap_buckets: Dict[str, int] = {
            "lt_1ms": 0,
            "lt_10ms": 0,
            "lt_100ms": 0,
            "lt_1s": 0,
            "ge_1s": 0,
        }
        self._dispatch_rx_backlog_buckets: Dict[str, int] = {
            "inq_0": 0,
            "le_512": 0,
            "le_4k": 0,
            "le_32k": 0,
            "gt_32k": 0,
        }

        for flow in self.flows:
            agent = SenderMrcAgent(
                tenant=flow.tenant,
                src_id=src_id,
                dst_id=flow.dst_id,
                table=self.tables[(flow.tenant, flow.dst_id)],
                config=agent_cfg,
                transport=self.transport,  # shared!
                clock_ns=clock_ns,
            )
            self.agents[(flow.tenant, flow.dst_id)] = agent
            peer_inner = inner_addr(flow.tenant, flow.dst_id)
            self._demux[_canon_ipv6(peer_inner)] = agent

    # --- public API ----------------------------------------------------

    def start(self) -> None:
        """Start dispatcher, snapshot publisher, and all per-flow agents.

        Order matters:
          1. Per-flow agents start FIRST (without their own RX loops)
             so probe TX threads are running when the dispatcher
             begins delivering replies.
          2. Dispatcher starts on the shared reply socket.
          3. Snapshot publisher starts writing initial snapshots.

        Reverse on stop().
        """
        self._stop.clear()
        self._ensure_snapshot_dir()

        for agent in self.agents.values():
            agent.start(own_reply_rx=False)

        self._spawn(self._dispatch_loop, name="mrc-daemon-dispatch")
        self._spawn(self._snapshot_loop, name="mrc-daemon-snapshot")

    def stop(self, *, timeout_s: float = 2.0) -> None:
        """Signal threads to exit, close transport, join.

        Per-flow agents are stopped with `close_transport=False`
        because the daemon owns the shared transport. The daemon
        closes it once after all agents have stopped.
        """
        self._stop.set()

        # Stop per-flow agents first so they stop emitting probes
        # before we close the transport (otherwise their emit loops
        # race against `transport.close()` and produce noisy OSError
        # tracebacks).
        for agent in self.agents.values():
            try:
                agent.stop(timeout_s=timeout_s, close_transport=False)
            except Exception as e:
                log.debug("mrc.daemon: agent stop: %s", e)

        # Close the transport, which unblocks the dispatcher's
        # recvfrom. The dispatcher exits, the publisher exits on
        # its next wakeup.
        try:
            self.transport.close()
        except Exception:
            pass

        deadline = time.monotonic() + timeout_s
        for t in self._threads:
            remaining = deadline - time.monotonic()
            if remaining > 0:
                t.join(timeout=remaining)

    def wait_for_stop(self, *, poll_s: float = 0.5) -> None:
        """Block until stop() is called (e.g. by signal handler).

        Used by the `spray --role mrc-daemon` CLI which sets up a
        SIGTERM handler that calls stop(). Returns when _stop is set.
        """
        while not self._stop.is_set():
            self._stop.wait(poll_s)

    def final_report(self) -> Dict[str, Any]:
        """Return a JSON-serializable snapshot of all flows' state.

        Called after stop() to flush the final per-flow `mrc.ev_state`
        + `mrc.probe_clock` + `mrc.loss_fusion` diagnostics out of
        the daemon's address space and into the orchestrator's report
        aggregator (via stdout).

        The shape mirrors what `cli/spray.py` produces under the
        `mrc` key today, except keyed per-flow under `flows`:

            {
              "src_host": "green-host00",
              "src_id": 0,
              "flows": {
                "green/01": {"ev_state": ..., "probe_clock": ..., "loss_fusion": ...},
                "green/02": {...},
              }
            }
        """
        from ..cli.spray import (  # lazy import to avoid CLI/runtime cycle
            _loss_fusion_stats_to_dict,
            _probe_clock_stats_to_jsonable,
        )
        flows_out: Dict[str, Any] = {}
        for (tenant, dst_id), agent in self.agents.items():
            try:
                flows_out[f"{tenant}/{dst_id:02d}"] = {
                    "ev_state": agent.table.snapshot(),
                    "probe_clock":
                        _probe_clock_stats_to_jsonable(agent.probe_clock.stats()),
                    "loss_fusion":
                        _loss_fusion_stats_to_dict(agent.stats),
                }
            except Exception as e:
                flows_out[f"{tenant}/{dst_id:02d}"] = {
                    "error": f"snapshot failed: {e}",
                }
        return {
            "src_host": self.src_host,
            "src_id": self.src_id,
            "flows": flows_out,
        }

    # --- thread bodies -------------------------------------------------

    def _dispatch_loop(self) -> None:
        """Single drain loop on the shared reply socket.

        Reads one packet, decodes the magic byte, looks up the right
        per-flow agent by `peer_addr[0]`, and dispatches. Drops packets
        from unknown peers (logged at debug level — could be probes
        crossing in flight from peers we don't have a flow to).
        """
        try:
            sock = self.transport.recv_reply_socket()
        except RuntimeError as e:
            log.error("mrc.daemon.dispatch: %s", e)
            return
        # Instrumentation state. last_recv_ns measures inter-recv gap;
        # inq_buf is a reusable 4-byte scratch for the SIOCINQ ioctl
        # (saves one allocation per packet on the hot path). On macOS
        # where _HAVE_SIOCINQ is False, backlog buckets stay at zero
        # everywhere and we do not call fcntl.ioctl at all.
        last_recv_ns: Optional[int] = None
        inq_buf = bytearray(4) if _HAVE_SIOCINQ else None
        while not self._stop.is_set():
            try:
                payload, peer = sock.recvfrom(DEFAULT_RECV_BUFSIZE)
            except socket.timeout:
                continue
            except OSError:
                return  # socket closed during stop()
            if not payload:
                continue
            now_ns = time.monotonic_ns()
            # Gap from previous recv. Skip on first iteration — a
            # cold-start "gap" from process boot isn't a meaningful
            # bucket value.
            if last_recv_ns is not None:
                gap_ns = now_ns - last_recv_ns
                if gap_ns < 1_000_000:
                    self._dispatch_rx_gap_buckets["lt_1ms"] += 1
                elif gap_ns < 10_000_000:
                    self._dispatch_rx_gap_buckets["lt_10ms"] += 1
                elif gap_ns < 100_000_000:
                    self._dispatch_rx_gap_buckets["lt_100ms"] += 1
                elif gap_ns < 1_000_000_000:
                    self._dispatch_rx_gap_buckets["lt_1s"] += 1
                else:
                    self._dispatch_rx_gap_buckets["ge_1s"] += 1
            last_recv_ns = now_ns
            # SIOCINQ AFTER recvfrom returns: bytes still queued in the
            # kernel for this socket. Best-effort — ioctl can fail in
            # odd kernel states; on failure we silently skip the bucket
            # update for this packet (matching agent.py's pattern).
            if _HAVE_SIOCINQ and inq_buf is not None:
                try:
                    fcntl.ioctl(sock.fileno(), _SIOCINQ, inq_buf, True)
                    inq_bytes = struct.unpack("i", bytes(inq_buf))[0]
                except (OSError, AttributeError):
                    # AttributeError covers test fakes lacking fileno();
                    # OSError covers real-socket ioctl failures.
                    inq_bytes = -1
                if inq_bytes >= 0:
                    if inq_bytes == 0:
                        self._dispatch_rx_backlog_buckets["inq_0"] += 1
                    elif inq_bytes <= 512:
                        self._dispatch_rx_backlog_buckets["le_512"] += 1
                    elif inq_bytes <= 4096:
                        self._dispatch_rx_backlog_buckets["le_4k"] += 1
                    elif inq_bytes <= 32768:
                        self._dispatch_rx_backlog_buckets["le_32k"] += 1
                    else:
                        self._dispatch_rx_backlog_buckets["gt_32k"] += 1
            self._dispatch_counters["replies_received"] += 1
            peer_addr = peer[0] if isinstance(peer, tuple) else None
            # Strip any scope-id suffix ("fe80::1%eth0") so link-local
            # forms don't break the lookup. Canonicalize through
            # ipaddress.IPv6Address so the key form matches what
            # inner_addr() produced at __init__ (both routed through
            # _canon_ipv6).
            agent = (
                self._demux.get(_canon_ipv6(peer_addr))
                if peer_addr else None
            )
            if agent is None:
                self._dispatch_counters["replies_no_peer"] += 1
                log.debug(
                    "mrc.daemon.dispatch: reply from unknown peer %s "
                    "(known: %s)", peer_addr, sorted(self._demux),
                )
                continue
            magic = payload[0]
            if magic == 0xA6:
                self._dispatch_counters["replies_dispatched_probe"] += 1
                self._handle_reply_safe(agent, payload, kind="probe_reply")
            elif magic == 0xA7:
                self._dispatch_counters["replies_dispatched_loss"] += 1
                self._handle_loss_safe(agent, payload, kind="loss_report")
            else:
                self._dispatch_counters["replies_unknown_magic"] += 1
                log.debug("mrc.daemon.dispatch: unknown magic 0x%02x", magic)

    @staticmethod
    def _handle_reply_safe(
        agent: SenderMrcAgent, payload: bytes, *, kind: str,
    ) -> None:
        try:
            agent._handle_probe_reply(payload)  # noqa: SLF001 — internal API
        except Exception as e:  # pragma: no cover — defensive
            log.debug("mrc.daemon.dispatch: %s handler raised: %s", kind, e)

    @staticmethod
    def _handle_loss_safe(
        agent: SenderMrcAgent, payload: bytes, *, kind: str,
    ) -> None:
        try:
            agent._handle_loss_report(payload)  # noqa: SLF001 — internal API
        except Exception as e:  # pragma: no cover — defensive
            log.debug("mrc.daemon.dispatch: %s handler raised: %s", kind, e)

    def _snapshot_loop(self) -> None:
        """Write per-flow EVStateTable.snapshot() JSON to /dev/shm.

        Writes are atomic (write-then-rename) so a reader catching the
        publisher mid-write never sees a truncated file. Cadence is
        `probe_interval_ms`; readers refresh on `loss_window_ms` so
        the snapshot is at most one publish interval stale.
        """
        interval_s = self.cfg.probe_interval_ms / 1000.0
        # Write an initial snapshot immediately so readers don't race
        # against the first publish.
        self._publish_all_snapshots()
        while not self._stop.is_set():
            if self._stop.wait(interval_s):
                return
            self._publish_all_snapshots()
        # Final snapshot on stop so any reader just-spawned sees the
        # last known state.
        try:
            self._publish_all_snapshots()
        except Exception:  # pragma: no cover — best effort on shutdown
            pass

    def _publish_all_snapshots(self) -> None:
        for (tenant, dst_id), agent in self.agents.items():
            try:
                snap = agent.table.snapshot()
            except Exception as e:
                log.debug(
                    "mrc.daemon.snapshot: snapshot(%s/%d) failed: %s",
                    tenant, dst_id, e,
                )
                continue
            # Wrap with daemon-context metadata so a reader can sanity
            # check it's the right file (paranoia against cross-flow
            # mixups during development). transport_stats is daemon-
            # wide (all flows share self.transport), included here so
            # any single snapshot file is self-contained for jq.
            payload = {
                "src_host": self.src_host,
                "src_id": self.src_id,
                "tenant": tenant,
                "dst_id": dst_id,
                "captured_ns": self.clock_ns(),
                "ev_state": snap,
                "transport_stats": self.transport.stats(),
                "dispatch_stats": dict(self._dispatch_counters),
                "dispatch_rx_gap_buckets": dict(self._dispatch_rx_gap_buckets),
                "dispatch_rx_backlog_buckets":
                    dict(self._dispatch_rx_backlog_buckets),
                "probe_reply_stats": dict(agent.probe_reply_stats),
                "reply_latency_buckets": dict(agent.reply_latency_buckets),
            }
            path = self._snapshot_path(tenant, dst_id)
            self._atomic_write_json(path, payload)

    def _snapshot_path(self, tenant: str, dst_id: int) -> Path:
        return self.snapshot_dir / f"{tenant}_{dst_id:02d}.json"

    def final_report_path(self) -> Path:
        """Path of the on-exit final-report file.

        See FINAL_REPORT_FILENAME for why the file (not stdout) is
        the source of truth the orchestrator reads back.
        """
        return self.snapshot_dir / FINAL_REPORT_FILENAME

    def write_final_report_file(self) -> Path:
        """Compute final_report() and atomically write it to disk.

        Returns the path written. The orchestrator retrieves this
        file via `docker exec <host> cat <path>` after teardown —
        far more reliable than draining the daemon's stdout through
        the dockerd exec multiplex stream, which silently drops
        trailing frames at container exit (see FINAL_REPORT_FILENAME).
        """
        self._ensure_snapshot_dir()
        path = self.final_report_path()
        self._atomic_write_json(path, self.final_report())
        return path

    def _ensure_snapshot_dir(self) -> None:
        try:
            self.snapshot_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            log.warning(
                "mrc.daemon: cannot create snapshot dir %s: %s",
                self.snapshot_dir, e,
            )

    @staticmethod
    def _atomic_write_json(path: Path, payload: Dict[str, Any]) -> None:
        """Atomic JSON write: write to <path>.tmp + rename(<path>).

        Linux rename(2) is atomic on the same filesystem, so a reader
        opening <path> always sees either the previous content or the
        new content — never a truncated/partial file.
        """
        tmp = path.with_suffix(path.suffix + ".tmp")
        try:
            with open(tmp, "w") as f:
                json.dump(payload, f)
            os.replace(tmp, path)
        except OSError as e:
            log.debug("mrc.daemon: atomic write %s: %s", path, e)
            # Best-effort cleanup of the temp file.
            try:
                tmp.unlink()
            except OSError:
                pass

    # --- internal helpers ----------------------------------------------

    def _spawn(self, fn: Callable[[], None], *, name: str) -> None:
        t = threading.Thread(target=fn, name=name, daemon=True)
        t.start()
        self._threads.append(t)
