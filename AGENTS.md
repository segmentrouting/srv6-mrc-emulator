# AGENTS.md — context for AI coding assistants

Read this first when picking up work in this repository. It captures
the non-obvious invariants and gotchas that aren't visible from a single
file or from the README alone.

For the human-facing tour: see `README.md` (overview), `docs/quickstart.md`
(deploy/run), `docs/design-fabric.md`, `docs/design-mrc.md`,
`docs/spray-protocol.md`, and `docs/design-appendix.md`.

---

## What this lab is

A 4-plane SRv6 fabric (8 spines × 16 leaves per plane = 128 fabric nodes)
on docker-sonic-vs + Containerlab, plus 32 alpine hosts (16 green + 16
yellow). It demonstrates the MRC + SRv6-spray model: one logical flow
fans out across all 4 planes by varying *only* the outer SID list.

Tenants:

- **green** — hybrid: leaf does encap+uDT6 decap in `Vrf-green`. Anycast
  inner dst `2001:db8:bbbb:<NN>::2` on all 4 NICs (`nodad`).
- **yellow** — host-based: leaf does encap+uA(host-port); host runs 4
  `seg6local End.DT6` (one per plane NIC) for decap. Anycast inner dst
  `2001:db8:cccc:<NN>::2` on all 4 NICs and on `lo` (`nodad`). Mirrors
  green's anycast plan with `bbbb`→`cccc` (Phase 1a).

## Repo layout

```
srv6_fabric/           Python package
  topo.py              fabric constants + addressing helpers (reads topo.yaml)
  runner.py, policy.py, reorder.py, netem.py, report.py, health.py
  cli/spray.py         userspace SRv6 packet generator (CLI: `spray`)
  cli/routes.py        static SRv6 route management   (CLI: `routes`)
  mrc/
    run.py             scenario orchestrator           (CLI: `run-scenario`)
    scenario.py        scenario YAML schema + executor (incl. `mrc:` block)
    agent.py           SenderMrcAgent / ReceiverMrcAgent + env-loader
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

host-image/Dockerfile  alpine + scapy + pip-installed srv6_fabric
scripts/config.sh      push configs to running containers
tests/                 329 unit tests mirroring srv6_fabric/ layout
docs/                  consolidated design + runbook docs
results/               scenario output JSON (gitignored)
Makefile               operator workflow entry point
```

## Source of truth

`topologies/<name>/topo.yaml` declares the topology: planes, spines,
leaves, container images, clab topology name. The generator
(`generators/fabric.py --topo <path>`) reads it and emits
`topology.clab.yaml` + the per-node `config/*` SONiC config snippets in
the same directory. The `srv6_fabric.topo` runtime module also reads
it at import time (via the `SRV6_TOPO` env var, defaulting to
`topologies/4p-8x16/topo.yaml`).

**Never hand-edit generated files**:
- `topologies/<name>/topology.clab.yaml`
- `topologies/<name>/config/<node>/{config_db.json,frr.conf}`

Files that must stay in sync because they share addressing/SID-list shape:

- `generators/fabric.py` (writes routes into SONiC + host configs)
- `srv6_fabric/cli/routes.py` (parses + writes host-side `ip -6 route`
  SRv6 routes)
- `srv6_fabric/cli/spray.py` (CLI; delegates encoding to
  `srv6_fabric.runner`)
- `srv6_fabric/topo.py` (fabric constants + `usid_outer_dst()` — the
  SID-list builder the runner uses)
- `srv6_fabric/runner.py` (wire format: `!QB` seq+plane, 32B pad,
  sport=dport=SPRAY_PORT)

If you change the SID-list shape, the per-plane block layout, or any
tenant naming/addressing, all of these must be updated. The
`test_reference_pairs_match_spray` test in `tests/test_topo.py` locks
the `srv6_fabric.topo` ↔ `spray` reference-pairs map in sync.

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
- **Phase 1b step 1 (EV-spray data-path):** done for green; yellow
  TODO. `EvSpray` policy varies BOTH plane and spine per packet via
  per-packet outer-DA rotation. Reports include `per_ev_sent` keyed
  `"P<p>:S<s>"`. No health awareness — every EV is always active.
  Scenarios shipped: `green-ev-spray.yaml` (full fan-out) and
  `green-ev-spray-n2.yaml` (narrow). **Yellow parity outstanding:**
  add `yellow-ev-spray.yaml` and `yellow-ev-spray-n2.yaml` (pure
  YAML; runner/policy are tenant-agnostic), then lab-validate that
  yellow's extra `e009` leaf→host hop survives per-packet spine
  rotation. Invariant 7 means yellow uses a 2-uSID list per packet
  while green uses 1 — the spine hextet position is the same, but
  the underlying SID list length isn't.
- **Phase 1b step 2 (probes on scapy raw-socket):** not started.
  Today's MRC probes use a UDP socket + per-plane `encap.red` route,
  which only has plane granularity (4 routes), not EV granularity
  (4 * NUM_SPINES routes). Step 2 moves the probe TX path to scapy
  raw-socket using the same `usid_outer_dst()` helper as the data
  path. Must work for both tenants from the start. Prerequisite for
  step 3.
- **Phase 1b step 3 (per-EV health):** not started. New policy that
  extends `EvSpray` with an "active EV mask" read from a per-EV
  health table (analogous to `EVStateTable` for planes). On loss
  detection at an EV granularity, the sender skips that EV on its
  round-robin turn. Tenant-agnostic from day one — when this lands,
  ship both `green-mrc-ev-spray-*` and `yellow-mrc-ev-spray-*`
  scenario sets together; don't repeat the step-1 yellow-lag pattern.

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

### `routes` (`srv6_fabric/cli/routes.py`)

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

### `spray` (`srv6_fabric/cli/spray.py`)

Userspace SRv6 sprayer, image `alpine-srv6-scapy:1.0`. The image
pip-installs the `srv6_fabric` package at build time, so `spray` lives
at `/usr/local/bin/spray` inside every host container — no bind mounts
needed for code. The active topology's `topo.yaml` is bind-mounted
into each host container at runtime by containerlab (per the
`binds:` block in `topology.clab.yaml`), landing at
`/etc/srv6_fabric/topo.yaml`; the image exports `SRV6_TOPO` pointing
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
it via `srv6_fabric.mrc.agent.load_configs_from_env()` into
`(AgentConfig, EVStateConfig | None)`. Unknown keys, bad JSON, or
non-object payloads fail loud rather than reverting to defaults.

### `run-scenario` (`srv6_fabric/mrc/run.py`)

Docker-host-side orchestrator for MRC scenarios. Loads a scenario YAML,
applies fault injection via `nsenter ... tc qdisc add ...` against host
veths, runs `spray` send/recv inside the relevant containers via
`docker exec`, and merges the JSON output into a `ScenarioReport`.

```
run-scenario topologies/4p-8x16/scenarios/baseline.yaml --verbose
run-scenario topologies/4p-8x16/scenarios/plane-loss.yaml --dry-run
```

`--dry-run` prints the plan plus the exact `nsenter ... tc qdisc add ...`
argvs that would be invoked — useful for verifying fault targeting
without touching the lab.

### MRC architecture (current build)

`health_aware_mrc` is the live MRC policy. It reads weights from an
`EVStateTable` shared with a `SenderMrcAgent` running in the same
process. The agent drives four background threads:

- emit-probes (one PROBE per plane every `probe_interval_ms`)
- probe-replies-RX (one socket per plane; feeds `EVStateTable.record_probe_result`)
- timeout-sweep (declares in-flight probes lost after `probe_timeout_ms`)
- loss-report-RX (consumes per-plane loss reports from the receiver and feeds the same table)

The receiver-side counterpart (`ReceiverMrcAgent`) runs three threads:
probe-RX-per-plane (replies in-band on the same plane), loss-emit (one
round per `loss_window_ms` per known sender), and the data-record hook
called from the existing receiver hot loop via `on_packet=`.

Detection signals fused into `EVStateTable`:

1. **EV Probes** — out-of-band UDP per plane, RTT-stamped. Demotes
   after `probe_fail_threshold` consecutive timeouts.
2. **Receiver-side loss reports** — receiver computes per-plane
   `(seen, expected_local, max_gap)` and unicasts to the sender; sender
   compares against its own per-plane sent windows. Demotes after
   `loss_demote_consecutive` windows over `loss_threshold`.

Demotions are subject to an `min_active_planes` floor (OCP's
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

Yellow MRC scenarios (`yellow-mrc-*.yaml`) are not yet written and are
the next milestone.

### Gotcha: rebuild the image whenever `srv6_fabric/` changes

`make image` runs `docker build`; layer caching has occasionally
shipped a stale `srv6_fabric` package inside the container even after
a clean `git pull`. Symptom is line-number mismatches between local
tracebacks and container tracebacks. When in doubt:

```bash
docker build --no-cache -f host-image/Dockerfile -t alpine-srv6-scapy:1.0 .
docker run --rm alpine-srv6-scapy:1.0 \
    wc -l /usr/lib/python3.12/site-packages/srv6_fabric/mrc/agent.py
```

The line count should match the local `srv6_fabric/mrc/agent.py`.
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
`make scenario SCEN=baseline`, which uses spray (sender-side plane
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
  `srv6_fabric/report.py`.

## Things to avoid

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

## Quick-start verification

After any change touching addressing / SID shape / routing:

```
make image                                                   # one image serves every topology
make regen                                                   # generate topo + configs
make deploy                                                  # containerlab deploy
make config                                                  # push SONiC configs (auto-verifies + repairs)
make host-routes                                             # full-mesh per-tenant host kernel routes
make scenario SCEN=baseline                                  # green tenant baseline
make scenario SCEN=yellow-baseline                           # yellow tenant baseline

docker exec -d yellow-host15 spray --role recv
docker exec yellow-host00 spray --role send \
    --dst-id 15 --rate 1000pps --duration 4s
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
make scenario SCEN=baseline           # green, no faults, round_robin
make scenario SCEN=yellow-baseline    # yellow, no faults, round_robin
make scenario SCEN=plane-loss         # 1% loss on plane 2 (green, round_robin)
make scenario SCEN=plane-blackhole    # plane 2 unreachable (green, round_robin)
make scenario SCEN=plane-latency      # plane 2 +5ms (green, round_robin)
make scenario SCEN=hash5tuple         # hash spray policy (green)

# Health-aware MRC variants (turn the agents on):
make scenario SCEN=green-mrc-baseline     # clean fabric + MRC; should match baseline
make scenario SCEN=green-mrc-plane-loss   # 5% loss on plane 2 + MRC; loss should
                                          # drop well below plane-loss.yaml after demote
make scenario SCEN=green-mrc-plane-latency # 10ms on plane 3 + MRC; currently no
                                          # demote (RTT not a signal yet)
```

Expect ~0% loss on baselines, balanced per-plane counts, low
`max_reorder_distance`. Yellow's per-flow `reord` is a bit higher
than green's (extra host-side decap stage adds scheduling jitter)
but loss and balance are identical. On the green-mrc-* runs, look in
the per-flow JSON for `per_plane_sent` to confirm the demotion shifted
traffic off the affected plane.
