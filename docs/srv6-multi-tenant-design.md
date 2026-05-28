# SRv6 uSID Multi-Tenancy and Security for AI Factory Network Fabrics

**Bruce McDougall | April 2026**

*CONFIDENTIAL*

---

**Scope of this document**

This document covers SRv6 uSID multi-tenancy encapsulation design options and their implications for enforcing security and trust boundary, including a two-dimensional ACL enforcement model that addresses both endpoint isolation and uA SID path enforcement.

---

## 1. Multi-Tenant Design Options

Multi-tenancy in the SRv6 AI factory fabric is achieved by encoding a Tenant-ID (uDT function SID) in the uSID carrier and enforcing VRF-based or port-based segmentation at the encapsulation and decapsulation points. This paper explores three design options, which differ in where SRv6 encapsulation and decapsulation are performed — at the leaf switch, at the GPU NIC, or in a hybrid split. This choice drives the trust model, leaf state requirements, and NIC capabilities needed.

*Figure 1. Three options for point of SRv6 multi-tenant encap/decap design \[diagram\]*

BGP VPNs (L3VPN/EVPN over SRv6) are not used. An SDN-programmed static forwarding model is used throughout; since the key value of SRv6 in this design is controller-driven uA path steering, BGP VPN signaling adds complexity without meaningful benefit.

Two additional design requirements have to do with scale: the design should support current 512×100G radix switches and future 1024×100G or 2048×100G generations, and it should support very large-scale AI factories (131k GPU) with the potential to link multiple AI factories via regional or wide area scale-across networks.

The high radix requirement implies using a large Local-ID Block (LIB), so a non-default GIB/LIB allocation of the 16-bit uSID space (65,536 total values) might look something like:

| Category | Range | Quantity | Purpose |
|---|---|---|---|
| End of Carrier | 0x0000 | 1 | Marks end of active uSID list, not available for allocation |
| GIB: Reserved WAN/Scale-Across | 0x0001–0x0FFF | 4,095 | Locators reserved for WAN/inter-factory scale-across nodes |
| GIB: 4-Cluster Fabric Locators | 0x1000–0x5FFF | 20,480 | Unique Locators per node in a three-tier fabric spanning 4 clusters, each with 4 spine-leaf planes |
| GIB: Reserved (future) | 0x6000–0xCFFF | 28,672 | Reserved for additional clusters, new fabric tiers, or WAN scale-across growth |
| LIB: Dynamic Functions | N/A | 0 | No dynamic (BGP/IGP) LIB allocation: all functions are explicit |
| LIB: Explicit Tenant-ID (uDT) | 0xD000–0xEFFF | 8,192 | Tenant VRF identifiers for uDT decapsulation lookups |
| LIB: Explicit uA Forwarding | 0xF000–0xFFFF | 4,096 | uA adjacency SIDs for fabric steering — covers 1,024 and 2,048-port next-gen switches |
| **Total** | | **65,535** | |

> **Note:** This allocation table illustrates how the 16-bit space might be carved to support multi-tenancy spanning a multi-site deployment of hyperscale AI factories. An alternate entirely valid design might allocate more space to GIB WAN and less space to tenant-IDs, etc.

Traffic engineering or traffic steering capability in any of the three multi-tenant models will depend on the number of fabric tiers and how explicit the path selection or path pinning needs to be. Explicit path pinning requires constructing the uSID carrier with uA SIDs to traverse the fabric. Loose path pinning might use node locator uN SIDs to load balance traffic when leaf and spine have multiple interconnect links, or even anycast uN SIDs for load balancing traffic across a slice of the fabric.

### Example uSID Carrier Construction Scenarios

#### Multi-Tenant Two-Tier Fabric with Explicit Path (uA) TE

| Bits 0–31 | Bits 32–47 | Bits 48–63 | Bits 64–79 | Bits 80–127 |
|---|---|---|---|---|
| FC00:0 (uSID Block) | F000 (Leaf→Spine uA) | F0A7 (Spine→Leaf uA) | E001 (uDT Tenant-ID) | Free |

#### Multi-Tenant Two-Tier Fabric with Loose Path (uN) TE

| Bits 0–31 | Bits 32–47 | Bits 48–63 | Bits 64–79 | Bits 80–127 |
|---|---|---|---|---|
| FC00:0 (uSID Block) | 1000 (Leaf→Spine uN) | 2001 (Spine→Leaf uN) | E001 (uDT Tenant-ID) | Free |

#### Multi-Tenant Three-Tier Fabric with Explicit Path (uA) TE

| Bits 0–31 | Bits 32–47 | Bits 48–63 | Bits 64–79 | Bits 80–95 | Bits 96–111 | Bits 112–127 |
|---|---|---|---|---|---|---|
| FC00:0 (uSID Block) | F000 (Leaf→Spine uA) | F100 (Spine→Super-Spine uA) | F200 (Super-Spine→Spine uA) | F0A7 (Spine→Leaf uA) | E001 (uDT Tenant-ID) | Free |

---

### 1.1 Multi-Tenant Option 1 — Network-Based SRv6

> **Model:** Ingress leaf encapsulates; egress leaf decapsulates and performs VRF lookup. GPU NIC sees only standard IPv6.

The ingress leaf assigns each GPU-facing interface to a tenant VRF and applies VRF static routes carrying the SRv6 encapsulation instruction. The full uSID carrier — including steering uSIDs and Tenant-ID uDT function — is applied at the leaf before the packet enters the fabric core. The uSID carrier can include the destination leaf Locator, though it is not required as the egress leaf should support a simple uSID-Block + uDT entry. Inclusion of the Locator can improve the security/trust-boundary solution covered in Section 2.

Example (SONiC FRR config) - ingress leaf VRF static route with SRv6 uSID encapsulation for explicit path steering over a two-tier fabric:

```
vrf Vrf-blue

  ipv6 route 2001:db8:aaaa:1::/64 Ethernet0 segments \
    fc00:0:f000:f0A7:e000:: encap-behavior H_Encaps_Red nexthop-vrf default

exit-vrf
```

| Route Element | Description |
|---|---|
| `2001:db8:aaaa:1::/64` | Remote host/GPU destination address or subnet |
| `Ethernet0` | Explicit egress interface for the static route |
| `segments fc00:0:f000:f0A7:e000::` | uSID carrier with two explicit uA SIDs and egress leaf uDT SID: **f000** = ingress leaf uA to spine; **f0A7** = spine uA to egress leaf; **e000** = uDT tenant-ID |
| `encap-behavior H_Encaps_Red` | SRv6 uSID encapsulation, no SRH needed |
| `nexthop-vrf default` | Encapsulating outer IPv6 destination address is known in the default routing table (no need to redistribute uSID block into VRF) |

Ingress leaf's static uA SID out Ethernet0 to spine:

```
segment-routing
  srv6
    static-sids
      sid fc00:0:f000::/48 locator MAIN behavior uA \
        interface Ethernet0 nexthop 2001:db8:1:1::1
```

On the egress side, the leaf resolves the incoming outer destination to its own locator and/or local uDT function, decapsulates, and routes the inner packet via the tenant VRF to the destination NIC port.

Example egress leaf static tenant-ID uDT SID:

```
segment-routing
  srv6
    static-sids
      sid fc00:0:e000::/48 locator MAIN behavior uDT6 vrf Vrf-blue
```

> **Note:** The uDT SID could include the leaf node's locator ID, but it is not required. If the locator ID were included the static SID entry would look something like:
>
> ```
> static-sids
>   sid fc00:0:2001:e000::/64 locator MAIN behavior uDT6 vrf Vrf-blue
> ```

The Network-Based SRv6 model does not require any SRv6 capability on the GPU NIC. The primary cost is per-tenant state: VRFs and static routes with encapsulation on every ingress leaf, and VRF + uDT state on egress leaves — scaling with tenant count × leaf count.

#### uSID Carrier — Two-Tier Fabric with TE

| Bits 0–31 | Bits 32–47 | Bits 48–63 | Bits 64–79 | Bits 80–127 |
|---|---|---|---|---|
| FC00:0 (uSID Block) | F000 (Leaf→Spine uA) | F0A7 (Spine→Leaf uA) | E001 (Leaf uDT Tenant-ID) | Free |

---

### 1.2 Multi-Tenant Option 2 — Host-Based SRv6

> **Model:** Source NIC encapsulates; destination NIC decapsulates. Egress leaf uses a uA SID to steer to the correct NIC port — no host locator required.

In the Host-Based SRv6 model the GPU NIC applies the full uSID carrier including steering uSIDs (leaf-to-spine, spine-to-leaf), a leaf-to-NIC uA SID, followed by the uDT Tenant-ID function. Leaf and spine switches perform only standard uSID Shift-and-Forward — no encapsulation or decapsulation needed at the leaf nodes. When the uSID carrier reaches the egress leaf, the leaf-to-NIC uA SID instructs it to forward out the specific port connected to the destination NIC — the uA identifies both the leaf and the egress port, eliminating any need for a dedicated host or chassis locator. The destination NIC decapsulates and resolves the uDT/Tenant-ID function via its local SID table. The leaf carries no per-tenant VRF state — only a uA SID per NIC-facing port.

Example Linux host iproute2 entry with SRv6 uSID encapsulation:

```
ip -6 route add 2001:db8:cccc:1::/64 \
  encap seg6 mode encap.red \
  segs fc00:0:f002:f001:f006:e002:: dev eth1
```

The Host-Based SRv6 model expands the diameter of the SRv6 domain, adding a third steering uA SID to the uSID carrier.

#### uSID Carrier — Two-Tier Fabric with TE

| Bits 0–31 | Bits 32–47 | Bits 48–63 | **Bits 64–79** | Bits 80–95 |
|---|---|---|---|---|
| FC00:0 (uSID Block) | F002 (Leaf→Spine uA) | F001 (Spine→Leaf uA) | **F006 (Leaf→NIC uA)** | E002 (uDT Tenant-ID) |

Example Linux host iproute2 SRv6 uDT tenant-ID "localsid" entry — instructs the host to decapsulate and forward to local GPU in table 2:

```
ip -6 route add fc00:0:e002::/48 dev eth1 \
  encap seg6local action End.DT6 table 2
```

---

### 1.3 Option 3 — Hybrid

> **Model:** Source NIC encapsulates; egress leaf decapsulates and performs VRF lookup. Ingress leaf carries no per-tenant state.

The transmitting NIC produces the same uSID carrier as Option 1's ingress leaf would. The ingress leaf performs pure destination-based forwarding / shift-and-forward with no VRF lookup or encapsulation step, significantly reducing its TCAM requirements. The egress leaf resolves the locator (if present), identifies the uDT function, decapsulates, and delivers via VRF — identical to Option 1. This option eliminates ingress leaf VRF + static route state while retaining hardware-accelerated decapsulation at the egress leaf and limits the NIC SRv6 requirement to encapsulation only.

#### uSID Carrier — identical to Option 1

| Bits 0–31 | Bits 32–47 | Bits 48–63 | Bits 64–79 | Bits 80–95 |
|---|---|---|---|---|
| FC00:0 (uSID Block) | F000 (Leaf→Spine uA) | F0A7 (Spine→Leaf uA) | E001 (Leaf uDT Tenant-ID) | Free |

---

### 1.4 Option Comparison

| Aspect | Option 1 Network-Based SRv6 | Option 2 Host-Based SRv6 | Option 3 Hybrid |
|---|---|---|---|
| Encapsulation point | Ingress leaf | Source NIC | Source NIC |
| Decapsulation point | Egress leaf | Destination NIC | Egress leaf |
| NIC SRv6 requirement | None | Encap + Decap | Encap only |
| Ingress leaf state | Per-tenant VRF + routes | None | None |
| Egress leaf state | uDT per tenant | uA per NIC port | uDT per tenant |
| Trust boundary | Strongest | Ingress enforcement required | Ingress enforcement required |
| 2-tier fabric uSID slots | 3 (4 with Locator) | 4 | 3 (4 with Locator) |
| 3-tier fabric uSID slots | 5 (6 with Locator) | 6 | 5 (6 with Locator) |

---

## 2. Security, Trust Boundary, and Enforcement

Security in a multi-tenant SRv6 AI factory revolves around a single central question: who controls the SRv6 encapsulation operation, and what happens if that control is compromised or misused? Whoever controls the uSID carrier controls the forwarding path, the tenant VRF lookup at the egress, and ultimately which GPU NIC receives the decapsulated inner packet.

### 2.1 Trust Boundary Models

#### Infrastructure-Controlled Encapsulation

When the infrastructure operator programs the SRv6 encapsulation via the ingress leaf switch, a vNIC abstraction, or a DPU/SmartNIC whose SRv6 stack is operator-managed, the outer SRv6 header is entirely under operator control. The tenant controls only the inner IPv6 source/destination addresses and application payload. A tenant workload cannot manipulate the uSID carrier, steer traffic to another tenant's GPU NIC, or inject arbitrary uA SIDs to hijack another tenant's bandwidth allocation.

#### Tenant-Controlled Encapsulation

- **Single-tenant dedicated cluster:** the tenant may be granted full encapsulation control without cross-tenant risk. Source address validation and uSID block boundary enforcement are recommended as defense-in-depth.

- **Multi-tenant shared cluster:** ingress enforcement is required. A tenant with NIC programming access could inject uA SIDs targeting another tenant's spine allocation, construct locators pointing to another tenant's NICs, or craft uDT Function values matching another tenant's VRF or tenant-ID. Even with ingress enforcement, a determined actor who defeats the ingress ACL may consume another tenant's bandwidth — this residual risk requires monitoring and anomaly detection.

---

### 2.2 Ingress and Egress Enforcement

#### Ingress Leaf Enforcement

- **Source address validation:** the outer IPv6 source address must match the expected NIC address for the ingress port — prevents address spoofing.

- **uSID block validation:** the outer destination must begin with the cluster's assigned uSID block prefix — packets with other blocks are dropped.

- **Tenant-to-port binding:** in Option 1, the GPU-facing interface is assigned to a VRF, inherently limiting reachable destinations. In Host-Based designs, an explicit ACL binding the source NIC's prefix to the tenant's allowed destinations provides equivalent enforcement.

#### Egress Leaf Enforcement

An ACL at the egress leaf's NIC-facing port permits inbound traffic only from the expected tenant's uSID Locator range or Tenant-ID function value. The egress ACL is the last line of defense — present regardless of encapsulation option or trust model — and provides defense-in-depth against controller misconfiguration or fabric forwarding anomalies.

---

### 2.3 IPv6 ACL Bitmask Matching — Tenant Isolation

The host-encapsulated SRv6 uSID carrier could be one of hundreds or thousands of combinations of uA SIDs and uDT egress values. An exact-match ACL implementation would require one entry per (Tenant-ID × uA-SID combination) — O(Tenants × Paths) — which is operationally unfeasible and leads to TCAM exhaustion.

The solution is IPv6 ACL entries with arbitrary bitmask matching. For tenant isolation, the relevant bit structure is:

| Bits | Contents | ACL Treatment |
|---|---|---|
| 0–31 | uSID Block (fixed per cluster) | Can match prefix-style |
| 32–63 | Steering uSIDs in a two-tier fabric (uA values — variable per flow) | Must be ignored — unknown at ACL programming time |
| 64–127 | Leaf-to-NIC uA + Tenant-ID, or Locator + Tenant-ID | Must match exactly to enforce tenant isolation |

Matching exactly on bits 64–127 while ignoring bits 0–63 is a contiguous suffix match — a single TCAM entry per tenant:

```
Value:  0000:0000:0000:0000:xxxx:xxxx:xxxx:xxxx
Mask:   0000:0000:0000:0000:FFFF:FFFF:FFFF:FFFF
```

Tenant endpoint isolation then scales as **O(Tenants)** — one entry per tenant — regardless of how many spine nodes or steering uSID combinations exist.

> **Note:** When the Locator is present in bits 64–79, the bitmask ACL must cover bits 64–127 (Locator + Tenant-ID). When the Locator is omitted, only bits 64–79 (the Tenant-ID directly) need to be matched — a simpler and more portable ACL entry.

---

### 2.4 uA SID Path Enforcement

The egress bitmask ACL in Section 2.3 enforces tenant isolation — it prevents a tenant from delivering traffic to another tenant's GPU NIC. However, it explicitly wildcards bits 32–63, which carry the steering uA SIDs. This means it does not prevent a tenant NIC from constructing a uSID carrier that uses spine nodes assigned to a different tenant's fabric slice, provided the packet still terminates at the tenant's own endpoint. The two threats are orthogonal and require separate enforcement:

| Threat | Egress bitmask ACL (suffix, bits 64–127) | Ingress uA-range ACL (mid-address, bits 32–63) |
|---|---|---|
| Deliver traffic to another tenant's GPU NIC (Tenant-ID spoofing) | ✓ Blocked | ✗ Not applicable |
| Use spine nodes from another tenant's allocated slice (uA injection) | ✗ Not blocked | ✓ Blocked |
| Inject wrong uDT function value | ✓ Blocked | ✓ Blocked |

To block uA SID path hijacking, an ingress ACL on the leaf must constrain the uA SID values a tenant NIC is permitted to place in the steering slots (bits 32–63). This requires the same hardware bitmask capability as Section 2.3, applied to a mid-address window rather than a suffix. Two operational models are available:

#### Hard Pinning — Dedicated Spine Slices per Tenant

Each tenant is allocated a non-overlapping set of spine nodes for their exclusive use, and the ingress leaf ACL enforces that their NICs may only place uA SID values within their allocated range in the steering slots.

Example: Tenant-A is allocated spine nodes 0–63, corresponding to uA SID range 0xF000–0xF03F. The combined ingress ACL entry constraining both uA steering slots to Tenant-A's range:

```
Value:  FC00:0000:F000:F000:0000:0000:0000:0000
Mask:   FFFFFFFF:FFC0:FFC0:0000:0000:0000:0000
```

This matches any packet from a Tenant-A NIC port whose uA slot 1 (bits 32–47) and uA slot 2 (bits 48–63) both fall within 0xF000–0xF03F, and wildcards the endpoint fields entirely. Combined with the egress suffix ACL, this provides full two-dimensional enforcement: correct tenant endpoint AND correct spine slice, each in a single TCAM entry.

Hard pinning requires that tenant uA allocations be power-of-two aligned and sized — the same constraint as CIDR prefix planning. An allocation of 64 spine nodes (a power of two) is covered by one TCAM entry with 6 wildcard bits in each uA slot. An allocation of 96 nodes requires two entries. The SDN controller must manage tenant uA allocations with this constraint in mind.

> **Hard Pinning:** Provides the strongest isolation guarantee — tenants are physically prevented by hardware ACL from using each other's spine capacity. Enables hard bandwidth guarantees and deterministic QoS per tenant. Requires power-of-two-aligned uA SID allocations and hardware bitmask ACL support on bits 32–63 at the ingress leaf. Best suited to multi-tenant shared clusters where SLA enforcement is required.

#### Loose Pinning — ECN-Driven NIC Re-encapsulation

In the loose-pinning model, the ingress leaf does not enforce which uA SIDs a tenant NIC may use. Tenants are assigned a default uA SID set by the SDN controller and expected to use it, but there is no hardware barrier preventing a NIC from placing out-of-range uA values in the carrier.

When two tenants' flows converge on the same spine node — whether by misconfiguration, NIC software error, or deliberate injection — the spine node experiences elevated queue depth and marks packets with ECN (Explicit Congestion Notification). The receiving NIC detects the ECN marks and signals the sending NIC, which in a correctly implemented SRv6 NIC stack requests a new path assignment from the SDN controller. The controller reprograms the NIC's uA SID set to route around the congested node.

This is an eventually-consistent model: congestion must occur and be detected before correction happens. In the window between collision onset and NIC re-encapsulation, both tenants experience degraded bandwidth and increased latency. For tightly-coupled All-Reduce collectives, even a brief spine collision can stall a training job.

> **Loose Pinning:** Lower operational overhead — no ingress uA-range ACLs required, no power-of-two alignment constraint on tenant allocations. Relies on ECN feedback and NIC cooperation to self-correct collisions. Does not provide hard bandwidth guarantees and provides no protection against a tenant NIC that ignores ECN or deliberately injects out-of-range uA SIDs. Best suited to trusted-tenant environments or single-tenant clusters where isolation is not a hard requirement.

#### Comparison

| Property | Hard Pinning | Loose Pinning |
|---|---|---|
| Spine slice enforcement | Hardware ACL — deterministic | ECN feedback — eventual |
| Bandwidth guarantee | Yes — hard isolation per tenant | No — statistical, collision-dependent |
| Protection against bad actors | Yes — ACL prevents injection | No — relies on NIC cooperation |
| Allocation constraint | Power-of-two-aligned uA SID ranges | None — any uA range |
| Hardware requirement | Bitmask ACL on bits 32–63 at ingress leaf | Standard ACL only |
| Operational complexity | Higher — per-tenant ACL programming | Lower — controller assignment only |
| Best fit | Multi-tenant shared clusters, SLA required | Trusted tenants, single-tenant clusters |

---

### 2.5 Current Platform Status: SONiC PR #4404

A pull request submitted to the SONiC sonic-swss repository in late March 2026 directly addresses the hardware bitmask ACL capability required by both the egress endpoint ACL (Section 2.3) and the hard-pinning ingress uA-range ACL (Section 2.4):

| Item | Detail |
|---|---|
| PR | sonic-net/sonic-swss#4404 |
| URL | https://github.com/sonic-net/sonic-swss/pull/4404 |
| Author | Cisco engineer (ashu@cisco.com) |
| Submitted | Late March 2026 |
| Status | Open — awaiting code owner review and merge approval as of April 2026 |
| Title | \[acl\] Support arbitrary IP masks and L3V6 IN\_PORTS/OUT\_PORTS |

The PR adds `SRC_IPV6_MASK`, `DST_IPV6_MASK`, `SRC_IP_MASK`, and `DST_IP_MASK` fields to the SONiC ACL framework, enabling arbitrary bitmask matching on IPv6 and IPv4 address fields via the SAI API. This single capability underpins both enforcement dimensions: the suffix match on bits 64–127 for endpoint isolation, and the mid-address window match on bits 32–63 for hard-pinned spine slice enforcement.
