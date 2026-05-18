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

    plane = policy.pick(seq, flow_key)
    sender.record_sent(plane)            # feeds SentWindowRing
    if plane != bad_plane:
        receiver.record_data(flow_key, plane, seq)  # feeds LossWindowTable

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
    AgentConfig, ReceiverMrcAgent, SenderMrcAgent,
)
from srv6_fabric.mrc.ev_state import EVStateConfig, EVStateTable
from srv6_fabric.policy import HealthAwareMrc
from srv6_fabric.topo import FlowKey, NUM_PLANES, NUM_SPINES, tenant_id as topo_tenant_id

# Reuse the loopback plumbing the I/O tests already built. Importing
# from a sibling test module is unusual; the alternative is duplicating
# ~80 lines of port allocation + socket factories, which would drift
# the moment the agent's construction signature changes.
from tests.test_mrc_agent_io import (  # noqa: E402
    FAST_CONFIG, PORTS, _PeerOverride, _build_receiver_factory,
    _build_sender_factory, _wait_for,
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
    sender_probe_base = PORTS.take(NUM_PLANES)
    sender_report_port = PORTS.take(1)
    recv_probe_base = PORTS.take(NUM_PLANES)

    s_probe_factory, s_report_factory = _build_sender_factory(
        sender_probe_base, sender_report_port,
    )
    sender = SenderMrcAgent(
        tenant="green", src_id=0, dst_id=15,
        table=table, config=FAST_CONFIG,
        sockets_factory=s_probe_factory,
        report_socket_factory=s_report_factory,
    )
    _PeerOverride(sender, recv_probe_base, sender_report_port)

    r_probe_factory = _build_receiver_factory(recv_probe_base)
    receiver = ReceiverMrcAgent(
        tenant="green", my_id=15,
        config=FAST_CONFIG,
        sockets_factory=r_probe_factory,
    )

    flow_key = (topo_tenant_id("green"), 0, 15)
    return sender, receiver, flow_key


def _drive_spray(policy: HealthAwareMrc, sender: SenderMrcAgent,
                 receiver: ReceiverMrcAgent, flow_key,
                 *, n: int, bad_plane=None, bad_loss: float = 1.0,
                 per_plane_seq=None,
                 ) -> collections.Counter:
    """Simulate `n` data packets through `policy.pick`.

    Each plane is given its own monotonic seq stream (in `per_plane_seq`,
    a mutable list of length NUM_PLANES the caller threads across
    invocations) so the receiver's max_seq − min_seq + 1 estimate of
    "expected" is accurate; mixing one global seq across planes would
    let normal plane-skipping look like loss in the receiver's per-plane
    window.

    `bad_plane` simulates partial-or-total loss on a single plane:
    fraction `bad_loss` of packets picked for `bad_plane` are dropped
    (not handed to receiver.record_data). bad_loss=1.0 means 100% loss
    but then the receiver sees zero arrivals on bad_plane and its loss
    window has nothing to estimate from — use 0.5 if you actually want
    the receiver to flag the plane.
    """
    if per_plane_seq is None:
        per_plane_seq = [0] * NUM_PLANES
    pol_flow = FlowKey(src_addr="fc00::1", dst_addr="fc00::15",
                       src_port=10000, dst_port=20000)
    picks: collections.Counter = collections.Counter()
    for i in range(n):
        plane = policy.pick(i, pol_flow)
        picks[plane] += 1
        sender.record_sent(plane)
        seq = per_plane_seq[plane]
        per_plane_seq[plane] += 1
        drop = False
        if plane == bad_plane and bad_loss > 0:
            if bad_loss >= 1.0:
                drop = True
            else:
                step = max(1, int(round(1.0 / bad_loss)))
                drop = (seq % step) == 0
        if not drop:
            receiver.record_data(flow_key, plane=plane, seq=seq)
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

            # Drive 200 picks across ~4 loss windows. With no plane
            # dropped, every packet reaches the receiver.
            seqs = [0] * NUM_PLANES
            picks = _drive_spray(policy, sender, receiver, flow_key,
                                 n=200, bad_plane=None,
                                 per_plane_seq=seqs)
            time.sleep(FAST_CONFIG.loss_window_ms * 3.0 / 1000.0)

            # No plane should be ASSUMED_BAD. We pull the per-plane
            # state from the EVStateTable directly.
            for plane in range(NUM_PLANES):
                st = table.state("green", plane, 0)
                self.assertNotEqual(
                    st.name, "ASSUMED_BAD",
                    f"plane {plane} unexpectedly demoted; state={st}",
                )

            # Distribution sanity: uniform-ish weights mean no plane
            # should have gotten zero picks across 200 draws. (The
            # golden-ratio picker is deterministic but does spread across
            # all bins when weights are equal.)
            self.assertEqual(set(picks), set(range(NUM_PLANES)),
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
        # failure drops *one EV* (here (BAD_PLANE, 0), since loss_window
        # still emits path_id=0 pending commit 4/5 of the per-EV
        # rewrite). We verify that EV's share collapses to ~0 and its
        # siblings on the same plane absorb the slack — *not* that the
        # whole plane drops out.
        BAD_PLANE = 2
        BAD_PATH = 0

        try:
            receiver.start()
            sender.start()
            self.assertTrue(_wait_for(
                lambda: (topo_tenant_id("green"), 0)
                        in receiver.known_senders(),
                timeout_s=1.0,
            ), "receiver never learned sender")

            # Phase 1: drive picks while dropping ~50% of plane-BAD
            # packets. We need enough arrivals on BAD that the receiver
            # gets a non-degenerate (max_seq − min_seq + 1) estimate of
            # "expected" for that plane — pure 100% loss would leave BAD
            # with zero arrivals and the LossWindow would report 0/0.
            seqs = [0] * NUM_PLANES
            for _round_ix in range(4):
                _drive_spray(policy, sender, receiver, flow_key,
                             n=200, bad_plane=BAD_PLANE, bad_loss=0.5,
                             per_plane_seq=seqs)
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
