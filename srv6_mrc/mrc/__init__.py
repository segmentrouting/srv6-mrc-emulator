"""srv6_mrc.mrc — Multi-plane Reliable Connectivity behaviors.

Implements the MRC paper's spray-and-recover model on top of the static
SRv6 fabric: plane-aware spray policies, per-flow reorder measurement,
plane-health probing, and scenario orchestration.

See `docs/design-mrc.md` for the design rationale and
`docs/running.md` for how to drive scenarios end-to-end.
"""
