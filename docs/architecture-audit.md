# Architecture Audit

Companion to `architecture-map.md`. The Map is descriptive; this Audit
is opinionated. For each component it answers: **keep as-is, refactor,
or cut?** Plus an honest cost/value estimate per refactor.

This Audit is the input to a Redesign session if one is warranted, and
the input to a focused Refactor session if not.

---

## TL;DR

- **80% of the codebase is solid.** Encap, probe framing, EV state
  machine, reorder accounting, scenario YAML schema, generator,
  topology inventory — all well-tested, well-scoped, would survive a
  rewrite essentially as-is.
- **Three concrete refactors would eliminate ~90% of the recent bug
  pattern** without touching any of that solid 80%:
  1. **Topology as parameter, not module global.** Eliminates Seam 1
     in one focused pass. ~1-2 days. High value, low risk.
  2. **Single source of truth for addressing.** `routes.py` redefines
     `inner_addr`, `build_segs`, `spine_for`, `host_name` locally
     instead of importing from `topo.py`. Pure deduplication. ~2 hours.
     Low value individually but removes a class of "which file is
     authoritative" bugs.
  3. **Test the cross-process boundary, not just the in-process logic.**
     Add a small set of subprocess-based integration tests that
     exercise the actual `docker exec spray` invocation. ~1 day.
     Catches Seam 2 timing races that current loopback tests can't see.
- **A from-scratch rewrite is not justified by current evidence.** The
  bugs are concentrated in two well-identified seams. Fixing them is
  cheaper than rewriting.

---

## Component-by-component verdict

Format: **keep / refactor / cut** + one-paragraph justification.

### Solid foundation (keep as-is)

| Component | LOC | Verdict | Justification |
|---|---|---|---|
| `srv6_mrc/encap.py` | 131 | **keep** | Tight, well-scoped, builds raw SRv6 packets. Used by both data path and probes. Zero bugs traced here. Do not touch. |
| `srv6_mrc/reorder.py` | 154 | **keep** | Per-flow reorder histograms. Pure logic, strong tests. Done. |
| `srv6_mrc/netem.py` | 328 | **keep** | tc/netem command construction. Strong test coverage, no bugs. |
| `srv6_mrc/mrc/probe.py` | 423 | **keep** | PROBE/REPLY/LOSS_REPORT wire format. Strong tests, zero framing bugs. |
| `srv6_mrc/mrc/probe_clock.py` | 277 | **keep** | In-flight probe bookkeeping. Pure logic, well-tested. |
| `srv6_mrc/mrc/loss_window.py` | 246 | **keep** | Receiver-side per-EV loss accumulator. May be the source of bug #4 (false-positive loss windows) — flag for investigation but don't preemptively rewrite. |
| `srv6_mrc/mrc/loss_compute.py` | 266 | **keep** | Sender-side SentWindowRing. Used to correlate LOSS_REPORTs to recent emit windows. Stable. |
| `srv6_mrc/mrc/ev_state.py` | 550 | **keep with one caveat** | The state machine itself is correct. The implicit merge of probe and loss signals (Seam 2) is a *policy* problem, not an *implementation* problem in this file. See "Seam 2" below. |
| `srv6_mrc/mrc/transport.py` | 372 | **keep** | MrcTransport ABC + Srv6Raw + LoopbackUdp. Clean separation, supports both real and loopback. The right abstraction. |
| `generators/` | small | **keep** | SONiC config + clab YAML generation from `topo.yaml`. Clean, well-tested. |
| `topologies/<size>/` | data | **keep** | Three deployable fabrics. The 4p-4x8 mid-size addition was a good call. |
| `tests/test_topo.py`, `test_policy.py`, `test_ev_state.py`, `test_probe.py`, `test_reorder.py`, `test_netem.py` | strong | **keep** | These tests are tight, fast, comprehensive. The model for what test coverage should look like elsewhere. |

### Refactor candidates

| Component | LOC | Verdict | Estimated cost | Why |
|---|---|---|---|---|
| `srv6_mrc/topo.py` | 489 | **refactor (Seam 1)** | 1-2 days | Module-level globals (`NUM_PLANES`, `NUM_SPINES`, `NUM_LEAVES`) bound at first import. Six band-aid commits trying to set `SRV6_TOPO` correctly. Convert to a `Topology` value class passed as parameter; module-global accessors stay as a thin compat shim during migration. |
| `srv6_mrc/cli/srctl.py` | 517 | **refactor + test** | 4 hours | Two issues: (a) duplicates `_infer_srv6_topo_from_argv` from `mrc/run.py`; (b) topology inference logic — the source of six bugs — has zero tests. Extract inference into `srv6_mrc/topology_select.py` shared by all entrypoints, add unit tests for it. |
| `srv6_mrc/cli/routes.py` | 727 | **refactor (deduplicate)** | 2 hours | Lines 150-189 redefine `inner_addr`, `build_segs`, `spine_for`, `host_name`, `container` locally instead of importing from `topo.py`. The hardcoded `2001:db8:bbbb:` and uSID format strings are split-source-of-truth bugs waiting to happen. Delete local copies, import from `topo.py`. |
| `srv6_mrc/runner.py` | 467 | **refactor (split sender from receiver)** | 1 day | One file owns both `run_sender` and `run_receiver`. They share almost nothing (encode_payload is the only common helper). Splitting clarifies process boundaries and makes the actual-process-model documentation easier. Low urgency but cheap. |
| `srv6_mrc/policy.py` | 432 | **refactor (split policy from health-aware)** | 4 hours | Five policies in one file; four are small (round_robin, hash5tuple, weighted, ev_spray) and one (`HealthAwareMrc` + `HealthAwareMrcFactory`) is half the LOC. The factory pattern exists because policies are constructed in YAML before the EV state table exists; the deferred-bind dance is awkward. Could be cleaner if policies took the table at `pick_ev` time instead of construction time. Optional. |
| `srv6_mrc/mrc/run.py` | 588 | **refactor (extract subprocess orchestration)** | 1 day | Mixes scenario YAML parsing, subprocess fan-out, JSON merging, and topology inference. The `docker exec` orchestration is the one piece that's hard to unit-test; extracting it lets the rest be pure. |
| `srv6_mrc/mrc/scenario.py` | 588 | **keep but split file** | 2 hours | The schema validation is solid. The scenario *resolution* logic (alias expansion, pair generation) is mixed in. Could be two files for clarity, but no actual bugs. Aesthetic refactor only. |
| `srv6_mrc/mrc/agent.py` | 861 | **refactor (split sender/receiver)** | 6 hours | Largest file. Holds both `SenderMrcAgent` and `ReceiverMrcAgent` plus shared helpers. The two agents share ~50 LOC of common code; everything else is independent. Splitting matches the process model and makes lifecycle clearer. |
| `srv6_mrc/report.py` | 448 | **refactor (extract JSON shape)** | 4 hours | The `_active_evs_from_mrc` / `_used_evs_from_counts` / `_missing_evs` triple has subtle precedence rules (MRC weights override sent-counts when present). Three pieces of data (mrc snapshot, per_ev_sent, scenario tenant) merged via implicit rules. Worth a single `EvUsageView` value type that owns the merge. |

### Cut candidates

| Component | Verdict | Justification |
|---|---|---|
| Per-tenant address scheme inconsistency | **cut** | Yellow inner = host-decap (`cccc:NN::2` on lo + eth1-4); green inner = leaf-decap (`bbbb:NN::2` on eth1-4 only). The `e009` extra hextet in yellow's uSID is residue from this asymmetry. The original reason (yellow demonstrates host-uA, green demonstrates leaf-uA) is valid for the paper-faithfulness goal. **Don't cut yet** — it's load-bearing for the experiment. But it's the source of a lot of conditional code; if the paper goal is dropped, this simplifies dramatically. |
| Hash5Tuple, Weighted policies | **could cut** | These exist for "comparison with round_robin under load" per the docstring. They've never been used in a scenario. Cut unless someone actually runs a comparison. ~50 LOC saved. Low value, low cost; defer. |
| `tests/mrc/test_run.py` orchestrator tests | **could cut** | Tests scenario parsing + dispatch with mocked subprocesses. Doesn't exercise the actual `docker exec` path. Replace with the real-subprocess integration tests (see "Refactor 3"). ~325 LOC. |

### Open questions (no verdict yet)

| Component | Question |
|---|---|
| `health_aware_mrc` weight computation | The "8/16 unused EVs" question from today's debug session is still open. Until we see the per-EV sent counts on the JSON, we can't tell if `weights_ev` is asymmetric or if `_weighted_pick` is biased. Resolve this first; it determines whether `policy.py` or `ev_state.py` is the source. |
| Bug #1 (probe thundering-herd at 4p-8x16) | Still open. May be a transport-layer issue (kernel raw socket queue), an agent-layer issue (probe scheduler), or a fabric-layer issue (docker-sonic-vs CPU saturation under burst). Need the grid scan we deferred. |
| Bug #4 (loss-window false positives) | Observed once, not yet reproduced. Could be a one-time race or a deterministic small-N artifact. |

---

## Detailed analysis: the three refactors

### Refactor 1: Topology as parameter (Seam 1)

**Current state:** `srv6_mrc.topo` reads `SRV6_TOPO` env-var at first
import. Module-level constants `NUM_PLANES`, `NUM_SPINES`, `NUM_LEAVES`
are frozen for the process lifetime. Every code path that imports
anything from `srv6_mrc` inherits the constants from whichever topology
was active at first import.

**Why it's a problem:**
- Three CLI entrypoints (`mrc/run.py`, `cli/srctl.py`, plus a duplicate
  in `cli/routes.py`) hand-roll inference logic before the import.
- Six commits in two weeks fixing bugs in this inference. None of
  them is wrong; the design forces every entrypoint to solve the
  same problem.
- Tests don't catch it because in tests SRV6_TOPO is set explicitly.
- A second topology in the same process is impossible by construction
  — needed for nothing today, but a smell.

**Proposed shape:**

```python
# srv6_mrc/topology.py (new file, replaces module-globals in topo.py)

@dataclass(frozen=True)
class Topology:
    name: str
    planes: int
    spines_per_plane: int
    leaves_per_plane: int
    tenants: tuple[str, ...]
    # ... addressing helpers as methods, not module functions

    @classmethod
    def from_yaml(cls, path: Path) -> "Topology": ...

    def inner_addr(self, tenant: str, host_id: int) -> str: ...
    def usid_outer_dst(self, plane: int, spine: int, dst_leaf: int, tenant: str) -> str: ...
    def num_evs(self) -> int: ...
    # ... etc
```

Then every function/class that currently uses `NUM_PLANES` takes
`topology: Topology` as an argument or constructor parameter.

**Migration path:**
1. Add `Topology` class alongside existing module globals.
2. Update modules one at a time to take `Topology` parameter; keep
   module globals as a "default topology" for backward compat.
3. Once all callers are migrated, delete module globals and the
   inference shims.

**Cost:** 1-2 days. 30-40 files touched but most are mechanical (`s/NUM_PLANES/topology.planes/`).

**Risk:** Low. The constants don't change between calls in any single
process — it's purely a naming/access refactor. Tests will catch any
miss.

**Value:** Eliminates an entire class of bug. Six commits saved per
year-of-similar-pace.

### Refactor 2: Single source of truth for addressing

**Current state:** `cli/routes.py:150-189` redefines `inner_addr`,
`build_segs`, `spine_for`, `host_name`, `container` with hardcoded
addresses (`2001:db8:bbbb:`, `2001:db8:cccc:`) and uSID format
(`fc00:000{plane:x}:f00{spine:x}:e00{dst_leaf:x}`). `topo.py` has its
own copies with the same hardcoded strings.

**Why it's a problem:**
- Two files with hardcoded `2001:db8:bbbb:` etc. If the address scheme
  changes (and it has — yellow flipped from `cccd:<NN>::1` to
  `cccc:<NN>::2`), both must be updated. Easy to miss one.
- The duplication is asymmetric: routes.py *imports* `REFERENCE_PAIRS_SPINES`
  from topo.py but *redefines* `spine_for`. No principled reason; just
  copy-paste-then-diverge.

**Proposed shape:** Delete the local copies in routes.py. Import from
topo.py. Verify tests still pass.

**Cost:** 2 hours including running the test suite.

**Risk:** Very low. Pure deduplication.

**Value:** Removes a footgun. Doesn't fix any bug today but prevents
the next "yellow address scheme drifted between the two files" bug.

### Refactor 3: Test the cross-process boundary

**Current state:** All integration tests run in-process with loopback
transports. The actual production path — `docker exec spray --role recv`
+ `docker exec spray --role send` + JSON-on-stdout merging — is
exercised only by manual `srctl run`.

**Why it's a problem:**
- Bug #1 (probe thundering-herd at 4p-8x16) and bug #4 (loss-window
  false positives) both involve real-network timing. Loopback transport
  collapses these timings to ~0; they can't reproduce in-process.
- Bug-find-rate-per-LOC in this region is much higher than in tested
  regions. We're paying the cost of these bugs in lab time, not test
  time.

**Proposed shape:** Add `tests/integration/` with a small set of tests
that:
- Spin up a minimal 2-host loopback fabric using netns + veth pairs
  (no docker-sonic-vs needed; just enough for raw sockets to work).
- Run `spray` as actual subprocesses.
- Capture and assert on the merged JSON output.

This is the smallest scope that exercises subprocess boundaries +
real-ish timing without requiring a full clab deployment.

**Cost:** 1 day for harness + 3-5 representative tests.

**Risk:** Medium. Netns-based fabrics are fiddly on macOS (you'd run
these on Linux only, conditional skip on Darwin).

**Value:** Future bugs in cross-process coordination get caught in CI
instead of in lab debugging sessions. Probably the highest-ROI refactor
of the three for ongoing development.

---

## What about a rewrite?

The Map identified two seams + accumulated convenience. Let me ask
honestly: **how much of the codebase would survive a rewrite?**

Going component by component:

| Component | LOC | Survives? | Why |
|---|---|---|---|
| encap, probe, reorder, netem, ev_state, loss_window/compute, transport, generators, topologies | ~3000 | Yes, ~95% | Solid, well-scoped, the actual research artifacts |
| topo.py (after Refactor 1) | ~300 | Yes | The addressing logic is correct |
| policy.py | 432 | Yes, ~90% | Logic is fine; only the factory dance is awkward |
| scenario.py | 588 | Yes, ~80% | YAML schema is good; resolution logic refactorable |
| runner.py | 467 | Yes, ~70% | Split into sender.py + receiver.py, much survives |
| agent.py | 861 | Yes, ~70% | Same split |
| report.py | 448 | Yes, ~60% | Merge logic worth re-deriving |
| cli/srctl.py | 517 | Yes, ~50% | Argparse boilerplate stays; inference logic gets replaced |
| cli/routes.py | 727 | Yes, ~70% after dedup | Most is solid; the duplicates go |
| cli/spray.py | 439 | Yes, ~80% | Thin wrapper, mostly fine |
| mrc/run.py | 588 | Yes, ~60% | Subprocess orchestration extracts cleanly |

**Estimate:** A rewrite would preserve 70-80% of the current LOC by
volume, structurally identical. The 20-30% that *would* change is
exactly the seams we've identified — and those can be refactored in
place.

**Conclusion:** A rewrite would not produce a meaningfully different
codebase. It would just be the current codebase with the three
refactors above already applied. Given that, **doing the refactors
in place is strictly cheaper** — no risk of losing accumulated
test coverage, no risk of forgetting load-bearing details (like the
yellow-vs-green decap asymmetry, the SO_BINDTODEVICE per-plane
invariant, the per-tenant uSID format).

---

## Recommended path forward

**Phase 1 (this week, ~2 days):**
1. Refactor 2 first (single source of truth for addressing) — 2 hours,
   builds confidence + warmup
2. Refactor 1 (topology as parameter) — 1-2 days, the big win

**Phase 2 (next week, ~1 day):**
3. Refactor 3 (cross-process integration tests) — 1 day

**Phase 3 (in parallel with Phase 1-2, on the lab):**
4. Resume bug #1 grid scan on 4p-8x16 (was deferred for this audit)
5. Resolve the open "8/16 unused EVs" question — quick: just need the
   per-EV sent counts from a fresh JSON

**What we're NOT doing:**
- No rewrite.
- No cutting Hash5Tuple/Weighted policies (defer until policy work
  needs cleanup).
- No refactoring policy.py's factory pattern (works fine, just ugly).
- No splitting agent.py until we have a concrete reason.

**Decision point at end of Phase 1:** Re-evaluate. If Refactor 1 reveals
deeper structural issues, return to this Audit. If it goes smoothly
(expected), continue with Phase 2.

---

## Honest caveats

1. **I have not traced bug #1 to its root cause.** All my opinions
   about Seam 2 are inference from symptoms. The grid scan might
   reveal something that changes this audit's recommendations.

2. **The "70-80% survives a rewrite" estimate is not rigorous.** It's
   based on having read each file's top-level structure, not on
   line-by-line analysis. It's a reasonable working hypothesis, not
   a guarantee.

3. **I have not tested any of the proposed refactors.** Costs are
   estimates. Refactor 1 in particular might surprise — module-level
   constants are sometimes load-bearing in unexpected ways
   (`@dataclass` field defaults, function default arguments).

4. **The bug pattern might shift after these refactors.** If most
   recent bugs were in Seams 1 and 2, future bugs might cluster
   somewhere this Audit doesn't anticipate.
