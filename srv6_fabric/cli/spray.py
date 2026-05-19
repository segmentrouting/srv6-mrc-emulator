#!/usr/bin/env python3
"""spray — userspace SRv6/uSID packet sprayer + receiver (CLI entry point).

Thin wrapper around `srv6_fabric.runner`. The send/recv loops, payload
codec, address builders, and per-flow stats live in the library; this
module is just the argparse surface. Installed as `/usr/local/bin/spray`
in the lab host image via pyproject.toml's `[project.scripts]`.

See `docs/spray-protocol.md` for wire format and design rationale.

Encap shape — uSID, NO SRH:

    +--------------------------------------------------------+
    | IPv6  src=<host underlay>  dst=<uSID per plane>  nh=41 |   <- outer
    |   +----------------------------------------------------+
    |   | IPv6  src=<inner>  dst=<inner anycast>  nh=17      |   <- inner
    |   |   +------------------------------------------------+
    |   |   | UDP  sport  dport=<SPRAY_PORT>                 |
    |   |   |   +-------------+--------------------------+   |
    |   |   |   | seq (8 B)   | plane (1 B) | pad …      |   |
    +---+---+---+-------------+--------------------------+---+

Run inside any lab host container:

    docker exec -it green-host15 spray --role recv
    docker exec -it green-host00 spray --role send \\
        --dst-id 15 --rate 1000pps --duration 5s

Optional flags:
    --policy {round_robin,hash5tuple,weighted:0.4,0.3,0.2,0.1,health_aware_mrc}
    --mrc               (recv) start MRC receiver agent (probe responder +
                        per-plane loss reporter); sender side auto-starts
                        a SenderMrcAgent when --policy=health_aware_mrc
    --json              machine-readable result instead of human text
"""

from __future__ import annotations


import argparse
import json
import os
import re
import sys
from dataclasses import asdict, is_dataclass

from srv6_fabric.runner import (
    FlowEndpoint, run_receiver, run_sender, detect_self_id,
)
from srv6_fabric.policy import (
    policy_from_spec, HealthAwareMrc, HealthAwareMrcFactory,
)
from srv6_fabric.topo import (
    NUM_PLANES, NUM_SPINES, PLANE_NICS, SPRAY_PORT,
    host_underlay_addr, inner_addr, usid_outer_dst, spine_for,
)


# --- CLI parsing ------------------------------------------------------------

def parse_rate(s: str) -> int:
    """Accept '1000pps' or '1000'."""
    m = re.match(r"^(\d+)\s*pps?$", s, re.I) or re.match(r"^(\d+)$", s)
    if not m:
        raise argparse.ArgumentTypeError(f"bad --rate: {s!r}")
    return int(m.group(1))


def parse_duration(s: str) -> float:
    """Accept '5s', '500ms', '0' (forever), or bare seconds."""
    s = s.strip().lower()
    if s in ("", "0", "0s"):
        return 0.0
    m = re.match(r"^(\d+(?:\.\d+)?)(ms|s)?$", s)
    if not m:
        raise argparse.ArgumentTypeError(f"bad --duration: {s!r}")
    val = float(m.group(1))
    return val / 1000.0 if m.group(2) == "ms" else val


def parse_policy(s: str, *, tenant: str, ev_config=None,
                 paths_per_plane: int | None = None):
    """Convert CLI string into a SprayPolicy via policy_from_spec.

    Accepted forms:
        round_robin
        hash5tuple
        weighted:0.4,0.3,0.2,0.1
        ev_spray              (uses paths_per_plane override or NUM_SPINES)
        ev_spray:N            (explicit fan-out, overrides paths_per_plane arg)
        health_aware_mrc

    `health_aware_mrc` resolves the factory returned by policy_from_spec
    into a live policy by binding it to an EVStateTable for this
    sender's tenant. The optional `ev_config` (an EVStateConfig from
    load_configs_from_env) is passed to the table; if None, the table's
    own defaults are used. The probe and loss-report threads that feed
    the table live in cmd_send, which constructs a SenderMrcAgent from
    `policy.table` after this returns.

    `paths_per_plane` is the scenario-level override forwarded from the
    SRV6_PATHS_PER_PLANE env or --paths-per-plane CLI flag. It applies
    only when the policy string is bare "ev_spray" (no explicit N).
    Embedded "ev_spray:N" syntax always wins over the override, so a
    user with a CLI policy spec doesn't get silently retuned by a
    leftover env var.
    """
    s = s.strip()
    if s.startswith("weighted:"):
        weights = [float(w) for w in s.split(":", 1)[1].split(",")]
        return policy_from_spec({"weighted": weights})
    if s.startswith("ev_spray:"):
        n = int(s.split(":", 1)[1])
        return policy_from_spec({"ev_spray": n})
    if s == "ev_spray" and paths_per_plane is not None:
        return policy_from_spec({"ev_spray": paths_per_plane})
    policy = policy_from_spec(s)
    if isinstance(policy, HealthAwareMrcFactory):
        # Lazy import: keeps stdlib-only imports at top of file and
        # mirrors the laziness around scapy elsewhere in the runner.
        from srv6_fabric.mrc.ev_state import EVStateTable
        # One tenant per sender process today. If we ever multiplex
        # tenants in a single sender, this becomes a per-host singleton.
        table = EVStateTable(
            tenants=(tenant,), num_planes=NUM_PLANES,
            num_paths=NUM_SPINES, cfg=ev_config,
        )
        return policy.bind(table=table, tenant=tenant)
    return policy


# --- send -------------------------------------------------------------------

def _loss_fusion_stats_to_dict(stats) -> dict:
    """Best-effort serializer for `LossFusionStats`.

    LossFusionStats is a dataclass today. We avoid importing it at the
    top of spray.py to keep MRC modules out of the non-MRC import path,
    so this helper duck-types: dataclasses.asdict if it's a dataclass,
    else __dict__ as a fallback. Callers wrap this in try/except so a
    serialization failure can't sink a real run.
    """
    if is_dataclass(stats):
        return asdict(stats)
    return dict(vars(stats))


def cmd_send(args, tenant: str, my_id: int) -> int:
    if args.dst_id is None:
        print("spray.py: --dst-id is required for --role send", file=sys.stderr)
        return 2
    if my_id == args.dst_id:
        print("spray.py: --dst-id must differ from this host's id", file=sys.stderr)
        return 2

    flow = FlowEndpoint(tenant=tenant, src_id=my_id, dst_id=args.dst_id)

    # Read MRC tunables from SRV6_MRC_CONFIG_JSON (set by mrc/run.py
    # when a scenario has an `mrc:` block). When unset, both configs
    # come back at defaults. Parsing failures are fatal — better to
    # crash early than run a scenario with the wrong knobs.
    agent_cfg = None
    ev_cfg = None
    try:
        from srv6_fabric.mrc.agent import load_configs_from_env
        agent_cfg, ev_cfg = load_configs_from_env()
    except ValueError as e:
        print(f"spray.py: {e}", file=sys.stderr)
        return 2
    except ImportError:
        # MRC modules missing — only relevant if we're actually using
        # them; fall through and let the policy build fail loudly.
        pass

    # Resolve paths_per_plane: CLI flag wins over env var, env var wins
    # over policy default. None means "let policy_from_spec use NUM_SPINES".
    ppp: int | None = None
    env_ppp = os.environ.get("SRV6_PATHS_PER_PLANE")
    if env_ppp:
        try:
            ppp = int(env_ppp)
        except ValueError:
            print(
                f"spray.py: SRV6_PATHS_PER_PLANE={env_ppp!r} is not an int",
                file=sys.stderr,
            )
            return 2
    if getattr(args, "paths_per_plane", None) is not None:
        ppp = args.paths_per_plane

    policy = parse_policy(
        args.policy, tenant=tenant, ev_config=ev_cfg, paths_per_plane=ppp,
    )

    # When the policy is health_aware_mrc, we own a live EVStateTable
    # via policy.table. Start a SenderMrcAgent to drive probes + ingest
    # loss reports for the duration of the spray; its progress_cb
    # increments per-plane sent counters that the agent's window-rotate
    # thread snapshots into SentWindowRing for sender-driven loss
    # attribution. Lazy import keeps the non-MRC path free of MRC deps.
    mrc_agent = None
    progress_cb = None
    if isinstance(policy, HealthAwareMrc):
        from srv6_fabric.mrc.agent import SenderMrcAgent
        mrc_agent = SenderMrcAgent(
            tenant=policy.tenant,
            src_id=my_id,
            dst_id=args.dst_id,
            table=policy.table,
            config=agent_cfg,
        )
        progress_cb = lambda _seq, plane, path: mrc_agent.record_sent(plane, path)

    if not args.json:
        spine = spine_for(my_id, args.dst_id)
        src_inner = inner_addr(tenant, my_id)
        dst_inner = inner_addr(tenant, args.dst_id)
        print(f"spray.py SEND  tenant={tenant}  "
              f"src=host{my_id:02d}  dst=host{args.dst_id:02d}")
        print(f"               spine=p<P>-spine{spine:02d}  "
              f"policy={policy.name}  "
              f"rate={args.rate}pps  duration={args.duration}s")
        print(f"               inner: {src_inner} -> {dst_inner}")
        for p in range(NUM_PLANES):
            src_outer = host_underlay_addr(tenant, p, my_id)
            dst_outer = usid_outer_dst(tenant, p, spine, args.dst_id)
            print(f"                 plane {p}: {src_outer} -> {dst_outer}"
                  f"  via {PLANE_NICS[p]}")

    if mrc_agent is not None:
        mrc_agent.start()
    mrc_diag = None
    try:
        result = run_sender(
            flow, policy, args.rate, args.duration,
            progress_cb=progress_cb,
        )
    finally:
        # Capture EV-state + fusion-stats BEFORE stop() so the snapshot
        # reflects the live counters that produced the per-plane spray
        # distribution we just ran. Stop drains background threads;
        # nothing should mutate the table afterwards but better to be
        # explicit about ordering. None-safe so the non-MRC path is
        # unchanged.
        if mrc_agent is not None:
            try:
                mrc_diag = {
                    "ev_state": mrc_agent.table.snapshot(),
                    "loss_fusion": _loss_fusion_stats_to_dict(
                        mrc_agent.stats,
                    ),
                }
            except Exception as e:  # diag must never crash the run
                mrc_diag = {"error": f"snapshot failed: {e}"}
            mrc_agent.stop(timeout_s=1.0)

    if args.json:
        d = result.to_dict()
        if mrc_diag is not None:
            d["mrc"] = mrc_diag
        json.dump(d, sys.stdout)
        sys.stdout.write("\n")
        return 0

    d = result.to_dict()
    print()
    print(f"  sent {d['sent']} packets in {d['elapsed_s']}s "
          f"({d['sent'] / max(d['elapsed_s'], 1e-9):.0f} pps)")
    for p in range(NUM_PLANES):
        n = d["per_plane_sent"].get(p, 0)
        print(f"    plane {p}  ({PLANE_NICS[p]}) : {n}")
    if d["errors"]:
        print(f"    errors: {d['errors']}")
    return 0


# --- recv -------------------------------------------------------------------

def cmd_recv(args, tenant: str, my_id: int) -> int:
    # Set up the MRC receiver agent up-front if requested; it owns the
    # probe-RX sockets + the loss-report emitter and consumes data
    # packets via the on_packet hook below. Lazy import keeps the
    # non-MRC path free of MRC deps.
    mrc_agent = None
    on_packet = None
    if args.mrc:
        from srv6_fabric.mrc.agent import (
            ReceiverMrcAgent, load_configs_from_env,
        )
        from srv6_fabric.topo import (
            host_id_from_inner_addr, tenant_id as topo_tenant_id,
        )
        # Pull tunables from SRV6_MRC_CONFIG_JSON; receiver only uses
        # AgentConfig (it has no EV state machine of its own).
        try:
            agent_cfg, _ev_cfg = load_configs_from_env()
        except ValueError as e:
            print(f"spray.py: {e}", file=sys.stderr)
            return 2
        mrc_agent = ReceiverMrcAgent(
            tenant=tenant,
            my_id=my_id,
            config=agent_cfg,
        )

        def _on_packet(flow_key, plane: int, path: int, seq: int) -> None:
            # FlowKey.src_addr is the sender's inner address. Translate
            # to (tenant_id, src_id) so the receiver can route loss
            # reports back via its sender cache. Packets from senders
            # we don't recognize (e.g. addresses outside our topology)
            # are dropped silently — they don't belong to this run.
            parsed = host_id_from_inner_addr(flow_key.src_addr)
            if parsed is None:
                return
            sender_tenant, src_id = parsed
            if sender_tenant != tenant:
                # Cross-tenant noise; shouldn't happen in the lab but
                # we don't trust the wire.
                return
            tid = topo_tenant_id(sender_tenant)
            agent_flow_key = (tid, src_id, my_id)
            mrc_agent.record_data(
                agent_flow_key, plane=plane, path=path, seq=seq,
            )

        on_packet = _on_packet

    if not args.json:
        idle_msg = (
            f"auto-exit after {args.idle_timeout:g}s of silence (after first pkt)"
            if args.idle_timeout > 0 else "Ctrl-C to stop"
        )
        print(f"spray.py RECV  tenant={tenant}  self=host{my_id:02d}")
        print(f"               listening on {', '.join(PLANE_NICS)}  "
              f"port={SPRAY_PORT}")
        print(f"               ({idle_msg})")
        if tenant == "yellow":
            print(f"               yellow: outer SR still on wire at NIC; "
                  f"sniffer peels it.")
        if mrc_agent is not None:
            print(f"               mrc: probe responder + loss reporter active")
        print()

    if mrc_agent is not None:
        mrc_agent.start()
    try:
        report = run_receiver(
            self_host=f"{tenant}-host{my_id:02d}",
            self_id=my_id,
            tenant=tenant,
            idle_timeout_s=args.idle_timeout,
            on_packet=on_packet,
        )
    finally:
        if mrc_agent is not None:
            mrc_agent.stop(timeout_s=1.0)

    if args.json:
        json.dump(report, sys.stdout)
        sys.stdout.write("\n")
        return 0

    total = sum(report["per_nic"].values())
    print()
    print(f"  received {total} packets")
    print(f"  per NIC:")
    for nic in PLANE_NICS:
        print(f"    {nic}: {report['per_nic'].get(nic, 0)}")
    print(f"  per plane (from payload):")
    for p in range(NUM_PLANES):
        print(f"    plane {p}: {report['per_plane'].get(p, 0)}")
    print(f"  flows: {len(report['flows'])}")
    for f in report["flows"]:
        print(f"    {f['src']}:{f['sport']} -> "
              f"{f['dst']}:{f['dport']}: "
              f"rx={f['received']}  loss={f['loss']}  dup={f['duplicates']}  "
              f"reord_max={f['reorder_max']}  "
              f"p99={f['reorder_p99']}")
    return 0


# --- main -------------------------------------------------------------------

def main() -> int:
    p = argparse.ArgumentParser(
        description="userspace SRv6/uSID packet sprayer + receiver",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--role", required=True, choices=("send", "recv"))
    p.add_argument("--dst-id", type=int, default=None,
                   help="(send) destination host id 0..15")
    p.add_argument("--rate", type=parse_rate, default=parse_rate("1000pps"),
                   help="(send) packets/sec, e.g. 1000 or 1000pps")
    p.add_argument("--duration", type=parse_duration,
                   default=parse_duration("5s"),
                   help="(send) e.g. 5s, 500ms, or 0 to run until ^C")
    p.add_argument("--policy", type=str, default="round_robin",
                   help="(send) spray policy: round_robin (default), "
                        "hash5tuple, 'weighted:0.4,0.3,0.2,0.1', "
                        "ev_spray[:N] (EV-aware spray with N spines per "
                        "plane; default N=NUM_SPINES), or "
                        "health_aware_mrc (auto-starts the SenderMrcAgent; "
                        "pair with --mrc on the receiver)")
    p.add_argument("--paths-per-plane", type=int, default=None,
                   help="(send) override the EV fan-out for ev_spray "
                        "policies. Equivalent to ev_spray:N in --policy, "
                        "but lets the scenario YAML's paths_per_plane "
                        "field propagate via env without rewriting the "
                        "policy string. CLI flag wins over env; env wins "
                        "over policy default.")
    p.add_argument("--idle-timeout", type=parse_duration,
                   default=parse_duration("6s"),
                   help="(recv) auto-exit after this much silence "
                        "following the first packet; 0 disables. "
                        "Default 6s.")
    p.add_argument("--mrc", action="store_true",
                   help="(recv) start the MRC receiver agent: probe "
                        "responder + per-window loss reporter back to "
                        "senders. Default off — baseline runs stay "
                        "scapy-only.")
    p.add_argument("--json", action="store_true",
                   help="emit machine-readable JSON result instead of "
                        "human-readable output (used by mrc orchestrator)")
    args = p.parse_args()

    try:
        tenant, my_id = detect_self_id()
    except ValueError as e:
        print(f"spray.py: {e}", file=sys.stderr)
        return 2

    if args.role == "send":
        return cmd_send(args, tenant, my_id)
    return cmd_recv(args, tenant, my_id)


if __name__ == "__main__":
    sys.exit(main())
