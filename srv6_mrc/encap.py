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

import socket


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


__all__ = ["build_outer_packet", "open_raw_send_socket"]
