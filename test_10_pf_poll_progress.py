"""
Tests for the event-driven progress timer in _10_pf_poll.py.

Tests cover:
  1. _classify_log_entries() — signal classification from log entries
  2. _detect_meta_command() — suppress/resume phrase detection
  3. _start_progress_timer() — full event-loop behavior:
     - Initial message sent after observation window
     - Event-driven messages fire on log activity (subagent, tool, synthesizing)
     - Throttling: min_interval respected between messages
     - Escalating fallback schedule (short → medium → long → hours → deep)
     - Time-aware fallback messages with {minutes}/{hours} formatting
     - pf_no_updates flag suppresses the loop
     - pf_no_updates flag reset on new task
     - is_running() False → loop exits
     - Existing task cancelled when new one starts
     - Disabled via config → no task created
  4. _handle_meta_command() — suppress/resume ack + flag lifecycle
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
    _detect_meta_command,
    _handle_meta_command,
    _start_progress_timer,
    _get_verbosity,
    _PROGRESS_DEFAULTS,
)

# Default _get_verbosity to "chatty" so pre-existing tests retain their
# original behavior. Tests that need a specific verbosity patch it explicitly.
mod._get_verbosity = lambda: "chatty"


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
        signal, _ = _classify_log_entries(entries, 0)
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
# Tests for _detect_meta_command()
# ---------------------------------------------------------------------------


class TestDetectMetaCommand(unittest.TestCase):
    """Tests for the meta-command phrase detector.

    The critical test suite — prevents false positives that would eat real
    user messages. Uses exact phrase matching (not substring) + length guard.
    """

    # --- Positive: suppress phrases ---

    def test_suppress_no_need_to_update_me(self):
        self.assertEqual(_detect_meta_command("no need to update me"), "suppress")

    def test_suppress_no_updates(self):
        self.assertEqual(_detect_meta_command("no updates"), "suppress")

    def test_suppress_stop_updates(self):
        self.assertEqual(_detect_meta_command("stop updates"), "suppress")

    def test_suppress_stop_updating_me(self):
        self.assertEqual(_detect_meta_command("stop updating me"), "suppress")

    def test_suppress_quiet(self):
        self.assertEqual(_detect_meta_command("quiet"), "suppress")

    def test_suppress_case_insensitive(self):
        self.assertEqual(_detect_meta_command("Stop Updates"), "suppress")
        self.assertEqual(_detect_meta_command("QUIET"), "suppress")

    def test_suppress_trailing_punctuation(self):
        self.assertEqual(_detect_meta_command("stop updates."), "suppress")
        self.assertEqual(_detect_meta_command("quiet!"), "suppress")
        self.assertEqual(_detect_meta_command("no updates?"), "suppress")

    def test_suppress_dont_without_apostrophe(self):
        self.assertEqual(_detect_meta_command("dont update me"), "suppress")

    # --- Positive: resume phrases ---

    def test_resume_update_me(self):
        self.assertEqual(_detect_meta_command("update me"), "resume")

    def test_resume_keep_me_posted(self):
        self.assertEqual(_detect_meta_command("keep me posted"), "resume")

    def test_resume_resume_updates(self):
        self.assertEqual(_detect_meta_command("resume updates"), "resume")

    def test_resume_send_updates(self):
        self.assertEqual(_detect_meta_command("send updates"), "resume")

    def test_resume_case_insensitive(self):
        self.assertEqual(_detect_meta_command("UPDATE ME"), "resume")

    # --- Negative: false positive prevention ---

    def test_negative_keep_this_quiet(self):
        """'keep this quiet' must NOT match — it's a real question for the agent."""
        self.assertIsNone(_detect_meta_command("keep this quiet"))

    def test_negative_hold_off_on_the_analysis(self):
        """'hold off on the analysis' must NOT match — contains 'hold off on' but
        is a longer phrase, not a meta-command."""
        self.assertIsNone(_detect_meta_command("hold off on the analysis"))

    def test_negative_can_you_update_me_on_the_status(self):
        """A longer message containing 'update me' must NOT match — length guard."""
        self.assertIsNone(_detect_meta_command("can you update me on the status of the project"))

    def test_negative_what_updates_do_you_have(self):
        self.assertIsNone(_detect_meta_command("what updates do you have"))

    def test_negative_empty_string(self):
        self.assertIsNone(_detect_meta_command(""))

    def test_negative_normal_question(self):
        self.assertIsNone(_detect_meta_command("what's the weather like?"))

    def test_negative_long_message_with_quiet(self):
        """A long message containing 'quiet' must NOT match — length guard."""
        self.assertIsNone(_detect_meta_command(
            "this is a long message about keeping things quiet in the repository"
        ))

    def test_negative_quietly_as_adverb(self):
        """'quietly' is not the same as 'quiet' — must not match."""
        self.assertIsNone(_detect_meta_command("quietly"))

    def test_negative_silencer(self):
        """'silencer' is not 'silence' — must not match."""
        self.assertIsNone(_detect_meta_command("silencer"))

    def test_negative_update_me_on_x_long_message(self):
        """A >50 char message with 'update me' must not match."""
        self.assertIsNone(_detect_meta_command(
            "please update me on the status of the quarterly report when ready"
        ))


# ---------------------------------------------------------------------------
# Tests for _handle_meta_command()
# ---------------------------------------------------------------------------


class TestHandleMetaCommand(unittest.TestCase):
    """Tests for suppress/resume ack + flag lifecycle."""

    def test_suppress_sets_flag(self):
        ctx = _make_context()
        with patch.object(asyncio, "to_thread", _noop_to_thread):
            asyncio.run(_handle_meta_command(ctx, "suppress", "c1", "signal", None))
        self.assertTrue(ctx.data.get("pf_no_updates"))

    def test_suppress_cancels_existing_task(self):
        ctx = _make_context()
        existing = MagicMock()
        existing.done.return_value = False
        ctx.data["pf_progress_task"] = existing
        with patch.object(asyncio, "to_thread", _noop_to_thread):
            asyncio.run(_handle_meta_command(ctx, "suppress", "c1", "signal", None))
        existing.cancel.assert_called_once()

    def test_suppress_sends_ack(self):
        ctx = _make_context()
        sent = []

        async def fake_to_thread(fn, *args):
            sent.append((fn.__name__, args))

        with patch.object(asyncio, "to_thread", fake_to_thread):
            asyncio.run(_handle_meta_command(ctx, "suppress", "c1", "signal", None))
        self.assertEqual(len(sent), 1)
        self.assertIn("hold off", sent[0][1][1])

    def test_resume_clears_flag(self):
        ctx = _make_context()
        ctx.data["pf_no_updates"] = True
        with patch.object(asyncio, "to_thread", _noop_to_thread), \
             patch.object(mod, "_start_progress_timer"):
            asyncio.run(_handle_meta_command(ctx, "resume", "c1", "signal", None))
        self.assertFalse(ctx.data.get("pf_no_updates"))

    def test_resume_sends_ack(self):
        ctx = _make_context()
        sent = []

        async def fake_to_thread(fn, *args):
            sent.append(args)

        with patch.object(asyncio, "to_thread", fake_to_thread), \
             patch.object(mod, "_start_progress_timer"):
            asyncio.run(_handle_meta_command(ctx, "resume", "c1", "signal", None))
        # First call is the resume ack
        self.assertGreaterEqual(len(sent), 1)
        self.assertIn("Back on it", sent[0][1])

    def test_resume_restarts_timer_when_running(self):
        ctx = _make_context(is_running_seq=[True])
        with patch.object(asyncio, "to_thread", _noop_to_thread), \
             patch.object(mod, "_start_progress_timer") as start_timer:
            asyncio.run(_handle_meta_command(ctx, "resume", "c1", "signal", None))
        start_timer.assert_called_once()

    def test_resume_sends_idle_ack_when_not_running(self):
        ctx = _make_context(is_running_seq=[False])
        sent = []

        async def fake_to_thread(fn, *args):
            sent.append(args)

        with patch.object(asyncio, "to_thread", fake_to_thread), \
             patch.object(mod, "_start_progress_timer") as start_timer:
            asyncio.run(_handle_meta_command(ctx, "resume", "c1", "signal", None))
        start_timer.assert_not_called()
        # Should send resume_ack + resume_idle_ack (2 sends)
        self.assertEqual(len(sent), 2)
        self.assertIn("nothing's running", sent[1][1])

    def test_unknown_command_returns_false(self):
        ctx = _make_context()
        with patch.object(asyncio, "to_thread", _noop_to_thread):
            result = asyncio.run(_handle_meta_command(ctx, "unknown", "c1", "signal", None))
        self.assertFalse(result)


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
                 "enabled": False, "observation_window": 0, "min_interval": 0,
                 "poll_interval": 0, "fallback_schedule": [],
                 "messages": {},
             }):
            _start_progress_timer(ctx, "c1", "signal", None)
        create_task.assert_not_called()
        self.assertNotIn("pf_progress_task", ctx.data)


class TestProgressTimerInitialMessage(unittest.TestCase):
    """Initial message sent after observation window."""

    def setUp(self):
        self._orig = mod._PROGRESS_DEFAULTS
        mod._PROGRESS_DEFAULTS = {
            "enabled": True, "observation_window": 0, "min_interval": 999,
            "poll_interval": 0,
            "fallback_schedule": [(999999, 999, "fallback_short")],
            "messages": _PROGRESS_DEFAULTS["messages"],
        }

    def tearDown(self):
        mod._PROGRESS_DEFAULTS = self._orig

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
        self.assertGreaterEqual(len(sent), 1)
        self.assertIn("On it", sent[0][1])


class TestProgressTimerEventDriven(unittest.TestCase):
    """Event-driven messages fire on log activity."""

    def setUp(self):
        self._orig = mod._PROGRESS_DEFAULTS
        mod._PROGRESS_DEFAULTS = {
            "enabled": True, "observation_window": 0, "min_interval": 0,
            "poll_interval": 0,
            "fallback_schedule": [(999999, 999, "fallback_short")],
            "messages": _PROGRESS_DEFAULTS["messages"],
        }

    def tearDown(self):
        mod._PROGRESS_DEFAULTS = self._orig

    def test_tool_activity_triggers_message(self):
        ctx = _make_context()
        sent = []
        sleep_calls = [0]

        async def fake_to_thread(fn, *args):
            sent.append(args)

        async def sleeping_sleep(_secs):
            sleep_calls[0] += 1
            if sleep_calls[0] == 2:
                ctx.log.add(FakeLogEntry("tool"))

        async def runner():
            with patch.object(asyncio, "sleep", sleeping_sleep), \
                 patch.object(asyncio, "to_thread", fake_to_thread), \
                 patch("builtins.open", MagicMock(side_effect=FileNotFoundError)):
                _start_progress_timer(ctx, "c1", "signal", None)
                ctx.is_running.side_effect = [True] * 10 + [False]
                await ctx.data["pf_progress_task"]

        asyncio.run(runner())
        msgs = [s[1] for s in sent]
        self.assertGreaterEqual(len(msgs), 2)
        self.assertIn("On it", msgs[0])
        self.assertTrue(any("got a few things" in m for m in msgs),
                        f"Expected tool_activity in {msgs}")

    def test_subagent_triggers_message(self):
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
                ctx.is_running.side_effect = [True] * 10 + [False]
                await ctx.data["pf_progress_task"]

        asyncio.run(runner())
        msgs = [s[1] for s in sent]
        self.assertGreaterEqual(len(msgs), 2)
        self.assertTrue(any("Digging" in m for m in msgs),
                        f"Expected subagent message in {msgs}")


class TestProgressTimerFallback(unittest.TestCase):
    """Escalating fallback nudges sent when no log activity."""

    def setUp(self):
        self._orig = mod._PROGRESS_DEFAULTS
        # Very short schedule for testing
        mod._PROGRESS_DEFAULTS = {
            "enabled": True, "observation_window": 0, "min_interval": 999,
            "poll_interval": 0,
            "fallback_schedule": [
                (10, 0, "fallback_short"),       # 0-10s elapsed: every 0s
                (30, 0, "fallback_medium"),       # 10-30s: every 0s
                (999999, 0, "fallback_long"),     # 30s+: every 0s
            ],
            "messages": _PROGRESS_DEFAULTS["messages"],
        }

    def tearDown(self):
        mod._PROGRESS_DEFAULTS = self._orig

    def test_fallback_sent_on_no_activity(self):
        ctx = _make_context()
        sent = []

        async def fake_to_thread(fn, *args):
            sent.append(args)

        async def runner():
            with patch.object(asyncio, "sleep", _no_op_sleep), \
                 patch.object(asyncio, "to_thread", fake_to_thread), \
                 patch("builtins.open", MagicMock(side_effect=FileNotFoundError)):
                _start_progress_timer(ctx, "c1", "signal", None)
                ctx.is_running.side_effect = [True] * 10 + [False]
                await ctx.data["pf_progress_task"]

        asyncio.run(runner())
        msgs = [s[1] for s in sent]
        self.assertGreaterEqual(len(msgs), 2)
        self.assertTrue(any("Still on this" in m or "Still working" in m for m in msgs),
                        f"Expected fallback in {msgs}")

    def test_escalation_switches_to_medium_message(self):
        """After 10s elapsed, the message switches from short to medium."""
        ctx = _make_context()
        sent = []
        mock_time = [0.0]

        async def fake_to_thread(fn, *args):
            sent.append(args)

        def fake_monotonic():
            return mock_time[0]

        async def time_sleep(_secs):
            mock_time[0] += 5  # each tick advances 5s

        async def runner():
            with patch.object(asyncio, "sleep", time_sleep), \
                 patch.object(asyncio, "to_thread", fake_to_thread), \
                 patch("builtins.open", MagicMock(side_effect=FileNotFoundError)), \
                 patch("time.monotonic", fake_monotonic):
                _start_progress_timer(ctx, "c1", "signal", None)
                # initial + poll(t=5, short) + poll(t=10, medium) + poll(t=15, medium) + exit
                ctx.is_running.side_effect = [True] * 10 + [False]
                await ctx.data["pf_progress_task"]

        asyncio.run(runner())
        msgs = [s[1] for s in sent]
        # First fallback (t=5, <10s) should be short
        self.assertGreaterEqual(len(msgs), 2)
        self.assertIn("Still on this", msgs[1])  # fallback_short
        # Second fallback (t=10+, ≥10s) should be medium
        if len(msgs) >= 3:
            self.assertIn("few minutes", msgs[2])  # fallback_medium

    def test_time_aware_message_includes_minutes(self):
        """Long fallback messages include {minutes} placeholder."""
        ctx = _make_context()
        sent = []
        mock_time = [0.0]

        async def fake_to_thread(fn, *args):
            sent.append(args)

        def fake_monotonic():
            return mock_time[0]

        async def time_sleep(_secs):
            mock_time[0] += 35  # jump past 30s threshold to fallback_long

        async def runner():
            with patch.object(asyncio, "sleep", time_sleep), \
                 patch.object(asyncio, "to_thread", fake_to_thread), \
                 patch("builtins.open", MagicMock(side_effect=FileNotFoundError)), \
                 patch("time.monotonic", fake_monotonic):
                _start_progress_timer(ctx, "c1", "signal", None)
                ctx.is_running.side_effect = [True, True, True, False]
                await ctx.data["pf_progress_task"]

        asyncio.run(runner())
        msgs = [s[1] for s in sent]
        # Should have initial + a long fallback with minutes
        self.assertGreaterEqual(len(msgs), 2)
        long_msgs = [m for m in msgs if "minutes in" in m]
        self.assertTrue(long_msgs, f"Expected time-aware message in {msgs}")


class TestProgressTimerSuppressFlag(unittest.TestCase):
    """pf_no_updates flag suppresses the progress loop."""

    def setUp(self):
        self._orig = mod._PROGRESS_DEFAULTS
        mod._PROGRESS_DEFAULTS = {
            "enabled": True, "observation_window": 0, "min_interval": 0,
            "poll_interval": 0,
            "fallback_schedule": [(999999, 999, "fallback_short")],
            "messages": _PROGRESS_DEFAULTS["messages"],
        }

    def tearDown(self):
        mod._PROGRESS_DEFAULTS = self._orig

    def test_flag_set_before_loop_is_cleared_by_timer_start(self):
        """If pf_no_updates was set before _start_progress_timer is called,
        the timer resets it for the new task and the loop runs normally."""
        ctx = _make_context()
        ctx.data["pf_no_updates"] = True
        sent = []

        async def fake_to_thread(fn, *args):
            sent.append(args)

        async def runner():
            with patch.object(asyncio, "sleep", _no_op_sleep), \
                 patch.object(asyncio, "to_thread", fake_to_thread), \
                 patch("builtins.open", MagicMock(side_effect=FileNotFoundError)):
                _start_progress_timer(ctx, "c1", "signal", None)
                # Flag is reset by _start_progress_timer
                self.assertNotIn("pf_no_updates", ctx.data)
                ctx.is_running.side_effect = [True, False]
                await ctx.data["pf_progress_task"]

        asyncio.run(runner())
        # Initial message should have been sent (flag was reset)
        self.assertGreaterEqual(len(sent), 1)

    def test_flag_reset_on_new_task(self):
        """_start_progress_timer resets pf_no_updates for a new task."""
        ctx = _make_context()
        ctx.data["pf_no_updates"] = True
        sent = []

        async def fake_to_thread(fn, *args):
            sent.append(args)

        async def runner():
            with patch.object(asyncio, "sleep", _no_op_sleep), \
                 patch.object(asyncio, "to_thread", fake_to_thread), \
                 patch("builtins.open", MagicMock(side_effect=FileNotFoundError)):
                _start_progress_timer(ctx, "c1", "signal", None)
                # Flag should be cleared by _start_progress_timer
                self.assertNotIn("pf_no_updates", ctx.data)
                ctx.is_running.side_effect = [True, False]
                await ctx.data["pf_progress_task"]

        asyncio.run(runner())
        # Initial message should have been sent (flag was reset)
        self.assertGreaterEqual(len(sent), 1)

    def test_flag_set_during_loop_exits(self):
        """If pf_no_updates becomes True mid-loop, the loop exits on next tick."""
        ctx = _make_context()
        sent = []

        async def fake_to_thread(fn, *args):
            sent.append(args)

        sleep_calls = [0]

        async def sleeping_sleep(_secs):
            sleep_calls[0] += 1
            # Observation window is 0, so ack is sent immediately without
            # any sleep. The first sleep is the main loop's first poll —
            # set the flag then so the main loop exits before sending more.
            if sleep_calls[0] == 1:
                ctx.data["pf_no_updates"] = True

        async def runner():
            with patch.object(asyncio, "sleep", sleeping_sleep), \
                 patch.object(asyncio, "to_thread", fake_to_thread), \
                 patch("builtins.open", MagicMock(side_effect=FileNotFoundError)):
                _start_progress_timer(ctx, "c1", "signal", None)
                ctx.is_running.side_effect = [True] * 10 + [False]
                await ctx.data["pf_progress_task"]

        asyncio.run(runner())
        # Only the initial message should have been sent
        self.assertEqual(len(sent), 1)
        self.assertIn("On it", sent[0][1])


class TestObservationWindow(unittest.TestCase):
    """Tests for the dynamic observation window that replaces the fixed initial delay.

    The observation loop watches the agent's log for ~10s before deciding
    whether to send an initial ack:
      - type="response" entry → skip ack (answer is coming)
      - tool/subagent entry → send ack immediately (agent is working)
      - rate-limit detected → send rate-limited ack
      - no entries after window expires → send initial ack (long inference)
      - agent finishes during observation → no ack
      - suppress flag set during observation → exit immediately
    """

    def setUp(self):
        self._orig = mod._PROGRESS_DEFAULTS
        mod._PROGRESS_DEFAULTS = {
            "enabled": True, "observation_window": 10, "min_interval": 999,
            "poll_interval": 0,
            "fallback_schedule": [(999999, 999, "fallback_short")],
            "messages": _PROGRESS_DEFAULTS["messages"],
        }

    def tearDown(self):
        mod._PROGRESS_DEFAULTS = self._orig

    def test_ack_skipped_when_response_during_observation(self):
        """Agent produces a 'response' entry during observation → skip ack."""
        ctx = _make_context()
        sent = []
        sleep_calls = [0]

        async def fake_to_thread(fn, *args):
            sent.append(args)

        async def sleeping_sleep(_secs):
            sleep_calls[0] += 1
            if sleep_calls[0] == 1:
                ctx.log.add(FakeLogEntry("response", content="partial answer..."))

        async def runner():
            with patch.object(asyncio, "sleep", sleeping_sleep), \
                 patch.object(asyncio, "to_thread", fake_to_thread), \
                 patch("builtins.open", MagicMock(side_effect=FileNotFoundError)):
                _start_progress_timer(ctx, "c1", "signal", None)
                ctx.is_running.side_effect = [True] * 10 + [False]
                await ctx.data["pf_progress_task"]

        asyncio.run(runner())
        # No "On it" message — the agent is already responding
        self.assertFalse(any("On it" in m for m in [s[1] for s in sent]),
                         f"Expected no initial ack when response is imminent: {sent}")

    def test_ack_sent_when_tool_during_observation(self):
        """Agent produces a 'tool' entry during observation → send ack immediately."""
        ctx = _make_context()
        sent = []
        sleep_calls = [0]

        async def fake_to_thread(fn, *args):
            sent.append(args)

        async def sleeping_sleep(_secs):
            sleep_calls[0] += 1
            if sleep_calls[0] == 1:
                ctx.log.add(FakeLogEntry("tool"))

        async def runner():
            with patch.object(mod, "_get_verbosity", return_value="chatty"), \
                 patch.object(asyncio, "sleep", sleeping_sleep), \
                 patch.object(asyncio, "to_thread", fake_to_thread), \
                 patch("builtins.open", MagicMock(side_effect=FileNotFoundError)):
                _start_progress_timer(ctx, "c1", "signal", None)
                ctx.is_running.side_effect = [True] * 10 + [False]
                await ctx.data["pf_progress_task"]

        asyncio.run(runner())
        msgs = [s[1] for s in sent]
        self.assertTrue(any("On it" in m for m in msgs),
                        f"Expected initial ack when tool activity detected: {msgs}")

    def test_ack_sent_when_rate_limited_during_observation(self):
        """Rate-limit detected during observation → send rate-limited ack."""
        ctx = _make_context()
        ctx.log.progress = "Rate limit reached, waiting..."
        sent = []

        async def fake_to_thread(fn, *args):
            sent.append(args)

        async def runner():
            with patch.object(mod, "_get_verbosity", return_value="chatty"), \
                 patch.object(asyncio, "sleep", _no_op_sleep), \
                 patch.object(asyncio, "to_thread", fake_to_thread), \
                 patch("builtins.open", MagicMock(side_effect=FileNotFoundError)):
                _start_progress_timer(ctx, "c1", "signal", None)
                ctx.is_running.side_effect = [True] * 10 + [False]
                await ctx.data["pf_progress_task"]

        asyncio.run(runner())
        msgs = [s[1] for s in sent]
        self.assertTrue(any("thinking this through" in m for m in msgs),
                        f"Expected rate-limited ack: {msgs}")

    def test_ack_sent_when_observation_window_expires(self):
        """No log entries after observation window → send initial ack (long inference)."""
        ctx = _make_context()
        sent = []
        mock_time = [0.0]

        async def fake_to_thread(fn, *args):
            sent.append(args)

        def fake_monotonic():
            return mock_time[0]

        async def time_sleep(_secs):
            mock_time[0] += 15  # advance past observation_window (10s)

        async def runner():
            with patch.object(asyncio, "sleep", time_sleep), \
                 patch.object(asyncio, "to_thread", fake_to_thread), \
                 patch("builtins.open", MagicMock(side_effect=FileNotFoundError)), \
                 patch("time.monotonic", fake_monotonic):
                _start_progress_timer(ctx, "c1", "signal", None)
                ctx.is_running.side_effect = [True] * 10 + [False]
                await ctx.data["pf_progress_task"]

        asyncio.run(runner())
        msgs = [s[1] for s in sent]
        self.assertTrue(any("On it" in m for m in msgs),
                        f"Expected initial ack after observation window: {msgs}")

    def test_no_ack_when_agent_finishes_during_observation(self):
        """Agent finishes during observation → no ack, loop exits."""
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
        self.assertEqual(sent, [],
                         f"Expected no messages when agent finishes during observation: {sent}")

    def test_suppress_flag_respected_during_observation(self):
        """pf_no_updates set during observation → loop exits, no ack."""
        ctx = _make_context()
        sent = []
        sleep_calls = [0]

        async def fake_to_thread(fn, *args):
            sent.append(args)

        async def sleeping_sleep(_secs):
            sleep_calls[0] += 1
            if sleep_calls[0] == 1:
                ctx.data["pf_no_updates"] = True

        async def runner():
            with patch.object(asyncio, "sleep", sleeping_sleep), \
                 patch.object(asyncio, "to_thread", fake_to_thread), \
                 patch("builtins.open", MagicMock(side_effect=FileNotFoundError)):
                _start_progress_timer(ctx, "c1", "signal", None)
                ctx.is_running.side_effect = [True] * 10 + [False]
                await ctx.data["pf_progress_task"]

        asyncio.run(runner())
        self.assertEqual(sent, [],
                         f"Expected no messages when suppressed during observation: {sent}")


class TestProgressTimerCancelsExisting(unittest.TestCase):
    """An existing progress task is cancelled when a new one is started."""

    def test_existing_task_cancelled(self):
        ctx = _make_context()
        existing = MagicMock()
        existing.done.return_value = False
        ctx.data["pf_progress_task"] = existing

        async def runner():
            with patch.object(asyncio, "sleep", _no_op_sleep), \
                 patch.object(asyncio, "to_thread", _noop_to_thread), \
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
        self._orig = mod._PROGRESS_DEFAULTS
        mod._PROGRESS_DEFAULTS = {
            "enabled": True, "observation_window": 0, "min_interval": 0,
            "poll_interval": 0,
            "fallback_schedule": [(999999, 999, "fallback_short")],
            "messages": _PROGRESS_DEFAULTS["messages"],
        }

    def tearDown(self):
        mod._PROGRESS_DEFAULTS = self._orig

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
                ctx.is_running.side_effect = [True] * 10 + [False]
                await ctx.data["pf_progress_task"]

        asyncio.run(runner())
        msgs = [s[1] for s in sent]
        self.assertTrue(any("thinking this through" in m for m in msgs),
                        f"Expected rate_limited message in {msgs}")


# ---------------------------------------------------------------------------
# Tests for verbosity (mute/normal/chatty)
# ---------------------------------------------------------------------------


class TestGetVerbosity(unittest.TestCase):
    """Tests for the _get_verbosity() helper."""

    def test_defaults_to_normal_when_no_config(self):
        # When plugins.get_plugin_config returns None, defaults to "normal"
        _helpers_mock.plugins.get_plugin_config.return_value = None
        result = _get_verbosity()
        self.assertEqual(result, "normal")

    def test_returns_configured_value(self):
        _helpers_mock.plugins.get_plugin_config.return_value = {"progress_verbosity": "chatty"}
        result = _get_verbosity()
        self.assertEqual(result, "chatty")

    def test_returns_normal_when_key_absent(self):
        _helpers_mock.plugins.get_plugin_config.return_value = {"other_key": "value"}
        result = _get_verbosity()
        self.assertEqual(result, "normal")


class TestVerbosityMute(unittest.TestCase):
    """Mute mode: _start_progress_timer returns immediately, no task created."""

    def test_mute_no_task_created(self):
        ctx = _make_context()
        with patch.object(mod, "_get_verbosity", return_value="mute"), \
             patch.object(asyncio, "create_task") as create_task:
            _start_progress_timer(ctx, "c1", "signal", None)

        create_task.assert_not_called()
        self.assertNotIn("pf_progress_task", ctx.data)


class TestVerbosityNormal(unittest.TestCase):
    """Normal mode: filters tool_activity + rate_limited, doubles intervals."""

    def setUp(self):
        self._orig = mod._PROGRESS_DEFAULTS
        mod._PROGRESS_DEFAULTS = {
            "enabled": True, "observation_window": 0, "min_interval": 0,
            "poll_interval": 0,
            "fallback_schedule": [(999999, 999, "fallback_short")],
            "messages": _PROGRESS_DEFAULTS["messages"],
        }

    def tearDown(self):
        mod._PROGRESS_DEFAULTS = self._orig

    def test_tool_activity_filtered_in_normal(self):
        """tool_activity signal is skipped in normal mode."""
        ctx = _make_context()
        sent = []
        sleep_calls = [0]

        async def fake_to_thread(fn, *args):
            sent.append(args)

        async def sleeping_sleep(_secs):
            sleep_calls[0] += 1
            if sleep_calls[0] == 2:
                ctx.log.add(FakeLogEntry("tool"))

        async def runner():
            with patch.object(mod, "_get_verbosity", return_value="normal"), \
                 patch.object(asyncio, "sleep", sleeping_sleep), \
                 patch.object(asyncio, "to_thread", fake_to_thread), \
                 patch("builtins.open", MagicMock(side_effect=FileNotFoundError)):
                _start_progress_timer(ctx, "c1", "signal", None)
                ctx.is_running.side_effect = [True] * 10 + [False]
                await ctx.data["pf_progress_task"]

        asyncio.run(runner())
        msgs = [s[1] for s in sent]
        # Initial message sent, but tool_activity should NOT follow
        self.assertGreaterEqual(len(msgs), 1)
        self.assertIn("On it", msgs[0])
        self.assertFalse(any("got a few things" in m for m in msgs),
                         f"tool_activity should be filtered in normal mode: {msgs}")

    def test_rate_limited_filtered_in_normal(self):
        """rate_limited signal is skipped in normal mode."""
        ctx = _make_context()
        ctx.log.progress = "Rate limit reached, waiting..."
        sent = []

        async def fake_to_thread(fn, *args):
            sent.append(args)

        async def runner():
            with patch.object(mod, "_get_verbosity", return_value="normal"), \
                 patch.object(asyncio, "sleep", _no_op_sleep), \
                 patch.object(asyncio, "to_thread", fake_to_thread), \
                 patch("builtins.open", MagicMock(side_effect=FileNotFoundError)):
                _start_progress_timer(ctx, "c1", "signal", None)
                ctx.is_running.side_effect = [True] * 10 + [False]
                await ctx.data["pf_progress_task"]

        asyncio.run(runner())
        msgs = [s[1] for s in sent]
        self.assertFalse(any("thinking this through" in m for m in msgs),
                         f"rate_limited should be filtered in normal mode: {msgs}")

    def test_subagent_not_filtered_in_normal(self):
        """subagent signal still fires in normal mode (key milestone)."""
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
            with patch.object(mod, "_get_verbosity", return_value="normal"), \
                 patch.object(asyncio, "sleep", sleeping_sleep), \
                 patch.object(asyncio, "to_thread", fake_to_thread), \
                 patch("builtins.open", MagicMock(side_effect=FileNotFoundError)):
                _start_progress_timer(ctx, "c1", "signal", None)
                ctx.is_running.side_effect = [True] * 10 + [False]
                await ctx.data["pf_progress_task"]

        asyncio.run(runner())
        msgs = [s[1] for s in sent]
        self.assertTrue(any("Digging" in m for m in msgs),
                        f"subagent should fire in normal mode: {msgs}")

    def test_synthesizing_not_filtered_in_normal(self):
        """synthesizing signal still fires in normal mode (key milestone)."""
        ctx = _make_context()
        sent = []
        sleep_calls = [0]

        async def fake_to_thread(fn, *args):
            sent.append(args)

        async def sleeping_sleep(_secs):
            sleep_calls[0] += 1
            if sleep_calls[0] == 2:
                ctx.log.add(FakeLogEntry("response", content="partial..."))

        async def runner():
            with patch.object(mod, "_get_verbosity", return_value="normal"), \
                 patch.object(asyncio, "sleep", sleeping_sleep), \
                 patch.object(asyncio, "to_thread", fake_to_thread), \
                 patch("builtins.open", MagicMock(side_effect=FileNotFoundError)):
                _start_progress_timer(ctx, "c1", "signal", None)
                ctx.is_running.side_effect = [True] * 10 + [False]
                await ctx.data["pf_progress_task"]

        asyncio.run(runner())
        msgs = [s[1] for s in sent]
        self.assertTrue(any("putting this together" in m for m in msgs),
                        f"synthesizing should fire in normal mode: {msgs}")


class TestVerbosityChatty(unittest.TestCase):
    """Chatty mode: all signals fire (current behavior, unchanged)."""

    def setUp(self):
        self._orig = mod._PROGRESS_DEFAULTS
        mod._PROGRESS_DEFAULTS = {
            "enabled": True, "observation_window": 0, "min_interval": 0,
            "poll_interval": 0,
            "fallback_schedule": [(999999, 999, "fallback_short")],
            "messages": _PROGRESS_DEFAULTS["messages"],
        }

    def tearDown(self):
        mod._PROGRESS_DEFAULTS = self._orig

    def test_tool_activity_fires_in_chatty(self):
        """tool_activity signal fires in chatty mode (not filtered)."""
        ctx = _make_context()
        sent = []
        sleep_calls = [0]

        async def fake_to_thread(fn, *args):
            sent.append(args)

        async def sleeping_sleep(_secs):
            sleep_calls[0] += 1
            if sleep_calls[0] == 2:
                ctx.log.add(FakeLogEntry("tool"))

        async def runner():
            with patch.object(mod, "_get_verbosity", return_value="chatty"), \
                 patch.object(asyncio, "sleep", sleeping_sleep), \
                 patch.object(asyncio, "to_thread", fake_to_thread), \
                 patch("builtins.open", MagicMock(side_effect=FileNotFoundError)):
                _start_progress_timer(ctx, "c1", "signal", None)
                ctx.is_running.side_effect = [True] * 10 + [False]
                await ctx.data["pf_progress_task"]

        asyncio.run(runner())
        msgs = [s[1] for s in sent]
        self.assertTrue(any("got a few things" in m for m in msgs),
                        f"tool_activity should fire in chatty mode: {msgs}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _no_op_sleep(_secs):
    """Async sleep that yields control without waiting."""
    return None


async def _noop_to_thread(*_args, **_kwargs):
    return None


if __name__ == "__main__":
    unittest.main()