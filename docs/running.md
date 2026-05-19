# Running MRC

Practical guide for running MRC scenarios and tests against a deployed
4-plane fabric. For *what* MRC is and the design rationale, see
`design-mrc.md`. For *how to read* the resulting JSON / ASCII reports,
see `results-format.md`.

All commands below assume you are at the repo root with the fabric
already deployed (`make deploy`).

## Unit tests

Pure-Python, stdlib unittest, no network or container access needed.
Run from the repo root:

```bash
make test
# or directly:
PYTHONPATH=. python3 -m unittest discover -s tests -t .
```

Expected: 329+ tests pass in ~1.5s. Use this as a fast smoke check
after any edit to `srv6_mrc/*` or `srv6_mrc/cli/spray.py`.

To run a single test module or case:

```bash
PYTHONPATH=. python3 -m unittest tests.test_report
PYTHONPATH=. python3 -m unittest tests.test_report.TestMatchingHappyPath
PYTHONPATH=. python3 -m unittest tests.test_report.TestMatchingHappyPath.test_single_flow_matched
```

## Manual two-host spray (no orchestrator)

Useful for ad-hoc debugging or sanity-checking a single host pair. Two
terminals on the lab host.

**Receiver** (start first; self-exits after `--idle-timeout`):

```bash
docker exec green-host15 spray --role recv --idle-timeout 6s
```

**Sender** (start after the receiver banner appears, ~1s settle):

```bash
docker exec green-host00 spray --role send \
    --dst-id 15 --rate 1000pps --duration 5s
```

Add `--json` to either side for machine-readable output (pipe through
`jq .` to validate).

Pick a different spray policy with `--policy`:

```bash
docker exec green-host00 spray --role send \
    --dst-id 15 --rate 1000pps --duration 5s --policy hash5tuple
```

With `hash5tuple` a single flow pins to a single plane — per-plane
sent counts will be unbalanced and `reord` should be 0.

## Scenario orchestrator

`run-scenario` (`srv6_mrc/mrc/run.py`) reads a YAML scenario, spawns
receivers, runs senders in parallel via `docker exec`, applies/reverts
`tc netem` faults, merges results, and renders an ASCII + JSON report.

**Dry run** (parses YAML, prints planned flows and netem argvs, no
containers touched):

```bash
run-scenario topologies/4p-8x16/scenarios/green-mrc-baseline.yaml --dry-run
# or via make:
make scenario SCEN=green-mrc-baseline   # add --dry-run by editing the Makefile target
```

**Real run**:

```bash
run-scenario topologies/4p-8x16/scenarios/green-mrc-baseline.yaml \
    --report results/green-mrc-baseline.json
# or:
make scenario SCEN=green-mrc-baseline
```

Scenarios that inject faults need `sudo` because `tc netem` is applied
via `nsenter` into container network namespaces:

```bash
sudo run-scenario topologies/4p-8x16/scenarios/green-mrc-plane-loss.yaml \
    --report results/plane-loss.json
```

The orchestrator always reverts netem in a `finally` block. Verify
between runs:

```bash
for h in green-host00 green-host15; do
  echo "=== $h ==="
  for nic in eth1 eth2 eth3 eth4; do
    docker exec $h tc qdisc show dev $nic
  done
done
```

Every line should be `qdisc noqueue` or `qdisc pfifo_fast` — no
`netem`. If any `netem` qdisc is left behind, clear it with:

```bash
docker exec <host> tc qdisc del dev <nic> root
```

## Bundled scenarios

All scenarios live in `topologies/4p-8x16/scenarios/`. They split into
five families based on what part of the system they exercise; the
"Spray policy" column is the key axis (it picks how packets fan out;
everything else is window-dressing on top).

Quick-pick guide:

- **First time? Or smoke test after a redeploy:** `baseline` (green)
  / `yellow-baseline`. Confirms the fabric is alive.
- **EV-spray data path (no health feedback):** `green-ev-spray` or
  `yellow-ev-spray`. Validates the per-packet outer-SID rotation
  hits all 32 entropy values without MRC in the loop.
- **MRC end-to-end (per-plane only):** `green-mrc-baseline` /
  `yellow-mrc-baseline`. Smallest scenario that actually runs the
  EV-state machine and loss feedback loop.
- **MRC end-to-end (full per-EV granularity, phase 1b step 2):**
  `green-mrc-ev-spray` / `yellow-mrc-ev-spray`. Demote a single
  (plane, path) cell, not a whole plane.
- **Failure injection with netem (programmatic, no manual `ip link`):**
  the `*-plane-loss` / `*-plane-latency` / `plane-blackhole`
  scenarios use `tc qdisc add` on a chosen plane's veth.

| Scenario | Family | Tenant | Spray policy | `paths_per_plane` | MRC | Faults | Expect |
|---|---|---|---|---|---|---|---|
| `baseline.yaml` | smoke | green | `hash5tuple` (default) | n/a | off | none | loss≈0, modest reord from per-plane jitter |
| `yellow-baseline.yaml` | smoke | yellow | `hash5tuple` | n/a | off | none | as above on the host-decap data path |
| `hash5tuple.yaml` | smoke | green | `hash5tuple` (explicit) | n/a | off | none | each flow pins a single plane; reord = 0 |
| `plane-latency.yaml` | static fault | green | `hash5tuple` | n/a | off | netem delay on one plane | loss≈0, `reord_max` ↑↑ |
| `plane-loss.yaml` | static fault | green | `hash5tuple` | n/a | off | 5% random loss on one plane | loss≈(plane_loss% / 4), modest reord |
| `plane-blackhole.yaml` | static fault | green | `hash5tuple` | n/a | off | 100% drop on one plane | loss≈25%, target plane rx=0, reord ↓ |
| `green-ev-spray.yaml` | EV-spray | green | `ev_spray` | 8 | off | none | balanced across all 32 EVs in `per_ev_sent` |
| `green-ev-spray-n2.yaml` | EV-spray | green | `ev_spray` | 2 | off | none | per-pair tcpdump alternates between 2 spine hextets |
| `yellow-ev-spray.yaml` | EV-spray | yellow | `ev_spray` | 8 | off | none | balanced 32 EVs; validates yellow's 2-uSID outer rotates correctly |
| `yellow-ev-spray-n2.yaml` | EV-spray | yellow | `ev_spray` | 2 | off | none | host-side decap stays clean across both spine choices |
| `green-mrc-baseline.yaml` | MRC plane-coarse | green | `health_aware_mrc` | 1 (default) | on | none | EV table = 4 cells all GOOD; no-op on clean fabric |
| `yellow-mrc-baseline.yaml` | MRC plane-coarse | yellow | `health_aware_mrc` | 1 (default) | on | none | as above; also exercises Phase 1a collapsed rx socket |
| `green-mrc-plane-loss.yaml` | MRC plane-coarse | green | `health_aware_mrc` | 1 | on | netem loss on one plane | EV table demotes the affected plane → ASSUMED_BAD; loss% drops post-demote |
| `yellow-mrc-plane-loss.yaml` | MRC plane-coarse | yellow | `health_aware_mrc` | 1 | on | netem loss on one plane | as above on host-decap path |
| `green-mrc-plane-latency.yaml` | MRC plane-coarse | green | `health_aware_mrc` | 1 | on | netem delay on one plane | RTT p99 climbs on affected plane; latency-based signal only (no demote unless config tuned) |
| `yellow-mrc-plane-latency.yaml` | MRC plane-coarse | yellow | `health_aware_mrc` | 1 | on | netem delay on one plane | as above |
| `green-mrc-ev-spray.yaml` | MRC per-EV | green | `health_aware_mrc` | 8 | on | none (manual link-shut recommended) | exercises phase 1b step 2: per-EV `seen/sent` accounting; demote a single (plane, path) cell |
| `yellow-mrc-ev-spray.yaml` | MRC per-EV | yellow | `health_aware_mrc` | 8 | on | none (manual link-shut recommended) | as above on host-decap path |

Spray-policy cheatsheet:

- `hash5tuple` — pin each flow to one plane by 5-tuple hash. No
  spraying inside a flow. Good for smoke tests; pinpoints whether
  the data path itself works.
- `ev_spray` — rotate plane *and* spine every packet. Pure data-
  plane fan-out across `4 * paths_per_plane` entropy values. No
  health awareness; broken EVs keep getting traffic.
- `health_aware_mrc` — same per-packet rotation as `ev_spray`, but
  the weight grid is driven by the EV state machine. Broken EVs
  drop to weight 0; survivors absorb the slack. This is the policy
  that actually consumes MRC loss feedback and probe RTT.

Family cheatsheet:

- **smoke** — clean fabric, simple policy. If this is broken,
  nothing else will work; start here after every redeploy.
- **static fault** — netem on one plane, no MRC. Validates the
  fabric handles a degraded plane gracefully when the spray policy
  is health-blind. Establishes a "without MRC" reference number.
- **EV-spray** — per-packet plane+spine rotation, no MRC. Validates
  the 32-EV fan-out plumbing (outer SID rebuild, yellow's second
  uSID, hash-derived spine subsets when `paths_per_plane < N`).
- **MRC plane-coarse** — MRC on, `paths_per_plane=1`. Each plane is
  a single EV from the policy's perspective. Tests pre-Phase-1b
  loss/probe semantics; useful regression baseline.
- **MRC per-EV** — MRC on, `paths_per_plane=8`. Each plane is 8
  independent EVs. Tests phase 1b step 2: per-EV `(plane, path)`
  loss attribution, per-EV demote, surviving siblings absorbing the
  load. Manual link shut on `p<P>-spine<S>` is the recommended
  failure: it maps to exactly 1/32 of the EV grid.

Run any scenario with `sudo make scenario SCEN=<name>` (drop the
`.yaml` suffix). See each file for the exact flow list, rates, and
netem specs.

### Spraying with EV-spray

The `ev_spray` policy varies BOTH plane and spine per packet — every
packet gets a new outer SID rotation, so a single flow walks `4 * N`
distinct leaf-to-spine paths (entropy values, EVs) where `N` is
`paths_per_plane`. Default `N = NUM_SPINES` (8 on the 4p-8x16
topology).

Two ways to enable it:

```bash
# 1. Scenario YAML (preferred): top-level paths_per_plane + ev_spray
#    in the flow's policy field.
sudo make scenario SCEN=green-ev-spray         # N=8, full fan-out
sudo make scenario SCEN=green-ev-spray         # N=8, full fan-out (use this; -n2 variant has been retired)

# 2. Manual two-host spray (no orchestrator). Start the receiver
#    first (see "Manual two-host spray" above), then:
docker exec green-host00 spray --role send \
    --dst-id 15 --rate 1000pps --duration 5s --policy ev_spray:2
# or use --paths-per-plane to override an env-set default:
#   --policy ev_spray --paths-per-plane 4
```

What to look for in the report (`results/<scenario>.json`):

- `per_plane_sent` — still balanced across 4 planes (plane rotates
  every packet).
- `per_ev_sent` — keyed `"P<p>:S<s>"`; with `paths_per_plane=8` every
  pair shows ~125 packets per EV at 1000pps/5s; with `paths_per_plane=2`
  exactly two spines are populated per pair (the subset is
  hash-derived from the (src, dst) pair, so different pairs land on
  different spine subsets).

Example filtered `results`:
```bash
jq '.flows[] | {flow: (.src_host + " -> " + .dst_host), per_ev_sent}' \
   results/green-ev-spray.json
```

```bash
jq '.flows[] | {
  flow: (.src_host + " -> " + .dst_host),
  spines_used: (.per_ev_sent | keys | map(split(":")[1]) | unique),
  per_ev_sent
}' results/green-ev-spray-n2.json
```

```bash
jq -r '.flows[] as $f | $f.per_ev_sent | to_entries[] |
       "\($f.src_host) -> \($f.dst_host)  \(.key)  \(.value)"' \
   results/green-ev-spray-n2.json | column -t
```

What to look for on the wire (tcpdump on a leaf eth1..eth4):

```bash
# On a leaf SONiC container:
docker exec -it sonic-leaf-00-p0 bash -lc \
  "tcpdump -i Ethernet0 -nn -c 20 udp port 9999"
# Outer DA hextets `fc00:000<P>:f00<S>:e00<L>:d000::` should show
# the `<S>` digit changing packet-to-packet. With paths_per_plane=2
# you'll see exactly 2 distinct `<S>` values per src/dst pair.
```

Precedence for `paths_per_plane`:
`ev_spray:N` in `--policy` > `--paths-per-plane` flag >
`SRV6_PATHS_PER_PLANE` env > policy default (`NUM_SPINES`). The
scenario orchestrator propagates the YAML's `paths_per_plane` via
env, so flows that say only `policy: ev_spray` pick it up.

## Writing a new scenario

Minimum shape (see existing files for full options):

```yaml
name: my-scenario
description: |
  One sentence about what this proves.

flows:
  - src: green-host00
    dst: green-host15
    rate: 1000pps
    duration: 5s
    policy: round_robin

faults:
  - target: { host: green-host00, plane: 2 }
    spec: loss 5%

report:
  out: results/my-scenario.json
```

Drop it in `topologies/<name>/scenarios/` and invoke with:

```bash
make scenario SCEN=my-scenario
```

Fault `target` accepts `plane: N` (all hosts), `host: NAME` (all four
NICs on that host), or both together (one specific NIC).

## Running MRC scenarios

MRC is opt-in: a scenario YAML enables it by including a top-level
`mrc:` block (even an empty one), and the senders use it by selecting
`policy: health_aware_mrc`. Three bundled green-tenant scenarios under
`topologies/4p-8x16/scenarios/`:

| Scenario | Fault | Expect |
|---|---|---|
| `green-mrc-baseline.yaml` | none | identical to `baseline.yaml` — MRC running but never demoting; per-plane sent counts balanced |
| `green-mrc-plane-loss.yaml` | 5% loss on plane 2 | plane 2 demotes within ~1–2 loss windows; total loss drops well below `plane-loss.yaml` |
| `green-mrc-plane-latency.yaml` | +10ms on plane 3 | reorder histogram comparable to `plane-latency.yaml`; **no demotion** (latency isn't a signal in the current build) |

Run them like any other scenario:

```bash
sudo run-scenario topologies/4p-8x16/scenarios/green-mrc-plane-loss.yaml \
    --report results/green-mrc-plane-loss.json
# or:
make scenario SCEN=green-mrc-plane-loss
```

### What "MRC is working" looks like in the output

For `green-mrc-plane-loss.yaml`, compare the per-flow result JSON
against the same fault under `plane-loss.yaml` (round-robin):

```bash
jq '.flows[0].per_plane_sent, .flows[0].per_plane_loss' \
    results/plane-loss.json results/green-mrc-plane-loss.json
```

- `plane-loss.json` (round_robin): `per_plane_sent` ≈ uniform across
  4 planes; `per_plane_loss` shows ~5% only on plane 2; total loss
  ≈ 1.25%.
- `green-mrc-plane-loss.json` (health_aware_mrc): `per_plane_sent`
  shows plane 2 starting at ~25% then dropping to near-zero;
  `per_plane_loss` similarly concentrated early, then minimal;
  total loss should be **substantially below 1.25%**.

If the two look identical, MRC isn't actually engaging. Common causes:

1. **Wrong image.** `make image` after pulling.
   `docker exec green-host00 spray --help` should list `--mrc` as a
   flag and `health_aware_mrc` under `--policy`.
2. **Policy not set.** Check the scenario `flows[].policy` — must be
   exactly `health_aware_mrc`.
3. **MRC block missing.** Confirm with
   `run-scenario <scenario> --dry-run | grep -i mrc` — the dry-run
   output names the mrc config it'll push.
4. **Loss too small / window too long.** Defaults need two consecutive
   windows over `loss_threshold` (5%) to demote. For very low loss
   rates, override in the scenario:

   ```yaml
   mrc:
     loss_threshold: 0.02
     loss_demote_consecutive: 1
   ```

### Known limitation: blackhole + MRC

The receiver-side loss estimator computes per-plane
`expected = max_seq − min_seq + 1`. A 100%-blackholed plane has zero
arrivals, so the loss window reports 0/0 and the plane stays UNKNOWN
on that signal. The probe channel still catches it (no replies →
`probe_fail_threshold` timeouts → demote), but the demote latency is
longer than for a partial-loss scenario. No `green-mrc-plane-blackhole.yaml`
ships today for this reason — once probe-driven demotion is exercised
in the lab we'll add it.

### Inspecting MRC state mid-run

`spray --role send` with `health_aware_mrc` doesn't print live EV
state today. If you need it, run a manual two-host spray (above) and
attach `strace -e network` or `tcpdump -i any port 9998` (loss-report
port) to confirm reports are flying. The sender's stats counters
(`reports_processed`, `planes_updated`) are also surfaced via the
agent's `stats` property, but they aren't yet plumbed into the JSON
report — that's a follow-up.

## Common failure modes

| Symptom | Likely cause | Fix |
|---|---|---|
| `KeyError: 'src_addr'` or similar in `spray` | Schema drift between `srv6_mrc/reorder.py` and consumers | Re-read `design-mrc.md` per-flow schema; both sides must use `src/dst/sport/dport/received/...` |
| Every flow shows "no flow at receiver" + matching "orphan flow" warning | IPv6 string-form mismatch (zero-padded vs RFC 5952 compressed) | Already fixed in `srv6_mrc/report.py:_canon_addr()`; if it returns, check that both sides go through `_canon_addr` |
| Sender PPS far below requested (e.g., 780 of 1000) | scapy packet build in the hot loop, host load | Not a correctness issue. Future optimization: precompute per-plane packet bytes, patch only seq+plane offsets |
| `policy_to_cli: NotImplementedError` for `health_aware_mrc` | Stale; shouldn't happen. `health_aware_mrc` is wired through `srv6_mrc.mrc.run.policy_to_cli` and `cli/spray.py` as of MRC commit-2b. If you see it, you're on an old image — rebuild with `make image`. |
| `tc qdisc show` shows leftover `netem` after a run | orchestrator crashed before revert (rare) | `docker exec <host> tc qdisc del dev <nic> root` |

## Post-redeploy validation

After any `make teardown && make deploy && make config` cycle, run
through these in order — each step gates the next:

1. `docker exec green-host00 which spray` — package installed in image
2. `docker exec green-host00 spray --help` — CLI loads cleanly
3. `docker exec green-host00 sh -c 'echo $SRV6_TOPO && cat $SRV6_TOPO | head'`
   — topo.yaml baked into image
4. Manual two-host spray (above) — fabric carries packets
5. Same with `--json` — receiver schema parses
6. Same with `--policy hash5tuple` — policy plumbing works
7. `run-scenario topologies/4p-8x16/scenarios/green-mrc-baseline.yaml --dry-run`
   — scenario parses
8. `make scenario SCEN=green-mrc-baseline` — orchestrator + merge + render pipeline works
9. Three fault scenarios in order — netem inject + revert works
10. `make scenario SCEN=green-mrc-baseline` — MRC enabled, clean fabric,
    should look like step 8's output
11. `make scenario SCEN=green-mrc-plane-loss` — MRC visibly reduces
    total loss vs `plane-loss.yaml` (compare with `jq` per above)
12. Final `tc qdisc show` sweep — clean state

If a step fails, stop and troubleshoot before proceeding.

## Phase 1a step 4: validate yellow MRC end-to-end

This is the lab side of the Phase 1a refactor (yellow inner anycast +
collapsed receiver rx socket). Both changes only matter on the yellow
data path; green has been lab-validated since `a151d4a`. Run these
steps from the lab host once, in order. Each step is independently
diagnosable — if one fails, the runbook below tells you which commit
to look at.

### Prerequisites

Pull and rebuild against the latest `main` (Phase 1a step 1 + step 3
must both be present — commits `554b72c` and `549dddb`):

```bash
git pull --ff-only
git log --oneline -5    # confirm 549dddb (collapse rx) and 554b72c (yellow anycast)
make image              # rebuild containers with latest agent code
make teardown
make deploy             # spins up the 4p-8x16 fabric with regenerated configs
make config             # pushes ConfigDB + frr.conf
make host-routes        # installs host-side End.DT6 policies (yellow needs these)
```

### Step 4.1: yellow data path is alive

Pre-MRC sanity that yellow underlay + decap survived the addressing
change. Should look identical to green's two-host spray.

```bash
# Receiver
docker exec yellow-host15 spray --role recv --idle-timeout 6s

# Sender (in a second terminal, after recv banner)
docker exec yellow-host00 spray --role send \
    --dst-id 15 --rate 1000pps --duration 5s
```

Expected: ~5000 sent / ~5000 rx, loss% ≈ 0, per-plane sent roughly
uniform.

If this fails, the bug is in step 1 (yellow addressing), not step 3.
Inspect with:

```bash
docker exec yellow-host00 ip -6 addr show
# Every NIC eth1..eth4 + lo should carry cccc:<NN>::2/64 nodad.
docker exec yellow-host00 ip -6 route show
# Default-ish route to dst should resolve via one of the plane uplinks.
docker exec yellow-leaf00 ip -6 addr show dev Ethernet36
# Should be cccc:<NN>::1/64.
```

### Step 4.2: yellow MRC baseline (validates collapsed rx socket)

This is the headline test for Phase 1a step 3. Yellow's host-side
End.DT6 decaps inner packets onto `lo`, so a NIC-bound rx socket
would never see them — the collapsed `(::, SPRAY_PROBE_PORT)` socket
is what makes this work.

```bash
sudo run-scenario topologies/4p-8x16/scenarios/yellow-mrc-baseline.yaml \
    --report results/yellow-mrc-baseline.json
# or:
make scenario SCEN=yellow-mrc-baseline
```

Expected: per-flow `loss` ≈ 0, per-plane sent within ~5% of uniform,
EV-state snapshot (when surfaced) shows all 4 planes `state=good`,
weight=0.25.

**If this scenario shows total loss but step 4.1 didn't**, the
collapsed rx socket isn't receiving probes. Check on a yellow host:

```bash
# What is the receiver agent actually bound to?
docker exec yellow-host15 ss -6 -ulnp | grep 9999
# Expected: one row, `[::]:9999`. NOT four rows on per-NIC underlay
# addresses. If you see four rows, the agent is still on the per-plane
# rx model — confirm `srv6_mrc/mrc/agent.py` is from commit 549dddb
# or later inside the image (rebuild with `make image`).
```

A second useful check while the scenario is running:

```bash
docker exec yellow-host15 tcpdump -ni any 'udp port 9999' -c 20
# You should see probes arriving on `lo` (ifindex of lo) after End.DT6
# decap. If they're only visible on eth1..eth4 and never on lo, the
# host's seg6local table-0 lookup isn't actually firing — check
# `docker exec yellow-host15 ip -6 route show table 0 | grep cccc`.
```

### Step 4.3: yellow MRC plane-loss (validates payload-driven plane attribution)

Under the collapsed rx model the receiver no longer learns plane
identity from the ingress socket — it reads `plane_id` out of the
probe payload. This scenario is the lab confirmation that loss
attribution still pins to the right plane.

```bash
sudo run-scenario topologies/4p-8x16/scenarios/yellow-mrc-plane-loss.yaml \
    --report results/yellow-mrc-plane-loss.json
# or:
make scenario SCEN=yellow-mrc-plane-loss
```

Compare against the round-robin reference:

```bash
sudo make scenario SCEN=green-mrc-plane-loss     # MRC variant (was previously plane-loss round_robin)
jq '.flows[0].per_plane_sent, .flows[0].per_plane_loss, .flows[0].loss' \
    results/green-mrc-plane-loss.json results/yellow-mrc-plane-loss.json
```

Expected (matching `green-mrc-plane-loss` semantics):
- `yellow-mrc-plane-loss.json` `per_plane_sent[2]` starts at ~25% then
  drops near zero as plane 2 demotes.
- Total `loss` should be **substantially below 1.25%** (the
  round-robin reference).
- Demotion of plane 2, NOT some other plane. If the demoted plane is
  wrong, payload-driven plane attribution is broken — re-check
  `_probe_rx_loop` in `srv6_mrc/mrc/agent.py` (collapsed model
  reads `probe.plane_id`, doesn't infer from socket).

### Step 4.4: yellow MRC plane-latency (regression fixture)

```bash
sudo run-scenario topologies/4p-8x16/scenarios/yellow-mrc-plane-latency.yaml \
    --report results/yellow-mrc-plane-latency.json
```

Expected (current MRC build doesn't react to RTT alone): all 4 planes
stay `state=good` weight=0.25; reorder histogram tail extends by
~10ms-worth of in-flight packets per plane; `loss` ≈ 0. Functionally
identical to `green-mrc-plane-latency` — divergence here would point
at host-side decap introducing timing asymmetry.

### Step 4.5: cleanup

```bash
# netem revert sweep
for h in yellow-host00 yellow-host15; do
  for nic in eth1 eth2 eth3 eth4; do
    docker exec $h tc qdisc show dev $nic | grep -q netem && \
      docker exec $h tc qdisc del dev $nic root
  done
done
```

### What to report back

If all four sub-steps pass, Phase 1a is lab-complete and we can move
on to Phase 1b (module restructure). If any sub-step fails, share:

1. Which sub-step (4.1 / 4.2 / 4.3 / 4.4)
2. The relevant `.json` from `results/`
3. Output of `git log --oneline -5` (so we know what commits the lab
   was running against)
4. For 4.2 / 4.3 specifically, the `ss -ulnp` and `tcpdump` snippets
   from the diagnostic blocks above

### Yellow Tenant including verbose output

Dry run
```bash
SRV6_TOPO=topologies/4p-8x16/topo.yaml \
  python3 -m srv6_mrc.mrc.run \
  topologies/4p-8x16/scenarios/yellow-ev-spray.yaml --dry-run --verbose
```

2 spine fabric slice:
```bash
sudo make scenario SCEN=yellow-ev-spray
```

Filtered:
```bash
jq '.flows[] | {flow: (.src_host + " -> " + .dst_host),
               loss_pct: (((.sent - .received) / .sent * 100) | . * 100 | round / 100),
               ev_keys: (.per_ev_sent | keys | sort)}' \
   results/yellow-ev-spray-n2.json
```

Full 32 EV spray
```bash
sudo make scenario SCEN=yellow-ev-spray
```

Filtered
```bash
jq '.flows[] | {flow: (.src_host + " -> " + .dst_host),
               loss_pct: (((.sent - .received) / .sent * 100) | . * 100 | round / 100),
               ev_keys: (.per_ev_sent | keys | sort)}' \
   results/yellow-ev-spray.json
```


