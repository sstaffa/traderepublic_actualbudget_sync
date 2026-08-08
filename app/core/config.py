import os
from dataclasses import dataclass

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


settings = Settings()
