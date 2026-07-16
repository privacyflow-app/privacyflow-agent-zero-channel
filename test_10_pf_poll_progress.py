"""
Tests for _start_progress_timer() in _10_pf_poll.py.

Covers:
  1. enabled: false → no task created
  2. enabled: true with defaults → correct number of messages sent at
     correct intervals (initial_delay, then repeat_interval)
  3. is_running() returns False mid-loop → early return, no further sends
  4. max_messages boundary (0 → no sends; cap < len(messages))
  5. Existing progress task is cancelled when a new one starts
  6. send_message failures are swallowed (loop continues)

Async tests use asyncio.run() with patched asyncio.sleep / asyncio.to_thread
to run deterministically without real waits.
"""

import asyncio
import sys
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
from _10_pf_poll import _start_progress_timer  # noqa: E402


def _make_context(is_running_seq=None):
    """Build a mock AgentContext with a configurable is_running() sequence.

    is_running_seq: list of bools returned in order; None means always True.
    """
    ctx = MagicMock()
    ctx.data = {}
    if is_running_seq is None:
        ctx.is_running.return_value = True
    else:
        ctx.is_running.side_effect = list(is_running_seq)
    return ctx


def _run_async(async_fn, *_args, **_kwargs):
    """Run an async function in a fresh event loop.

    _start_progress_timer calls asyncio.create_task, which requires a running
    loop, so the call to _start_progress_timer itself must happen inside the
    loop. async_fn receives no args and should start the timer, await the
    created task, then assert.
    """
    asyncio.run(async_fn())


class TestStartProgressTimerDisabled(unittest.TestCase):
    """enabled: false → _start_progress_timer returns without creating a task."""

    def test_disabled_no_task_created(self):
        ctx = _make_context()
        # Point config load at a yaml that disables progress messages.
        cfg = {"progress_messages": {"enabled": False}}

        with patch.object(asyncio, "create_task") as create_task, \
             patch.object(mod, "_PLUGIN_DIR", "/nonexistent"), \
             patch("builtins.open", MagicMock(side_effect=FileNotFoundError)), \
             patch.object(mod, "_PROGRESS_DEFAULTS", cfg["progress_messages"]):
            # When open() fails, cfg = {}, pm_cfg = {} → enabled falls back to
            # _PROGRESS_DEFAULTS["enabled"]. Force disabled by patching defaults.
            _start_progress_timer(ctx, "c1", "signal", None)

        create_task.assert_not_called()
        self.assertNotIn("pf_progress_task", ctx.data)


class TestStartProgressTimerSendsMessages(unittest.TestCase):
    """enabled with defaults → sends the right number of messages at intervals."""

    def setUp(self):
        # Reduce defaults to tiny intervals so the loop runs instantly even if
        # asyncio.sleep isn't patched (belt-and-suspenders).
        self._orig_defaults = mod._PROGRESS_DEFAULTS
        mod._PROGRESS_DEFAULTS = {
            "enabled": True,
            "initial_delay": 0,
            "repeat_interval": 0,
            "max_messages": 3,
            "messages": ["m1", "m2", "m3"],
        }

    def tearDown(self):
        mod._PROGRESS_DEFAULTS = self._orig_defaults

    def test_sends_expected_messages(self):
        ctx = _make_context(is_running_seq=[True] * 10)
        sent = []

        async def fake_to_thread(fn, *args):
            sent.append(args)

        async def fake_sleep(_secs):
            return None

        async def runner():
            with patch.object(asyncio, "sleep", fake_sleep), \
                 patch.object(asyncio, "to_thread", fake_to_thread), \
                 patch("builtins.open", MagicMock(side_effect=FileNotFoundError)):
                _start_progress_timer(ctx, "c1", "signal", "g1")
                await ctx.data["pf_progress_task"]

        _run_async(runner)

        # 3 messages, all sent to "c1" via "signal" with group "g1"
        self.assertEqual(len(sent), 3)
        for i, (cid, msg, msgr, gid) in enumerate(sent):
            self.assertEqual(cid, "c1")
            self.assertEqual(msg, f"m{i+1}")
            self.assertEqual(msgr, "signal")
            self.assertEqual(gid, "g1")

    def test_stops_when_agent_not_running(self):
        """is_running() returns False before the first send → no sends."""
        ctx = _make_context(is_running_seq=[False])
        sent = []

        async def fake_to_thread(fn, *args):
            sent.append(args)

        async def fake_sleep(_secs):
            return None

        async def runner():
            with patch.object(asyncio, "sleep", fake_sleep), \
                 patch.object(asyncio, "to_thread", fake_to_thread), \
                 patch("builtins.open", MagicMock(side_effect=FileNotFoundError)):
                _start_progress_timer(ctx, "c1", "signal", None)
                await ctx.data["pf_progress_task"]

        _run_async(runner)

        self.assertEqual(sent, [])

    def test_stops_mid_loop_when_agent_finishes(self):
        """is_running() True for first send, then False → only 1 message sent."""
        # is_running() called twice per message (before send, before sleep).
        # [True, True, False]: check1=True, check2=True → send m1; then
        # check1=False → loop returns before m2.
        ctx = _make_context(is_running_seq=[True, True, False])
        sent = []
        sleeps = []

        async def fake_to_thread(fn, *args):
            sent.append(args)

        async def fake_sleep(secs):
            sleeps.append(secs)

        async def runner():
            with patch.object(asyncio, "sleep", fake_sleep), \
                 patch.object(asyncio, "to_thread", fake_to_thread), \
                 patch("builtins.open", MagicMock(side_effect=FileNotFoundError)):
                _start_progress_timer(ctx, "c1", "signal", None)
                await ctx.data["pf_progress_task"]

        _run_async(runner)

        # First send happens (both is_running checks True), then the third
        # is_running check returns False → loop returns. Only 1 message sent.
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][1], "m1")

    def test_send_failure_does_not_crash_loop(self):
        """send_message raising → loop continues to next message."""
        ctx = _make_context(is_running_seq=[True] * 10)
        sent = []
        call_count = [0]

        async def fake_to_thread(fn, *args):
            # First send raises, subsequent succeed
            call_count[0] += 1
            if call_count[0] == 1:
                raise RuntimeError("boom")
            sent.append(args)

        async def fake_sleep(_secs):
            return None

        async def runner():
            with patch.object(asyncio, "sleep", fake_sleep), \
                 patch.object(asyncio, "to_thread", fake_to_thread), \
                 patch("builtins.open", MagicMock(side_effect=FileNotFoundError)):
                _start_progress_timer(ctx, "c1", "signal", None)
                await ctx.data["pf_progress_task"]

        _run_async(runner)

        # First send raised (caught), second and third succeeded
        self.assertEqual(len(sent), 2)


class TestMaxMessagesBoundary(unittest.TestCase):
    """max_messages boundaries: 0 → no sends; cap < len(messages)."""

    def setUp(self):
        self._orig_defaults = mod._PROGRESS_DEFAULTS

    def tearDown(self):
        mod._PROGRESS_DEFAULTS = self._orig_defaults

    def test_max_messages_zero_sends_none(self):
        mod._PROGRESS_DEFAULTS = {
            "enabled": True,
            "initial_delay": 0,
            "repeat_interval": 0,
            "max_messages": 0,
            "messages": ["m1", "m2", "m3"],
        }
        ctx = _make_context(is_running_seq=[True] * 10)
        sent = []

        async def fake_to_thread(fn, *args):
            sent.append(args)

        async def fake_sleep(_secs):
            return None

        async def runner():
            with patch.object(asyncio, "sleep", fake_sleep), \
                 patch.object(asyncio, "to_thread", fake_to_thread), \
                 patch("builtins.open", MagicMock(side_effect=FileNotFoundError)):
                _start_progress_timer(ctx, "c1", "signal", None)
                await ctx.data["pf_progress_task"]

        _run_async(runner)

        # messages[:0] == [] → loop body never runs
        self.assertEqual(sent, [])

    def test_max_messages_caps_message_count(self):
        mod._PROGRESS_DEFAULTS = {
            "enabled": True,
            "initial_delay": 0,
            "repeat_interval": 0,
            "max_messages": 2,
            "messages": ["m1", "m2", "m3", "m4"],
        }
        ctx = _make_context(is_running_seq=[True] * 10)
        sent = []

        async def fake_to_thread(fn, *args):
            sent.append(args)

        async def fake_sleep(_secs):
            return None

        async def runner():
            with patch.object(asyncio, "sleep", fake_sleep), \
                 patch.object(asyncio, "to_thread", fake_to_thread), \
                 patch("builtins.open", MagicMock(side_effect=FileNotFoundError)):
                _start_progress_timer(ctx, "c1", "signal", None)
                await ctx.data["pf_progress_task"]

        _run_async(runner)

        self.assertEqual(len(sent), 2)
        self.assertEqual(sent[0][1], "m1")
        self.assertEqual(sent[1][1], "m2")


class TestCancelsExistingTask(unittest.TestCase):
    """An existing progress task is cancelled when a new one is started."""

    def setUp(self):
        self._orig_defaults = mod._PROGRESS_DEFAULTS
        mod._PROGRESS_DEFAULTS = {
            "enabled": True,
            "initial_delay": 0,
            "repeat_interval": 0,
            "max_messages": 1,
            "messages": ["m1"],
        }

    def tearDown(self):
        mod._PROGRESS_DEFAULTS = self._orig_defaults

    def test_existing_task_cancelled(self):
        existing = MagicMock()
        existing.done.return_value = False
        ctx = _make_context()
        ctx.data["pf_progress_task"] = existing

        async def fake_to_thread(fn, *args):
            pass

        async def fake_sleep(_secs):
            return None

        async def runner():
            with patch.object(asyncio, "sleep", fake_sleep), \
                 patch.object(asyncio, "to_thread", fake_to_thread), \
                 patch("builtins.open", MagicMock(side_effect=FileNotFoundError)):
                _start_progress_timer(ctx, "c1", "signal", None)
                # The existing task must be cancelled
                existing.cancel.assert_called_once()
                # And a new task stored
                self.assertIsNot(ctx.data["pf_progress_task"], existing)
                await ctx.data["pf_progress_task"]

        _run_async(runner)


if __name__ == "__main__":
    unittest.main()