"""Fixtures for the runner tests; the helpers themselves live in runner_helpers.py."""
import pytest

from runner_helpers import Sent


@pytest.fixture
def sent() -> Sent:
    return Sent()
