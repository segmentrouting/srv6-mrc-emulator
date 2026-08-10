## Quickstart 4-Plane Containerlab Topology

The default 4-plane topology deploys with 4 spines and 8 leafs in each plane (48 dockerized sonic-vs routers total). It also deploys 16 light Alpine containers simulating hosts attached to the network. The Alpine containers are divided into tenants *`green`* and *`yellow`*, with one *`green`* and one *`yellow`* attached to each leaf.

Example: *`green-host00`* has 4 uplinks, one to *`leaf00`* in each of the 4 planes.

![Topology](./4p-4x8-topology.png)

The **docker-sonic-vs** is pretty lightweight and takes up only 160MB of memory. That said, the lab has been tested on Ubuntu 22.04 and 24.04 virtual machines with 32 vCPU and 96GB of memory, which appears to be more than sufficient.

### Pre-requisites

1. Download a **docker-sonic-vs** image that supports SRv6 uSID shift-and-forward. The `Branch Master` version on the public sonic downloads page works well: [docker-sonic-vs.gz](https://artprodcus3.artifacts.visualstudio.com/Af91412a5-a906-4990-9d7c-f697b81fc04d/be1b070f-be15-4154-aade-b1d3bfb17054/_apis/artifact/cGlwZWxpbmVhcnRpZmFjdDovL21zc29uaWMvcHJvamVjdElkL2JlMWIwNzBmLWJlMTUtNDE1NC1hYWRlLWIxZDNiZmIxNzA1NC9idWlsZElkLzExMTc1MDIvYXJ0aWZhY3ROYW1lL3NvbmljLWJ1aWxkaW1hZ2UudnM1/content?format=file&subpath=/target/docker-sonic-vs.gz)

2. Load the docker-sonic-vs image:
```bash
docker load -i docker-sonic-vs.gz
```

3. Pull custom alpine docker image (image has linux SRv6 and has Scapy installed to support the MRC traffic emulation scripts)
```bash
docker pull bmcdougall/alpine-srv6-scapy:1.0 
```

4. Tag the image for local deployment
```bash
docker tag bmcdougall/alpine-srv6-scapy:1.0 alpine-srv6-scapy:1.0 
```

5. Optional: verify images
```bash
docker images
```
```bash
cisco@topology-host:~/images$ docker images
REPOSITORY                     TAG       IMAGE ID       CREATED         SIZE
bmcdougall/alpine-srv6-scapy   1.0       e94cbf733792   21 hours ago    102MB
alpine-srv6-scapy              1.0       e94cbf733792   21 hours ago    102MB
docker-sonic-vs                latest    ebfc27f6b246   13 days ago     815MB
```

6. Install Containerlab: https://containerlab.dev/install/

7. Clone this repo and cd into the top level directory
```bash
git clone https://github.com/segmentrouting/srv6-mrc-emulator.git
```

```bash
cd ./srv6-mrc-emulator
```

### Make (deploy, config, host-routes)

>[!Note]
> the `make` commands in the following section default to the 4-plane 4x8 spine-leaf topology. 
> If you wish to work with another topology use `make TOPO=<topology-directory-name> deploy/config/etc.`
> Example `make TOPO=2p-4x8 deploy` will deploy the smaller 2-plane 4x8 spine-leaf topology


1. Deploy the topology
```bash
make deploy
```

Alternatively you can run the traditional `clab deploy`:
```bash
sudo clab deploy -t topologies/4p-4x8/topology.clab.yaml
```

Other topologies can be deployed (and configured, etc.) by specifying TOPO=<topology>
```bash
make TOPO=2p-4x8 deploy 
```

2. Run `make config` to apply sonic *`config_db.json`* and *`frr.conf`* configs to each device (under `topologies/4p-4x8/config/`)
```bash
make config
```

Alternatively you can run the `config.sh` shell script:
```bash
scripts/config.sh all
```

It will take a minute or two for the script to fully configure the fabric nodes

```bash
============================================================
  sonic-docker-4p-4x8 — 4 planes x (4 spine x 8 leaf) SRv6 CLOS
============================================================
  Topology:     sonic-docker-4p-4x8 (from topology.clab.yaml)
  Config dir:   /home/cisco/srv6-mrc-emulator/topologies/4p-4x8/config
  Routing:      Controller-driven (no BGP, no IGP)
  Tenants:      green (uDT d000 -> Vrf-green on every leaf)
                yellow (host-based; uDT d001 seg6local on hosts)
============================================================

Configuration complete!
```

3. Optional: install Green and Yellow Tenant Host Routes (useful for verifying paths/fabric-connectivity, etc.)
```bash
make host-routes
```

### Quick test - Tenant Green - Host SRv6 Encap, Egress Leaf SRv6 uDT
> [!Note]
> The host-routes script sets metrics such that ping tests will default to fabric plane-0

1. Run a ping from *`green-host00`* to *`green-host07`*
```bash
docker exec -it green-host00 ping 2001:db8:bbbb:7::2 -i .3 -I 2001:db8:bbbb::2
```

2. In another terminal session run tcpdump on the sonic nodes' interfaces along the path:

tcpdump Plane-0 Leaf00 (*`p0-leaf00`*) ingress from *`green-host00`*
```bash
docker exec -it p0-leaf00 tcpdump -ni Ethernet32
```

We expect to see encapsulated echo requests and plain ipv6 echo replies (post uDT decapsulation):
```bash
$ docker exec -it p0-leaf00 tcpdump -ni Ethernet32
tcpdump: verbose output suppressed, use -v[v]... for full protocol decode
listening on Ethernet32, link-type EN10MB (Ethernet), snapshot length 262144 bytes
17:16:39.628230 IP6 2001:db8:bbbb::2 > fc00:0:f003:e007:d000::: IP6 2001:db8:bbbb::2 > 2001:db8:bbbb:7::2: ICMP6, echo request, id 278, seq 11, length 64
17:16:39.628956 IP6 2001:db8:bbbb:7::2 > 2001:db8:bbbb::2: ICMP6, echo reply, id 278, seq 11, length 64
```

3. You can tcpdump along the entire path by following the uSID encapsulation pattern.

```
fc00:0:f003:e007:d000::
└──┬──┘└┬──┘└┬──┘└┬──┘
   │    │    │    └──── d000     : tenant-ID green leaf07 uDT Ethernet32
   │    │    └───────── e007     : spine03 uA Ethernet28 toward leaf07
   │    └────────────── f003     : leaf00 uA Ethernet8 toward spine03 
   └─────────────────── fc00:0:  : plane 0 block
```
   
```bash
# p0-leaf00 egress to p0-spine03
docker exec -it p0-leaf00 tcpdump -ni Ethernet8

# p0-spine03 egress to p0-leaf07
docker exec -it p0-spine00 tcpdump -ni Ethernet28
```

### Quick test - Tenant Yellow - Host SRv6 Encap and Decap

1. Run a ping from *`yellow-host01`* to *`yellow-host04`* . Packet capture/tcpdump procedure is the same.
```bash
docker exec -it yellow-host01 ping 2001:db8:cccc:4::2 -i .3 -I 2001:db8:cccc:1::2
```

For other quick tests see the [spray-tool.md](./spray-tool.md)


## srctl command line utility
*`srctl`* is a simple CLI modeled after K8s `kubectl` and can be used to interact with the MRC-SRv6 emulator. 
`srctl's` primary use is to run MRC traffic scenarios over the deployed topology.

1. Install `srctl` python packages
```bash
pip install -e . --user
```

2. Test `srctl` was installed
```bash
which srctl
```

If *`~/.local/bin`* isn't in your PATH:
```bash
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc && source ~/.bashrc
```

**`srctl` help**
```bash
srctl --help
```

```bash
$ srctl -h
usage: srctl [-h] {get,run,fault} ...

SRv6 fabric emulator control CLI

positional arguments:
  {get,run,fault}
    get            inspect topology / hosts / EVs
    run            execute a traffic scenario by name (or --list)
    fault          inject/clear/list faults in the fabric

options:
  -h, --help       show this help message and exit
```

```bash
srctl get {topology,hosts,evs}
```

**srctl get topology**

`srctl` auto-discovers scenarios under topologies/<active>/scenarios/ (where "active" is determined by _active_topo_dir(). If you have multiple topologies and want to be explicit, set TOPO=4p-4x8 srctl run … if it honors that, or check srctl get topology to see which one srctl thinks is active.

Quick sanity check to verify it sees your deployed topology:
```bash
srctl get topology
```

**srctl get evs**

Example: `srctl get evs` <src-host> <dst-host>
```bash
srctl get evs yellow-host05 yellow-host02
```

Will output the list of EVs between any src/dst host pair and their corresponding SRv6 SIDs
```bash
$ srctl get evs yellow-host05 yellow-host02
PLANE  PATH  EV     SID                            
0      0     P0:S0  fc00:0000:f000:e002:e009:d001::
0      1     P0:S1  fc00:0000:f001:e002:e009:d001::
0      2     P0:S2  fc00:0000:f002:e002:e009:d001::
0      3     P0:S3  fc00:0000:f003:e002:e009:d001::
1      0     P1:S0  fc00:0001:f000:e002:e009:d001::
1      1     P1:S1  fc00:0001:f001:e002:e009:d001::
1      2     P1:S2  fc00:0001:f002:e002:e009:d001::
1      3     P1:S3  fc00:0001:f003:e002:e009:d001::
2      0     P2:S0  fc00:0002:f000:e002:e009:d001::
2      1     P2:S1  fc00:0002:f001:e002:e009:d001::
2      2     P2:S2  fc00:0002:f002:e002:e009:d001::
2      3     P2:S3  fc00:0002:f003:e002:e009:d001::
3      0     P3:S0  fc00:0003:f000:e002:e009:d001::
3      1     P3:S1  fc00:0003:f001:e002:e009:d001::
3      2     P3:S2  fc00:0003:f002:e002:e009:d001::
3      3     P3:S3  fc00:0003:f003:e002:e009:d001::
```

### srctl run - running MRC traffic scenarios:

1. List available traffic scenarios for the active topology:
```bash
srctl run --list
```

>[!Note]
> Use mrc and non-mrc options with a fault in the network to see how probes/mrc logic 
> causes the transmitting host to deprecate the faulted EV and rebalance traffic 
> around the fault. Non-mrc mode will simply blackhole a percentage of traffic.

Example output
```bash
$ srctl run --list
green-all-to-all         # models all-to-all collective for tenant green
green-allreduce-ring     # models all-reduce collective ring for tenant green
green-baseline           # basic traffic gen smoke test - 4 hosts communicate as pairs over SRv6 fabric
green-ev-spray           # basic traffic spray smoke test - 4 hosts communicate as pairs, traffic sprayed across all EVs
green-mrc-baseline       # a generic 'pairs' traffic pattern with MRC probes to establish base functionality
green-mrc-ev-spray       # Same as the above `ev-spray` but with MRC probes enabled. 

# yellow tenant versions of the above
yellow-all-to-all
yellow-allreduce-ring
yellow-baseline
yellow-ev-spray
yellow-mrc-baseline
yellow-mrc-ev-spray
```

2. Run a traffic scenario:
```bash
srctl run yellow-baseline
```

3. Or with options:
```bash
srctl run yellow-allreduce-ring --verbose
```
```bash
srctl run green-mrc-baseline --dry-run
```

>[!Note]
> When running any traffic scenario the emulator sprays SRv6 encapsulated traffic across all available EVs (paths).
> You should be able to run tcpdump on any spine interface in the fabric and see some encapsulated traffic

```bash
docker exec -it p0-spine02 tcpdump -ni Ethernet8

docker exec -it p2-spine01 tcpdump -ni Ethernet20

# etc
```

### srctl `fault` and MRC traffic re-balance on failure

To see MRC rebalance on probe failure, use `srctl fault` commands to inject failures before or during a MRC scenario run:

**Example 1: Shutdown a link and observe MRC rebalance**

```bash
# Break p1-spine01 ↔ p1-leaf00 link (bidirectional by default)
srctl fault shutdown p1-spine01 Ethernet0

# Run MRC scenario - should detect failure and route around it
srctl run green-mrc-ev-spray

# Run a non-MRC scenario - the report should identify where packet loss ocdurred

# View active faults
srctl fault list

# Restore the link
srctl fault clear p1-spine01
```

**Example 2: Inject partial loss with tc/netem**

```bash
# Add 5% loss on plane 2 of a specific host
srctl fault netem "host yellow-host00 plane 2" "loss 5%"

# Run scenario - expect MRC to demote affected plane
srctl run yellow-mrc-baseline --duration 10

# Clean up all faults
srctl fault clear --all
```

**Example 3: Break an entire spine (all downlinks)**

```bash
# Shut down all interfaces on p0-spine01
srctl fault shutdown p0-spine01 all

# Run collective communication scenario
srctl run yellow-all-to-all --duration 30

# Restore everything
srctl fault clear --all
```

For more details on fault injection, see the `srctl fault` section in [AGENTS.md](../AGENTS.md).

## Appendix

### MRC traffic generation scenarios using `make`

1. Basic EV spray, no MRC state or failure detection
```bash
# Open-loop EV-spray (data-path validation, no MRC feedback, provides EV SRv6 encap list)
make scenario SCEN=green-ev-spray 
make scenario SCEN=yellow-ev-spray
```

2. Basic MRC scenarios
```bash
# Green MRC scenarios (all use health_aware_mrc)
make scenario SCEN=green-mrc-baseline        # 4 EVs/pair, 5s clean — sanity: MRC is no-op
make scenario SCEN=green-mrc-ev-spray        # 32 EVs/pair, 60s — manually shutdown a port to see rebalance
```

```bash
# Yellow MRC scenarios
make scenario SCEN=yellow-mrc-baseline       # same as green, host-decap data path
make scenario SCEN=yellow-mrc-ev-spray       # same as green-mrc-ev-spray, host-decap
```

 - Run tcpdumps anywhere in the fabric to see SRv6 encapsulated MRC (UDP port 9999) or probe (UDP port 9998) traffic:
```bash
docker exec -it p1-leaf00 tcpdump -ni Ethernet0
docker exec -it p1-leaf00 tcpdump -ni Ethernet4
docker exec -it p1-leaf00 tcpdump -ni Ethernet8
docker exec -it p1-leaf00 tcpdump -ni Ethernet12

docker exec -it p1-spine00 tcpdump -ni Ethernet60

# etc.
```

## Running a different topology

Each variant lives under `topologies/<name>/` with its own `topo.yaml`
declaring planes / spines / leaves / images / clab name. To run the
existing 2-plane variant:

```bash
make TOPO=2p-4x8 regen deploy config host-routes
make TOPO=2p-4x8 scenario SCEN=yellow-baseline
```

`make image` only needs to run once -- the same host image
(`alpine-srv6-scapy:1.0`) serves every topology, because each variant's
`topo.yaml` is bind-mounted into its host containers at runtime (via
the generated `topology.clab.yaml`). Inside a container, the runtime
reads `SRV6_TOPO=/etc/srv6_mrc/topo.yaml`. Outside containers (lab
host, dev box), it reads `topologies/<name>/topo.yaml` relative to the
repo root, picking the active variant from `TOPO=`.

To add a new variant, copy an existing `topologies/<name>/topo.yaml`,
adjust the dimensions, and run `make TOPO=<new> regen`. The generator
emits a fresh `topology.clab.yaml` plus per-node `config/` from
scratch.

## Testing

```bash
make test     # ~1.5s, no external deps
```