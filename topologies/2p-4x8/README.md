# 2p-4x8 topology

Smaller variant: 2 planes × 4 spines × 8 leaves, 8 green + 8 yellow
hosts. Useful for faster iteration on resource-constrained hosts; the
container count is roughly 1/3 of `4p-8x16` (40 vs 128 total).

See `docs/design-fabric.md` for the general design; the address scheme
is the same as 4p-8x16 with the plane index running 0..1 and host IDs
running 0..7.

This directory contains:

- `topo.yaml` — declarative parameters (counts, image tag); source of
  truth consumed by the generator and srv6_mrc runtime.
- `topology.clab.yaml` — generated containerlab file. Regenerate via
  `make TOPO=2p-4x8 regen`.
- `config/` — generated per-node SONiC `config_db.json` + `frr.conf`.
  Regenerate via `make TOPO=2p-4x8 regen`.
- `routes/` — route-set YAMLs (`full-mesh.yaml` by default).
- `scenarios/` — MRC scenarios (`baseline.yaml`, `plane-loss.yaml`).

## Quickstart

```bash
make TOPO=2p-4x8 regen                # generate clab + SONiC configs
make image                            # one image (alpine-srv6-scapy:1.0)
                                      # serves every topology; topo.yaml is
                                      # bind-mounted at runtime
make TOPO=2p-4x8 deploy
make TOPO=2p-4x8 config
make TOPO=2p-4x8 host-routes
make TOPO=2p-4x8 scenario SCEN=yellow-baseline
```
