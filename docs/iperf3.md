# iperf3 — quick bandwidth test between hosts

The lab host image includes `iperf3` for simple throughput checks over the SRv6 fabric. This example sends traffic from `green-host00` to `green-host04` on the default `4p-4x8` topology.

## Prerequisites

1. Deploy and configure the lab (from repo root):

```bash
make deploy
make config
make host-routes
```

2. Rebuild the host image if you added `iperf3` recently:

```bash
make image
```

Redeploy hosts (or restart containers) so they pick up the new image.

## Addresses

| Host          | Inner (tenant) address   |
| ------------- | ------------------------ |
| green-host00  | `2001:db8:bbbb:00::2`    |
| green-host04  | `2001:db8:bbbb:04::2`    |

Traffic uses the green tenant's kernel `encap seg6` routes installed by `make host-routes`. By default the kernel picks plane 0 (lowest route metric).

Green hosts are multi-homed (four plane uplinks) with the same anycast inner address on every NIC. **Set the inner source address explicitly** (`-B` for iperf3, `-I` for ping) so the kernel selects the tenant address and matching SRv6 encap route. Without it, connectivity checks often fail even when `make host-routes` is correct.

## Run the test

Start an iperf3 server on the receiver (`green-host04`):

```bash
docker exec -d green-host04 iperf3 -s
```

Run the client on the sender (`green-host00`) for a 10-second TCP test:

```bash
docker exec green-host00 iperf3 -c 2001:db8:bbbb:04::2 -t 10 \
  -M 1200 -B 2001:db8:bbbb:00::2
```

`-M 1200` clamps the TCP MSS for SRv6 double-encapsulation overhead on the fabric path (see [Troubleshooting](#troubleshooting) below). `-B` binds the inner source; `2001:db8:bbbb:00::2` is host00's tenant address (often printed in collapsed form as `2001:db8:bbbb::2`).

You should see matching sender/receiver totals (~400+ Mbits/sec on a healthy lab). Stop the background server when finished:

```bash
docker exec green-host04 pkill iperf3
```

Quick reachability check with the same source binding:

```bash
docker exec green-host00 ping -I 2001:db8:bbbb:00::2 -c 3 2001:db8:bbbb:04::2
```

## Optional: pin to one fabric plane

Each host uplink maps to one plane (`eth1` = plane 0, `eth2` = plane 1, …). Add `--bind-dev` to force egress on a specific NIC (keep `-M` and `-B`):

```bash
# plane 2 only (eth3)
docker exec green-host00 iperf3 -c 2001:db8:bbbb:04::2 -t 10 \
  -M 1200 -B 2001:db8:bbbb:00::2 --bind-dev eth3
```

Without `--bind-dev`, the kernel picks a plane from the installed route metrics (plane 0 by default after `make host-routes`). For multi-plane spray and balance reporting, use [spray-tool.md](./spray-tool.md) instead.

## Troubleshooting

### Stalls after ~256 KBytes; receiver shows 0 bytes

SRv6 encap shrinks the effective path MTU. TCP may send segments the fabric drops silently (PMTUD black hole). Symptom: the session connects, one small burst transfers, then throughput goes to zero.

Fix: add `-M 1200` (or lower). Probe with ping:

```bash
docker exec green-host00 ping -I 2001:db8:bbbb:00::2 -c 3 -s 1400 2001:db8:bbbb:04::2
```

If large payloads fail but default-size ping works, MSS clamping is the answer.

### Ping or iperf fails without `-I` / `-B`

Use the sender's tenant inner address as the source. On `green-host00` that is `2001:db8:bbbb:00::2`.

## See also

- [quickstart.md](./quickstart.md) — deploy, config, and ping/tcpdump walkthrough
- [spray-tool.md](./spray-tool.md) — userspace SRv6 spray with per-plane balance reporting
