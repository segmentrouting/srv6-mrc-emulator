"""Closed-loop integration test: sender + receiver + EVStateTable + HealthAwareMrc.

The agent-io tests (test_mrc_agent_io.py) prove the probe and report
wire formats survive a real socket round-trip. The scenario tests prove
the YAML schema validates. This test stitches the two together: drive a
fake spray loop where the policy under test (HealthAwareMrc) reads
`weights()` from the same EVStateTable the receiver's loss reports are
feeding, and verify that injecting per-plane loss bends the pick
distribution as expected.

The data plane is fully simulated — we do not run the spray runner here.
A simulated "send" is just:

    plane, path = policy.pick_ev(seq, flow_key)
    sender.record_sent(plane, path)       # feeds SentWindowRing[plane][path]
    if (plane, path) != bad_ev:
        receiver.record_data(flow_key, plane=plane, path=path, seq=seq)

So the receiver "sees" 100% of plane-0 traffic, 0% of bad_plane traffic
(the rest somewhere in between for the asymmetric variant). The
receiver's loss-emit thread then sends a real LOSS_REPORT to the real
sender over loopback, the sender's report-RX thread decodes it, the
EVStateTable transitions the bad plane to ASSUMED_BAD, and the policy's
next `pick()` call reads the new weights.

This is the test that catches breakage at the seam between "wire format
works" and "policy reacts": e.g. a future schema change that breaks the
report decode, a thresholding regression, or a weight-table caching bug.
"""

from __future__ import annotations

import collections
import time
import unittest

from srv6_fabric.mrc.agent import (
    ReceiverMrcAgent, SenderMrcAgent,
)
from srv6_fabric.mrc.ev_state import EVStateConfig, EVStateTable
from srv6_fabric.policy import HealthAwareMrc
from srv6_fabric.topo import FlowKey, NUM_PLANES, NUM_SPINES, tenant_id as topo_tenant_id

# Reuse the loopback plumbing the I/O tests already built. Importing
# from a sibling test module is unusual; the alternative is duplicating
# ~80 lines of port allocation + transport construction, which would
# drift the moment the agent's construction signature changes.
from tests.test_mrc_agent_io import (  # noqa: E402
    FAST_CONFIG, PORTS, _build_loopback_pair, _wait_for,
)


# Aggressive thresholds so a few hundred ms of simulated traffic is
# enough to flip a plane. Production defaults need a couple of windows
# of sustained loss; the test budget is too small for that.
FAST_EV = EVStateConfig(
    loss_threshold=0.05,
    loss_demote_consecutive=1,   # one bad window is enough
    min_active_evs=1,         # let us demote up to 3 of 4
)


def _build_pair(table: EVStateTable):
    """Spin up a sender+receiver pair over loopback bound to `table`.

    Returns (sender, receiver, flow_key). Caller is responsible for
    .start() and .stop() on both agents.
    """
    sender_report_port = PORTS.take(1)
    receiver_probe_port = PORTS.take(1)

    sender_xport, receiver_xport = _build_loopback_pair(
        sender_report_port=sender_report_port,
        receiver_probe_port=receiver_probe_port,
    )
    sender = SenderMrcAgent(
        tenant="green", src_id=0, dst_id=15,
        table=table, config=FAST_CONFIG,
        transport=sender_xport,
    )
    receiver = ReceiverMrcAgent(
        tenant="green", my_id=15,
        config=FAST_CONFIG,
        transport=receiver_xport,
    )

    flow_key = (topo_tenant_id("green"), 0, 15)
    return sender, receiver, flow_key


def _drive_spray(policy: HealthAwareMrc, sender: SenderMrcAgent,
                 receiver: ReceiverMrcAgent, flow_key,
                 *, n: int, bad_ev=None, bad_loss: float = 1.0,
                 per_ev_seq=None,
                 ) -> collections.Counter:
    """Simulate `n` data packets through `policy.pick_ev`.

    Each EV (plane, path) is given its own monotonic seq stream (in
    `per_ev_seq`, a mutable dict the caller threads across invocations)
    so the receiver's per-EV max_seq − min_seq + 1 estimate of
    "expected" matches actual sent packets — using a per-plane stream
    would let normal EV-skipping look like loss in the receiver's
    per-EV loss window.

    `bad_ev` is a `(plane, path)` tuple or callable `(plane, path) ->
    bool` that decides whether the EV is "broken". `bad_loss` is the
    drop probability for matching EVs (0..1). With `bad_loss < 1.0`
    we still need *some* arrivals on the broken EV so the receiver's
    loss window has min/max to estimate expected from; use 0.5 for
    real partial-loss scenarios.

    Returns a Counter keyed by `(plane, path)` recording how many
    packets the policy picked for each EV.
    """
    if per_ev_seq is None:
        per_ev_seq = {}
    pol_flow = FlowKey(src_addr="fc00::1", dst_addr="fc00::15",
                       src_port=10000, dst_port=20000)
    if callable(bad_ev):
        is_bad = bad_ev
    elif bad_ev is None:
        is_bad = lambda _p, _q: False  # noqa: E731
    else:
        bp, bq = bad_ev
        is_bad = lambda p, q: p == bp and q == bq  # noqa: E731
    picks: collections.Counter = collections.Counter()
    for i in range(n):
        plane, path = policy.pick_ev(i, pol_flow)
        picks[(plane, path)] += 1
        sender.record_sent(plane, path)
        seq = per_ev_seq.get((plane, path), 0)
        per_ev_seq[(plane, path)] = seq + 1
        drop = False
        if is_bad(plane, path) and bad_loss > 0:
            if bad_loss >= 1.0:
                drop = True
            else:
                step = max(1, int(round(1.0 / bad_loss)))
                drop = (seq % step) == 0
        if not drop:
            receiver.record_data(
                flow_key, plane=plane, path=path, seq=seq,
            )
    return picks


class HealthyFabricTests(unittest.TestCase):
    """No loss injected: every plane should remain GOOD/UNKNOWN and the
    pick distribution should stay roughly uniform across windows."""

    def test_clean_fabric_keeps_planes_healthy(self) -> None:
        table = EVStateTable(
            tenants=("green",), num_planes=NUM_PLANES, num_paths=NUM_SPINES, cfg=FAST_EV,
        )
        policy = HealthAwareMrc(table=table, tenant="green")
        sender, receiver, flow_key = _build_pair(table)

        try:
            receiver.start()
            sender.start()
            # Wait for the receiver to learn the sender via probes
            # (otherwise the first loss-emit round can't reach back).
            self.assertTrue(_wait_for(
                lambda: (topo_tenant_id("green"), 0)
                        in receiver.known_senders(),
                timeout_s=1.0,
            ), "receiver never learned sender")

            # Drive 200 picks across ~4 loss windows. With no EV
            # dropped, every packet reaches the receiver.
            seqs = {}
            picks = _drive_spray(policy, sender, receiver, flow_key,
                                 n=200, bad_ev=None,
                                 per_ev_seq=seqs)
            time.sleep(FAST_CONFIG.loss_window_ms * 3.0 / 1000.0)

            # No EV should be ASSUMED_BAD. Check every (plane, path)
            # cell in the EVStateTable.
            for plane in range(NUM_PLANES):
                for path in range(NUM_SPINES):
                    st = table.state("green", plane, path)
                    self.assertNotEqual(
                        st.name, "ASSUMED_BAD",
                        f"EV ({plane},{path}) unexpectedly demoted; "
                        f"state={st}",
                    )

            # Distribution sanity: uniform weights mean every plane
            # should have gotten picks across 200 draws. (We aggregate
            # ev_picks to plane buckets for the legacy check.)
            planes_seen = {plane for (plane, _path) in picks}
            self.assertEqual(planes_seen, set(range(NUM_PLANES)),
                             f"clean fabric left a plane unused: {picks}")
        finally:
            sender.stop(timeout_s=0.5)
            receiver.stop(timeout_s=0.5)


class PlaneLossShiftsDistributionTests(unittest.TestCase):
    """Inject loss on a single EV: the receiver reports it, the EV
    table demotes that EV (not the whole plane), and subsequent picks
    redistribute to the remaining 31 EVs."""

    def test_plane_loss_demotes_and_picks_shift(self) -> None:
        table = EVStateTable(
            tenants=("green",), num_planes=NUM_PLANES, num_paths=NUM_SPINES, cfg=FAST_EV,
        )
        policy = HealthAwareMrc(table=table, tenant="green")
        sender, receiver, flow_key = _build_pair(table)

        # EV-centric expectation: with NUM_PLANES planes and NUM_SPINES
        # paths/plane there are NUM_PLANES * NUM_SPINES EVs; a single
        # failure drops *one EV*. We pick (BAD_PLANE, BAD_PATH) and
        # drop only packets the policy steers there. The receiver's
        # per-EV loss window attributes the drops to that EV's record,
        # the EVStateTable demotes it, and pick_ev never visits it
        # again. Sibling EVs (same plane, different path) stay healthy
        # and absorb the slack.
        BAD_PLANE = 2
        BAD_PATH = 1

        try:
            receiver.start()
            sender.start()
            self.assertTrue(_wait_for(
                lambda: (topo_tenant_id("green"), 0)
                        in receiver.known_senders(),
                timeout_s=1.0,
            ), "receiver never learned sender")

            # Phase 1: drive picks while dropping ~50% of the bad EV's
            # packets. We need enough arrivals on it that the receiver
            # gets a non-degenerate (max_seq − min_seq + 1) estimate of
            # "expected" for that EV — pure 100% loss would leave the
            # EV with zero arrivals and the LossWindow would emit no
            # record for it at all.
            seqs = {}
            for _round_ix in range(4):
                _drive_spray(policy, sender, receiver, flow_key,
                             n=200, bad_ev=(BAD_PLANE, BAD_PATH),
                             bad_loss=0.5, per_ev_seq=seqs)
                time.sleep(FAST_CONFIG.loss_window_ms / 1000.0)

            # Wait for the demote to propagate to the specific EV that
            # carried the loss feedback.
            def bad_ev_demoted() -> bool:
                return (
                    table.state("green", BAD_PLANE, BAD_PATH).name
                    == "ASSUMED_BAD"
                )
            self.assertTrue(_wait_for(bad_ev_demoted, timeout_s=2.0),
                            f"EV ({BAD_PLANE},{BAD_PATH}) never demoted; "
                            f"snapshot={table.snapshot()}")

            # Sanity: targeted EV should be demoted but the loss-feedback
            # plumbing should not collateral-damage healthy siblings.
            # A regression where SentWindow.sent is rolled up per-plane
            # (instead of per-EV) would over-count denominators by the
            # spray-fanout factor and force every EV in the affected
            # plane toward ASSUMED_BAD; if we ever lose that property
            # this assert will catch it.
            snap = table.snapshot()["tenants"]["green"]
            bad_count = sum(1 for e in snap if e["state"] == "assumed_bad")
            self.assertEqual(
                bad_count, 1,
                f"expected exactly one demoted EV; got {bad_count}: "
                f"{[ (e['plane'], e['path'], e['state']) for e in snap ]}",
            )

            # Phase 2: post-demote distribution. Use pick_ev so we can
            # observe per-EV behavior. The demoted EV should attract
            # essentially zero picks; the remaining (NUM_SPINES − 1) EVs
            # on plane BAD share its load with their plane peers.
            pol_flow = FlowKey(
                src_addr="fc00::1", dst_addr="fc00::15",
                src_port=10000, dst_port=20000,
            )
            n_picks = 4096
            ev_picks: collections.Counter = collections.Counter()
            for i in range(n_picks):
                ev_picks[policy.pick_ev(i, pol_flow)] += 1

            bad_ev_share = ev_picks[(BAD_PLANE, BAD_PATH)] / n_picks
            self.assertLess(
                bad_ev_share, 0.01,
                f"demoted EV ({BAD_PLANE},{BAD_PATH}) still receiving "
                f"{bad_ev_share:.2%} of picks; ev_picks={ev_picks}",
            )

            # The surviving EVs on plane BAD should still absorb traffic
            # (and slightly more than 1/(num_evs) each, since the
            # demoted EV's share got redistributed).
            uniform = 1.0 / (NUM_PLANES * NUM_SPINES)
            for path in range(NUM_SPINES):
                if path == BAD_PATH:
                    continue
                share = ev_picks[(BAD_PLANE, path)] / n_picks
                self.assertGreater(
                    share, uniform * 0.5,
                    f"surviving EV ({BAD_PLANE},{path}) under-utilised "
                    f"post-demote: {share:.2%}; ev_picks={ev_picks}",
                )
        finally:
            sender.stop(timeout_s=0.5)
            receiver.stop(timeout_s=0.5)


if __name__ == "__main__":
    unittest.main()
