"""Shared fixtures (TESTS builder). Tests run offline: tmp dirs, 127.0.0.1, the fake agent; Docker tests are opt-in."""
import os

import pytest


OPT_IN = {"docker": ("VALET_TEST_DOCKER", "needs Docker; set VALET_TEST_DOCKER=1"),
          "chrome": ("VALET_TEST_CHROME", "drives a real Chrome; set VALET_TEST_CHROME=1")}


def pytest_collection_modifyitems(config, items):
    """@pytest.mark.docker needs Docker and the agent-base image, @pytest.mark.chrome a real Chrome: each is skipped
    unless its variable is 1 (VALET_TEST_DOCKER, VALET_TEST_CHROME)."""
    for marker, (var, reason) in OPT_IN.items():
        if os.environ.get(var) == "1":
            continue
        skip = pytest.mark.skip(reason=reason)
        for item in items:
            if marker in item.keywords:
                item.add_marker(skip)


@pytest.fixture(autouse=True)
def _owner_name_default():
    """The owner's name (shared/owner.py) is process-wide: every test starts, and ends, with the default."""
    from valet.shared import owner
    owner.configure(None)
    yield
    owner.configure(None)
