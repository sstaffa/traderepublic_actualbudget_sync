from typing import List, Dict, Tuple, Any
from app.core.config import settings
from app.core.i18n import tr
from app.services.state import load_state
import asyncio
import uuid
import json
import logging
import re
from pathlib import Path
import threading
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP

log = logging.getLogger(__name__)

BOND_NAME_PATTERN = re.compile(
    r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec|January|February|March|April|May|June|July|August|September|October|November|December|Januar|Februar|März|April|Mai|Juni|Juli|August|September|Oktober|November|Dezember)\.?\s+20\d{2}",
    re.IGNORECASE,
)


# Simple in-memory session store; persisted next to TR_COOKIES_FILE for survivability.
SESSIONS = {}
SESSIONS_LOCK = threading.RLock()
API_CLIENTS = {}
LEGACY_SESSIONS_PATH = Path('/tmp/ab_tr_2_tr_sessions.json')
LAST_HISTORY_META: Dict = {}


def _normalize_phone_number(phone: str | None) -> str | None:
    """Normalize a phone number for the Trade Republic API.

    Keep E.164 numbers, remove common separators, and convert local French
    mobile formats to their international representation:
      - `06XXXXXXXX` -> `+336XXXXXXXX`
      - `6XXXXXXXX`  -> `+336XXXXXXXX`
    """
    if not phone:
        return phone

    cleaned = (
        str(phone)
        .strip()
        .replace(" ", "")
        .replace("-", "")
        .replace(".", "")
        .replace("(", "")
        .replace(")", "")
    )

    if cleaned.startswith("+"):
        return cleaned

    if cleaned.startswith("00"):
        return "+" + cleaned[2:]

    if cleaned.isdigit():
        # French mobile/local format support.
        if len(cleaned) == 10 and cleaned.startswith(("06", "07")):
            return "+33" + cleaned[1:]
        if len(cleaned) == 9 and cleaned.startswith(("6", "7")):
            return "+33" + cleaned

    return cleaned


def _atomic_write_json(path: Path, payload: Dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.parent.chmod(0o700)
    except Exception:
        pass
    tmp_path = path.with_suffix(path.suffix + '.tmp')
    tmp_path.write_text(json.dumps(payload))
    try:
        tmp_path.chmod(0o600)
    except Exception:
        pass
    tmp_path.replace(path)


def _sessions_dir() -> Path:
    cookies_path = Path(settings.tr_cookies_file or "./pytr_cookies.json")
    if cookies_path.suffix:
        return cookies_path.parent / f"{cookies_path.stem}_sessions"
    return cookies_path / "sessions"


def _sessions_path() -> Path:
    cookies_path = Path(settings.tr_cookies_file or "./pytr_cookies.json")
    if cookies_path.suffix:
        return cookies_path.parent / f"{cookies_path.stem}_sessions.json"
    return cookies_path / "sessions.json"


def _load_sessions():
    global SESSIONS
    with SESSIONS_LOCK:
        sessions_path = _sessions_path()
        source_path = sessions_path if sessions_path.exists() else LEGACY_SESSIONS_PATH
        if source_path.exists():
            try:
                SESSIONS = json.loads(source_path.read_text())
            except Exception:
                SESSIONS = {}
        else:
            SESSIONS = {}


def _save_sessions():
    with SESSIONS_LOCK:
        try:
            _atomic_write_json(_sessions_path(), SESSIONS)
        except Exception:
            pass


def _cookies_file_for_session(session_id: str) -> str:
    sessions_dir = _sessions_dir()
    sessions_dir.mkdir(parents=True, exist_ok=True)
    try:
        sessions_dir.chmod(0o700)
    except Exception:
        pass
    return str(sessions_dir / f'{session_id}.cookies.json')


def _init_session(session_id: str, status: str, message: str) -> Dict:
    data = {
        'status': status,
        'message': message,
        'cookies_file': _cookies_file_for_session(session_id),
    }
    with SESSIONS_LOCK:
        SESSIONS[session_id] = data
    return data


def _store_api_client(session_id: str, api):
    with SESSIONS_LOCK:
        API_CLIENTS[session_id] = api


def _get_api_client(session_id: str):
    with SESSIONS_LOCK:
        return API_CLIENTS.get(session_id)


def _build_api_client(cookies_file: str):
    from pytr.api import TradeRepublicApi

    try:
        return TradeRepublicApi(
            phone_no=_normalize_phone_number(settings.tr_phone) or None,
            pin=settings.tr_pin or None,
            save_cookies=True,
            cookies_file=cookies_file,
        )
    except Exception:
        return TradeRepublicApi()


def _load_cookies_into_client(api) -> bool:
    """Load cookies without calling settings during an active login challenge."""
    try:
        if api._save_cookies and api._cookies_file.exists():
            api._websession.cookies.load(ignore_discard=True)
            log.info("Loaded cookies from %s", api._cookies_file)
            return True
    except Exception as exc:
        log.warning("Could not load cookies from file: %s", exc)
    return False


def _mark_session_expired(session_id: str | None, message: str = "websession expired") -> None:
    if not session_id:
        return
    with SESSIONS_LOCK:
        session = SESSIONS.get(session_id)
        if session and session.get("status") == "connected":
            session.update({"status": "expired", "message": message})
    _save_sessions()


def _mark_session_connected(session_id: str | None, message: str = "websession valid", api=None) -> None:
    if not session_id:
        return
    with SESSIONS_LOCK:
        session = SESSIONS.get(session_id)
        if session:
            session.update({"status": "connected", "message": message})
    _save_sessions()
    if api is not None:
        try:
            api.save_websession()
        except Exception as exc:
            log.warning(
                "save_websession failed while persisting rotated cookies for session %s: %s",
                session_id,
                exc,
            )


class TRRateLimitError(Exception):
    """Raised when Trade Republic returns 429 Too Many Requests."""
    def __init__(self, message: str, retry_after: int | None = None):
        super().__init__(message)
        self.retry_after = retry_after


def _raise_if_rate_limited(exc: Exception):
    """Convert an HTTP 429 response into TRRateLimitError."""
    try:
        import requests
        if isinstance(exc, requests.exceptions.HTTPError):
            resp = exc.response
            if resp is not None and resp.status_code == 429:
                retry_after = None
                try:
                    retry_after = int(resp.headers.get("Retry-After", 0)) or None
                except Exception:
                    pass
                wait = str(retry_after // 60) if retry_after else tr("tr.wait_unknown")
                msg = tr("tr.rate_limited", wait=wait)
                raise TRRateLimitError(msg, retry_after=retry_after) from exc
    except TRRateLimitError:
        raise
    except Exception:
        pass



_SAMPLE = [
    {
        "id_externe": "1c263c75-45c6-5a7d-8ed3-8d43d445c180",
        "date": "2026-04-20T08:52:15.398+0000",
        "amount": "-4.87",
        "currency": "EUR",
        "type": "CARD",
        "category": "Expense",
        "status": "EXECUTED",
        "title": "Electra Paris",
        "subtitle": "",
        "instrument": {"isin": None, "name": "Electra Paris"},
        "raw": {
            "id": "1c263c75-45c6-5a7d-8ed3-8d43d445c180",
            "timestamp": "2026-04-20T08:52:15.398+0000",
            "title": "Electra Paris",
            "amount": {"currency": "EUR", "value": -4.87, "fractionDigits": 2},
            "status": "EXECUTED",
            "eventType": "CARD_TRANSACTION",
        },
    },
    {
        "id_externe": "af05b58b-1608-44fe-802f-ccf8123853f1",
        "date": "2026-04-16T14:44:36.978+0000",
        "amount": "-37.0",
        "currency": "EUR",
        "type": "BUY",
        "category": "Investment",
        "status": "EXECUTED",
        "title": "S&P 500 USD (Acc)",
        "subtitle": "Sparplan ausgeführt",
        "instrument": {"isin": "IE00B3YCGJ38", "name": "S&P 500 USD (Acc)"},
        "raw": {
            "id": "af05b58b-1608-44fe-802f-ccf8123853f1",
            "timestamp": "2026-04-16T14:44:36.978+0000",
            "title": "S&P 500 USD (Acc)",
            "amount": {"currency": "EUR", "value": -37.0, "fractionDigits": 2},
            "status": "EXECUTED",
            "eventType": "TRADING_SAVINGSPLAN_EXECUTED",
        },
    },
]


def _find_connected_session() -> tuple[str | None, str | None]:
    """Return the newest connected session and its cookie file."""
    with SESSIONS_LOCK:
        for sid, data in reversed(list(SESSIONS.items())):
            if data.get("status") == "connected":
                return sid, data.get("cookies_file") or _cookies_file_for_session(sid)
    return None, None


def _reset_api_async_state(api) -> None:
    """Reset API state that is tied to the event loop created by asyncio.run()."""
    import asyncio as _asyncio
    api._lock = _asyncio.Lock()
    api._ws = None
    api._subscription_id_counter = 1
    api._previous_responses = {}
    api.subscriptions = {}


def _decimal_from_money(value: Any) -> Decimal:
    if value is None:
        return Decimal("0")
    if isinstance(value, Decimal):
        return value
    if isinstance(value, dict):
        if "value" in value and "fractionDigits" in value:
            try:
                return Decimal(str(value["value"])) / (Decimal(10) ** int(value["fractionDigits"]))
            except Exception:
                return Decimal("0")
        for key in ("amount", "value", "netValue"):
            if key in value:
                return _decimal_from_money(value[key])
        return Decimal("0")
    if isinstance(value, str):
        normalized = value.strip().replace("€", "").replace(" ", "")
        if "," in normalized:
            normalized = normalized.replace(".", "").replace(",", ".")
        try:
            return Decimal(normalized)
        except Exception:
            return Decimal("0")
    try:
        return Decimal(str(value))
    except Exception:
        return Decimal("0")


def _extract_position_value(position: Dict[str, Any]) -> Decimal:
    for key in ("netValue", "currentValue", "marketValue", "value"):
        amount = _decimal_from_money(position.get(key))
        if amount:
            return amount

    size = _decimal_from_money(position.get("netSize") or position.get("size") or position.get("quantity"))
    price = _decimal_from_money(position.get("price") or position.get("lastPrice"))
    if size and price:
        return size * price

    return Decimal("0")


def _extract_depot_value(compact_portfolio: Dict | None) -> Dict:
    positions = []
    if isinstance(compact_portfolio, dict):
        positions = compact_portfolio.get("positions") or []
    total = Decimal("0")
    valued_positions = 0
    for position in positions:
        if not isinstance(position, dict):
            continue
        position_value = _extract_position_value(position)
        if position_value:
            valued_positions += 1
        total += position_value
    return {
        "currency": "EUR",
        "depot_value": float(total),
        "positions": len(positions),
        "valued_positions": valued_positions,
        "raw": compact_portfolio,
    }


def _position_value_breakdown(position: Dict[str, Any]) -> Dict[str, Any]:
    value = _extract_position_value(position)
    size = _decimal_from_money(
        position.get("netSize") or position.get("virtualSize") or position.get("size") or position.get("quantity")
    )
    price = _decimal_from_money(position.get("price") or position.get("lastPrice"))
    error = position.get("ticker_error") or position.get("instrumentDetails_error")
    if not value and not error:
        error = "missing_price"
    return {
        "instrument_id": position.get("instrumentId") or position.get("isin"),
        "name": position.get("name") or position.get("instrumentId") or position.get("isin"),
        "quantity": str(size),
        "price": float(price) if price else None,
        "value": float(value) if value else None,
        "valued": bool(value),
        "error": str(error) if error else None,
    }


def _as_position_payload(response: Any) -> Dict:
    def collect_positions(value: Any) -> List[Dict]:
        if isinstance(value, list):
            positions = []
            for item in value:
                positions.extend(collect_positions(item))
            return positions
        if not isinstance(value, dict):
            return []
        instrument_id = value.get("instrumentId") or value.get("isin")
        if instrument_id:
            return [{**value, "instrumentId": instrument_id}]

        direct = value.get("positions")
        if isinstance(direct, list):
            positions = collect_positions(direct)
            if positions:
                return positions

        positions = []
        for key, nested in value.items():
            if key != "positions":
                positions.extend(collect_positions(nested))
        return positions

    positions = collect_positions(response)
    if isinstance(response, dict):
        return {**response, "positions": positions}
    return {"positions": positions, "raw": response}


def _securities_account_number(api) -> str | None:
    sec_acc_no = getattr(api, "_sec_acc_no", None)
    if sec_acc_no:
        return str(sec_acc_no)

    settings_method = getattr(api, "settings", None)
    if not callable(settings_method):
        return None

    account_settings = settings_method()
    if isinstance(account_settings, dict):
        sec_acc_no = account_settings.get("securitiesAccountNumber")
    if not sec_acc_no:
        raise ValueError(tr("tr.securities_account_missing"))

    api._sec_acc_no = sec_acc_no
    return str(sec_acc_no)


async def _fetch_position_subscription(api, payload: Dict[str, Any]) -> Dict:
    subscription_id = await api.subscribe(payload)
    try:
        response = await _receive_subscription_response(api, subscription_id)
        return _as_position_payload(response)
    finally:
        await _unsubscribe_safely(api, subscription_id)


async def _fetch_compact_portfolio(api) -> Dict:
    sec_acc_no = _securities_account_number(api)
    if sec_acc_no and hasattr(api, "subscribe"):
        last_error = None
        received_empty_portfolio = False
        for topic in ("compactPortfolioByType", "compactPortfolio"):
            try:
                portfolio = await _fetch_position_subscription(
                    api,
                    {"type": topic, "secAccNo": sec_acc_no},
                )
                if portfolio.get("positions"):
                    return portfolio
                received_empty_portfolio = True
                log.warning(
                    "%s returned no recognizable positions (keys=%s)",
                    topic,
                    sorted(portfolio.keys()),
                )
            except Exception as exc:
                if "BAD_SUBSCRIPTION_TYPE" not in str(exc):
                    raise
                last_error = exc
                log.info("%s subscription rejected by Trade Republic: %s", topic, exc)

        if received_empty_portfolio:
            raise ValueError(tr("tr.portfolio_response_empty"))
        if last_error is not None:
            raise last_error

    # Compatibility with API wrappers that already implement the current payload.
    subscription_id = await api.compact_portfolio()
    try:
        response = await _receive_subscription_response(api, subscription_id)
        return _as_position_payload(response)
    finally:
        await _unsubscribe_safely(api, subscription_id)


async def _fetch_cash(api) -> Any:
    subscription_id = await api.cash()
    try:
        return await _receive_subscription_response(api, subscription_id)
    finally:
        await _unsubscribe_safely(api, subscription_id)


async def _fetch_instrument_details(api, isin: str) -> Dict:
    subscription_id = await api.instrument_details(isin)
    try:
        return await _receive_subscription_response(api, subscription_id)
    finally:
        await _unsubscribe_safely(api, subscription_id)


async def _fetch_ticker(api, isin: str, exchange: str = "LSX") -> Dict:
    subscription_id = await api.ticker(isin, exchange=exchange)
    try:
        return await _receive_subscription_response(api, subscription_id, timeout=10)
    finally:
        await _unsubscribe_safely(api, subscription_id)


def _ticker_price(ticker_response: Dict | None) -> Decimal:
    if not isinstance(ticker_response, dict):
        return Decimal("0")
    for path in (
        ("last", "price"),
        ("bid", "price"),
        ("ask", "price"),
        ("pre", "price"),
        ("open", "price"),
    ):
        value: Any = ticker_response
        for key in path:
            if not isinstance(value, dict):
                value = None
                break
            value = value.get(key)
        amount = _decimal_from_money(value)
        if amount:
            return amount
    return Decimal("0")


def _extract_cash_value(cash_response: Any) -> tuple[Decimal, str]:
    if isinstance(cash_response, list) and cash_response:
        first = cash_response[0]
        if isinstance(first, dict):
            return _decimal_from_money(first.get("amount")), first.get("currencyId") or first.get("currency") or "EUR"
    if isinstance(cash_response, dict):
        return _decimal_from_money(cash_response.get("amount") or cash_response.get("cash")), (
            cash_response.get("currencyId") or cash_response.get("currency") or "EUR"
        )
    return Decimal("0"), "EUR"


async def _fetch_depot_value_summary(api) -> Dict:
    compact = await _fetch_compact_portfolio(api)
    cash_response = await _fetch_cash(api)
    cash_value, currency = _extract_cash_value(cash_response)
    positions = compact.get("positions") if isinstance(compact, dict) else []
    if not isinstance(positions, list):
        positions = []

    positions = [
        position for position in positions
        if isinstance(position, dict) and position.get("instrumentId")
    ]

    detail_subscriptions = {}
    for position in positions:
        isin = position.get("instrumentId")
        try:
            subscription_id = await api.instrument_details(isin)
            detail_subscriptions[subscription_id] = position
        except Exception as exc:
            position["instrumentDetails_error"] = str(exc)

    while detail_subscriptions:
        try:
            subscription_id, subscription, response = await api.recv()
        except Exception as exc:
            for position in detail_subscriptions.values():
                position["instrumentDetails_error"] = str(exc)
            break
        if subscription.get("type") != "instrument":
            continue
        await _unsubscribe_safely(api, subscription_id)
        position = detail_subscriptions.pop(subscription_id, None)
        if position is None:
            continue
        position["instrumentDetails"] = response
        if isinstance(response, dict):
            position["name"] = response.get("shortName") or position.get("name")
            position["exchangeIds"] = response.get("exchangeIds") or position.get("exchangeIds") or []

    ticker_subscriptions = {}
    for position in positions:
        exchange_ids = position.get("exchangeIds") or []
        if not isinstance(exchange_ids, list) or not exchange_ids:
            continue
        try:
            subscription_id = await api.ticker(position["instrumentId"], exchange=exchange_ids[0])
            ticker_subscriptions[subscription_id] = position
        except Exception as exc:
            position["ticker_error"] = str(exc)

    while ticker_subscriptions:
        try:
            subscription_id, subscription, response = await asyncio.wait_for(api.recv(), 5)
        except asyncio.TimeoutError:
            for position in ticker_subscriptions.values():
                position["ticker_error"] = "Timed out waiting for ticker"
            break
        if subscription.get("type") != "ticker":
            continue
        await _unsubscribe_safely(api, subscription_id)
        position = ticker_subscriptions.pop(subscription_id, None)
        if position is None:
            continue
        position["ticker"] = response
        price = _ticker_price(response)
        if price:
            if BOND_NAME_PATTERN.search(str(position.get("name") or "")):
                price = price / Decimal("100")
            position["price"] = str(price)
            size = _decimal_from_money(position.get("netSize") or position.get("virtualSize"))
            if "netSize" not in position:
                position["netSize"] = "0"
                position["averageBuyIn"] = str(price)
            position["netValue"] = str((size * price).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))

    summary = _extract_depot_value({"positions": positions})
    buy_cost = Decimal("0")
    for position in positions:
        size = _decimal_from_money(position.get("netSize") or position.get("virtualSize"))
        avg = _decimal_from_money(position.get("averageBuyIn"))
        if size and avg:
            buy_cost += (size * avg).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    depot_value = Decimal(str(summary["depot_value"]))
    summary.update({
        "currency": currency,
        "cash_value": float(cash_value),
        "buy_cost": float(buy_cost),
        "total_buy_cost": float(cash_value + buy_cost),
        "total_value": float(cash_value + depot_value),
        "position_breakdown": [_position_value_breakdown(position) for position in positions],
        "raw": {
            "positions": positions,
            "cash": cash_response,
        },
    })
    return summary


def fetch_depot_value(session_id: str | None = None) -> Dict:
    """Fetch current Trade Republic depot market value from compactPortfolio."""
    if settings.app_mode == "mock":
        return {
            "status": "mocked",
            "currency": "EUR",
            "depot_value": 3000.0,
            "positions": 1,
            "valued_positions": 1,
            "position_breakdown": [{
                "instrument_id": "MOCK00000001",
                "name": "Mock-Position",
                "quantity": "1",
                "price": 3000.0,
                "value": 3000.0,
                "valued": True,
                "error": None,
            }],
        }

    _load_sessions()

    resolved_sid = session_id
    cookies_file = None

    if not resolved_sid:
        resolved_sid, cookies_file = _find_connected_session()
        if not resolved_sid:
            raise NotImplementedError(tr("tr.no_active_session_depot"))
        log.info("fetch_depot_value: using connected session %s", resolved_sid)

    session = SESSIONS.get(resolved_sid, {})
    cookies_file = cookies_file or session.get("cookies_file") or _cookies_file_for_session(resolved_sid)

    api = _get_api_client(resolved_sid)
    if api is None:
        log.warning("fetch_depot_value: rebuilding API client from cookies")
        api = _build_api_client(cookies_file)
        _store_api_client(resolved_sid, api)

    if not api.resume_websession():
        _mark_session_expired(resolved_sid)
        raise NotImplementedError(tr("tr.session_expired"))
    _mark_session_connected(resolved_sid, api=api)

    try:
        _reset_api_async_state(api)
        summary = asyncio.run(_fetch_depot_value_summary(api))
        summary["status"] = "ok"
        summary["session_id"] = resolved_sid
        return summary
    except Exception as e:
        raise NotImplementedError(tr("tr.depot_fetch_failed", error=e))


def fetch_transactions(session_id: str | None = None) -> List[Dict]:
    """Fetch transactions from Trade Republic using an authenticated session."""
    if settings.app_mode == "mock":
        return _SAMPLE

    _load_sessions()

    resolved_sid = session_id
    cookies_file = None

    if not resolved_sid:
        resolved_sid, cookies_file = _find_connected_session()
        if not resolved_sid:
            raise NotImplementedError(tr("tr.no_active_session_transactions"))
        log.info("fetch_transactions: using connected session %s", resolved_sid)

    session = SESSIONS.get(resolved_sid, {})
    cookies_file = cookies_file or session.get("cookies_file") or _cookies_file_for_session(resolved_sid)

    api = _get_api_client(resolved_sid)
    if api is None:
        log.warning("fetch_transactions: rebuilding API client from cookies")
        api = _build_api_client(cookies_file)
        _store_api_client(resolved_sid, api)

    # Authentication is complete, so resume_websession can safely call settings.
    if not api.resume_websession():
        _mark_session_expired(resolved_sid)
        raise NotImplementedError(tr("tr.session_expired"))
    _mark_session_connected(resolved_sid, api=api)

    # Reset state before each run_blocking/asyncio.run call because locks are loop-bound.
    _reset_api_async_state(api)

    try:
        response = api.run_blocking(api.timeline_transactions(), timeout=30)
        log.info("fetch_transactions: received response type=%s", type(response).__name__)

        if isinstance(response, dict):
            items = response.get("items", [])
        elif isinstance(response, list):
            items = response
        else:
            log.warning("fetch_transactions: unexpected response format: %s", response)
            items = []

        log.info("fetch_transactions: fetched %d transaction(s)", len(items))
        return _enrich_trade_details(api, items)

    except Exception as e:
        raise NotImplementedError(tr("tr.transactions_fetch_failed", error=e))


def _parse_filter_date(value: str | None) -> date | None:
    if not value:
        return None
    return date.fromisoformat(value[:10])


def _transaction_date(item: Dict) -> date | None:
    raw_value = item.get("date") or item.get("timestamp") or (item.get("raw") or {}).get("timestamp")
    if not raw_value:
        return None
    try:
        return datetime.fromisoformat(str(raw_value).replace("Z", "+00:00")).date()
    except Exception:
        try:
            return date.fromisoformat(str(raw_value)[:10])
        except Exception:
            return None


def _enrich_trade_details(api, items: List[Dict]) -> List[Dict]:
    for item in items:
        if item.get("eventType") != "TRADING_TRADE_EXECUTED":
            continue
        item_id = item.get("id")
        if not item_id:
            continue
        try:
            _reset_api_async_state(api)
            item["timelineDetailV2"] = api.run_blocking(api.timeline_detail_v2(item_id), timeout=30)
        except Exception as exc:
            item["timelineDetailV2_error"] = str(exc)
            log.warning("timelineDetailV2 failed for %s: %s", item_id, exc)
    return items


def get_last_history_meta() -> Dict:
    return dict(LAST_HISTORY_META)


def _set_last_history_meta(meta: Dict) -> None:
    global LAST_HISTORY_META
    LAST_HISTORY_META = dict(meta)


def _filter_timestamp(value: str | None, *, end_of_day: bool = False) -> float:
    parsed = _parse_filter_date(value)
    if parsed is None:
        return float("inf") if end_of_day else float(0)
    if end_of_day:
        parsed_dt = datetime.combine(parsed + timedelta(days=1), datetime.min.time())
    else:
        parsed_dt = datetime.combine(parsed, datetime.min.time())
    return parsed_dt.timestamp()


def _next_timeline_cursor(cursors: Dict | None) -> str | None:
    if not isinstance(cursors, dict):
        return None
    for key in ("after", "next", "before"):
        value = cursors.get(key)
        if value:
            return value
    return None


async def _receive_subscription_response(api, subscription_id: str, timeout: int = 30) -> Dict:
    while True:
        response_id, _subscription, payload = await asyncio.wait_for(api.recv(), timeout=timeout)
        if response_id == subscription_id:
            return payload


async def _unsubscribe_safely(api, subscription_id: str) -> None:
    try:
        await api.unsubscribe(subscription_id)
    except Exception as exc:
        log.debug("Timeline unsubscribe failed for %s: %s", subscription_id, exc)


async def _fetch_trade_detail(api, item_id: str) -> Dict:
    subscription_id = await api.timeline_detail_v2(item_id)
    try:
        return await _receive_subscription_response(api, subscription_id)
    finally:
        await _unsubscribe_safely(api, subscription_id)


async def _fetch_timeline_transactions_paginated(
    api,
    *,
    from_date: str | None = None,
    to_date: str | None = None,
    max_pages: int = 1000,
) -> Tuple[List[Dict], Dict]:
    start = _parse_filter_date(from_date)
    end = _parse_filter_date(to_date)
    cursor = None
    pages: list[Dict] = []
    items: list[Dict] = []
    seen_ids: set[str] = set()

    for page_number in range(1, max_pages + 1):
        subscription_id = await api.timeline_transactions(cursor)
        try:
            response = await _receive_subscription_response(api, subscription_id)
        finally:
            await _unsubscribe_safely(api, subscription_id)

        if not isinstance(response, dict):
            pages.append({
                "page": page_number,
                "cursor": cursor,
                "items": 0,
                "error": f"unexpected response type {type(response).__name__}",
            })
            break

        page_items = response.get("items") or []
        cursors = response.get("cursors") or {}
        next_cursor = _next_timeline_cursor(cursors)
        accepted = 0
        page_dates = []

        for item in page_items:
            tx_date = _transaction_date(item)
            if tx_date is not None:
                page_dates.append(tx_date)
            if tx_date is not None and start is not None and tx_date < start:
                continue
            if tx_date is not None and end is not None and tx_date > end:
                continue

            item_id = str(item.get("id") or item.get("id_externe") or "")
            if item_id and item_id in seen_ids:
                continue
            if item_id:
                seen_ids.add(item_id)
            items.append(item)
            accepted += 1

        pages.append({
            "page": page_number,
            "cursor": cursor,
            "items": len(page_items),
            "accepted": accepted,
            "next_cursor": bool(next_cursor),
            "cursor_keys": sorted(cursors.keys()),
        })

        if start is not None and page_dates and max(page_dates) < start:
            break
        if not next_cursor:
            break
        if next_cursor == cursor:
            log.warning("Timeline pagination stopped because cursor did not advance: %s", cursor)
            break
        cursor = next_cursor

    for item in items:
        if item.get("eventType") != "TRADING_TRADE_EXECUTED":
            continue
        item_id = item.get("id")
        if not item_id:
            continue
        try:
            item["timelineDetailV2"] = await _fetch_trade_detail(api, item_id)
        except Exception as exc:
            item["timelineDetailV2_error"] = str(exc)
            log.warning("timelineDetailV2 failed for %s: %s", item_id, exc)

    meta = {
        "from_date": from_date,
        "to_date": to_date,
        "pages_read": len(pages),
        "items_returned": len(items),
        "pages": pages,
    }
    return items, meta


def fetch_all_transactions(
    session_id: str | None = None,
    max_pages: int = 1000,
    from_date: str | None = None,
    to_date: str | None = None,
) -> List[Dict]:
    """Fetch all available history through timelineTransactions pagination."""
    if settings.app_mode == "mock":
        start = _parse_filter_date(from_date)
        end = _parse_filter_date(to_date)
        items = [
            item for item in _SAMPLE
            if ((tx_date := _transaction_date(item)) is None)
            or ((start is None or tx_date >= start) and (end is None or tx_date <= end))
        ]
        _set_last_history_meta({
            "from_date": from_date,
            "to_date": to_date,
            "pages_read": 1,
            "items_returned": len(items),
            "pages": [{"page": 1, "items": len(_SAMPLE), "accepted": len(items), "next_cursor": False}],
        })
        return items

    _load_sessions()

    resolved_sid = session_id
    cookies_file = None

    if not resolved_sid:
        resolved_sid, cookies_file = _find_connected_session()
        if not resolved_sid:
            raise NotImplementedError(tr("tr.no_active_session_transactions"))
        log.info("fetch_all_transactions: using connected session %s", resolved_sid)

    session = SESSIONS.get(resolved_sid, {})
    cookies_file = cookies_file or session.get("cookies_file") or _cookies_file_for_session(resolved_sid)

    api = _get_api_client(resolved_sid)
    if api is None:
        log.warning("fetch_all_transactions: rebuilding API client from cookies")
        api = _build_api_client(cookies_file)
        _store_api_client(resolved_sid, api)

    if not api.resume_websession():
        _mark_session_expired(resolved_sid)
        raise NotImplementedError(tr("tr.session_expired"))
    _mark_session_connected(resolved_sid, api=api)

    try:
        _reset_api_async_state(api)
        items, meta = asyncio.run(_fetch_timeline_transactions_paginated(
            api,
            from_date=from_date,
            to_date=to_date,
            max_pages=max_pages,
        ))
        _set_last_history_meta(meta)
        log.info(
            "fetch_all_transactions: fetched %d transaction(s) across %d page(s)",
            len(items),
            meta.get("pages_read"),
        )
        return items

    except Exception as e:
        raise NotImplementedError(tr("tr.history_fetch_failed", error=e))


def start_login() -> Dict:
    """Start the Trade Republic authentication flow."""
    _load_sessions()
    sid = str(uuid.uuid4())

    if settings.app_mode == "mock":
        _init_session(sid, "connected", tr("tr.mock_connected"))
        _save_sessions()
        return {"session_id": sid, "status": "connected", "message": tr("tr.mock_connected")}

    try:
        from pytr.api import TradeRepublicApi
    except Exception as e:
        raise NotImplementedError(tr("tr.pytr_missing", error=e))

    session = _init_session(sid, "pending", "started")
    cookies_file = session["cookies_file"]

    api = _build_api_client(cookies_file)
    _store_api_client(sid, api)
    _save_sessions()

    try:
        if hasattr(api, 'initiate_weblogin'):
            # initiate_weblogin is synchronous in pytr. It is already executed in a worker thread
            # from the FastAPI route, so call it directly once.
            countdown = api.initiate_weblogin()
            log.info("Web login initiated, process_id=%s, countdown=%s", api._process_id, countdown)
            # Save cookies right away so the fallback path (api lost from memory) can reload them.
            try:
                api.save_websession()
            except Exception as exc:
                log.warning("save_websession failed after initiate_weblogin: %s", exc)
            with SESSIONS_LOCK:
                SESSIONS[sid].update({
                    "status": "challenge",
                    "message": tr("tr.weblogin_initiated"),
                    "process_id": getattr(api, "_process_id", None),
                    "phone": _normalize_phone_number(settings.tr_phone) or None,
                    "countdown": countdown,
                })
            _save_sessions()
            return {
                "session_id": sid,
                "status": "challenge",
                "message": tr("tr.login_started"),
                "countdown_seconds": countdown,
            }
    except Exception as e:
        _raise_if_rate_limited(e)
        log.error("initiate_weblogin failed: %s", e, exc_info=True)
        with SESSIONS_LOCK:
            SESSIONS[sid].update({"status": "error", "message": str(e)})
        _save_sessions()
        raise RuntimeError(tr("tr.login_start_failed", error=e)) from e

    raise NotImplementedError(tr("tr.login_method_missing"))


def complete_login(code: str, session_id: str | None = None) -> Dict:
    """Complete Trade Republic authentication with a code and session ID."""
    _load_sessions()

    if settings.app_mode == "mock":
        sid = session_id or str(uuid.uuid4())
        if sid not in SESSIONS:
            _init_session(sid, "connected", tr("tr.mock_connected_code"))
        else:
            with SESSIONS_LOCK:
                SESSIONS[sid].update({"status": "connected", "message": tr("tr.mock_connected_code")})
        _save_sessions()
        return {"session_id": sid, "status": "connected", "message": tr("tr.mock_connected")}

    try:
        from pytr.api import TradeRepublicApi
    except Exception as e:
        raise NotImplementedError(tr("tr.pytr_missing", error=e))

    if not session_id:
        raise NotImplementedError(tr("tr.login_session_required"))

    session = SESSIONS.get(session_id)
    if not session:
        raise NotImplementedError(tr("tr.session_not_found"))

    cookies_file = session.get('cookies_file') or _cookies_file_for_session(session_id)

    api = _get_api_client(session_id)
    if api is None:
        log.warning(
            "API client missing from memory for session %s; rebuilding it from cookies",
            session_id,
        )
        api = _build_api_client(cookies_file)
        _store_api_client(session_id, api)
        # Load cookies directly. resume_websession calls settings, receives 401 during
        # the challenge, and clears the cookies required by complete_weblogin.
        _load_cookies_into_client(api)
    else:
        log.info("Reusing API client for session %s", session_id)

    process_id = session.get("process_id")
    if process_id:
        setattr(api, "_process_id", process_id)
        log.info("complete_login: process_id=%s, session_id=%s", process_id, session_id)
    else:
        raise NotImplementedError(tr("tr.invalid_session_process"))

    try:
        # During challenge completion, do NOT call resume_websession() — it calls settings()
        # which returns 401 and clears the cookies needed for complete_weblogin.
        if hasattr(api, 'complete_weblogin'):
            # complete_weblogin is synchronous in pytr and the route already runs in a worker thread.
            api.complete_weblogin(code)
            try:
                api.save_websession()
            except Exception as exc:
                log.warning("save_websession failed after complete_weblogin: %s", exc)
            log.info("complete_weblogin succeeded for session %s", session_id)
            with SESSIONS_LOCK:
                SESSIONS[session_id].update({"status": "connected", "message": tr("tr.weblogin_completed")})
            _save_sessions()
            return {
                "session_id": session_id,
                "status": SESSIONS[session_id]["status"],
                "message": SESSIONS[session_id]["message"],
            }
    except Exception as e:
        _raise_if_rate_limited(e)
        log.error("complete_weblogin failed for session %s: %s", session_id, e, exc_info=True)
        with SESSIONS_LOCK:
            SESSIONS[session_id].update({"status": "error", "message": str(e)})
        _save_sessions()
        raise NotImplementedError(tr("tr.login_complete_failed", error=e))

    raise NotImplementedError(tr("tr.login_complete_method_missing"))


def get_login_status() -> Dict:
    _load_sessions()
    sid, _cookies_file = _find_connected_session()
    validity = "none"
    if sid:
        validity = "mock" if settings.app_mode == "mock" else "unknown"
        if settings.app_mode != "mock":
            try:
                session = SESSIONS.get(sid, {})
                api = _get_api_client(sid)
                if api is None:
                    api = _build_api_client(session.get("cookies_file") or _cookies_file_for_session(sid))
                    _store_api_client(sid, api)
                if api.resume_websession():
                    validity = "valid"
                    _mark_session_connected(sid, api=api)
                else:
                    validity = "expired"
                    _mark_session_expired(sid)
            except Exception:
                validity = "expired"
                _mark_session_expired(sid)
    return {
        "current_session_id": sid,
        "session_validity": validity,
        "session_store": str(_sessions_path()),
        "sync_state": load_state(),
        "sessions": SESSIONS,
    }


def resend_login(session_id: str) -> Dict:
    """Resend the Trade Republic code for a challenge session."""
    _load_sessions()

    if settings.app_mode == "mock":
        return {"session_id": session_id, "status": "challenge", "message": tr("tr.mock_code_resent")}

    try:
        from pytr.api import TradeRepublicApi  # noqa: F401
    except Exception as e:
        raise NotImplementedError(tr("tr.pytr_missing", error=e))

    session = SESSIONS.get(session_id)
    if not session:
        raise NotImplementedError(tr("tr.session_not_found"))

    if session.get("status") != "challenge":
        raise NotImplementedError(tr("tr.session_not_waiting", status=session.get("status")))

    api = _get_api_client(session_id)
    if api is None:
        cookies_file = session.get("cookies_file") or _cookies_file_for_session(session_id)
        api = _build_api_client(cookies_file)
        _store_api_client(session_id, api)
        _load_cookies_into_client(api)

    process_id = session.get("process_id")
    if process_id:
        setattr(api, "_process_id", process_id)
    else:
        raise NotImplementedError(tr("tr.invalid_session_process"))

    try:
        if hasattr(api, 'resend_weblogin'):
            api.resend_weblogin()
            log.info("resend_weblogin succeeded for session %s", session_id)
            return {"session_id": session_id, "status": "challenge", "message": tr("tr.code_resent")}
    except Exception as e:
        _raise_if_rate_limited(e)
        log.error("resend_weblogin failed for session %s: %s", session_id, e, exc_info=True)
        raise NotImplementedError(tr("tr.code_resend_failed", error=e))

    raise NotImplementedError(tr("tr.code_resend_method_missing"))