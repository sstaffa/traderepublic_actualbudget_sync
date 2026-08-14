from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from app.services import backup as backup_service
from app.core.config import settings
import asyncio
from typing import List
from app.models.schemas import PytrTransaction, ActualTransaction
from app.core.i18n import tr
from app.mapping.mapper import map_pytr_to_actual
from app.services.trade_republic import fetch_transactions as tr_fetch
from app.services.trade_republic import totp_available
from app.services.trade_republic import (
    fetch_all_transactions as tr_fetch_history,
    get_last_history_meta,
    start_login as tr_start_login,
    complete_login as tr_complete_login,
    complete_login_with_totp as tr_complete_login_with_totp,
    confirm_login as tr_confirm_login,
    prune_sessions as tr_prune_sessions,
    fetch_depot_value as tr_fetch_depot_value,
    get_login_status as tr_get_status,
    resend_login as tr_resend_login,
    TRRateLimitError,
)
from typing import Optional
from app.services.actual import push_transactions as actual_push
from app.services.actual import list_budget_files as actual_list_files
from app.services.actual import encrypt_budget as actual_encrypt_budget
from app.services.actual import preview_import as actual_preview_import
from app.services.actual import reset_imported_transactions as actual_reset_import
from app.services.actual import adjust_depot_balance as actual_adjust_depot_balance
from app.services.actual import adjust_sub_depot_balances as actual_adjust_sub_depot_balances
from app.services.actual import reset_sync_and_compact as actual_reset_sync
from app.mapping.event_types import EVENT_TYPE_GROUPS, get_excluded_event_types
from app.services.notify import notifications_enabled, notify
from app.services.scheduler import run_history_sync, run_scheduled_login, run_scheduled_sync
from app.services.state import mark_sync_failure, mark_sync_success

router = APIRouter()


def _serialize_models(items):
    serialized = []
    for item in items:
        if hasattr(item, "model_dump"):
            serialized.append(item.model_dump())
        else:
            serialized.append(item.dict())
    return serialized


@router.post("/tr/map", response_model=List[ActualTransaction])
async def map_preview(transactions: List[PytrTransaction]):
    """Return mapped transactions without sending them to Actual."""
    mapped = map_pytr_to_actual(_serialize_models(transactions))
    return mapped


@router.post("/tr/preview-import")
async def preview_import(transactions: List[ActualTransaction]):
    serialized = _serialize_models(transactions)
    try:
        return await asyncio.to_thread(actual_preview_import, serialized)
    except NotImplementedError as e:
        raise HTTPException(status_code=501, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/tr/fetch")
async def fetch_from_tr(payload: Optional[dict] = None):
    session_id = (payload or {}).get("session_id") or None
    try:
        txs = await asyncio.to_thread(tr_fetch, session_id)
    except NotImplementedError as e:
        raise HTTPException(status_code=501, detail=str(e))
    return {"count": len(txs), "transactions": txs}


@router.post("/tr/fetch-history")
async def fetch_history_from_tr(payload: Optional[dict] = None):
    payload = payload or {}
    session_id = payload.get("session_id") or None
    from_date = payload.get("from_date") or None
    to_date = payload.get("to_date") or None
    try:
        txs = await asyncio.to_thread(
            tr_fetch_history,
            session_id,
            from_date=from_date,
            to_date=to_date,
        )
    except NotImplementedError as e:
        raise HTTPException(status_code=501, detail=str(e))
    return {
        "count": len(txs),
        "transactions": txs,
        "fetch_meta": get_last_history_meta(),
    }


@router.post("/tr/push-mapped")
async def push_mapped_to_actual(transactions: List[ActualTransaction]):
    serialized = _serialize_models(transactions)
    try:
        pushed = await asyncio.to_thread(actual_push, serialized)
        response = {"mapped_count": len(serialized), "pushed": pushed}
        mark_sync_success(response, scheduled=False)
        return response
    except Exception as e:
        mark_sync_failure(str(e), scheduled=False)
        raise



@router.post("/tr/connect")
async def tr_connect():
    """Start Trade Republic authentication and return its session ID."""
    try:
        resp = await asyncio.to_thread(tr_start_login)
    except TRRateLimitError as e:
        headers = {"Retry-After": str(e.retry_after)} if e.retry_after else {}
        raise HTTPException(status_code=429, detail=str(e), headers=headers or None)
    except (RuntimeError, NotImplementedError) as e:
        raise HTTPException(status_code=500, detail=str(e))
    return resp


@router.post("/tr/complete")
async def tr_complete(payload: dict):
    """Complete Trade Republic authentication with a code and session ID."""
    code = payload.get("code") or payload.get("pin")
    session_id = payload.get("session_id")
    if not code:
        raise HTTPException(status_code=400, detail=tr("api.code_required"))
    try:
        resp = await asyncio.to_thread(tr_complete_login, code, session_id)
    except TRRateLimitError as e:
        headers = {"Retry-After": str(e.retry_after)} if e.retry_after else {}
        raise HTTPException(status_code=429, detail=str(e), headers=headers or None)
    except NotImplementedError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return resp


@router.post("/tr/confirm")
async def tr_confirm(payload: dict):
    """Wait for the login to be confirmed in the Trade Republic app (v2).

    Blocks until confirmed or the login window closes (~120s), so the client
    should allow for a long-running request rather than a quick round-trip.
    """
    session_id = payload.get("session_id")
    if not session_id:
        raise HTTPException(status_code=400, detail=tr("api.session_id_required"))
    try:
        resp = await asyncio.to_thread(tr_confirm_login, session_id)
    except TimeoutError as e:
        # 408 so the UI can offer a retry instead of showing a hard error.
        raise HTTPException(status_code=408, detail=str(e))
    except TRRateLimitError as e:
        headers = {"Retry-After": str(e.retry_after)} if e.retry_after else {}
        raise HTTPException(status_code=429, detail=str(e), headers=headers or None)
    except NotImplementedError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return resp


@router.post("/tr/totp")
async def tr_totp(payload: dict):
    """Answer an authenticator challenge with a locally generated code.

    Only works when TR_TOTP_SECRET is configured; without it, the code has to
    come from the authenticator app on the phone.
    """
    session_id = payload.get("session_id")
    if not session_id:
        raise HTTPException(status_code=400, detail=tr("api.session_id_required"))
    if not totp_available():
        raise HTTPException(status_code=501, detail=tr("tr.totp_secret_missing"))
    try:
        resp = await asyncio.to_thread(tr_complete_login_with_totp, session_id)
    except TRRateLimitError as e:
        headers = {"Retry-After": str(e.retry_after)} if e.retry_after else {}
        raise HTTPException(status_code=429, detail=str(e), headers=headers or None)
    except NotImplementedError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return resp


@router.post("/tr/resend")
async def tr_resend(payload: dict):
    """Resend the code for an initiated Trade Republic session."""
    session_id = payload.get("session_id")
    if not session_id:
        raise HTTPException(status_code=400, detail=tr("api.session_id_required"))
    try:
        resp = await asyncio.to_thread(tr_resend_login, session_id)
    except TRRateLimitError as e:
        headers = {"Retry-After": str(e.retry_after)} if e.retry_after else {}
        raise HTTPException(status_code=429, detail=str(e), headers=headers or None)
    except NotImplementedError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return resp


@router.get("/actual/files")
async def list_actual_files():
    """List budget files available on the Actual server."""
    try:
        files = await asyncio.to_thread(actual_list_files)
    except NotImplementedError as e:
        raise HTTPException(status_code=501, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return {"files": files}


@router.post("/actual/encrypt")
async def encrypt_actual_budget():
    """Enable encryption for the configured Actual budget."""
    try:
        result = await asyncio.to_thread(actual_encrypt_budget)
    except NotImplementedError as e:
        raise HTTPException(status_code=501, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return result


@router.post("/actual/reset-tr-import")
async def reset_actual_tr_import(payload: Optional[dict] = None):
    payload = payload or {}
    dry_run = bool(payload.get("dry_run", True))
    try:
        return await asyncio.to_thread(actual_reset_import, dry_run)
    except NotImplementedError as e:
        raise HTTPException(status_code=501, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/actual/depot-adjustment")
async def adjust_actual_depot(payload: dict):
    target_value = payload.get("target_value")
    if target_value in (None, ""):
        raise HTTPException(status_code=400, detail=tr("api.target_value_required"))
    date = payload.get("date") or None
    dry_run = bool(payload.get("dry_run", False))
    try:
        return await asyncio.to_thread(actual_adjust_depot_balance, target_value, date, dry_run)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except NotImplementedError as e:
        raise HTTPException(status_code=501, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/actual/sub-depot-adjustment")
async def adjust_actual_sub_depots(payload: Optional[dict] = None):
    """Adjust every account configured via ACTUAL_SUB_DEPOTS using fresh Trade
    Republic position data. Does NOT touch the main depot account, so it can
    be combined with a manually-overridden /actual/depot-adjustment call."""
    payload = payload or {}
    session_id = payload.get("session_id") or None
    date = payload.get("date") or None
    dry_run = bool(payload.get("dry_run", False))
    try:
        depot_summary = await asyncio.to_thread(tr_fetch_depot_value, session_id)
        return await asyncio.to_thread(
            actual_adjust_sub_depot_balances,
            depot_summary.get("sub_depot_values", {}),
            date,
            dry_run,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except NotImplementedError as e:
        raise HTTPException(status_code=501, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/actual/depot-adjustment-all")
async def adjust_actual_depot_all(payload: Optional[dict] = None):
    """Fetch the current TR depot value (already net of any configured
    ACTUAL_SUB_DEPOTS ISINs) and adjust the main depot account plus every
    configured sub-depot account in one call."""
    payload = payload or {}
    session_id = payload.get("session_id") or None
    date = payload.get("date") or None
    dry_run = bool(payload.get("dry_run", False))
    try:
        depot_summary = await asyncio.to_thread(tr_fetch_depot_value, session_id)
        main_result = await asyncio.to_thread(
            actual_adjust_depot_balance, depot_summary.get("depot_value", 0), date, dry_run
        )
        sub_results = await asyncio.to_thread(
            actual_adjust_sub_depot_balances,
            depot_summary.get("sub_depot_values", {}),
            date,
            dry_run,
        )
        return {"main": main_result, "sub_accounts": sub_results}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except NotImplementedError as e:
        raise HTTPException(status_code=501, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/tr/status")
async def tr_status():
    return tr_get_status()


@router.post("/tr/depot-value")
async def tr_depot_value(payload: Optional[dict] = None):
    session_id = (payload or {}).get("session_id") or None
    try:
        return await asyncio.to_thread(tr_fetch_depot_value, session_id)
    except NotImplementedError as e:
        raise HTTPException(status_code=502, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/tr/sync")
async def sync_to_actual(payload: Optional[dict] = None):
    """Fetch, map, and push transactions to Actual."""
    session_id = (payload or {}).get("session_id") or None
    try:
        txs = await asyncio.to_thread(tr_fetch, session_id)
        mapped = map_pytr_to_actual(txs)
        result = await asyncio.to_thread(actual_push, mapped)
        response = {"mapped_count": len(mapped), "pushed": result}
        mark_sync_success(response, scheduled=False)
        return response
    except Exception as e:
        mark_sync_failure(str(e), scheduled=False)
        raise


@router.post("/tr/sync-now")
async def sync_now():
    """Run the scheduler sync with its concurrency lock."""
    return await run_scheduled_sync()


@router.post("/tr/sync-history")
async def sync_history(payload: Optional[dict] = None):
    """Fetch paginated Trade Republic history, map it, and push it to Actual."""
    payload = payload or {}
    session_id = payload.get("session_id") or None
    from_date = payload.get("from_date") or None
    to_date = payload.get("to_date") or None
    return await run_history_sync(session_id, from_date=from_date, to_date=to_date)


# --- Event-type blocklist (read-only, configured via TR_EXCLUDED_EVENT_TYPES) -

@router.get("/settings/event-filters")
async def read_event_filters():
    """Static catalogue of known event types plus the blocklist in effect.

    The blocklist comes from TR_EXCLUDED_EVENT_TYPES and is read at startup,
    so changes require a container restart."""
    excluded = get_excluded_event_types()
    return {
        "excluded_event_types": excluded,
        "event_type_groups": EVENT_TYPE_GROUPS,
        "unknown_excluded": [
            event_type
            for event_type in excluded
            if event_type not in {e for group in EVENT_TYPE_GROUPS.values() for e in group}
        ],
    }


@router.post("/actual/reset-sync")
async def reset_actual_sync():
    """Clean the budget (drop soft-deleted rows and change history, VACUUM) and
    re-upload it as a new base file. Manual only - other clients must
    re-download the budget afterwards."""
    try:
        return await asyncio.to_thread(actual_reset_sync)
    except NotImplementedError as e:
        raise HTTPException(status_code=501, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/notify/test")
async def send_test_notification():
    """Send a test message to the configured Discord webhook, so the setup can
    be verified without waiting for a real failure."""
    if not notifications_enabled():
        raise HTTPException(status_code=501, detail=tr("api.notify_disabled"))
    delivered = await asyncio.to_thread(
        notify,
        "tr-sync test notification",
        "If you can read this, Discord notifications are configured correctly.",
    )
    if not delivered:
        raise HTTPException(status_code=502, detail=tr("api.notify_failed"))
    return {"status": "ok", "delivered": True}


@router.post("/tr/scheduled-login")
async def trigger_scheduled_login():
    """Run the scheduled login now: open a login window, wait for the tap in
    the Trade Republic app, then sync. Useful for testing the schedule without
    waiting for it, and to catch up after a missed window."""
    try:
        return await run_scheduled_login()
    except NotImplementedError as e:
        raise HTTPException(status_code=501, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# --- Backups of the Actual budget --------------------------------------------

@router.get("/backups")
async def read_backups():
    """All stored backups, newest first, plus the retention currently in force."""
    return {
        "backups": await asyncio.to_thread(backup_service.list_backups),
        "directory": str(backup_service.backup_dir()),
        "retention": {
            "daily": settings.backup_keep_daily,
            "weekly": settings.backup_keep_weekly,
            "monthly": settings.backup_keep_monthly,
        },
    }


@router.post("/backups")
async def create_backup_now():
    """Create a backup right away, then apply the retention policy."""
    try:
        return await asyncio.to_thread(backup_service.create_backup)
    except NotImplementedError as e:
        raise HTTPException(status_code=501, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/backups/{name}")
async def download_backup(name: str):
    """Download one backup.

    Only names matching the backup pattern are accepted, so a crafted name
    cannot reach files outside the backup directory.
    """
    try:
        path = await asyncio.to_thread(backup_service.backup_path, name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return FileResponse(path, media_type="application/zip", filename=name)


@router.delete("/backups/{name}")
async def remove_backup(name: str):
    try:
        return await asyncio.to_thread(backup_service.delete_backup, name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/tr/prune-sessions")
async def prune_tr_sessions(payload: Optional[dict] = None):
    """Remove old login sessions and their cookie files.

    Runs automatically at startup and after every successful login; this is for
    clearing out a backlog on demand. Pass {"dry_run": true} to preview.
    """
    payload = payload or {}
    dry_run = bool(payload.get("dry_run", False))
    try:
        return await asyncio.to_thread(tr_prune_sessions, None, dry_run)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))