# srv6-mrc-emulator

A research-grade simulator for Multipath Reliable Connection
(MRC) layered on top of a static SRv6 uSID dataplane. The simulator uses 
[containerlab](https://containerlab.dev/) to deploy a multiplane fabric of
dockerized SONiC-VS instances and SRv6 capable Alpine linux containers 
simulating hosts.

The reference topology is a **4-plane × 8-spine × 16-leaf Clos** carrying
two tenants (`green`, `yellow`). This repository also includes a topology 
generator tool so additional topologies can be added under `topologies/<name>/`.

## Key Elements

- **Pure-static control plane**: No BGP, no IGP. Every leaf carries its
  own SRv6 locator + transit SIDs as `static-sids` in FRR
- **config.sh shell script**: containerlab deploys the topology,  
  `scripts/config.sh` pushes the sonic nodes' config_db.json and FRR configs.
- **Userspace MRC sender.** `spray` builds per-plane uSID-encapsulated
  UDP frames in scapy, varies the spine per packet under the
  `ev_spray` / `health_aware_mrc` policies so each packet traces a
  distinct `(plane, path)` EV, and the receiver computes per-flow
  reorder-distance histograms (the MRC / SRv6 paper's reorder metric)
  plus loss, latency, and PPS. The MRC control plane (probes + loss
  feedback) rides the same SRv6-encapped raw-socket path as the data
  packets, with per-`(plane, path)` granularity.
- **Fault injection.** Scenarios under `topologies/<name>/scenarios/`
  drive `tc netem` against host veths via `nsenter`, exercising
  plane-loss, plane-latency, and plane-blackhole failure modes. 
  Alternatively the user can simply shutdown fabric interfaces with 
  `docker exec -it <nodename> config interface shutdown <interface>`
- **Multi-tenancy with two SRv6 patterns.** Both tenants perform
  host-encap. Green is *leaf-decapped* (uDT6 into `Vrf-green` on
  every leaf; the destination is an anycast `2001:db8:bbbb:<NN>::2`
  configured on all 4 of the host's NICs). Yellow is *host-decapped*
  via per-NIC `seg6local End.DT6 table 0` policies on the destination
  host. 

For more detail see design docs under [docs/](./docs/)

## Layout

```
srv6_mrc/           Python package: topology constants, runtime libs
  topo.py              fabric dimensions + addressing helpers (reads topo.yaml)
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
  mrc/
    run.py             scenario orchestrator           (CLI: `run-scenario`)
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
  4p-8x16/             4 planes × 8 spines × 16 leaves (default)
  2p-4x8/              2 planes × 4 spines × 8 leaves  (smaller variant)
    topo.yaml          single source of truth for this variant
    topology.clab.yaml containerlab topology (generated)
    config/            per-node SONiC + FRR configs   (generated)
    scenarios/         MRC scenario YAMLs
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
- at least 16 vCPU and 32GB of memory (tested with 32 vCPU and 96GB memory)

## Quickstart



## License

Apache-2.0. See `LICENSE`.
