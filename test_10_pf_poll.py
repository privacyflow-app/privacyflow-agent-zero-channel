"""
Tests for _cleanup_stale_state_mappings() in _10_pf_poll.py.

Covers:
  1. State with all valid mappings (no cleanup)
  2. State with some stale mappings (partial cleanup)
  3. State with all stale mappings (full cleanup)
  4. Empty state (no-op)
  5. Corrupted state.json (handled by _load_state returning {})
"""

import json
import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock


# --- Mock A0 framework imports before importing the module under test ---
import types as _types

_helpers_mock = _types.ModuleType("helpers")
_helpers_mock.plugins = MagicMock()
_helpers_mock.extension = MagicMock()
_helpers_mock.print_style = MagicMock()
_helpers_mock.errors = MagicMock()
_helpers_mock.persist_chat = MagicMock()
_helpers_mock.files = MagicMock()
sys.modules["helpers"] = _helpers_mock
sys.modules["helpers.extension"] = MagicMock()
sys.modules["helpers.print_style"] = MagicMock()
sys.modules["helpers.errors"] = MagicMock()
sys.modules["helpers.plugins"] = MagicMock()
sys.modules["helpers.persist_chat"] = MagicMock()
sys.modules["helpers.files"] = MagicMock()
sys.modules["helpers.message_queue"] = MagicMock()
sys.modules["agent"] = MagicMock()
sys.modules["plugins.privacyflow_channel.helpers.pf_client"] = MagicMock()
try:
    import requests  # noqa: F401
except ImportError:
    sys.modules["requests"] = MagicMock()

sys.path.insert(0, "extensions/python/job_loop")
from _10_pf_poll import _cleanup_stale_state_mappings, _format_message_text  # noqa: E402


class _StubFiles:
    """Minimal stub for helpers.files used by _load_state / _save_state."""

    def __init__(self, state_path: str):
        self._state_path = state_path

    def get_abs_path(self, path: str) -> str:
        return self._state_path if path.endswith("state.json") else path

    def read_file(self, path: str) -> str:
        with open(path, "r") as f:
            return f.read()

    def write_file(self, path: str, content: str) -> None:
        with open(path, "w") as f:
            f.write(content)

    def make_dirs(self, path: str) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)


def _write_state_file(state_path: str, state: dict) -> None:
    with open(state_path, "w") as f:
        json.dump(state, f)


def _read_state_file(state_path: str) -> dict:
    with open(state_path, "r") as f:
        return json.load(f)


class TestCleanupStaleStateMappings(unittest.TestCase):
    """Tests for _cleanup_stale_state_mappings() covering all branches."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="pf_poll_test_")
        self._state_path = os.path.join(self._tmpdir, "state.json")
        self._chat_dir = os.path.join(self._tmpdir, "chats")
        os.makedirs(self._chat_dir, exist_ok=True)

        # Patch _STATE_FILE via the module's globals and helpers.files
        import _10_pf_poll as mod

        self._mod = mod
        self._orig_state_file = mod._STATE_FILE
        mod._STATE_FILE = "state.json"  # value is irrelevant; stub resolves it

        self._stub_files = _StubFiles(self._state_path)
        self._orig_files = getattr(mod, "files", None)
        mod.files = self._stub_files

        # Patch _get_chat_file_path to map ctx_id -> path under chat dir
        def fake_chat_path(ctx_id: str) -> str:
            return os.path.join(self._chat_dir, f"{ctx_id}.json")

        self._orig_get_chat_file_path = sys.modules[
            "helpers.persist_chat"
        ]._get_chat_file_path
        sys.modules["helpers.persist_chat"]._get_chat_file_path = fake_chat_path

    def tearDown(self):
        import shutil

        self._mod._STATE_FILE = self._orig_state_file
        if self._orig_files is not None:
            self._mod.files = self._orig_files
        sys.modules["helpers.persist_chat"]._get_chat_file_path = (
            self._orig_get_chat_file_path
        )
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _write_chat_file(self, ctx_id: str) -> None:
        with open(os.path.join(self._chat_dir, f"{ctx_id}.json"), "w") as f:
            f.write("{}")

    # --- Test cases ---

    def test_all_valid_mappings_no_cleanup(self):
        """All chat dirs exist → no stale keys, state unchanged."""
        self._write_chat_file("ctx-a")
        self._write_chat_file("ctx-b")
        _write_state_file(
            self._state_path,
            {"chats": {"contact1": "ctx-a", "contact2": "ctx-b"}},
        )

        _cleanup_stale_state_mappings()

        state = _read_state_file(self._state_path)
        self.assertEqual(state["chats"], {"contact1": "ctx-a", "contact2": "ctx-b"})

    def test_some_stale_mappings_partial_cleanup(self):
        """Some chat dirs missing → only stale keys removed."""
        self._write_chat_file("ctx-a")
        # ctx-b intentionally not written → missing on disk
        _write_state_file(
            self._state_path,
            {"chats": {"contact1": "ctx-a", "contact2": "ctx-b"}},
        )

        _cleanup_stale_state_mappings()

        state = _read_state_file(self._state_path)
        self.assertEqual(state["chats"], {"contact1": "ctx-a"})

    def test_all_stale_mappings_full_cleanup(self):
        """All chat dirs missing → all keys removed, chats dict empty."""
        _write_state_file(
            self._state_path,
            {"chats": {"contact1": "ctx-x", "contact2": "ctx-y"}},
        )

        _cleanup_stale_state_mappings()

        state = _read_state_file(self._state_path)
        self.assertEqual(state["chats"], {})

    def test_empty_state_noop(self):
        """Empty chats dict → no-op, no save performed."""
        _write_state_file(self._state_path, {"chats": {}})

        _cleanup_stale_state_mappings()

        state = _read_state_file(self._state_path)
        self.assertEqual(state["chats"], {})

    def test_missing_chats_key_noop(self):
        """state.json with no 'chats' key → no-op."""
        _write_state_file(self._state_path, {"other": "data"})

        _cleanup_stale_state_mappings()

        state = _read_state_file(self._state_path)
        self.assertEqual(state, {"other": "data"})


class TestFormatMessageText(unittest.TestCase):
    """Tests for _format_message_text() group/direct formatting."""

    def test_group_message_with_contact_id_prefixes_sender(self):
        result = _format_message_text({
            "content": "hello group",
            "isGroupMessage": True,
            "groupId": "g1",
            "contactId": "sender-1",
        })
        self.assertEqual(result, "[sender-1]: hello group")

    def test_group_message_without_contact_id_keeps_content(self):
        result = _format_message_text({
            "content": "hello group",
            "isGroupMessage": True,
            "groupId": "g1",
        })
        self.assertEqual(result, "hello group")

    def test_direct_message_returns_content(self):
        result = _format_message_text({
            "content": "hi",
            "contactId": "c1",
        })
        self.assertEqual(result, "hi")

    def test_missing_content_returns_empty(self):
        result = _format_message_text({"groupId": "g1"})
        self.assertEqual(result, "")

if __name__ == "__main__":
    unittest.main()

