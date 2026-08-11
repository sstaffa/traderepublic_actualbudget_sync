import pytest

from app.core.config import settings


@pytest.fixture(autouse=True)
def isolate_event_type_blocklist(monkeypatch):
    """Run every test with an empty event-type blocklist.

    Settings are read from the environment at import time, so without this the
    suite would silently inherit TR_EXCLUDED_EVENT_TYPES from the deployment's
    .env: the mapper would drop fixture transactions and unrelated tests would
    fail depending on who runs them and where. Tests that care about filtering
    set the blocklist explicitly.
    """
    monkeypatch.setattr(settings, "tr_excluded_event_types", [])