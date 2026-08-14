import asyncio
from datetime import datetime

import pytest

from app.core.config import settings
from app.services import scheduler
from app.services.scheduler import next_run_of_any, parse_cron_list, run_scheduled_login
from app.services.trade_republic import (
    LOGIN_METHOD_APP_CONFIRMATION,
    LOGIN_METHOD_CODE,
    TRRateLimitError,
)


def run(coro):
    return asyncio.run(coro)


# --- several login windows ---------------------------------------------------

def test_single_expression_still_works():
    schedules = parse_cron_list("0 18 * * 3")

    assert len(schedules) == 1
    assert schedules[0].matches(datetime(2026, 8, 12, 18, 0))


def test_two_windows_at_different_times():
    """A single cron expression cannot express Wednesday 18:00 and Saturday
    12:00: it would also match Wednesday 12:00 and Saturday 18:00."""
    schedules = parse_cron_list("0 18 * * 3; 0 12 * * 6")

    assert len(schedules) == 2
    assert schedules[0].matches(datetime(2026, 8, 12, 18, 0))   # Wednesday
    assert schedules[1].matches(datetime(2026, 8, 15, 12, 0))   # Saturday
    assert not schedules[0].matches(datetime(2026, 8, 15, 12, 0))
    assert not schedules[1].matches(datetime(2026, 8, 12, 18, 0))


def test_blank_entries_and_spacing_are_tolerated():
    assert len(parse_cron_list("  0 18 * * 3 ;; 0 12 * * 6 ; ")) == 2


def test_empty_expression_yields_no_schedules():
    assert parse_cron_list("") == []
    assert parse_cron_list("   ") == []


def test_next_run_picks_the_earliest_window():
    schedules = parse_cron_list("0 18 * * 3; 0 12 * * 6")

    # Wednesday morning: the same day's 18:00 window comes first.
    assert next_run_of_any(schedules, datetime(2026, 8, 12, 9, 0)) == datetime(2026, 8, 12, 18, 0)
    # Wednesday evening: next is Saturday noon.
    assert next_run_of_any(schedules, datetime(2026, 8, 12, 19, 0)) == datetime(2026, 8, 15, 12, 0)


def test_next_run_of_no_schedules_is_none():
    assert next_run_of_any([], datetime(2026, 8, 12, 9, 0)) is None


# --- scheduled login ---------------------------------------------------------

@pytest.fixture
def login_env(monkeypatch):
    """No real Trade Republic calls, no waiting, no notifications."""
    monkeypatch.setattr(settings, "tr_sync_after_login", True)
    monkeypatch.setattr(settings, "tr_login_retry_count", 1)
    monkeypatch.setattr(settings, "tr_login_retry_minutes", 5)

    sent = []
    monkeypatch.setattr(scheduler, "notify", lambda title, message="": sent.append((title, message)))
    monkeypatch.setattr(scheduler, "notify_sync_failure", lambda kind, error: sent.append((kind, str(error))))

    synced = []
    depot_synced = []

    async def _fake_sync():
        synced.append(True)
        return {"status": "ok"}

    async def _fake_depot_sync():
        depot_synced.append(True)
        return {"status": "ok"}

    monkeypatch.setattr(scheduler, "run_scheduled_sync", _fake_sync)
    monkeypatch.setattr(scheduler, "run_scheduled_depot_sync", _fake_depot_sync)

    # Retries must not actually wait during tests.
    async def _no_sleep(_seconds):
        return None

    monkeypatch.setattr(scheduler.asyncio, "sleep", _no_sleep)
    return {"sent": sent, "synced": synced, "depot_synced": depot_synced}


def _valid_session(monkeypatch, valid: bool):
    monkeypatch.setattr(
        scheduler, "get_login_status",
        lambda: {"session_validity": "valid" if valid else "expired"},
    )


def test_skipped_while_the_session_is_still_valid(monkeypatch, login_env):
    """A second weekly window must not ask for a confirmation that is not
    needed."""
    _valid_session(monkeypatch, True)

    result = run(run_scheduled_login())

    assert result["status"] == "skipped"
    assert login_env["synced"] == []


def test_successful_login_triggers_the_sync(monkeypatch, login_env):
    _valid_session(monkeypatch, False)
    monkeypatch.setattr(
        scheduler, "start_login",
        lambda: {"session_id": "s1", "status": "challenge", "login_method": LOGIN_METHOD_APP_CONFIRMATION},
    )
    monkeypatch.setattr(scheduler, "confirm_login", lambda sid: {"status": "connected", "session_id": sid})

    result = run(run_scheduled_login())

    assert result["status"] == "connected"
    assert login_env["synced"] == [True]


def test_sync_can_be_disabled(monkeypatch, login_env):
    monkeypatch.setattr(settings, "tr_sync_after_login", False)
    _valid_session(monkeypatch, False)
    monkeypatch.setattr(
        scheduler, "start_login",
        lambda: {"session_id": "s1", "status": "challenge", "login_method": LOGIN_METHOD_APP_CONFIRMATION},
    )
    monkeypatch.setattr(scheduler, "confirm_login", lambda sid: {"status": "connected", "session_id": sid})

    result = run(run_scheduled_login())

    assert result["status"] == "connected"
    assert login_env["synced"] == []


def test_unconfirmed_window_is_retried_then_reported(monkeypatch, login_env):
    _valid_session(monkeypatch, False)
    monkeypatch.setattr(
        scheduler, "start_login",
        lambda: {"session_id": "s1", "status": "challenge", "login_method": LOGIN_METHOD_APP_CONFIRMATION},
    )

    attempts = []

    def _never_confirmed(sid):
        attempts.append(sid)
        raise TimeoutError("not confirmed in time")

    monkeypatch.setattr(scheduler, "confirm_login", _never_confirmed)

    result = run(run_scheduled_login())

    assert result["status"] == "failed"
    # retry_count=1 means one retry, so two windows in total.
    assert len(attempts) == 2
    assert login_env["sent"], "a failed login must be reported"


def test_retry_succeeding_on_the_second_window(monkeypatch, login_env):
    _valid_session(monkeypatch, False)
    monkeypatch.setattr(
        scheduler, "start_login",
        lambda: {"session_id": "s1", "status": "challenge", "login_method": LOGIN_METHOD_APP_CONFIRMATION},
    )

    calls = {"n": 0}

    def _second_time_lucky(sid):
        calls["n"] += 1
        if calls["n"] == 1:
            raise TimeoutError("not confirmed in time")
        return {"status": "connected", "session_id": sid}

    monkeypatch.setattr(scheduler, "confirm_login", _second_time_lucky)

    result = run(run_scheduled_login())

    assert result["status"] == "connected"
    assert login_env["synced"] == [True]


def test_rate_limiting_stops_immediately(monkeypatch, login_env):
    """Repeated attempts against a rate-limited endpoint would only make it
    worse, so no retry here."""
    _valid_session(monkeypatch, False)

    def _rate_limited():
        raise TRRateLimitError("too many requests", retry_after=600)

    monkeypatch.setattr(scheduler, "start_login", _rate_limited)

    result = run(run_scheduled_login())

    assert result["status"] == "rate_limited"
    assert login_env["synced"] == []


def test_code_based_login_cannot_be_scheduled(monkeypatch, login_env):
    """The scheduler cannot type a code, so this must fail with a clear
    message rather than hang on a window nobody can close."""
    _valid_session(monkeypatch, False)
    monkeypatch.setattr(
        scheduler, "start_login",
        lambda: {"session_id": "s1", "status": "challenge", "login_method": LOGIN_METHOD_CODE},
    )

    result = run(run_scheduled_login())

    assert result["status"] == "failed"
    assert login_env["synced"] == []


def test_depot_valuation_rides_along_with_the_login(monkeypatch, login_env):
    """The depot sync needs a session too, so it runs after a login instead of
    on a schedule of its own that would fire while the session is dead. It
    gates itself, so it only actually writes once per month."""
    _valid_session(monkeypatch, False)
    monkeypatch.setattr(
        scheduler, "start_login",
        lambda: {"session_id": "s1", "status": "challenge", "login_method": LOGIN_METHOD_APP_CONFIRMATION},
    )
    monkeypatch.setattr(scheduler, "confirm_login", lambda sid: {"status": "connected", "session_id": sid})

    result = run(run_scheduled_login())

    assert result["status"] == "connected"
    assert login_env["depot_synced"] == [True]


def test_no_depot_valuation_without_a_login(monkeypatch, login_env):
    _valid_session(monkeypatch, True)

    run(run_scheduled_login())

    assert login_env["depot_synced"] == []


def test_failing_depot_valuation_does_not_fail_the_login(monkeypatch, login_env):
    """A broken valuation must not discard a session that was just confirmed."""
    _valid_session(monkeypatch, False)
    monkeypatch.setattr(
        scheduler, "start_login",
        lambda: {"session_id": "s1", "status": "challenge", "login_method": LOGIN_METHOD_APP_CONFIRMATION},
    )
    monkeypatch.setattr(scheduler, "confirm_login", lambda sid: {"status": "connected", "session_id": sid})

    async def _boom():
        raise RuntimeError("actual unreachable")

    monkeypatch.setattr(scheduler, "run_scheduled_depot_sync", _boom)

    result = run(run_scheduled_login())

    assert result["status"] == "connected"
    assert result["depot_sync"]["status"] == "failed"