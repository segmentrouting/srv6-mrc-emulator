"""The receiver's fixed-offset frame parser must read what scapy read.

run_receiver used to dissect every captured frame with scapy (~70 us
each), which capped one receiver at ~3,000 pps. It now parses the fixed
headers itself (runner._parse_frame) and slices RoCE framing
(runner._unwrap_bth). These tests compare both against the scapy
dissection the old handler performed, over every frame shape the lab
produces plus the ones it must ignore.
"""
from __future__ import annotations

import struct
import unittest

try:
    from scapy.all import IP, UDP, Ether, IPv6, Padding, raw  # type: ignore
    _HAVE_SCAPY = True
except ImportError:  # pragma: no cover
    _HAVE_SCAPY = False

from srv6_mrc.runner import (
    _build_packet_bytes, _parse_frame, _unwrap_bth, encode_payload,
)
from srv6_mrc.topo import RDMA_PORT, SPRAY_PORT, inner_addr, usid_outer_dst


def _scapy_reference(frame: bytes):
    """What the old scapy-based handler extracted from a frame.

    One deliberate difference: scapy keeps Ethernet padding (frames under
    the 60-byte minimum) as part of the UDP payload; _parse_frame bounds
    the packet by the IPv6 payload length, so the reference strips it
    too. Lab data frames are always above the minimum.
    """
    try:
        pkt = Ether(frame)
    except Exception:
        return None
    if IPv6 not in pkt:
        return None
    outer = pkt[IPv6]
    if outer.nh == 41:
        inner = outer.payload
        if not isinstance(inner, IPv6) or UDP not in inner:
            return None
        udp, src, dst = inner[UDP], inner.src, inner.dst
    else:
        if UDP not in pkt:
            return None
        udp, src, dst = pkt[UDP], outer.src, outer.dst
    payload = bytes(udp.payload)
    if Padding in udp:
        payload = payload[:-len(udp[Padding].load)]
    return src, dst, int(udp.sport), int(udp.dport), payload


def _eth(l3: bytes, *, vlan: bool = False) -> bytes:
    """Ethernet header (optionally 802.1Q-tagged) + an IPv6 packet."""
    hdr = bytes.fromhex("0242ac120001" "aac1ab000001")
    if vlan:
        hdr += struct.pack("!HH", 0x8100, 10)
    return hdr + struct.pack("!H", 0x86DD) + l3


@unittest.skipUnless(_HAVE_SCAPY, "scapy not installed")
class ParseFrameEquivalenceTests(unittest.TestCase):

    def _frames(self):
        out = []
        for tenant in ("green", "yellow"):
            src, dst = inner_addr(tenant, 2), inner_addr(tenant, 5)
            for transport in ("udp", "rdma"):
                for trimmed in (False, True):
                    pkt = _build_packet_bytes(
                        src, usid_outer_dst(tenant, 1, 2, 5), src, dst,
                        41, 1, 2, transport=transport, trimmed=trimmed)
                    out.append((f"{tenant} encap {transport} trim={trimmed}",
                                _eth(pkt)))
                    # green after leaf decap: plain inner IPv6/UDP
                    out.append((f"{tenant} decapped {transport} "
                                f"trim={trimmed}", _eth(pkt[40:])))
            out.append((f"{tenant} vlan-tagged",
                        _eth(_build_packet_bytes(
                            src, usid_outer_dst(tenant, 0, 0, 5), src, dst,
                            7, 0, 0), vlan=True)))
        # padded to the Ethernet minimum (short frame + trailer bytes)
        small = raw(IPv6(src="2001:db8::1", dst="2001:db8::2")
                    / UDP(sport=SPRAY_PORT, dport=SPRAY_PORT) / b"x")
        out.append(("short frame with padding", _eth(small) + b"\x00" * 20))
        out.append(("non-spray UDP",
                    _eth(raw(IPv6() / UDP(sport=1, dport=53) / b"q"))))
        out.append(("IPv6 non-UDP", _eth(raw(IPv6(nh=59)))))
        out.append(("IPv4", raw(Ether() / IP() / UDP(dport=SPRAY_PORT))))
        out.append(("truncated IPv6", _eth(raw(IPv6())[:20])))
        out.append(("too short", b"\x00" * 10))
        return out

    def test_matches_scapy_dissection(self):
        for name, frame in self._frames():
            with self.subTest(frame=name):
                self.assertEqual(_parse_frame(frame), _scapy_reference(frame))

    def test_bth_unwrap_matches_rdma_module(self):
        from srv6_mrc.rdma import unwrap_rdma, wrap_rdma
        for body in (encode_payload(41, 1, 2),
                     struct.pack("!QBB", 41, 1, 2)):  # full, trimmed
            framed = wrap_rdma(body, plane=1, path=2, seq=41,
                               src="2001:db8::1", dst="2001:db8::2",
                               sport=SPRAY_PORT, dport=RDMA_PORT)
            self.assertEqual(_unwrap_bth(framed), unwrap_rdma(framed))
            self.assertEqual(_unwrap_bth(framed), body)
        self.assertIsNone(_unwrap_bth(b"\x00" * 16))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
