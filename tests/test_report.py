"""Tests for srv6_mrc.report — merge logic between sender/receiver records."""
import json
import unittest

from srv6_mrc.report import ScenarioReport


def _sender(src="green-host00", dst="green-host15", tenant="green",
            policy="round_robin", spine=0, sent=4000, **kw):
    base = {
        "src": src, "dst": dst, "tenant": tenant, "policy": policy,
        "spine": spine, "rate_pps": 1000, "duration_s": 4.0,
        "sent": sent, "elapsed_s": 4.001,
        "per_plane_sent": {0: 1000, 1: 1000, 2: 1000, 3: 1000},
        "errors": 0,
    }
    base.update(kw)
    return base


def _recv_flow(src, dst, received=1000, loss=0, **kw):
    """Fixture matching FlowStats.to_dict() schema exactly."""
    base = {
        "src": src, "dst": dst,
        "sport": 9999, "dport": 9999,
        "received": received, "loss": loss, "duplicates": 0,
        "first_seq": 0, "last_seq": received - 1,
        "expected": received,
        "reorder_hist": {0: received},
        "reorder_max": 0,
        "reorder_mean": 0.0,
        "reorder_p99": 0,
        "per_plane_recv": {0: received // 4, 1: received // 4,
                           2: received // 4, 3: received // 4},
    }
    base.update(kw)
    return base


def _receiver(host="green-host15", tenant="green",
              flows=None, per_nic=None, per_plane=None):
    return {
        "host": host, "self_id": int(host[-2:]),
        "tenant": tenant,
        "per_nic": per_nic or {f"eth{i+1}": 1000 for i in range(4)},
        "per_plane": per_plane or {0: 1000, 1: 1000, 2: 1000, 3: 1000},
        "flows": flows or [],
    }


class TestMatchingHappyPath(unittest.TestCase):

    def test_single_flow_matched(self):
        s = _sender()
        r = _receiver(flows=[_recv_flow(
            "2001:db8:bbbb:00::2", "2001:db8:bbbb:0f::2",
            received=4000,
        )])
        rep = ScenarioReport.from_records("baseline", [s], [r])
        self.assertEqual(len(rep.flows), 1)
        f = rep.flows[0]
        self.assertEqual(f.sent, 4000)
        self.assertEqual(f.received, 4000)
        self.assertEqual(f.loss, 0)
        self.assertEqual(f.loss_pct(), 0.0)
        self.assertEqual(rep.warnings, [])

    def test_yellow_tenant_addresses_resolve(self):
        s = _sender(src="yellow-host03", dst="yellow-host12", tenant="yellow")
        r = _receiver(host="yellow-host12", tenant="yellow",
                      flows=[_recv_flow(
                          "2001:db8:cccc:03::2", "2001:db8:cccc:0c::2",
                          received=4000,
                      )])
        rep = ScenarioReport.from_records("baseline", [s], [r])
        self.assertEqual(len(rep.flows), 1)
        self.assertEqual(rep.flows[0].received, 4000)
        self.assertEqual(rep.warnings, [])

    def test_compressed_vs_padded_ipv6_addresses_canonicalize(self):
        # Sender side computes inner_addr() = '2001:db8:bbbb:0f::2' (padded);
        # receiver side gets scapy-canonical '2001:db8:bbbb:f::2'. They MUST
        # match. Regression test for the orphan-flow bug seen in baseline.
        s = _sender()  # default src=host00, dst=host15
        r = _receiver(flows=[_recv_flow(
            "2001:db8:bbbb::2",     # scapy form of host00 (was 0:::2)
            "2001:db8:bbbb:f::2",   # scapy form of host15 (was 0f::2)
            received=4000,
        )])
        rep = ScenarioReport.from_records("baseline", [s], [r])
        self.assertEqual(len(rep.flows), 1)
        self.assertEqual(rep.flows[0].received, 4000)
        self.assertEqual(rep.warnings, [])


class TestMissingReceiver(unittest.TestCase):

    def test_sender_with_no_matching_receiver_record(self):
        s = _sender()
        rep = ScenarioReport.from_records("x", [s], [])
        self.assertEqual(len(rep.flows), 1)
        self.assertIsNone(rep.flows[0].received)
        self.assertTrue(any("no receiver record" in n
                            for n in rep.flows[0].notes))

    def test_receiver_present_but_no_matching_flow(self):
        s = _sender(dst="green-host15")
        r = _receiver(host="green-host15", flows=[])
        rep = ScenarioReport.from_records("x", [s], [r])
        self.assertIsNone(rep.flows[0].received)
        self.assertTrue(any("saw no flow" in n
                            for n in rep.flows[0].notes))


class TestOrphanFlows(unittest.TestCase):

    def test_orphan_receiver_flow_becomes_warning(self):
        s = _sender()
        r = _receiver(flows=[
            _recv_flow("2001:db8:bbbb:00::2", "2001:db8:bbbb:0f::2",
                       received=4000),
            # Stray flow nobody sent: warning.
            _recv_flow("2001:db8:bbbb:07::2", "2001:db8:bbbb:0f::2",
                       received=42),
        ])
        rep = ScenarioReport.from_records("x", [s], [r])
        self.assertEqual(len(rep.flows), 1)
        self.assertEqual(rep.flows[0].received, 4000)
        self.assertTrue(any("orphan flow" in w for w in rep.warnings))


class TestDuplicateReceiverHost(unittest.TestCase):

    def test_duplicate_receiver_host_warns(self):
        r1 = _receiver(host="green-host15", flows=[
            _recv_flow("2001:db8:bbbb:00::2", "2001:db8:bbbb:0f::2",
                       received=4000)])
        r2 = _receiver(host="green-host15", flows=[])
        rep = ScenarioReport.from_records("x", [_sender()], [r1, r2])
        self.assertTrue(any("duplicate receiver record" in w
                            for w in rep.warnings))
        # First record should still drive the merge.
        self.assertEqual(rep.flows[0].received, 4000)


class TestLossAccounting(unittest.TestCase):

    def test_loss_pct_computed(self):
        s = _sender(sent=4000)
        r = _receiver(flows=[_recv_flow(
            "2001:db8:bbbb:00::2", "2001:db8:bbbb:0f::2",
            received=3000, loss=1000,
        )])
        rep = ScenarioReport.from_records("x", [s], [r])
        self.assertEqual(rep.flows[0].loss_pct(), 25.0)


class TestSerialization(unittest.TestCase):

    def test_to_dict_round_trip(self):
        s = _sender()
        r = _receiver(flows=[_recv_flow(
            "2001:db8:bbbb:00::2", "2001:db8:bbbb:0f::2", received=4000)])
        rep = ScenarioReport.from_records("baseline", [s], [r])
        d = rep.to_dict()
        self.assertEqual(d["scenario"], "baseline")
        self.assertEqual(len(d["flows"]), 1)
        self.assertEqual(d["flows"][0]["loss_pct"], 0.0)
        # JSON-serializable
        import json
        json.dumps(d, default=str)

    def test_render_ascii_contains_key_fields(self):
        # 30 packets reordered (bin k=4 has 30), max=12, p99=8.
        s = _sender(sent=5000)
        r = _receiver(flows=[_recv_flow(
            "2001:db8:bbbb:00::2", "2001:db8:bbbb:0f::2",
            received=4750, loss=250,
            reorder_hist={0: 4720, 4: 30},
            reorder_max=12, reorder_p99=8)])
        rep = ScenarioReport.from_records("hash5tuple", [s], [r])
        out = rep.render_ascii()
        self.assertIn("scenario: hash5tuple", out)
        self.assertIn("green-host00 -> green-host15", out)
        self.assertIn("5000", out)
        self.assertIn("4750", out)
        self.assertIn("5.00%", out)  # loss pct
        self.assertIn("per-plane (sent / rx)", out)

    def test_reordered_count_derived_from_histogram(self):
        # reordered = sum of bins with k > 0.
        s = _sender(sent=1000)
        r = _receiver(flows=[_recv_flow(
            "2001:db8:bbbb:00::2", "2001:db8:bbbb:0f::2",
            received=1000,
            reorder_hist={0: 950, 1: 30, 5: 20},
            reorder_max=5, reorder_p99=5)])
        rep = ScenarioReport.from_records("x", [s], [r])
        self.assertEqual(rep.flows[0].reordered, 50)

    def test_render_ascii_renders_warnings(self):
        rep = ScenarioReport(scenario="x")
        rep.warnings.append("something happened")
        self.assertIn("something happened", rep.render_ascii())

    def test_render_ascii_renders_notes(self):
        rep = ScenarioReport.from_records("x", [_sender()], [])
        self.assertIn("no receiver record", rep.render_ascii())

    def test_render_ascii_shows_duration_at_top(self):
        # Cosmetic: the report should surface wall-clock duration so
        # operators don't have to guess how long to wait. Uniform
        # durations render as "duration: 4s (1 flow(s))".
        rep = ScenarioReport.from_records("x", [_sender()], [])
        out = rep.render_ascii()
        lines = out.splitlines()
        # Header line is line 0 ("scenario: x"); duration should be
        # the very next line, before the "=" separator.
        self.assertEqual(lines[0], "scenario: x")
        self.assertEqual(lines[1], "  duration: 4s (1 flow(s))")
        self.assertTrue(lines[2].startswith("="))

    def test_render_ascii_mixed_durations_says_up_to(self):
        # Heterogeneous flow durations get the "up to … (mixed)" form,
        # matching the orchestrator's --verbose preamble convention.
        s1 = _sender(src="green-host00", dst="green-host15")
        s2_dict = _sender(src="green-host01", dst="green-host14")
        s2_dict["duration_s"] = 8.0
        rep = ScenarioReport.from_records("x", [s1, s2_dict], [])
        out = rep.render_ascii()
        self.assertIn("duration: up to 8s (2 flow(s), mixed durations)",
                      out)

    def test_render_ascii_no_flows_omits_duration(self):
        # Empty scenario (e.g. dry-run merge of nothing) must not blow
        # up trying to derive a duration from an empty list.
        rep = ScenarioReport(scenario="empty")
        out = rep.render_ascii()
        self.assertNotIn("duration:", out)

    def test_render_ascii_collapses_mrc_snapshot_policy_label(self):
        # PR #1 introduced policy labels like
        # `mrc_snapshot(green@/dev/shm/srv6-mrc/<host>/<tenant>_<dd>.json)`
        # that ran ~60 chars wide and broke column alignment. The
        # ASCII renderer must collapse those to just `mrc_snapshot`
        # so the sent/rx/loss columns stay aligned with the header.
        # JSON output is unaffected (full label preserved).
        s = _sender(
            policy=("mrc_snapshot(green@/dev/shm/srv6-mrc/"
                    "green-host00/green_07.json)"),
        )
        rep = ScenarioReport.from_records("x", [s], [])
        out = rep.render_ascii()
        # Short form present in ASCII...
        self.assertIn("mrc_snapshot ", out)
        # ...long form absent from ASCII...
        self.assertNotIn("/dev/shm/srv6-mrc", out)
        # ...but still in the JSON (forensic value).
        self.assertIn("/dev/shm/srv6-mrc",
                      json.dumps(rep.to_dict(), default=str))

    def test_render_ascii_header_and_row_columns_align(self):
        # Regression guard for the pre-PR-1 bug where the header used
        # `<24` for policy but the row used `<14`, causing the sent/
        # rx/loss columns to march left of their headers. Pin that
        # the header and the first row have their "sent" column
        # starting at the same character offset.
        s = _sender(policy="round_robin")
        rep = ScenarioReport.from_records("x", [s], [])
        out = rep.render_ascii()
        lines = out.splitlines()
        # Find header line (contains "policy" and "sent").
        hdr = next(l for l in lines if " policy " in l and " sent " in l)
        # Find the first data row (starts with "  green-host00 ").
        row = next(l for l in lines
                   if l.startswith("  green-host00 -> green-host15"))
        # Both lines must have "5000"-style numbers in the same
        # column band. Take the offset of `sent` in the header and
        # require the row to have a digit there (no padding mismatch).
        sent_col = hdr.index("sent")
        # The sent column is right-justified width 6, so the digit
        # may appear in any of the 6 positions ending at sent_col+4.
        self.assertTrue(
            any(row[sent_col:sent_col + 6].strip().isdigit()
                for _ in [None]),
            f"row 'sent' column doesn't align with header. "
            f"hdr={hdr!r}\nrow={row!r}",
        )

    def test_per_ev_sent_is_forwarded_into_flow_row(self):
        # EV-aware senders (e.g. ev_spray) emit a per_ev_sent map keyed
        # by "P<p>:S<s>" strings. The report layer must preserve those
        # keys verbatim in the merged JSON so consumers can plot EV-
        # level balance.
        s = _sender(policy="ev_spray",
                    per_ev_sent={"P0:S3": 25, "P1:S3": 25,
                                 "P2:S3": 25, "P3:S3": 25,
                                 "P0:S7": 25, "P1:S7": 25,
                                 "P2:S7": 25, "P3:S7": 25})
        rep = ScenarioReport.from_records("ev", [s], [])
        row = rep.flows[0]
        self.assertEqual(row.per_ev_sent["P0:S3"], 25)
        self.assertEqual(sum(row.per_ev_sent.values()), 200)
        # And it serializes back out.
        self.assertEqual(row.to_dict()["per_ev_sent"]["P0:S3"], 25)

    def test_per_ev_sent_absent_for_non_ev_policies(self):
        # Non-EV senders (round_robin, hash5tuple, …) don't emit the
        # field; report must default to an empty dict, never raise.
        rep = ScenarioReport.from_records("x", [_sender()], [])
        self.assertEqual(rep.flows[0].per_ev_sent, {})


class TestEvColumnAscii(unittest.TestCase):
    """The `evs` column and the 'unused EVs' note in render_ascii."""

    def _ev_sender(self, **kw):
        per_ev = {f"P{p}:S{s}": 50 for p in range(4) for s in range(8)}
        # Simulate (1, 6) demoted to assumed_bad: zero packets sent on it.
        per_ev.pop("P1:S6")
        return _sender(
            policy="health_aware_mrc",
            per_ev_sent=per_ev,
            **kw,
        )

    def test_evs_column_shows_used_over_expected(self):
        rep = ScenarioReport.from_records(
            "ev-scen", [self._ev_sender()], [],
            topology_dims=(4, 8),
        )
        out = rep.render_ascii()
        self.assertIn("31/32", out)
        self.assertIn("unused EVs: ['p1-spine06']", out)

    def test_evs_column_dash_for_non_ev_policy(self):
        rep = ScenarioReport.from_records(
            "ev-scen", [_sender()], [],
            topology_dims=(4, 8),
        )
        out = rep.render_ascii()
        # No "/N" pattern on the round_robin row; the policy column is
        # round_robin and the evs column should be a bare dash.
        rr_line = [
            ln for ln in out.splitlines()
            if "round_robin" in ln and "->" in ln
        ][0]
        # Last column is evs; expect "-" not "<int>/<int>".
        self.assertRegex(rr_line.rstrip(), r"\s-\s*$")

    def test_no_evs_denominator_when_topology_dims_missing(self):
        rep = ScenarioReport.from_records(
            "ev-scen", [self._ev_sender()], [],
        )
        # No topology_dims: render must not crash; show used count alone
        # and skip the "unused EVs:" line.
        out = rep.render_ascii()
        ev_line = [
            ln for ln in out.splitlines()
            if "health_aware_mrc" in ln and "->" in ln
        ][0]
        # Last column is `evs` right-aligned to width 7.
        self.assertTrue(ev_line.rstrip().endswith(" 31"))
        self.assertNotIn("/32", out)
        self.assertNotIn("unused EVs", out)

    def test_no_unused_note_when_all_evs_used(self):
        per_ev = {f"P{p}:S{s}": 50 for p in range(4) for s in range(8)}
        rep = ScenarioReport.from_records(
            "ev-scen",
            [_sender(policy="ev_spray", per_ev_sent=per_ev)],
            [],
            topology_dims=(4, 8),
        )
        out = rep.render_ascii()
        self.assertIn("32/32", out)
        self.assertNotIn("unused EVs", out)

    def test_mrc_demoted_ev_excluded_even_when_leaked_packets(self):
        # Reproduces the lab artifact: a port is shut mid-run; the MRC
        # state machine demotes the EV to assumed_bad after the third
        # probe timeout, but a packet or two leaked through before then.
        # `per_ev_sent` still has the EV as a non-zero key, so the old
        # `len(per_ev_sent)` logic would report 32/32 — wrong. The new
        # logic prefers the MRC snapshot's weight==0 signal.
        per_ev = {f"P{p}:S{s}": 50 for p in range(4) for s in range(8)}
        per_ev["P1:S0"] = 1  # one packet leaked before demotion
        # Mock MRC snapshot: P1:S0 demoted to assumed_bad (weight 0.0),
        # everything else has positive weight.
        tenants = {
            "green": [
                {
                    "plane": p, "path": s,
                    "state": "assumed_bad" if (p, s) == (1, 0) else "good",
                    "weight": 0.0 if (p, s) == (1, 0) else 1.0 / 31,
                }
                for p in range(4) for s in range(8)
            ],
        }
        sender = _sender(
            policy="health_aware_mrc",
            per_ev_sent=per_ev,
            mrc={"ev_state": {"tenants": tenants}},
        )
        rep = ScenarioReport.from_records(
            "ev-scen", [sender], [],
            topology_dims=(4, 8),
        )
        out = rep.render_ascii()
        self.assertIn("31/32", out)
        self.assertIn("unused EVs: ['p1-spine00']", out)

    def test_mrc_snapshot_overrides_per_ev_sent_for_active_count(self):
        # Even if `per_ev_sent` has all 32 keys with healthy counts,
        # an MRC snapshot saying one EV is demoted (weight=0) wins.
        per_ev = {f"P{p}:S{s}": 50 for p in range(4) for s in range(8)}
        tenants = {
            "green": [
                {
                    "plane": p, "path": s,
                    "state": "assumed_bad" if (p, s) == (2, 5) else "good",
                    "weight": 0.0 if (p, s) == (2, 5) else 1.0 / 31,
                }
                for p in range(4) for s in range(8)
            ],
        }
        sender = _sender(
            policy="health_aware_mrc",
            per_ev_sent=per_ev,
            mrc={"ev_state": {"tenants": tenants}},
        )
        rep = ScenarioReport.from_records(
            "ev-scen", [sender], [],
            topology_dims=(4, 8),
        )
        out = rep.render_ascii()
        self.assertIn("31/32", out)
        self.assertIn("unused EVs: ['p2-spine05']", out)

    def test_malformed_mrc_snapshot_falls_back_to_counts(self):
        # If the mrc dict is missing or malformed, we must not crash and
        # we must fall back to per_ev_sent-based counting.
        per_ev = {f"P{p}:S{s}": 50 for p in range(4) for s in range(8)}
        per_ev.pop("P1:S6")  # 31 EVs in counts
        for bad_mrc in ({}, {"ev_state": "garbage"},
                        {"ev_state": {"tenants": None}},
                        {"ev_state": {"tenants": {"yellow": []}}}):  # wrong tenant
            sender = _sender(
                policy="health_aware_mrc",
                per_ev_sent=per_ev,
                mrc=bad_mrc,
            )
            rep = ScenarioReport.from_records(
                "ev-scen", [sender], [],
                topology_dims=(4, 8),
            )
            out = rep.render_ascii()
            self.assertIn("31/32", out, f"bad_mrc={bad_mrc!r}")


if __name__ == "__main__":
    unittest.main()
