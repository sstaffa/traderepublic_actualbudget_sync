import json
import os
from dataclasses import dataclass, field

try:
    from dotenv import load_dotenv
except Exception:
    load_dotenv = None

if load_dotenv is not None:
    load_dotenv()


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.lower() in ("true", "1", "yes", "on")


def _parse_csv_list(raw: str) -> list[str]:
    """Parse a comma-separated env value into an uppercased, de-duplicated list."""
    result: list[str] = []
    for part in (raw or "").split(","):
        value = part.strip().upper()
        if value and value not in result:
            result.append(value)
    return result


def _parse_sub_depots(raw: str) -> dict[str, list[str]]:
    """Parse ACTUAL_SUB_DEPOTS as JSON: {"<account name>": ["<ISIN>", ...], ...}.

    Extensible to any number of sub-accounts, each with any number of ISINs.
    ISINs are normalized to uppercase so lookups are case-insensitive.
    Positions matching a listed ISIN are excluded from the main depot account
    and tracked separately in the named sub-account instead.
    """
    raw = (raw or "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}
    if not isinstance(parsed, dict):
        return {}

    result: dict[str, list[str]] = {}
    for account_name, isins in parsed.items():
        if isinstance(isins, str):
            isins = [isins]
        if not isinstance(isins, (list, tuple)):
            continue
        result[str(account_name)] = [str(isin).strip().upper() for isin in isins if str(isin).strip()]
    return result


@dataclass
class Settings:
    app_mode: str = os.getenv("APP_MODE", "mock")
    tr_phone: str = os.getenv("TR_PHONE_NUMBER", "")
    tr_pin: str = os.getenv("TR_PIN", "")
    tr_cookies_file: str = os.getenv("TR_COOKIES_FILE", "./pytr_cookies.json")
    actual_url: str = os.getenv("ACTUAL_URL", "")
    actual_password: str = os.getenv("ACTUAL_PASSWORD", "")
    actual_encryption_password: str = os.getenv("ACTUAL_ENCRYPTION_PASSWORD", "")
    actual_budget_id: str = os.getenv("ACTUAL_BUDGET_ID", "")
    actual_account_name: str = os.getenv("ACTUAL_ACCOUNT_NAME", "")
    actual_cash_account_name: str = os.getenv(
        "ACTUAL_CASH_ACCOUNT_NAME",
        os.getenv("ACTUAL_ACCOUNT_NAME", "Trade Republic Cash"),
    )
    actual_depot_account_name: str = os.getenv("ACTUAL_DEPOT_ACCOUNT_NAME", "Trade Republic Depot")
    actual_cash_account_offbudget: bool = _env_bool("ACTUAL_CASH_ACCOUNT_OFFBUDGET", False)
    actual_depot_account_offbudget: bool = _env_bool("ACTUAL_DEPOT_ACCOUNT_OFFBUDGET", True)
    actual_transfer_account_name: str = os.getenv("ACTUAL_TRANSFER_ACCOUNT_NAME", "")
    sync_cron: str = os.getenv("SYNC_CRON", "0 1 * * *")
    basic_auth_username: str = os.getenv("BASIC_AUTH_USERNAME", "")
    basic_auth_password: str = os.getenv("BASIC_AUTH_PASSWORD", "")
    autocreate_transfer: bool = _env_bool("TR_AUTOCREATE_TRANSFER", False)
    transfer_match_days: int = int(os.getenv("TR_TRANSFER_MATCH_DAYS", "3"))
    transfer_match_tolerance_cents: int = int(os.getenv("TR_TRANSFER_MATCH_TOLERANCE_CENTS", "0"))
    include_status_in_notes: bool = _env_bool("INCLUDE_STATUS_IN_NOTES", False)
    include_raw_in_notes: bool = _env_bool("INCLUDE_RAW_IN_NOTES", False)
    run_rules_after_sync: bool = _env_bool("RUN_RULES_AFTER_SYNC", True)
    # Off by default: re-running every rule against every existing transaction
    # on each sync produces avoidable CRDT sync messages and database growth.
    run_rules_on_all_transactions: bool = _env_bool("RUN_RULES_ON_ALL_TRANSACTIONS", False)
    # Seed for the UI-editable blocklist. Once the user saves a selection in the
    # web UI it is persisted in the user-settings file and takes precedence.
    tr_excluded_event_types: list[str] = field(
        default_factory=lambda: _parse_csv_list(os.getenv("TR_EXCLUDED_EVENT_TYPES", ""))
    )
    # False (default) keeps the previous behaviour: a transaction deleted in
    # Actual is re-imported on the next sync. True treats soft-deleted
    # (tombstone=1) rows as "already imported", making manual deletions
    # permanent and preventing repeated re-insert/re-delete cycles.
    skip_tombstoned_duplicates: bool = _env_bool("TR_SKIP_TOMBSTONED_DUPLICATES", False)
    actual_sub_depots: dict[str, list[str]] = field(
        default_factory=lambda: _parse_sub_depots(os.getenv("ACTUAL_SUB_DEPOTS", ""))
    )
    actual_sub_depot_offbudget: bool = _env_bool("ACTUAL_SUB_DEPOT_OFFBUDGET", True)
    # Depot valuation sync: adjusts the main depot account + all ACTUAL_SUB_DEPOTS
    # accounts to their current market value. Does NOT touch cash accounts -
    # those are covered by the regular SYNC_CRON transaction sync.
    # DEPOT_SYNC_CRON is checked daily at the given time; the adjustment only
    # actually runs once DEPOT_SYNC_INTERVAL_DAYS have passed since the last run.
    depot_sync_cron: str = os.getenv("DEPOT_SYNC_CRON", "0 18 * * *")
    depot_sync_interval_days: int = int(os.getenv("DEPOT_SYNC_INTERVAL_DAYS", "30"))


settings = Settings()