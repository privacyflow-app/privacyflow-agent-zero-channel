"""
Tests for hooks.py — save_plugin_config reloads plugin extensions on save.

Covers:
  1. save_plugin_config returns the settings dict unchanged (so A0 persists it).
  2. save_plugin_config calls helpers.plugins.after_plugin_change(["privacyflow_channel"]).
  3. save_plugin_config returns settings even if after_plugin_change raises (resilience).
  4. save_plugin_config handles a no-arg call without crashing.
"""

import importlib
import sys
import types
import unittest
from unittest.mock import MagicMock


def _load_hooks():
    """Import hooks.py fresh with stubbed A0 core helpers in sys.modules.

    Stubs helpers.plugins (with an after_plugin_change spy) and
    helpers.print_style so the plugin can be imported outside the A0 core.
    Returns (hooks_module, fake_plugins_module) for assertions.
    """
    fake_plugins = types.ModuleType("helpers.plugins")
    fake_plugins.after_plugin_change = MagicMock()

    fake_print_style = types.ModuleType("helpers.print_style")
    fake_print_style.PrintStyle = MagicMock()

    # Ensure a 'helpers' package namespace exists and keep its 'plugins'
    # attribute in sync with the stub so `from helpers import plugins`
    # binds to the current stub (not a stale one from a prior test).
    if "helpers" not in sys.modules:
        sys.modules["helpers"] = types.ModuleType("helpers")
    sys.modules["helpers"].plugins = fake_plugins
    sys.modules["helpers"].print_style = fake_print_style
    sys.modules["helpers.plugins"] = fake_plugins
    sys.modules["helpers.print_style"] = fake_print_style

    # Drop any cached hooks import so we get a fresh module bound to the
    # current stubs.
    sys.modules.pop("hooks", None)
    return importlib.import_module("hooks"), fake_plugins


class SavePluginConfigTests(unittest.TestCase):

    def test_returns_settings_unchanged(self):
        hooks, _ = _load_hooks()
        settings = {"pf_api_base": "api.privacyflow.app", "pf_api_key": "k", "pf_app_id": "a"}
        result = hooks.save_plugin_config(settings=settings)
        self.assertIs(result, settings)

    def test_calls_after_plugin_change(self):
        hooks, fake_plugins = _load_hooks()
        settings = {"pf_api_base": "api.privacyflow.app"}
        hooks.save_plugin_config(settings=settings)
        fake_plugins.after_plugin_change.assert_called_once_with(["privacyflow_channel"])

    def test_returns_settings_when_reload_raises(self):
        hooks, fake_plugins = _load_hooks()
        fake_plugins.after_plugin_change.side_effect = RuntimeError("boom")
        settings = {"pf_api_base": "api.privacyflow.app"}
        result = hooks.save_plugin_config(settings=settings)
        # Must still return settings so A0 persists the config despite the reload failure.
        self.assertEqual(result, settings)

    def test_accepts_no_args(self):
        # A0 may call with only default; hook should not crash.
        hooks, fake_plugins = _load_hooks()
        result = hooks.save_plugin_config()
        self.assertIsNone(result)
        fake_plugins.after_plugin_change.assert_called_once_with(["privacyflow_channel"])


if __name__ == "__main__":
    unittest.main()
