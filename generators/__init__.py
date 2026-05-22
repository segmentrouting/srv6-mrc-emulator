"""generators — build-time code that emits the lab artifacts.

Reads `topologies/<name>/topo.yaml` and emits `topology.clab.yaml` plus
per-node SONiC `config_db.json` + `frr.conf` into
`topologies/<name>/config/`.

These are committed artifacts: the deploy pipeline (`config.sh`,
`containerlab deploy`) consumes them directly. Regenerate via
`make regen-topology TOPO=4p-4x8` or
`python3 -m generators.fabric topologies/4p-4x8/topo.yaml`.
"""
