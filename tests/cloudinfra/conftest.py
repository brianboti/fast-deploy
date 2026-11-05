"""Shared setup for cloudinfra's tests."""
import logging

import pytest


@pytest.fixture(autouse=True)
def _events_reach_caplog():
    """The service stops its events at its own handlers once it sets up logging (observability.configure); tests read
    them through pytest's handler on the root logger, so each test starts with them passed up again."""
    logging.getLogger("cloudinfra").propagate = True
    yield
