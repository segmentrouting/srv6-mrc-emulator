import unittest

from srv6_mrc.mrc import scenario


# --- minimal valid scenario fixture ----------------------------------------

MINIMAL = {
    "name": "smoke",
    "flows": [
        {
            "pairs": "green-pairs-8",
            "policy": "round_robin",
            "rate": "1000pps",
            "duration": "5s",
        }
    ],
}


class TestMinimalValidation(unittest.TestCase):
    def test_smoke_validates(self):
        s = scenario.validate(MINIMAL)
        self.assertEqual(s.name, "smoke")
        self.assertEqual(s.description, "")
        self.assertEqual(len(s.flows), 1)
        self.assertEqual(len(s.flows[0].pairs), 8)
        self.assertEqual(s.flows[0].rate_pps, 1000)
        self.assertEqual(s.flows[0].duration_s, 5.0)
        self.assertEqual(s.flows[0].policy_label, "round_robin")
        self.assertEqual(s.faults, ())
        self.assertIsNone(s.report.out)


class TestRequiredKeys(unittest.TestCase):
    def test_missing_name(self):
        doc = {**MINIMAL}
        del doc["name"]
        with self.assertRaises(scenario.ScenarioError) as cm:
            scenario.validate(doc)
        self.assertIn("$", cm.exception.path)

    def test_missing_flows(self):
        doc = {"name": "x"}
        with self.assertRaises(scenario.ScenarioError):
            scenario.validate(doc)

    def test_unknown_top_level_key(self):
        doc = {**MINIMAL, "paris": []}  # the classic typo
        with self.assertRaises(scenario.ScenarioError) as cm:
            scenario.validate(doc)
        self.assertIn("paris", str(cm.exception))

    def test_top_level_must_be_mapping(self):
        with self.assertRaises(scenario.ScenarioError):
            scenario.validate([])
        with self.assertRaises(scenario.ScenarioError):
            scenario.validate("hello")


# --- pairs -----------------------------------------------------------------

class TestPairs(unittest.TestCase):
    def test_named_set_resolves(self):
        s = scenario.validate(MINIMAL)
        flow = s.flows[0]
        # First pair should be (green, 0, 15) — known reference table
        self.assertEqual(flow.pairs[0].tenant, "green")
        self.assertEqual(flow.pairs[0].src, 0)
        self.assertEqual(flow.pairs[0].dst, 15)

    def test_unknown_named_set(self):
        doc = {**MINIMAL,
               "flows": [{**MINIMAL["flows"][0], "pairs": "purple-pairs-99"}]}
        with self.assertRaises(scenario.ScenarioError) as cm:
            scenario.validate(doc)
        self.assertIn("pairs", cm.exception.path)

    def test_explicit_pair_list(self):
        doc = {
            **MINIMAL,
            "flows": [{
                **MINIMAL["flows"][0],
                "pairs": [{"tenant": "green", "src": 0, "dst": 15}],
            }],
        }
        s = scenario.validate(doc)
        self.assertEqual(len(s.flows[0].pairs), 1)
        self.assertEqual(s.flows[0].pairs[0].dst_host(), "green-host15")

    def test_empty_pair_list(self):
        doc = {**MINIMAL,
               "flows": [{**MINIMAL["flows"][0], "pairs": []}]}
        with self.assertRaises(scenario.ScenarioError):
            scenario.validate(doc)

    def test_src_equals_dst_rejected(self):
        doc = {**MINIMAL,
               "flows": [{**MINIMAL["flows"][0],
                          "pairs": [{"tenant": "green", "src": 5, "dst": 5}]}]}
        with self.assertRaises(scenario.ScenarioError):
            scenario.validate(doc)

    def test_bad_tenant(self):
        doc = {**MINIMAL,
               "flows": [{**MINIMAL["flows"][0],
                          "pairs": [{"tenant": "blue", "src": 0, "dst": 1}]}]}
        with self.assertRaises(scenario.ScenarioError):
            scenario.validate(doc)

    def test_bad_host_id(self):
        doc = {**MINIMAL,
               "flows": [{**MINIMAL["flows"][0],
                          "pairs": [{"tenant": "green", "src": 0, "dst": 16}]}]}
        with self.assertRaises(scenario.ScenarioError):
            scenario.validate(doc)


# --- policy ----------------------------------------------------------------

class TestPolicy(unittest.TestCase):
    def test_hash5tuple(self):
        doc = {**MINIMAL,
               "flows": [{**MINIMAL["flows"][0], "policy": "hash5tuple"}]}
        s = scenario.validate(doc)
        self.assertEqual(s.flows[0].policy_label, "hash5tuple")

    def test_weighted(self):
        doc = {**MINIMAL,
               "flows": [{**MINIMAL["flows"][0],
                          "policy": {"weighted": [1, 1, 1, 1]}}]}
        s = scenario.validate(doc)
        self.assertTrue(s.flows[0].policy_label.startswith("weighted"))

    def test_health_aware_rejected(self):
        # Legacy `health_aware` wrapper was removed; scenarios using it
        # should fail validation rather than silently fall back.
        doc = {**MINIMAL,
               "flows": [{**MINIMAL["flows"][0],
                          "policy": {"health_aware": "round_robin"}}]}
        with self.assertRaises(scenario.ScenarioError):
            scenario.validate(doc)

    def test_unknown_policy(self):
        doc = {**MINIMAL,
               "flows": [{**MINIMAL["flows"][0], "policy": "magic"}]}
        with self.assertRaises(scenario.ScenarioError):
            scenario.validate(doc)


# --- rate / duration -------------------------------------------------------

class TestRateDuration(unittest.TestCase):
    def test_rate_forms(self):
        for v in (1000, "1000", "1000pps", "1000 pps"):
            with self.subTest(rate=v):
                doc = {**MINIMAL,
                       "flows": [{**MINIMAL["flows"][0], "rate": v}]}
                s = scenario.validate(doc)
                self.assertEqual(s.flows[0].rate_pps, 1000)

    def test_bad_rate(self):
        for v in (0, -5, "fast", "1000pppppps", None):
            with self.subTest(rate=v):
                doc = {**MINIMAL,
                       "flows": [{**MINIMAL["flows"][0], "rate": v}]}
                with self.assertRaises(scenario.ScenarioError):
                    scenario.validate(doc)

    def test_duration_forms(self):
        cases = {"5s": 5.0, "500ms": 0.5, "1.5s": 1.5, 10: 10.0, 0.25: 0.25}
        for raw, expected in cases.items():
            with self.subTest(duration=raw):
                doc = {**MINIMAL,
                       "flows": [{**MINIMAL["flows"][0], "duration": raw}]}
                s = scenario.validate(doc)
                self.assertEqual(s.flows[0].duration_s, expected)

    def test_bad_duration(self):
        for v in (0, -1, "5x", "forever", None):
            with self.subTest(duration=v):
                doc = {**MINIMAL,
                       "flows": [{**MINIMAL["flows"][0], "duration": v}]}
                with self.assertRaises(scenario.ScenarioError):
                    scenario.validate(doc)


# --- faults ----------------------------------------------------------------

class TestFaults(unittest.TestCase):
    def test_valid_fault(self):
        doc = {
            **MINIMAL,
            "faults": [
                {"kind": "netem", "target": "plane 2", "spec": "loss 5%"},
            ],
        }
        s = scenario.validate(doc)
        self.assertEqual(len(s.faults), 1)
        self.assertEqual(s.faults[0].target, "plane 2")
        self.assertEqual(s.faults[0].spec, "loss 5%")

    def test_blackhole_spec(self):
        doc = {**MINIMAL,
               "faults": [{"kind": "netem", "target": "plane 0",
                           "spec": "blackhole"}]}
        s = scenario.validate(doc)
        self.assertEqual(s.faults[0].spec, "blackhole")

    def test_bad_kind(self):
        doc = {**MINIMAL,
               "faults": [{"kind": "bgp-shutdown", "target": "plane 0",
                           "spec": "loss 5%"}]}
        with self.assertRaises(scenario.ScenarioError):
            scenario.validate(doc)

    def test_bad_target_caught_early(self):
        doc = {**MINIMAL,
               "faults": [{"kind": "netem", "target": "plane 99",
                           "spec": "loss 5%"}]}
        with self.assertRaises(scenario.ScenarioError):
            scenario.validate(doc)

    def test_bad_spec_caught_early(self):
        doc = {**MINIMAL,
               "faults": [{"kind": "netem", "target": "plane 0",
                           "spec": "rm -rf /"}]}
        with self.assertRaises(scenario.ScenarioError):
            scenario.validate(doc)

    def test_unknown_key_in_fault(self):
        doc = {**MINIMAL,
               "faults": [{"kind": "netem", "target": "plane 0",
                           "spec": "loss 1%", "duration": "30s"}]}
        with self.assertRaises(scenario.ScenarioError):
            scenario.validate(doc)


# --- report ----------------------------------------------------------------

class TestReport(unittest.TestCase):
    def test_default_when_omitted(self):
        s = scenario.validate(MINIMAL)
        self.assertIsNone(s.report.out)

    def test_explicit_out(self):
        doc = {**MINIMAL, "report": {"out": "results/x.json"}}
        s = scenario.validate(doc)
        self.assertEqual(s.report.out, "results/x.json")

    def test_unknown_key(self):
        doc = {**MINIMAL, "report": {"format": "json"}}
        with self.assertRaises(scenario.ScenarioError):
            scenario.validate(doc)


class TestMrc(unittest.TestCase):
    """Optional top-level `mrc:` block that enables MRC for the scenario.

    Absence = MRC disabled (Scenario.mrc is None). Presence = enabled
    with all subkeys optional. The orchestrator (mrc/run.py) reads
    Scenario.mrc and translates it into --mrc + SRV6_MRC_CONFIG_JSON env
    on the docker exec invocations.
    """

    def test_absent_means_disabled(self):
        s = scenario.validate(MINIMAL)
        self.assertIsNone(s.mrc)

    def test_empty_block_is_enabled_with_defaults(self):
        doc = {**MINIMAL, "mrc": {}}
        s = scenario.validate(doc)
        self.assertIsNotNone(s.mrc)
        self.assertIsNone(s.mrc.probe_interval_ms)
        self.assertEqual(s.mrc.to_env_json(), "{}")

    def test_null_block_treated_as_empty(self):
        # `mrc:` with no value in YAML loads as None; we treat that
        # as "enable with defaults" rather than raising.
        doc = {**MINIMAL, "mrc": None}
        s = scenario.validate(doc)
        self.assertIsNotNone(s.mrc)

    def test_full_block_overrides(self):
        doc = {**MINIMAL, "mrc": {
            "probe_interval_ms": 50,
            "probe_timeout_ms": 25,
            "loss_window_ms": 100,
            "max_window_skew_ms": 200,
            "probe_fail_threshold": 2,
            "probe_recover_threshold": 4,
            "loss_threshold": 0.02,
            "loss_demote_consecutive": 3,
            "min_active_evs": 1,
            "rtt_ring_size": 64,
        }}
        s = scenario.validate(doc)
        self.assertEqual(s.mrc.probe_interval_ms, 50)
        self.assertEqual(s.mrc.loss_threshold, 0.02)
        # to_env_json round-trips through json
        import json
        env = json.loads(s.mrc.to_env_json())
        self.assertEqual(env["probe_interval_ms"], 50)
        self.assertEqual(env["loss_threshold"], 0.02)
        # All 10 fields present
        self.assertEqual(len(env), 10)

    def test_unknown_subkey_rejected(self):
        doc = {**MINIMAL, "mrc": {"made_up_knob": 1}}
        with self.assertRaises(scenario.ScenarioError):
            scenario.validate(doc)

    def test_negative_int_rejected(self):
        doc = {**MINIMAL, "mrc": {"probe_interval_ms": -1}}
        with self.assertRaises(scenario.ScenarioError):
            scenario.validate(doc)

    def test_zero_int_rejected(self):
        # All positive-int fields use the same validator; 0 is rejected.
        doc = {**MINIMAL, "mrc": {"loss_window_ms": 0}}
        with self.assertRaises(scenario.ScenarioError):
            scenario.validate(doc)

    def test_bool_int_rejected(self):
        # Python bool is a subclass of int; the validator must reject it
        # so `probe_interval_ms: true` doesn't silently land as 1.
        doc = {**MINIMAL, "mrc": {"probe_interval_ms": True}}
        with self.assertRaises(scenario.ScenarioError):
            scenario.validate(doc)

    def test_loss_threshold_in_range(self):
        for good in (0.0, 0.05, 1.0):
            doc = {**MINIMAL, "mrc": {"loss_threshold": good}}
            s = scenario.validate(doc)
            self.assertEqual(s.mrc.loss_threshold, good)
        for bad in (-0.01, 1.01, "0.5"):
            doc = {**MINIMAL, "mrc": {"loss_threshold": bad}}
            with self.assertRaises(scenario.ScenarioError):
                scenario.validate(doc)

    def test_loss_threshold_int_accepted(self):
        # YAML may load 0 and 1 as int; coerce to float.
        doc = {**MINIMAL, "mrc": {"loss_threshold": 0}}
        s = scenario.validate(doc)
        self.assertEqual(s.mrc.loss_threshold, 0.0)
        self.assertIsInstance(s.mrc.loss_threshold, float)

    def test_to_env_json_only_emits_set_fields(self):
        doc = {**MINIMAL, "mrc": {"probe_interval_ms": 50}}
        s = scenario.validate(doc)
        self.assertEqual(s.mrc.to_env_json(), '{"probe_interval_ms": 50}')

    def test_mrc_must_be_mapping(self):
        doc = {**MINIMAL, "mrc": [1, 2, 3]}
        with self.assertRaises(scenario.ScenarioError):
            scenario.validate(doc)


# --- yaml roundtrip --------------------------------------------------------

class TestYamlLoading(unittest.TestCase):
    def test_from_yaml_string(self):
        try:
            import yaml  # noqa: F401
        except ImportError:
            self.skipTest("PyYAML not available")
        text = """
name: smoke
description: basic
flows:
  - pairs: green-pairs-8
    policy: round_robin
    rate: 1000pps
    duration: 5s
faults:
  - kind: netem
    target: plane 2
    spec: loss 5%
report:
  out: results/x.json
"""
        s = scenario.from_yaml_string(text)
        self.assertEqual(s.name, "smoke")
        self.assertEqual(s.description, "basic")
        self.assertEqual(len(s.faults), 1)


class TestPathsPerPlane(unittest.TestCase):
    """Top-level scenario `paths_per_plane: N` override for EV-spray."""

    _BASE = """
name: x
flows:
  - pairs: green-pairs-8
    policy: ev_spray
    rate: 1000pps
    duration: 5s
"""

    def test_absent_defaults_to_none(self):
        s = scenario.from_yaml_string(self._BASE)
        self.assertIsNone(s.paths_per_plane)

    def test_explicit_value_accepted(self):
        from srv6_mrc.topo import NUM_SPINES
        text = self._BASE + f"paths_per_plane: {NUM_SPINES}\n"
        s = scenario.from_yaml_string(text)
        self.assertEqual(s.paths_per_plane, NUM_SPINES)

    def test_one_accepted(self):
        text = self._BASE + "paths_per_plane: 1\n"
        s = scenario.from_yaml_string(text)
        self.assertEqual(s.paths_per_plane, 1)

    def test_zero_rejected(self):
        text = self._BASE + "paths_per_plane: 0\n"
        with self.assertRaises(scenario.ScenarioError):
            scenario.from_yaml_string(text)

    def test_over_max_rejected(self):
        from srv6_mrc.topo import NUM_SPINES
        text = self._BASE + f"paths_per_plane: {NUM_SPINES + 1}\n"
        with self.assertRaises(scenario.ScenarioError):
            scenario.from_yaml_string(text)

    def test_bool_rejected(self):
        text = self._BASE + "paths_per_plane: true\n"
        with self.assertRaises(scenario.ScenarioError):
            scenario.from_yaml_string(text)

    def test_string_rejected(self):
        text = self._BASE + "paths_per_plane: 'four'\n"
        with self.assertRaises(scenario.ScenarioError):
            scenario.from_yaml_string(text)


# --- public CLI helpers ----------------------------------------------------

class TestParseDurationStr(unittest.TestCase):
    """`scenario.parse_duration_str` is the CLI's source of truth for
    `--duration`. Mirrors the YAML-side `_parse_duration` but raises
    `ValueError` (argparse-friendly), not `ScenarioError`."""

    def test_seconds_forms(self):
        for raw, expected in (("5s", 5.0), ("30s", 30.0), ("1.5s", 1.5),
                              ("5", 5.0)):
            with self.subTest(raw=raw):
                self.assertEqual(scenario.parse_duration_str(raw), expected)

    def test_millis_form(self):
        self.assertAlmostEqual(scenario.parse_duration_str("500ms"), 0.5)

    def test_rejects_zero_and_negative(self):
        for raw in ("0s", "-1s", "0"):
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError):
                    scenario.parse_duration_str(raw)

    def test_rejects_garbage(self):
        for raw in ("forever", "5x", "", "   ", None):
            with self.subTest(raw=raw):
                with self.assertRaises((ValueError, TypeError)):
                    scenario.parse_duration_str(raw)  # type: ignore[arg-type]


class TestOverrideDuration(unittest.TestCase):
    """`scenario.override_duration(scenario, dur)` returns a copy with
    every flow's duration rewritten. Frozen-dataclass-safe; original
    is untouched."""

    def _two_flow_scenario(self):
        doc = {
            **MINIMAL,
            "flows": [
                {**MINIMAL["flows"][0], "duration": "30s"},
                {**MINIMAL["flows"][0], "duration": "500ms"},
            ],
        }
        return scenario.validate(doc)

    def test_overrides_all_flows(self):
        s = self._two_flow_scenario()
        s2 = scenario.override_duration(s, 5.0)
        self.assertEqual([f.duration_s for f in s2.flows], [5.0, 5.0])

    def test_does_not_mutate_original(self):
        s = self._two_flow_scenario()
        _ = scenario.override_duration(s, 5.0)
        # Original frozen scenario unchanged.
        self.assertEqual([f.duration_s for f in s.flows], [30.0, 0.5])

    def test_preserves_other_fields(self):
        s = self._two_flow_scenario()
        s2 = scenario.override_duration(s, 7.5)
        self.assertEqual(s2.name, s.name)
        for orig, new in zip(s.flows, s2.flows):
            self.assertEqual(orig.pairs, new.pairs)
            self.assertEqual(orig.policy_spec, new.policy_spec)
            self.assertEqual(orig.rate_pps, new.rate_pps)


if __name__ == "__main__":
    unittest.main()
