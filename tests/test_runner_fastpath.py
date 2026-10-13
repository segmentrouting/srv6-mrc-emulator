"""The data hot path's byte templates must match the scapy builder exactly.

runner._PacketTemplates patches seq/plane/path, the UDP checksum and, in
rdma mode, the BTH PSN and ICRC into per-EV templates instead of running
scapy per packet. Any drift from runner._build_packet_bytes would put
different bytes on the wire, so this compares the two across transports,
trimmed and full packets, tenants, EVs, uSID modes and sequence numbers
that wrap the 24-bit PSN.
"""
from __future__ import annotations

import unittest

try:
    import scapy.all  # type: ignore # noqa: F401
    _HAVE_SCAPY = True
except ImportError:  # pragma: no cover
    _HAVE_SCAPY = False

from srv6_mrc.runner import _build_packet_bytes, _PacketTemplates
from srv6_mrc.topo import NUM_PLANES, NUM_SPINES, inner_addr, usid_outer_dst

SEQS = (0, 1, 41, 2**24 - 1, 2**24 + 5, 2**40 + 3)


@unittest.skipUnless(_HAVE_SCAPY, "scapy not installed")
class PacketTemplateIdentityTests(unittest.TestCase):

    def _compare(self, *, tenant, transport, trimmed, sid_mode):
        src, dst = inner_addr(tenant, 2), inner_addr(tenant, 5)
        tpl = _PacketTemplates(src_inner=src, dst_inner=dst,
                               transport=transport)
        for plane in range(NUM_PLANES):
            for path in range(NUM_SPINES):
                da = usid_outer_dst(tenant, plane, path, 5,
                                    sid_mode=sid_mode)
                for seq in SEQS:
                    want = _build_packet_bytes(src, da, src, dst, seq,
                                               plane, path,
                                               transport=transport,
                                               trimmed=trimmed)
                    got = tpl.build(seq, plane, path, src_underlay=src,
                                    dst_outer=da, trimmed=trimmed)
                    self.assertEqual(
                        got, want,
                        f"{tenant} {transport} trimmed={trimmed} "
                        f"{sid_mode} ev=({plane},{path}) seq={seq}")

    def test_identical_to_scapy_builder(self):
        for tenant in ("green", "yellow"):
            for transport in ("udp", "rdma"):
                for trimmed in (False, True):
                    with self.subTest(tenant=tenant, transport=transport,
                                      trimmed=trimmed):
                        self._compare(tenant=tenant, transport=transport,
                                      trimmed=trimmed, sid_mode="uA")

    def test_identical_with_un_sids(self):
        self._compare(tenant="green", transport="rdma", trimmed=False,
                      sid_mode="uN")

    def test_template_is_reused_per_ev(self):
        src, dst = inner_addr("green", 2), inner_addr("green", 5)
        tpl = _PacketTemplates(src_inner=src, dst_inner=dst)
        da = usid_outer_dst("green", 0, 1, 5)
        for seq in range(10):
            tpl.build(seq, 0, 1, src_underlay=src, dst_outer=da)
        self.assertEqual(len(tpl._cache), 1)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
