# Live visibility / Grafana dashboard — design

Status: **design only, no implementation yet**. Skeleton lives under
`contrib/visibility-poc/`. This document is the authoritative target
for that skeleton; the next agent should be able to read this end-to-
end and start filling in `scraper/scraper.py` without making any more
architectural decisions.

Companion to the "Roadmap > Live visibility / Grafana dashboard
(planned)" section of `AGENTS.md`. When the two disagree, AGENTS.md
wins (it gets updated first when the lab teaches us something).

---

## 1. Goal

Make the **per-plane balance** story of the SRv6-spray demo observable
in real time during a scenario run. Today the only way to see it is to
`jq` post-run JSON in `results/`. That is fine for offline analysis but
useless for a live demo and useless for diagnosing a scenario that
hasn't finished yet.

Headline panel (this is THE demo visual):

> **Per-plane TX pps**, on at least one chosen leaf or per-host
> aggregate, four colored series — one per plane — live during a
> scenario run, with fault-injection annotations overlaid.

Everything else in this design is secondary to that panel rendering
cleanly during a `make scenario SCEN=…` run.

### Non-goals (explicit)

- **Not** a SONiC telemetry replacement. We do not run a gNMI / streaming
  telemetry container in the lab; that ship sailed and no SONiC mods
  are in scope.
- **Not** a packet-capture / pcap dashboard. Wireshark and `tcpdump`
  remain the tools for wire-level inspection.
- **Not** a multi-user / multi-tenant Grafana. Topology host is
  dedicated, single user, no auth needed beyond Grafana's default
  anonymous viewer.
- **Not** persistent across `make destroy`. Prometheus TSDB lives in
  a container volume that goes away with the lab; that is the desired
  behaviour. Use `results/` JSON for post-run history.
- **Not** a replacement for `ScenarioReport`. The post-run JSON
  remains the source of truth for correctness; the dashboard is for
  in-the-moment diagnostics and demo affordance.

---

## 2. Architecture

```
                       topology host (single docker daemon)
   ┌────────────────────────────────────────────────────────────────┐
   │                                                                │
   │  ┌─────────────────────────────  fabric nodes  ──────────────┐ │
   │  │ p0-spine00 … p3-leaf07   (32 SONiC-VS containers)         │ │
   │  │ green-host00 … yellow-host07 (16 alpine containers)       │ │
   │  └────────────▲──────────────────────▲──────────────────────┘ │
   │               │ docker exec          │ docker exec            │
   │               │ ip -stats link show  │ cat /dev/shm/srv6-mrc/ │
   │               │ cat /proc/net/snmp6  │   <host>/<tenant>_<dd>.json
   │               │                      │                        │
   │  ┌────────────┴──────────────────────┴────────────┐           │
   │  │  srv6-scraper  (Python, prometheus_client)    │           │
   │  │  - /metrics on :9100                          │           │
   │  │  - reads topology.clab.yaml at start          │           │
   │  │  - polls every 1s, exposes Prometheus text    │           │
   │  └────────────────────┬──────────────────────────┘           │
   │                       │ HTTP /metrics                         │
   │  ┌────────────────────▼───────────┐    ┌───────────────────┐ │
   │  │  prometheus  (:9090, scrape 1s)│───▶│  grafana (:3000)  │ │
   │  │  retention: in-memory + 1h tsdb│    │  provisioned ds + │ │
   │  └────────────────────────────────┘    │  dashboards       │ │
   │                                        └─────────▲─────────┘ │
   └──────────────────────────────────────────────────┼───────────┘
                                                      │
                                          http://<topology-host>:3000
                                                      │
                                                    user
```

Three new containers, all declared in `topology.clab.yaml` so
`make destroy` tears them down with the rest of the lab. They share
a docker network named `visibility` to keep Prometheus → scraper
discovery dead-simple (DNS by container name).

---

## 3. Container topology additions

Merged into `topologies/<name>/topology.clab.yaml` (the fragment in
`contrib/visibility-poc/containerlab/visibility.yaml.fragment` is the
canonical template — `generators/fabric.py` should learn to emit it,
gated on a `visibility:` block in `topo.yaml`; see §9).

Containers:

| Name              | Image                              | Ports (host→ctr) | Notes                                                            |
|-------------------|------------------------------------|------------------|------------------------------------------------------------------|
| `srv6-scraper`    | `srv6-scraper:1.0` (built locally) | —                | `network_mode: host` OR docker socket bind. See §4.              |
| `srv6-prometheus` | `prom/prometheus:v2.55.0`          | 9090→9090        | Bind-mounts `prometheus/prometheus.yml`. In-memory + 1h TSDB.    |
| `srv6-grafana`    | `grafana/grafana:11.2.0`           | 3000→3000        | Provisioned datasource + the `per-plane-balance` dashboard.      |

A new docker network `visibility` (containerlab `mgmt:` plus a
secondary network is fiddly; instead declare it as a clab `networks:`
top-level entry on the new containers only — Prometheus and Grafana
only need to talk to the scraper and each other, not the fabric).

Image build for the scraper: a tiny Python 3.12 image with
`prometheus_client`, `pyyaml`, and the docker CLI installed.
Dockerfile lives at `contrib/visibility-poc/scraper/Dockerfile`.

---

## 4. Scraper: docker exec vs nsenter

**Decision: `docker exec` for v1. Document the nsenter upgrade path
but do not implement it yet.**

### Why

| Aspect                       | docker exec                                     | nsenter (privileged sidecar)                            |
|------------------------------|-------------------------------------------------|---------------------------------------------------------|
| Per-call cost                | 50–100 ms                                       | 1–5 ms                                                  |
| Total budget at 1s cadence   | (32 fabric + 16 hosts) × ~75ms ≈ 3.6s. *Tight.* | Fits easily.                                            |
| Setup complexity             | Trivial — needs docker socket only.             | Needs `pidMode: host`, `cap_add: SYS_ADMIN`, `privileged: true`, and looking up container PIDs. |
| Failure modes                | rc=1 transient race (documented in AGENTS.md).  | Same `ip` / `cat` semantics, but in host namespace.     |
| Portability                  | Works against any docker daemon.                | Requires the scraper container to share host PID ns.    |
| Hard ceiling                 | ~1s cadence on 4p-4x8; ~5s on 4p-8x16.          | ~200ms cadence on 4p-8x16 in principle.                 |

For the **headline per-plane balance panel**, 1s cadence is fine. The
visual story is "traffic shifts off plane 2 after fault injection",
which plays out over seconds. Sub-second cadence buys nothing for the
demo. For diagnostic deep-dives (per-EV RTT, microbursts) we accept
that this dashboard is the wrong tool — that's a post-run job.

If the user later runs `4p-8x16` regularly and wants <1s cadence, the
upgrade is well-scoped: swap `docker_exec()` for `nsenter_exec()` in
`scraper.py` and add `pidMode: host` + caps to the clab fragment.

### Concurrency

The scraper uses a thread pool (e.g. `concurrent.futures`) of ~16
workers to parallelize `docker exec` calls within a single scrape
tick. With 48 targets per tick on 4p-4x8 this collapses wall-clock
scrape time from ~3.6s sequential to ~250ms — comfortably under 1s.

### The retry pattern (mandatory)

Per the AGENTS.md gotcha (`docker exec cat` transient rc=1 race
during exec-session teardown overlap), any `docker exec <node> cat …`
in the scraper MUST retry up to 3× with 100ms backoff. The retry
helper lives in `scraper/scraper.py` as `docker_exec_with_retry()`
and applies to all three data sources. Failure after 3 retries logs
the stderr and increments `srv6_scraper_exec_failures_total{source=…,
target=…}` — Prometheus will catch sustained failure as a missing
metric, not a stuck dashboard.

### `docker exec` stdout truncation guard

Per the same gotcha, large JSON payloads can lose trailing bytes if
read directly via `docker exec cat`. The host MRC snapshots can be
several KiB once `num_paths` is large. The scraper reads them via
`docker exec cat /dev/shm/srv6-mrc/<host>/<tenant>_<dd>.json` and
**validates JSON parse** before using the payload; on parse failure
it does ONE retry (which the rc=1 retry path will exhaust naturally)
and then marks the sample missing. We do not need the "write a
sentinel + second cat" workaround the orchestrator uses, because the
daemon is not exiting — its snapshot file is steady-state and the
truncation race is dominated by the rc=1 race, which the retry
already covers.

---

## 5. Data sources

Three sources, all already exposed by the existing system. **No
production-code changes required.**

### 5.1 Fabric NIC counters

```
docker exec <fabric-node> ip -stats link show <iface>
```

One call per fabric node × per NIC of interest. The scraper parses
the `RX:`/`TX:` lines for `bytes` and `packets`, computes rates by
subtracting the previous sample and dividing by elapsed time, and
exposes both gauges (bytes, packets) and derived rates.

Which interfaces matter for the headline panel:

- On leaves: the 4 spine-facing uplinks (`Ethernet0..Ethernet12` per
  the topology, NIC ordinals 0..3 per plane).
- On hosts: `eth1..eth4` (the 4 plane NICs).

The scraper does not parse `topology.clab.yaml` directly. It reads
the active topology's `topo.yaml` (the same source of truth the rest
of the lab uses) via the bind-mounted path, and derives the fabric
node list + per-node plane-NIC mapping from it. This means the
scraper is portable across `4p-4x8`, `4p-8x16`, `2p-4x8` without
code changes — same as `spray`, `routes`, `run-scenario`.

### 5.2 Host kernel IPv6 counters

```
docker exec <host> cat /proc/net/snmp6
```

Cheap (single exec, single file). Exposed as gauges:

- `srv6_host_ip6_in_delivers_total`
- `srv6_host_ip6_in_discards_total`
- `srv6_host_udp6_in_datagrams_total`
- `srv6_host_udp6_in_csum_errors_total`
- `srv6_host_udp6_rcvbuf_errors_total`

These are exactly the counters we keep grepping by hand during MRC
debugging. The diagnostic panel showing `Udp6RcvbufErrors > 0`
catches "replies arriving but kernel can't deliver them" in one
glance.

### 5.3 MRC snapshot JSON

```
docker exec <host> cat /dev/shm/srv6-mrc/<host>/<tenant>_<dd>.json
```

The daemon already writes per-flow snapshots here (envelope shape
`{captured_ns, dst_id, ev_state, src_host, src_id, tenant,
transport_stats}` with `.ev_state.tenants["<tenant>"]` as a list of
per-EV dicts — see AGENTS.md gotcha and `srv6_mrc/mrc/daemon.py:222`).

The scraper discovers snapshot files by:

1. `docker exec <host> sh -c 'ls /dev/shm/srv6-mrc/<host>/ 2>/dev/null'`
   to list available flows.
2. For each, exec-cat the JSON, parse, and emit per-EV gauges.

This is the most expensive source per host (1 list + N flow files).
At the all-to-all scale on 4p-8x16, a host has ~31 snapshot files of
a few KiB each — feasible at 1s cadence but slow. v1 will sample
snapshots at a longer cadence (5s) than interface counters (1s).
Cadence is a per-source config knob in `scraper.py`.

---

## 6. Prometheus metric naming

All metrics live under the `srv6_*` prefix. Type and unit suffixes
follow Prometheus conventions (`_total` for counters, no unit suffix
for gauges, `_seconds`/`_nanoseconds` only where strictly necessary).

### Fabric interface counters (THE headline panel feeds these)

| Metric                       | Type    | Labels                                                |
|------------------------------|---------|-------------------------------------------------------|
| `srv6_iface_tx_packets_total`| Counter | `plane`, `tier` (spine/leaf), `node`, `iface`         |
| `srv6_iface_tx_bytes_total`  | Counter | same                                                  |
| `srv6_iface_rx_packets_total`| Counter | same                                                  |
| `srv6_iface_rx_bytes_total`  | Counter | same                                                  |

`plane` is derived from the node name (`p2-leaf03` → `plane="2"`)
or from the NIC ordinal on hosts (`eth1` → `plane="0"`, per the
host topology convention).

The headline per-plane pps panel is a `sum by (plane)` over
`rate(srv6_iface_tx_packets_total{tier="leaf"}[10s])` filtered by a
templated `node` variable (default: all leaves, drill-down to one).

### Host kernel counters

| Metric                                        | Type    | Labels        |
|-----------------------------------------------|---------|---------------|
| `srv6_host_ip6_in_delivers_total`             | Counter | `host`, `tenant` |
| `srv6_host_ip6_in_discards_total`             | Counter | same          |
| `srv6_host_udp6_in_datagrams_total`           | Counter | same          |
| `srv6_host_udp6_rcvbuf_errors_total`          | Counter | same          |

### MRC snapshot — per-EV

| Metric                                        | Type    | Labels                                              |
|-----------------------------------------------|---------|-----------------------------------------------------|
| `srv6_mrc_ev_state`                           | Gauge   | `host`, `tenant`, `dst_id`, `plane`, `path` (0=unknown, 1=good, 2=suspect, 3=assumed_bad) |
| `srv6_mrc_ev_weight`                          | Gauge   | same                                                |
| `srv6_mrc_ev_rtt_p50_seconds`                 | Gauge   | same                                                |
| `srv6_mrc_ev_consecutive_probe_successes`     | Gauge   | same                                                |
| `srv6_mrc_ev_consecutive_probe_timeouts`      | Gauge   | same                                                |
| `srv6_mrc_flow_active_evs`                    | Gauge   | `host`, `tenant`, `dst_id`                          |
| `srv6_mrc_snapshot_captured_ns`               | Gauge   | `host`, `tenant`, `dst_id`                          |

The `srv6_mrc_flow_active_evs` gauge feeds the "per-flow EV active
count" secondary panel. The `srv6_mrc_snapshot_captured_ns` lets
Grafana flag stale snapshots (gauge < now − 10s ⇒ stale).

### Scraper self-metrics

| Metric                                        | Type    | Labels                              |
|-----------------------------------------------|---------|-------------------------------------|
| `srv6_scraper_scrape_duration_seconds`        | Gauge   | `source` (`iface`/`snmp6`/`mrc`)    |
| `srv6_scraper_exec_failures_total`            | Counter | `source`, `target`                  |
| `srv6_scraper_targets_scraped`                | Gauge   | `source`                            |

These let an operator confirm the scraper is keeping up. If
`srv6_scraper_scrape_duration_seconds{source="iface"}` ever climbs
above the cadence floor, we know we've hit the docker-exec ceiling
and need to either drop cadence or move to nsenter.

---

## 7. Grafana dashboard layout

One dashboard initially: `per-plane-balance.json`. JSON in
`contrib/visibility-poc/grafana/dashboards/`. Provisioned via
the standard Grafana `provisioning/dashboards/*.yaml` mechanism
(the scraper image's docker-entrypoint mounts the JSON into
`/etc/grafana/provisioning/dashboards/` on Grafana startup).

### Row 1 — HEADLINE: Per-plane TX pps (4 series)

- Panel type: Time series, stacked = false.
- Query: `sum by (plane) (rate(srv6_iface_tx_packets_total{tier="leaf",node=~"$leaf"}[10s]))`.
- Template variable `$leaf` defaulting to `p.*-leaf.*` (all leaves);
  user can scope to one leaf to see a single-leaf view.
- Plane colors hardcoded in the JSON: P0 blue, P1 green, P2 yellow,
  P3 red. (Yellow being P2 is unrelated to the yellow tenant — these
  are plane identifiers, not tenant colors. The accidental
  collision is acceptable; if it confuses the demo audience, switch
  P2 to orange.)
- Y axis: pps. No log scale.
- This panel renders the demo headline. If it doesn't render, the
  feature is broken; if it does render and the others are flaky,
  ship anyway.

### Row 2 — Per-flow MRC health

- Panel A: `srv6_mrc_flow_active_evs` per `(host, tenant, dst_id)`,
  table or heatmap.
- Panel B: `srv6_mrc_ev_state` heatmap, X = time, Y = `(plane, path)`,
  cell color = state enum. Templated by `host` + `dst_id`.

### Row 3 — Per-EV probe health over time

- Panel A: `srv6_mrc_ev_rtt_p50_seconds` time series, lines per EV,
  templated by `host`+`dst_id`.
- Panel B: `srv6_mrc_ev_consecutive_probe_timeouts` time series, same
  template. Annotate threshold line at `probe_fail_threshold`.

### Row 4 — Diagnostics

- Panel A: `srv6_host_udp6_rcvbuf_errors_total` rate by `host`. Red
  if non-zero.
- Panel B: Scraper-self-metrics — scrape duration vs cadence floor.

### Fault-injection annotations

`run-scenario` already shells `tc qdisc add` and `tc qdisc del` via
nsenter during scenario execution. To get these into Grafana as
dashboard annotations, the **simplest path** is to have
`run-scenario` POST to a Grafana annotation API endpoint when it
applies/removes a fault. This is a small change in `srv6_mrc/mrc/run.py`
and is **explicitly deferred** to the post-skeleton work (see §11).
For v1 the annotations come from the scraper noticing
`per-plane TX rate dropped to ~0 on plane X` — but that's
correlation not causation. Real annotations need orchestrator
cooperation.

---

## 8. Failure modes + retry

| Failure                                                                 | Symptom                                                  | Mitigation                                                                                  |
|--------------------------------------------------------------------------|-----------------------------------------------------------|--------------------------------------------------------------------------------------------|
| `docker exec cat` transient rc=1 (AGENTS.md gotcha)                      | Single missing sample                                     | `docker_exec_with_retry()` — 3 attempts, 100 ms backoff. Then increment failure counter.   |
| `docker exec` stdout truncation on large JSON                            | JSON parse error                                          | Validated parse, retry once via the rc=1 retry path; drop sample if still bad.             |
| MRC snapshot file doesn't exist yet (scenario starting)                  | `ls /dev/shm/srv6-mrc/<host>/` returns nothing            | Silent skip; emit zero `srv6_mrc_flow_active_evs` series until first snapshot appears.     |
| Fabric node restarted (clab does not auto-restart, but for completeness) | docker exec returns `Error: No such container`            | Catch and skip; rediscover topology on next full scrape cycle (every 60s).                 |
| Scraper itself wedged                                                    | Prometheus marks target down                              | Grafana alert on `up{job="srv6-scraper"} == 0`. (v2: docker healthcheck.)                  |
| Prometheus TSDB full (in-memory + 1h retention is the cap)               | Old data drops off                                        | Acceptable; live dashboard, not historical archive.                                        |

The retry helper signature, matching the orchestrator's pattern:

```python
def docker_exec_with_retry(container: str, argv: list[str], *,
                            attempts: int = 3, backoff_ms: int = 100,
                            timeout_s: float = 5.0) -> str:
    """Run `docker exec <container> <argv>`. On rc=1 retry up to
    `attempts` times. Raise on final failure with stderr included.
    Used by all three data sources."""
```

---

## 9. `make deploy` / `make destroy` integration

The visibility containers MUST come up and go down with the lab.
This is non-negotiable per the constraint set — no side-channel
docker-compose, no manual stand-up.

Two implementation options, both viable:

### Option A: generator-emitted (recommended)

`generators/fabric.py` learns a new optional top-level block in
`topo.yaml`:

```yaml
visibility:
  enabled: true
  scraper_image: srv6-scraper:1.0
  prometheus_image: prom/prometheus:v2.55.0
  grafana_image: grafana/grafana:11.2.0
  grafana_port: 3000
  prometheus_port: 9090
```

When `visibility.enabled: true`, the generator emits the three
containers from `contrib/visibility-poc/containerlab/visibility.yaml.fragment`
into the generated `topology.clab.yaml`. This keeps the source-of-
truth invariant (`topo.yaml` describes the whole lab) intact.

Cost: ~30 lines added to `generators/fabric.py`. Mechanically
simple.

### Option B: post-deploy injection

The Makefile's `deploy` target shells `containerlab deploy` then
`containerlab deploy -t visibility.yaml`, where `visibility.yaml`
is a second clab topology in the same lab namespace. This is
hackier; `containerlab destroy --cleanup` against the primary
topology won't tear down the second one without a matching second
destroy. **Not recommended.**

### Makefile target change

`make deploy` does not change shape. The visibility containers are
just additional nodes in `topology.clab.yaml`. Add one informational
echo on success:

```
@if [ "$$(grep -c srv6-grafana $(CLAB_YAML))" -gt 0 ]; then \
    echo "Grafana: http://$$(hostname):3000  (anonymous viewer)"; \
fi
```

`make destroy` is unchanged — `containerlab destroy --cleanup`
removes everything declared in the topology, including the
visibility nodes and the `visibility` network.

### Image build target

Add:

```
.PHONY: visibility-image
visibility-image: ## build the scraper container image
	docker build -f contrib/visibility-poc/scraper/Dockerfile \
	    -t srv6-scraper:1.0 contrib/visibility-poc/scraper
```

`make image` should NOT automatically build the scraper image — keep
the existing image target focused on the host image. The first-time
deploy sequence becomes:

```
make image
make visibility-image          # new, one-time
make regen
make deploy
make config
make host-routes
make scenario SCEN=…
```

If `srv6-scraper:1.0` is not present at `containerlab deploy` time
and `visibility.enabled: true` in `topo.yaml`, clab fails fast with a
"no such image" error — desired behaviour; better than silently
deploying without visibility.

---

## 10. Estimated cost

See the summary report. Skeleton ships unfilled; v1 implementation
is roughly 3–4 PRs across ~5–8 working days for one engineer who
already has the lab up.

---

## 11. Intentionally deferred

The skeleton ships these as stubs. They are NOT v1 work and the next
agent should resist scope creep:

- **Fault-injection annotations as proper Grafana annotations.** Needs
  a tiny POST helper in `srv6_mrc/mrc/run.py` and Grafana's annotation
  API token. Defer to v2.
- **nsenter scrape path.** Only needed if `4p-8x16` at sub-second
  cadence becomes a real requirement.
- **Per-EV pps from MRC snapshots.** The snapshot publishes counts of
  state and weights but not data-path pps per EV. Adding it would
  require either parsing receiver-side `per_ev_recv` from a running
  spray process (no exposed endpoint today) or sniffing — both out of
  scope.
- **Alerting.** Pure dashboard; no Prometheus alerts beyond the
  scraper-self-up gauge.
- **Authentication / TLS.** Single-user lab host. Grafana stays on
  anonymous viewer.
- **Persistent storage / cross-run history.** Use `results/` JSON for
  that. Prometheus TSDB is in-memory + 1h, blown away on `make destroy`.
- **Topology auto-discovery via clab inspect.** The scraper reads
  `topo.yaml` (same as `spray`), not `containerlab inspect`. Keeps
  one source of truth.

---

## PR 1 status (2026-05-24)

PR 1 lands the scraper itself: `contrib/visibility-poc/scraper/scraper.py`
(~1060 lines) plus 30 unit tests under
`tests/contrib/visibility_poc/`. Full suite is now 580 tests, ~2.8s,
no lab required.

### Implemented in PR 1

- `parse_ip_link_stats(output)` — positional, header-driven; tolerant
  of iproute2 ≥5.x adding a `missed` column on RX. Returns
  `IfaceCounters` or `None` on malformed input.
- `parse_snmp6(output)` — line-oriented, malformed lines silently
  skipped. Returns full dict; scraper publishes the subset in
  `SNMP6_EXPOSED_KEYS`.
- `parse_mrc_snapshot(payload: dict)` — decodes the wrapped envelope
  from `MrcDaemon._publish_snapshot`, including the new
  `reply_latency_buckets` block introduced in commit `813abcf`. Pure
  function over a parsed dict so it can be tested without docker.
- `docker_exec_with_retry(host, argv, *, attempts=3, backoff_s=0.1)` —
  the AGENTS.md SO_REUSEPORT-cascade cure. Retries only on rc=1;
  non-retryable failures raise `DockerExecError` immediately;
  exhaustion includes stderr in the message.
- `discover_targets(topo_yaml_path)` — reads `Topology.from_yaml()` and
  emits `(iface_targets, host_targets)`. Leaf `Ethernet0` per plane +
  host `eth1..eth<planes>`.
- `run_scrape_loop()` — `ThreadPoolExecutor(max_workers=16)`, 1s fast
  cadence (iface + snmp6), 5s slow cadence (MRC snapshots, larger
  payloads). Accepts `iterations` parameter for tests.
- `main()` — argparse front end with `--topo-yaml`, `--port`,
  `--interval`, `--slow-interval`, `--workers`, `--log-level`. Smoke-
  tests docker presence (rc=3 on missing) and prometheus_client
  availability (rc=2) before opening the HTTP server.

### Metrics surface (all Gauges; scrape-replace semantics)

- `srv6_iface_{rx,tx}_{bytes,packets,errors,dropped}` labeled
  `(plane, tier, node, iface)` — per-NIC counters.
- `srv6_host_<snmp6_key>` labeled `(host, tenant)` — 11 keys from
  `SNMP6_EXPOSED_KEYS`.
- `srv6_flow_ev_state` (0/1/2/3 enum), `_weight`, `_rtt_p50_seconds`,
  `_consecutive_probe_{successes,timeouts}`,
  `_demotes_suppressed_by_floor` labeled
  `(host, tenant, dst_id, plane, path)`.
- `srv6_flow_active_evs`, `srv6_flow_snapshot_captured_ns` labeled
  `(host, tenant, dst_id)`.
- `srv6_flow_probe_reply_{received,decode_failed,no_match,matched}_total`
  per flow.
- `srv6_flow_reply_latency_{lt_50ms,lt_200ms,lt_1s,lt_5s,ge_5s}_total`
  per flow (the no_match-localization signal from `813abcf`).
- `srv6_dispatch_replies_{received,no_peer,unknown_magic,dispatched_probe,dispatched_loss}_total`
  per host (daemon-wide; last write per tick wins).
- `srv6_scraper_scrape_duration_seconds`, `srv6_scraper_targets`,
  `srv6_scrape_up`, `srv6_scraper_exec_failures_total` — self-metrics.

### Design deltas vs `docs/design-visibility.md §6`

- **Counter → Gauge.** Every nominally-monotonic counter is a Gauge
  exposing the current absolute value. Reason: `prometheus_client`
  Counter is `.inc(delta)` only and refuses to go backwards on
  container restart; scrape-replace via Gauge is cleaner and
  `rate()` works identically.
- **`reply_latency_buckets` as 5 separate gauges**, not a Prom
  Histogram. The daemon already pre-bucketed; we forward counts.
- **`dispatch_stats` emitted once per host** (not per flow). The
  daemon publishes the same numbers in every snapshot file on a
  given host; last-write-wins is correct.

### Deferred to PR 2+

- **Spine NIC scraping.** `tier="spine"` is a valid label value but
  no spine targets are emitted. Adding
  `planes × spines_per_plane × leaves_per_plane` targets
  (`4 × 8 × 16 = 512` on 4p-8x16) would blow past the 1s cadence
  ceiling; sizing/cadence analysis is a PR 2 prerequisite.
- **Image build** (Dockerfile lives in `contrib/visibility-poc/scraper/`
  but isn't validated end-to-end).
- **Containerlab integration.** `generators/fabric.py` patching, the
  `visibility.yaml.fragment`, and `make deploy` wiring all live in
  PR 2.
- **Grafana provisioning YAMLs** (`provisioning/datasources/*.yaml`,
  `provisioning/dashboards/*.yaml`).
- **`nsenter` scrape path** for sub-second cadence at 4p-8x16 scale
  if `docker exec` cadence overhead becomes binding.
