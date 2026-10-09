"""Tests for opt-in packet trimming, NACK and fast retransmit (Phase 1).

Covers the wire codecs, trim-aware loss accounting and fusion, the
sender's trim oracle and retransmit path, the receiver's trim detection,
and the scenario schema. Also pins the opt-in contract: without trims,
LOSS_REPORT bytes are exactly the v2 encoding.
"""
from __future__ import annotations

import socket
import struct
import threading
import unittest
from unittest import mock

from srv6_mrc import runner
from srv6_mrc.encap import build_outer_packet
from srv6_mrc.mrc import scenario
from srv6_mrc.mrc.agent import AgentConfig, ReceiverMrcAgent
from srv6_mrc.mrc.ev_state import EVState, EVStateConfig, EVStateTable
from srv6_mrc.mrc.loss_compute import (
    LossFusionStats, SentWindow, SentWindowRing, apply_loss_report,
)
from srv6_mrc.mrc.loss_window import LossWindowTable
from srv6_mrc.mrc.probe import (
    LOSS_REPORT_VERSION, LOSS_REPORT_VERSION_TRIMMED, LossReport, Nack,
    PlaneLossRecord, ProbeDecodeError, decode_loss_report, decode_nack,
    encode_loss_report, encode_nack,
)
from srv6_mrc.policy import EvSpray
from srv6_mrc.topo import SPRAY_PORT, inner_addr, nack_port
from srv6_mrc.trim import NackListener, TrimOracle, TrimSpec, TrimStats


class CodecTests(unittest.TestCase):
    def test_nack_roundtrip(self):
        n = Nack(plane_id=1, path_id=2, tenant_id=1, src_id=2, dst_id=5,
                 seq=2**40 + 7)
        raw = encode_nack(n)
        self.assertEqual(len(raw), 18)
        self.assertEqual(raw[0], 0xA9)
        self.assertEqual(decode_nack(raw), n)

    def test_nack_rejects_other_magic(self):
        raw = bytearray(encode_nack(Nack(0, 0, 1, 0, 1, 1)))
        raw[0] = 0xA7
        with self.assertRaises(ProbeDecodeError):
            decode_nack(bytes(raw))

    def test_report_without_trims_is_v2_byte_for_byte(self):
        recs = [PlaneLossRecord(plane_id=1, path_id=3, seen=9, expected=12,
                                max_gap=4)]
        raw = encode_loss_report(7, recs)
        legacy = (struct.pack("!BBHHH", 0xA7, LOSS_REPORT_VERSION, 7, 1, 0)
                  + struct.pack("!BBHIII", 1, 3, 0, 9, 12, 4))
        self.assertEqual(raw, legacy)

    def test_report_with_trims_is_v3_and_roundtrips(self):
        recs = (PlaneLossRecord(1, 3, seen=0, expected=0, max_gap=0,
                                trimmed=5),
                PlaneLossRecord(0, 0, seen=9, expected=9, max_gap=1))
        raw = encode_loss_report(7, recs)
        self.assertEqual(raw[1], LOSS_REPORT_VERSION_TRIMMED)
        rep = decode_loss_report(raw)
        self.assertEqual([r.trimmed for r in rep.planes], [5, 0])

    def test_trimmed_clamped_to_u16_on_the_wire(self):
        raw = encode_loss_report(0, [PlaneLossRecord(0, 0, 0, 0, 0,
                                                     trimmed=70000)])
        self.assertEqual(decode_loss_report(raw).planes[0].trimmed, 65535)


class LossAccountingTests(unittest.TestCase):
    def test_trimmed_only_ev_is_reported(self):
        t = LossWindowTable(num_planes=2, num_paths=2)
        for _ in range(4):
            t.record_trimmed("f", 1, 1)
        rep = t.snapshot_and_reset("f")
        self.assertEqual(len(rep.planes), 1)
        r = rep.planes[0]
        self.assertEqual((r.plane_id, r.path_id, r.seen, r.trimmed),
                         (1, 1, 0, 4))

    def _fuse(self, report, sent, windows=3):
        t = EVStateTable(tenants=("g",), num_planes=2, num_paths=2,
                         cfg=EVStateConfig(min_active_evs=1))
        ring = SentWindowRing(num_planes=2, num_paths=2)
        ring.push(SentWindow(start_ns=0, end_ns=100_000_000, sent=sent))
        stats = LossFusionStats()
        for _ in range(windows):
            apply_loss_report(table=t, tenant="g", report=report,
                              sent_ring=ring, received_at_ns=50_000_000,
                              max_window_skew_ns=10**9, stats=stats)
        return t, stats

    def test_trims_do_not_count_as_loss(self):
        report = LossReport(window_id=0, planes=(
            PlaneLossRecord(0, 0, seen=10, expected=10, max_gap=1),
            PlaneLossRecord(1, 0, seen=4, expected=10, max_gap=3,
                            trimmed=6),
        ))
        t, _ = self._fuse(report, ((10, 0), (10, 0)))
        self.assertIsNot(t.state("g", 1, 0), EVState.ASSUMED_BAD)
        self.assertEqual(t.inspect("g", 1, 0)["last_loss_ratio"], 0.0)

    def test_fully_trimmed_ev_is_not_treated_as_blackholed(self):
        report = LossReport(window_id=0, planes=(
            PlaneLossRecord(0, 0, seen=10, expected=10, max_gap=1),
            PlaneLossRecord(1, 0, seen=0, expected=0, max_gap=0, trimmed=10),
        ))
        t, stats = self._fuse(report, ((10, 0), (10, 0)))
        self.assertEqual(stats.absent_evs_counted_as_lost, 0)
        self.assertIsNot(t.state("g", 1, 0), EVState.ASSUMED_BAD)

    def test_same_loss_as_drops_still_demotes(self):
        # Control: without the trimmed count the EV reads 60% loss.
        report = LossReport(window_id=0, planes=(
            PlaneLossRecord(0, 0, seen=10, expected=10, max_gap=1),
            PlaneLossRecord(1, 0, seen=4, expected=10, max_gap=3),
        ))
        t, _ = self._fuse(report, ((10, 0), (10, 0)))
        self.assertIs(t.state("g", 1, 0), EVState.ASSUMED_BAD)


class TrimSpecTests(unittest.TestCase):
    def test_env_roundtrip(self):
        spec = TrimSpec(rate=0.3, mode="drop", planes=(1,), evs=((0, 2),),
                        seed=9, max_retransmits=2)
        self.assertEqual(TrimSpec.from_env(spec.to_env_json()), spec)

    def test_unset_env_means_off(self):
        self.assertIsNone(TrimSpec.from_env(""))

    def test_validation(self):
        with self.assertRaises(ValueError):
            TrimSpec(rate=1.5)
        with self.assertRaises(ValueError):
            TrimSpec(rate=0.1, mode="ecn")

    def test_matching(self):
        spec = TrimSpec(rate=1.0, planes=(1,), evs=((0, 3),))
        self.assertTrue(spec.matches(1, 0))
        self.assertTrue(spec.matches(0, 3))
        self.assertFalse(spec.matches(0, 2))
        self.assertTrue(TrimSpec(rate=1.0).matches(3, 3))

    def test_oracle_is_deterministic_and_near_rate(self):
        spec = TrimSpec(rate=0.3, seed=4)
        o1, o2 = TrimOracle(spec), TrimOracle(spec)
        s1 = [o1.hits(0, 0) for _ in range(2000)]
        s2 = [o2.hits(0, 0) for _ in range(2000)]
        self.assertEqual(s1, s2)
        self.assertAlmostEqual(sum(s1) / 2000, 0.3, delta=0.04)

    def test_oracle_ignores_other_evs(self):
        o = TrimOracle(TrimSpec(rate=1.0, planes=(1,)))
        self.assertFalse(any(o.hits(0, q) for q in range(4)))


class NackListenerTests(unittest.TestCase):
    def _listener(self, max_retx=2):
        sock = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
        sock.bind(("::1", 0))
        return NackListener(dst_id=5, max_retransmits=max_retx,
                            stats=TrimStats(), sock=sock), sock

    def test_bounded_retransmits_for_known_seqs_only(self):
        lst, sock = self._listener(max_retx=2)
        self.addCleanup(sock.close)
        lst.note_sent(41)
        for _ in range(3):
            lst.on_nack(41, 1, 2)
        lst.on_nack(99, 1, 2)  # never sent
        self.assertEqual(lst.take(), [(41, 1, 2), (41, 1, 2)])
        self.assertEqual(lst.stats.nacks_received, 4)
        self.assertEqual(lst.stats.nacks_ignored, 2)

    def test_nack_over_a_socket_reaches_the_queue(self):
        lst, sock = self._listener()
        port = sock.getsockname()[1]
        lst.note_sent(7)
        lst.start()
        self.addCleanup(lst.stop)
        tx = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
        self.addCleanup(tx.close)
        tx.sendto(encode_nack(Nack(1, 0, 1, 2, 5, 7)), ("::1", port))
        got = []
        for _ in range(50):
            got = lst.take()
            if got:
                break
            threading.Event().wait(0.02)
        self.assertEqual(got, [(7, 1, 0)])


class _FakeSock:
    def __init__(self, plane, log):
        self.plane, self.log = plane, log

    def sendto(self, pkt, sa):
        self.log.append((self.plane, pkt))

    def close(self):
        pass


class _StubListener:
    """Hands the sender its NACKs once `after` packets have gone out."""

    def __init__(self, nacks, after=6):
        self._nacks = list(nacks)
        self._after = after
        self.sent = []

    def start(self):
        pass

    def stop(self):
        pass

    def note_sent(self, seq):
        self.sent.append(seq)

    def take(self):
        if len(self.sent) < self._after:
            return []
        out, self._nacks = self._nacks, []
        return out


def _udp_payload_len(pkt: bytes) -> int:
    return len(pkt) - 40 - 40 - 8


def _seq_plane_path(pkt: bytes):
    return struct.unpack("!QBB", pkt[88:98])


class SenderTests(unittest.TestCase):
    def _run(self, *, trim=None, listener=None, duration=0.05, rate=400):
        log = []
        socks = iter(range(8))
        with mock.patch.object(runner, "_open_send_socket",
                               side_effect=lambda nic: _FakeSock(next(socks),
                                                                 log)):
            res = runner.run_sender(
                runner.FlowEndpoint(tenant="green", src_id=2, dst_id=5),
                EvSpray(), rate, duration, trim=trim,
                nack_listener=listener,
            )
        return res, log

    def test_without_trim_every_packet_is_full(self):
        res, log = self._run()
        self.assertTrue(log)
        self.assertTrue(all(_udp_payload_len(p) == 42 for _, p in log))
        self.assertNotIn("trim", res.to_dict())

    def test_oracle_trims_matching_plane_only(self):
        spec = TrimSpec(rate=1.0, planes=(1,))
        res, log = self._run(trim=spec, listener=_StubListener([]))
        for plane, pkt in log:
            self.assertEqual(_udp_payload_len(pkt), 10 if plane == 1 else 42)
        self.assertEqual(res.trim.trimmed, sum(1 for pl, _ in log if pl == 1))
        self.assertEqual(res.sent, len(log))

    def test_rdma_trimmed_form_keeps_the_bth(self):
        from srv6_mrc.rdma import unwrap_rdma
        spec = TrimSpec(rate=1.0, planes=(1,))
        log = []
        socks = iter(range(8))
        with mock.patch.object(runner, "_open_send_socket",
                               side_effect=lambda nic: _FakeSock(next(socks),
                                                                 log)):
            runner.run_sender(
                runner.FlowEndpoint(tenant="green", src_id=2, dst_id=5),
                EvSpray(), 400, 0.05, trim=spec,
                nack_listener=_StubListener([]), transport="rdma",
            )
        self.assertTrue(log)
        for plane, pkt in log:
            dport = struct.unpack("!H", pkt[82:84])[0]
            self.assertEqual(dport, 4791)
            inner = unwrap_rdma(pkt[88:])
            self.assertEqual(len(inner), 10 if plane == 1 else 42)

    def test_drop_mode_sends_nothing_but_counts_it(self):
        spec = TrimSpec(rate=1.0, planes=(1,), mode="drop")
        res, log = self._run(trim=spec, listener=_StubListener([]))
        self.assertTrue(all(plane != 1 for plane, _ in log))
        self.assertGreater(res.trim.dropped, 0)
        self.assertEqual(res.per_plane_sent.get(1), res.trim.dropped)

    def test_nack_triggers_full_retransmit_on_another_ev(self):
        spec = TrimSpec(rate=0.0)
        res, log = self._run(trim=spec,
                             listener=_StubListener([(3, 1, 2)]))
        copies = [(pl, p) for pl, p in log if _seq_plane_path(p)[0] == 3]
        self.assertEqual(res.trim.retransmits, 1)
        self.assertEqual(len(copies), 2)   # original, then the retransmit
        plane, pkt = copies[1]
        _seq, pl, path = _seq_plane_path(pkt)
        self.assertEqual(_udp_payload_len(pkt), 42)
        self.assertNotEqual((pl, path), (1, 2))


class _FakeSniffer:
    """Feeds canned frames to run_receiver's handler on start()."""
    frames: list = []

    def __init__(self, iface, filter, prn, store):
        self.prn = prn

    def start(self):
        from scapy.all import IPv6
        for raw in _FakeSniffer.frames:
            pkt = IPv6(raw)
            pkt.sniffed_on = "eth2"
            self.prn(pkt)

    def stop(self):
        pass


def _frame(seq, plane, path, *, trimmed, transport="udp"):
    src = inner_addr("yellow", 2)
    dst = inner_addr("yellow", 5)
    body = struct.pack("!QBB", seq, plane, path)
    if not trimmed:
        body += b"X" * 32
    dport = SPRAY_PORT
    if transport == "rdma":
        from srv6_mrc.rdma import wrap_rdma
        from srv6_mrc.topo import RDMA_PORT
        body = wrap_rdma(body, plane=plane, path=path, seq=seq)
        dport = RDMA_PORT
    return build_outer_packet(src_underlay=src, dst_outer="fc00:1::",
                              src_inner=src, dst_inner=dst,
                              sport=SPRAY_PORT, dport=dport,
                              payload=body)


class ReceiverTests(unittest.TestCase):
    def _run(self, frames):
        _FakeSniffer.frames = frames
        trimmed_hook = []
        stop = threading.Event()
        stop.set()
        with mock.patch("scapy.all.AsyncSniffer", _FakeSniffer):
            rep = runner.run_receiver(
                "yellow-host05", 5, "yellow", nics=("eth2",),
                stop_event=stop, install_signal_handlers=False,
                on_trimmed=lambda *a: trimmed_hook.append(a),
            )
        return rep, trimmed_hook

    def test_trimmed_then_retransmitted_counts_as_recovered(self):
        rep, hook = self._run([
            _frame(1, 0, 0, trimmed=False),
            _frame(2, 1, 2, trimmed=True),
            _frame(3, 0, 1, trimmed=False),
            _frame(2, 0, 3, trimmed=False),   # the retransmit
        ])
        self.assertEqual([h[1:] for h in hook], [(1, 2, 2)])
        flow = rep["flows"][0]
        self.assertEqual(flow["received"], 3)
        self.assertEqual(flow["trim"], {"trimmed": 1, "recovered": 1,
                                        "unrecovered": 0})

    def test_rdma_framed_trim_is_detected_and_recovered(self):
        # --transport rdma: a trimmed packet keeps its BTH; the trim
        # check runs on the payload after unwrapping it.
        rep, hook = self._run([
            _frame(1, 0, 0, trimmed=False, transport="rdma"),
            _frame(2, 1, 2, trimmed=True, transport="rdma"),
            _frame(2, 0, 3, trimmed=False, transport="rdma"),
        ])
        self.assertEqual([h[1:] for h in hook], [(1, 2, 2)])
        self.assertEqual(rep["flows"][0]["trim"],
                         {"trimmed": 1, "recovered": 1, "unrecovered": 0})

    def test_no_trims_keeps_the_flow_shape(self):
        rep, hook = self._run([_frame(1, 0, 0, trimmed=False)])
        self.assertEqual(hook, [])
        self.assertNotIn("trim", rep["flows"][0])


class ReceiverAgentNackTests(unittest.TestCase):
    def test_trimmed_arrival_sends_nack_to_the_flow_port(self):
        sent = []

        class Xport:
            def send_nack(self, **kw):
                sent.append(kw)

            def close(self):
                pass

        agent = ReceiverMrcAgent(tenant="green", my_id=5,
                                 config=AgentConfig(use_loopback=True),
                                 transport=Xport())
        agent.record_data((1, 2, 5), plane=0, path=3, seq=40)
        agent.record_trimmed((1, 2, 5), plane=1, path=2, seq=41)
        self.assertEqual(len(sent), 1)
        kw = sent[0]
        self.assertEqual((kw["plane"], kw["path"], kw["dst_leaf"]), (0, 3, 2))
        self.assertEqual(kw["dport"], nack_port(5))
        n = decode_nack(kw["payload"])
        self.assertEqual((n.seq, n.plane_id, n.path_id, n.src_id, n.dst_id),
                         (41, 1, 2, 2, 5))
        rep = agent.loss_table.snapshot_and_reset((1, 2, 5))
        trimmed = {(r.plane_id, r.path_id): r.trimmed for r in rep.planes}
        self.assertEqual(trimmed[(1, 2)], 1)
        self.assertEqual(agent.diagnostic_snapshot()["trim"]["nacks_sent"], 1)


class ScenarioTrimTests(unittest.TestCase):
    BASE = {"name": "t", "flows": [{"pairs": "green-pairs-4",
                                    "policy": "health_aware_mrc",
                                    "rate": 100, "duration": "5s"}],
            "mrc": {}}

    def test_trim_block_parses(self):
        s = scenario.validate({**self.BASE, "trim": {
            "rate": 0.3, "planes": [1], "evs": [[0, 2]], "mode": "drop",
            "seed": 3, "max_retransmits": 2}})
        self.assertEqual(s.trim, TrimSpec(rate=0.3, mode="drop", planes=(1,),
                                          evs=((0, 2),), seed=3,
                                          max_retransmits=2))

    def test_trim_needs_mrc(self):
        doc = {k: v for k, v in self.BASE.items() if k != "mrc"}
        with self.assertRaises(scenario.ScenarioError):
            scenario.validate({**doc, "trim": {"rate": 0.1}})

    def test_bad_values_rejected(self):
        for bad in ({"rate": 2}, {"rate": 0.1, "mode": "ecn"},
                    {"rate": 0.1, "evs": [[1]]}, {"rate": 0.1, "knob": 1}):
            with self.subTest(bad=bad), \
                    self.assertRaises(scenario.ScenarioError):
                scenario.validate({**self.BASE, "trim": bad})

    def test_no_trim_block_means_off(self):
        self.assertIsNone(scenario.validate(self.BASE).trim)

    def test_env_carries_the_spec(self):
        from srv6_mrc.mrc.run import _scenario_env
        spec = TrimSpec(rate=0.3, planes=(1,))
        env = _scenario_env(None, None, None, trim=spec)
        self.assertEqual(TrimSpec.from_env(env["SRV6_TRIM_JSON"]), spec)
        self.assertIsNone(_scenario_env(None, None, None))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
