"""RoCEv2 (RDMA over Converged Ethernet v2) payload framing.

Wraps/unwraps the spray data path's existing payload
(`runner.encode_payload`) inside a real RoCEv2 BTH (Base Transport
Header), so `--transport rdma` traffic looks like genuine RDMA on the
wire — UDP dport=`topo.RDMA_PORT` (4791, the real IANA-assigned RoCEv2
port), a real opcode, an incrementing PSN — while MRC's own
`(seq, plane, path)` accounting, decoded by `runner.parse_payload`,
travels unchanged as the BTH's payload.

This is "Option A" from the RDMA feature design discussion: BTH is a
skin over the existing accounting struct, not a replacement for it.
`psn` and `dqpn` are populated from the same `(seq, plane, path)`
triple the struct already carries (`psn` = `seq` truncated to 24 bits;
`dqpn` = `topo.dqpn_for_ev(plane, path)`), so a future "Option B" (BTH
fields become the actual accounting mechanism) is a pure subtraction —
drop the now-redundant struct fields, read psn/dqpn instead — not a
wire-format change.

Transport modeled: UD (unreliable datagram) SEND_ONLY. UD is the right
fit for MRC-sprayed traffic — no ACK/NAK is expected per packet
(matches MRC's own out-of-band loss-report design), and UD's only
opcode family is SEND, so no RETH (remote addr/rkey/length, needed for
RC/UC WRITE/READ) is required.

ICRC: real RoCEv2 ICRC covers a pseudo-header of the packet's own
IPv6 + UDP headers (variable fields masked) plus BTH and payload. When
the caller passes the inner IPv6/UDP fields (`src`, `dst`, `sport`,
`dport`), the BTH is built inside that IPv6/UDP packet so scapy computes
the real ICRC, and the bytes after the UDP header are returned. Those
are the same inner IPv6/UDP headers `encap.build_outer_packet` puts in
front of them, so the ICRC on the wire is valid. Without those
arguments scapy cannot see the IP layer and writes an ICRC of 0.

Known simplification (confirmed via Wireshark, 2026-09-30): we don't
build a DETH (Datagram Extended Transport Header), which the real
RoCEv2 spec always requires immediately after a UD-transport BTH.
Wireshark's dissector doesn't know that and parses the next 8 bytes —
which are actually the leading bytes of the wrapped MRC payload struct
(`seq`) — as if they were a real DETH (q_key/srcqp). Cosmetic only;
nothing here or on the (nonexistent) receive side ever reads those
bytes as DETH fields. See docs/quickstart.md's "Inspecting RoCEv2
traffic" section for how to capture and decode this traffic.

Scapy is lazy-imported inside each function (see `encap.py`'s
docstring for why): this module must import cleanly on the
orchestrator side, which has no scapy installed.
"""

from __future__ import annotations

from typing import Optional

from .topo import dqpn_for_ev

# UD (unreliable datagram) SEND_ONLY. In scapy.contrib.roce's own
# encoding this is opcode('UD', 'SEND_ONLY') = _transports['UD'] (0x60)
# + _ops['SEND_ONLY'] (0x04); hardcoded here rather than importing
# those (underscore-prefixed, not a public scapy API).
RDMA_OPCODE = 0x64  # UD_SEND_ONLY


def wrap_rdma(payload: bytes, *, plane: int, path: int, seq: int,
              src: Optional[str] = None, dst: Optional[str] = None,
              sport: Optional[int] = None,
              dport: Optional[int] = None) -> bytes:
    """Wrap `payload` (runner.encode_payload's bytes) in a RoCEv2 BTH frame.

    `seq` becomes the BTH PSN, truncated to its 24-bit field (real PSNs
    wrap the same way over a long-running QP); `plane`/`path` become
    `dqpn` via `topo.dqpn_for_ev`. Returned bytes are ready to pass as
    the `payload=` argument to `encap.build_outer_packet(..., dport=
    topo.RDMA_PORT, ...)`.

    Pass the inner IPv6 `src`/`dst` and UDP `sport`/`dport` that
    `build_outer_packet` will use to get a valid ICRC (see module doc).
    """
    import logging as _logging
    _logging.getLogger("scapy.runtime").setLevel(_logging.ERROR)
    from scapy.contrib.roce import BTH  # type: ignore
    from scapy.packet import Raw  # type: ignore

    bth = BTH(
        opcode=RDMA_OPCODE,
        dqpn=dqpn_for_ev(plane, path),
        psn=seq & 0xFFFFFF,
    )
    if None in (src, dst, sport, dport):
        return bytes(bth / Raw(payload))
    from scapy.layers.inet import UDP  # type: ignore
    from scapy.layers.inet6 import IPv6  # type: ignore
    framed = bytes(IPv6(src=src, dst=dst) / UDP(sport=sport, dport=dport)
                   / bth / Raw(payload))
    return framed[_IPV6_UDP_LEN:]


_IPV6_UDP_LEN = 40 + 8


def unwrap_rdma(raw: bytes) -> Optional[bytes]:
    """Strip a RoCEv2 BTH frame, returning the inner payload bytes.

    Returns None if `raw` doesn't parse as a BTH-framed payload (too
    short, no trailing Raw layer) — the caller (`runner.run_receiver`)
    treats that the same as any other malformed packet: drop and keep
    counting.
    """
    import logging as _logging
    _logging.getLogger("scapy.runtime").setLevel(_logging.ERROR)
    from scapy.contrib.roce import BTH  # type: ignore
    from scapy.packet import Raw  # type: ignore

    try:
        pkt = BTH(raw)
    except Exception:
        return None
    if Raw not in pkt:
        return None
    return bytes(pkt[Raw])
