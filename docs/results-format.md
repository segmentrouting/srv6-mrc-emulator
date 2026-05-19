# MRC results format

The `results/` directory at the repo root holds JSON reports written
by `run-scenario --report <path>` (a.k.a.
`srv6_mrc/mrc/run.py`). Each file is a single scenario run. Files
there are gitignored by convention — they're test artifacts, not
source.

For *how* to produce these reports, see `running.md`. For the
underlying per-flow record schema, see `design-mrc.md`.

## ASCII summary

The orchestrator prints a compact ASCII table to stdout while writing
the full JSON to `--report`. Example (from `baseline.yaml`):

```
scenario: baseline
==============================================================================
  flow                            policy           sent     rx   loss%  reord  max
  --------------------------------------------------------------------------------
  green-host00 -> green-host15    round_robin      3719   3719   0.00%    424  127
  ...
  per-plane (sent / rx):
    plane 0:    7270 /   7270
    plane 1:    7269 /   7269
    plane 2:    7267 /   7267
    plane 3:    7263 /   7263
==============================================================================
```

Column meanings:

| Column | Meaning |
|---|---|
| `flow` | sender host → receiver host (tenant inferred from name) |
| `policy` | spray policy used by this flow's sender (`round_robin`, `hash5tuple`, ...) |
| `sent` | packets the sender reports emitting (sum across all four NICs) |
| `rx` | packets the receiver matched to this flow (sum across all four NICs) |
| `loss%` | `(sent - rx) / sent * 100`, formatted to 2 decimals |
| `reord` | packets that arrived with a sequence number lower than the highest seen so far — `sum(v for k, v in reorder_hist.items() if k > 0)` |
| `max` | largest single reorder distance observed (`reorder_max`) |

The `per-plane (sent / rx)` block shows per-plane delivery for the
whole scenario (summed across flows). Use this to spot which plane
is degraded in a fault scenario.

## JSON schema

The JSON report is `ScenarioReport.to_dict()`. Top-level shape:

```jsonc
{
  "scenario": "baseline",
  "flows": [ { ...FlowRow }, ... ],
  "per_plane_sent": { "0": 7270, "1": 7269, "2": 7267, "3": 7263 },
  "per_plane_recv": { "0": 7270, "1": 7269, "2": 7267, "3": 7263 },
  "warnings": []
}
```

Each `FlowRow`:

```jsonc
{
  "tenant": "green",
  "src_host": "green-host00",
  "dst_host": "green-host15",
  "policy": "round_robin",
  "sent": 3719,
  "received": 3719,
  "loss": 0,
  "duplicates": 0,
  "reorder_max": 127,
  "reorder_mean": 1.83,
  "reorder_p99": 14,
  "reorder_hist": { "0": 3295, "1": 380, "5": 40, "127": 4 },
  "per_plane_sent": { "0": 930, "1": 930, "2": 930, "3": 929 },
  "per_plane_recv": { "0": 930, "1": 930, "2": 930, "3": 929 },
  "notes": []
}
```

`notes` carries per-flow diagnostics (e.g., "receiver X saw no flow
A -> B"). `warnings` at the top level carries scenario-wide issues
(orphan receiver flows nobody sent, duplicate receiver records, etc.).
Both should be empty on a clean baseline run.

## Reading the reorder histogram

`reorder_hist` is `{distance: count}` where *distance* is
`(max_seq_so_far − this_packet_seq)` at arrival time, and *count* is
how many packets had that distance.

- `"0": N` — packets that arrived in order (the common case).
- `"1": N` — off-by-one swaps, typical when two adjacent packets
  cross planes.
- Large keys with small counts — tail events, usually one slow plane
  catching up after a brief stall.

`reorder_mean` and `reorder_p99` are derived from this histogram and
are the values you'd size a real reorder buffer against. The `max`
column in the ASCII table is `reorder_max` — worst observed, not
necessarily representative.

## Interpreting common patterns

| Pattern | Meaning |
|---|---|
| `loss% = 0`, `reord = 0` | Single plane (e.g., `hash5tuple` policy) or extremely quiet links |
| `loss% = 0`, `reord ≈ 10–15%` of sent, small `max` | Healthy round-robin — just inter-plane jitter |
| `loss% = 0`, `reord ≈ 30%+` of sent, large `max` | One plane is slow (latency injection) but lossless |
| `loss% > 0`, target plane `rx < sent` | Random loss on that plane; aggregate loss ≈ plane_loss / 4 |
| `loss% ≈ 25%`, one plane `rx = 0` | One plane fully blackholed |
| `loss% ≈ 50%`, two planes `rx = 0` | Two planes blackholed (MRC's claimed survivability ceiling at N=4) |

## Comparing runs

A useful quick diff between two report files:

```bash
jq -c '.flows[] | {src:.src_host, dst:.dst_host, loss_pct:.loss_pct, reord_max:.reorder_max, reord_p99:.reorder_p99}' \
   results/baseline.json results/plane-latency.json
```

Or to see how the reorder histogram changes shape under fault:

```bash
jq '.flows[0].reorder_hist' results/baseline.json
jq '.flows[0].reorder_hist' results/plane-latency.json
```
