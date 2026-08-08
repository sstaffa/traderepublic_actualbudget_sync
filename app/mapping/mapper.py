from typing import List, Dict, Any
from datetime import datetime
from app.core.config import settings
import json


def _parse_date(tx: Dict[str, Any]) -> str:
    """Extract and format the date from a real or mocked Trade Republic item."""
    # Mock format uses date; the API format uses timestamp.
    iso_str = tx.get("date") or tx.get("timestamp") or (tx.get("raw") or {}).get("timestamp") or ""
    if not iso_str:
        return ""
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        return dt.date().isoformat()
    except Exception:
        return iso_str[:10]


def _parse_amount(tx: Dict[str, Any]) -> int:
    """Return the transaction amount as integer cents."""
    value = None

    # Real API format.
    amount_field = tx.get("amount")
    if isinstance(amount_field, dict):
        value = amount_field.get("value")

    # Preprocessed mock format.
    if value is None:
        if isinstance(amount_field, (int, float)):
            value = float(amount_field)
        elif isinstance(amount_field, str):
            normalized = (
                amount_field.strip()
                .replace("€", "")
                .replace(" ", "")
            )

            # German decimal format.
            if "," in normalized:
                normalized = normalized.replace(".", "").replace(",", ".")
            try:
                value = float(normalized)
            except Exception:
                value = 0.0

    # Fallback to raw.amount.value.
    if value is None:
        raw = tx.get("raw") or {}
        raw_amount = raw.get("amount") if isinstance(raw, dict) else None
        if isinstance(raw_amount, dict):
            value = raw_amount.get("value")

    try:
        return int(round(float(value) * 100))
    except Exception:
        return 0


def _extract_currency(tx: Dict[str, Any]) -> str:
    """Extract the currency from an item."""
    # Real API format.
    amount_field = tx.get("amount")
    if isinstance(amount_field, dict):
        return amount_field.get("currency") or "EUR"
    # Mock format.
    if tx.get("currency"):
        return tx["currency"]
    # Fallback raw
    raw = tx.get("raw") or {}
    if isinstance(raw, dict) and isinstance(raw.get("amount"), dict):
        return raw["amount"].get("currency") or "EUR"
    return "EUR"


def _extract_payee(tx: Dict[str, Any]) -> str:
    title = tx.get("title") or ""
    if title:
        return title
    raw = tx.get("raw") or {}
    if isinstance(raw, dict):
        return raw.get("title") or ""
    return ""


def _extract_source_id(tx: Dict[str, Any]) -> str | None:
    """Extract the source ID from real and mocked payloads."""
    return (
        tx.get("id_externe")
        or tx.get("id")
        or ((tx.get("raw") or {}).get("id") if isinstance(tx.get("raw"), dict) else None)
    )


def _extract_event_type(tx: Dict[str, Any]) -> str:
    raw = tx.get("raw") or {}
    raw_event_type = raw.get("eventType") if isinstance(raw, dict) else None
    return tx.get("eventType") or raw_event_type or tx.get("type") or ""


def _classify_event(event_type: str) -> tuple[str, str | None]:
    if event_type in {"BANK_TRANSACTION_INCOMING", "BANK_TRANSACTION_OUTGOING"}:
        return "cash", "external"
    if event_type == "TRADING_TRADE_EXECUTED":
        return "cash", "depot"
    return "cash", None


def _build_memo(tx: Dict[str, Any], event_type: str, status: str, raw: Dict[str, Any] | None = None) -> str:
    parts = []
    subtitle = tx.get("subtitle")
    if subtitle:
        parts.append(str(subtitle))
    if event_type:
        parts.append(f"eventType: {event_type}")

    if settings.include_status_in_notes and status:
        parts.append(f"TR status: {status}")

    if settings.include_raw_in_notes:
        payload = raw if raw is not None else tx
        try:
            details = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
        except Exception:
            details = str(payload)
        parts.append("Trade Republic raw: " + details)
    return "\n".join(parts)


def map_pytr_to_actual(transactions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Map real or mocked Trade Republic items to the Actual schema."""
    out = []
    for tx in transactions:
        status = (tx.get("status") or "").upper()
        if status and status not in {"EXECUTED", "PENDING"}:
            continue

        date = _parse_date(tx)
        amount = _parse_amount(tx)
        payee = _extract_payee(tx) or "(unknown)"
        source_id = _extract_source_id(tx)
        currency = _extract_currency(tx)
        event_type = _extract_event_type(tx)
        account_key, transfer_kind = _classify_event(event_type)
        pending = status == "PENDING"
        cleared = status == "EXECUTED"

        out.append({
            "date": date,
            "payee": payee,
            "amount": amount,
            "currency": currency,
            "memo": _build_memo(tx, event_type, status, raw=tx.get("raw")),
            "source_id": source_id,
            "event_type": event_type,
            "cleared": cleared,
            "pending": pending,
            "is_transfer": transfer_kind is not None,
            "account_key": account_key,
            "transfer_kind": transfer_kind,
        })
    return out
