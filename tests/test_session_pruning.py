import time

import pytest

from app.core.config import settings
from app.services import trade_republic
from app.services.trade_republic import prune_sessions


@pytest.fixture
def sessions(monkeypatch, tmp_path):
    """An isolated session store plus a helper to create session files."""
    monkeypatch.setattr(settings, "tr_cookies_file", str(tmp_path / "pytr_cookies.json"))
    monkeypatch.setattr(settings, "tr_session_retention_days", 2)
    monkeypatch.setattr(trade_republic, "SESSIONS", {})
    monkeypatch.setattr(trade_republic, "API_CLIENTS", {})
    monkeypatch.setattr(trade_republic, "_load_sessions", lambda: None)
    monkeypatch.setattr(trade_republic, "_save_sessions", lambda: None)

    directory = tmp_path / "pytr_cookies_sessions"
    directory.mkdir()

    def _add(session_id: str, status: str, age_days: float):
        path = directory / f"{session_id}.cookies.json"
        path.write_text("{}")
        stamp = time.time() - age_days * 86400
        import os

        os.utime(path, (stamp, stamp))
        trade_republic.SESSIONS[session_id] = {
            "status": status,
            "cookies_file": str(path),
        }
        return path

    return {"dir": directory, "add": _add}


UUID_A = "2017ed3f-9719-4cf4-ba7d-683ef157d987"
UUID_B = "20bdbaa2-9093-4026-8501-8f2dae04dfc2"
UUID_C = "2381de99-ada4-4046-ae1f-4272883e7ac4"


def test_old_sessions_are_removed(sessions):
    path = sessions["add"](UUID_A, "expired", age_days=5)

    result = prune_sessions()

    assert not path.exists()
    assert UUID_A in result["removed_sessions"]


def test_recent_sessions_are_kept(sessions):
    path = sessions["add"](UUID_A, "expired", age_days=0.5)

    prune_sessions()

    assert path.exists()


def test_the_connected_session_is_never_removed(sessions):
    """Trade Republic sessions can outlive the retention window; dropping the
    one in use would force an unnecessary login."""
    path = sessions["add"](UUID_A, "connected", age_days=30)

    result = prune_sessions()

    assert path.exists()
    assert UUID_A not in result["removed_sessions"]
    assert result["kept_session"] == UUID_A


def test_only_the_connected_one_survives_among_old_sessions(sessions):
    keep = sessions["add"](UUID_A, "connected", age_days=10)
    drop_a = sessions["add"](UUID_B, "expired", age_days=10)
    drop_b = sessions["add"](UUID_C, "error", age_days=10)

    prune_sessions()

    assert keep.exists()
    assert not drop_a.exists()
    assert not drop_b.exists()


def test_foreign_files_are_left_alone(sessions):
    """The data volume also holds cookies, the device id and the session store,
    so only files this module created may ever be deleted."""
    sessions["add"](UUID_A, "expired", age_days=10)
    stranger = sessions["dir"] / "notes.txt"
    stranger.write_text("keep me")
    import os

    old = time.time() - 30 * 86400
    os.utime(stranger, (old, old))

    prune_sessions()

    assert stranger.exists()


def test_dry_run_removes_nothing(sessions):
    path = sessions["add"](UUID_A, "expired", age_days=10)

    result = prune_sessions(dry_run=True)

    assert path.exists()
    assert UUID_A in trade_republic.SESSIONS
    assert result["removed_files"]


def test_negative_retention_disables_pruning(sessions, monkeypatch):
    monkeypatch.setattr(settings, "tr_session_retention_days", -1)
    path = sessions["add"](UUID_A, "expired", age_days=999)

    result = prune_sessions()

    assert path.exists()
    assert result["removed_files"] == []


def test_zero_retention_removes_everything_but_the_connected_one(sessions):
    keep = sessions["add"](UUID_A, "connected", age_days=1)
    drop = sessions["add"](UUID_B, "expired", age_days=1)

    prune_sessions(retention_days=0)

    assert keep.exists()
    assert not drop.exists()


def test_entries_without_a_file_are_dropped(sessions):
    """Left over from earlier manual cleanups: the entry lingers in the session
    store even though its file is long gone."""
    trade_republic.SESSIONS["orphan"] = {
        "status": "expired",
        "cookies_file": str(sessions["dir"] / "gone.cookies.json"),
    }

    result = prune_sessions()

    assert "orphan" in result["removed_sessions"]
    assert "orphan" not in trade_republic.SESSIONS


def test_api_clients_are_dropped_with_the_session(sessions):
    sessions["add"](UUID_A, "expired", age_days=10)
    trade_republic.API_CLIENTS[UUID_A] = object()

    prune_sessions()

    assert UUID_A not in trade_republic.API_CLIENTS


def test_missing_directory_is_harmless(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "tr_cookies_file", str(tmp_path / "pytr_cookies.json"))
    monkeypatch.setattr(trade_republic, "SESSIONS", {})
    monkeypatch.setattr(trade_republic, "_load_sessions", lambda: None)
    monkeypatch.setattr(trade_republic, "_save_sessions", lambda: None)

    result = prune_sessions()

    assert result["removed_files"] == []