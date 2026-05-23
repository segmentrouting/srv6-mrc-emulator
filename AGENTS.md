# AGENTS.md — context for AI coding assistants

Read this first when picking up work in this repository. It captures
the non-obvious invariants and gotchas that aren't visible from a single
file or from the README alone.

For the human-facing tour: see `README.md` (overview), `docs/quickstart.md`
(deploy/run), `docs/design-fabric.md`, `docs/design-mrc.md`,
`docs/spray-protocol.md`, and `docs/design-appendix.md`.

---

## What this lab is

A 4-plane SRv6 fabric on docker-sonic-vs + Containerlab. The default
topology is `4p-4x8` (4 planes × 4 spines × 8 leaves per plane = 32
fabric nodes, plus 16 alpine hosts: 8 green + 8 yellow). A larger scale
design `4p-8x16` (4 planes × 8 spines × 16 leaves = 128 fabric
nodes + 32 hosts) is available via `make TOPO=4p-8x16 …` or
`SRV6_TOPO=…/4p-8x16/topo.yaml`. It demonstrates the MRC + SRv6-spray
model: one logical flow fans out across all 4 planes by varying *only*
the outer SID list.

Tenants:

- **green** — hybrid: leaf does encap+uDT6 decap in `Vrf-green`. Anycast
  inner dst `2001:db8:bbbb:<NN>::2` on all 4 NICs (`nodad`).
- **yellow** — host-based: leaf does encap+uA(host-port); host runs 4
  `seg6local End.DT6` (one per plane NIC) for decap. Anycast inner dst
  `2001:db8:cccc:<NN>::2` on all 4 NICs and on `lo` (`nodad`). Mirrors
  green's anycast plan with `bbbb`→`cccc` (Phase 1a).

## Repo layout

```
srv6_mrc/           Python package
  topo.py              fabric constants + addressing helpers (reads topo.yaml)
  topology.py          typed Topology accessor (Refactor 1 in progress;
                       parallel to topo.py during migration)
  runner.py, policy.py, reorder.py, netem.py, report.py
  encap.py             shared raw-socket SRv6 outer-packet builder
                       (used by runner.py and mrc/transport.py)
  cli/spray.py         userspace SRv6 packet generator (CLI: `spray`)
  cli/routes.py        static SRv6 route management   (CLI: `routes`)
  cli/srctl.py         kubectl-shaped lab CLI         (CLI: `srctl`)
  mrc/
    run.py             scenario orchestrator           (CLI: `run-scenario`)
    daemon.py          per-host MRC daemon: single SO_REUSEPORT reply-
                       socket owner + per-flow snapshot writer
    scenario.py        scenario YAML schema + executor (incl. `mrc:` block)
    agent.py           SenderMrcAgent / ReceiverMrcAgent + env-loader
    transport.py       MrcTransport ABC + Srv6RawTransport +
                       LoopbackUdpTransport
    ev_state.py        EVStateTable + per-plane state machine
    probe.py           PROBE / PROBE_REPLY / LOSS_REPORT wire format
    probe_clock.py     in-flight probe tracking + timeout sweep
    loss_window.py     receiver per-(flow,plane) loss accounting
    loss_compute.py    sender-side fusion of LOSS_REPORT with sent windows

generators/fabric.py   parameterized generator (reads topo.yaml)

topologies/<name>/
  topo.yaml            declarative single source of truth for one variant
  topology.clab.yaml   containerlab topology (generated)
  config/              per-node SONiC + FRR configs   (generated)
  scenarios/           MRC scenario YAMLs
  routes/              route-spec YAMLs for `routes apply`

host-image/Dockerfile  alpine + scapy + pip-installed srv6_mrc
scripts/config.sh      push configs to running containers
tests/                 329 unit tests mirroring srv6_mrc/ layout
docs/                  consolidated design + runbook docs
results/               scenario output JSON (gitignored)
Makefile               operator workflow entry point
```

## Source of truth

`topologies/<name>/topo.yaml` declares the topology: planes, spines,
leaves, container images, clab topology name. The generator
(`generators/fabric.py --topo <path>`) reads it and emits
`topology.clab.yaml` + the per-node `config/*` SONiC config snippets in
the same directory. The `srv6_mrc.topo` runtime module also reads
it at import time (via the `SRV6_TOPO` env var, defaulting to
`topologies/4p-4x8/topo.yaml`). 

**Never hand-edit generated files**:
- `topologies/<name>/topology.clab.yaml`
- `topologies/<name>/config/<node>/{config_db.json,frr.conf}`

Files that must stay in sync because they share addressing/SID-list shape:

- `generators/fabric.py` (writes routes into SONiC + host configs)
- `srv6_mrc/cli/routes.py` (parses + writes host-side `ip -6 route`
  SRv6 routes)
- `srv6_mrc/cli/spray.py` (CLI; delegates encoding to
  `srv6_mrc.runner`)
- `srv6_mrc/topo.py` (fabric constants + `usid_outer_dst()` — the
  SID-list builder the runner uses)
- `srv6_mrc/runner.py` (wire format: `!QBB` seq+plane+path, 32B pad,
  sport=dport=SPRAY_PORT)
- `srv6_mrc/encap.py` (shared `build_outer_packet()` +
  `open_raw_send_socket()` helpers — used by both `runner.py` for data
  packets and `mrc/transport.py` for SRv6-encapped probes)
- `srv6_mrc/mrc/transport.py` (`MrcTransport` ABC + `Srv6RawTransport`
  + `LoopbackUdpTransport`; the agent does all I/O through this)

If you change the SID-list shape, the per-plane block layout, or any
tenant naming/addressing, all of these must be updated. The
`test_reference_pairs_match_spray` test in `tests/test_topo.py` locks
the `srv6_mrc.topo` ↔ `spray` reference-pairs map in sync.

## Hard invariants (do not violate)

1. **uSID = no SRH.** Outer is plain IPv6 with `nh = 41` (IPv6-in-IPv6);
   the SID list is the destination address itself and shifts left at each
   hop. `encap.red` semantics. Do not add SRH. If you see code or design
   notes producing or assuming an SRH on the wire, that's a bug. When
   talking about the encap, say "encap.red" or "outer IPv6 with uSID DA",
   never "SRH" — there is no SRH in this fabric. Any TX path that does
   not go through `ip -6 route add ... encap seg6 mode encap.red` (or the
   equivalent raw-socket build of that exact wire format) is wrong.
2. **Plane identity lives ONLY in the outer SID list** (the `<P>` hextet
   at index 1 of the SID). Never in the inner/tenant address. Putting
   plane in the inner address breaks the MRC invariant — the whole point
   of the demo.
3. **Per-plane uSID `/32` blocks** under the cluster `fc00:0000::/30`:
   plane P uses `fc00:<P>::/32`.
4. **Tier role hextets** are self-describing:
   - `f...` = leaf-up uA (spine→leaf)
   - `e...` = spine-down uA *and* leaf→host uA (overloaded; tier is
     disambiguated by position in the SID list)
   - `d...` = tenant uDT6 (decap into VRF / End.DT6)
5. **`e<NIC ordinal>` rule**, not `e<port number>`:
   - `Ethernet0`  → `e000`
   - `Ethernet32` → `e008`
   - `Ethernet36` → `e009`
   This bit us before. Don't "fix" it back to using port numbers.
6. **Green leaf VRF**: `Ethernet32` lives in `Vrf-green`; leaf `d000`
   uDT6 decaps into it.
7. **Yellow decap on host, not leaf**: yellow's `End.DT6` runs on the
   host's NIC (one per plane), not on the leaf. The sender's SID list
   includes the extra `e009` hop (leaf→host) that green omits.
8. **Sender plane selection is NIC-bound, not route-metric-bound.**
   `spray` uses a raw IPv6 socket per plane with `SO_BINDTODEVICE`.
   Kernel ECMP would defeat plane spray since green's inner dst is
   anycast. Don't replace this with kernel routing.
9. **`encap seg6` route shape** (what `routes` and `delete --all`
   match on) is what defines "an SRv6 pair route". Yellow's per-NIC
   `seg6local End.DT6` rules are *decap* policies, intentionally not
   touched by `routes` apply/delete (they're installed by the
   generator).
10. **Spine entropy via SID rotation, never kernel ECMP.** When EV-
    spray varies the spine per packet, the runner builds a new outer
    DA in scapy (`usid_outer_dst(tenant, plane, spine, dst_leaf)`)
    and sends through the same plane-bound raw socket. Do NOT add
    multipath/ECMP host routes per spine and rely on the kernel
    hashing flows across them — that defeats per-packet EV identity
    (the receiver-side health table needs every packet's EV to be
    known to the sender, not chosen randomly by the kernel) and
    would resurrect the same anycast/ECMP ambiguity that invariant 8
    exists to prevent. Per-EV state attribution (planned for Phase
    1b step 3) depends on the sender owning the spine choice.

## Phase status (where the codebase is right now)

- **Phase 1a (plane-aware MRC):** done, validated in lab for BOTH
  tenants (green + yellow). Plane-level loss detection via receiver
  MRC agent; sender demotes lossy planes in `health_aware_mrc`
  policy.
- **Phase 1b step 1 (EV-spray data-path):** done + lab-validated for
  BOTH tenants. `EvSpray` policy varies BOTH plane and spine per packet
  via per-packet outer-DA rotation. Reports include `per_ev_sent` keyed
  `"P<p>:S<s>"`. No health awareness on its own — every EV is always
  active. Scenarios shipped: `green-ev-spray.yaml`,
  `green-ev-spray-n2.yaml`, `yellow-ev-spray.yaml`,
  `yellow-ev-spray-n2.yaml`. The data wire format carries `path` (the
  MRC-layer name for `spine`) in the payload so the receiver can
  attribute per-EV stats.
- **Phase 1b step 2 (per-EV scapy raw-socket probes):** done. Probe TX
  moved off the per-plane UDP+`encap.red`-route path onto a shared
  `srv6_mrc/encap.build_outer_packet()` raw-socket builder — the
  same helper the data path uses for EV-spray. Probes are now per
  `(plane, path)` EV, so probe granularity matches data-path EV
  granularity (NUM_PLANES × NUM_SPINES probes per round). All I/O for
  both `SenderMrcAgent` and `ReceiverMrcAgent` flows through an
  `MrcTransport` (`Srv6RawTransport` in lab, `LoopbackUdpTransport`
  in tests). The sender collapses to one rx socket on
  `SPRAY_REPORT_PORT` that demuxes PROBE_REPLY (magic `0xA6`) vs
  LOSS_REPORT (magic `0xA7`); per-plane reply-rx loops are gone.
  Receiver replies on the same `(plane, path)` the inbound PROBE
  arrived on; loss reports ride the last-seen-PROBE EV per sender.
  Scenarios shipped: `green-mrc-ev-spray.yaml`, `yellow-mrc-ev-spray.yaml`.
- **Phase 1b step 3 (per-EV health):** not started. New policy that
  extends `EvSpray` with an "active EV mask" read from a per-EV
  health table (the `EVStateTable` is already per-EV in step 2 — it
  keys on `(tenant, plane, path)`). On loss detection at an EV
  granularity, the sender skips that EV on its round-robin turn.
  Ship both `green-mrc-ev-spray-*` and `yellow-mrc-ev-spray-*`
  scenario sets together — yellow parity is now the steady-state
  expectation, not a follow-up.
- **Collective-communication scenarios (in progress):** scaffolding
  shipped for AI-style traffic patterns on top of the existing
  `health_aware_mrc` data path.
  - Pair-set aliases in `srv6_mrc/mrc/scenario.py`:
    `{green,yellow}-all-to-all` (N*(N-1) ordered pairs) and
    `{green,yellow}-ring` (N pairs forming `i -> (i+1) mod N`,
    NCCL-style unidirectional ring). Both auto-size from
    `NUM_LEAVES`: 240 / 16 on 4p-8x16, 56 / 8 on 2p-4x8.
  - 4p-8x16 scenarios shipped:
    `green-all-to-all.yaml`, `yellow-all-to-all.yaml`
    (240 flows @ 20pps, `paths_per_plane: 8`),
    `green-allreduce-ring.yaml`, `yellow-allreduce-ring.yaml`
    (16 flows @ 100pps, `paths_per_plane: 8`), plus the diagnostic
    `yellow-allreduce-ring-half.yaml` (paths_per_plane: 4).
  - **Known-broken**: every `health_aware_mrc` scenario where every
    host runs *both* a sender process AND a receiver process (i.e.
    the ring and all-to-all patterns) fails to keep EVs healthy.
    At paths_per_plane=8 every flow ends with ~16/32 EVs demoted to
    `assumed_bad` despite 0% data-plane loss; per-plane traffic
    spreads ~5-7x. `yellow-mrc-ev-spray` (8 senders, 8 *different*
    receivers, same paths_per_plane=8) is unaffected. See "Things
    learned" below for the current diagnostic state.

    **2026-05-20 RESOLVED** (PR #1, merged): structural cure was the
    MRC daemon refactor + sniffer egress filter + orphan-prevention
    teardown. Lab-verified clean on `4p-4x8` `yellow-all-to-all` over
    4 consecutive runs (~0.1% loss steady, zero orphans between runs).
    See "RESOLVED: SO_REUSEPORT cascade on collective scenarios" below
    for full diagnosis and fix.

## Yellow parity — do not forget

Every spray feature added for green needs a yellow counterpart in
the same commit (or the immediate follow-up). Past pattern: green
gets a feature, yellow lags by N commits, and the demo story
becomes "green works, yellow is broken in interesting ways."
Concretely:

- New policy → exercise it in both `green-…yaml` and `yellow-…yaml`
  scenarios before declaring the feature done.
- New probe / health code path → test with both tenants in the
  lab; yellow's host-side decap (invariant 7) is the failure mode
  green never exposes.
- Phase 1b step 1's yellow-ev-spray YAMLs are the immediate
  outstanding piece. Pick those up at the same time as step 2's
  scapy probes — they're cheap (~20 lines of YAML each) and rerun
  the same `green-ev-spray-n2` validation against yellow as a
  yellow-decap regression check.

## Naming conventions

- Containers: `p<P>-spine<NN>`, `p<P>-leaf<NN>`, `<tenant>-host<NN>`.
- Host N attaches to `leafN` on every plane. (`hostNN` ↔ `leafNN`.)
- User-facing term is **"tenant"**, never "color".

## Tooling specifics

### `routes` (`srv6_mrc/cli/routes.py`)

Declarative kubectl-style route manager. Requires PyYAML.
Spec format: `apiVersion: srv6-lab/v1`, `kind: RouteSet`, with `pairs`
and/or `mesh` entries only. **There is intentionally no low-level
`routes:` escape hatch** — keep specs high-level.

`spine: auto` resolves via `REFERENCE_PAIRS_SPINES` lookup, falling back
to `(a*16+b) % 8` hash.

Subcommands:

```
routes apply  -f spec.yaml
routes delete -f spec.yaml
routes delete --all
routes list   [--host h1,h2] [--tenant green|yellow] [-o wide|raw]
```

`list` modes:

- default — collapsed: `-> <tenant>-host<NN>  via spine<NN>  planes [...]`
- `-o wide` — full per-plane path: `p<P>-leaf<src> -> p<P>-spine<NN> -> p<P>-leaf<dst>  (eth<P+1> metric 10<P>)`
- `-o raw` / `--raw` — literal `ip -6 route` lines

### `spray` (`srv6_mrc/cli/spray.py`)

Userspace SRv6 sprayer, image `alpine-srv6-scapy:1.0`. The image
pip-installs the `srv6_mrc` package at build time, so `spray` lives
at `/usr/local/bin/spray` inside every host container — no bind mounts
needed for code. The active topology's `topo.yaml` is bind-mounted
into each host container at runtime by containerlab (per the
`binds:` block in `topology.clab.yaml`), landing at
`/etc/srv6_mrc/topo.yaml`; the image exports `SRV6_TOPO` pointing
at that path. This means a single image serves every topology
variant — the topology identity moves with the container, not the
image.

`--role send|recv`, auto-detects tenant from hostname. Sender uses one
raw socket per plane bound via `SO_BINDTODEVICE`. Receiver sniffs at NIC
pre-decap (yellow can't sniff post-decap on `lo` per-NIC).

Notable flags:
- `--policy {round_robin,hash5tuple,weighted:0.4,0.3,0.2,0.1,ev_spray[:N],health_aware_mrc}`
  — default `round_robin`. `health_aware_mrc` only does something useful
  when the sender also starts a `SenderMrcAgent` (auto-started from
  `cmd_send` when this policy is selected). `ev_spray[:N]` varies BOTH
  plane AND spine per packet (4 * N EVs total); bare `ev_spray` uses
  the full fan-out (N = `NUM_SPINES` from `topo.yaml`). The outer DA's
  `f<S>` hextet rotates packet-by-packet so each EV traces a distinct
  leaf-to-spine path; the receiver doesn't care which EV a probe came
  from, because the data-path runner doesn't use kernel routes for the
  outer encap (scapy builds the wire form directly).
- `--paths-per-plane N` — runtime override for `ev_spray` fan-out.
  Equivalent to `ev_spray:N` in `--policy` but lets scenario YAML's
  `paths_per_plane` field propagate via the `SRV6_PATHS_PER_PLANE` env
  without rewriting the policy string. Order of precedence: explicit
  `ev_spray:N` in `--policy` > `--paths-per-plane` CLI flag >
  `SRV6_PATHS_PER_PLANE` env > policy default (`NUM_SPINES`).
- `--mrc` — receiver-only flag; opens probe-reply + loss-emit sockets
  and starts a `ReceiverMrcAgent`. Off by default; baseline runs are
  unaffected.
- `--json` — emit machine-readable result instead of human-readable
  output; used by the orchestrator (`run-scenario`).

The orchestrator sets `SRV6_MRC_CONFIG_JSON` on every `docker exec` when
the scenario has an `mrc:` block. Both `cmd_send` and `cmd_recv` decode
it via `srv6_mrc.mrc.agent.load_configs_from_env()` into
`(AgentConfig, EVStateConfig | None)`. Unknown keys, bad JSON, or
non-object payloads fail loud rather than reverting to defaults.

### `run-scenario` (`srv6_mrc/mrc/run.py`)

Docker-host-side orchestrator for MRC scenarios. Loads a scenario YAML,
applies fault injection via `nsenter ... tc qdisc add ...` against host
veths, runs `spray` send/recv inside the relevant containers via
`docker exec`, and merges the JSON output into a `ScenarioReport`.

```
run-scenario topologies/4p-4x8/scenarios/green-mrc-baseline.yaml --verbose
run-scenario topologies/4p-4x8/scenarios/green-mrc-plane-loss.yaml --dry-run
```

`--dry-run` prints the plan plus the exact `nsenter ... tc qdisc add ...`
argvs that would be invoked — useful for verifying fault targeting
without touching the lab.

### MRC architecture (current build)

`health_aware_mrc` is the live MRC policy. It reads weights from an
`EVStateTable` shared with a `SenderMrcAgent` running in the same
process. All probe / reply / loss-report I/O is delegated to an
`MrcTransport` (`srv6_mrc/mrc/transport.py`): `Srv6RawTransport`
in the lab (raw-socket SRv6 encap via `srv6_mrc.encap`),
`LoopbackUdpTransport` in unit tests. There is one code path; the
agent never branches on transport.

The sender drives four background threads:

- `mrc-emit` — emits one PROBE per `(plane, path)` EV every
  `probe_interval_ms`. With 4 planes × 8 spines that's 32 probes
  per round.
- `mrc-reply-rx` — single rx socket on `SPRAY_REPORT_PORT`, demuxes
  by magic byte: `0xA6` → PROBE_REPLY (feeds
  `EVStateTable.record_probe_result(tenant, plane, path, ...)`);
  `0xA7` → LOSS_REPORT (feeds `record_loss_window`). Replaces the
  pre-step-2 design's per-plane reply sockets and the separate
  loss-report-rx loop.
- `mrc-sweep` — declares in-flight probes lost after
  `probe_timeout_ms`.
- `mrc-window` — advances the per-EV `SentWindowRing` so the
  receiver-side loss-fusion has matching `(plane, path)` sent
  counts to compare against.

The receiver-side counterpart (`ReceiverMrcAgent`) runs two threads:
`mrc-probe-rx` (replies on the same `(plane, path)` EV the inbound
PROBE arrived on) and `mrc-report-emit` (one LOSS_REPORT round per
`loss_window_ms` per known sender, sent on the last-seen-PROBE EV
for that sender). The data-record hook is called from the existing
receiver hot loop via `on_packet=`.

Detection signals fused into `EVStateTable`:

1. **EV Probes** — per `(plane, path)` SRv6-encapped raw-socket
   probes, RTT-stamped. Demotes after `probe_fail_threshold`
   consecutive timeouts.
2. **Receiver-side loss reports** — receiver computes per-EV
   `(plane, path)` `(seen, expected_local, max_gap)` and unicasts
   to the sender; sender compares against its own per-EV sent
   windows (`SentWindowRing` is 2-D `[plane][path]`). Demotes after
   `loss_demote_consecutive` windows over `loss_threshold`.

Demotions are subject to an `min_active_evs` floor (OCP's
`ev_min_active`). Recovery is asymmetric: demote fast, recover slow
(`probe_recover_threshold` defaults to 5).

### Known limitations

- **Receiver loss estimator needs partial loss.** The receiver estimates
  per-plane `expected = max_seq − min_seq + 1`. A 100% blackholed plane
  has zero arrivals, so the loss window reports 0/0 and the plane stays
  UNKNOWN. The probe channel still catches a hard blackhole (no replies
  → `probe_fail_threshold` timeouts → demote), but a partial-loss netem
  scenario will demote much faster than a blackhole one.
- **Reorder isn't a demote signal yet.** Latency-only faults
  (`green-mrc-plane-latency.yaml`) leave loss windows clean and probe
  replies arrive (slow but successful), so plane state stays GOOD. The
  per-plane RTT ring is collected for diagnostics but not consulted by
  the weight builder. This is by design today; the plane-latency
  scenario is the regression fixture for when RTT-aware weighting
  lands.
- **Per-flow MRC agent, not per-host.** Today there's one
  `SenderMrcAgent` per spray invocation, so two simultaneous flows from
  the same host emit duplicate probe streams. A per-host agent with IPC
  is the planned commit-3 refactor.

### Lab validation status (green tenant)

First end-to-end lab run on the docker-sonic-vs 4-plane fabric
confirms the three headline green-tenant scenarios behave as designed
(see `docs/design-mrc.md` for the detailed table):

- `green-mrc-baseline` — uniform spray, all planes `GOOD`, 0% loss
- `green-mrc-plane-loss` — 5% loss on plane 2 triggers loss-path
  demotion (`consecutive_loss_demote_windows ≥ 2`); plane 2 weight
  drops to 0, planes 0/1/3 carry uniformly, total loss ≈ 0.07% vs
  ~1.25% under round-robin (16× improvement)
- `green-mrc-plane-latency` — 10ms delay on plane 3 leaves all planes
  `GOOD` at weight 0.25 each; plane 3 RTT p50 ≈ 21ms is observed in
  EV-state diagnostics but correctly not acted on

The sender's `--json` output includes an `mrc` block (EV-state +
LossFusionStats) when `--policy=health_aware_mrc` is active; the
ScenarioReport JSON passes it through as `flows[].mrc`. Inspect with:

```bash
jq '.flows[].mrc' results/green-mrc-plane-loss.json
```

Yellow MRC scenarios (`yellow-mrc-ev-spray.yaml` etc.) ship in the
same commit set as their green counterparts as of Phase 1b step 2.
Per-host MRC agent w/ IPC (deduplicate probes across N flows on
one host) is the next milestone.

### Gotcha: rebuild the image whenever `srv6_mrc/` changes

`make image` runs `docker build`; layer caching has occasionally
shipped a stale `srv6_mrc` package inside the container even after
a clean `git pull`. Symptom is line-number mismatches between local
tracebacks and container tracebacks. When in doubt:

```bash
docker build --no-cache -f host-image/Dockerfile -t alpine-srv6-scapy:1.0 .
docker run --rm alpine-srv6-scapy:1.0 \
    wc -l /usr/lib/python3.12/site-packages/srv6_mrc/mrc/agent.py
```

The line count should match the local `srv6_mrc/mrc/agent.py`.
Roadmapped: a `make image` sanity rail to fail the build if the line
count drifts.

### Per-NIC RX in `ScenarioReport` (unchanged limitation)

Receiver reports per-NIC totals aggregated across flows. The merge
attaches them to the first matched flow per receiver; multi-sender-to-
one-receiver loses per-NIC fidelity. `FlowStats` would need a per-NIC
counter to fix this.

### Removed: `scripts/validate.sh`

Previously a ping+tcpdump per-plane verification harness. Removed because
its model — ping with `-I eth<N>` to force outbound plane — couldn't verify
the return path: ICMPv6 replies bypass any plane affinity (the kernel just
picks the lowest-metric route to the source's anycast address), so planes
1..N-1 always reported FAIL. End-to-end verification is now via
`make scenario SCEN=green-mrc-baseline`, which uses spray (sender-side plane
selection via SO_BINDTODEVICE) and measures per-plane stats at the receiver.

## Test command (run from repo root)

```
PYTHONPATH=. python3 -m unittest discover -s tests -t .
```

or:

```
make test
```

329 tests, ~1.5s, no lab needed.

## Gotchas (caught the hard way)

- **Alpine iproute2 output**: addresses print in canonical-collapsed form
  (`fc00:0:f000:e00f:d000::`, not `fc00:0000:...`). Parsers must match
  both forms. `segs` prints in bracketed form: `segs 1 [ <addr> ]`.
- **iproute2 omits `/128`** from natural-width host-route dst even when
  installed as `/128`. Parsers should not require it.
- **Container short names work directly** (`green-host00`) — no
  `clab-<topo>-` prefix needed when invoking `docker exec`.
- **CAP_NET_RAW**: `spray` needs it; clab privileged containers have it.
- **SIGPIPE**: `routes` installs `SIG_DFL` so `routes list | head`
  doesn't traceback. Keep this if you refactor `main()`.
- **Tenant container suffix is the leaf id in hex** (`...:f::2` is
  host15), and SID f-hextet `f00<N>` is spine N. `routes`'s
  `_decode_*` helpers depend on this.
- **IPv6 string canonicalization**: `fc00:0000:f000:0e00:d000::` and
  `fc00:0:f000:e00f:d000::` are equal but the strings differ. Anywhere
  that compares IPv6 addresses-as-strings, route them through
  `ipaddress.IPv6Address` first. See `_canon_addr()` in
  `srv6_mrc/report.py`.
- **`yellow-mrc-ev-spray` is NOT a general MRC smoke test**: it works
  because hosts 0-7 are senders-only and hosts 8-15 are receivers-only
  — a host runs `SenderMrcAgent` XOR `ReceiverMrcAgent`, never both.
  Any scenario where the same host appears as both source AND
  destination across the flow set (ring, all-to-all, any future
  collective) puts both agents in the same network namespace and
  exposes a class of bugs that the sender-only/receiver-only scenarios
  never hit. Treat collective-comm scenarios as a distinct regression
  surface from the existing MRC scenarios.
- **`select_spines_for_addrs` doesn't balance at small N**: with N=16
  ring pairs and paths_per_plane=4 (= 16 EVs per pair from a 32-EV
  grid), the union of selected (plane, spine) cells across all 16
  pairs is non-uniform. Per-plane traffic can still skew 3-4x even
  when all MRC weights are equal. all-to-all (N=240) gets way more
  entropy and is expected to self-balance. Not a bug per se — just
  a property to be aware of when reading ring-scenario reports.
- **`SRV6_TOPO` must be set before any srv6_mrc import**: module-level
  constants `NUM_LEAVES`, `NUM_PLANES`, `NUM_SPINES` (in `srv6_mrc.topo`)
  bind once at import time from whatever topology the env var points
  at. Auto-sized scenario aliases (`<tenant>-pairs`, `-ring`,
  `-all-to-all`) use those constants directly, so a stale or missing
  `SRV6_TOPO` produces silently-wrong host IDs (e.g. expanding
  `yellow-pairs` to 8 mirror pairs touching host15 on a topology
  that only has 8 hosts). The CLI entrypoints `srv6_mrc.mrc.run`
  and `srv6_mrc.cli.srctl` set `SRV6_TOPO` themselves before any
  srv6_mrc import by inspecting argv for the scenario path/name —
  see `_infer_srv6_topo_from_argv()` in each. The Makefile sets it
  inline on every target that runs srv6_mrc Python (`host-routes`,
  `scenario`). If you add a new entrypoint, do the same.
- **`config interface shutdown/startup` on docker-sonic-vs strips the L3
  IPv6 address binding.** `docker exec <node> config interface shutdown
  EthernetN` toggles admin state in CONFIG_DB; the matching `startup`
  reverses admin state but *does not always re-install* the
  `INTERFACE|EthernetN|<addr>/127` L3 binding. The port returns to
  `up/up` administratively with only its link-local — any packet that
  needs to reach the peer's underlay (`2001:db8:fab:N::/127`) is
  silently blackholed. The smoking-gun MRC symptom is
  `cps=0, cpt>>3, transitions=1` on the EVs whose **reply** path
  egresses that port (probes go one direction, the reply needs the
  reverse-direction egress); meanwhile the data plane shows 0% loss
  because the policy correctly steers around the demoted EVs. **For
  repeatable fault injection in MRC tests use one of:**
    - `docker exec <node> ip link set EthernetN down` and `... up`
      (touches netdev only, CONFIG_DB and L3 bindings persist;
      simplest cable-yank semantics — verify it works cleanly with
      docker-sonic-vs SAI/orchagent before adopting broadly),
    - `sudo nsenter -t $(docker inspect -f '{{.State.Pid}}' <node>)
      -n tc qdisc add dev EthernetN root netem loss 100%` then
      `... tc qdisc del dev EthernetN root` (what `srctl scenario`
      plane-loss already uses; zero-touch on container), or
    - `ip -6 addr del/add` on the address itself (most explicit but
      requires remembering the per-port subnet:
      `Ethernet0=2001:db8:fab::/127`, `Ethernet4=2001:db8:fab:1::/127`,
      etc — error-prone for non-zero ports).
  **To recover from an already-stripped binding**: `make config`
  re-pushes CONFIG_DB and auto-repairs (`scripts/config.sh` already
  does verify+repair on `seg6local` rules; the L3 address binding
  comes back along with it). The MRC stack itself handles this
  correctly — the "stuck demotion" was a lab-hygiene artifact, not
  an MRC bug. Found 2026-05-23 during validation of the demux/policy
  fixes.
- **MRC daemon writes snapshots in a wrapped envelope; readers must
  unwrap.** `MrcDaemon._publish_snapshot` writes
  `{"src_host":..., "tenant":..., "dst_id":..., "captured_ns":...,
   "ev_state": <EVStateTable.snapshot()>}` — the actual snapshot
  payload lives under the `ev_state` key, with traceability metadata
  at the top level. The `MrcSnapshot` policy
  (`policy.py::_wgrid_from_snapshot`) and `report.py::_active_evs_from_mrc`
  both accept the wrapped shape; if you add a new consumer, do the
  same (look for `ev_state` and unwrap before reading `num_planes`
  etc.). Pre-fix, the policy read `data["num_planes"]` directly,
  hit `KeyError`, swallowed it at the caller as `refresh_errors +=
  1`, and the policy kept its cold-start uniform grid **forever** —
  the sender never honored any demotion. Unit tests in
  `tests/test_mrc_snapshot_policy.py::_make_snapshot` write the flat
  shape directly via `EVStateTable.snapshot()`, predating the daemon
  wrapper; new tests should follow `test_daemon_wrapped_snapshot_accepted`
  and use the wrapper to model the real I/O contract. Found
  2026-05-23 alongside the demux IPv6 canonicalization fix; the
  two together account for the entire "EV demoted by daemon but
  sender keeps spraying through it" failure mode.

## Open investigations (not yet root-caused)

These are bugs surfaced by the collective-comm scenarios; carry
forward across sessions until resolved. Adding new diagnostic data?
Update this section, don't start a fresh "what we know" thread.

### Universal probe failure in "every host is both sender AND receiver" scenarios

**Symptom**: Run `yellow-allreduce-ring` (16 flows, 16 hosts each
acting as both source and destination in the ring). Data plane is
perfect (0% loss, 5800 packets per flow, balanced delivery). But
the MRC snapshot shows **every EV has 294-296 consecutive probe
timeouts and 0 successes**. Half end up `assumed_bad` (weight 0),
half stay `unknown` only because the per-tenant "demotes suppressed
by floor" guard fires. Per-plane traffic spreads 5-7x because the
remaining live EVs cluster on a couple of planes by chance.

**Falsified hypotheses**:
1. *MRC config inconsistency between flows* — no, the policy is
   round-robin/weighted-CDF per packet over the per-flow EV grid;
   16/32 active is a real demotion not a structural cap.
2. *Probe-emit thundering herd* (16 senders firing 32-probe bursts
   in lockstep every 200ms saturates spine TX queues) — pacing
   `_emit_loop` to one probe per `interval/num_evs` slot plus
   per-instance startup jitter (committed; see
   `tests/test_mrc_agent_io.py::ProbePacingTests`) did not fix it.
   The lab repro after pacing is essentially identical to before.

**Still open**:
3. *Probe egress NIC contention*: every host in the ring has TWO
   `Srv6RawTransport` instances open simultaneously — one in the
   sender process (4 raw sockets bound to PLANE_NICS), one in the
   receiver process (4 more raw sockets bound to the SAME
   PLANE_NICS). SO_BINDTODEVICE is per-socket but the kernel TX
   queue is shared. Maybe contention, maybe a routing oddity when
   two processes both have raw sends out the same NIC.
4. *Probe ingress demux*: receiver listens on `::, SPRAY_PROBE_PORT`
   with SO_REUSEPORT; sender listens on `::, SPRAY_REPORT_PORT` also
   with SO_REUSEPORT. Different ports so they shouldn't fight, but
   SO_REUSEPORT load-balancing kernels-side could be a factor at
   higher scales than the existing tests exercise.
5. *Receiver probe-RX thread starvation*: each receiver runs ONE
   probe-RX thread that decodes the probe and synchronously builds
   + sends a reply via scapy outer construction. With 32 probes
   landing per round per receiver, the per-probe budget is
   ~6ms — scapy build for a 32-byte SRv6 outer typically takes
   1-3ms on alpine-on-docker-sonic-vs, so we may be at the edge.

**Diagnostic progress (session log)**:

- Single-spine tcpdump runs (p0-spine00 Ethernet0, then p2-spine00
  Ethernet0) confirmed data + replies *do* traverse the fabric
  for ring pairs, encapsulated correctly with `encap.red` outer.
  Single-spine captures CANNOT answer the real question (is
  traffic concentrated on a few EVs?) — they only confirm the
  spine being watched is carrying something.
- **µSID decoding reminder** (got this wrong twice in one
  session — don't repeat): outer `fc00:<plane>:e00L:e009:d00H::`
  on a spine's Ethernet0 is *post-spine-uN-pop*, so the leftmost
  remaining slot `e00L` is "uA from this spine toward leaf `L`",
  `e009` is "leaf uA out the host port", `d00H` is "uDT decap on
  the yellow host at leaf `H`". The spine identity is implicit
  in WHICH router you captured on, not in the µSID string.
- **ICMPv6 port-unreachable on port 9999** observed in p0-spine00
  capture — host00 sending PROBE_REPLY/LOSS_REPORT to host15:9999,
  host15's kernel returning ICMP unreachable. Six in ~150ms then
  stopped. Probably a startup race (receiver-side sender process
  not yet bound on `:::9999` when first reply arrives) or process
  teardown. **Parked as a separate issue** — not believed to be
  the cause of the 16/32 EV demotion. Revisit once main bug is
  fixed.
- **2026-05-20**: 4p-4x8 phantom-demote investigation closed (see
  RESOLVED entry below). Two stacked bugs in `loss_compute` +
  `ev_state` defaults; fixed in `d37d70e` (skip-on-no-pairing) and
  `5093246` (raise loss thresholds above straggle floor). 16/16 EVs
  on healthy fabric across green and yellow baselines confirmed.

**Next diagnostic step (tomorrow morning, 2 minutes of lab time)**:
4×8 grid scan to count packets per (plane, spine) during a 60s
`yellow-allreduce-ring` run:

```bash
for p in 0 1 2 3; do
  for s in 00 01 02 03 04 05 06 07; do
    n=$(timeout 5 docker exec p${p}-spine${s} tcpdump -nni Ethernet0 \
        'ip6 and udp' 2>/dev/null | wc -l)
    printf "p%d-spine%s: %s pkts\n" $p $s $n
  done
done
```

Three possible outcomes branch the investigation cleanly:

1. **All 32 cells roughly equal** → data is well-distributed
   across EVs. The 16/32 demotion is purely a probe/reply path
   bug (hypotheses 3, 4, 5 above remain in play; the ICMP-9999
   issue gets promoted back to suspect).
2. **~16 of 32 cells near zero** → data path itself isn't using
   half the EVs. Probe loss on those cells is a *consequence*
   not a cause. Root cause is in `select_spines_for_addrs` or
   in how `EvSpray` / `health_aware_mrc` rotates outer DAs.
3. **Skewed but not binary** → look at the actual distribution
   shape; hypothesis-set depends on which cells are cold.

### Spurious "orphan flow" report warnings on collective scenarios

Every host in the ring scenario gets an "orphan flow" warning where
the source/dest inner addresses match the host's own send pair. The
data is delivered correctly to the real destination too; this is a
report-side false positive. Receiver process appears to count its
own outbound packets as inbound, then `report.py` doesn't find a
matching scenario flow keyed to that direction. Lower priority than
the probe-failure bug. Anycast routing was ruled out — every host
has distinct inner addresses (`cccc::2`, `cccc:1::2`, etc).

**2026-05-20 update**: PR #1 added a sniffer egress filter
(`inner_dst == self`) which addresses the most likely contributor.
Re-check on collective scenarios; if warnings persist they're a
distinct issue.

### Per-plane traffic skew on small-N scenarios

`select_spines_for_addrs` doesn't balance well across small flow
counts (e.g. 16 ring pairs). See "Things learned" above. Expected
to self-balance at all-to-all scale (240 flows); benchmark and
confirm once the probe-failure bug is fixed.

### Flaky `test_plane_loss_demotes_and_picks_shift`

`tests/test_mrc_integration.py::PlaneLossShiftsDistributionTests::test_plane_loss_demotes_and_picks_shift`
passes ~50% in isolation and in full-suite runs. Failure mode is
"expected exactly one demoted EV; got 5" — looks like a stochastic
ordering issue in the simulated event sequence rather than a real
defect. Pre-existing; not introduced by PR #1 but visible now that
the suite is otherwise clean. Worth a small follow-up before it
masks a real regression.

### RESOLVED: Phantom EV demotions on healthy 4p-4x8 baselines

**Symptom (4p-4x8 green/yellow MRC baseline runs, all flows 0% loss
with clean probes)**: 5–8 of 16 EVs per flow ended in `assumed_bad`
with `last_loss_ratio` 0.14–0.86, `consecutive_probe_timeouts: 0`,
and the demoted EVs always had dramatically fewer `per_ev_sent`
packets than their plane peers (e.g. 30–55 vs ~125).

**Root cause was two stacked bugs**:

1. **Receiver-derived `expected` fallback** (commit `d37d70e`).
   `loss_compute.apply_loss_report` fell back to the receiver's
   `rec.expected = max_seq − min_seq + 1` when no `SentWindow`
   could be paired against a `LossReport` (first 1–2 windows of a
   flow, or wall-clock skew bursts beyond `max_window_skew_ms`).
   Under packet-level EV spray that field is the **flow-global**
   seq span seen on a single EV, which is a strict and typically
   wildly inflated upper bound: `seen=10` on an EV whose seqs span
   0..400 yields a 97.5% phantom loss ratio. Two such reports
   crossed `loss_demote_consecutive` and demoted healthy EVs.
   *Fix:* skip the plane entirely when no sender-side denominator
   is available; the `fell_back_to_receiver_expected` counter is
   kept for diagnostics but now means "skipped" instead of
   "attributed via rec.expected". Wire format unchanged.

2. **Window-edge straggle** (commit `5093246`). After (1), residual
   phantom demotes still appeared in flows where one or two EVs
   had skewed `per_ev_sent`. Cause: with 16 EVs at ~370 pps/flow
   and 200ms loss windows, each EV accumulates ~5 packets per
   window. A single packet straddling a window boundary (sent in
   sender-window N but received in receiver-window N+1, or vice
   versa) shows up as ~20% apparent loss in the paired report.
   The `seen < sent` case can't be clamped — it's structurally
   indistinguishable from real loss. With the old defaults
   (`loss_threshold=0.05`, `loss_demote_consecutive=2`) two
   unlucky windows in a row tripped the demote, and the
   redistribution after demote skewed neighbor EVs' counts
   further, cascading. *Fix:* lift defaults to
   `loss_threshold=0.25`, `loss_demote_consecutive=3`. Three
   consecutive >25% windows on a healthy EV from random straggle
   is statistically negligible. Real EV failures (interface
   shutdown, spine down) produce ~100% loss on affected EVs and
   demote in `3 * loss_window_ms = 600ms`, matching the probe
   path's `3 * probe_interval_ms = 600ms` floor — neither path
   becomes the long pole.

**Lab confirmation**: post-fix re-runs of `green-mrc-baseline` and
`yellow-mrc-baseline` show 16/16 EVs across all 8 flows, 0% loss,
balanced per-plane distribution.

**Lessons / invariants worth pinning down**:
- The receiver's `expected_local = max_seq − min_seq + 1` field is
  a *strict upper bound* under packet-level spray, never a usable
  denominator. The receiver still emits it for wire compatibility
  but the sender ignores it.
- Tune loss-window thresholds to the **window-edge straggle noise
  floor**, not to the lossy-fabric SLO. `loss_threshold` lower than
  `1 / (avg_packets_per_ev_per_window)` is below the noise floor
  by construction. At 5 pkts/EV/window, anything under 20% is
  noise.
- The probe path's `probe_fail_threshold * probe_interval_ms` is
  the lower bound on detection latency for a hard EV failure;
  loss-window tuning should aim to match it, not beat it (cheap
  detection latency is bought with false positives, which cascade).


### RESOLVED: SO_REUSEPORT cascade on collective scenarios

**Symptom (4p-4x8 + 4p-8x16, every `health_aware_mrc` scenario where
every host is both sender AND receiver — `*-all-to-all`,
`*-allreduce-ring`)**: ~16 of 32 EVs per flow demoted to
`assumed_bad` despite 0% data-plane loss; per-plane traffic spreads
5–7×; the demote pattern was deterministic per-tenant.

**Root cause was three stacked bugs**, fixed in PR #1 (merged
2026-05-20). Same wire format, same scenario YAML — entirely a
host-side / orchestrator-side cure.

1. **SO_REUSEPORT reply misdelivery (the cascade)**.
   When N flows on the same src_host all ran their own
   `SenderMrcAgent`, every agent bound `(::, SPRAY_REPORT_PORT)`
   with `SO_REUSEPORT`. The Linux kernel hashes inbound replies
   across the bound socket set, so a probe reply destined for
   flow A's agent gets delivered to flow B's agent ~(N−1)/N of
   the time. B has no pending probe matching that seq → drops it
   as unknown → A's probe times out → demote.
   *Fix:* one MRC daemon process per src_host owns the single
   shared reply socket and dispatches inbound replies to per-flow
   `SenderMrcAgent` instances by peer source address (`recvfrom`
   recovers `dst_id`). Data senders no longer run their own
   agent; they read EV health from the daemon's snapshot file
   `/dev/shm/srv6-mrc/<src_host>/<tenant>_<dst_id:02d>.json`
   via the new `mrc_snapshot:<path>` policy. See
   `docs/mrc-daemon-design.md` and `srv6_mrc/mrc/daemon.py`.

2. **Sniffer egress mis-attribution**. Each receiver's per-NIC
   sniffer listens promiscuously, so on hosts that are also
   senders it captures *outbound* probe and data packets. When
   `inner_dst != self`, those captured-egress packets had been
   miscounted as "received but unexpected", inflating the orphan
   warning counter and (worse) feeding the receiver's own loss
   model with bogus arrivals. *Fix:* receiver precomputes its
   own inner address and drops captures where `inner_dst != self`
   before any further processing. New `TestSnifferEgressFilter`
   pins this.

3. **Orphan daemon accumulation across runs**. `Popen.terminate()`
   on the local `docker exec` client does **not** propagate
   SIGTERM through dockerd into the in-container `spray` process
   — the exec session detaches the child from the local client's
   signal group. Each `srctl run` therefore leaked one MRC daemon
   per src_host. Orphans accumulated, all bound to UDP/9997 with
   `SO_REUSEPORT`, so successive runs reproduced bug (1) at the
   process level: only the orphan from the active run wrote
   snapshots its own readers consumed, while N−1 orphans
   silently swallowed reply traffic. Loss climbed monotonically
   across consecutive identical runs (lab: 0% → 13% → 38% →
   60% → 75%+ over 4 runs without code changes). *Fix:*
   `mrc/run.py` daemon teardown now sends SIGTERM **inside the
   container** via `docker_exec(host, ["pkill", "-TERM", "-x",
   "spray"])` before draining the daemon's stdout. On
   `TimeoutExpired`, escalates to `pkill -KILL` in-container
   so orphans cannot survive even pathological cases. New
   `TestMrcDaemonTeardown.test_pkill_invoked_per_daemon_host_on_teardown`
   pins the contract.

**Lab confirmation (4p-4x8)**:
- `yellow-all-to-all` over 4 consecutive runs: per-plane loss
  steady at ~0.05–0.30%, no run-over-run drift.
- `docker exec yellow-host00 pgrep -x spray` returns 0 between
  runs — zero orphan daemons.
- tcpdump on `eth1` confirms traffic stops cleanly when `srctl
  run` returns.

**Invariants pinned by this episode**:
- **Never bind `(::, SPRAY_REPORT_PORT)` with `SO_REUSEPORT`
  more than once per host.** The MRC daemon is the single
  authoritative reply-socket owner per src_host. Data senders
  read snapshots, never bind the reply port.
- **Subprocess SIGTERM does not cross `docker exec`.** Any
  in-container long-lived process spawned via `docker_exec_async`
  must be torn down with an explicit `docker exec <host> pkill
  -TERM -x <name>`. `Popen.terminate()` alone leaks orphans.
- **Sniffer-based receivers must filter on `inner_dst == self`**
  on hosts that are also senders, before any accounting. Egress
  capture is unavoidable (promiscuous sockets) but trivially
  filterable once you know your own address.
- **`docker exec` stdout is unreliable for large payloads at
  container exit.** Dockerd's stdout multiplex stream silently
  drops trailing frames if the container's monitor goroutine
  observes exit before draining the stream. Symptom on
  `green-all-to-all` (8 hosts): 7 daemons produced empty stdout,
  1 produced stdout truncated at ~16 KiB (`Expecting ':' delimiter
  at char 16390`) — frame-aligned, not arbitrary truncation. Any
  in-container long-lived process whose final output exceeds a
  few KiB must write the payload to a file (e.g. `/dev/shm/...`)
  and have the orchestrator retrieve it via a second `docker exec
  cat`. The MRC daemon does this for `final_report.json`; the
  orchestrator falls back to stdout only for back-compat with
  pre-fix daemon images.
- **`docker exec cat` immediately after the previous exec
  session ends can transiently return rc=1.** Even when the file
  exists (confirmed post-hoc via `ls -la` showing the file with a
  timestamp matching the run), a fresh `docker exec <host> cat
  /path` issued within microseconds of the previous exec session
  terminating can fail with "no such file or directory". Working
  theory is dockerd exec-session teardown overlapping with the
  new exec session — the mount-namespace view is briefly
  inconsistent. Cure: retry up to 3 times with a small backoff
  (100ms suffices; the race clears within hundreds of
  microseconds in practice). The orchestrator's daemon teardown
  uses this pattern; any new code that does back-to-back
  `docker_exec_async` → drain → `docker_exec(cat ...)` on the
  same container should do the same. The failure-mode message
  on retry exhaustion includes the cat command's stderr so a
  real "file truly never written" bug is distinguishable from
  a stuck race.



- Renaming subcommands or files without sweeping `README.md`, `AGENTS.md`,
  and the relevant `docs/*.md`.
- Adding kernel-ECMP / multipath as a sender plane-selection mechanism.
- Putting plane identity anywhere other than the outer SID list.
- Reusing `e<port>` numbering instead of `e<NIC ordinal>`.
- Committing into `topologies/<name>/topology.clab.yaml` or
  `topologies/<name>/config/` directly (they're generated).
- Creating `CLAUDE.md` alongside this file. One file, this one.

## Style

- Terse, technical commit messages and PR bodies.
- Code comments: explain *why*, not *what*. Mention invariants that
  would otherwise look arbitrary.
- Don't add emojis to files or output unless asked.

## Knowledge graph (graphify)

A graphify-built knowledge graph of the repo lives under
`graphify-out/` (gitignored). Useful for "where does X connect to Y"
questions, finding code↔design-doc bridges, and auditing the
god-node neighbourhood (`EVStateTable`, `SenderMrcAgent`,
`ReceiverMrcAgent`, `MrcDaemon`, `Topology`, etc.) before refactors.

- `graphify-out/graph.html` — interactive node-level viz, open in
  any browser.
- `graphify-out/GRAPH_REPORT.md` — god nodes, surprising
  connections, suggested questions.
- `graphify-out/graph.json` — raw graph (consumed by
  `graphify query "..."` for BFS/DFS traversal and by the MCP
  server if wired up).

Pruned artefacts the current graph deliberately collapses (every
rebuild must redo this, or the graph drowns in template noise):

1. `topologies/<topo>/config/<node>/config_db.json` — the AST
   extractor walks every JSON key. Each per-node dir is collapsed
   to one synthetic vertex (label = node name, e.g. `p0-leaf00`),
   with a `Topology <name>` root vertex containing them via
   `contains` edges. Saves ~44k noise nodes on a 4p-4x8 + 4p-8x16
   pair.
2. `generators/sonic_vs_port_table.json` — config-generator
   template, same key-explosion pattern. Collapsed to a single
   `sonic_vs_port_table.json (template)` vertex. ~225 noise
   nodes.

Both pruned subgraphs were verified to have **zero bridge edges**
to the MRC code subgraph before collapse, so no signal was lost.

If you regenerate the graph and add new JSON data templates under
`generators/` or new topology variants under `topologies/`, apply
the same prune (see `graphify-out/cost.json` for the prior run's
manifest; the prune logic itself was ad-hoc Python over
`graph.json`, not yet codified — TODO: ship a
`scripts/graphify_prune.py` so the cleanup is repeatable).

The graph's audit trail (EXTRACTED vs INFERRED edges) is
trustworthy with one known caveat: the AST extractor emits
`confidence=INFERRED, confidence_score=0.5, relation=uses` for
plain class references that should be EXTRACTED. Filter these out
when reading "INFERRED edge count" warnings — only edges with
`confidence_score >= 0.55` are genuine LLM-reasoned guesses.

## Quick-start verification

After any change touching addressing / SID shape / routing:

```
make image                                                   # one image serves every topology
make regen                                                   # generate topo + configs
make deploy                                                  # containerlab deploy
make config                                                  # push SONiC configs (auto-verifies + repairs)
make host-routes                                             # full-mesh per-tenant host kernel routes
make scenario SCEN=green-mrc-baseline                        # green tenant baseline (MRC)
make scenario SCEN=yellow-baseline                           # yellow tenant baseline (round_robin)

docker exec -d yellow-host07 spray --role recv
docker exec yellow-host00 spray --role send \
    --dst-id 7 --rate 1000pps --duration 4s
```

`spray` recv (foreground variant) should show roughly balanced counts
across 4 planes.

`make config` will print a "Verifying leaf tier" section at the end
that confirms every leaf has the expected number of `seg6local`
entries in the kernel FIB (expected count is derived from each node's
generated `frr.conf`, so it works for any topology + tenant mix).
Mismatched leaves are auto-re-pushed up to `VERIFY_RETRIES` times
(default 3). If you ever suspect a leaf is mis-programmed without
re-deploying, run `make verify-config` for a read-only check that
also repairs.

For MRC end-to-end:

```
make scenario SCEN=green-mrc-baseline    # green tenant, MRC enabled, no faults
make scenario SCEN=yellow-baseline       # yellow tenant, round_robin, no faults
make scenario SCEN=green-mrc-plane-loss  # 1% loss on plane 2 + MRC demote
make scenario SCEN=green-mrc-plane-latency  # plane 2 +5ms + MRC (RTT not yet a demote signal)

# Health-aware MRC variants (turn the agents on):
make scenario SCEN=green-mrc-baseline     # clean fabric + MRC; should match baseline
make scenario SCEN=green-mrc-plane-loss   # 5% loss on plane 2 + MRC; loss should
                                          # drop well below round_robin after demote
make scenario SCEN=green-mrc-plane-latency # 10ms on plane 3 + MRC; currently no
                                          # demote (RTT not a signal yet)
make scenario SCEN=green-mrc-ev-spray     # per-EV sender control (green)
make scenario SCEN=yellow-mrc-ev-spray    # per-EV sender control (yellow)
```

Expect ~0% loss on baselines, balanced per-plane counts, low
`max_reorder_distance`. Yellow's per-flow `reord` is a bit higher
than green's (extra host-side decap stage adds scheduling jitter)
but loss and balance are identical. On the green-mrc-* runs, look in
the per-flow JSON for `per_plane_sent` to confirm the demotion shifted
traffic off the affected plane.
