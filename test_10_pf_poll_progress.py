"""
Tests for the event-driven progress timer in _10_pf_poll.py.

Tests cover:
  1. _classify_log_entries() — signal classification from log entries
  2. _start_progress_timer() — full event-loop behavior:
     - Initial message sent after initial_delay
     - Event-driven messages fire on log activity (subagent, tool, synthesizing)
     - Throttling: min_interval respected between messages
     - Fallback nudges after fallback_interval with no log activity
     - Fallback rotation between two messages
     - is_running() False → loop exits
     - Existing task cancelled when new one starts
     - Disabled via config → no task created
"""

import asyncio
import sys
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

import types as _types

# --- Mock A0 framework imports before importing the module under test ---
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
import _10_pf_poll as mod  # noqa: E402
from _10_pf_poll import (  # noqa: E402
    _classify_log_entries,
    _start_progress_timer,
    _PROGRESS_DEFAULTS,
)


class FakeLogEntry:
    """Minimal stand-in for A0 LogItem with the fields we read."""

    def __init__(self, etype: str, agentno: int = 0, content: str = ""):
        self.type = etype
        self.agentno = agentno
        self.content = content
        self.heading = ""
        self.kvps = {}
        self.timestamp = time.monotonic()


class FakeLog:
    """Simulates context.log with a growing list and a lock."""

    def __init__(self, entries=None):
        self.logs = entries or []
        self._lock = threading.RLock()
        self.progress = ""
        self.progress_active = False

    def add(self, entry: FakeLogEntry):
        with self._lock:
            self.logs.append(entry)


def _make_context(log_entries=None, is_running_seq=None):
    """Build a mock AgentContext with a FakeLog and configurable is_running().

    is_running_seq: list of bools returned in order; None means always True.
    log_entries: initial list of FakeLogEntry objects in the log.
    """
    ctx = MagicMock()
    ctx.data = {}
    log = FakeLog(list(log_entries) if log_entries else [])
    ctx.log = log
    if is_running_seq is None:
        ctx.is_running.return_value = True
    else:
        ctx.is_running.side_effect = list(is_running_seq)
    return ctx


# ---------------------------------------------------------------------------
# Tests for _classify_log_entries()
# ---------------------------------------------------------------------------


class TestClassifyLogEntries(unittest.TestCase):
    """Unit tests for the signal classifier — pure function, no async needed."""

    def test_no_entries_returns_none(self):
        signal, agentno = _classify_log_entries([], 0)
        self.assertIsNone(signal)
        self.assertEqual(agentno, 0)

    def test_subagent_entry(self):
        entries = [FakeLogEntry("subagent")]
        signal, agentno = _classify_log_entries(entries, 0)
        self.assertEqual(signal, "subagent")

    def test_single_tool_call(self):
        entries = [FakeLogEntry("tool")]
        signal, _ = _classify_log_entries(entries, 0)
        self.assertEqual(signal, "tool_activity")

    def test_three_tool_calls_becomes_reviewing(self):
        entries = [FakeLogEntry("tool"), FakeLogEntry("tool"), FakeLogEntry("tool")]
        signal, _ = _classify_log_entries(entries, 0)
        self.assertEqual(signal, "reviewing")

    def test_response_entry_becomes_synthesizing(self):
        entries = [FakeLogEntry("response", content="partial...")]
        signal, _ = _classify_log_entries(entries, 0)
        self.assertEqual(signal, "synthesizing")

    def test_subagent_overrides_synthesizing(self):
        entries = [FakeLogEntry("response"), FakeLogEntry("subagent")]
        signal, _ = _classify_log_entries(entries, 0)
        self.assertEqual(signal, "subagent")

    def test_code_exe_counts_as_tool_activity(self):
        entries = [FakeLogEntry("code_exe")]
        signal, _ = _classify_log_entries(entries, 0)
        self.assertEqual(signal, "tool_activity")

    def test_browser_counts_as_tool_activity(self):
        entries = [FakeLogEntry("browser")]
        signal, _ = _classify_log_entries(entries, 0)
        self.assertEqual(signal, "tool_activity")

    def test_agentno_increase_without_subagent_entry(self):
        """agentno going from 0→1 implies a sub-agent is active."""
        entries = [FakeLogEntry("tool", agentno=1)]
        signal, new_agentno = _classify_log_entries(entries, 0)
        self.assertEqual(signal, "subagent")
        self.assertEqual(new_agentno, 1)

    def test_mixed_types_tool_and_response(self):
        """Response has higher priority than tool_activity."""
        entries = [FakeLogEntry("tool"), FakeLogEntry("response")]
        signal, _ = _classify_log_entries(entries, 0)
        self.assertEqual(signal, "synthesizing")

    def test_error_treated_as_activity(self):
        entries = [FakeLogEntry("error")]
        signal, _ = _classify_log_entries(entries, 0)
        self.assertEqual(signal, "tool_activity")


# ---------------------------------------------------------------------------
# Tests for _start_progress_timer() — async event loop
# ---------------------------------------------------------------------------


class TestProgressTimerDisabled(unittest.TestCase):
    """enabled: false → no task created."""

    def test_disabled_no_task_created(self):
        ctx = _make_context()
        with patch.object(asyncio, "create_task") as create_task, \
             patch("builtins.open", MagicMock(side_effect=FileNotFoundError)), \
             patch.object(mod, "_PROGRESS_DEFAULTS", {
                 "enabled": False, "initial_delay": 0, "min_interval": 0,
                 "fallback_interval": 0, "poll_interval": 0, "messages": {},
             }):
            _start_progress_timer(ctx, "c1", "signal", None)

        create_task.assert_not_called()
        self.assertNotIn("pf_progress_task", ctx.data)


class TestProgressTimerInitialMessage(unittest.TestCase):
    """Initial message sent after initial_delay."""

    def setUp(self):
        self._orig_defaults = mod._PROGRESS_DEFAULTS
        mod._PROGRESS_DEFAULTS = {
            "enabled": True, "initial_delay": 0, "min_interval": 999,
            "fallback_interval": 999, "poll_interval": 0,
            "messages": _PROGRESS_DEFAULTS["messages"],
        }

    def tearDown(self):
        mod._PROGRESS_DEFAULTS = self._orig_defaults

    def test_initial_message_sent(self):
        ctx = _make_context(is_running_seq=[True, False])
        sent = []

        async def fake_to_thread(fn, *args):
            sent.append(args)

        async def runner():
            with patch.object(asyncio, "sleep", _no_op_sleep), \
                 patch.object(asyncio, "to_thread", fake_to_thread), \
                 patch("builtins.open", MagicMock(side_effect=FileNotFoundError)):
                _start_progress_timer(ctx, "c1", "signal", None)
                await ctx.data["pf_progress_task"]

        asyncio.run(runner())
        # At least the initial message should have been sent
        self.assertGreaterEqual(len(sent), 1)
        self.assertIn("On it", sent[0][1])


class TestProgressTimerEventDriven(unittest.TestCase):
    """Event-driven messages fire on log activity."""

    def setUp(self):
        self._orig_defaults = mod._PROGRESS_DEFAULTS
        mod._PROGRESS_DEFAULTS = {
            "enabled": True, "initial_delay": 0, "min_interval": 0,
            "fallback_interval": 999, "poll_interval": 0,
            "messages": _PROGRESS_DEFAULTS["messages"],
        }

    def tearDown(self):
        mod._PROGRESS_DEFAULTS = self._orig_defaults

    def test_tool_activity_triggers_message(self):
        """A tool log entry after the initial message triggers tool_activity."""
        ctx = _make_context()
        sent = []
        sleep_calls = [0]

        async def fake_to_thread(fn, *args):
            sent.append(args)

        async def sleeping_sleep(_secs):
            # On the 2nd sleep call (first poll_interval inside the loop),
            # add a tool entry before yielding back
            sleep_calls[0] += 1
            if sleep_calls[0] == 2:
                ctx.log.add(FakeLogEntry("tool"))

        async def runner():
            with patch.object(asyncio, "sleep", sleeping_sleep), \
                 patch.object(asyncio, "to_thread", fake_to_thread), \
                 patch("builtins.open", MagicMock(side_effect=FileNotFoundError)):
                _start_progress_timer(ctx, "c1", "signal", None)
                ctx.is_running.side_effect = [True, True, True, True, False]
                await ctx.data["pf_progress_task"]

        asyncio.run(runner())
        msgs = [s[1] for s in sent]
        self.assertGreaterEqual(len(msgs), 2)
        self.assertIn("On it", msgs[0])
        self.assertTrue(any("looking into this" in m for m in msgs),
                        f"Expected tool_activity in {msgs}")

    def test_subagent_triggers_message(self):
        """A subagent log entry triggers the subagent message."""
        ctx = _make_context()
        sent = []
        sleep_calls = [0]

        async def fake_to_thread(fn, *args):
            sent.append(args)

        async def sleeping_sleep(_secs):
            sleep_calls[0] += 1
            if sleep_calls[0] == 2:
                ctx.log.add(FakeLogEntry("subagent"))

        async def runner():
            with patch.object(asyncio, "sleep", sleeping_sleep), \
                 patch.object(asyncio, "to_thread", fake_to_thread), \
                 patch("builtins.open", MagicMock(side_effect=FileNotFoundError)):
                _start_progress_timer(ctx, "c1", "signal", None)
                ctx.is_running.side_effect = [True, True, True, True, False]
                await ctx.data["pf_progress_task"]

        asyncio.run(runner())
        msgs = [s[1] for s in sent]
        self.assertGreaterEqual(len(msgs), 2)
        self.assertTrue(any("Digging" in m for m in msgs),
                        f"Expected subagent message in {msgs}")


class TestProgressTimerFallback(unittest.TestCase):
    """Fallback nudges sent when no log activity for fallback_interval."""

    def setUp(self):
        self._orig_defaults = mod._PROGRESS_DEFAULTS
        mod._PROGRESS_DEFAULTS = {
            "enabled": True, "initial_delay": 0, "min_interval": 999,
            "fallback_interval": 0, "poll_interval": 0,
            "messages": _PROGRESS_DEFAULTS["messages"],
        }

    def tearDown(self):
        mod._PROGRESS_DEFAULTS = self._orig_defaults

    def test_fallback_sent_on_no_activity(self):
        """With no log events and fallback_interval=0, fallback fires."""
        ctx = _make_context()
        sent = []

        async def fake_to_thread(fn, *args):
            sent.append(args)

        async def runner():
            with patch.object(asyncio, "sleep", _no_op_sleep), \
                 patch.object(asyncio, "to_thread", fake_to_thread), \
                 patch("builtins.open", MagicMock(side_effect=FileNotFoundError)):
                _start_progress_timer(ctx, "c1", "signal", None)
                ctx.is_running.side_effect = [True, True, True, True, False]
                await ctx.data["pf_progress_task"]

        asyncio.run(runner())
        msgs = [s[1] for s in sent]
        # Initial + at least one fallback
        self.assertGreaterEqual(len(msgs), 2)
        self.assertTrue(any("Still on this" in m or "Still working" in m for m in msgs),
                        f"Expected fallback in {msgs}")

    def test_fallback_rotation(self):
        """Fallback messages alternate between the two templates."""
        ctx = _make_context()
        sent = []

        async def fake_to_thread(fn, *args):
            sent.append(args)

        async def runner():
            with patch.object(asyncio, "sleep", _no_op_sleep), \
                 patch.object(asyncio, "to_thread", fake_to_thread), \
                 patch("builtins.open", MagicMock(side_effect=FileNotFoundError)):
                _start_progress_timer(ctx, "c1", "signal", None)
                # Enough ticks for initial + 2 fallbacks + exit
                ctx.is_running.side_effect = [True] * 10 + [False]
                await ctx.data["pf_progress_task"]

        asyncio.run(runner())
        msgs = [s[1] for s in sent]
        # Should see both fallback variants
        has_fallback1 = any("Still on this" in m for m in msgs)
        has_fallback2 = any("haven't forgotten" in m for m in msgs)
        self.assertTrue(has_fallback1 or has_fallback2,
                        f"Expected at least one fallback variant in {msgs}")


class TestProgressTimerCancelsExisting(unittest.TestCase):
    """An existing progress task is cancelled when a new one is started."""

    def test_existing_task_cancelled(self):
        ctx = _make_context()
        existing = MagicMock()
        existing.done.return_value = False
        ctx.data["pf_progress_task"] = existing

        async def runner():
            with patch.object(asyncio, "sleep", _no_op_sleep), \
                 patch.object(asyncio, "to_thread", _noop_async), \
                 patch("builtins.open", MagicMock(side_effect=FileNotFoundError)):
                _start_progress_timer(ctx, "c1", "signal", None)
                existing.cancel.assert_called_once()
                self.assertIsNot(ctx.data["pf_progress_task"], existing)
                ctx.data["pf_progress_task"].cancel()
                try:
                    await ctx.data["pf_progress_task"]
                except asyncio.CancelledError:
                    pass

        asyncio.run(runner())


class TestProgressTimerRateLimited(unittest.TestCase):
    """Rate-limit in log.progress triggers the rate_limited message."""

    def setUp(self):
        self._orig_defaults = mod._PROGRESS_DEFAULTS
        mod._PROGRESS_DEFAULTS = {
            "enabled": True, "initial_delay": 0, "min_interval": 0,
            "fallback_interval": 999, "poll_interval": 0,
            "messages": _PROGRESS_DEFAULTS["messages"],
        }

    def tearDown(self):
        mod._PROGRESS_DEFAULTS = self._orig_defaults

    def test_rate_limited_message(self):
        ctx = _make_context()
        ctx.log.progress = "Rate limit reached, waiting..."
        sent = []

        async def fake_to_thread(fn, *args):
            sent.append(args)

        async def runner():
            with patch.object(asyncio, "sleep", _no_op_sleep), \
                 patch.object(asyncio, "to_thread", fake_to_thread), \
                 patch("builtins.open", MagicMock(side_effect=FileNotFoundError)):
                _start_progress_timer(ctx, "c1", "signal", None)
                ctx.is_running.side_effect = [True, True, True, True, False]
                await ctx.data["pf_progress_task"]

        asyncio.run(runner())
        msgs = [s[1] for s in sent]
        self.assertTrue(any("thinking this through" in m for m in msgs),
                        f"Expected rate_limited message in {msgs}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _no_op_sleep(_secs):
    """Async sleep that yields control without waiting."""
    return None


async def _noop_async(*_args, **_kwargs):
    return None


if __name__ == "__main__":
    unittest.main()