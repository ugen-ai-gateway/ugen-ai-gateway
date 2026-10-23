import os

# Keep tests from creating data/ugen.db: routes resolve the usage store from settings.
os.environ["USAGE_DB_PATH"] = ":memory:"

import pytest

from ugen_ai_gateway.analysis.pii import set_custom_finder


@pytest.fixture(autouse=True)
def _no_custom_rules():
    """Custom privacy rules are process-wide; keep tests independent."""
    yield
    set_custom_finder(None)
