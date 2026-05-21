"""Tests for srv6_mrc.runner — pieces that don't need raw sockets or scapy.

The send/recv loops themselves require CAP_NET_RAW and live NICs; those
are exercised in the lab via the orchestrator. Here we cover the
serialization, dataclasses, and host-id parsing logic that are pure.
"""
import unittest

from srv6_mrc.runner import (
    FlowEndpoint, SenderResult, detect_self_id,
    encode_payload, host_for, parse_payload,
    _canon_inner, _should_count_inner,
)
from srv6_mrc.topo import SPRAY_PORT, inner_addr


class TestPayloadCodec(unittest.TestCase):

    def test_roundtrip_basic(self):
        for seq in (0, 1, 42, 2**63 - 1):
            for plane in range(4):
                for path in (0, 1, 7, 255):
                    buf = encode_payload(seq, plane, path)
                    self.assertEqual(
                        parse_payload(buf), (seq, plane, path),
                    )

    def test_default_path_is_zero(self):
        # encode_payload(seq, plane) without an explicit path defaults
        # to 0; lets non-EV-aware callers keep their two-arg form.
        buf = encode_payload(5, 2)
        self.assertEqual(parse_payload(buf), (5, 2, 0))

    def test_payload_length_is_42(self):
        # 8 (seq) + 1 (plane) + 1 (path) + 32 (pad) = 42. Total frame
        # stays >= 64B when wrapped in IPv6+IPv6+UDP, satisfying min
        # ethernet.
        self.assertEqual(len(encode_payload(0, 0, 0)), 42)

    def test_parse_short_returns_none(self):
        self.assertIsNone(parse_payload(b""))
        self.assertIsNone(parse_payload(b"\x00" * 9))   # one byte short

    def test_parse_ignores_trailing_bytes(self):
        # Receiver should accept any pad length >= 0 after the 10B header.
        buf = encode_payload(99, 2, 3) + b"extra junk"
        self.assertEqual(parse_payload(buf), (99, 2, 3))

    def test_wire_format_stability(self):
        # Lock byte-for-byte format so we don't accidentally break
        # interop between the `spray` CLI sender and receiver.
        # !QBB encodes seq=1 as 8 big-endian bytes, plane=3 as 1 byte,
        # path=5 as 1 byte.
        buf = encode_payload(1, 3, 5)
        self.assertEqual(
            buf[:10],
            b"\x00\x00\x00\x00\x00\x00\x00\x01\x03\x05",
        )
        self.assertEqual(buf[10:], b"X" * 32)


class TestFlowEndpoint(unittest.TestCase):

    def test_to_flow_key_uses_inner_addrs(self):
        f = FlowEndpoint(tenant="green", src_id=0, dst_id=15)
        k = f.to_flow_key()
        self.assertEqual(k.src_addr, "2001:db8:bbbb:00::2")
        self.assertEqual(k.dst_addr, "2001:db8:bbbb:0f::2")
        self.assertEqual(k.src_port, SPRAY_PORT)
        self.assertEqual(k.dst_port, SPRAY_PORT)

    def test_to_flow_key_yellow(self):
        f = FlowEndpoint(tenant="yellow", src_id=3, dst_id=12,
                         src_port=1111, dst_port=2222)
        k = f.to_flow_key()
        self.assertEqual(k.src_addr, "2001:db8:cccc:03::2")
        self.assertEqual(k.dst_addr, "2001:db8:cccc:0c::2")
        self.assertEqual(k.src_port, 1111)
        self.assertEqual(k.dst_port, 2222)

    def test_frozen(self):
        f = FlowEndpoint("green", 0, 1)
        with self.assertRaises(Exception):
            f.src_id = 99  # type: ignore[misc]


class TestSenderResult(unittest.TestCase):

    def test_to_dict_shape(self):
        f = FlowEndpoint("green", 0, 15)
        r = SenderResult(flow=f, policy="round_robin",
                         rate_pps=100, duration_s=1.0, spine=0,
                         sent=400, elapsed_s=1.0023,
                         per_plane_sent={0: 100, 1: 100, 2: 100, 3: 100})
        d = r.to_dict()
        self.assertEqual(d["src"], "green-host00")
        self.assertEqual(d["dst"], "green-host15")
        self.assertEqual(d["tenant"], "green")
        self.assertEqual(d["policy"], "round_robin")
        self.assertEqual(d["spine"], 0)
        self.assertEqual(d["sent"], 400)
        self.assertEqual(d["elapsed_s"], 1.002)   # rounded to 3dp
        self.assertEqual(d["per_plane_sent"], {0: 100, 1: 100, 2: 100, 3: 100})
        # per_ev_sent always present in the dict shape, even when empty
        # (non-EV policies leave it as an empty mapping).
        self.assertEqual(d["per_ev_sent"], {})
        self.assertEqual(d["errors"], 0)

    def test_per_ev_sent_serializes_as_p_s_keys(self):
        # EV-aware runs populate per_ev_sent with (plane, spine) tuple
        # keys; to_dict serializes those as "P<p>:S<s>" strings for
        # JSON compatibility.
        f = FlowEndpoint("green", 0, 15)
        r = SenderResult(flow=f, policy="ev_spray",
                         rate_pps=100, duration_s=1.0, spine=0,
                         sent=4, elapsed_s=0.04,
                         per_plane_sent={0: 1, 1: 1, 2: 1, 3: 1},
                         per_ev_sent={(0, 0): 1, (1, 0): 1, (2, 0): 1, (3, 0): 1})
        d = r.to_dict()
        self.assertEqual(d["per_ev_sent"],
                         {"P0:S0": 1, "P1:S0": 1, "P2:S0": 1, "P3:S0": 1})

    def test_per_plane_sent_sorted_in_dict(self):
        f = FlowEndpoint("green", 0, 1)
        r = SenderResult(flow=f, policy="x", rate_pps=10, duration_s=0,
                         per_plane_sent={3: 1, 0: 1, 2: 1, 1: 1})
        keys = list(r.to_dict()["per_plane_sent"].keys())
        self.assertEqual(keys, [0, 1, 2, 3])


class TestHostFor(unittest.TestCase):

    def test_zero_padded_two_digits(self):
        self.assertEqual(host_for("green", 0), "green-host00")
        self.assertEqual(host_for("green", 9), "green-host09")
        self.assertEqual(host_for("green", 10), "green-host10")
        self.assertEqual(host_for("yellow", 15), "yellow-host15")


class TestDetectSelfId(unittest.TestCase):

    def test_valid_green(self):
        self.assertEqual(detect_self_id("green-host00"), ("green", 0))
        self.assertEqual(detect_self_id("green-host15"), ("green", 15))

    def test_valid_yellow(self):
        self.assertEqual(detect_self_id("yellow-host07"), ("yellow", 7))

    def test_invalid_tenant_rejected(self):
        with self.assertRaises(ValueError):
            detect_self_id("blue-host00")

    def test_missing_digits_rejected(self):
        with self.assertRaises(ValueError):
            detect_self_id("green-host0")     # only 1 digit

    def test_trailing_garbage_rejected(self):
        with self.assertRaises(ValueError):
            detect_self_id("green-host00.local")


class TestSnifferEgressFilter(unittest.TestCase):
    """Cover `_should_count_inner`, the helper that drops egress packets
    the receiver sniffer captures on this host's own NICs.

    Background: in all-to-all scenarios every host runs both a sender
    and a receiver. The receiver's per-NIC sniffer captures BOTH
    directions, so without this filter the receiver records this host's
    own outbound flows as if they were inbound, surfacing as 56 spurious
    "orphan flow" warnings on a 4p-4x8 yellow-all-to-all run.
    """

    def test_canon_zero_padded_matches_compressed(self):
        # topo.inner_addr() returns zero-padded ("...:01::2"); scapy
        # gives the RFC-5952 form ("...:1::2"). Both must canonicalize
        # to the same string.
        padded = inner_addr("yellow", 1)        # 2001:db8:cccc:01::2
        compressed = "2001:db8:cccc:1::2"       # what scapy emits
        self.assertEqual(_canon_inner(padded), _canon_inner(compressed))

    def test_canon_handles_garbage(self):
        # Falls back to the input on parse failure rather than raising.
        # Lets the sniffer drop bad packets via the != self_canon path.
        self.assertEqual(_canon_inner("not-an-addr"), "not-an-addr")

    def test_count_when_inner_dst_is_self(self):
        self_canon = _canon_inner(inner_addr("yellow", 1))
        # Inbound traffic: inner_dst is this host (RFC-5952 form from scapy).
        self.assertTrue(_should_count_inner("2001:db8:cccc:1::2", self_canon))

    def test_drop_when_inner_dst_is_other_host(self):
        # The all-to-all egress case: this host sends to another host;
        # sniffer captures the packet at NIC egress with inner_dst != self.
        self_canon = _canon_inner(inner_addr("yellow", 1))
        for other_id in (0, 2, 3, 7):
            with self.subTest(other_id=other_id):
                other = inner_addr("yellow", other_id)
                self.assertFalse(_should_count_inner(other, self_canon))

    def test_drop_when_inner_dst_is_other_tenant(self):
        # Cross-tenant packets (shouldn't happen, but defensive):
        # green and yellow inner addresses differ, so a green packet
        # captured on a yellow host's NIC is not for this host.
        self_canon = _canon_inner(inner_addr("yellow", 0))
        green0 = inner_addr("green", 0)
        self.assertFalse(_should_count_inner(green0, self_canon))


if __name__ == "__main__":
    unittest.main()
