"""MRC daemon — single per-host process owning shared RX + per-flow agents.

Background
----------
At all-to-all scale the previous design (one `spray --role send`
process per flow, each binding `(::, SPRAY_REPORT_PORT)` with
`SO_REUSEPORT` for its own reply listener) produced catastrophic
reply misdelivery: the Linux kernel hashes inbound UDP packets across
the REUSEPORT group by 4-tuple, so a peer's reply stream gets pinned
to one specific process — the *correct* owner of the host->peer flow
only ~1/N of the time.

The cure: a single MRC daemon process per src_host that owns the one
shared recv listener and dispatches inbound packets to the right
per-flow `SenderMrcAgent`.

Process model (stateless-probe design, 2026-05-25)
--------------------------------------------------
For src_host yellow-host00 with N peer flows:

    spray --role mrc-daemon --flows-json '[{"tenant":"yellow","dst_id":1},...]'
        └─ MrcDaemon owns:
             - 1 Srv6RawTransport(is_sender=True), binds 9997 once
             - N EVStateTables, N SenderMrcAgent instances
             - 1 dispatcher thread on the shared recv socket
             - 1 snapshot publisher thread

Wire-format demux (replaces the v3 reply/match design)
------------------------------------------------------
Two packet types arrive on the daemon's single
``(::, SPRAY_REPORT_PORT)`` listener:

  - magic ``0xA5`` — a stateless PROBE that round-tripped via the
    peer host's leaf and came back to this sender. The payload's
    ``dst_id`` byte identifies the originating peer flow; the
    payload's ``(plane_id, path_id)`` identifies the EV. The
    inner-dst (the sender's own per-EV /128) is a cross-check via
    ``probe_ev_from_inner_dst`` but is not the primary key.
  - magic ``0xA7`` — a LOSS_REPORT from a peer receiver. The sender
    of the LOSS_REPORT is the host the report describes; we recover
    its host_id from ``peer_addr`` (the inbound IPv6 source = the
    peer's tenant-inner anycast) via ``host_id_from_inner_addr``.

There is no per-probe correspondence state on the sender. The probe-
clock / match-table from the v3 design is deleted; EV health is a
sliding-window recv/sent ratio rotated by ``EVStateTable.tick()``.

Threading model
---------------
Daemon threads (2):
  - dispatcher: reads ``transport.recv_socket()``, decodes magic
    byte, dispatches to the right per-flow agent by ``dst_id``
    (payload byte for probes, ``host_id_from_inner_addr(peer)``
    for loss reports).
  - snapshot publisher: every ``probe_interval_ms``, writes per-flow
    ``EVStateTable.snapshot()`` JSON to
    ``/dev/shm/srv6-mrc/<host>/<tenant>_<dst>.json``.

Each per-flow ``SenderMrcAgent`` spawns 2 threads: ``mrc-emit`` and
``mrc-window`` (the latter also drives ``EVStateTable.tick()``). The
old ``mrc-sweep`` and ``mrc-reply-rx`` threads are gone.

Lifecycle
---------
  d = MrcDaemon(src_host, flows, ...)
  d.start()
  d.wait_for_stop()
  d.stop()
"""

from __future__ import annotations

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

# SIOCINQ guard: macOS dev laptops without termios.FIONREAD degrade to
# a no-op so unit tests run on any OS. Lab containers are linux/alpine
# so this is always available.
try:
    import fcntl  # type: ignore[import-untyped]
    import termios  # type: ignore[import-untyped]
    _SIOCINQ = getattr(termios, "FIONREAD", None)
    _HAVE_SIOCINQ = _SIOCINQ is not None
except ImportError:
    _HAVE_SIOCINQ = False
    _SIOCINQ = None

# SCM_TIMESTAMPNS constant for parsing recvmsg ancillary data. On Linux
# it's the same numeric value as SO_TIMESTAMPNS (35). macOS dev laptops
# lack the symbol; we fall back to the numeric value so lab containers
# work even if Python's socket module didn't surface it.
_SCM_TIMESTAMPNS = getattr(socket, "SCM_TIMESTAMPNS", 35)

# struct timespec: 2 * native long (tv_sec, tv_nsec). 16 bytes on
# 64-bit Linux.
_TIMESPEC_FMT = "ll"
_TIMESPEC_SIZE = struct.calcsize(_TIMESPEC_FMT)

from ..topo import (
    NUM_PLANES,
    NUM_SPINES,
    host_id_from_inner_addr,
    probe_ev_from_inner_dst,
)
from .agent import AgentConfig, SenderMrcAgent
from .ev_state import EVStateConfig, EVStateTable
from .probe import ProbeDecodeError, decode_probe
from .transport import (
    DEFAULT_RECV_BUFSIZE,
    MrcTransport,
    Srv6RawTransport,
)


log = logging.getLogger(__name__)


DEFAULT_SNAPSHOT_DIR = "/dev/shm/srv6-mrc"

# Filename used by `write_final_report_file()` for the on-exit dump.
# Reading this back from disk is the orchestrator's authoritative path
# for retrieving the daemon's final report — `docker exec` stdout is
# unreliable for payloads of this size (see AGENTS.md gotcha).
FINAL_REPORT_FILENAME = "final_report.json"


# Magic byte constants (mirror srv6_mrc.mrc.probe but pinned here so
# the dispatch hot loop doesn't import private names).
_MAGIC_PROBE = 0xA5
_MAGIC_LOSS_REPORT = 0xA7


@dataclass(frozen=True)
class DaemonFlow:
    """One flow owned by the daemon: (tenant, dst_id).

    `src_id` is the daemon's own host id and is shared by all flows.
    """
    tenant: str
    dst_id: int


class MrcDaemon:
    """Per-src_host MRC daemon: shared RX + per-flow agents.

    Constructs ONE Srv6RawTransport(is_sender=True) and shares it
    across all per-flow SenderMrcAgent instances. The daemon's
    dispatcher thread reads from the shared recv socket, decodes the
    magic byte, and routes:
      - returning stateless probes (0xA5) -> agent.record_probe_recv
        keyed on the payload's ``dst_id`` byte.
      - loss reports (0xA7) -> agent._handle_loss_report keyed on the
        peer's inner-anycast IPv6 source.

    Snapshot publisher writes per-flow ``EVStateTable.snapshot()`` JSON
    to ``<snapshot_dir>/<src_host>/<tenant>_<dst>.json`` every
    ``probe_interval_ms``. Data sender processes read those files via
    the ``mrc_snapshot:<path>`` policy.

    Tests should pass ``transport=LoopbackUdpTransport(...)`` so the
    daemon doesn't try to open AF_INET6/SOCK_RAW.
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
        sid_mode: str = "uA",
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
                tenant=flows[0].tenant,
                my_id=src_id,
                is_sender=True,
                sid_mode=sid_mode,
            )
        self.transport = transport

        # Per-flow EVStateTable: one per (tenant, dst_id). Per-flow
        # (not per-tenant) was the 2026-05-24 fix for cross-flow
        # trampling of the consecutive-success / consecutive-timeout
        # counter pair; preserved here even though the stateless
        # design uses a sliding window because the recv/sent ratio
        # is still per-peer and racing writes from N flows on the
        # same shared record would produce nonsensical ratios.
        self.tables: Dict[Tuple[str, int], EVStateTable] = {
            (flow.tenant, flow.dst_id): EVStateTable(
                num_planes=NUM_PLANES,
                num_paths=NUM_SPINES,
                tenants=(flow.tenant,),
                cfg=ev_cfg,
            )
            for flow in self.flows
        }

        # Per-flow agents. `transport=self.transport` shares the daemon's
        # transport; the agent does NOT construct its own.
        self.agents: Dict[Tuple[str, int], SenderMrcAgent] = {}

        # Demux map for the dispatcher hot loop: dst_id -> agent.
        #
        # Returning-probe path: the payload's ``dst_id`` byte names the
        # peer host the probe was directed at, which is the per-flow
        # agent's own dst_id. Single dict lookup.
        #
        # Loss-report path: peer_addr is the LOSS_REPORT sender's inner
        # anycast = ``inner_addr(tenant, peer_host_id)``. We recover
        # peer_host_id via ``host_id_from_inner_addr`` and look up the
        # same map (peer_host_id is the sender's dst_id).
        #
        # V1 assumes one tenant per daemon (the Srv6RawTransport itself
        # is single-tenant). Multi-tenant daemons would key by
        # (tenant, dst_id); deferred.
        self._demux: Dict[int, SenderMrcAgent] = {}

        # Dispatch instrumentation. Single-writer (dispatcher thread)
        # int += 1 is GIL-atomic for our read-modify-write pattern;
        # snapshot publisher tolerates one-tick stale reads.
        self._dispatch_counters: Dict[str, int] = {
            "packets_received": 0,        # every recvfrom() w/ payload
            "probes_dispatched": 0,       # routed to agent.record_probe_recv
            "probes_no_flow": 0,          # payload dst_id not in _demux
            "probes_decode_failed": 0,    # ProbeDecodeError
            "probes_ev_mismatch": 0,      # payload (plane,path) <> inner-dst
            "loss_reports_dispatched": 0, # routed to _handle_loss_report
            "loss_reports_no_peer": 0,    # peer_addr -> host_id not in _demux
            "unknown_magic": 0,           # not 0xA5 / 0xA7
        }
        # Sender RX gap / backlog / kernel-dwell buckets. Preserved
        # from the v3 design; they are bug-innocent infrastructure
        # that localizes whether the dispatcher is starved (gap heavy
        # at lt_100ms+) or idle (mostly lt_1ms). Bucket shapes and
        # bucketing logic are unchanged from the previous design so
        # historical snapshot comparisons remain valid.
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
        self._kernel_rx_dwell_buckets: Dict[str, int] = {
            "lt_1ms": 0,
            "lt_10ms": 0,
            "lt_100ms": 0,
            "lt_1s": 0,
            "ge_1s": 0,
            "negative": 0,
            "no_timestamp": 0,
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
            self._demux[flow.dst_id] = agent

    # --- public API ----------------------------------------------------

    def start(self) -> None:
        """Start dispatcher, snapshot publisher, and all per-flow agents.

        Order matters:
          1. Per-flow agents start FIRST so probe TX threads are
             running when the dispatcher begins delivering replies.
          2. Dispatcher starts on the shared recv socket.
          3. Snapshot publisher starts writing initial snapshots.
        """
        self._stop.clear()
        self._ensure_snapshot_dir()

        for agent in self.agents.values():
            agent.start()

        self._spawn(self._dispatch_loop, name="mrc-daemon-dispatch")
        self._spawn(self._snapshot_loop, name="mrc-daemon-snapshot")

    def stop(self, *, timeout_s: float = 2.0) -> None:
        """Signal threads to exit, close transport, join.

        Per-flow agents are stopped with `close_transport=False`
        because the daemon owns the shared transport. The daemon
        closes it once after all agents have stopped.
        """
        self._stop.set()

        for agent in self.agents.values():
            try:
                agent.stop(timeout_s=timeout_s, close_transport=False)
            except Exception as e:
                log.debug("mrc.daemon: agent stop: %s", e)

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
        """Block until stop() is called (e.g. by signal handler)."""
        while not self._stop.is_set():
            self._stop.wait(poll_s)

    def final_report(self) -> Dict[str, Any]:
        """JSON-serializable snapshot of all flows' state on shutdown.

        Shape (mirrors what the v3 daemon emitted, minus the
        ``probe_clock`` block which no longer exists in the
        stateless-probe design):

            {
              "src_host": "yellow-host00",
              "src_id": 0,
              "flows": {
                "yellow/01": {"ev_state": ..., "loss_fusion": ...},
                ...
              }
            }
        """
        from ..cli.spray import (  # lazy import to avoid CLI/runtime cycle
            _loss_fusion_stats_to_dict,
        )
        flows_out: Dict[str, Any] = {}
        for (tenant, dst_id), agent in self.agents.items():
            try:
                flows_out[f"{tenant}/{dst_id:02d}"] = {
                    "ev_state": agent.table.snapshot(),
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
        """Single drain loop on the shared recv socket.

        Reads one packet, decodes the magic byte, and dispatches:
          0xA5 (returning stateless probe) -> agent.record_probe_recv
              keyed on the payload's dst_id byte.
          0xA7 (loss report) -> agent._handle_loss_report keyed on
              host_id_from_inner_addr(peer_addr).

        Unknown peers / unknown magic increment the dispatch counters
        and continue; defensive against malformed packets in the hot
        loop.
        """
        try:
            sock = self.transport.recv_socket()
        except RuntimeError as e:
            log.error("mrc.daemon.dispatch: %s", e)
            return
        last_recv_ns: Optional[int] = None
        inq_buf = bytearray(4) if _HAVE_SIOCINQ else None
        # `recvmsg` is the path that surfaces SCM_TIMESTAMPNS ancillary
        # data; test fakes (FakeRecvSocket) only expose `recvfrom`.
        use_recvmsg = hasattr(sock, "recvmsg")
        anc_bufsize = 256
        while not self._stop.is_set():
            kernel_rx_realtime_ns: Optional[int] = None
            try:
                if use_recvmsg:
                    payload, ancdata, _flags, peer = sock.recvmsg(
                        DEFAULT_RECV_BUFSIZE, anc_bufsize,
                    )
                    for cmsg_level, cmsg_type, cmsg_data in ancdata:
                        if (
                            cmsg_level == socket.SOL_SOCKET
                            and cmsg_type == _SCM_TIMESTAMPNS
                            and len(cmsg_data) >= _TIMESPEC_SIZE
                        ):
                            ts_sec, ts_nsec = struct.unpack(
                                _TIMESPEC_FMT, cmsg_data[:_TIMESPEC_SIZE],
                            )
                            kernel_rx_realtime_ns = (
                                ts_sec * 1_000_000_000 + ts_nsec
                            )
                            break
                else:
                    payload, peer = sock.recvfrom(DEFAULT_RECV_BUFSIZE)
            except socket.timeout:
                continue
            except OSError:
                return  # socket closed during stop()
            if not payload:
                continue
            now_ns = time.monotonic_ns()
            if kernel_rx_realtime_ns is not None:
                userland_realtime_ns = time.time_ns()
                dwell_ns = userland_realtime_ns - kernel_rx_realtime_ns
                if dwell_ns < 0:
                    self._kernel_rx_dwell_buckets["negative"] += 1
                elif dwell_ns < 1_000_000:
                    self._kernel_rx_dwell_buckets["lt_1ms"] += 1
                elif dwell_ns < 10_000_000:
                    self._kernel_rx_dwell_buckets["lt_10ms"] += 1
                elif dwell_ns < 100_000_000:
                    self._kernel_rx_dwell_buckets["lt_100ms"] += 1
                elif dwell_ns < 1_000_000_000:
                    self._kernel_rx_dwell_buckets["lt_1s"] += 1
                else:
                    self._kernel_rx_dwell_buckets["ge_1s"] += 1
            else:
                self._kernel_rx_dwell_buckets["no_timestamp"] += 1
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
            if _HAVE_SIOCINQ and inq_buf is not None:
                try:
                    fcntl.ioctl(sock.fileno(), _SIOCINQ, inq_buf, True)
                    inq_bytes = struct.unpack("i", bytes(inq_buf))[0]
                except (OSError, AttributeError):
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
            self._dispatch_counters["packets_received"] += 1

            magic = payload[0]
            if magic == _MAGIC_PROBE:
                self._dispatch_probe(payload, peer)
            elif magic == _MAGIC_LOSS_REPORT:
                self._dispatch_loss_report(payload, peer)
            else:
                self._dispatch_counters["unknown_magic"] += 1
                log.debug(
                    "mrc.daemon.dispatch: unknown magic 0x%02x", magic,
                )

    def _dispatch_probe(self, payload: bytes, peer: Any) -> None:
        """Returning-probe path: payload byte ``dst_id`` keys the agent.

        Optionally cross-checks the (plane, path) extracted from the
        peer source IP against the payload's declared (plane, path)
        as a corruption canary. peer is the inner-src placeholder
        (e.g. cccc::ffff for yellow) so we can't extract EV identity
        from it — instead the daemon's recvmsg path could surface
        the inner-DST via IPV6_PKTINFO. Today we trust the payload
        and log mismatches via probe_ev_from_inner_dst only when the
        test fixture passes a peer that happens to be a per-EV /128
        (this is the case for some test transports). On the lab path
        peer_addr is the placeholder and we skip the cross-check.
        """
        try:
            probe = decode_probe(payload)
        except ProbeDecodeError as e:
            self._dispatch_counters["probes_decode_failed"] += 1
            log.debug("mrc.daemon.dispatch: bad probe: %s", e)
            return
        agent = self._demux.get(probe.dst_id)
        if agent is None:
            self._dispatch_counters["probes_no_flow"] += 1
            log.debug(
                "mrc.daemon.dispatch: returning probe for unknown "
                "dst_id %d (known: %s)",
                probe.dst_id, sorted(self._demux),
            )
            return
        # Optional cross-check: if peer happens to be a per-EV /128
        # (some loopback tests synthesize this), the EV recovered
        # from inner-dst MUST match the payload's (plane, path).
        # On the lab path peer is the inner-src placeholder and the
        # check is skipped.
        peer_addr = peer[0] if isinstance(peer, tuple) else None
        if peer_addr:
            ev = probe_ev_from_inner_dst(peer_addr)
            if ev is not None:
                _t, _h, ev_plane, ev_path = ev
                if (ev_plane, ev_path) != (probe.plane_id, probe.path_id):
                    self._dispatch_counters["probes_ev_mismatch"] += 1
                    log.debug(
                        "mrc.daemon.dispatch: probe EV mismatch "
                        "payload=(%d,%d) inner_dst=(%d,%d)",
                        probe.plane_id, probe.path_id, ev_plane, ev_path,
                    )
                    # Mismatch is a corruption canary; record the
                    # payload-stated EV anyway so a single bit-flip
                    # doesn't lose the signal.
        try:
            agent.record_probe_recv(probe.plane_id, probe.path_id)
            self._dispatch_counters["probes_dispatched"] += 1
        except Exception as e:  # pragma: no cover - defensive
            log.debug("mrc.daemon.dispatch: record_probe_recv raised: %s", e)

    def _dispatch_loss_report(self, payload: bytes, peer: Any) -> None:
        """Loss-report path: peer_addr -> host_id -> agent.

        peer_addr is the receiver's inner anycast (e.g.
        ``2001:db8:cccc:01::2`` for yellow-host01). We recover the
        host_id and use it as the dst_id key into ``self._demux``
        (the per-flow agent for src_host -> peer_host_id).
        """
        peer_addr = peer[0] if isinstance(peer, tuple) else None
        host = host_id_from_inner_addr(peer_addr) if peer_addr else None
        if host is None:
            self._dispatch_counters["loss_reports_no_peer"] += 1
            log.debug(
                "mrc.daemon.dispatch: loss report from unparseable "
                "peer %s", peer_addr,
            )
            return
        _tenant, host_id = host
        agent = self._demux.get(host_id)
        if agent is None:
            self._dispatch_counters["loss_reports_no_peer"] += 1
            log.debug(
                "mrc.daemon.dispatch: loss report from unknown "
                "peer host_id=%d (known: %s)",
                host_id, sorted(self._demux),
            )
            return
        try:
            agent._handle_loss_report(payload)  # noqa: SLF001 — internal API
            self._dispatch_counters["loss_reports_dispatched"] += 1
        except Exception as e:  # pragma: no cover - defensive
            log.debug("mrc.daemon.dispatch: loss handler raised: %s", e)

    def _snapshot_loop(self) -> None:
        """Write per-flow EVStateTable.snapshot() JSON to /dev/shm."""
        interval_s = self.cfg.probe_interval_ms / 1000.0
        self._publish_all_snapshots()
        while not self._stop.is_set():
            if self._stop.wait(interval_s):
                return
            self._publish_all_snapshots()
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
                "kernel_rx_dwell_buckets":
                    dict(self._kernel_rx_dwell_buckets),
                "probe_emit_buckets": dict(agent.probe_emit_buckets),
            }
            path = self._snapshot_path(tenant, dst_id)
            self._atomic_write_json(path, payload)

    def _snapshot_path(self, tenant: str, dst_id: int) -> Path:
        return self.snapshot_dir / f"{tenant}_{dst_id:02d}.json"

    def final_report_path(self) -> Path:
        """Path of the on-exit final-report file."""
        return self.snapshot_dir / FINAL_REPORT_FILENAME

    def write_final_report_file(self) -> Path:
        """Compute final_report() and atomically write it to disk."""
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
        """Atomic JSON write: write to <path>.tmp + rename(<path>)."""
        tmp = path.with_suffix(path.suffix + ".tmp")
        try:
            with open(tmp, "w") as f:
                json.dump(payload, f)
            os.replace(tmp, path)
        except OSError as e:
            log.debug("mrc.daemon: atomic write %s: %s", path, e)
            try:
                tmp.unlink()
            except OSError:
                pass

    # --- internal helpers ----------------------------------------------

    def _spawn(self, fn: Callable[[], None], *, name: str) -> None:
        t = threading.Thread(target=fn, name=name, daemon=True)
        t.start()
        self._threads.append(t)
