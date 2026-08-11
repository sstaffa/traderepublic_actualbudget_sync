"""Known Trade Republic event types and the import blocklist.

The catalogue below is static and extended via commits, not at runtime. It is
derived from `pytr/event.py` (tr_event_type_mapping) plus event types that have
been observed in live timelines but are not mapped by pytr.

The blocklist itself is configured exclusively through the environment
(`TR_EXCLUDED_EVENT_TYPES`). It is re-read from the .env file on every call, so
changes take effect without restarting the container - see
`excluded_event_types_source()` for how the value is resolved.
"""

import logging
import os
from pathlib import Path

from app.core.config import settings

try:
    from dotenv import dotenv_values
except Exception:  # pragma: no cover - dotenv is a hard dependency in practice
    dotenv_values = None

log = logging.getLogger(__name__)

EXCLUDED_EVENT_TYPES_VAR = "TR_EXCLUDED_EVENT_TYPES"

# Grouped purely for display in the UI; grouping has no effect on filtering.
EVENT_TYPE_GROUPS: dict[str, list[str]] = {
    "deposits": [
        "ACCOUNT_TRANSFER_INCOMING",
        "BANK_TRANSACTION_INCOMING",
        "CARD_REFUND",
        "CARD_SUCCESSFUL_OCT",
        "CARD_TR_REFUND",
        "INCOMING_TRANSFER",
        "INCOMING_TRANSFER_DELEGATION",
        "PAYMENT_INBOUND",
        "PAYMENT_INBOUND_APPLE_PAY",
        "PAYMENT_INBOUND_CREDIT_CARD",
        "PAYMENT_INBOUND_GOOGLE_PAY",
        "PAYMENT_INBOUND_SEPA_DIRECT_DEBIT",
        "PAYMENT-SERVICE-IN-PAYMENT-DIRECT-DEBIT",
    ],
    "removals": [
        "BANK_TRANSACTION_OUTGOING",
        "CARD_FAILED_TRANSACTION",
        "CARD_ORDER_BILLED",
        "CARD_SUCCESSFUL_ATM_WITHDRAWAL",
        "CARD_SUCCESSFUL_TRANSACTION",
        "CARD_TRANSACTION",
        "JUNIOR_P2P_TRANSFER",
        "OUTGOING_TRANSFER",
        "OUTGOING_TRANSFER_DELEGATION",
        "PAYMENT_OUTBOUND",
    ],
    "interest": [
        "CREDIT",
        "INTEREST_PAYOUT",
        "INTEREST_PAYOUT_CREATED",
    ],
    "saveback": [
        "ACQUISITION_TRADE_PERK",
        "BENEFITS_SAVEBACK_EXECUTION",
        "SAVEBACK_AGGREGATE",
    ],
    "taxes": [
        "SSP_TAX_CORRECTION",
        "SSP_TAX_CORRECTION_INVOICE",
        "TAX_CORRECTION",
        "TAX_REFUND",
    ],
    "trades": [
        "BENEFITS_SPARE_CHANGE_EXECUTION",
        "IPO_TRADE_EXECUTED",
        "ORDER_EXECUTED",
        "SAVINGS_PLAN_EXECUTED",
        "SAVINGS_PLAN_INVOICE_CREATED",
        "SPARE_CHANGE_AGGREGATE",
        "TRADE_CORRECTED",
        "TRADE_INVOICE",
        "TRADING_SAVINGSPLAN_EXECUTED",
        "TRADING_TRADE_EXECUTED",
    ],
    "privateMarkets": [
        "PRIVATE_MARKET_FUND_TRADE_EXECUTED",
        "PRIVATE_MARKETS_ORDER_CREATED",
        "PRIVATE_MARKETS_TRADE_EXECUTED",
    ],
    "transfers": [
        "SSP_SECURITIES_TRANSFER_INCOMING",
    ],
    # Seen in live timelines but absent from pytr's mapping table.
    "other": [
        "CARD_OCT",
        "CARD_VERIFICATION",
        "SSP_CORPORATE_ACTION_CASH",
    ],
}

KNOWN_EVENT_TYPES: list[str] = sorted(
    {event_type for group in EVENT_TYPE_GROUPS.values() for event_type in group}
)


def normalize_event_types(values) -> list[str]:
    """Uppercase, strip and de-duplicate while preserving order."""
    if isinstance(values, str):
        values = values.split(",")
    if not isinstance(values, (list, tuple, set)):
        return []
    result: list[str] = []
    for value in values:
        normalized = str(value).strip().upper()
        if normalized and normalized not in result:
            result.append(normalized)
    return result


def env_file_path() -> Path:
    """Path of the .env file that is re-read at runtime.

    Override with TR_ENV_FILE if the file is mounted elsewhere.
    """
    return Path(os.getenv("TR_ENV_FILE", "/app/.env"))


def excluded_event_types_source() -> str:
    """Where the current blocklist comes from - shown in the UI so it is
    obvious whether live reloading is active."""
    path = env_file_path()
    if dotenv_values is not None and path.is_file():
        return f"env-file:{path}"
    return "environment"


def get_excluded_event_types() -> list[str]:
    """Event types that must never be imported into Actual (blocklist).

    Resolution order:

    1. If the .env file exists it is authoritative and re-read on every call,
       so edits apply without restarting the container. A missing key there
       means "exclude nothing", rather than silently falling back to the
       process environment, which would make removing a line look ineffective.
    2. Otherwise the value captured from the process environment at startup is
       used (the normal behaviour when only `env_file:` in compose is used).

    Unknown or newly introduced event types are always imported: this is a
    blocklist, so a change on Trade Republic's side can never silently drop
    transactions.
    """
    path = env_file_path()
    if dotenv_values is not None and path.is_file():
        try:
            values = dotenv_values(path)
            return normalize_event_types(values.get(EXCLUDED_EVENT_TYPES_VAR) or "")
        except Exception as exc:
            log.warning("Could not read %s from %s: %s", EXCLUDED_EVENT_TYPES_VAR, path, exc)
    return normalize_event_types(settings.tr_excluded_event_types)