"""MRC probe + loss-report I/O layer (sender and receiver agents).

This module wraps the pure-logic pieces from probe_clock.py,
loss_window.py and loss_compute.py with the actual sockets and threads,
and exposes two coordinator classes:

  SenderMrcAgent
      Started inside spray.py --role send when policy=health_aware_mrc.
      Drives EV probes per plane, listens for replies + loss reports,
      and feeds an EVStateTable that the HealthAwareMrc policy reads
      from once per pick.

  ReceiverMrcAgent
      Started inside spray.py --role recv when MRC is enabled.
      Listens for probes on every plane and unicasts replies back on
      the same plane; tracks per-(flow, plane) data-packet loss in
      rolling windows and unicasts LOSS_REPORTs back to the senders
      identified by the reply_addr cached from received probes.

Threading model
---------------
SenderMrcAgent runs FOUR daemon threads:
  - emit thread: every probe_interval_ms, send one PROBE per
    `(plane, path)` EV (NUM_PLANES * NUM_SPINES probes per round)
  - rx thread (1 total): blocks on the single reply socket
    (sender's well-known report port) and demultiplexes by magic
    byte — PROBE_REPLY -> ProbeClock.match_reply -> EVStateTable;
    LOSS_REPORT -> loss_compute.apply_loss_report. The unified
    socket model matches the actual wire (PROBE_REPLYs and
    LOSS_REPORTs both land on SPRAY_REPORT_PORT after the kernel
    decaps the SRv6 carrier).
  - timeout-sweep thread: every probe_interval_ms, calls
    ProbeClock.sweep_timeouts, pushes each timeout into EVStateTable
    as a failed probe
  - window-rotate thread: snapshots the per-EV sent counters into
    a SentWindow and pushes it into the SentWindowRing every
    loss_window_ms

Plus a small piece of state on the sender hot path: per-EV TX counters
that the runner's progress_cb feeds via `agent.record_sent(plane, path)`.

ReceiverMrcAgent runs TWO daemon threads per agent:
  - probe-RX thread (1 total): blocks on the receiver's well-known
    probe socket, decodes PROBE, builds + sends a PROBE_REPLY back on
    the same `(plane, path)` EV the PROBE arrived on (extracted from
    the payload). The reply is SRv6-encapped via the transport so
    plane symmetry is preserved on the wire.
  - loss-emit thread: every loss_window_ms, snapshot each known flow
    in LossWindowTable, encode LOSS_REPORT, send it via the transport
    on the (plane, path) EV cached from the last PROBE we received
    from that sender.

Plus: `agent.record_data(flow_key, plane, path, seq)` hooked into the
existing data-receive path so the LossWindowTable sees data packets.

All threads are daemons; they exit when the main thread does. Each
thread checks `self._stop.is_set()` on its select/sleep wakeups so a
caller-driven stop is responsive too (used by tests).

Transport abstraction
---------------------
All on-the-wire I/O is delegated to an `MrcTransport` (see
`srv6_fabric.mrc.transport`):

  Srv6RawTransport  (lab)
      Per-plane AF_INET6 SOCK_RAW IPPROTO_RAW sockets bound via
      SO_BINDTODEVICE to PLANE_NICS[p]. Sends are
      `encap.build_outer_packet` outputs so probes follow the same
      EV-steered SRv6 path as data. RX uses a single UDP listener
      on the well-known port (kernel decaps inbound).
  LoopbackUdpTransport  (tests)
      Per-plane UDP sockets on ::1, no encap, per-plane port
      offsets for plane attribution. Sender and receiver typically
      live in one test process.

The agent never branches on `use_loopback` directly; that flag lives
in `AgentConfig` only to decide which transport to construct by
default. Tests inject their own transport via the `transport=` kwarg.
"""

from __future__ import annotations

import logging
import socket
import threading
import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple

from ..topo import (
    NUM_PLANES,
    NUM_SPINES,
    SPRAY_PROBE_PORT,
    SPRAY_REPORT_PORT,
    tenant_id as topo_tenant_id,
)
from .ev_state import EVStateTable
from .loss_compute import (
    LossFusionStats,
    SentWindow,
    SentWindowRing,
    apply_loss_report,
)
from .loss_window import LossWindowTable
from .probe import (
    ProbeDecodeError,
    decode_loss_report,
    decode_probe,
    decode_probe_reply,
    encode_loss_report,
    encode_probe,
    encode_probe_reply,
)
from .probe_clock import ProbeClock
from .transport import (
    DEFAULT_RECV_BUFSIZE,
    DEFAULT_SOCKET_TIMEOUT_S,
    LoopbackUdpTransport,
    MrcTransport,
    Srv6RawTransport,
)


log = logging.getLogger(__name__)


# --- defaults (conservative; agent is off by default in scenarios) ---------

DEFAULT_PROBE_INTERVAL_MS = 200
DEFAULT_PROBE_TIMEOUT_MS = 100
DEFAULT_LOSS_WINDOW_MS = 200
DEFAULT_MAX_WINDOW_SKEW_MS = 500
# DEFAULT_RECV_BUFSIZE and DEFAULT_SOCKET_TIMEOUT_S now live in
# srv6_fabric.mrc.transport and are re-imported above so existing
# call sites in this module keep working unchanged.


@dataclass
class AgentConfig:
    """Wall-clock cadence + sockets config. Times are milliseconds at
    this layer (converted to ns for the ProbeClock)."""
    probe_interval_ms: int = DEFAULT_PROBE_INTERVAL_MS
    probe_timeout_ms: int = DEFAULT_PROBE_TIMEOUT_MS
    loss_window_ms: int = DEFAULT_LOSS_WINDOW_MS
    max_window_skew_ms: int = DEFAULT_MAX_WINDOW_SKEW_MS
    use_loopback: bool = False  # tests set True; lab leaves False

    def __post_init__(self) -> None:
        for f in ("probe_interval_ms", "probe_timeout_ms",
                  "loss_window_ms", "max_window_skew_ms"):
            v = getattr(self, f)
            if v <= 0:
                raise ValueError(f"{f} must be positive, got {v}")


# Env-var name used by the orchestrator (mrc/run.py) to push MRC
# tunables into per-container spray.py invocations. Single JSON blob
# so we don't fan out to one env var per knob.
MRC_CONFIG_ENV = "SRV6_MRC_CONFIG_JSON"

# Fields the env config may set on each dataclass. Kept in sync with
# scenario.MrcSpec; anything not in either set is rejected.
_AGENT_CONFIG_FIELDS = frozenset({
    "probe_interval_ms", "probe_timeout_ms",
    "loss_window_ms", "max_window_skew_ms",
})
_EV_STATE_CONFIG_FIELDS = frozenset({
    "probe_fail_threshold", "probe_recover_threshold",
    "loss_threshold", "loss_demote_consecutive",
    "min_active_evs", "rtt_ring_size",
})


def load_configs_from_env(
    env_value: Optional[str] = None,
) -> Tuple["AgentConfig", "EVStateConfig | None"]:
    """Build (AgentConfig, EVStateConfig|None) from the JSON env blob.

    `env_value` is the literal env-var value (or None to read from
    os.environ[MRC_CONFIG_ENV]; missing env returns all-defaults).
    Returns the EVStateConfig as None if no ev-state fields were
    overridden, so callers can pass `cfg=None` to EVStateTable() and
    get the table's own defaults.

    Raises ValueError on malformed JSON or unknown keys. We deliberately
    fail loud here — a typo in a scenario YAML that survives validation
    (e.g. a future schema field) shouldn't silently revert to defaults
    in the lab.
    """
    import json
    import os
    if env_value is None:
        env_value = os.environ.get(MRC_CONFIG_ENV)
    if not env_value:
        return AgentConfig(), None
    try:
        payload = json.loads(env_value)
    except json.JSONDecodeError as e:
        raise ValueError(
            f"{MRC_CONFIG_ENV} is not valid JSON: {e}"
        ) from None
    if not isinstance(payload, dict):
        raise ValueError(
            f"{MRC_CONFIG_ENV} must encode a JSON object, "
            f"got {type(payload).__name__}"
        )
    known = _AGENT_CONFIG_FIELDS | _EV_STATE_CONFIG_FIELDS
    unknown = set(payload) - known
    if unknown:
        raise ValueError(
            f"{MRC_CONFIG_ENV} has unknown keys {sorted(unknown)}; "
            f"known: {sorted(known)}"
        )
    agent_kwargs = {k: payload[k] for k in payload if k in _AGENT_CONFIG_FIELDS}
    ev_kwargs = {k: payload[k] for k in payload if k in _EV_STATE_CONFIG_FIELDS}
    # Lazy import EVStateConfig only when we actually need to build one,
    # to keep the import graph minimal for tests that don't touch env.
    if ev_kwargs:
        from .ev_state import EVStateConfig
        ev_cfg = EVStateConfig(**ev_kwargs)
    else:
        ev_cfg = None
    return AgentConfig(**agent_kwargs), ev_cfg



# --- sender agent ----------------------------------------------------------


class SenderMrcAgent:
    """Per-flow sender-side MRC agent.

    One instance per --role send process. Owns:
      - the EVStateTable read by HealthAwareMrc.pick()
      - a ProbeClock + an MrcTransport for probe TX
      - a SentWindowRing + a window-rotate timer
      - a LossFusionStats counter

    Lifecycle: construct -> start() -> run for the duration of the
    spray flow -> stop(). stop() is best-effort: threads are daemons.

    All on-the-wire I/O is delegated to `self.transport` (an
    MrcTransport). In the lab the default Srv6RawTransport builds
    SRv6-encapped probes via `srv6_fabric.encap.build_outer_packet`
    and writes them to per-plane raw sockets bound via
    SO_BINDTODEVICE. In tests a LoopbackUdpTransport is passed in
    explicitly via the `transport=` kwarg so no encap or CAP_NET_RAW
    is needed.
    """

    def __init__(
        self,
        *,
        tenant: str,
        src_id: int,
        dst_id: int,
        table: EVStateTable,
        config: AgentConfig,
        transport: Optional[MrcTransport] = None,
        clock_ns: Callable[[], int] = time.monotonic_ns,
    ) -> None:
        if table.num_planes != NUM_PLANES:
            raise ValueError(
                f"EVStateTable.num_planes={table.num_planes} but "
                f"topo NUM_PLANES={NUM_PLANES}"
            )
        if tenant not in table.tenants:
            raise ValueError(
                f"tenant {tenant!r} not in table {table.tenants}"
            )

        self.tenant = tenant
        self.tenant_id = topo_tenant_id(tenant)
        self.src_id = src_id
        self.dst_id = dst_id
        self.table = table
        self.cfg = config
        self.clock_ns = clock_ns

        self._stop = threading.Event()
        self._threads: List[threading.Thread] = []
        self._lock = threading.Lock()

        self.stats = LossFusionStats()
        self.probe_clock = ProbeClock(
            num_planes=NUM_PLANES,
            num_paths=NUM_SPINES,
            probe_timeout_ns=config.probe_timeout_ms * 1_000_000,
        )
        self.sent_ring = SentWindowRing(
            num_planes=NUM_PLANES, num_paths=NUM_SPINES,
        )

        # If the caller didn't supply a transport, build the default
        # lab transport. Construction opens NUM_PLANES raw sockets,
        # which requires CAP_NET_RAW — tests therefore must always
        # pass an explicit transport=LoopbackUdpTransport(...) and
        # never let this fallback run.
        if transport is None:
            transport = Srv6RawTransport(
                tenant=tenant, my_id=src_id, is_sender=True,
            )
        self.transport = transport

        # Sender-side per-EV TX counter for the current emit-window,
        # indexed `[plane][path]`. Updated by record_sent() on the hot
        # path; snapshotted (and reset) by the window-rotate thread.
        # Per-EV (rather than per-plane) granularity is required so the
        # apply_loss_report fusion can pair its denominator against the
        # receiver's per-EV `seen` counter — a per-plane denominator
        # over-counts by the spray-fanout factor and would force every
        # EV's loss ratio toward 1.0 even when the EV is healthy.
        self._current_window_sent: List[List[int]] = [
            [0] * NUM_SPINES for _ in range(NUM_PLANES)
        ]
        self._current_window_start_ns: int = self.clock_ns()
        self._current_window_id: int = 0

    # --- public API ----------------------------------------------------

    def start(self) -> None:
        """Start all daemon threads."""
        self._stop.clear()
        self._spawn(self._emit_loop, name="mrc-emit")
        self._spawn(self._sweep_loop, name="mrc-sweep")
        self._spawn(self._window_rotate_loop, name="mrc-window")
        # Single rx thread: demultiplexes PROBE_REPLY vs LOSS_REPORT
        # by magic byte. Replaces the per-plane reply-rx loops and
        # the separate report-rx loop that the pre-Phase-1b/step-2
        # design used.
        self._spawn(self._reply_rx_loop, name="mrc-reply-rx")

    def stop(self, *, timeout_s: float = 1.0) -> None:
        """Signal threads to exit; close sockets. Threads are daemons so
        we don't require them to actually join in time."""
        self._stop.set()
        # Closing the transport's sockets unblocks any in-flight
        # recvfrom. The transport owns close()-safety; we just call it.
        try:
            self.transport.close()
        except Exception:
            pass
        deadline = time.monotonic() + timeout_s
        for t in self._threads:
            remaining = deadline - time.monotonic()
            if remaining > 0:
                t.join(timeout=remaining)

    def record_sent(self, plane: int, path: int = 0) -> None:
        """Hook for the runner's progress_cb. O(1), lock-free per call;
        the window-rotate thread snapshots the counters under the lock.

        `path` is the EV index inside the plane (0..NUM_SPINES-1). The
        default of 0 lets older non-EV-aware callers keep working — they
        attribute all packets to path 0, which is still correct for
        the per-plane roll-up but degrades per-EV loss attribution.
        EV-aware spray paths (cli/spray.py) pass the real spine id.

        Out-of-range (plane, path) are silently dropped — defensive: a
        policy returning garbage shouldn't crash the agent.
        """
        if 0 <= plane < NUM_PLANES and 0 <= path < NUM_SPINES:
            # We trade strict atomicity for speed here: Python's list
            # element += is not atomic, but a single thread (the spray
            # hot loop) calls record_sent, and the window-rotate thread
            # takes the lock when snapshotting. Worst case is one
            # off-by-one in a snapshot taken concurrently with an
            # increment, which is negligible for loss math.
            self._current_window_sent[plane][path] += 1

    # --- thread bodies -------------------------------------------------

    def _emit_loop(self) -> None:
        """Send one PROBE per `(plane, path)` EV every probe_interval_ms.

        Iterates the full NUM_PLANES * NUM_SPINES grid each round so
        every EV gets independent liveness and RTT signal. With the
        default probe_interval_ms=200 and the 4*8 grid that's
        160 probes/sec — trivial overhead next to the data rate.
        Probes use the transport, which builds the SRv6 outer for the
        EV in the lab path and addresses the matching per-plane
        loopback socket in tests.
        """
        interval_s = self.cfg.probe_interval_ms / 1000.0
        next_tick = time.monotonic()
        while not self._stop.is_set():
            now_ns = self.clock_ns()
            for plane in range(NUM_PLANES):
                for path in range(NUM_SPINES):
                    req_id, tx_ns = self.probe_clock.emit(
                        plane, path=path, now_ns=now_ns,
                    )
                    try:
                        payload = encode_probe(
                            req_id=req_id,
                            plane_id=plane,
                            tx_ns=tx_ns,
                            path_id=path,
                            tenant_id=self.tenant_id,
                            src_id=self.src_id,
                            reply_port=SPRAY_REPORT_PORT,
                        )
                    except ValueError:
                        # tx_ns occasionally exceeds u64 on systems
                        # with unusual clocks — treat as a soft error.
                        log.warning(
                            "mrc.probe: encode_probe failed "
                            "for (plane=%d, path=%d)", plane, path,
                        )
                        continue
                    try:
                        self.transport.send_probe(
                            plane=plane, path=path,
                            dst_leaf=self.dst_id,
                            payload=payload,
                        )
                    except OSError as e:
                        log.debug(
                            "mrc.probe: send (plane=%d, path=%d) "
                            "failed: %s", plane, path, e,
                        )
                        # Probe is still considered "outstanding"; it
                        # will time out naturally and trigger a
                        # probe-fail signal. Right semantic for "I
                        # tried to probe but the kernel refused."
            next_tick += interval_s
            sleep_s = next_tick - time.monotonic()
            if sleep_s < 0:
                # Falling behind; reset cadence rather than spin.
                next_tick = time.monotonic()
            else:
                self._stop.wait(sleep_s)

    def _sweep_loop(self) -> None:
        """Check for outstanding probes past the timeout."""
        interval_s = self.cfg.probe_interval_ms / 1000.0
        while not self._stop.is_set():
            timeouts = self.probe_clock.sweep_timeouts(self.clock_ns())
            for plane, path, _req_id in timeouts:
                self.table.record_probe_result(
                    self.tenant, plane, path, success=False,
                )
            self._stop.wait(interval_s)

    def _reply_rx_loop(self) -> None:
        """Unified RX loop: PROBE_REPLY + LOSS_REPORT on one socket.

        The well-known port (SPRAY_REPORT_PORT) receives both reply
        types after the kernel decaps the SRv6 carrier (lab) or after
        the loopback transport delivers the raw payload (tests). We
        dispatch by the first byte (the magic field that every wire
        format defines uniquely — see srv6_fabric.mrc.probe).
        """
        try:
            sock = self.transport.recv_reply_socket()
        except RuntimeError as e:
            # Mis-configured transport (e.g. receiver-only). Log + bail.
            log.error("mrc.reply: %s", e)
            return
        while not self._stop.is_set():
            try:
                payload, _from = sock.recvfrom(DEFAULT_RECV_BUFSIZE)
            except socket.timeout:
                continue
            except OSError:
                return  # socket closed during stop()
            if not payload:
                continue
            magic = payload[0]
            if magic == 0xA6:
                self._handle_probe_reply(payload)
            elif magic == 0xA7:
                self._handle_loss_report(payload)
            else:
                log.debug("mrc.reply: unknown magic 0x%02x", magic)

    def _handle_probe_reply(self, payload: bytes) -> None:
        try:
            reply = decode_probe_reply(payload)
        except ProbeDecodeError as e:
            log.debug("mrc.probe: bad reply: %s", e)
            return
        now_ns = self.clock_ns()
        rtt_ns = self.probe_clock.match_reply(
            req_id=reply.req_id,
            plane=reply.plane_id,
            path=reply.path_id,
            reply_tx_ns=reply.tx_ns,
            now_ns=now_ns,
        )
        if rtt_ns is None:
            return
        self.table.record_probe_result(
            self.tenant, reply.plane_id, reply.path_id,
            success=True, rtt_ns=rtt_ns,
        )

    def _handle_loss_report(self, payload: bytes) -> None:
        try:
            report = decode_loss_report(payload)
        except ProbeDecodeError as e:
            log.debug("mrc.probe: bad loss report: %s", e)
            return
        apply_loss_report(
            table=self.table,
            tenant=self.tenant,
            report=report,
            sent_ring=self.sent_ring,
            received_at_ns=self.clock_ns(),
            max_window_skew_ns=self.cfg.max_window_skew_ms * 1_000_000,
            stats=self.stats,
        )

    def _window_rotate_loop(self) -> None:
        """Close + ring-push a SentWindow every loss_window_ms."""
        interval_s = self.cfg.loss_window_ms / 1000.0
        while not self._stop.is_set():
            self._stop.wait(interval_s)
            if self._stop.is_set():
                return
            self._rotate_window()

    def _rotate_window(self) -> None:
        """Snapshot the current sent counters into a closed SentWindow.

        Resets per-EV counters under the lock so concurrent record_sent
        calls don't lose increments straddling the rotate. The snapshot
        is stored as a tuple-of-tuples so SentWindow remains hashable /
        immutable.
        """
        now_ns = self.clock_ns()
        with self._lock:
            sent = tuple(tuple(row) for row in self._current_window_sent)
            start = self._current_window_start_ns
            wid = self._current_window_id
            self._current_window_sent = [
                [0] * NUM_SPINES for _ in range(NUM_PLANES)
            ]
            self._current_window_start_ns = now_ns
            self._current_window_id = (wid + 1) & 0xFFFF
        if any(any(row) for row in sent):
            self.sent_ring.push(SentWindow(
                start_ns=start, end_ns=now_ns,
                sent=sent, window_id=wid,
            ))

    # --- internal helpers ----------------------------------------------

    def _spawn(self, fn, *, name: str, args: tuple = ()) -> None:
        t = threading.Thread(target=fn, name=name, args=args, daemon=True)
        t.start()
        self._threads.append(t)


# --- receiver agent --------------------------------------------------------

@dataclass
class _SenderAddr:
    """Cached reply EV for a sender we've seen probes from.

    The receiver returns PROBE_REPLY + LOSS_REPORT to the sender by
    SRv6-encap'ing a packet on the EV `(last_plane, last_path)` — the
    same EV the most-recent PROBE arrived on. Because that PROBE
    survived the round-trip from sender to us, the reverse EV is the
    best-known-working candidate; if it later fails the next PROBE
    from the same sender on a different EV updates the cache.

    `src_id` is the sender's host_id (which equals its leaf id in
    the 1-host-per-leaf topology); we use it as `dst_leaf` for the
    outer uSID lookup via the transport.

    `report_port` is preserved for forward-compat with future probe
    payload versions that may steer replies to a non-default port;
    today every sender uses SPRAY_REPORT_PORT.
    """
    src_id: int
    last_plane: int
    last_path: int
    report_port: int


class ReceiverMrcAgent:
    """Per-host receiver-side MRC agent.

    Started by spray.py --role recv when MRC is enabled. Owns:
      - the LossWindowTable into which the data-RX path feeds packets
      - an MrcTransport whose recv_probe_socket() is the single
        UDP listener for inbound PROBEs (the kernel decaps the
        SRv6 carrier for us; the inner UDP arrives natively)
      - a loss-emit timer that periodically encodes + transports
        LOSS_REPORTs back to the cached sender addresses, using the
        most-recent EV we received a PROBE from that sender on

    The data RX path stays in spray.py / runner.py; this agent
    exposes `record_data(flow_key, plane, path, seq)` for that
    path to call.

    Reply egress symmetry — the PROBE_REPLY for a probe that
    arrived on `(plane=P, path=S)` is sent SRv6-encapped on the
    same `(P, S)` EV. This preserves the property that, in steady
    state, each EV experiences round-trip traffic, so probe RTTs
    measure end-to-end EV health (not just forward path).
    """

    def __init__(
        self,
        *,
        tenant: str,
        my_id: int,
        config: AgentConfig,
        transport: Optional[MrcTransport] = None,
        clock_ns: Callable[[], int] = time.monotonic_ns,
    ) -> None:
        self.tenant = tenant
        self.my_id = my_id
        self.cfg = config
        self.clock_ns = clock_ns
        self._stop = threading.Event()
        self._threads: List[threading.Thread] = []

        self.loss_table = LossWindowTable(
            num_planes=NUM_PLANES, num_paths=NUM_SPINES,
        )

        # Cache of (tenant_id, src_id) -> sender reply info, learned
        # from received PROBEs. Keyed by tenant_id + src_id (not the
        # full FlowKey, because the receiver doesn't yet know which
        # FlowKey a sender's data packets will use; senders identify
        # themselves at the probe level by tenant/src_id pair).
        self._senders: Dict[Tuple[int, int], _SenderAddr] = {}
        self._senders_lock = threading.Lock()

        if transport is None:
            transport = Srv6RawTransport(
                tenant=tenant, my_id=my_id, is_sender=False,
            )
        self.transport = transport

    # --- public API ----------------------------------------------------

    def start(self) -> None:
        self._stop.clear()
        self._spawn(self._probe_rx_loop, name="mrc-probe-rx")
        self._spawn(self._report_emit_loop, name="mrc-report-emit")

    def stop(self, *, timeout_s: float = 1.0) -> None:
        self._stop.set()
        try:
            self.transport.close()
        except Exception:
            pass
        deadline = time.monotonic() + timeout_s
        for t in self._threads:
            remaining = deadline - time.monotonic()
            if remaining > 0:
                t.join(timeout=remaining)

    def record_data(self, flow_key, plane: int, path: int, seq: int) -> None:
        """Hook for the data-RX path."""
        self.loss_table.record(flow_key, plane=plane, path=path, seq=seq)

    def known_senders(self) -> Tuple[Tuple[int, int], ...]:
        """Test/diagnostic accessor for the sender cache."""
        with self._senders_lock:
            return tuple(self._senders.keys())

    # --- thread bodies -------------------------------------------------

    def _probe_rx_loop(self) -> None:
        """On PROBE rx, learn sender + send a PROBE_REPLY on the same EV.

        Plane / path attribution comes from the probe payload's
        `plane_id` / `path_id` (every probe carries both). The reply
        is sent SRv6-encapped on that same EV via the transport, so
        the round-trip stays plane-symmetric on the wire.
        """
        try:
            sock = self.transport.recv_probe_socket()
        except RuntimeError as e:
            log.error("mrc.recv: %s", e)
            return
        while not self._stop.is_set():
            try:
                payload, _peer = sock.recvfrom(DEFAULT_RECV_BUFSIZE)
            except socket.timeout:
                continue
            except OSError:
                return
            try:
                probe = decode_probe(payload)
            except ProbeDecodeError as e:
                log.debug("mrc.recv: bad probe: %s", e)
                continue
            self._learn_sender(
                tenant_id=probe.tenant_id, src_id=probe.src_id,
                plane=probe.plane_id, path=probe.path_id,
                report_port=probe.reply_port,
            )
            try:
                reply_payload = encode_probe_reply(
                    req_id=probe.req_id,
                    plane_id=probe.plane_id,
                    tx_ns=probe.tx_ns,
                    svc_time_ns=0,  # we don't measure service time today
                    path_id=probe.path_id,
                    tenant_id=probe.tenant_id,
                    src_id=probe.src_id,
                    reply_port=probe.reply_port,
                )
            except ValueError:
                continue
            try:
                self.transport.send_probe_reply(
                    plane=probe.plane_id, path=probe.path_id,
                    dst_leaf=probe.src_id, payload=reply_payload,
                )
            except OSError as e:
                log.debug("mrc.recv: reply send failed: %s", e)

    def _report_emit_loop(self) -> None:
        """Every loss_window_ms, emit a LOSS_REPORT per known flow."""
        interval_s = self.cfg.loss_window_ms / 1000.0
        while not self._stop.is_set():
            self._stop.wait(interval_s)
            if self._stop.is_set():
                return
            self._emit_one_round()

    def _emit_one_round(self) -> None:
        """For each known flow, snapshot + send a LOSS_REPORT.

        The report is steered back to the sender via the transport on
        the EV cached from the last PROBE we received from that
        sender. This is the best-known-working reverse EV; no
        receiver-side health table is required because the sender
        will catch the missing-report case via its own probe-timeout
        sweep on a different EV.
        """
        for flow_key in self.loss_table.known_flows():
            report = self.loss_table.snapshot_and_reset(flow_key)
            if not report.planes:
                continue
            # flow_key convention: (tenant_id, src_id, dst_id). Receiver
            # code in spray.py is responsible for choosing this shape
            # when calling record_data.
            if not (isinstance(flow_key, tuple) and len(flow_key) >= 2):
                log.debug(
                    "mrc.recv: flow_key %r not in (tid, sid, ...) "
                    "shape; cannot route report", flow_key,
                )
                continue
            key = (flow_key[0], flow_key[1])
            with self._senders_lock:
                sender = self._senders.get(key)
            if sender is None:
                # Never received a probe from this sender; we have no
                # reply EV. Skip (the report will retry next window
                # once a probe arrives).
                continue
            try:
                payload = encode_loss_report(
                    window_id=report.window_id,
                    planes=list(report.planes),
                )
            except ValueError:
                continue
            try:
                self.transport.send_loss_report(
                    plane=sender.last_plane, path=sender.last_path,
                    dst_leaf=sender.src_id, payload=payload,
                )
            except OSError as e:
                log.debug("mrc.recv: loss report send failed: %s", e)

    # --- helpers -------------------------------------------------------

    def _learn_sender(
        self, *, tenant_id: int, src_id: int,
        plane: int, path: int, report_port: int,
    ) -> None:
        with self._senders_lock:
            self._senders[(tenant_id, src_id)] = _SenderAddr(
                src_id=src_id,
                last_plane=plane,
                last_path=path,
                report_port=report_port,
            )

    def _spawn(self, fn, *, name: str, args: tuple = ()) -> None:
        t = threading.Thread(target=fn, name=name, args=args, daemon=True)
        t.start()
        self._threads.append(t)


__all__ = [
    "AgentConfig",
    "MRC_CONFIG_ENV",
    "SenderMrcAgent",
    "ReceiverMrcAgent",
    "load_configs_from_env",
]
