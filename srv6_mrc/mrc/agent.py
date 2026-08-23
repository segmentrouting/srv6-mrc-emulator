"""MRC stateless-probe + loss-report I/O layer (sender + receiver agents).

This module wraps the pure-logic pieces (`ev_state`, `loss_window`,
`loss_compute`) with sockets and threads. The stateless-probe redesign
(2026-05-25) removed the per-probe match table: there is no
``probe_clock`` and no per-probe correspondence state. EV health is a
sliding-window recv/sent ratio rotated by ``EVStateTable.tick()``.

  SenderMrcAgent
      Owned by ``MrcDaemon``, one per (src_host, peer_host_id) flow.
      Emits stateless PROBEs on each (plane, path) EV every
      ``probe_interval_ms``; the daemon-side dispatch loop calls
      ``record_probe_recv(plane, path)`` when a returning probe
      arrives. The window-rotate thread also ticks the EV state
      table so the sliding window advances on the same cadence as
      probe emission.

  ReceiverMrcAgent
      Started on receiver hosts. Owns the per-(flow,plane,path)
      LossWindowTable and emits LOSS_REPORTs back to senders on the
      EV last observed in DATA arrivals (probes no longer touch the
      receiver — kernel-only forwarding via the leaf `dfff End.DT6`).

All on-the-wire I/O is delegated to an ``MrcTransport`` (lab:
``Srv6RawTransport``; tests: ``LoopbackUdpTransport``).

Threading model
---------------
SenderMrcAgent runs TWO daemon threads:
  - ``mrc-emit``: emits one PROBE per (plane, path) EV every
    ``probe_interval_ms``, paced one probe per slot within the
    interval. Increments ``EVStateTable.record_probe_sent`` after
    each send.
  - ``mrc-window``: every ``loss_window_ms`` rotates the per-EV
    sent-window for loss-fusion. Every ``probe_interval_ms`` also
    calls ``EVStateTable.tick(tenant)`` to advance the EV sliding
    window. (Both cadences live in this loop; the window-id only
    advances on the ``loss_window_ms`` boundary.)

The old ``mrc-sweep`` and ``mrc-reply-rx`` threads are gone — there
are no per-probe timeouts and no separate reply RX (the daemon owns
the single shared recv socket and calls
``SenderMrcAgent.record_probe_recv`` directly).

ReceiverMrcAgent runs ONE daemon thread:
  - ``mrc-report-emit``: every ``loss_window_ms``, encode +
    transport-send a LOSS_REPORT per known sender on the EV last
    observed in DATA arrivals from that sender.

The data-RX path calls ``receiver.record_data(flow_key, plane,
path, seq)`` and that hook now also updates the per-sender
``last_plane, last_path`` cache used to steer the loss report.
"""

from __future__ import annotations

import logging
import random
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..topo import (
    NUM_PLANES,
    NUM_SPINES,
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
    encode_loss_report,
    encode_probe,
)
from .transport import (
    MrcTransport,
    Srv6RawTransport,
)


log = logging.getLogger(__name__)


# --- defaults --------------------------------------------------------------

DEFAULT_PROBE_INTERVAL_MS = 500
# Retained for AgentConfig back-compat; not used by the stateless path
# (no per-probe timeout). Kept so existing scenario JSON / env blobs
# with ``probe_timeout_ms`` keep loading without ValueError.
DEFAULT_PROBE_TIMEOUT_MS = 500
DEFAULT_LOSS_WINDOW_MS = 300
DEFAULT_MAX_WINDOW_SKEW_MS = 500


@dataclass
class AgentConfig:
    """Wall-clock cadence config. Times are milliseconds."""
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
# tunables into per-container spray.py invocations.
MRC_CONFIG_ENV = "SRV6_MRC_CONFIG_JSON"

_AGENT_CONFIG_FIELDS = frozenset({
    "probe_interval_ms", "probe_timeout_ms",
    "loss_window_ms", "max_window_skew_ms",
})
_EV_STATE_CONFIG_FIELDS = frozenset({
    "probe_window_ticks", "probe_min_samples",
    "probe_fail_ratio", "probe_recover_ratio", "probe_recover_ticks",
    "loss_threshold", "loss_demote_consecutive",
    "min_active_evs", "rtt_ring_size",
})


def load_configs_from_env(
    env_value: Optional[str] = None,
) -> Tuple["AgentConfig", "EVStateConfig | None"]:
    """Build (AgentConfig, EVStateConfig|None) from the JSON env blob."""
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
    if ev_kwargs:
        from .ev_state import EVStateConfig
        ev_cfg = EVStateConfig(**ev_kwargs)
    else:
        ev_cfg = None
    return AgentConfig(**agent_kwargs), ev_cfg


# --- sender agent ----------------------------------------------------------


class SenderMrcAgent:
    """Per-flow sender-side MRC agent (stateless-probe design).

    One instance per (src_host, peer_host_id). Owns:
      - the EVStateTable read by HealthAwareMrc.pick()
      - a per-EV SentWindowRing for loss-fusion math
      - LossFusionStats counters

    All probe TX goes through ``self.transport.send_probe`` and the
    returning probe is delivered to ``record_probe_recv`` by the
    daemon dispatcher. The agent never reads from a socket itself.
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
        sid_mode: str = "uA",
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
        # Probe-emit-side latency histogram. Single-writer (emit
        # thread); snapshot publisher tolerates torn reads.
        self.probe_emit_buckets: Dict[str, int] = {
            "lt_100us": 0,
            "lt_1ms": 0,
            "lt_10ms": 0,
            "lt_100ms": 0,
            "lt_1s": 0,
            "ge_1s": 0,
        }
        self.sent_ring = SentWindowRing(
            num_planes=NUM_PLANES, num_paths=NUM_SPINES,
        )

        # If the caller didn't supply a transport, build the default
        # lab transport. Construction opens NUM_PLANES raw sockets,
        # requiring CAP_NET_RAW — tests therefore must always pass
        # transport=LoopbackUdpTransport(...) explicitly.
        if transport is None:
            transport = Srv6RawTransport(
                tenant=tenant, my_id=src_id, is_sender=True,
                sid_mode=sid_mode,
            )
        self.transport = transport

        # Per-instance random jitter applied once at emit-thread start.
        # Decorrelates concurrent senders that would otherwise lockstep
        # on the same probe_interval_ms cadence (see history in the
        # 2026-05-23 emit-pacing entry of AGENTS.md).
        self._emit_jitter_s: float = random.uniform(
            0.0, config.probe_interval_ms / 1000.0,
        )

        # Per-EV TX counter for the current loss-window, indexed
        # [plane][path]. Updated by record_sent() on the hot path
        # (data-plane runner); snapshotted + reset by the window
        # rotate thread.
        self._current_window_sent: List[List[int]] = [
            [0] * NUM_SPINES for _ in range(NUM_PLANES)
        ]
        self._current_window_start_ns: int = self.clock_ns()
        self._current_window_id: int = 0

    # --- public API ----------------------------------------------------

    def start(self) -> None:
        """Start the emit + window threads."""
        self._stop.clear()
        self._spawn(self._emit_loop, name="mrc-emit")
        self._spawn(self._window_loop, name="mrc-window")

    def stop(self, *, timeout_s: float = 1.0,
             close_transport: bool = True) -> None:
        """Signal threads to exit; optionally close sockets."""
        self._stop.set()
        if close_transport:
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
        """Hook for the runner's progress_cb. Per-EV sent counter."""
        if 0 <= plane < NUM_PLANES and 0 <= path < NUM_SPINES:
            self._current_window_sent[plane][path] += 1

    def record_probe_recv(self, plane: int, path: int) -> None:
        """Called by the daemon dispatch loop when a probe round-trips back.

        Bumps the EV's in-progress recv bucket; the sliding window
        is advanced by ``_window_loop`` on each ``probe_interval_ms``
        tick.
        """
        try:
            self.table.record_probe_recv(self.tenant, plane, path)
        except (ValueError, KeyError) as e:
            log.debug("mrc.agent: record_probe_recv ignored: %s", e)

    # --- thread bodies -------------------------------------------------

    def _emit_loop(self) -> None:
        """Send one stateless PROBE per (plane, path) EV every interval.

        Probes within one round are *paced* across the interval (one
        per slot of ``interval / num_evs``) rather than emitted as a
        back-to-back burst. A per-instance startup jitter further
        decorrelates concurrently launched senders.
        """
        interval_s = self.cfg.probe_interval_ms / 1000.0
        num_evs = NUM_PLANES * NUM_SPINES
        slot_s = interval_s / num_evs

        if self._emit_jitter_s > 0.0:
            self._stop.wait(self._emit_jitter_s)
            if self._stop.is_set():
                return

        next_tick = time.monotonic() + interval_s
        while not self._stop.is_set():
            for plane in range(NUM_PLANES):
                for path in range(NUM_SPINES):
                    try:
                        payload = encode_probe(
                            plane_id=plane, path_id=path,
                            tenant_id=self.tenant_id,
                            src_id=self.src_id,
                            dst_id=self.dst_id,
                        )
                    except ValueError:
                        log.warning(
                            "mrc.probe: encode_probe failed "
                            "for (plane=%d, path=%d)", plane, path,
                        )
                        continue
                    emit_t0 = time.monotonic_ns()
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
                        # Treat as a not-sent: skip record_probe_sent
                        # so the sliding-window denominator isn't
                        # inflated by send-failed probes.
                        if self._stop.wait(slot_s):
                            return
                        continue
                    # Successful TX -> bump the EV's sent bucket.
                    try:
                        self.table.record_probe_sent(
                            self.tenant, plane, path,
                        )
                    except (ValueError, KeyError) as e:  # pragma: no cover
                        log.debug(
                            "mrc.agent: record_probe_sent ignored: %s", e,
                        )
                    emit_elapsed_ns = time.monotonic_ns() - emit_t0
                    if emit_elapsed_ns < 100_000:
                        self.probe_emit_buckets["lt_100us"] += 1
                    elif emit_elapsed_ns < 1_000_000:
                        self.probe_emit_buckets["lt_1ms"] += 1
                    elif emit_elapsed_ns < 10_000_000:
                        self.probe_emit_buckets["lt_10ms"] += 1
                    elif emit_elapsed_ns < 100_000_000:
                        self.probe_emit_buckets["lt_100ms"] += 1
                    elif emit_elapsed_ns < 1_000_000_000:
                        self.probe_emit_buckets["lt_1s"] += 1
                    else:
                        self.probe_emit_buckets["ge_1s"] += 1
                    if self._stop.wait(slot_s):
                        return
            sleep_s = next_tick - time.monotonic()
            next_tick += interval_s
            if sleep_s > 0:
                if self._stop.wait(sleep_s):
                    return
            else:
                next_tick = time.monotonic() + interval_s

    def _handle_loss_report(self, payload: bytes) -> None:
        """Process an inbound LOSS_REPORT. Called by MrcDaemon dispatch."""
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

    def _window_loop(self) -> None:
        """Drive both cadences:

          - every probe_interval_ms: tick the EV sliding window so
            the recv/sent ratio observed in the just-completed
            interval drives the next health decision.
          - every loss_window_ms: rotate the sent-window for the
            loss-fusion path.

        We loop at the GCD of the two cadences. In practice both
        defaults are 200ms so a single timer fires both events; if a
        scenario YAML overrides one but not the other, we just tick
        independently with two separate deadlines.
        """
        probe_interval_s = self.cfg.probe_interval_ms / 1000.0
        loss_window_s = self.cfg.loss_window_ms / 1000.0
        next_probe_tick = time.monotonic() + probe_interval_s
        next_window_tick = time.monotonic() + loss_window_s
        while not self._stop.is_set():
            now = time.monotonic()
            sleep_s = min(
                next_probe_tick - now, next_window_tick - now,
            )
            if sleep_s > 0:
                if self._stop.wait(sleep_s):
                    return
            now = time.monotonic()
            if now >= next_probe_tick:
                try:
                    self.table.tick(self.tenant)
                except (ValueError, KeyError) as e:  # pragma: no cover
                    log.debug("mrc.agent: tick raised: %s", e)
                next_probe_tick += probe_interval_s
                if now >= next_probe_tick:
                    # Falling behind: reset.
                    next_probe_tick = now + probe_interval_s
            if now >= next_window_tick:
                self._rotate_window()
                next_window_tick += loss_window_s
                if now >= next_window_tick:
                    next_window_tick = now + loss_window_s

    def _rotate_window(self) -> None:
        """Snapshot + reset the current sent counters."""
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
    """Cached reply EV for a sender we've seen DATA arrivals from.

    Loss reports flow receiver -> sender on the EV most recently
    observed in *data* arrivals from that sender. With stateless
    probes the receiver never sees probes (the leaf decaps + routes
    them back to the sender), so the data-RX path is the only source
    of per-sender EV signal.
    """
    src_id: int
    last_plane: int
    last_path: int


class ReceiverMrcAgent:
    """Per-host receiver-side MRC agent (stateless-probe design).

    Started on receiver hosts. Owns:
      - the LossWindowTable into which the data-RX path feeds packets
      - a per-sender ``last_plane / last_path`` cache learned from
        data arrivals (probes no longer touch this process)
      - a loss-emit timer that periodically encodes + transports
        LOSS_REPORTs back to known senders

    The data RX path stays in spray.py / runner.py; this agent
    exposes ``record_data(flow_key, plane, path, seq)`` for that path
    to call. The ``flow_key`` shape is expected to be a tuple whose
    first two elements are ``(tenant_id, src_id)`` so the receiver
    can attribute data arrivals to a specific sender for loss-report
    routing.
    """

    def __init__(
        self,
        *,
        tenant: str,
        my_id: int,
        config: AgentConfig,
        transport: Optional[MrcTransport] = None,
        clock_ns: Callable[[], int] = time.monotonic_ns,
        sid_mode: str = "uA",
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
        # from received DATA. The receiver no longer sees probes
        # (stateless-probe design), so the only signal for "which EV
        # is alive between me and that sender" is the data-arrival EV.
        self._senders: Dict[Tuple[int, int], _SenderAddr] = {}
        self._senders_lock = threading.Lock()

        if transport is None:
            transport = Srv6RawTransport(
                tenant=tenant, my_id=my_id, is_sender=False,
                sid_mode=sid_mode,
            )
        self.transport = transport

    # --- public API ----------------------------------------------------

    def start(self) -> None:
        self._stop.clear()
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
        """Hook for the data-RX path.

        Updates the loss-window table AND learns the sender's
        per-EV `(last_plane, last_path)` so the next LOSS_REPORT
        can be steered back on a known-working EV.
        """
        self.loss_table.record(flow_key, plane=plane, path=path, seq=seq)
        # flow_key convention: (tenant_id, src_id, dst_id) — the same
        # shape spray.py uses when calling us. We tolerate any tuple
        # whose first two elements are (tenant_id, src_id).
        if isinstance(flow_key, tuple) and len(flow_key) >= 2:
            tenant_id_val = flow_key[0]
            src_id_val = flow_key[1]
            if isinstance(tenant_id_val, int) and isinstance(src_id_val, int):
                with self._senders_lock:
                    self._senders[(tenant_id_val, src_id_val)] = _SenderAddr(
                        src_id=src_id_val,
                        last_plane=plane,
                        last_path=path,
                    )

    def known_senders(self) -> Tuple[Tuple[int, int], ...]:
        """Test/diagnostic accessor for the sender cache."""
        with self._senders_lock:
            return tuple(self._senders.keys())

    def diagnostic_snapshot(self) -> Dict[str, Dict[str, int]]:
        """Per-agent diagnostic counters (placeholder for now).

        The receiver-side probe-RX instrumentation is gone with the
        stateless-probe design; this method is retained so callers
        relying on its presence (the CLI's JSON output) keep working.
        """
        return {}

    # --- thread bodies -------------------------------------------------

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

        Steers via the per-sender cache populated by ``record_data``
        (data-arrival EV; no probe-RX path in the stateless-probe
        design).
        """
        for flow_key in self.loss_table.known_flows():
            report = self.loss_table.snapshot_and_reset(flow_key)
            if not report.planes:
                continue
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
                # No data observed yet; nothing to report against.
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
