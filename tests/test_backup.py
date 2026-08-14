import datetime

import pytest

from app.core.config import settings
from app.services import backup
from app.services.backup import (
    backup_dir,
    backup_path,
    delete_backup,
    is_valid_backup_name,
    list_backups,
    rotate_backups,
    select_backups_to_keep,
)


@pytest.fixture
def backups(monkeypatch, tmp_path):
    """An isolated backup directory plus a helper to populate it."""
    monkeypatch.setattr(settings, "tr_cookies_file", str(tmp_path / "pytr_cookies.json"))
    monkeypatch.setattr(settings, "backup_dir", "")
    monkeypatch.setattr(settings, "backup_keep_daily", 7)
    monkeypatch.setattr(settings, "backup_keep_weekly", 3)
    monkeypatch.setattr(settings, "backup_keep_monthly", 12)

    directory = tmp_path / "backups"
    directory.mkdir()

    def _create(*moments: datetime.datetime, size: int = 10):
        for moment in moments:
            name = f"actual-backup-{moment:%Y%m%d-%H%M%S}.zip"
            (directory / name).write_bytes(b"x" * size)

    return {"dir": directory, "create": _create}


def _daily_series(days: int, end: datetime.datetime) -> list[datetime.datetime]:
    return [end - datetime.timedelta(days=n) for n in range(days)]


# --- where backups live ------------------------------------------------------

def test_backups_live_next_to_the_cookies(monkeypatch, tmp_path):
    """Inside the /data volume, so they survive restarts and rebuilds."""
    monkeypatch.setattr(settings, "tr_cookies_file", str(tmp_path / "pytr_cookies.json"))
    monkeypatch.setattr(settings, "backup_dir", "")

    assert backup_dir() == tmp_path / "backups"


def test_explicit_directory_wins(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "backup_dir", str(tmp_path / "elsewhere"))

    assert backup_dir() == tmp_path / "elsewhere"


# --- retention policy --------------------------------------------------------

def test_everything_is_kept_below_the_daily_limit():
    end = datetime.datetime(2026, 8, 15, 3, 0)
    moments = _daily_series(5, end)

    assert select_backups_to_keep(moments, 7, 3, 12) == set(moments)


def test_a_year_of_dailies_collapses_to_the_configured_counts():
    end = datetime.datetime(2026, 8, 15, 3, 0)
    moments = _daily_series(400, end)

    keep = select_backups_to_keep(moments, 7, 3, 12)

    assert len(keep) == 7 + 3 + 12


def test_the_seven_newest_are_always_kept():
    end = datetime.datetime(2026, 8, 15, 3, 0)
    moments = _daily_series(400, end)

    keep = select_backups_to_keep(moments, 7, 3, 12)

    assert set(_daily_series(7, end)).issubset(keep)


def test_nothing_older_than_about_a_year_survives():
    end = datetime.datetime(2026, 8, 15, 3, 0)
    moments = _daily_series(400, end)

    keep = select_backups_to_keep(moments, 7, 3, 12)

    assert (end - min(keep)).days < 400


def test_weekly_tier_covers_additional_weeks():
    """Beyond the daily window the history must continue at week granularity,
    rather than jumping straight to monthly and leaving a month-wide hole."""
    end = datetime.datetime(2026, 8, 15, 3, 0)
    moments = _daily_series(60, end)

    keep = sorted(select_backups_to_keep(moments, 7, 3, 12), reverse=True)
    older = keep[7:]

    weeks = {moment.isocalendar()[:2] for moment in older}
    daily_weeks = {moment.isocalendar()[:2] for moment in keep[:7]}

    # Three weekly slots, so at least three weeks the dailies do not cover.
    assert len(weeks - daily_weeks) >= 3


def test_only_one_backup_survives_per_month_in_the_monthly_range():
    end = datetime.datetime(2026, 8, 15, 3, 0)
    moments = _daily_series(400, end)

    keep = select_backups_to_keep(moments, 7, 3, 12)
    old = [moment for moment in keep if (end - moment).days > 40]
    months = [(moment.year, moment.month) for moment in old]

    assert len(months) == len(set(months))


def test_gaps_in_the_series_do_not_break_rotation():
    """Backups may be missing, e.g. while the container was down."""
    end = datetime.datetime(2026, 8, 15, 3, 0)
    moments = [end - datetime.timedelta(days=n) for n in (0, 1, 2, 40, 80, 200)]

    keep = select_backups_to_keep(moments, 7, 3, 12)

    assert keep == set(moments)


def test_disabling_a_tier_is_respected():
    end = datetime.datetime(2026, 8, 15, 3, 0)
    moments = _daily_series(400, end)

    keep = select_backups_to_keep(moments, 7, 0, 0)

    assert len(keep) == 7


# --- rotation on disk --------------------------------------------------------

def test_rotation_deletes_only_what_the_policy_drops(backups):
    end = datetime.datetime(2026, 8, 15, 3, 0)
    backups["create"](*_daily_series(30, end))

    result = rotate_backups()

    remaining = {path.name for path in backups["dir"].iterdir()}
    assert len(remaining) == len(result["kept"])
    assert set(result["kept"]) == remaining
    assert result["deleted"]


def test_dry_run_deletes_nothing(backups):
    end = datetime.datetime(2026, 8, 15, 3, 0)
    backups["create"](*_daily_series(30, end))
    before = {path.name for path in backups["dir"].iterdir()}

    result = rotate_backups(dry_run=True)

    assert {path.name for path in backups["dir"].iterdir()} == before
    assert result["deleted"]


def test_unrelated_files_are_left_alone(backups):
    """Rotation must never touch anything it did not create - the data volume
    also holds cookies and the session store."""
    backups["create"](*_daily_series(30, datetime.datetime(2026, 8, 15, 3, 0)))
    (backups["dir"] / "notes.txt").write_text("keep me")

    rotate_backups()

    assert (backups["dir"] / "notes.txt").exists()


def test_rotation_without_a_directory_is_harmless(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "tr_cookies_file", str(tmp_path / "pytr_cookies.json"))
    monkeypatch.setattr(settings, "backup_dir", "")

    assert rotate_backups() == {"kept": [], "deleted": [], "dry_run": False}


# --- listing -----------------------------------------------------------------

def test_listing_is_newest_first(backups):
    end = datetime.datetime(2026, 8, 15, 3, 0)
    backups["create"](*_daily_series(3, end))

    entries = list_backups()

    assert [entry["created_at"] for entry in entries] == sorted(
        (entry["created_at"] for entry in entries), reverse=True
    )


def test_listing_reports_size(backups):
    backups["create"](datetime.datetime(2026, 8, 15, 3, 0), size=2048)

    assert list_backups()[0]["size_bytes"] == 2048


def test_listing_ignores_foreign_files(backups):
    backups["create"](datetime.datetime(2026, 8, 15, 3, 0))
    (backups["dir"] / "pytr_cookies.json").write_text("{}")

    assert len(list_backups()) == 1


# --- name validation / path traversal ----------------------------------------

@pytest.mark.parametrize("name", [
    "../../etc/passwd",
    "../pytr_cookies.json",
    "actual-backup-20260815-031500.zip/../../secret",
    "/etc/passwd",
    "pytr_cookies.json",
    "actual-backup-2026-08-15.zip",
    "",
])
def test_bad_names_are_rejected(name):
    """The name is the only guard against reaching files outside the backup
    directory, so anything that is not exactly a backup name must be refused."""
    assert is_valid_backup_name(name) is False


def test_a_real_backup_name_is_accepted():
    assert is_valid_backup_name("actual-backup-20260815-031500.zip") is True


def test_backup_path_refuses_traversal(backups):
    with pytest.raises(ValueError):
        backup_path("../pytr_cookies.json")


def test_backup_path_reports_missing_files(backups):
    with pytest.raises(FileNotFoundError):
        backup_path("actual-backup-20260101-000000.zip")


def test_delete_refuses_traversal(backups, tmp_path):
    victim = tmp_path / "pytr_cookies.json"
    victim.write_text("{}")

    with pytest.raises(ValueError):
        delete_backup("../pytr_cookies.json")

    assert victim.exists()


def test_delete_removes_the_backup(backups):
    moment = datetime.datetime(2026, 8, 15, 3, 0)
    backups["create"](moment)
    name = f"actual-backup-{moment:%Y%m%d-%H%M%S}.zip"

    delete_backup(name)

    assert not (backups["dir"] / name).exists()


# --- mock mode ---------------------------------------------------------------

def test_mock_mode_writes_nothing(monkeypatch, backups):
    monkeypatch.setattr(settings, "app_mode", "mock")

    result = backup.create_backup()

    assert result["status"] == "mocked"
    assert list(backups["dir"].iterdir()) == []