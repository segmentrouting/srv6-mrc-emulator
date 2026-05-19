"""Tests for srv6_mrc.mrc.agent.load_configs_from_env.

This is the bridge between the scenario YAML's `mrc:` block (validated
into MrcSpec, serialised by MrcSpec.to_env_json, propagated through
docker-exec -e SRV6_MRC_CONFIG_JSON=...) and the AgentConfig +
EVStateConfig dataclasses the running spray.py constructs.

The loader is deliberately strict: unknown keys, bad JSON, and non-object
payloads all raise ValueError. The scenario validator already catches the
common cases, but a typo in a future schema field — or someone setting
the env var by hand — should fail loud in the container rather than
silently revert to defaults.
"""

import json
import os
import unittest
from unittest import mock

from srv6_mrc.mrc.agent import (
    MRC_CONFIG_ENV, AgentConfig, load_configs_from_env,
)


class TestLoadConfigsDefaults(unittest.TestCase):
    """When the env var is unset or empty, both configs come back at
    defaults (and EVStateConfig is None so callers can pass cfg=None
    to EVStateTable())."""

    def test_no_env_returns_defaults(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(MRC_CONFIG_ENV, None)
            agent_cfg, ev_cfg = load_configs_from_env()
        self.assertEqual(agent_cfg, AgentConfig())
        self.assertIsNone(ev_cfg)

    def test_empty_string_returns_defaults(self):
        # Empty string is "falsy" — treated the same as unset.
        agent_cfg, ev_cfg = load_configs_from_env("")
        self.assertEqual(agent_cfg, AgentConfig())
        self.assertIsNone(ev_cfg)

    def test_empty_object_returns_defaults(self):
        # `{}` is a valid object with no overrides; same effect as unset
        # but exercises the JSON parse path.
        agent_cfg, ev_cfg = load_configs_from_env("{}")
        self.assertEqual(agent_cfg, AgentConfig())
        self.assertIsNone(ev_cfg)


class TestLoadConfigsOverrides(unittest.TestCase):

    def test_agent_only_override(self):
        agent_cfg, ev_cfg = load_configs_from_env(
            json.dumps({"probe_interval_ms": 50})
        )
        self.assertEqual(agent_cfg.probe_interval_ms, 50)
        # Other agent fields stay at defaults.
        self.assertEqual(agent_cfg.probe_timeout_ms,
                         AgentConfig().probe_timeout_ms)
        # No ev-state fields overridden → None.
        self.assertIsNone(ev_cfg)

    def test_ev_state_only_override(self):
        agent_cfg, ev_cfg = load_configs_from_env(
            json.dumps({"loss_threshold": 0.02})
        )
        self.assertEqual(agent_cfg, AgentConfig())
        self.assertIsNotNone(ev_cfg)
        self.assertEqual(ev_cfg.loss_threshold, 0.02)

    def test_mixed_override(self):
        # Most common real shape: a couple of agent-side knobs and a
        # couple of ev-state knobs in the same blob.
        agent_cfg, ev_cfg = load_configs_from_env(json.dumps({
            "probe_interval_ms": 50,
            "loss_window_ms": 100,
            "loss_threshold": 0.02,
            "loss_demote_consecutive": 2,
        }))
        self.assertEqual(agent_cfg.probe_interval_ms, 50)
        self.assertEqual(agent_cfg.loss_window_ms, 100)
        self.assertIsNotNone(ev_cfg)
        self.assertEqual(ev_cfg.loss_threshold, 0.02)
        self.assertEqual(ev_cfg.loss_demote_consecutive, 2)

    def test_reads_from_environ_when_no_arg(self):
        # Round-trip through the actual env var to make sure the
        # default-arg path agrees with the explicit-arg path.
        payload = json.dumps({"probe_interval_ms": 75})
        with mock.patch.dict(os.environ, {MRC_CONFIG_ENV: payload}):
            agent_cfg, _ = load_configs_from_env()
        self.assertEqual(agent_cfg.probe_interval_ms, 75)


class TestLoadConfigsRejects(unittest.TestCase):
    """The loader is the last line of defence between a malformed
    scenario blob and a long lab run that silently uses the wrong
    knobs. Every error path here is "raise ValueError, fail loud"."""

    def test_invalid_json(self):
        with self.assertRaises(ValueError) as cm:
            load_configs_from_env("{not json")
        self.assertIn("not valid JSON", str(cm.exception))

    def test_non_object_payload(self):
        # A bare list, number, or string is valid JSON but not the
        # shape we expect. The error message names the offending type
        # so the operator can grep for it in the container logs.
        for bad in ("[1, 2, 3]", "42", '"hello"', "null"):
            with self.subTest(payload=bad):
                with self.assertRaises(ValueError) as cm:
                    load_configs_from_env(bad)
                self.assertIn("JSON object", str(cm.exception))

    def test_unknown_key(self):
        with self.assertRaises(ValueError) as cm:
            load_configs_from_env(json.dumps({"made_up": 1}))
        msg = str(cm.exception)
        self.assertIn("unknown keys", msg)
        self.assertIn("made_up", msg)

    def test_unknown_and_known_keys_mixed(self):
        # Even one bad key in an otherwise-valid blob is a hard fail —
        # we don't want a typoed field to silently take its default.
        with self.assertRaises(ValueError):
            load_configs_from_env(json.dumps({
                "probe_interval_ms": 50,
                "made_up_knob": 1,
            }))

    def test_invalid_agent_field_value(self):
        # AgentConfig.__post_init__ rejects non-positive ints; the
        # loader surfaces that as the underlying ValueError.
        with self.assertRaises(ValueError):
            load_configs_from_env(json.dumps({"probe_interval_ms": -1}))


if __name__ == "__main__":
    unittest.main()
