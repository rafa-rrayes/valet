"""Fixtures for the end-to-end tests; the stack itself is in stack.py."""
import pytest

from tests.e2e.stack import Stack


@pytest.fixture
async def make_stack(tmp_path):
    """make_stack(sessions="load"|"resume"|"none", runner=True, clock=None, timezone="") -> a started Stack, closed
    after the test."""
    stacks: list[Stack] = []

    async def _make(sessions: str = "load", runner: bool = True, clock=None, timezone: str = "") -> Stack:
        s = Stack(tmp_path / f"s{len(stacks)}", sessions, clock, timezone)
        stacks.append(s)
        return await s.start(runner=runner)

    yield _make
    for s in stacks:
        await s.close()


@pytest.fixture
async def stack(make_stack) -> Stack:
    return await make_stack()
