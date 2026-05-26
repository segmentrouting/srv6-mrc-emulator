"""Unit tests for srv6_mrc.mrc.probe (v4 stateless probe wire format).

Wire-format codec tests only — no sockets. Round-trip every encode with
its matching decode; verify magic/version checking; verify truncation
and range checks reject malformed input cleanly.

v4 PROBE (stateless redesign, 2026-05-25): 10-byte payload
`magic | version | plane | path | tenant | src | dst | _reserved`.
No PROBE_REPLY; the receiver-side decap/re-encap is now kernel-only,
so the only on-wire frames in the probe channel are the outbound PROBE
(magic 0xA5) and its byte-identical (modulo hlim) return copy.
"""


import unittest

from srv6_mrc.mrc import probe
from srv6_mrc.mrc.probe import (
    LossReport,
    LOSS_REPORT_VERSION,
    PROBE_PAYLOAD_LEN,
    PROBE_VERSION,
    PlaneLossRecord,
    Probe,
    ProbeDecodeError,
    decode_loss_report,
    decode_probe,
    encode_loss_report,
    encode_probe,
)


# Default identity fields used everywhere we don't care about them.
# Picked to be non-zero so a "field omitted" bug shows up as a value
# mismatch.
_ID = dict(tenant_id=1, src_id=15, dst_id=7)


class TestProbeRoundTrip(unittest.TestCase):
    def test_roundtrip_basic(self):
        b = encode_probe(plane_id=2, path_id=3, **_ID)
        p = decode_probe(b)
        self.assertEqual(p, Probe(
            plane_id=2, path_id=3,
            tenant_id=1, src_id=15, dst_id=7,
        ))

    def test_roundtrip_max_values(self):
        b = encode_probe(
            plane_id=0xFF, path_id=0xFF,
            tenant_id=0xFFFF, src_id=0xFFFF, dst_id=0xFF,
        )
        p = decode_probe(b)
        self.assertEqual(p.plane_id, 0xFF)
        self.assertEqual(p.path_id, 0xFF)
        self.assertEqual(p.tenant_id, 0xFFFF)
        self.assertEqual(p.src_id, 0xFFFF)
        self.assertEqual(p.dst_id, 0xFF)

    def test_encoded_size(self):
        # v4 wire format is 10 bytes: magic, version, plane, path,
        # tenant (u16), src (u16), dst, _reserved.
        b = encode_probe(plane_id=0, path_id=0, **_ID)
        self.assertEqual(len(b), PROBE_PAYLOAD_LEN)
        self.assertEqual(len(b), 10)

    def test_range_checks(self):
        with self.assertRaises(ValueError):
            encode_probe(plane_id=-1, path_id=0, **_ID)
        with self.assertRaises(ValueError):
            encode_probe(plane_id=256, path_id=0, **_ID)
        with self.assertRaises(ValueError):
            encode_probe(plane_id=0, path_id=-1, **_ID)
        with self.assertRaises(ValueError):
            encode_probe(plane_id=0, path_id=256, **_ID)
        with self.assertRaises(ValueError):
            encode_probe(plane_id=0, path_id=0,
                         tenant_id=-1, src_id=0, dst_id=0)
        with self.assertRaises(ValueError):
            encode_probe(plane_id=0, path_id=0,
                         tenant_id=0, src_id=0x10000, dst_id=0)
        with self.assertRaises(ValueError):
            encode_probe(plane_id=0, path_id=0,
                         tenant_id=0, src_id=0, dst_id=256)


class TestProbeMagicAndVersion(unittest.TestCase):
    def test_decode_probe_rejects_loss_magic(self):
        # A LOSS_REPORT shouldn't decode as PROBE.
        b = encode_loss_report(window_id=0, planes=[])
        with self.assertRaises(ProbeDecodeError):
            decode_probe(b)

    def test_decode_rejects_wrong_version(self):
        # Hand-build a packet with version=3 (the now-retired v3
        # match-table wire format). v4 is the only accepted version.
        good = encode_probe(plane_id=0, path_id=0, **_ID)
        bad = bytes([good[0], 3]) + good[2:]
        with self.assertRaises(ProbeDecodeError):
            decode_probe(bad)

    def test_decode_rejects_truncated_probe(self):
        b = encode_probe(plane_id=0, path_id=0, **_ID)
        with self.assertRaises(ProbeDecodeError):
            decode_probe(b[:5])

    def test_decode_accepts_oversize_probe(self):
        # Forward-compat: decoder only consumes the first
        # PROBE_PAYLOAD_LEN bytes. Trailing padding (e.g. from a
        # future wire-format extension still using PROBE_VERSION=4)
        # must not cause rejection of an otherwise-valid frame.
        b = encode_probe(plane_id=0, path_id=0, **_ID)
        p = decode_probe(b + b"\x00\x00")
        self.assertEqual(p.dst_id, 7)


class TestLossReportRoundTrip(unittest.TestCase):
    def test_empty_report_roundtrip(self):
        b = encode_loss_report(window_id=7, planes=[])
        r = decode_loss_report(b)
        self.assertEqual(r, LossReport(window_id=7, planes=()))

    def test_multi_ev_roundtrip(self):
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
        with self.assertRaises(TypeError):
            encode_loss_report(window_id=0, planes=[(0, 1, 1, 0)])


class TestLossReportMalformedDecode(unittest.TestCase):
    def test_short_header(self):
        with self.assertRaises(ProbeDecodeError):
            decode_loss_report(b"\x00\x01\x02")

    def test_truncated_records(self):
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
        with self.assertRaises(ProbeDecodeError):
            decode_loss_report(truncated)

    def test_wrong_magic(self):
        b = encode_probe(plane_id=0, path_id=0, **_ID)
        with self.assertRaises(ProbeDecodeError):
            decode_loss_report(b)

    def test_wrong_version(self):
        b = encode_loss_report(window_id=0, planes=[])
        bad = bytes([b[0], 1]) + b[2:]
        with self.assertRaises(ProbeDecodeError):
            decode_loss_report(bad)


class TestModuleSurface(unittest.TestCase):
    def test_version_constants(self):
        # PROBE bumped to v4 (stateless redesign — removed
        # req_id, tx_ns, reply_port; added dst_id; same 10-byte
        # payload as v3 but different field layout).
        # LOSS_REPORT unchanged at v2.
        self.assertEqual(PROBE_VERSION, 4)
        self.assertEqual(LOSS_REPORT_VERSION, 2)

    def test_payload_len_constant(self):
        # PROBE_PAYLOAD_LEN is the contract that the receiver-side
        # template builders and the sender-side recv-buffer sizing
        # depend on. Locking it to 10 prevents accidental wire
        # format expansion without a coordinated update.
        self.assertEqual(PROBE_PAYLOAD_LEN, 10)

    def test_all_exports_present(self):
        for name in probe.__all__:
            self.assertTrue(
                hasattr(probe, name),
                f"probe.__all__ lists {name!r} but it's not exported",
            )

    def test_distinct_magics(self):
        # PROBE (0xA5) and LOSS_REPORT (0xA7) must demux cleanly on
        # the shared recv socket — distinct first-byte magics is
        # the demux contract the daemon dispatcher relies on.
        b1 = encode_probe(plane_id=0, path_id=0, **_ID)[0]
        b2 = encode_loss_report(0, [])[0]
        self.assertNotEqual(b1, b2)
        self.assertEqual(b1, 0xA5)
        self.assertEqual(b2, 0xA7)


if __name__ == "__main__":
    unittest.main()
