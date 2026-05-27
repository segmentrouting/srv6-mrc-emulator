# Live Visibility Stack

**Status: Scraper implementation complete (PR 1 landed). Integration work in progress (PR 2).**

Live Grafana dashboard for observing SRv6 spray balance, MRC EV health, and fault-injection effects in real time during scenario runs.

See `docs/design-visibility.md` for full architecture and design.

---

## Quick Start

### Run unit tests

```bash
PYTHONPATH=. python3 -m unittest discover -s tests/contrib -t .
```

30 tests covering parsers, retry logic, and target discovery. No docker required.

### Build scraper image

```bash
docker build -t srv6-scraper:1.0 visibility/scraper/
```

### Deploy with fabric (once integrated)

```bash
make deploy    # stands up fabric + visibility stack
# Access Grafana at http://<topology-host>:3000
```

---

## Architecture

Three containers deployed alongside the fabric:

- **srv6-scraper** - Polls fabric/host containers via `docker exec`, exposes Prometheus metrics on :9100
- **prometheus** - Scrapes metrics every 1s, TSDB retention 1h (in-memory)
- **grafana** - Dashboard UI on :3000, auto-provisioned datasource + dashboards

All declared in `topology.clab.yaml` so `make destroy` tears them down with the lab.

---

## Layout

```
visibility/
  README.md                              this file
  scraper/
    scraper.py                           Prometheus exporter (~1060 lines)
    Dockerfile                           Image build
    requirements.txt                     prometheus_client + pyyaml
  prometheus/
    prometheus.yml                       Prometheus config template
  grafana/
    dashboards/
      per-plane-balance.json             Headline dashboard JSON
    provisioning/
      datasources/
        prometheus.yaml                  (to be created in PR 2)
      dashboards/
        srv6.yaml                        (to be created in PR 2)
  containerlab/
    visibility.yaml.fragment             Clab YAML fragment for topology.clab.yaml
```

---

## Implementation Status

### ✅ Complete (PR 1)

- Scraper implementation (`scraper/scraper.py`)
- 30 unit tests (all passing)
- Parser implementations:
  - `parse_ip_link_stats()` - fabric NIC stats (pps/bps)
  - `parse_snmp6()` - host kernel IPv6/UDP6 counters
  - `parse_mrc_snapshot()` - MRC EV health from `/dev/shm/srv6-mrc/`
- `docker_exec_with_retry()` - implements AGENTS.md retry pattern
- `discover_targets()` - auto-discovers leaf + host targets from topo.yaml
- Prometheus metrics exposure via prometheus_client
- Architecture and design doc

### 🚧 In Progress (PR 2)

- Image build validation
- Grafana provisioning YAMLs
- `generators/fabric.py` integration (emit visibility.yaml.fragment)
- Makefile targets (`make deploy` wiring)
- Dashboard completion (beyond headline panel)

### ⏸️ Deferred

- Spine NIC scraping (sizing analysis needed for 512 targets on 4p-8x16)
- `nsenter` optimization (if `docker exec` cadence becomes bottleneck)

---

## Data Sources

Three sources, all polled via `docker exec`:

1. **Fabric NICs** - `ip -stats link show <iface>` on leaves → per-plane pps/bps
2. **Host kernel stats** - `cat /proc/net/snmp6` → Udp6RcvbufErrors, etc.
3. **MRC snapshots** - `/dev/shm/srv6-mrc/<host>/<tenant>_<dd>.json` → EV health state

See `docs/design-visibility.md` §4-6 for metric naming and Prometheus label schemas.

---

## Testing Without Full Lab

### Stub docker smoke test

Iterate on the scraper against stub containers:

1. Stand up fake containers:
   ```bash
   docker run -d --name fake-leaf alpine sleep infinity
   docker run -d --name fake-host alpine sleep infinity
   ```

2. Create minimal `fake-topo.yaml` listing those containers

3. Run scraper:
   ```bash
   python3 visibility/scraper/scraper.py --topo fake-topo.yaml
   ```

4. Check metrics:
   ```bash
   curl http://localhost:9100/metrics
   ```

This exercises parsers and `docker_exec_with_retry()` without needing the SRv6 fabric.

---

## Hard Constraints (from AGENTS.md)

- Retry `docker exec cat` on rc=1 with 100ms backoff (×3)
- Treat `docker exec` stdout as unreliable for large payloads (validate JSON parse)
- MRC snapshot envelope format:
  ```json
  {
    "captured_ns": ...,
    "dst_id": ...,
    "ev_state": {
      "tenants": {
        "<tenant>": [ /* list of per-EV dicts */ ]
      }
    },
    "transport_stats": ...
  }
  ```
