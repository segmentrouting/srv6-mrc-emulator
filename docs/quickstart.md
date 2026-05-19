## Quickstart 4-Plane Containerlab Topology

The 4-plane topology deploys 96 dockerized sonic-vs routers and 32 light Alpine containers simulating hosts attached to the network. The Alpine containers are divided into tenants *`green`* and *`yellow`*, with one *`green`* and one *`yellow`* attached to each leaf.

Example: *`green-host00`* has 4 uplinks, one to *`leaf00`* in each of the 4 planes.

The **docker-sonic-vs** is pretty lightweight and takes up only 160MB of memory. That said, the lab has been tested on Ubuntu 22.04 and 24.04 virtual machines with 32 vCPU and 96GB of memory, which appears to be more than sufficient.

1. Download a **docker-sonic-vs** image that supports SRv6 uSID shift-and-forward

2. Install Containerlab: https://containerlab.dev/install/

3. Clone this repo
```bash
git clone https://github.com/segmentrouting/srv6-ai-fabric.git
```

```bash
cd ./srv6-ai-fabric
```

4. Build the Alpine-srv6-scapy docker image for our simulated hosts
```bash
make image
# equivalent to:
docker build -f host-image/Dockerfile \
             --build-arg TOPO=topologies/4p-8x16/topo.yaml \
             -t alpine-srv6-scapy:1.0 .
```

5. Deploy the topology
```bash
make deploy
# equivalent to:
sudo clab deploy -t topologies/4p-8x16/topology.clab.yaml
```

The topology will take a couple minutes to fully deploy. Once the containers have been up for 2+ minutes its safe to run the configuration script.

5. Check containers/nodes' status
```bash
docker ps
```

6. Run the **config** target to apply sonic *`config_db.json`* and *`frr.conf`* configs to each device (under `topologies/4p-8x16/config/`)
```bash
make config
# equivalent to:
scripts/config.sh all
```

It will take a couple minutes for the script to run through all 96 routers.
Once it has completed you should see output something like this:

```bash
============================================================
  sonic-docker-4p-8x16 — 4 planes x (8 spine x 16 leaf) SRv6 CLOS
============================================================
  Topology:     sonic-docker-4p-8x16 (from topology.clab.yaml)
  Config dir:   /home/cisco/srv6-ai-fabric/topologies/4p-8x16/config
  Routing:      Controller-driven (no BGP, no IGP)
  Tenants:      green (uDT d000 -> Vrf-green on every leaf)
                yellow (host-based; uDT d001 seg6local on hosts)
============================================================

Deploy complete!
```

### Quick test - Tenant Green - Host SRv6 Encap, Egress Leaf SRv6 uDT

1. Add a test route from *`green-host00`* to *`green-host15`* thru *`fabric plane-0`*

`Path: green-host00 -> p0-leaf00 -> p0-spine00 -> p0-leaf15 -> green-host15`

```bash
docker exec -it green-host00 ip -6 route add 2001:db8:bbbb:f::/64 encap seg6 mode encap.red segs fc00:0:f000:e00f:d000:: dev eth1
docker exec -it green-host15 ip -6 route add 2001:db8:bbbb::/64 encap seg6 mode encap.red segs fc00:0:f000:e000:d000:: dev eth1
```

2. Run a ping from *`green-host00`* to *`green-host15`*
```bash
docker exec -it green-host00 ping 2001:db8:bbbb:f::2 -i .3
```

3. In another terminal session run tcpdump on the sonic nodes' interfaces along the path:

tcpdump Plane-0 Leaf00 (*`p0-leaf00`*) ingress from *`green-host00`*
```bash
docker exec -it p0-leaf00 tcpdump -ni Ethernet32
```

We expect to see encapsulated echo requests and plain ipv6 echo replies (post uDT decapsulation):
```bash
$ docker exec -it p0-leaf00 tcpdump -ni Ethernet32
tcpdump: verbose output suppressed, use -v[v]... for full protocol decode
listening on Ethernet32, link-type EN10MB (Ethernet), snapshot length 262144 bytes
16:02:00.574771 IP6 2001:db8:bbbb::2 > fc00:0:f000:e00f:d000::: IP6 2001:db8:bbbb::2 > 2001:db8:bbbb:f::2: ICMP6, echo request, id 68, seq 51, length 64
16:02:00.576097 IP6 2001:db8:bbbb:f::2 > 2001:db8:bbbb::2: ICMP6, echo reply, id 68, seq 51, length 64
16:02:00.874927 IP6 2001:db8:bbbb::2 > fc00:0:f000:e00f:d000::: IP6 2001:db8:bbbb::2 > 2001:db8:bbbb:f::2: ICMP6, echo request, id 68, seq 52, length 64
16:02:00.875812 IP6 2001:db8:bbbb:f::2 > 2001:db8:bbbb::2: ICMP6, echo reply, id 68, seq 52, length 64
```


tcpdump *`p0-leaf00`* egress to *`p0-spine00`*
```
docker exec -it p0-leaf00 tcpdump -ni Ethernet0
```

We expect to see encapsulated traffic in both directions:
```bash
$ docker exec -it p0-leaf00 tcpdump -ni Ethernet0
tcpdump: verbose output suppressed, use -v[v]... for full protocol decode
listening on Ethernet0, link-type EN10MB (Ethernet), snapshot length 262144 bytes
16:03:46.535405 IP6 2001:db8:bbbb::2 > fc00:0:e00f:d000::: IP6 2001:db8:bbbb::2 > 2001:db8:bbbb:f::2: ICMP6, echo request, id 68, seq 404, length 64
16:03:46.536145 IP6 2001:db8:bbbb:f::2 > fc00:0:d000::: IP6 2001:db8:bbbb:f::2 > 2001:db8:bbbb::2: ICMP6, echo reply, id 68, seq 404, length 64
16:03:46.835564 IP6 2001:db8:bbbb::2 > fc00:0:e00f:d000::: IP6 2001:db8:bbbb::2 > 2001:db8:bbbb:f::2: ICMP6, echo request, id 68, seq 405, length 64
16:03:46.836538 IP6 2001:db8:bbbb:f::2 > fc00:0:d000::: IP6 2001:db8:bbbb:f::2 > 2001:db8:bbbb::2: ICMP6, echo reply, id 68, seq 405, length 64
```

tcpdump *`p0-spine00`* egress to *`p0-leaf15`* - expect SRv6 encapsulated traffic in both directions
```bash
docker exec -it p0-spine00 tcpdump -ni Ethernet60
```

tcpdump *`p0-leaf15`* ingress from *`spine00`* - expect SRv6 encapsulated traffic in both directions
```bash
docker exec -it p0-leaf15 tcpdump -ni Ethernet0
```

tcpdump *`p0-leaf15`* egress to *`green-host15`* - expect decapsulated echo requests and SRv6 encapsulated echo replies
```bash
docker exec -it p0-leaf15 tcpdump -ni Ethernet32
```

### Quick test - Tenant Yellow - Host SRv6 Encap and Decap

1. Add a test route from *`yellow-host01`* to *`yellow-host14`* via *`fabric plane-1`*

`Path: yellow-host01 -> p1-leaf01 -> p1-spine01 -> p1-leaf14 -> yellow-host14`

Phase 1a: yellow's inner DA is now anycast `2001:db8:cccc:<NN>::2` (on
`eth1..eth4` + `lo`, `nodad`) instead of the historical `cccd:<NN>::1`
on `lo`. The kernel `seg6 encap` route below targets the *peer*'s
anycast, which is not locally assigned, so the encap path works. The
spray / MRC data and control paths bypass kernel `seg6 encap` routes
entirely as of Phase 1b — they build the outer in user space via
`srv6_mrc/encap.py` (`build_outer_packet`). The manual route below
is kept as a debugging fallback so simple `ping`/`tcpdump` flows still
exercise SRv6 encap.

```bash
docker exec -it yellow-host01 ip -6 route add 2001:db8:cccc:e::2/128 encap seg6 mode encap.red segs fc00:1:f001:e00e:e009:d001:: dev eth1
docker exec -it yellow-host14 ip -6 route add 2001:db8:cccc:1::2/128 encap seg6 mode encap.red segs fc00:1:f001:e001:e009:d001:: dev eth1
```

2. Run a ping from *`yellow-host01`* to *`yellow-host14`*


The ping should be sourced from *`yellow-host01's`* anycast address: **-I 2001:db8:cccc:1::2**
```bash
docker exec -it yellow-host01 ping 2001:db8:cccc:e::2 -i .3 -I 2001:db8:cccc:1::2
```

3. tcpdump sequence:
```bash
docker exec -it p1-leaf01 tcpdump -ni Ethernet36
```
```bash
docker exec -it p1-leaf01 tcpdump -ni Ethernet4
```
```bash
docker exec -it p1-spine01 tcpdump -ni Ethernet4
```
```bash
docker exec -it p1-spine01 tcpdump -ni Ethernet56
```
```bash
docker exec -it p1-leaf14 tcpdump -ni Ethernet4
```
```bash
docker exec -it p1-leaf14 tcpdump -ni Ethernet36
```
```bash
docker exec -it yellow-host14 tcpdump -ni eth2
```

### Install Green and Yellow Tenant Test Routes

1. Run *`host-routes`* to install the full-mesh route set on hosts (every host can reach every other host of the same tenant; 1920 routes total):
```bash
make host-routes
# equivalent to:
routes apply -f topologies/4p-8x16/routes/full-mesh.yaml

# alternative: smaller 8-pair-per-tenant set for ad-hoc testing:
#   make host-routes ROUTES=reference-pairs
```

2. List the added routes:
```bash
routes list
```

Other ready-made specs in *`topologies/4p-8x16/routes/`*:
- *`full-mesh.yaml`* — every host talks to every other host across 4-planes (1920 routes)
- *`host00-fanout.yaml`* — host00 reaches all 15 peers across 4-planes (120 routes)

See *`routes --help`* for `delete -f`, `delete --all`, and `list` subcommands.

### Spray a flow across all 4 planes (MRC demo)

`spray` (source: `srv6_mrc/cli/spray.py`) is a userspace SRv6/uSID packet generator that splits a single logical flow round-robin across all 4 fabric planes — the **MRC/SRv6** model as described [Here](https://cdn.openai.com/pdf/resilient-ai-supercomputer-networking-using-mrc-and-srv6.pdf).

The `spray` CLI runs inside the Alpine host containers using a scapy-equipped image (`alpine-srv6-scapy:1.0`, built from `host-image/Dockerfile`). The `srv6_mrc` package is pip-installed into the image at build time, so `spray` lives at `/usr/local/bin/spray` inside every host. The image also bakes the matching `topo.yaml` at `/etc/srv6_mrc/topo.yaml` and exports `SRV6_TOPO`. No bind mounts are needed at runtime; rebuild the image (`make image`) when the package or `topo.yaml` changes.


Start the receiver on the destination host (sniffs all 4 NICs):
```bash
docker exec -it green-host15 spray --role recv
```

In another terminal, send 5 seconds of traffic from the source host:
```bash
docker exec -it green-host00 spray --role send \
    --dst-id 15 --rate 100pps --duration 5s
```

The receiver prints per-NIC and per-plane arrival counts. In a healthy fabric you should see ≈25% on each NIC and the per-plane counts matching exactly (plane *P* arrives on `eth(P+1)`).

**Note:** due to the large topology and entire simulation being CPU bound, the send side may not transmit the full pps. The goal is even spray distribution rather than high throughput.

To watch the wire while spraying, drop the rate and tap any hop — the outer is a uSID-compressed SID list (no SRH), `ip6 proto 41`:
```bash
docker exec -it green-host00 spray --role send \
    --dst-id 15 --rate 5pps --duration 60s &

docker exec -it p0-leaf00 tcpdump -ni Ethernet32 'ip6 proto 41'   # ingress leaf
docker exec -it p0-leaf00 tcpdump -ni Ethernet0  'ip6 proto 41'   # leaf -> spine (one uSID consumed)
docker exec -it p0-leaf15 tcpdump -ni Ethernet32 'udp port 9999'  # post-uDT6 decap
```

Yellow works the same way — the sender auto-detects tenant from its hostname and emits the longer SID list (`...e009:d001::`), and the receiver's BPF was widened to also catch the `ip6 proto 41` frames that arrive at the NIC before the host kernel's `seg6local End.DT6` fires. Yellow does require `make host-routes` first so the per-NIC seg6local policies are installed:

```bash
docker exec -it yellow-host15 spray --role recv
docker exec -it yellow-host00 spray --role send --dst-id 15 --rate 1000pps --duration 5s
```

See [`spray-protocol.md`](./spray-protocol.md) for the full packet diagram, per-tenant uSID-shift sequence, and limitations.

### Check SRv6 SIDs
```bash
for p in 0 1 2 3; do
  for l in $(seq -w 0 15); do
    n=$(docker exec p${p}-leaf${l} ip -6 route show table all 2>/dev/null | grep -cE "seg6local|End\.")
    printf "p%s-leaf%s: %s   " "$p" "$l" "$n"
  done
  echo
done
```
