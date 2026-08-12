import asyncio
import logging
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timedelta

from app.core.config import settings
from app.mapping.mapper import map_pytr_to_actual
from app.services.actual import adjust_depot_balance, adjust_sub_depot_balances, push_transactions
from app.services.notify import notify_sync_failure
from app.services.state import (
    load_state,
    mark_depot_sync_failure,
    mark_depot_sync_success,
    mark_sync_failure,
    mark_sync_success,
)
from app.services.trade_republic import (
    fetch_all_transactions,
    fetch_depot_value,
    fetch_transactions,
    get_last_history_meta,
)

log = logging.getLogger(__name__)

# Shared across the transaction sync and the depot sync: both write to the
# same Actual budget file, so only one should run at a time.
_sync_lock = asyncio.Lock()


@dataclass(frozen=True)
class CronSchedule:
    minutes: set[int]
    hours: set[int]
    days: set[int]
    months: set[int]
    weekdays: set[int]

    def matches(self, dt: datetime) -> bool:
        # Python: Monday=0. Cron: Sunday=0 or 7.
        cron_weekday = (dt.weekday() + 1) % 7
        return (
            dt.minute in self.minutes
            and dt.hour in self.hours
            and dt.day in self.days
            and dt.month in self.months
            and cron_weekday in self.weekdays
        )

    def next_after(self, dt: datetime) -> datetime:
        candidate = (dt + timedelta(minutes=1)).replace(second=0, microsecond=0)
        end = candidate + timedelta(days=366)
        while candidate <= end:
            if self.matches(candidate):
                return candidate
            candidate += timedelta(minutes=1)
        raise ValueError("No matching cron time found in the next year")


def _parse_cron_field(value: str, minimum: int, maximum: int, *, allow_7_as_0: bool = False) -> set[int]:
    values: set[int] = set()

    for part in value.split(","):
        part = part.strip()
        if not part:
            raise ValueError("empty cron field part")

        if "/" in part:
            base, step_text = part.split("/", 1)
            step = int(step_text)
            if step <= 0:
                raise ValueError("cron step must be greater than zero")
        else:
            base = part
            step = 1

        if base == "*":
            start, end = minimum, maximum
        elif "-" in base:
            start_text, end_text = base.split("-", 1)
            start, end = int(start_text), int(end_text)
        else:
            start = end = int(base)

        if start > end:
            raise ValueError("cron ranges must be ascending")
        if start < minimum or end > maximum:
            raise ValueError(f"cron value {start}-{end} outside {minimum}-{maximum}")

        for field_value in range(start, end + 1, step):
            values.add(0 if allow_7_as_0 and field_value == 7 else field_value)

    return values


def parse_cron(expr: str) -> CronSchedule:
    fields = expr.split()
    if len(fields) != 5:
        raise ValueError("Cron expression must have 5 fields: minute hour day month weekday")

    return CronSchedule(
        minutes=_parse_cron_field(fields[0], 0, 59),
        hours=_parse_cron_field(fields[1], 0, 23),
        days=_parse_cron_field(fields[2], 1, 31),
        months=_parse_cron_field(fields[3], 1, 12),
        weekdays=_parse_cron_field(fields[4], 0, 7, allow_7_as_0=True),
    )


# --- Transaction (cash) sync -------------------------------------------------

async def run_scheduled_sync() -> dict:
    state = load_state()
    from_date = (state.get("last_successful_sync_at") or "")[:10] or None
    if from_date:
        fetcher = lambda: (fetch_all_transactions(from_date=from_date), get_last_history_meta())
    else:
        fetcher = fetch_transactions
    return await _run_sync(fetcher, scheduled=True)


async def run_history_sync(
    session_id: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
) -> dict:
    return await _run_sync(
        lambda: (fetch_all_transactions(session_id, from_date=from_date, to_date=to_date), get_last_history_meta()),
        scheduled=False,
    )


async def _run_sync(fetcher, *, scheduled: bool = False) -> dict:
    if _sync_lock.locked():
        log.warning("Scheduled sync skipped because another sync is still running")
        return {"status": "skipped", "reason": "sync already running"}

    async with _sync_lock:
        try:
            fetched = await asyncio.to_thread(fetcher)
            fetch_meta = None
            if isinstance(fetched, tuple) and len(fetched) == 2:
                txs, fetch_meta = fetched
            else:
                txs = fetched
            mapped = map_pytr_to_actual(txs)
            pushed = await asyncio.to_thread(push_transactions, mapped)
            result = {"mapped_count": len(mapped), "pushed": pushed}
            if fetch_meta:
                result["fetch_meta"] = fetch_meta
            mark_sync_success(result, scheduled=scheduled)
            log.info("Sync completed: %s", result)
            return result
        except Exception as exc:
            mark_sync_failure(str(exc), scheduled=scheduled)
            if scheduled:
                # Only for unattended runs: a manual sync already surfaces the
                # error in the UI, so notifying there would just be noise.
                notify_sync_failure("scheduled transaction sync", exc)
            raise


async def scheduler_loop(cron_expr: str | None = None) -> None:
    cron_expr = settings.sync_cron if cron_expr is None else cron_expr
    cron_expr = (cron_expr or "").strip()
    if not cron_expr:
        log.info("Scheduled sync disabled because SYNC_CRON is empty")
        return

    schedule = parse_cron(cron_expr)
    log.info("Scheduled sync enabled with SYNC_CRON=%s", cron_expr)

    while True:
        now = datetime.now()
        next_run = schedule.next_after(now)
        delay = max(0.0, (next_run - now).total_seconds())
        log.info("Next scheduled sync at %s", next_run.isoformat(timespec="seconds"))
        await asyncio.sleep(delay)
        try:
            await run_scheduled_sync()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Scheduled sync failed")


# --- Depot valuation sync -----------------------------------------------------
# Adjusts the main depot account + every account configured via
# ACTUAL_SUB_DEPOTS to its current Trade Republic market value. Deliberately
# does NOT touch cash accounts - those are covered by the transaction sync
# above. Runs on its own DEPOT_SYNC_CRON schedule (checked daily by default),
# but only actually executes once DEPOT_SYNC_INTERVAL_DAYS have passed since
# the last successful depot sync, so "every 30 days" works without relying on
# non-standard cron syntax. The elapsed-time check is state-file based, so it
# survives container restarts/rebuilds.

def _depot_sync_due(interval_days: int) -> bool:
    state = load_state()
    last = state.get("last_depot_sync_at")
    if not last:
        return True
    try:
        last_dt = datetime.fromisoformat(last)
    except ValueError:
        return True
    if last_dt.tzinfo is not None:
        last_dt = last_dt.astimezone().replace(tzinfo=None)
    return (datetime.now() - last_dt) >= timedelta(days=interval_days)


async def run_scheduled_depot_sync(*, force: bool = False) -> dict:
    """Adjust the main depot account and all ACTUAL_SUB_DEPOTS accounts.

    force=True skips the DEPOT_SYNC_INTERVAL_DAYS gate (used for manual
    triggers/tests); the scheduler loop itself always respects the interval.
    """
    if not force and not _depot_sync_due(settings.depot_sync_interval_days):
        log.info(
            "Depot sync skipped: interval of %s day(s) not yet reached",
            settings.depot_sync_interval_days,
        )
        return {"status": "skipped", "reason": "interval not reached"}

    if _sync_lock.locked():
        log.warning("Depot sync skipped because another sync is still running")
        return {"status": "skipped", "reason": "sync already running"}

    async with _sync_lock:
        try:
            depot_summary = await asyncio.to_thread(fetch_depot_value)
            main_result = await asyncio.to_thread(
                adjust_depot_balance, depot_summary.get("depot_value", 0)
            )
            sub_results = await asyncio.to_thread(
                adjust_sub_depot_balances, depot_summary.get("sub_depot_values", {})
            )
            result = {"main": main_result, "sub_accounts": sub_results}
            mark_depot_sync_success(result)
            log.info("Depot sync completed: %s", result)
            return result
        except Exception as exc:
            mark_depot_sync_failure(str(exc))
            notify_sync_failure("depot valuation sync", exc)
            raise


async def depot_scheduler_loop(cron_expr: str | None = None) -> None:
    cron_expr = settings.depot_sync_cron if cron_expr is None else cron_expr
    cron_expr = (cron_expr or "").strip()
    if not cron_expr:
        log.info("Scheduled depot sync disabled because DEPOT_SYNC_CRON is empty")
        return

    schedule = parse_cron(cron_expr)
    log.info(
        "Scheduled depot sync enabled with DEPOT_SYNC_CRON=%s (every %s day(s))",
        cron_expr,
        settings.depot_sync_interval_days,
    )

    while True:
        now = datetime.now()
        next_run = schedule.next_after(now)
        delay = max(0.0, (next_run - now).total_seconds())
        log.info("Next depot-sync check at %s", next_run.isoformat(timespec="seconds"))
        await asyncio.sleep(delay)
        try:
            await run_scheduled_depot_sync()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Scheduled depot sync failed")


# --- Lifecycle ----------------------------------------------------------------

def start_scheduler() -> list[asyncio.Task]:
    tasks: list[asyncio.Task] = []

    if (settings.sync_cron or "").strip():
        tasks.append(asyncio.create_task(scheduler_loop()))
    else:
        log.info("Scheduled sync disabled because SYNC_CRON is empty")

    if (settings.depot_sync_cron or "").strip():
        tasks.append(asyncio.create_task(depot_scheduler_loop()))
    else:
        log.info("Scheduled depot sync disabled because DEPOT_SYNC_CRON is empty")

    return tasks


async def stop_scheduler(tasks: list[asyncio.Task] | asyncio.Task | None) -> None:
    if tasks is None:
        return
    if isinstance(tasks, asyncio.Task):
        tasks = [tasks]
    for task in tasks:
        task.cancel()
    for task in tasks:
        with suppress(asyncio.CancelledError):
            await task