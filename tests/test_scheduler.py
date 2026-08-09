from datetime import datetime

from app.services.scheduler import parse_cron


def test_parse_default_daily_midnight():
    schedule = parse_cron("0 0 * * *")

    assert schedule.matches(datetime(2026, 5, 31, 0, 0))
    assert not schedule.matches(datetime(2026, 5, 31, 0, 1))
    assert not schedule.matches(datetime(2026, 5, 31, 1, 0))


def test_next_after_default_daily_midnight():
    schedule = parse_cron("0 0 * * *")

    assert schedule.next_after(datetime(2026, 5, 31, 10, 30)) == datetime(2026, 6, 1, 0, 0)


def test_parse_steps_and_weekday():
    schedule = parse_cron("*/15 8-10 * * 1-5")

    assert schedule.matches(datetime(2026, 6, 1, 8, 15))
    assert schedule.matches(datetime(2026, 6, 1, 10, 45))
    assert not schedule.matches(datetime(2026, 6, 1, 11, 0))
    assert not schedule.matches(datetime(2026, 5, 31, 8, 15))


def test_depot_sync_default_cron_daily_at_18():
    schedule = parse_cron("0 18 * * *")

    assert schedule.matches(datetime(2026, 6, 1, 18, 0))
    assert not schedule.matches(datetime(2026, 6, 1, 18, 1))
    assert not schedule.matches(datetime(2026, 6, 1, 17, 0))


def test_depot_sync_due_when_never_run_before(monkeypatch, tmp_path):
    from app.services import scheduler
    from app.core.config import settings

    monkeypatch.setattr(settings, "tr_cookies_file", str(tmp_path / "cookies.json"))

    assert scheduler._depot_sync_due(30) is True


def test_depot_sync_not_due_within_interval(monkeypatch, tmp_path):
    from app.services import scheduler
    from app.services.state import mark_depot_sync_success
    from app.core.config import settings

    monkeypatch.setattr(settings, "tr_cookies_file", str(tmp_path / "cookies.json"))

    mark_depot_sync_success({"status": "ok"})

    assert scheduler._depot_sync_due(30) is False


def test_depot_sync_due_after_interval_elapsed(monkeypatch, tmp_path):
    from datetime import timedelta, timezone
    from app.services import scheduler
    from app.services.state import load_state, save_state
    from app.core.config import settings

    monkeypatch.setattr(settings, "tr_cookies_file", str(tmp_path / "cookies.json"))

    state = load_state()
    old_timestamp = (datetime.now(timezone.utc) - timedelta(days=31)).isoformat()
    state["last_depot_sync_at"] = old_timestamp
    save_state(state)

    assert scheduler._depot_sync_due(30) is True


def test_depot_sync_transaction_sync_state_kept_independent(monkeypatch, tmp_path):
    """Depot-sync bookkeeping must never influence the transaction sync's
    `last_successful_sync_at` cursor, which controls the incremental
    from_date used by the regular cash-transaction cron sync."""
    from app.services.state import load_state, mark_depot_sync_success, mark_sync_success
    from app.core.config import settings

    monkeypatch.setattr(settings, "tr_cookies_file", str(tmp_path / "cookies.json"))

    mark_sync_success({"mapped_count": 0, "pushed": {}}, scheduled=True)
    mark_depot_sync_success({"main": {}, "sub_accounts": {}})

    state = load_state()
    assert "last_successful_sync_at" in state
    assert "last_depot_sync_at" in state
    assert state["last_depot_sync_result"] == {"main": {}, "sub_accounts": {}}