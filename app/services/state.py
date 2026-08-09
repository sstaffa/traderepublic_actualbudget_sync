import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.core.config import settings


def _state_path() -> Path:
    cookies_path = Path(settings.tr_cookies_file or "./pytr_cookies.json")
    if cookies_path.suffix:
        return cookies_path.parent / f"{cookies_path.stem}_sync_state.json"
    return cookies_path / "sync_state.json"


def load_state() -> dict[str, Any]:
    path = _state_path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def save_state(state: dict[str, Any]) -> None:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True))
    try:
        tmp.chmod(0o600)
    except Exception:
        pass
    tmp.replace(path)


def mark_sync_success(result: dict[str, Any], *, scheduled: bool) -> None:
    state = load_state()
    now = datetime.now(timezone.utc).isoformat()
    state["last_successful_sync_at"] = now
    state["last_sync_at"] = now
    state["last_sync_scheduled"] = scheduled
    state["last_sync_result"] = result
    state.pop("last_sync_error", None)
    save_state(state)


def mark_sync_failure(error: str, *, scheduled: bool) -> None:
    state = load_state()
    now = datetime.now(timezone.utc).isoformat()
    state["last_sync_at"] = now
    state["last_sync_scheduled"] = scheduled
    state["last_sync_error"] = error
    save_state(state)


def mark_depot_sync_success(result: dict[str, Any]) -> None:
    """Separate from mark_sync_success: depot-value adjustments run on their
    own schedule (DEPOT_SYNC_CRON) and must not affect the transaction sync's
    `last_successful_sync_at`, which the incremental cash-transaction sync
    uses as its `from_date` cursor."""
    state = load_state()
    now = datetime.now(timezone.utc).isoformat()
    state["last_depot_sync_at"] = now
    state["last_depot_sync_result"] = result
    state.pop("last_depot_sync_error", None)
    save_state(state)


def mark_depot_sync_failure(error: str) -> None:
    state = load_state()
    now = datetime.now(timezone.utc).isoformat()
    state["last_depot_sync_attempt_at"] = now
    state["last_depot_sync_error"] = error
    save_state(state)