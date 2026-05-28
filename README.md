# srv6-mrc-emulator

A topology and traffic generator tool for simulating Multipath Reliable Connection
(MRC) over an SRv6 uSID dataplane. This tool is intended to give engineers 
and operators a feel for the multiplanar network design, uSID allocation scheme, host-based
SRv6 encapsulation and decapsulation operations, and the packet spraying patterns described in 
the work published by OpenAI, Microsoft, AMD, Broadcom, and Nvidia here:

https://cdn.openai.com/pdf/resilient-ai-supercomputer-networking-using-mrc-and-srv6.pdf


The simulator uses [containerlab](https://containerlab.dev/) to deploy a multiplane fabric of
dockerized SONiC-VS instances and SRv6 capable Alpine linux containers simulating hosts.

## Design Docs and Configs
[Fabric Design](./docs/fabric-design.md): describes the multi-planar design elements including IPv6 addressing and SRv6 uSID allocation scheme with pointers to SONiC config examples.

[SRv6-Multi-Tenant Design](./docs/srv6-multi-tenant-design.md): whitepaper outlining multi-tenant encap/decap models and security considerations.

[SONiC Configs](./topologies/4p-4x8/config/)

## Quickstart Guide

[Quickstart](./docs/quickstart.md): Guide to install, deploy and configure a topology, and run MRC-SRv6 traffic simulations


## Key MRC-SRv6 Emulator Elements

- **Containerlab topology**: 4-plane × 4-spine × 8-leaf *docker-sonic-vs* Clos carrying
  two tenants (`green`, `yellow`) with 8 Alpine container *hosts* each. 
- **config.sh shell script**: containerlab deploys the topology,  
  `scripts/config.sh` pushes the sonic nodes' config_db.json and FRR configs.
- **Userspace MRC traffic simulator** builds uSID-encapsulated
  UDP frames in scapy and sprays them into the fabric so each packet traces a
  distinct `(plane, path)` Entropy Value or EV. Accompanying MRC receiver computes per-flow
  reorder-distance histograms (the MRC / SRv6 paper's reorder metric)
  plus loss, latency, and PPS. The MRC control plane (probes + loss
  feedback) rides the same SRv6-encapped raw-socket path as the data
  packets, with per-`(plane, path)` EV granularity.
- **srctl command-line tool**: Kubectl-style CLI for managing the lab. Key commands:
  - `srctl get topology` — show fabric dimensions and tenants
  - `srctl get hosts [--tenant green|yellow]` — list hosts
  - `srctl get evs <src> <dst>` — show available EVs for a host pair
  - `srctl run <scenario>` — execute traffic simulations (baseline, all-to-all, all-reduce, etc.)
  - `srctl fault shutdown <node> <interface>` — inject interface or node failures
  - `srctl fault netem "<target>" "<spec>"` — inject loss/delay with tc netem
  - `srctl fault list` — show active faults
  - `srctl fault clear --all` — restore fabric state
- **Multi-tenancy with two SRv6 patterns.** Both tenants perform
  *host-encap*. The Green tenant is *leaf-decapped* (uDT6 into `Vrf-green` on
  every leaf; the destination is an anycast `2001:db8:bbbb:<NN>::2`
  configured on all 4 of the host's NICs) simulating multi-plane 
  breakout. The Yellow tenant receives SRv6 encapsulated traffic and 
  performs its own *host-decap* via linux `seg6local End.DT6` policies. 
  For more information see [Multi-Tenant Design Doc](./docs/design-multi-tenant.md)
- **Optional live visibility** - Grafana dashboard for real-time per-plane balance,
  MRC EV health, and fault injection effects. Enable by uncommenting `visibility.enabled`
  in `topo.yaml`. See [visibility/README.md](./visibility/README.md)

This repository also includes a topology 
generator tool so additional topologies can be added under `topologies/<name>/`.

For more detail see design docs under [docs/](./docs/)

## Layout

```
srv6_mrc/           Python package: topology constants, runtime libs
  topo.py              fabric dimensions + addressing helpers (reads topo.yaml)
  topology.py          typed Topology accessor (Refactor 1 in progress;
                       parallel to topo.py during migration)
  runner.py            spray sender/receiver core
  encap.py             shared raw-socket SRv6 outer-packet builder
                       (used by runner.py and mrc/transport.py)
  policy.py            per-plane / per-EV scheduling policies
                       (round_robin, hash5tuple, weighted, ev_spray,
                        health_aware_mrc)
  reorder.py           reorder-distance histogram + FlowStats schema
  netem.py             tc netem helpers (run via nsenter)
  report.py            JSON + ascii summary writer
  cli/
    spray.py           userspace SRv6 packet generator (CLI: `spray`)
    routes.py          static SRv6 route management   (CLI: `routes`)
    srctl.py           kubectl-shaped lab CLI         (CLI: `srctl`)
  mrc/
    run.py             scenario orchestrator           (CLI: `run-scenario`)
    daemon.py          per-host MRC daemon: single SO_REUSEPORT reply-
                       socket owner + per-flow snapshot writer
    scenario.py        scenario YAML schema + executor
    agent.py           SenderMrcAgent + ReceiverMrcAgent
    transport.py       MrcTransport ABC + Srv6RawTransport +
                       LoopbackUdpTransport
    ev_state.py        per-(tenant, plane, path) EV state machine
    probe.py           PROBE / PROBE_REPLY / LOSS_REPORT wire format
    probe_clock.py     per-EV in-flight probe bookkeeping
    loss_window.py     per-EV receiver-side loss accounting
    loss_compute.py    per-EV SentWindowRing on the sender

generators/
  fabric.py            parameterized generator: reads topo.yaml,
                       writes topology.clab.yaml + config/

topologies/
  4p-4x8/              4 planes × 4 spines × 8 leaves x 16 hosts (8 Green, 8 Yellow) - default topology
  4p-8x16/             4 planes × 8 spines × 16 leaves x 32 hosts (16 Green, 16 Yellow)
  2p-4x8/              2 planes × 4 spines × 8 leaves  x 16 hosts
    topo.yaml          single source of truth for this variant
    topology.clab.yaml containerlab topology (generated)
    config/            per-node SONiC + FRR configs   (generated)
    scenarios/         MRC traffic scenario YAMLs
    routes/            route-spec YAMLs for `routes apply`
    README.md          per-topology design notes

host-image/
  Dockerfile           alpine + scapy + pip-installed srv6_mrc

scripts/
  config.sh            push config_db.json + frr.conf into containers

tests/                 unittest mirror of srv6_mrc/ layout
docs/                  consolidated design + runbook documentation
results/               scenario JSON output (gitignored)
```

## Requires
- containerlab
- docker-sonic-vs: tested with Branch Master docker-sonic-vs.gz from [SONiC Dowloads](https://sonic.software/) site.
- Alpine SRv6 docker image *`bmcdougall/alpine-srv6-scapy:1.0`*
- at least 16 vCPU and 32GB of memory (tested with 32 vCPU and 96GB memory)

## License

Apache-2.0. See [LICENSE](./LICENSE)
