# srv6-ai-fabric

A research-grade simulator for Cisco's Multi-plane Reliable Connectivity
(MRC) approach to AI-fabric networking, layered on top of a static SRv6
uSID dataplane in [SONiC](https://sonic-net.github.io/SONiC/) +
[containerlab](https://containerlab.dev/) + Linux kernel SRv6.

The reference topology is a **4-plane × 8-spine × 16-leaf Clos** carrying
two tenants (`green`, `yellow`) with anycast hosts. The generator is
parameterized via `topo.yaml`, so additional Clos variants are
straightforward to add under `topologies/<name>/`.

## Key Elements

- **Pure-static control plane.** No BGP, no IGP. Every leaf carries its
  own SRv6 locator + transit SIDs as `static-sids` in FRR (pushed by
  `scripts/config.sh`), and every host carries its tenant routes as
  kernel `ip -6 route ... encap seg6 ...` entries (pushed by the
  controller-side `routes` CLI from a YAML route spec). Both layers are
  generated declaratively from `topo.yaml` -- no runtime control plane.
- **Self-healing config push.** `make config` runs `scripts/config.sh`
  which pushes FRR config to every switch in parallel, then verifies
  that the kernel FIB on each leaf contains the expected number of
  `seg6local` entries (the count is derived per-node from the
  generated `frr.conf` so it works for any topology and any tenant
  mix). Any leaf whose SIDs were silently dropped during the FRR
  staticd startup race gets re-pushed automatically.
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
- **Multi-tenancy with two SRv6 patterns.** Both tenants perform
  host-encap. Green is *leaf-decapped* (uDT6 into `Vrf-green` on
  every leaf; the destination is an anycast `2001:db8:bbbb:<NN>::2`
  configured on all 4 of the host's NICs). Yellow is *host-decapped*
  via per-NIC `seg6local End.DT6 table 0` policies on the destination
  host. Phase 1a: yellow now mirrors green's anycast plan exactly
  with `bbbb`→`cccc` — anycast `2001:db8:cccc:<NN>::2` on all 4 NICs
  and on `lo` (`nodad`); the leaf-side gateway `2001:db8:cccc:<NN>::1`
  is also anycast across all 4 planes. Both paths run end-to-end at
  0% loss; yellow shows slightly higher reorder due to the extra
  software decap stage.

For the why behind each design choice, see [`docs/design-fabric.md`](./docs/design-fabric.md),
[`docs/design-mrc.md`](./docs/design-mrc.md), and [`docs/design-appendix.md`](./docs/design-appendix.md).

For detail on multi-tenant design for SRv6 AI factories see [`docs/design-multi-tenant.md](./docs/design-multi-tenant.md)

## Layout

```
srv6_fabric/           Python package: topology constants, runtime libs
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
  Dockerfile           alpine + scapy + pip-installed srv6_fabric

scripts/
  config.sh            push config_db.json + frr.conf into containers

tests/                 unittest mirror of srv6_fabric/ layout
docs/                  consolidated design + runbook documentation
results/               scenario JSON output (gitignored)
```

## Requires
- containerlab
- docker-sonic-vs: tested with Branch Master docker-sonic-vs.gz from [SONiC Dowloads](https://sonic.software/) site.
- at least 16 vCPU and 32GB of memory (tested with 32 vCPU and 96GB memory)

## Quickstart

>[!Note]
> the `make` commands in the following section default to the 4-plane 8x16 spine-leaf topology. If you wish to work with another topology use `make TOPO=<topology-directory-name> deploy/config/etc.`
> Example `make TOPO=2p-4x8 deploy` will deploy the smaller 2-plane 4x8 spine-leaf topology

```bash
# 0. install Python deps for the controller side
pip install -e '.[dev]'

# 1. build the host image (alpine + scapy + srv6_fabric)
#    One image (alpine-srv6-scapy:1.0) serves every topology;
#    topo.yaml is bind-mounted into containers at runtime.
make image

# 2. Optional: (re)generate topology.clab.yaml + per-node SONiC/FRR configs - do this only if you want to change the topology
# make regen

# 3. deploy the lab (containerlab)
make deploy

# 4. push SONiC + FRR configs into the running containers.
#    Self-healing: any leaf whose SIDs failed to install gets re-pushed.
make config

# 5. install per-tenant SRv6 routes on hosts (full-mesh by default;
#    override with ROUTES=reference-pairs etc.). This is what gives
#    each host its `ip -6 route ... encap seg6 ...` entries per plane,
#    and (for yellow) the per-NIC seg6local End.DT6 decap policies.
make host-routes

# 6. run a traffic scenario (spray + per-plane stats + reorder histograms)
make scenario SCEN=baseline           # green tenant, no faults
make scenario SCEN=yellow-baseline    # yellow tenant, no faults

# roadmap traffic scenarios
make scenario SCEN=plane-loss         # 1% loss on plane 2
make scenario SCEN=plane-blackhole    # plane 2 unreachable
make scenario SCEN=plane-latency      # plane 2 +5ms one-way
make scenario SCEN=hash5tuple         # per-flow hash spraying
```

Ad-hoc diagnostics:

```bash
make verify-config                    # re-check + repair leaf SIDs without re-pushing config_db
make TOPO=2p-4x8 deploy config host-routes scenario   # smaller variant (8 spines + 16 leaves + 16 hosts)
```

The CLIs (`spray`, `routes`, `run-scenario`) work both on the lab host
(after `pip install -e .`) and inside the host containers (baked into
the image at build time).

## Run a different topology

Each variant lives under `topologies/<name>/` with its own `topo.yaml`
declaring planes / spines / leaves / images / clab name. To run the
existing 2-plane variant:

```bash
make TOPO=2p-4x8 regen deploy config host-routes
make TOPO=2p-4x8 scenario SCEN=baseline
```

`make image` only needs to run once -- the same host image
(`alpine-srv6-scapy:1.0`) serves every topology, because each variant's
`topo.yaml` is bind-mounted into its host containers at runtime (via
the generated `topology.clab.yaml`). Inside a container, the runtime
reads `SRV6_TOPO=/etc/srv6_fabric/topo.yaml`. Outside containers (lab
host, dev box), it reads `topologies/<name>/topo.yaml` relative to the
repo root, picking the active variant from `TOPO=`.

To add a new variant, copy an existing `topologies/<name>/topo.yaml`,
adjust the dimensions, and run `make TOPO=<new> regen`. The generator
emits a fresh `topology.clab.yaml` plus per-node `config/` from
scratch.

## Testing

```bash
make test     # ~1.5s, no external deps
```

Tests cover address derivation, SID-list construction, the spray wire
format, reorder-distance computation, scenario YAML parsing, route-spec
patch generation, and the MRC orchestrator's argv plumbing.


## License

Apache-2.0. See `LICENSE`.
