# Runbook: stateless-probe (host-turnaround uDT) µSID round-trip validation

Status: pre-design lab validation. No code change in this RFC; only
hand-built kernel state on the lab. The goal is to prove the 6-slot
µSID round-trip design works end-to-end before committing to the
implementation branch (`feature/stateless-probes`).

## Background (read this first, the design depends on it)

Cycles 8-13 collectively ruled out every measured layer (receiver,
sender emit, sender qdisc, sender kernel-rx dwell, reply handler,
dispatch gap/backlog) as the source of the 22-second `reply_age_max_ns`
tail under all-to-all on `4p-4x8`. Cycle 13 confirmed kernel-rx dwell
is sub-millisecond for ~95% of packets while `reply_age_avg_ns` is
7-9 seconds per matched reply, and `reply_age_count` is only ~12-15%
of `replies_received` (i.e., most replies are received fine but
rejected at the match step in `probe_clock.match_reply`). The
stateful `(plane, path, req_id, tx_ns)` match table is the binding
bottleneck, not any I/O layer.

The structural fix is to remove the match table entirely. Replace
"sender emits probe, receiver decodes + reconstructs reply, sender
match-keys on tx_ns" with "sender emits probe, the probe round-trips
through the fabric and the peer host's kernel forwarding plane, and
arrives back at the sender's recv socket byte-identical (modulo
hop-limit decrements)". No userland involvement on the peer side.
No timing-sensitive correlation. EV identity is encoded directly in
the inner-dst (one /128 per `(plane, path)` per host on the host's
NICs); peer-host identity (`dst_id` in MRC terminology) is encoded
in the probe payload.

## The addressing scheme

For 4p-4x8 (NUM_SPINES=4, so 4 EVs per plane = 16 EVs per
`(src_host, dst_host)` pair):

Each host gets 16 per-EV unicast addresses, distributed 4-per-NIC:

  host00 eth1 (plane 0): cccc::100, ::101, ::102, ::103
  host00 eth2 (plane 1): cccc::110, ::111, ::112, ::113
  host00 eth3 (plane 2): cccc::120, ::121, ::122, ::123
  host00 eth4 (plane 3): cccc::130, ::131, ::132, ::133

  host01 eth1: cccc:1::100..::103
  host01 eth2: cccc:1::110..::113
  ... etc

These are in addition to the existing `cccc::2` (per-NIC anycast for
the data path) on every NIC.

Each leaf gets ONE `End.DT6` entry on the µSID `fc00:0:dfff::` (the
"d" hextet says uDT, `fff` reserved for the probe-only entry). The
entry decaps the outer header and routes the inner packet via the
leaf's main table — which already has a connected route for that
leaf's attached host's tenant subnet (cccc::/64 for host00, cccc:1::/64
for host01, etc).

## The µSID walk (host00 probes EV0 toward host01 via plane 0 spine 0)

Outer DA at emission: `fc00:0:f000:e001:e009:f000:e000:dfff`
Inner dst: `cccc::100` (host00's own per-EV address for p0-ev0)
Inner src: `cccc::ffff` (reserved, never configured on any host)

Six active µSID slots: `f000 e001 e009 f000 e000 dfff`

1. host00 emits via raw socket on eth1 (plane 0).
2. p0-leaf00 uA-pops `f000` (leaf-up to spine on plane 0).
   New outer DA: `fc00:0:e001:e009:f000:e000:dfff::`
3. p0-spine00 uA-pops `e001` (spine-down to leaf01).
   New outer DA: `fc00:0:e009:f000:e000:dfff::`
4. p0-leaf01 uA-pops `e009` (leaf-to-host).
   New outer DA: `fc00:0:f000:e000:dfff::`
   Packet egresses leaf01's Ethernet36 toward host01.
5. host01 receives on eth1. Inner-dst `cccc::100` is NOT locally
   configured on host01 (it's host00's address). Kernel FIB lookup
   on outer DA: matches `fc00::/32 via 2001:db8:cccc:1::1 dev eth1`.
   Forwards back out eth1 to leaf01.
6. p0-leaf01 uA-pops `f000` (leaf-up to spine on plane 0, return).
   New outer DA: `fc00:0:e000:dfff::`
7. p0-spine00 uA-pops `e000` (spine-down to leaf00).
   New outer DA: `fc00:0:dfff::`
8. p0-leaf00 — the slot is `dfff`, which is the probe-only `End.DT6`
   entry. Strip outer. Inner packet: src=`cccc::ffff`, dst=`cccc::100`.
   Route via main table.
9. cccc::/64 is connected via Ethernet36 (toward host00). Forward.
10. host00 receives on eth1. Inner-dst `cccc::100` claims locally.
    Daemon's recv socket receives the packet.

EV identity at step 10 = inner-dst `cccc::100` -> (plane 0, ev 0).
Peer-host identity (`dst_id`) lives in payload (1 byte stamped by
sender at emission).

## What this runbook validates

ONE hand-built case: host00 probes the EV0 path through plane 0 spine 0
toward host01, returns to host00. If this works end-to-end, the design
is structurally sound. We do NOT need to validate all 16 EVs by hand;
the generator and runtime code will handle the full set if the single
case works.

## Required pre-state

* Lab on `cisco` host, HEAD `02cd48a` or later on origin/main.
* Topology `4p-4x8` deployed clean: `SUDO= make deploy && make config
  && make host-routes` (the cycle-13 sequence; missing any of these
  yields 100% loss).
* `ssh cisco` works key-based.
* No prior in-container manual state (this runbook is repeatable
  from a clean deploy).

## Validation steps

All commands are run via `ssh cisco '<cmd>'`. Group them in a single
ssh session per phase to avoid the docker-exec frozen-counters gotcha
(cycle 12).

### Phase 1 — confirm clean state

```
ssh cisco 'cd ~/srv6-mrc-emulator && git log --oneline -1 && \
  docker exec yellow-host00 ip -6 addr show dev eth1 | grep cccc && \
  docker exec yellow-host01 ip -6 addr show dev eth1 | grep cccc'
```

Expected: HEAD is `02cd48a` or later. yellow-host00 eth1 has
`2001:db8:cccc::2/64`. yellow-host01 eth1 has `2001:db8:cccc:1::2/64`.

### Phase 2 — add the per-EV address on the sender

```
ssh cisco 'docker exec yellow-host00 ip -6 addr add 2001:db8:cccc::100/64 dev eth1 nodad && \
  docker exec yellow-host00 ip -6 addr show dev eth1 | grep -E "cccc::(2|100)"'
```

Expected: both `cccc::2` and `cccc::100` show up on eth1.

Important: `cccc::100` is added ONLY on yellow-host00 (the sender).
It must NOT be added on yellow-host01 — that's what makes step 5 of
the walk forward the packet instead of claiming it locally.

### Phase 3 — add the dfff End.DT6 entry on both leaves

The leaves are SONiC + FRR. `seg6local End.DT6` rules are managed
via FRR or via direct kernel route addition (the lab uses both
patterns elsewhere). For this validation, use direct kernel addition
inside the leaf's network namespace so it's reversible without
config push:

```
ssh cisco 'docker exec p0-leaf00 ip -6 route add fc00:0:dfff::/48 encap seg6local action End.DT6 table 254 dev lo && \
  docker exec p0-leaf00 ip -6 route show fc00:0:dfff::/48'
ssh cisco 'docker exec p0-leaf01 ip -6 route add fc00:0:dfff::/48 encap seg6local action End.DT6 table 254 dev lo && \
  docker exec p0-leaf01 ip -6 route show fc00:0:dfff::/48'
```

Expected: route shows up with `encap seg6local End.DT6 table 254` on
both leaves. Table 254 is `main` on Linux.

Verify by inspecting the actual kernel state:

```
ssh cisco 'docker exec p0-leaf00 ip -6 route show table all | grep dfff'
```

If the route is missing or the `encap seg6local` keyword isn't
present, abort and report — the SONiC kernel may need a different
syntax (some SONiC images require `dev <devname>` to be a real
interface in the seg6local namespace, not `lo`). Try `dev Ethernet0`
as a fallback before reporting.

### Phase 4 — scapy-build and send the probe

The probe outer DA encodes the round-trip µSID list. Use scapy
inside yellow-host00 (the image has scapy preinstalled per
host-image/Dockerfile). Run tcpdump in parallel on every fabric
hop. Single ssh session.

In one terminal, start captures:

```
ssh cisco 'docker exec yellow-host00 tcpdump -nei eth1 -c 4 ip6 and dst net fc00::/32 or src net cccc::/32 &
docker exec p0-leaf00 tcpdump -nei Ethernet0 -c 2 ip6 and dst net fc00::/32 &
docker exec p0-spine00 tcpdump -nei Ethernet0 -c 2 ip6 and dst net fc00::/32 &
docker exec p0-leaf01 tcpdump -nei Ethernet36 -c 2 ip6 and dst net fc00::/32 &
docker exec yellow-host01 tcpdump -nei eth1 -c 4 ip6 and dst net fc00::/32 &
wait'
```

(Adjust the spine/leaf NIC names if the topology differs; check
`docker exec p0-leaf00 ip -br link show` first if unsure.)

In the same ssh session, after captures are armed, send ONE probe:

```
ssh cisco 'docker exec yellow-host00 python3 -c "
from scapy.all import IPv6, UDP, sendp, Ether, Raw
import socket

# Outer = µSID round trip; six active slots in big-endian µSID order
outer_dst = \"fc00:0:f000:e001:e009:f000:e000:dfff\"
inner_dst = \"2001:db8:cccc::100\"
inner_src = \"2001:db8:cccc::ffff\"

pkt = (
    IPv6(src=\"2001:db8:cccc::2\", dst=outer_dst, nh=41)
    / IPv6(src=inner_src, dst=inner_dst)
    / UDP(sport=9999, dport=9997)
    / Raw(load=b\"PROBE-EV0-DST1\")
)
sendp(Ether() / pkt, iface=\"eth1\", verbose=False)
print(\"sent\")
"'
```

Note: this uses `sendp` at L2. Alternative is a raw socket at L3;
match whatever the existing `srv6_mrc/encap.py` uses to ensure the
outer-header build is equivalent.

### Phase 5 — interpret captures

Expected sightings:

* yellow-host00 eth1 OUT: outer src=`cccc::2`, outer dst=
  `fc00:0:f000:e001:e009:f000:e000:dfff`, inner src=`cccc::ffff`,
  inner dst=`cccc::100`.
* p0-leaf00 Ethernet0 (the uplink to spine on plane 0) OUT: outer
  dst rewritten to `fc00:0:e001:e009:f000:e000:dfff::`.
* p0-spine00 Ethernet0 (downlink to leaf01) OUT: outer dst
  `fc00:0:e009:f000:e000:dfff::`.
* p0-leaf01 Ethernet36 (downlink to yellow-host01) OUT: outer dst
  `fc00:0:f000:e000:dfff::`.
* yellow-host01 eth1 IN: outer dst `fc00:0:f000:e000:dfff::`,
  inner dst `cccc::100`. tcpdump `-Q in`.
* yellow-host01 eth1 OUT: SAME outer dst (host01 forwards without
  rewriting; the kernel's plain IPv6 forward path doesn't consume
  µSIDs). tcpdump `-Q out`. **CRITICAL CHECK — this is the moment
  the round trip survives or dies.**
* p0-leaf01 Ethernet36 IN: same packet returning.
* p0-leaf01 Ethernet0 OUT: outer dst `fc00:0:e000:dfff::`.
* p0-spine00 Ethernet0 OUT (other direction): outer dst
  `fc00:0:dfff::`.
* p0-leaf00 sees outer dst `fc00:0:dfff::`, executes `End.DT6`,
  decaps. The inner packet emerges on its main table.
* yellow-host00 eth1 IN: the inner packet (no outer header anymore).
  src=`cccc::ffff`, dst=`cccc::100`, UDP sport=9999 dport=9997.

If yellow-host00 receives the inner packet, the design is validated.
If the packet is lost anywhere along the path, the capture that's
the LAST to see it tells you which hop failed:

* Lost at host01 eth1 OUT: forwarding is the problem. Confirm
  `net.ipv6.conf.eth1.forwarding=1` (per cycle-13 chat history this
  is already 1 in the alpine image, but verify) and the FIB has
  `fc00::/32 via 2001:db8:cccc:1::1 dev eth1`. Also check
  `ip -6 neigh show dev eth1` for `2001:db8:cccc:1::1` resolved.
* Lost at leaf00 after the seventh capture (dfff arrives): the
  End.DT6 entry isn't taking effect. Verify the `ip -6 route` you
  installed in phase 3 actually has `encap seg6local action End.DT6`.
  SONiC's kernel may need an alternate syntax.
* Lost at host00 eth1 IN after decap: `cccc::100` not configured
  on host00 eth1, or kernel dropping. Should not happen if phase 2
  succeeded.

### Phase 6 — tear down

```
ssh cisco 'docker exec yellow-host00 ip -6 addr del 2001:db8:cccc::100/64 dev eth1 || true; \
  docker exec p0-leaf00 ip -6 route del fc00:0:dfff::/48 || true; \
  docker exec p0-leaf01 ip -6 route del fc00:0:dfff::/48 || true'
```

Then `SUDO= make destroy` to clean the topology.

## What to report back

Single message containing:

* Phase 1-3 results (did each setup step succeed?).
* The actual outer-DA values seen at every capture point (this is
  the design verification; getting these wrong means the µSID walk
  in the docs is wrong, not that the lab is broken).
* Whether yellow-host00 received the round-tripped probe on eth1.
* If lost: the last capture point that saw the packet, and the
  state of the relevant FIB / neighbor table / seg6local route at
  the failing hop.
* Cleanup state.

Do NOT modify any source files. Do NOT commit. This is read-only
on the source tree; the only side effects are on the lab host
(docker containers and ip route state), which the runbook itself
cleans up.

## If validation passes

The orchestrator will then create a new branch
`feature/stateless-probes` and implement the design across
`generators/fabric.py`, `srv6_mrc/topo.py`,
`srv6_mrc/mrc/transport.py`, `srv6_mrc/mrc/probe.py`,
`srv6_mrc/mrc/daemon.py`, `srv6_mrc/mrc/agent.py`,
`srv6_mrc/mrc/ev_state.py`, with `srv6_mrc/mrc/probe_clock.py`
deleted. The receiver-side `ReceiverMrcAgent` probe path is also
deleted (the bouncing host does nothing in userland). The MRC
loss-report path (separate from probe path) is unchanged.

## If validation fails

Stop. Report. The orchestrator will not pivot to a fallback design
without user input.
