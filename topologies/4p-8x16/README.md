# 4p-8x16 topology

Static SRv6 fabric with 4 planes × 8 spines × 16 leaves, 16 green + 16
yellow hosts. See `docs/design-fabric.md` for the address scheme,
SID-list shape, and general design.

This directory contains:

- `topo.yaml` — declarative parameters (counts, address blocks); source
  of truth consumed by the generator and srv6_mrc runtime.
- `topology.clab.yaml` — generated containerlab file. Regenerate via
  `make TOPO=4p-8x16 regen`.
- `config/` — generated per-node SONiC `config_db.json` + `frr.conf`.
  Regenerate via `make TOPO=4p-8x16 regen`. Committed.
- `routes/` — route-set YAMLs (input to `routes` CLI).
- `scenarios/` — MRC scenarios for this topology.
