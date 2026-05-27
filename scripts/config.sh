#!/bin/bash
# Push config_db.json and frr.conf to SONiC nodes for an SRv6 fabric.
#
# Usage:
#   scripts/config.sh [gen|all|leaf|spine|verify|<node_name>]
#
# Environment:
#   TOPO_DIR   path to the topology directory holding topology.clab.yaml +
#              config/ (default: topologies/4p-8x16)
#
# Generate configs first via: scripts/config.sh gen
# (which calls generators/fabric.py with the matching topo.yaml).
#
# Container names are usually clab-<topology>-<node> (see name: in
# topology.clab.yaml) or the short node name when your Containerlab build
# uses short names.
#
# `all` and `leaf` automatically run the post-push verifier, which checks
# that every leaf has the expected number of seg6local entries programmed
# into the kernel FIB (the expected count is derived per node from its
# generated frr.conf -- topology- and tenant-agnostic). Any mismatched
# leaves get their frr.conf re-pushed up to VERIFY_RETRIES times; this
# papers over a startup race in FRR staticd that drops SIDs silently when
# vtysh accepts config before zebra has the locator route installed.
# `verify` runs only the check (useful for ad-hoc diagnosis).

set +e

SCOPE="${1:-all}"
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TOPO_DIR="${TOPO_DIR:-$REPO_ROOT/topologies/4p-4x8}"
CONFIGS_DIR="$TOPO_DIR/config"
TOPO_YAML="$TOPO_DIR/topo.yaml"

TOPOLOGY_NAME="$(grep -m1 '^name:' "$TOPO_DIR/topology.clab.yaml" 2>/dev/null | awk '{print $2}' | tr -d '\r')"
TOPOLOGY_NAME="${TOPOLOGY_NAME:-sonic-docker-4p-4x8}"

# Read NUM_PLANES/SPINES/LEAVES from topo.yaml. Lightweight grep avoids
# a python dep on the lab host for just three integers.
NUM_PLANES="$(grep -m1 '^planes:' "$TOPO_YAML" 2>/dev/null | awk '{print $2}')"
NUM_SPINES="$(grep -m1 '^spines_per_plane:' "$TOPO_YAML" 2>/dev/null | awk '{print $2}')"
NUM_LEAVES="$(grep -m1 '^leaves_per_plane:' "$TOPO_YAML" 2>/dev/null | awk '{print $2}')"
NUM_PLANES="${NUM_PLANES:-4}"
NUM_SPINES="${NUM_SPINES:-4}"
NUM_LEAVES="${NUM_LEAVES:-8}"

LEAF_NODES=""
for p in $(seq 0 $((NUM_PLANES - 1))); do
    for i in $(seq 0 $((NUM_LEAVES - 1))); do
        LEAF_NODES+="p${p}-leaf$(printf '%02d' "$i") "
    done
done
LEAF_NODES="${LEAF_NODES%% }"

SPINE_NODES=""
for p in $(seq 0 $((NUM_PLANES - 1))); do
    for i in $(seq 0 $((NUM_SPINES - 1))); do
        SPINE_NODES+="p${p}-spine$(printf '%02d' "$i") "
    done
done
SPINE_NODES="${SPINE_NODES%% }"

ALL_NODES="$LEAF_NODES $SPINE_NODES"

# This lab has no BGP, but stock SONiC may still ship a default BGP instance.
# Strip any common defaults so vtysh -f loads the file cleanly.
BGP_ASNS_TO_CLEAR="65000 65001 65100"

resolve_container() {
    local NODE_ID="$1"
    local LONG_NAME="clab-${TOPOLOGY_NAME}-${NODE_ID}"

    if docker inspect "$NODE_ID" &>/dev/null; then
        echo "$NODE_ID"
        return 0
    fi
    if docker inspect "$LONG_NAME" &>/dev/null; then
        echo "$LONG_NAME"
        return 0
    fi
    echo ""
    return 1
}

# List the VRF names the given frr.conf declares (i.e. `vrf <name>` blocks).
# Used to gate `vtysh -f frr.conf` on the matching Linux VRF netdevs being
# present in the container — see wait_for_vrfs().
vrfs_in_frr_conf() {
    local FRR="$1"
    [ -f "$FRR" ] || return 0
    # `vrf vrfdefault` is the FRR-side wrapper for the default VRF and does
    # not need a Linux netdev called "vrfdefault" (deploy_node creates it
    # explicitly anyway). Skip it so we don't wait for something we already
    # made.
    awk '/^vrf [^ ]+/ && $2 != "vrfdefault" { print $2 }' "$FRR"
}

# Wait for the named VRFs to appear in the container's netdev table. This
# closes the race where SONiC's vrfmgrd hasn't finished creating Vrf-green
# from config_db.json before `vtysh -f frr.conf` runs, which causes
# staticd to silently drop any `sid ... vrf Vrf-green` lines (the SID is
# parsed but the VRF lookup fails, and FRR doesn't retry on the next
# config reload because the config text is unchanged).
#
# Bounded: VRF_WAIT_TRIES * 1s ceiling. Returns 0 if all VRFs are present,
# 1 if we timed out (caller proceeds anyway — the verifier will catch any
# missing SIDs and we'd rather attempt the push than block forever).
VRF_WAIT_TRIES="${VRF_WAIT_TRIES:-30}"
wait_for_vrfs() {
    local CONTAINER="$1"
    local FRR="$2"
    local wanted
    wanted="$(vrfs_in_frr_conf "$FRR")"
    [ -z "$wanted" ] && return 0

    local i missing v
    for i in $(seq 1 "$VRF_WAIT_TRIES"); do
        missing=""
        for v in $wanted; do
            if ! docker exec "$CONTAINER" ip link show "$v" type vrf &>/dev/null; then
                missing+="$v "
            fi
        done
        if [ -z "$missing" ]; then
            return 0
        fi
        sleep 1
    done
    echo "    WARN: VRFs still missing after ${VRF_WAIT_TRIES}s: $missing" >&2
    return 1
}

deploy_node() {
    local NODE_ID="$1"
    local CONTAINER
    CONTAINER="$(resolve_container "$NODE_ID")"

    echo "  Deploying $NODE_ID -> ${CONTAINER:-<not found>}"

    if [ -z "$CONTAINER" ]; then
        echo "    SKIP: no docker container for $NODE_ID (tried $NODE_ID, clab-${TOPOLOGY_NAME}-${NODE_ID})"
        return 1
    fi

    # Create Loopback0 if missing
    docker exec "$CONTAINER" bash -c "ip link show Loopback0 &>/dev/null || { ip link add Loopback0 type dummy && ip link set Loopback0 up; }" 2>/dev/null || true

    # Copy config_db.json
    if [ -f "$CONFIGS_DIR/$NODE_ID/config_db.json" ]; then
        docker cp "$CONFIGS_DIR/$NODE_ID/config_db.json" "$CONTAINER:/etc/sonic/config_db.json"
        echo "    config_db.json copied"
    else
        echo "    WARN: no config_db.json found for $NODE_ID"
    fi

    # Reload SONiC config
    docker exec "$CONTAINER" bash -c "sonic-cfggen -j /etc/sonic/config_db.json --write-to-db" 2>/dev/null || true
    docker exec "$CONTAINER" bash -c "supervisorctl restart all" 2>/dev/null || true
    echo "    config reloaded"

    # Setup VRF default and sysctl
    docker exec "$CONTAINER" ip link add vrfdefault type vrf table main 2>/dev/null || true
    docker exec "$CONTAINER" ip link set vrfdefault up 2>/dev/null || true
    docker exec "$CONTAINER" sysctl -w net.vrf.strict_mode=1 2>/dev/null || true
    docker exec "$CONTAINER" sysctl -w net.ipv4.conf.vrfdefault.rp_filter=0 2>/dev/null || true
    docker exec "$CONTAINER" ip link add sr0 type dummy 2>/dev/null || true
    docker exec "$CONTAINER" ip link set sr0 up 2>/dev/null || true
    docker exec "$CONTAINER" sysctl -w net.ipv6.conf.all.forwarding=1 2>/dev/null || true
    echo "    vrfdefault, sr0, and sysctl configured"

    # Enable ports (admin up) - SONiC default may have them down
    docker exec "$CONTAINER" bash -c 'for port in $(sonic-cfggen -d --var-json PORT | python3 -c "import sys,json; print(\" \".join(json.load(sys.stdin).keys()))"); do config interface startup $port 2>/dev/null; done' 2>/dev/null || true

    # Wait for FRR to be ready
    for i in $(seq 1 30); do
        if docker exec "$CONTAINER" vtysh -c "show version" &>/dev/null; then
            break
        fi
        sleep 2
    done

    # Copy and load FRR config
    if [ -f "$CONFIGS_DIR/$NODE_ID/frr.conf" ]; then
        local FRR_DIR=""
        if docker exec "$CONTAINER" test -d /etc/sonic/frr 2>/dev/null; then
            FRR_DIR="/etc/sonic/frr"
        elif docker exec "$CONTAINER" test -d /etc/frr 2>/dev/null; then
            FRR_DIR="/etc/frr"
        else
            FRR_DIR="/etc/sonic/frr"
            docker exec "$CONTAINER" mkdir -p "$FRR_DIR" 2>/dev/null || true
        fi

        docker cp "$CONFIGS_DIR/$NODE_ID/frr.conf" "$CONTAINER:$FRR_DIR/frr.conf"
        echo "    frr.conf copied to $FRR_DIR/frr.conf"

        docker exec "$CONTAINER" supervisorctl stop bgpd zebra staticd 2>/dev/null || true
        sleep 2
        docker exec "$CONTAINER" supervisorctl start bgpd zebra staticd 2>/dev/null || true
        sleep 3
        # Strip any default BGP instance(s) so vtysh -f applies cleanly.
        for asn in $BGP_ASNS_TO_CLEAR; do
            docker exec "$CONTAINER" vtysh -c "configure terminal" -c "no router bgp $asn" -c "exit" 2>/dev/null || true
        done
        # Gate vtysh -f on Linux VRFs being present (closes vrfmgrd race).
        wait_for_vrfs "$CONTAINER" "$CONFIGS_DIR/$NODE_ID/frr.conf" || true
        docker exec "$CONTAINER" vtysh -f "$FRR_DIR/frr.conf" 2>/dev/null || true
        echo "    frr.conf loaded"
    else
        echo "    WARN: no frr.conf found for $NODE_ID"
    fi

    echo "    OK: $NODE_ID deployed"
}

deploy_group() {
    local NODES="$1"
    local GROUP_NAME="$2"
    echo "=== Deploying $GROUP_NAME (parallel) ==="
    for node in $NODES; do
        deploy_node "$node" &
    done
    wait
    echo "=== $GROUP_NAME done ==="
    echo ""
}

# How many SIDs should this node have programmed into the kernel FIB?
# Source of truth is the generated frr.conf, which embeds one `sid ...`
# line per static-sid under `segment-routing / srv6 / static-sids`. This
# makes the verifier topology- and tenant-agnostic: green-only fabrics,
# yellow-only fabrics, mixed fabrics, and different plane/leaf counts
# all derive their expected SID count from the same generator output
# they were configured from.
expected_sids() {
    local NODE_ID="$1"
    local FRR="$CONFIGS_DIR/$NODE_ID/frr.conf"
    [ -f "$FRR" ] || { echo 0; return; }
    grep -cE '^[[:space:]]+sid[[:space:]]' "$FRR" 2>/dev/null || echo 0
}

# Count seg6local entries actually programmed in the kernel for a node.
# Returns 0 if the container isn't reachable (treated as "not yet ready",
# which the retry loop handles correctly).
actual_sids() {
    local CONTAINER="$1"
    docker exec "$CONTAINER" ip -6 route show table all 2>/dev/null \
        | grep -cE 'seg6local|End\.' \
        || echo 0
}

# Re-push just the frr.conf for a single node. Used by the verifier when
# the initial deploy_node race caused staticd to drop SIDs. Cheaper than
# a full deploy_node redo because config_db / ports / VRFs / sr0 are
# already in place from the first pass.
repush_frr() {
    local NODE_ID="$1"
    local CONTAINER
    CONTAINER="$(resolve_container "$NODE_ID")"
    [ -z "$CONTAINER" ] && return 1

    local FRR_DIR=""
    if docker exec "$CONTAINER" test -d /etc/sonic/frr 2>/dev/null; then
        FRR_DIR="/etc/sonic/frr"
    elif docker exec "$CONTAINER" test -d /etc/frr 2>/dev/null; then
        FRR_DIR="/etc/frr"
    else
        FRR_DIR="/etc/sonic/frr"
        docker exec "$CONTAINER" mkdir -p "$FRR_DIR" 2>/dev/null || true
    fi

    if [ ! -f "$CONFIGS_DIR/$NODE_ID/frr.conf" ]; then
        return 1
    fi

    docker cp "$CONFIGS_DIR/$NODE_ID/frr.conf" "$CONTAINER:$FRR_DIR/frr.conf"
    docker exec "$CONTAINER" supervisorctl stop bgpd zebra staticd 2>/dev/null || true
    sleep 2
    docker exec "$CONTAINER" supervisorctl start bgpd zebra staticd 2>/dev/null || true
    sleep 3
    for asn in $BGP_ASNS_TO_CLEAR; do
        docker exec "$CONTAINER" vtysh -c "configure terminal" -c "no router bgp $asn" -c "exit" 2>/dev/null || true
    done
    # Same VRF-presence gate as deploy_node — this is the whole point of
    # the repush, since the first pass likely raced vrfmgrd.
    wait_for_vrfs "$CONTAINER" "$CONFIGS_DIR/$NODE_ID/frr.conf" || true
    docker exec "$CONTAINER" vtysh -f "$FRR_DIR/frr.conf" 2>/dev/null || true
}

# Verify every node in NODES has its expected SID count programmed. For
# any mismatch, repush its frr.conf and recheck, up to VERIFY_RETRIES
# attempts. Reports pass/fail summary at the end and returns nonzero if
# any node is still wrong after retries (caller can decide).
VERIFY_RETRIES="${VERIFY_RETRIES:-3}"
verify_and_repair() {
    local NODES="$1"
    local GROUP_NAME="$2"
    echo "=== Verifying $GROUP_NAME (expected SIDs from generated frr.conf) ==="

    local attempt
    for attempt in $(seq 1 "$VERIFY_RETRIES"); do
        local bad=""
        for node in $NODES; do
            local CONTAINER
            CONTAINER="$(resolve_container "$node")"
            if [ -z "$CONTAINER" ]; then
                # Container missing -- not our problem to fix here, but
                # report it so the operator can see.
                bad+="$node(no-container) "
                continue
            fi
            local want have
            want="$(expected_sids "$node")"
            have="$(actual_sids "$CONTAINER")"
            if [ "$have" != "$want" ]; then
                bad+="$node($have/$want) "
            fi
        done

        if [ -z "$bad" ]; then
            echo "  attempt $attempt: all $GROUP_NAME OK"
            echo "=== $GROUP_NAME verified ==="
            echo ""
            return 0
        fi

        echo "  attempt $attempt: re-pushing -> $bad"
        for entry in $bad; do
            # entry looks like "p3-leaf12(0/11)" -- strip the suffix
            local node="${entry%%(*}"
            repush_frr "$node" &
        done
        wait
    done

    echo "  FAIL: nodes still mismatched after $VERIFY_RETRIES attempts: $bad"
    echo "=== $GROUP_NAME verification incomplete ==="
    echo ""
    return 1
}

VERIFY_RC=0
case "$SCOPE" in
    gen)
        exec python3 "$REPO_ROOT/generators/fabric.py" --topo "$TOPO_YAML"
        ;;
    all)
        deploy_group "$LEAF_NODES" "leaf tier"
        deploy_group "$SPINE_NODES" "spine tier"
        verify_and_repair "$LEAF_NODES" "leaf tier"
        VERIFY_RC=$?
        ;;
    leaf)
        deploy_group "$LEAF_NODES" "leaf tier"
        verify_and_repair "$LEAF_NODES" "leaf tier"
        VERIFY_RC=$?
        ;;
    spine) deploy_group "$SPINE_NODES" "spine tier" ;;
    verify)
        verify_and_repair "$LEAF_NODES" "leaf tier"
        VERIFY_RC=$?
        ;;
    *)
        if echo "$ALL_NODES" | grep -qw "$SCOPE"; then
            deploy_node "$SCOPE"
        else
            echo "Unknown scope: $SCOPE"
            echo "Valid: gen, all, leaf, spine, verify, or node name (see topology.clab.yaml)"
            exit 1
        fi
        ;;
esac

echo ""
echo "============================================================"
echo "  $TOPOLOGY_NAME — $NUM_PLANES planes x ($NUM_SPINES spine x $NUM_LEAVES leaf) SRv6 CLOS"
echo "============================================================"
echo "  Topology:     $TOPOLOGY_NAME (from topology.clab.yaml)"
echo "  Config dir:   $CONFIGS_DIR"
echo "  Routing:      Controller-driven (no BGP, no IGP)"
echo "  Tenants:      green (uDT d000 -> Vrf-green on every leaf)"
echo "                yellow (host-based; uDT d001 seg6local on hosts)"
echo "============================================================"
echo ""
if [ "$VERIFY_RC" -ne 0 ]; then
    echo "Configuration complete with verification FAILURES (see log above)."
    echo "Re-run 'scripts/config.sh verify' or inspect the named leaves manually."
    exit "$VERIFY_RC"
fi
echo "Configuration complete!"
