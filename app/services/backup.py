"""Backups of the Actual Budget file.

Produces the same zip that "Export data" in the Actual UI creates: db.sqlite
plus metadata.json, restorable through _Import file_ -> _Actual_. Unlike the
transaction and depot syncs this needs no Trade Republic session, only access
to the Actual server, so it runs on its own daily schedule.

Retention follows a grandfather-father-son scheme: every backup of the last few
days, then one per week, then one per month. That keeps recent mistakes
recoverable at day granularity without the directory growing without bound.
"""

import datetime
import logging
import re
from pathlib import Path
from typing import Any, Dict, List

from app.core.config import settings
from app.core.i18n import tr

log = logging.getLogger(__name__)

BACKUP_PREFIX = "actual-backup-"
BACKUP_SUFFIX = ".zip"
# actual-backup-20260815-031500.zip
BACKUP_PATTERN = re.compile(
    rf"^{re.escape(BACKUP_PREFIX)}(\d{{8}})-(\d{{6}}){re.escape(BACKUP_SUFFIX)}$"
)


def backup_dir() -> Path:
    """Directory holding the backups.

    Defaults to a "backups" folder next to the pytr cookies, i.e. inside the
    /data volume, so backups survive container restarts and image rebuilds.
    """
    configured = (settings.backup_dir or "").strip()
    if configured:
        return Path(configured)
    cookies_path = Path(settings.tr_cookies_file or "./pytr_cookies.json")
    base_dir = cookies_path.parent if cookies_path.suffix else cookies_path
    return base_dir / "backups"


def _timestamp_of(path: Path) -> datetime.datetime | None:
    """Read the creation time from the file name.

    Using the name rather than the filesystem mtime keeps rotation stable even
    if files are copied around, which would reset mtime.
    """
    match = BACKUP_PATTERN.match(path.name)
    if not match:
        return None
    try:
        return datetime.datetime.strptime(f"{match.group(1)}{match.group(2)}", "%Y%m%d%H%M%S")
    except ValueError:
        return None


def is_valid_backup_name(name: str) -> bool:
    """Whether a name refers to a backup file.

    Also the guard for downloads: only names matching this pattern are served,
    which rules out path traversal ("../../etc/passwd") by construction rather
    than by trying to sanitise the input.
    """
    return bool(BACKUP_PATTERN.match(name))


def list_backups() -> List[Dict[str, Any]]:
    """All backups, newest first."""
    directory = backup_dir()
    if not directory.is_dir():
        return []

    entries = []
    for path in directory.iterdir():
        created = _timestamp_of(path)
        if created is None:
            continue
        try:
            size = path.stat().st_size
        except OSError:
            continue
        entries.append({
            "name": path.name,
            "created_at": created.isoformat(timespec="seconds"),
            "size_bytes": size,
        })

    entries.sort(key=lambda entry: entry["created_at"], reverse=True)
    return entries


def select_backups_to_keep(
    timestamps: List[datetime.datetime],
    keep_daily: int,
    keep_weekly: int,
    keep_monthly: int,
) -> set[datetime.datetime]:
    """Decide which backups survive rotation.

    Pure function over timestamps so the policy can be tested without touching
    the filesystem. Newest first: the most recent `keep_daily` are kept as-is,
    then the newest backup of each of the next `keep_weekly` calendar weeks,
    then the newest of each of the next `keep_monthly` calendar months.
    """
    ordered = sorted(timestamps, reverse=True)
    keep: set[datetime.datetime] = set(ordered[:keep_daily])

    seen_weeks: set[tuple[int, int]] = set()
    seen_months: set[tuple[int, int]] = set()

    # Weeks and months already covered by the daily backups must not use up a
    # weekly or monthly slot, otherwise a week's worth of history would be lost
    # the moment the daily window moves on.
    for moment in ordered[:keep_daily]:
        iso = moment.isocalendar()
        seen_weeks.add((iso[0], iso[1]))
        seen_months.add((moment.year, moment.month))

    weekly_kept = 0
    monthly_kept = 0

    for moment in ordered[keep_daily:]:
        iso = moment.isocalendar()
        week_key = (iso[0], iso[1])
        month_key = (moment.year, moment.month)

        if weekly_kept < keep_weekly and week_key not in seen_weeks:
            seen_weeks.add(week_key)
            seen_months.add(month_key)
            keep.add(moment)
            weekly_kept += 1
            continue

        if monthly_kept < keep_monthly and month_key not in seen_months:
            seen_months.add(month_key)
            keep.add(moment)
            monthly_kept += 1

    return keep


def rotate_backups(dry_run: bool = False) -> Dict[str, Any]:
    """Apply the retention policy and delete everything it does not cover."""
    directory = backup_dir()
    if not directory.is_dir():
        return {"kept": [], "deleted": [], "dry_run": dry_run}

    by_timestamp: Dict[datetime.datetime, Path] = {}
    for path in directory.iterdir():
        created = _timestamp_of(path)
        if created is not None:
            by_timestamp[created] = path

    keep = select_backups_to_keep(
        list(by_timestamp),
        settings.backup_keep_daily,
        settings.backup_keep_weekly,
        settings.backup_keep_monthly,
    )

    deleted = []
    for created, path in sorted(by_timestamp.items(), reverse=True):
        if created in keep:
            continue
        if not dry_run:
            try:
                path.unlink()
            except OSError as exc:
                log.warning("Could not delete old backup %s: %s", path, exc)
                continue
        deleted.append(path.name)

    kept = sorted((by_timestamp[moment].name for moment in keep), reverse=True)
    return {"kept": kept, "deleted": deleted, "dry_run": dry_run}


def create_backup() -> Dict[str, Any]:
    """Download the Actual budget and write it to the backup directory."""
    directory = backup_dir()
    created = datetime.datetime.now()
    filename = f"{BACKUP_PREFIX}{created:%Y%m%d-%H%M%S}{BACKUP_SUFFIX}"
    target = directory / filename

    if settings.app_mode == "mock":
        return {
            "status": "mocked",
            "name": filename,
            "size_bytes": 0,
            "created_at": created.isoformat(timespec="seconds"),
        }

    try:
        from actual import Actual
    except ImportError as e:
        raise NotImplementedError(tr("actual.package_required", error=e))

    if not settings.actual_url:
        raise NotImplementedError(tr("actual.setting_missing", setting="ACTUAL_URL"))

    directory.mkdir(parents=True, exist_ok=True)

    with Actual(
        base_url=settings.actual_url,
        password=settings.actual_password or None,
        file=settings.actual_budget_id or None,
        encryption_password=settings.actual_encryption_password or None,
    ) as actual:
        # export_data() compacts the copy first, which runs VACUUM through its
        # own sqlite connection; release the SQLAlchemy session so the two do
        # not contend for the same file. Only the downloaded copy is touched,
        # never the server.
        actual.session.close()
        actual.engine.dispose()
        actual.export_data(target)

    size = target.stat().st_size
    log.info("Wrote backup %s (%s bytes)", filename, size)

    rotation = rotate_backups()
    if rotation["deleted"]:
        log.info("Rotation removed %s old backup(s)", len(rotation["deleted"]))

    return {
        "status": "ok",
        "name": filename,
        "size_bytes": size,
        "created_at": created.isoformat(timespec="seconds"),
        "rotation": rotation,
    }


def backup_path(name: str) -> Path:
    """Resolve a backup name to a path, rejecting anything else."""
    if not is_valid_backup_name(name):
        raise ValueError(tr("backup.invalid_name"))
    path = backup_dir() / name
    if not path.is_file():
        raise FileNotFoundError(tr("backup.not_found", name=name))
    return path


def delete_backup(name: str) -> Dict[str, Any]:
    path = backup_path(name)
    path.unlink()
    log.info("Deleted backup %s", name)
    return {"status": "ok", "name": name}