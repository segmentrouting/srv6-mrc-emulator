"""MRC probe / loss-report packet encode + decode.

Two packet types, both carried as UDP/IPv6 payloads. The IPv6 outer is
built by the runner exactly the same way as for spray data — this
module only knows about the bytes after the UDP header.

| Packet        | UDP dport            | Encoder           | Decoder           |
|---------------|----------------------|-------------------|-------------------|
| PROBE         | SPRAY_PROBE_PORT     | encode_probe      | decode_probe      |
| LOSS_REPORT   | SPRAY_REPORT_PORT    | encode_loss_report| decode_loss_report|

Stateless probes (v4): there is no PROBE_REPLY. A probe round-trips
back to the sender via a 6-slot uSID list ending in `End.DT6` on the
sender's own leaf; the returning probe is byte-identical (modulo hop
limit) to the outbound packet and lands on the daemon's recv socket
at the sender. The daemon attributes it to the originating EV via
the inner-dst (per-EV /128) and to the peer host via the `dst_id`
payload byte. No req_id / tx_ns / svc_time / match table is needed.

See `docs/stateless-probes-validation.md` for the lab walkthrough.

Wire format (all big-endian / network byte order):

  PROBE (v4):
      magic       u8   = 0xA5
      version     u8   = 4
      plane_id    u8
      path_id     u8
      tenant_id   u16   (sender's tenant per topo.TENANT_ID; 0 = unknown)
      src_id      u16   (sender's host id)
      dst_id      u8    (peer host id the probe was directed at)
      _reserved   u8    = 0

  LOSS_REPORT (v2, unchanged from prior versions):
      magic       u8   = 0xA7
      version     u8   = 2
      window_id   u16   (monotonically increasing per (sender, receiver))
      num_records u16
      _reserved   u16   = 0
      then num_records × per-EV records:
          plane_id    u8
          path_id     u8
          _reserved   u16 = 0
          seen        u32
          expected    u32
          max_gap     u32

The per-message magic byte is preserved so a recv socket that sees
both probe and loss-report traffic can demux defensively. PROBE v4 is
NOT backward-compatible with v3 — v3 is rejected by the decoder;
this is a lab tool with no in-flight upgrades, lockstep is free.

LOSS_REPORT v2 is unchanged.

The codecs are pure functions — no scapy, no sockets, no clocks. Same
test discipline as srv6_mrc/policy.py.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass


# --- magic / versions ------------------------------------------------------

PROBE_VERSION = 4
LOSS_REPORT_VERSION = 2

_MAGIC_PROBE = 0xA5
_MAGIC_LOSS_REPORT = 0xA7


# struct format strings (network byte order)
# PROBE v4: magic, version, plane_id, path_id, tenant_id, src_id,
#           dst_id, _reserved
_PROBE_FMT = "!BBBBHHBB"
_PROBE_SIZE = struct.calcsize(_PROBE_FMT)   # 10 bytes

# Public alias for byte-template consumers (e.g. Srv6RawTransport's
# probe-emit fast path). The returning probe is byte-identical to the
# outbound so this is also the inbound payload length.
PROBE_PAYLOAD_LEN = _PROBE_SIZE

# LOSS_REPORT header: magic, version, window_id, num_records, _rsv
_LOSS_HDR_FMT = "!BBHHH"
_LOSS_HDR_SIZE = struct.calcsize(_LOSS_HDR_FMT)  # 8 bytes
# per-EV record (v2): plane_id, path_id, _rsv16, seen, expected, max_gap
_LOSS_REC_FMT = "!BBHIII"
_LOSS_REC_SIZE = struct.calcsize(_LOSS_REC_FMT)  # 16 bytes


# --- exceptions ------------------------------------------------------------

class ProbeDecodeError(ValueError):
    """Raised when a packet doesn't conform to the expected layout."""


# --- dataclasses for decoded payloads --------------------------------------

@dataclass(frozen=True)
class Probe:
    """A decoded PROBE packet (v4, stateless).

    `dst_id` is the peer host the probe was directed at; the daemon
    uses it to demux the returning probe to the right per-flow
    EVStateTable. `(plane_id, path_id)` identifies the EV; both are
    also recoverable from the inner-dst /128, so the on-wire copy
    serves as a self-consistency check rather than the primary signal.
    """
    plane_id: int
    path_id: int
    tenant_id: int
    src_id: int
    dst_id: int

    def __post_init__(self) -> None:
        _check_u8(self.plane_id, "plane_id")
        _check_u8(self.path_id, "path_id")
        _check_u16(self.tenant_id, "tenant_id")
        _check_u16(self.src_id, "src_id")
        _check_u8(self.dst_id, "dst_id")


@dataclass(frozen=True)
class PlaneLossRecord:
    """One per-EV entry inside a LOSS_REPORT.

    Named PlaneLossRecord for back-compat; v2 widens the key from plane
    to (plane, path). A v1 record had path_id implicitly zero; this
    type now carries it explicitly. Consumers that only care about the
    plane axis can still aggregate by `plane_id` and ignore `path_id`.
    """
    plane_id: int
    path_id: int
    seen: int
    expected: int
    max_gap: int

    def __post_init__(self) -> None:
        _check_u8(self.plane_id, "plane_id")
        _check_u8(self.path_id, "path_id")
        _check_u32(self.seen, "seen")
        _check_u32(self.expected, "expected")
        _check_u32(self.max_gap, "max_gap")


@dataclass(frozen=True)
class LossReport:
    """A decoded LOSS_REPORT packet.

    `planes` (historical name; really "EV records") is in wire order;
    receivers should treat duplicate (plane_id, path_id) entries as
    latest-wins (we don't enforce uniqueness on decode).
    """
    window_id: int
    planes: tuple[PlaneLossRecord, ...]

    def __post_init__(self) -> None:
        _check_u16(self.window_id, "window_id")
        if not isinstance(self.planes, tuple):
            raise TypeError("planes must be a tuple")


# --- encoders --------------------------------------------------------------

def encode_probe(
    plane_id: int, path_id: int,
    *, tenant_id: int, src_id: int, dst_id: int,
) -> bytes:
    """Build the UDP-payload bytes for a PROBE packet (v4, stateless).

    `(plane_id, path_id)` identifies the EV the probe will traverse.
    `dst_id` is the peer host_id that forwards the probe back via
    its leaf (no userland involvement on the peer).

    `tenant_id` / `src_id` identify the sender for diagnostics and
    for the daemon's per-flow EVStateTable lookup on the returning
    probe.
    """
    _check_u8(plane_id, "plane_id")
    _check_u8(path_id, "path_id")
    _check_u16(tenant_id, "tenant_id")
    _check_u16(src_id, "src_id")
    _check_u8(dst_id, "dst_id")
    return struct.pack(
        _PROBE_FMT,
        _MAGIC_PROBE, PROBE_VERSION,
        plane_id, path_id,
        tenant_id, src_id,
        dst_id, 0,
    )


def encode_loss_report(
    window_id: int,
    planes: list[PlaneLossRecord] | tuple[PlaneLossRecord, ...],
) -> bytes:
    """Build the UDP-payload bytes for a LOSS_REPORT packet.

    Empty `planes` is allowed (an empty report still carries a
    window_id, so the sender can confirm the receiver is alive).
    """
    _check_u16(window_id, "window_id")
    if len(planes) > 0xFFFF:
        raise ValueError(f"too many records: {len(planes)} > 65535")
    out = bytearray(
        struct.pack(
            _LOSS_HDR_FMT,
            _MAGIC_LOSS_REPORT, LOSS_REPORT_VERSION,
            window_id, len(planes), 0,
        )
    )
    for rec in planes:
        if not isinstance(rec, PlaneLossRecord):
            raise TypeError(
                f"loss report entries must be PlaneLossRecord, got {type(rec)}"
            )
        out += struct.pack(
            _LOSS_REC_FMT,
            rec.plane_id, rec.path_id, 0,
            rec.seen, rec.expected, rec.max_gap,
        )
    return bytes(out)


# --- decoders --------------------------------------------------------------

def decode_probe(payload: bytes) -> Probe:
    if len(payload) < _PROBE_SIZE:
        raise ProbeDecodeError(
            f"probe payload too short: {len(payload)} < {_PROBE_SIZE}"
        )
    (magic, version, plane_id, path_id,
     tenant_id, src_id, dst_id, _rsv) = struct.unpack(
         _PROBE_FMT, payload[:_PROBE_SIZE],
     )
    if magic != _MAGIC_PROBE:
        raise ProbeDecodeError(
            f"expected probe magic 0x{_MAGIC_PROBE:02x}, got 0x{magic:02x}"
        )
    if version != PROBE_VERSION:
        raise ProbeDecodeError(
            f"unsupported probe protocol version {version}"
        )
    return Probe(
        plane_id=plane_id, path_id=path_id,
        tenant_id=tenant_id, src_id=src_id, dst_id=dst_id,
    )


def decode_loss_report(payload: bytes) -> LossReport:
    if len(payload) < _LOSS_HDR_SIZE:
        raise ProbeDecodeError(
            f"loss_report payload too short: {len(payload)} < {_LOSS_HDR_SIZE}"
        )
    magic, version, window_id, num_records, _rsv = struct.unpack(
        _LOSS_HDR_FMT, payload[:_LOSS_HDR_SIZE],
    )
    if magic != _MAGIC_LOSS_REPORT:
        raise ProbeDecodeError(
            f"expected LOSS_REPORT magic 0x{_MAGIC_LOSS_REPORT:02x}, "
            f"got 0x{magic:02x}"
        )
    if version != LOSS_REPORT_VERSION:
        raise ProbeDecodeError(
            f"unsupported loss-report protocol version {version}"
        )
    expected_len = _LOSS_HDR_SIZE + num_records * _LOSS_REC_SIZE
    if len(payload) < expected_len:
        raise ProbeDecodeError(
            f"loss_report truncated: got {len(payload)}B, "
            f"expected {expected_len}B for {num_records} records"
        )
    planes: list[PlaneLossRecord] = []
    off = _LOSS_HDR_SIZE
    for _ in range(num_records):
        plane_id, path_id, _r16, seen, expected, max_gap = struct.unpack(
            _LOSS_REC_FMT, payload[off:off + _LOSS_REC_SIZE],
        )
        planes.append(PlaneLossRecord(
            plane_id=plane_id, path_id=path_id,
            seen=seen, expected=expected, max_gap=max_gap,
        ))
        off += _LOSS_REC_SIZE
    return LossReport(window_id=window_id, planes=tuple(planes))


# --- range checks ----------------------------------------------------------

def _check_u8(v: int, name: str) -> None:
    if not isinstance(v, int) or v < 0 or v > 0xFF:
        raise ValueError(f"{name} must be uint8, got {v!r}")


def _check_u16(v: int, name: str) -> None:
    if not isinstance(v, int) or v < 0 or v > 0xFFFF:
        raise ValueError(f"{name} must be uint16, got {v!r}")


def _check_u32(v: int, name: str) -> None:
    if not isinstance(v, int) or v < 0 or v > 0xFFFFFFFF:
        raise ValueError(f"{name} must be uint32, got {v!r}")


# --- module-level constants for consumers ----------------------------------

__all__ = [
    "PROBE_VERSION", "LOSS_REPORT_VERSION",
    "PROBE_PAYLOAD_LEN",
    "Probe", "PlaneLossRecord", "LossReport",
    "encode_probe", "encode_loss_report",
    "decode_probe", "decode_loss_report",
    "ProbeDecodeError",
]
