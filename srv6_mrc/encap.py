"""Shared SRv6 encap helpers for the spray data path and the MRC probe path.

Both the spray runner (`runner.py`) and the MRC sender/receiver agent
(`mrc/agent.py`) need to put bytes on the wire that look like:

    [outer IPv6 nh=41, dst=usid_outer_dst(...)]
       └── [inner IPv6, dst=inner anycast]
              └── [UDP sport=X, dport=Y]
                     └── [application payload bytes]

The outer IPv6 carries the per-EV uSID (`fc00:000<P>:f00<S>:e00<L>:...`)
so the fabric forwards the packet through the chosen `(plane, spine)`
on its way to the destination leaf (green) or destination host (yellow).
The inner header uses the plane-independent tenant anycast as src/dst.

This module provides:

  build_outer_packet()
      Compose the full outer/inner/UDP bytes from precomputed addresses
      and a caller-supplied UDP payload. Lazy-imports scapy so the
      orchestrator side (no scapy) can import callers without errors.
      The data path passes the spray data payload (encode_payload());
      the MRC probe path passes encode_probe() / encode_probe_reply() /
      encode_loss_report() output.

  open_raw_send_socket()
      Create one AF_INET6 SOCK_RAW IPPROTO_RAW socket, SO_BINDTODEVICE
      to the given NIC. Per AGENTS.md invariant 8, plane selection
      MUST be NIC-bound. The runner opens NUM_PLANES of these up-front;
      the MRC agent does the same so probe traffic is steered through
      the same plane attribution as data traffic.

Why share rather than re-implement
----------------------------------
Until phase 1b step 2 commit 5, the MRC sender used plain UDP-over-
IPv6 sockets with SO_BINDTODEVICE for plane attribution and relied on
the kernel to deliver probes to the receiver's inner anycast. That
worked accidentally for green's leaf-decap End.DT6 setup at
paths_per_plane=1, and broke silently the moment we wanted to steer
probes through a specific spine (no kernel route for the inner-anycast
addr that picks a specific spine). The fix is to follow the data path
exactly — sender-built outer SRv6 — and the cleanest way to do that
is to share this builder between runner.py and agent.py.

This module is intentionally tiny and dependency-light; everything
specific to the data payload or probe wire format lives in the caller.
"""

from __future__ import annotations

import ipaddress
import socket
import struct


def build_outer_packet(
    *,
    src_underlay: str,
    dst_outer: str,
    src_inner: str,
    dst_inner: str,
    sport: int,
    dport: int,
    payload: bytes,
) -> bytes:
    """Build full outer-IPv6 / inner-IPv6 / UDP / payload bytes.

    Scapy is lazy-imported so this module's top-level import is free of
    scapy noise; the orchestrator side never calls into this function.

    The outer header sets `nh=41` (IPv6-in-IPv6 encap) so the fabric's
    SRv6 forwarding treats the inner IPv6 packet as the encapsulated
    inner per encap.red — there is intentionally NO SRH on the wire
    (see AGENTS.md invariant: "never say SRH as shorthand"). uSID
    progression happens via destination-address rewrite on the outer
    header alone.

    Args:
        src_underlay: per-plane sender underlay addr (host's eth(P+1)
            global); used as the outer src.
        dst_outer: SRv6 outer destination uSID, the
            `usid_outer_dst(tenant, plane, spine, dst_leaf)` value for
            the chosen EV. For yellow this is the 2-uSID form
            (`f<S>:e009:d001::`); for green it is the 1-uSID form
            (`f<S>:e00<L>:d000::`).
        src_inner: sender's plane-independent inner anycast.
        dst_inner: receiver's plane-independent inner anycast.
        sport / dport: inner UDP src/dst ports. The data path uses
            SPRAY_PORT for both; the MRC probe/report paths use
            SPRAY_PROBE_PORT / SPRAY_REPORT_PORT.
        payload: application-layer bytes (data payload, PROBE,
            PROBE_REPLY, or LOSS_REPORT).

    Returns:
        Wire bytes ready to feed `sendto(pkt, (dst_outer, 0, 0, 0))`
        on a raw IPv6 socket bound to the plane's NIC.
    """
    import logging as _logging
    _logging.getLogger("scapy.runtime").setLevel(_logging.ERROR)
    from scapy.all import IPv6, UDP  # type: ignore

    inner = (
        IPv6(src=src_inner, dst=dst_inner)
        / UDP(sport=sport, dport=dport)
        / payload
    )
    outer = IPv6(src=src_underlay, dst=dst_outer, nh=41) / inner
    return bytes(outer)


def open_raw_send_socket(iface: str) -> socket.socket:
    """Raw IPv6 socket bound to a single NIC via SO_BINDTODEVICE.

    Per AGENTS.md invariant 8 — plane selection MUST be NIC-bound, not
    route-metric-bound. Kernel ECMP would defeat plane spray since both
    tenants' inner dst is anycast across every plane.

    Raises PermissionError with a helpful message if the caller lacks
    CAP_NET_RAW (e.g. running tests on the laptop outside the alpine
    host containers).
    """
    s = socket.socket(socket.AF_INET6, socket.SOCK_RAW, socket.IPPROTO_RAW)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, iface.encode())
    except PermissionError as e:
        raise PermissionError(
            f"SO_BINDTODEVICE on {iface} needs CAP_NET_RAW. "
            "Run inside the alpine host containers or as root."
        ) from e
    return s


__all__ = [
    "build_outer_packet",
    "open_raw_send_socket",
    "build_outer_template",
    "udp6_checksum_inplace",
    "OUTER_HEADER_LEN",
    "INNER_HEADER_LEN",
    "UDP_HEADER_LEN",
    "UDP_CSUM_OFFSET",
    "PAYLOAD_OFFSET",
]


# --- byte-level constants (RFC 8200 IPv6 + RFC 768 UDP) --------------------
#
# Layout of the bytes produced by `build_outer_packet`, used by the
# fast-path reply builder in `mrc/transport.py` to splice payload +
# checksum into a pre-built template without re-invoking scapy. These
# offsets are NOT scapy-specific; they're the wire layout per RFC.
# Verified against scapy output by `tests/test_encap_template.py`.

OUTER_HEADER_LEN = 40      # outer IPv6 header
INNER_HEADER_LEN = 40      # inner IPv6 header (nh=41 IPv6-in-IPv6)
UDP_HEADER_LEN = 8         # UDP header

# Offset of the UDP checksum field inside the full buffer:
# 40 (outer IPv6) + 40 (inner IPv6) + 6 (UDP header offset of csum)
UDP_CSUM_OFFSET = OUTER_HEADER_LEN + INNER_HEADER_LEN + 6  # = 86

# Offset where the UDP payload begins:
# 40 (outer IPv6) + 40 (inner IPv6) + 8 (UDP header)
PAYLOAD_OFFSET = OUTER_HEADER_LEN + INNER_HEADER_LEN + UDP_HEADER_LEN  # = 88

# Offset of the inner-IPv6 src/dst (needed for the UDP-checksum
# pseudo-header). Inner header starts at OUTER_HEADER_LEN (40); src is
# bytes 8..24 of the IPv6 header, dst is 24..40.
INNER_SRC_OFFSET = OUTER_HEADER_LEN + 8   # = 48
INNER_DST_OFFSET = OUTER_HEADER_LEN + 24  # = 64


def build_outer_template(
    *,
    src_underlay: str,
    dst_outer: str,
    src_inner: str,
    dst_inner: str,
    sport: int,
    dport: int,
    payload_len: int,
) -> bytearray:
    """Build a `bytearray` template with zero-filled payload and zero UDP csum.

    The hot path (receiver's `_probe_rx_loop`) takes this template,
    splices in the actual `payload_len`-byte probe-reply payload at
    `PAYLOAD_OFFSET`, recomputes the UDP checksum via
    `udp6_checksum_inplace`, and writes the result to the plane's raw
    socket — skipping scapy entirely. Per-reply cost drops from ~2-3 ms
    (scapy build) to ~10-20 µs.

    This builder uses scapy under the hood (same as `build_outer_packet`)
    so the result is byte-identical to what `build_outer_packet` would
    produce with an all-zero payload of length `payload_len`. The
    `tests/test_encap_template.py::test_template_bytes_match_scapy_build`
    regression rail pins this equivalence.

    Returns:
        A mutable `bytearray` of length
        `OUTER_HEADER_LEN + INNER_HEADER_LEN + UDP_HEADER_LEN + payload_len`
        with the payload region zeroed and the UDP checksum field set to
        zero. Caller mutates `[PAYLOAD_OFFSET:PAYLOAD_OFFSET+payload_len]`
        to splice in real bytes, then calls `udp6_checksum_inplace` to
        fix up the checksum, then `sendto`s the result.
    """
    if payload_len < 0:
        raise ValueError(f"payload_len must be >= 0, got {payload_len}")
    template_bytes = build_outer_packet(
        src_underlay=src_underlay,
        dst_outer=dst_outer,
        src_inner=src_inner,
        dst_inner=dst_inner,
        sport=sport, dport=dport,
        payload=b"\x00" * payload_len,
    )
    expected_len = (
        OUTER_HEADER_LEN + INNER_HEADER_LEN + UDP_HEADER_LEN + payload_len
    )
    if len(template_bytes) != expected_len:
        raise RuntimeError(
            f"build_outer_template internal error: expected "
            f"{expected_len} bytes, got {len(template_bytes)}"
        )
    buf = bytearray(template_bytes)
    # Clear the checksum field — caller MUST recompute via
    # udp6_checksum_inplace after splicing the real payload.
    buf[UDP_CSUM_OFFSET:UDP_CSUM_OFFSET + 2] = b"\x00\x00"
    return buf


def udp6_checksum_inplace(buf: bytearray, *, payload_len: int) -> None:
    """Compute IPv6 UDP checksum for `buf` and write it in place.

    `buf` must be a `bytearray` produced by `build_outer_template` (or
    structurally identical: 40B outer IPv6, 40B inner IPv6 with nh=17,
    8B UDP header, then `payload_len` bytes of UDP payload). The UDP
    checksum field at `UDP_CSUM_OFFSET..UDP_CSUM_OFFSET+2` is assumed
    to be zero before this call; we don't enforce it because we
    overwrite anyway.

    Per RFC 2460 §8.1, the IPv6 UDP checksum is the one's-complement
    sum over the IPv6 pseudo-header + UDP header (with the checksum
    field set to zero) + UDP payload. The pseudo-header is:

        src (16) + dst (16) + length (4) + zeros (3) + next-header (1)

    Where src/dst are the inner-IPv6 addresses (the inner header is
    what carries the UDP, not the outer SRv6 carrier). UDP RFC says
    "if the computed checksum is zero, transmit it as all-ones (0xFFFF)"
    so we apply that fixup before writing.
    """
    # IPv6 pseudo-header
    src = bytes(buf[INNER_SRC_OFFSET:INNER_SRC_OFFSET + 16])
    dst = bytes(buf[INNER_DST_OFFSET:INNER_DST_OFFSET + 16])
    udp_len = UDP_HEADER_LEN + payload_len
    # length is a u32 in the pseudo-header (big-endian); next-header u8
    pseudo = src + dst + struct.pack("!I", udp_len) + b"\x00\x00\x00\x11"

    # UDP header + payload (checksum field already zero)
    udp_region = bytes(
        buf[OUTER_HEADER_LEN + INNER_HEADER_LEN:
            OUTER_HEADER_LEN + INNER_HEADER_LEN + udp_len]
    )

    # One's-complement sum over pseudo + udp_region, treated as
    # 16-bit big-endian words. Odd byte at end (if any) is padded
    # with a trailing zero — pseudo is 40 bytes (even), udp_region
    # is udp_len bytes and udp_len is always even here (8 + 28 = 36
    # for probe replies), so no padding is needed for our hot path.
    # We handle the odd case anyway for robustness.
    blob = pseudo + udp_region
    if len(blob) & 1:
        blob = blob + b"\x00"
    s = 0
    # unpack words for speed
    for (word,) in struct.iter_unpack("!H", blob):
        s += word
        s = (s & 0xFFFF) + (s >> 16)
    csum = (~s) & 0xFFFF
    if csum == 0:
        csum = 0xFFFF
    buf[UDP_CSUM_OFFSET:UDP_CSUM_OFFSET + 2] = struct.pack("!H", csum)
