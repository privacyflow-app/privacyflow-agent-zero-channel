"""
Tests for _get_logs_safe() and _extract_last_response() in _50_pf_reply.py.

Covers all three code paths in _get_logs_safe():
  1. log.get_logs() exists and is callable
  2. log.snapshot() exists and is callable
  3. Fallback to log._lock + list(log.logs)
"""

import sys
import threading
import unittest
from unittest.mock import MagicMock


class MockLogEntry:
    def __init__(self, type: str, content: str | None = None):
        self.type = type
        self.content = content


class MockLog:
    """Simulates AgentContext.log with configurable API surface."""

    def __init__(self, entries: list[MockLogEntry] | None = None):
        self.logs = entries or []
        self._lock = threading.Lock()

    def get_logs(self) -> list[MockLogEntry]:
        return list(self.logs)

    def snapshot(self) -> list[MockLogEntry]:
        return list(self.logs)


class MockAgentContext:
    def __init__(self, log: MockLog):
        self.log = log


# Mock A0 framework imports before importing the module under test
sys.modules["helpers.extension"] = MagicMock()
sys.modules["helpers.print_style"] = MagicMock()
sys.modules["helpers.errors"] = MagicMock()
sys.modules["agent"] = MagicMock()
sys.modules["plugins.privacyflow_channel.helpers.pf_client"] = MagicMock()
sys.modules["plugins.privacyflow_channel.helpers.message_splitter"] = MagicMock()

sys.path.insert(0, "extensions/python/process_chain_end")
from _50_pf_reply import _get_logs_safe, _extract_last_response


class TestGetLogsSafe(unittest.TestCase):
    """Tests for _get_logs_safe() covering all three code paths."""

    def setUp(self):
        self._had_get_logs = hasattr(MockLog, "get_logs")
        self._had_snapshot = hasattr(MockLog, "snapshot")

    def tearDown(self):
        if self._had_get_logs and not hasattr(MockLog, "get_logs"):
            MockLog.get_logs = lambda self: list(self.logs)
        if self._had_snapshot and not hasattr(MockLog, "snapshot"):
            MockLog.snapshot = lambda self: list(self.logs)

    def test_get_logs_path(self):
        """Branch 1: log.get_logs() exists and is callable."""
        entries = [MockLogEntry("response", "hello")]
        log = MockLog(entries)
        ctx = MockAgentContext(log)
        result = _get_logs_safe(ctx)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].content, "hello")

    def test_snapshot_path(self):
        """Branch 2: log.snapshot() exists, get_logs() does not."""
        delattr(MockLog, "get_logs")
        entries = [MockLogEntry("response", "world")]
        log = MockLog(entries)
        ctx = MockAgentContext(log)
        result = _get_logs_safe(ctx)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].content, "world")

    def test_lock_fallback_path(self):
        """Branch 3: neither get_logs() nor snapshot() — falls back to _lock."""
        delattr(MockLog, "get_logs")
        delattr(MockLog, "snapshot")
        entries = [MockLogEntry("response", "fallback")]
        log = MockLog(entries)
        ctx = MockAgentContext(log)
        result = _get_logs_safe(ctx)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].content, "fallback")

    def test_empty_logs(self):
        """All branches return empty list when no entries exist."""
        log = MockLog([])
        ctx = MockAgentContext(log)
        result = _get_logs_safe(ctx)
        self.assertEqual(result, [])


class TestExtractLastResponse(unittest.TestCase):
    """Tests for _extract_last_response()."""

    def test_extracts_last_response(self):
        entries = [
            MockLogEntry("user", "ping"),
            MockLogEntry("response", "pong"),
        ]
        log = MockLog(entries)
        ctx = MockAgentContext(log)
        result = _extract_last_response(ctx)
        self.assertEqual(result, "pong")

    def test_skips_non_response_entries(self):
        entries = [
            MockLogEntry("response", "first"),
            MockLogEntry("tool", "intermediate"),
            MockLogEntry("response", "last"),
        ]
        log = MockLog(entries)
        ctx = MockAgentContext(log)
        result = _extract_last_response(ctx)
        self.assertEqual(result, "last")

    def test_no_response_entries(self):
        entries = [
            MockLogEntry("user", "hello"),
            MockLogEntry("tool", "action"),
        ]
        log = MockLog(entries)
        ctx = MockAgentContext(log)
        result = _extract_last_response(ctx)
        self.assertEqual(result, "")

    def test_empty_logs(self):
        log = MockLog([])
        ctx = MockAgentContext(log)
        result = _extract_last_response(ctx)
        self.assertEqual(result, "")

    def test_response_with_none_content(self):
        entries = [MockLogEntry("response", None)]
        log = MockLog(entries)
        ctx = MockAgentContext(log)
        result = _extract_last_response(ctx)
        self.assertEqual(result, "")


if __name__ == "__main__":
    unittest.main()
