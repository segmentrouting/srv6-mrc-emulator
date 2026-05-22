# spray — userspace SRv6/uSID packet spray

A small Python tool that demonstrates a very basic SRv6 packet spray model where one logical flow is split across all 4 fabric planes by varying only the **outer** SID list.

The tool has two roles, sender and receiver. Run the receiver first, then the sender in a separate terminal. Tenant (Green/Yellow) is auto-detected from the container hostname; the same flags work for both.

Green:
```
docker exec -it green-host07 spray --role recv
docker exec -it green-host00 spray --role send --dst-id 7 --rate 1000pps --duration 5s
```

Yellow (precondition: `make host-routes` — or `routes apply -f topologies/4p-4x8/routes/full-mesh.yaml` — to install the per-NIC `seg6local End.DT6` policies on yellow hosts):
```
docker exec -it yellow-host07 spray --role recv
docker exec -it yellow-host00 spray --role send --dst-id 7 --rate 1000pps --duration 5s
```

---

## What it sends

For each packet `i` the sender picks plane `P = i mod 4` and emits:

```
+--------------------------------------------------------------------+
| IPv6  src = host-underlay[P]  dst = uSID[P,S]   nh = 41            |   outer
|   +----------------------------------------------------------------+
|   | IPv6  src = inner          dst = inner-anycast   nh = 17       |   inner
|   |   +------------------------------------------------------------+
|   |   | UDP  sport = dport = 9999                                  |
|   |   |   +-----------+----------+----------+--------------+       |
|   |   |   | seq (8B)  | plane(1B)| path(1B) | pad ('X' x32)|       |
+---+---+---+-----------+----------+----------+--------------+-------+
```


Example outer destination per plane - transit spine=0 (f000), egress leaf=7 (e007), tenant green ID (d000):

| plane | egress NIC | outer dst                                |
| ----- | ---------- | ---------------------------------------- |
| 0     | eth1       | `fc00:0000:f000:e00f:d000::`             |
| 1     | eth2       | `fc00:0001:f000:e00f:d000::`             |
| 2     | eth3       | `fc00:0002:f000:e00f:d000::`             |
| 3     | eth4       | `fc00:0003:f000:e00f:d000::`             |

Each hop consumes one uSID by shifting the address left:

```
fc00:000P:f000:e007:d000::      sender emits
fc00:000P:e007:d000::            after p<P>-leaf00 (consumed f000)
fc00:000P:d000::                 after p<P>-spine00 (consumed e00f)
(plain inner)                    after p<P>-leaf07 uDT6 decap (d000 in Vrf-green)
```

For yellow the SID list is one hextet longer (the extra `e009` is the egress-leaf→host uA), and decap moves to the receiver host's kernel:

| plane | egress NIC | outer dst                                       |
| ----- | ---------- | ----------------------------------------------- |
| 0     | eth1       | `fc00:0000:f000:e007:e009:d001::`               |
| 1     | eth2       | `fc00:0001:f000:e007:e009:d001::`               |
| 2     | eth3       | `fc00:0002:f000:e007:e009:d001::`               |
| 3     | eth4       | `fc00:0003:f000:e007:e009:d001::`               |

```
fc00:000P:f000:e007:e009:d001::  sender emits
fc00:000P:e007:e009:d001::       after p<P>-leaf00 (consumed f000)
fc00:000P:e009:d001::            after p<P>-spine00 (consumed e00f)
fc00:000P:d001::                 after p<P>-leaf07 (consumed e009; egress NOT decapped)
(plain inner)                    after host kernel seg6local End.DT6 (d001) -> lo
```

The recv side sniffs **before** the host kernel decap, so it observes the `d001`-still-present frame and per-NIC counts continue to reflect the fabric path.

---

## What it counts

Receiver opens one scapy `AsyncSniffer` per NIC with BPF `ip6 proto 41 or udp port 9999`. The two clauses cover the two tenant decap models:

- **Green:** the egress leaf does `End.DT6` (`d000` in Vrf-green), so by the time the packet reaches the host NIC the outer SRv6 is already gone. The sniffer sees a plain inner IPv6/UDP frame (matched by `udp port 9999`) and reads `(seq, plane)` from the payload.
- **Yellow:** the egress leaf only consumes `e009`, leaving the final `d001` uSID on the wire. Decap happens in the receiver host's kernel `seg6local End.DT6`. The sniffer fires *before* that decap, peels one IPv6 layer to reach the inner UDP, and reads the same `(seq, plane)` payload. Sniffing pre-decap is deliberate: per-NIC counts only mean "the fabric used 4 paths" if we count at the NIC, not on `lo` after kernel decap.

After Ctrl-C, idle-timeout, or send-side `--duration` expiry, the receiver prints:

```
  received N packets
  per NIC:
    eth1: ...
    eth2: ...
    eth3: ...
    eth4: ...
  per plane (from payload):
    plane 0: ...
    plane 1: ...
    plane 2: ...
    plane 3: ...
  seq range: first..last  (E expected, missing=L)
```

A healthy lab gives:
- per-NIC counts roughly equal (≈ N/4 each),
- per-plane counts exactly equal to per-NIC counts (plane P arrives on `eth(P+1)` — anything else means a routing surprise),
- `missing = 0`.

---

## Spot-checking the wire

Run `spray` at a low rate (--dst-id 7 is leaf07) so you can read tcpdump in another terminal:

```bash
docker exec -it green-host00 spray --role send \
    --dst-id 7 --rate 5pps --duration 60s
```

## Command Arguments

```
--role send|recv              required
--dst-id N                    (send) destination host id (range depends on topology: 0..7 on 4p-4x8, 0..15 on 4p-8x16)
--rate Npps | N               (send) packets/sec, default 1000pps
--duration Ns | Nms | 0       (send) default 5s; 0 = run until ^C
--policy SPEC                 (send) plane / EV selection policy;
                              default round_robin. Built-ins:
                                  round_robin
                                  hash5tuple
                                  weighted:w0,w1,w2,w3
                                  ev_spray[:N]            (varies BOTH
                                       plane and spine per packet;
                                       N = paths-per-plane)
                                  health_aware_mrc        (MRC-aware
                                       weighted RR; reads EVStateTable)
                              See srv6_mrc/policy.py for the full list.
--idle-timeout Ns | Nms | 0   (recv) auto-exit after this much silence
                              following the first packet; default 6s,
                              0 disables (run until ^C)
--json                        emit machine-readable JSON on stdout instead
                              of the human-readable summary. Receiver
                              JSON is the per-flow record the MRC
                              orchestrator (run-scenario) consumes.
```

The receiver only arms its idle timer **after** the first packet arrives, so you can safely start `recv` before `send`. Once a burst ends and 6s pass with no new packets, recv prints its summary and exits cleanly. For "leave it running across multiple bursts" use `--idle-timeout 0`.

The sender infers its own tenant + id from the container hostname (`green-host00` → tenant=green, id=0). It will refuse to spray to itself.

### Picking a policy

- `round_robin` (default) — packet N goes to plane `N % 4`. Best for
  surfacing reorder behavior.
- `hash5tuple` — hash of `(src, dst, sport, dport, proto)` picks one
  plane per flow. With a single flow this pins all packets to one
  plane (per-plane sent counts will be unbalanced, `reord` will be 0).
- `weighted:30,30,20,20` — biased random; sum need not be 100.
- `ev_spray[:N]` — rotates BOTH plane and spine per packet, so a
  single flow walks `4 × N` distinct `(plane, path)` EVs. `N` defaults
  to `NUM_SPINES` (4 on the default 4p-4x8, 8 on 4p-8x16); set lower
  to constrain the spine
  subset per (src, dst) pair.
- `health_aware_mrc` — same per-packet rotation as `ev_spray`, but
  weights are driven by the EV state machine. Reads `EVStateTable`
  and drops broken `(plane, path)` EVs to weight 0 while survivors
  absorb the slack. Requires the scenario YAML to declare an
  `mrc:` block (even an empty one) so the orchestrator pushes
  config to the senders. See `design-mrc.md` for the full table.



