"""Unit tests for srv6_mrc.mrc.probe.

Wire-format codec tests only — no sockets. Round-trip every encode with
its matching decode; verify magic/version checking; verify truncation
and range checks reject malformed input cleanly.
"""


import unittest

from srv6_mrc.mrc import probe
from srv6_mrc.mrc.probe import (
    LossReport,
    LOSS_REPORT_VERSION,
    PROBE_VERSION,
    PlaneLossRecord,
    Probe,
    ProbeDecodeError,
    ProbeReply,
    decode_loss_report,
    decode_probe,
    decode_probe_reply,
    encode_loss_report,
    encode_probe,
    encode_probe_reply,
)


# Default identity fields used everywhere we don't care about them.
# Picked to be non-zero so a "field omitted" bug shows up as a value
# mismatch. path_id defaults to a non-zero value too so a "path
# dropped" regression doesn't silently round-trip via 0.
_ID = dict(path_id=3, tenant_id=1, src_id=15, reply_port=9997)


class TestProbeRoundTrip(unittest.TestCase):
    def test_roundtrip_basic(self):
        b = encode_probe(req_id=42, plane_id=2, tx_ns=1_234_567_890, **_ID)
        p = decode_probe(b)
        self.assertEqual(p, Probe(
            req_id=42, plane_id=2, path_id=3, tx_ns=1_234_567_890,
            tenant_id=1, src_id=15, reply_port=9997,
        ))

    def test_roundtrip_max_values(self):
        b = encode_probe(
            req_id=0xFFFF, plane_id=0xFF, tx_ns=0xFFFFFFFFFFFFFFFF,
            path_id=0xFF,
            tenant_id=0xFFFF, src_id=0xFFFF, reply_port=0xFFFF,
        )
        p = decode_probe(b)
        self.assertEqual(p.req_id, 0xFFFF)
        self.assertEqual(p.plane_id, 0xFF)
        self.assertEqual(p.path_id, 0xFF)
        self.assertEqual(p.tx_ns, 0xFFFFFFFFFFFFFFFF)
        self.assertEqual(p.tenant_id, 0xFFFF)
        self.assertEqual(p.src_id, 0xFFFF)
        self.assertEqual(p.reply_port, 0xFFFF)

    def test_encoded_size(self):
        # v3 wire format still fits in 28B — path_id reuses the v2
        # reserved byte after plane_id.
        b = encode_probe(req_id=0, plane_id=0, tx_ns=0, **_ID)
        self.assertEqual(len(b), 28)

    def test_range_checks(self):
        with self.assertRaises(ValueError):
            encode_probe(req_id=-1, plane_id=0, tx_ns=0, **_ID)
        with self.assertRaises(ValueError):
            encode_probe(req_id=0x10000, plane_id=0, tx_ns=0, **_ID)
        with self.assertRaises(ValueError):
            encode_probe(req_id=0, plane_id=256, tx_ns=0, **_ID)
        with self.assertRaises(ValueError):
            encode_probe(req_id=0, plane_id=0, tx_ns=-1, **_ID)
        with self.assertRaises(ValueError):
            encode_probe(req_id=0, plane_id=0, tx_ns=0,
                         path_id=-1, tenant_id=1, src_id=0, reply_port=0)
        with self.assertRaises(ValueError):
            encode_probe(req_id=0, plane_id=0, tx_ns=0,
                         path_id=256, tenant_id=1, src_id=0, reply_port=0)
        with self.assertRaises(ValueError):
            encode_probe(req_id=0, plane_id=0, tx_ns=0,
                         path_id=0, tenant_id=-1, src_id=0, reply_port=0)
        with self.assertRaises(ValueError):
            encode_probe(req_id=0, plane_id=0, tx_ns=0,
                         path_id=0, tenant_id=0, src_id=0x10000, reply_port=0)


class TestProbeReplyRoundTrip(unittest.TestCase):
    def test_roundtrip_basic(self):
        b = encode_probe_reply(
            req_id=42, plane_id=2, tx_ns=1_234_567_890,
            svc_time_ns=1_500, **_ID,
        )
        r = decode_probe_reply(b)
        self.assertEqual(r, ProbeReply(
            req_id=42, plane_id=2, path_id=3,
            tx_ns=1_234_567_890, svc_time_ns=1_500,
            tenant_id=1, src_id=15, reply_port=9997,
        ))

    def test_zero_svc_time_ok(self):
        b = encode_probe_reply(
            req_id=1, plane_id=0, tx_ns=100, svc_time_ns=0, **_ID,
        )
        r = decode_probe_reply(b)
        self.assertEqual(r.svc_time_ns, 0)

    def test_negative_svc_time_rejected(self):
        with self.assertRaises(ValueError):
            encode_probe_reply(
                req_id=0, plane_id=0, tx_ns=0, svc_time_ns=-1, **_ID,
            )

    def test_identity_echoed_independently(self):
        # The reply identity fields are filled in by the responder from
        # the probe it's echoing; verify they aren't tied to the probe
        # identity at codec level (decoder just passes them through).
        b = encode_probe_reply(
            req_id=1, plane_id=0, tx_ns=0, svc_time_ns=0,
            path_id=7, tenant_id=2, src_id=99, reply_port=5555,
        )
        r = decode_probe_reply(b)
        self.assertEqual(r.path_id, 7)
        self.assertEqual(r.tenant_id, 2)
        self.assertEqual(r.src_id, 99)
        self.assertEqual(r.reply_port, 5555)


class TestProbeMagicAndVersion(unittest.TestCase):
    def test_decode_probe_rejects_reply_magic(self):
        # A PROBE_REPLY shouldn't decode as PROBE.
        b = encode_probe_reply(
            req_id=1, plane_id=0, tx_ns=0, svc_time_ns=0, **_ID,
        )
        with self.assertRaises(ProbeDecodeError):
            decode_probe(b)

    def test_decode_reply_rejects_probe_magic(self):
        b = encode_probe(req_id=1, plane_id=0, tx_ns=0, **_ID)
        with self.assertRaises(ProbeDecodeError):
            decode_probe_reply(b)

    def test_decode_rejects_wrong_version(self):
        # Hand-build a packet with version=2 (the now-retired wire fmt
        # where the path_id byte was reserved). We don't carry v2
        # backward compat — confirm decoder rejects.
        good = encode_probe(req_id=1, plane_id=0, tx_ns=0, **_ID)
        bad = bytes([good[0], 2]) + good[2:]
        with self.assertRaises(ProbeDecodeError):
            decode_probe(bad)

    def test_decode_rejects_truncated_probe(self):
        b = encode_probe(req_id=1, plane_id=0, tx_ns=0, **_ID)
        with self.assertRaises(ProbeDecodeError):
            decode_probe(b[:10])

    def test_decode_rejects_truncated_reply(self):
        b = encode_probe_reply(
            req_id=1, plane_id=0, tx_ns=0, svc_time_ns=0, **_ID,
        )
        with self.assertRaises(ProbeDecodeError):
            decode_probe_reply(b[:10])

    def test_decode_rejects_short_packet(self):
        # A 22B packet with v3 magic+version but truncated mid-payload
        # must be rejected, not silently zero-filled.
        b = encode_probe(req_id=1, plane_id=0, tx_ns=0, **_ID)
        with self.assertRaises(ProbeDecodeError):
            decode_probe(b[:22])


class TestLossReportRoundTrip(unittest.TestCase):
    def test_empty_report_roundtrip(self):
        b = encode_loss_report(window_id=7, planes=[])
        r = decode_loss_report(b)
        self.assertEqual(r, LossReport(window_id=7, planes=()))

    def test_multi_ev_roundtrip(self):
        # Same plane appears with different paths — exactly the new v2
        # capability. Two paths on plane 0, one on plane 1, etc.
        planes = [
            PlaneLossRecord(plane_id=0, path_id=2,
                            seen=1000, expected=1000, max_gap=0),
            PlaneLossRecord(plane_id=0, path_id=5,
                            seen=950, expected=1000, max_gap=5),
            PlaneLossRecord(plane_id=1, path_id=2,
                            seen=500, expected=1000, max_gap=99),
            PlaneLossRecord(plane_id=2, path_id=7,
                            seen=1000, expected=1000, max_gap=1),
        ]
        b = encode_loss_report(window_id=12345, planes=planes)
        r = decode_loss_report(b)
        self.assertEqual(r.window_id, 12345)
        self.assertEqual(len(r.planes), 4)
        for got, want in zip(r.planes, planes):
            self.assertEqual(got, want)

    def test_size_calculation(self):
        # 8B header + N×16B per EV. Record size unchanged from v1.
        b = encode_loss_report(
            window_id=0,
            planes=[
                PlaneLossRecord(plane_id=p, path_id=0,
                                seen=0, expected=0, max_gap=0)
                for p in range(4)
            ],
        )
        self.assertEqual(len(b), 8 + 4 * 16)

    def test_max_values(self):
        plane = PlaneLossRecord(
            plane_id=0xFF, path_id=0xFF, seen=0xFFFFFFFF,
            expected=0xFFFFFFFF, max_gap=0xFFFFFFFF,
        )
        b = encode_loss_report(window_id=0xFFFF, planes=[plane])
        r = decode_loss_report(b)
        self.assertEqual(r.window_id, 0xFFFF)
        self.assertEqual(r.planes[0], plane)

    def test_tuple_accepted_as_input(self):
        # encode_loss_report accepts list or tuple.
        planes = (PlaneLossRecord(plane_id=0, path_id=0,
                                  seen=1, expected=1, max_gap=0),)
        b = encode_loss_report(window_id=0, planes=planes)
        self.assertEqual(decode_loss_report(b).planes, planes)


class TestLossReportRangeChecks(unittest.TestCase):
    def test_negative_seen_rejected(self):
        with self.assertRaises(ValueError):
            PlaneLossRecord(plane_id=0, path_id=0,
                            seen=-1, expected=10, max_gap=0)

    def test_overflow_seen_rejected(self):
        with self.assertRaises(ValueError):
            PlaneLossRecord(
                plane_id=0, path_id=0,
                seen=0x100000000, expected=10, max_gap=0,
            )

    def test_negative_path_id_rejected(self):
        with self.assertRaises(ValueError):
            PlaneLossRecord(plane_id=0, path_id=-1,
                            seen=0, expected=0, max_gap=0)

    def test_overflow_path_id_rejected(self):
        with self.assertRaises(ValueError):
            PlaneLossRecord(plane_id=0, path_id=256,
                            seen=0, expected=0, max_gap=0)

    def test_negative_window_id_rejected(self):
        with self.assertRaises(ValueError):
            encode_loss_report(window_id=-1, planes=[])

    def test_overflow_window_id_rejected(self):
        with self.assertRaises(ValueError):
            encode_loss_report(window_id=0x10000, planes=[])

    def test_bad_planes_input_rejected(self):
        # Wrong element type.
        with self.assertRaises(TypeError):
            encode_loss_report(window_id=0, planes=[(0, 1, 1, 0)])


class TestLossReportMalformedDecode(unittest.TestCase):
    def test_short_header(self):
        with self.assertRaises(ProbeDecodeError):
            decode_loss_report(b"\x00\x01\x02")

    def test_truncated_records(self):
        # Header claims 4 records but only 1 follows.
        good = encode_loss_report(
            window_id=0,
            planes=[
                PlaneLossRecord(plane_id=p, path_id=0,
                                seen=0, expected=0, max_gap=0)
                for p in range(4)
            ],
        )
        # Lop off the last 3 records (3 * 16 = 48 bytes).
        truncated = good[: -48]
        # Header still says num_records=4; decode should reject.
        with self.assertRaises(ProbeDecodeError):
            decode_loss_report(truncated)

    def test_wrong_magic(self):
        # Encode a PROBE and try to decode it as a loss report.
        b = encode_probe(req_id=0, plane_id=0, tx_ns=0, **_ID)
        with self.assertRaises(ProbeDecodeError):
            decode_loss_report(b)

    def test_wrong_version(self):
        # v1 loss reports are no longer accepted.
        b = encode_loss_report(window_id=0, planes=[])
        # version is byte index 1.
        bad = bytes([b[0], 1]) + b[2:]
        with self.assertRaises(ProbeDecodeError):
            decode_loss_report(bad)


class TestModuleSurface(unittest.TestCase):
    def test_version_constants(self):
        # PROBE bumped to v3 (path_id reuses former reserved byte).
        # LOSS_REPORT bumped to v2 (same change in each record).
        self.assertEqual(PROBE_VERSION, 3)
        self.assertEqual(LOSS_REPORT_VERSION, 2)

    def test_all_exports_present(self):
        for name in probe.__all__:
            self.assertTrue(
                hasattr(probe, name),
                f"probe.__all__ lists {name!r} but it's not exported",
            )

    def test_distinct_magics(self):
        # Sanity: PROBE / PROBE_REPLY / LOSS_REPORT first bytes differ.
        b1 = encode_probe(0, 0, 0, **_ID)[0]
        b2 = encode_probe_reply(0, 0, 0, 0, **_ID)[0]
        b3 = encode_loss_report(0, [])[0]
        self.assertEqual(len({b1, b2, b3}), 3)


if __name__ == "__main__":
    unittest.main()
