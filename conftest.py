"""Pytest autouse fixture to isolate per-module helpers mocks.

Each test module installs its own `helpers` mock into sys.modules at import
time. Because pytest imports all modules during collection, the last module
imported wins the shared sys.modules["helpers"] slot, so tests that resolve
`from helpers import plugins` at call time can read another module's mock
instead of their own.

This fixture re-asserts the current test module's own `_helpers_mock` (if it
has one) before every test, restoring correct isolation so the full suite
passes when run together.
"""

import sys

import pytest


@pytest.fixture(autouse=True)
def _restore_helpers_mock(request):
    module = request.module
    mock = getattr(module, "_helpers_mock", None)
    if mock is not None:
        sys.modules["helpers"] = mock
    yield
