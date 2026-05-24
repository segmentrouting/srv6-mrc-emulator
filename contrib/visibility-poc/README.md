# visibility-poc — skeleton

**Status: design-only skeleton. Nothing here runs against the lab
today. See `docs/design-visibility.md` for the architecture this
fills in.**

This directory is the staging area for the live visibility /
Grafana dashboard work. It is intentionally outside `srv6_mrc/`,
`generators/`, and `topologies/` so it cannot accidentally affect
production code paths. When the implementation lands, the contents
move as follows:

- `containerlab/visibility.yaml.fragment` →
  emitted into `topologies/<name>/topology.clab.yaml` by
  `generators/fabric.py` when `topo.yaml` has
  `visibility.enabled: true`.
- `scraper/` → builds the `srv6-scraper:1.0` docker image; the
  source can stay under `contrib/` or graduate to a top-level
  directory once stable.
- `prometheus/prometheus.yml` → bind-mounted into
  `srv6-prometheus` by the clab fragment.
- `grafana/dashboards/*.json` and `grafana/provisioning/` →
  bind-mounted into `srv6-grafana`.

---

## Layout

```
contrib/visibility-poc/
  README.md                              this file
  scraper/
    scraper.py                           Python scraper skeleton (stubs)
    Dockerfile                           image build (not yet written)
    requirements.txt                     prometheus_client + pyyaml
  prometheus/
    prometheus.yml                       Prometheus config template
  grafana/
    dashboards/
      per-plane-balance.json             headline dashboard JSON
    provisioning/
      datasources/
        prometheus.yaml                  (not yet written; trivial)
      dashboards/
        srv6.yaml                        (not yet written; trivial)
  containerlab/
    visibility.yaml.fragment             clab YAML block to merge into topology.clab.yaml
```

## Dev-test path (without the full lab)

You can iterate on the scraper alone against a stub docker
environment by:

1. Stand up two arbitrary alpine containers locally:
   ```
   docker run -d --name fake-leaf alpine sleep infinity
   docker run -d --name fake-host alpine sleep infinity
   ```
2. Have the scraper read a hand-written `topo.yaml` listing those two
   containers as fabric/host.
3. Run the scraper as `python3 scraper.py --topo path/to/fake-topo.yaml`.
4. `curl http://localhost:9100/metrics` and confirm Prometheus-format
   output.

This exercises `docker_exec_with_retry()`, the parser stubs, and the
`prometheus_client` plumbing without needing the SRv6 fabric. Once
the scraper is solid against fake containers, plug it into the real
lab via the clab fragment.

For dashboard work, run Prometheus + Grafana standalone via
`docker compose` against `prometheus.yml` and a pre-recorded metric
dump (`textfile_collector` style). The clab fragment is only needed
for the full integrated end-to-end test.

## What's stubbed vs done

| Component                             | Status   |
|---------------------------------------|----------|
| Architecture decision (docker exec)   | Done     |
| Metric naming scheme                  | Done     |
| Scraper Python skeleton + retry helper| Stubbed  |
| Per-source parsing logic              | Stubbed  |
| Dockerfile + image build              | Not done |
| Prometheus config template            | Done (template) |
| Grafana dashboard JSON                | Stubbed (headline panel only) |
| Grafana provisioning YAML             | Not done |
| `generators/fabric.py` integration    | Not done |
| Makefile target                       | Not done |

## Hard constraints inherited from AGENTS.md

- Retry `docker exec cat` on rc=1 with 100 ms backoff (×3). The
  helper is in `scraper.py`.
- Treat `docker exec` stdout as unreliable for large payloads:
  validate JSON parse before use; on parse failure, mark sample
  missing and continue.
- Never modify `srv6_mrc/`, `generators/`, or `topologies/<name>/`
  from this skeleton. The integration patch into
  `generators/fabric.py` is a separate PR.
- The snapshot envelope is
  `{captured_ns, dst_id, ev_state, src_host, src_id, tenant,
  transport_stats}` with `.ev_state.tenants["<tenant>"]` as a
  **list** of per-EV dicts. Do not assume it is a dict.
