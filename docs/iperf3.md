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

## Run the test

Start an iperf3 server on the receiver (`green-host04`):

```bash
docker exec -d green-host04 iperf3 -s
```

Run the client on the sender (`green-host00`) for a 10-second TCP test:

```bash
docker exec green-host00 iperf3 -c 2001:db8:bbbb:04::2 -t 10
```

You should see a summary with bandwidth (Mbits/sec) and retransmits. Stop the background server when finished:

```bash
docker exec green-host04 pkill iperf3
```

## Optional: pin to one fabric plane

Each host uplink maps to one plane (`eth1` = plane 0, `eth2` = plane 1, …). Green hosts use the same anycast inner address on every NIC, so bind the client to the device to force egress on that plane:

```bash
# plane 2 only (eth3)
docker exec green-host00 iperf3 -c 2001:db8:bbbb:04::2 --bind-dev eth3 -t 10
```

Without `--bind-dev`, the kernel picks a plane from the installed route metrics (plane 0 by default after `make host-routes`). For multi-plane spray and balance reporting, use [spray-tool.md](./spray-tool.md) instead.

## See also

- [quickstart.md](./quickstart.md) — deploy, config, and ping/tcpdump walkthrough
- [spray-tool.md](./spray-tool.md) — userspace SRv6 spray with per-plane balance reporting
