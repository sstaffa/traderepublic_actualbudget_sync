import datetime
import logging
from decimal import Decimal
from typing import List, Dict, Any

from app.core.config import settings
from app.core.i18n import tr

log = logging.getLogger(__name__)

DEPOT_VALUATION_PAYEE = "TR Depotwert-Anpassung seit letzter Bewertung"
DEPOT_VALUATION_IMPORT_PREFIX = "tr-depot-valuation-adjustment:"


def _is_truthy(value: Any) -> bool:
    return str(value).lower() in {"1", "true", "yes", "on"}


def _configure_account_budget_status(account, offbudget: bool):
    account.offbudget = int(offbudget)
    return account


def _find_transaction_by_financial_id(session, imported_id: str | None):
    if not imported_id:
        return None
    try:
        from actual.database import Transactions
        from sqlmodel import select
    except ImportError:
        return None
    query = select(Transactions).where(Transactions.financial_id == imported_id)
    if not settings.skip_tombstoned_duplicates:
        # Default: only active rows count as duplicates, so a transaction
        # deleted in Actual is re-imported on the next sync.
        query = query.where(Transactions.tombstone == 0)
    return session.exec(query).first()




def _find_existing_linked_transfer_duplicate(
    session,
    transfer_account,
    target_account,
    date: datetime.date,
    amount_eur: float,
):
    """Find a transfer-account row already linked to the target account."""
    if transfer_account is None or target_account is None:
        return None
    try:
        from actual.database import Transactions
        from actual.utils.conversions import date_to_int, decimal_to_cents
        from sqlmodel import select
    except ImportError:
        return None

    target_amount = decimal_to_cents(Decimal(str(amount_eur)))
    counterpart_amount = -target_amount
    tolerance = max(0, settings.transfer_match_tolerance_cents)
    match_days = max(0, settings.transfer_match_days)
    start = date_to_int(date - datetime.timedelta(days=match_days))
    end = date_to_int(date + datetime.timedelta(days=match_days))

    counterparts = session.exec(
        select(Transactions)
        .where(Transactions.acct == transfer_account.id)
        .where(Transactions.amount >= counterpart_amount - tolerance)
        .where(Transactions.amount <= counterpart_amount + tolerance)
        .where(Transactions.date >= start)
        .where(Transactions.date <= end)
        .where(Transactions.transferred_id.is_not(None))
        .where(Transactions.tombstone == 0)
        .where(Transactions.is_parent == 0)
    ).all()
    for counterpart in counterparts:
        linked = session.exec(
            select(Transactions)
            .where(Transactions.id == counterpart.transferred_id)
            .where(Transactions.acct == target_account.id)
            .where(Transactions.amount >= target_amount - tolerance)
            .where(Transactions.amount <= target_amount + tolerance)
            .where(Transactions.tombstone == 0)
            .where(Transactions.is_parent == 0)
        ).first()
        if linked is not None:
            return linked
    return None



def _find_matching_transfer_counterpart(session, account, date: datetime.date, amount_eur: float):
    """Find an existing unlinked transaction that can become the other side of a transfer."""
    try:
        from actual.database import Transactions
        from actual.utils.conversions import date_to_int, decimal_to_cents
        from sqlmodel import select
    except ImportError:
        return None

    target_amount = decimal_to_cents(Decimal(str(amount_eur)))
    tolerance = max(0, settings.transfer_match_tolerance_cents)
    start = date_to_int(date - datetime.timedelta(days=max(0, settings.transfer_match_days)))
    end = date_to_int(date + datetime.timedelta(days=max(0, settings.transfer_match_days)))

    return session.exec(
        select(Transactions)
        .where(Transactions.acct == account.id)
        .where(Transactions.amount >= target_amount - tolerance)
        .where(Transactions.amount <= target_amount + tolerance)
        .where(Transactions.date >= start)
        .where(Transactions.date <= end)
        .where(Transactions.transferred_id.is_(None))
        .where(Transactions.tombstone == 0)
        .where(Transactions.is_parent == 0)
    ).first()


def _serialize_transfer_match(transaction, fallback_account_name: str | None = None) -> Dict[str, Any]:
    try:
        from actual.utils.conversions import cents_to_decimal, int_to_date
    except ImportError:
        cents_to_decimal = None
        int_to_date = None

    def relation_name(relation) -> str | None:
        return getattr(relation, "name", None) or getattr(relation, "transfer_acct", None)

    try:
        date_value = int_to_date(transaction.date).isoformat() if int_to_date else str(transaction.date)
    except Exception:
        date_value = str(getattr(transaction, "date", ""))

    try:
        amount_value = float(cents_to_decimal(transaction.amount)) if cents_to_decimal else (transaction.amount or 0) / 100
    except Exception:
        amount_value = (getattr(transaction, "amount", 0) or 0) / 100

    return {
        "id": getattr(transaction, "id", None),
        "date": date_value,
        "amount": amount_value,
        "account": relation_name(getattr(transaction, "account", None)) or fallback_account_name,
        "payee": relation_name(getattr(transaction, "payee", None)) or getattr(transaction, "imported_description", None),
        "notes": getattr(transaction, "notes", None),
        "financial_id": getattr(transaction, "financial_id", None),
        "imported_description": getattr(transaction, "imported_description", None),
    }


def _serialize_transaction_for_reset(transaction, fallback_account_name: str | None = None) -> Dict[str, Any]:
    serialized = _serialize_transfer_match(transaction, fallback_account_name)
    serialized.update({
        "transferred_id": getattr(transaction, "transferred_id", None),
        "tombstone": getattr(transaction, "tombstone", None),
    })
    return serialized


def _create_or_link_transfer(
    session,
    date: datetime.date,
    account,
    transfer_account,
    amount_eur: float,
    notes: str,
    imported_id: str | None,
    payee: str,
    cleared: bool,
    pending: bool,
    allow_create_pair: bool,
):
    from actual.queries import create_transaction, create_transaction_from_ids, create_transfer

    if amount_eur > 0:
        source_account = transfer_account
        dest_account = account
        transfer_amount = amount_eur
        main_amount = amount_eur
        counterpart_amount = -amount_eur
    else:
        source_account = account
        dest_account = transfer_account
        transfer_amount = abs(amount_eur)
        main_amount = amount_eur
        counterpart_amount = abs(amount_eur)

    existing_counterpart = _find_matching_transfer_counterpart(
        session,
        transfer_account,
        date,
        counterpart_amount,
    )

    if existing_counterpart is not None:
        main_tx = create_transaction_from_ids(
            session,
            date,
            account.id,
            transfer_account.payee.id,
            notes,
            None,
            main_amount,
            imported_id,
            cleared,
            payee,
            process_payee=False,
        )
        main_tx.pending = int(pending)
        existing_counterpart.transferred_id = main_tx.id
        main_tx.transferred_id = existing_counterpart.id
        existing_counterpart.payee_id = account.payee.id
        existing_counterpart.category_id = None
        existing_counterpart.notes = existing_counterpart.notes or notes
        existing_counterpart.cleared = int(cleared)
        existing_counterpart.pending = int(pending)
        if imported_id and not existing_counterpart.financial_id:
            existing_counterpart.financial_id = f"{imported_id}:counterpart"
        return main_tx, existing_counterpart, True
    if allow_create_pair:
        source_tx, dest_tx = create_transfer(
            session,
            date=date,
            source_account=source_account,
            dest_account=dest_account,
            amount=transfer_amount,
            notes=notes,
        )
        main_tx = dest_tx if amount_eur > 0 else source_tx
        counterpart_tx = source_tx if amount_eur > 0 else dest_tx
        main_tx.financial_id = imported_id
        counterpart_tx.financial_id = f"{imported_id}:counterpart" if imported_id else None
        main_tx.imported_description = payee
        counterpart_tx.imported_description = payee
        main_tx.cleared = int(cleared)
        counterpart_tx.cleared = int(cleared)
        main_tx.pending = int(pending)
        counterpart_tx.pending = int(pending)
        return main_tx, counterpart_tx, False

    main_tx = create_transaction(
        session,
        date=date,
        account=account,
        payee=payee,
        notes=notes,
        amount=main_amount,
        imported_id=imported_id,
        cleared=cleared,
        imported_payee=payee,
    )
    main_tx.pending = int(pending)
    main_tx.imported_description = payee
    return main_tx, None, False


def list_budget_files() -> List[Dict[str, Any]]:
    """Return budget files available on the Actual server."""
    try:
        from actual import Actual
    except ImportError as e:
        raise NotImplementedError(tr("actual.package_required", error=e))

    url = settings.actual_url
    password = settings.actual_password

    if not url:
        raise NotImplementedError(tr("actual.url_missing"))

    with Actual(base_url=url, password=password or None) as actual:
        files = actual.list_user_files()
        return [
            {
                "file_id": f.file_id,
                "name": f.name,
                "group_id": getattr(f, "group_id", None),
                "deleted": f.deleted,
                "encrypted": getattr(f, "encrypt_key_id", None) is not None,
            }
            for f in files.data
            if not f.deleted
        ]


def encrypt_budget() -> Dict[str, Any]:
    """Enable encryption for the configured Actual budget."""
    if settings.app_mode == "mock":
        return {"status": "mocked", "encrypted": True}

    try:
        from actual import Actual
    except ImportError as e:
        raise NotImplementedError(tr("actual.package_required", error=e))

    url = settings.actual_url
    password = settings.actual_password
    budget_id = settings.actual_budget_id
    encryption_password = settings.actual_encryption_password

    if not url:
        raise NotImplementedError(tr("actual.url_missing"))
    if not budget_id:
        raise NotImplementedError(tr("actual.budget_id_missing"))
    if not encryption_password:
        raise NotImplementedError(tr("actual.encryption_password_missing"))

    with Actual(
        base_url=url,
        password=password or None,
        file=budget_id,
        encryption_password=encryption_password,
    ) as actual:
        actual.encrypt(encryption_password)
        return {
            "status": "ok",
            "file_id": actual.file.file_id,
            "name": actual.file.name,
            "encrypted": True,
        }


def preview_import(transactions: List[Dict]) -> Dict[str, Any]:
    """Read-only preview for mapped transactions against Actual state."""
    external_transfers = [tx for tx in transactions if tx.get("transfer_kind") == "external"]
    depot_transfers = [tx for tx in transactions if tx.get("transfer_kind") == "depot"]
    cash_account_name = settings.actual_cash_account_name
    depot_account_name = settings.actual_depot_account_name
    transfer_account_name = settings.actual_transfer_account_name

    def planned_account_name(tx: Dict) -> str:
        return depot_account_name if tx.get("account_key") == "depot" else cash_account_name

    def add_count(bucket: Dict[str, int], key: str | None) -> None:
        bucket[key or "(unknown)"] = bucket.get(key or "(unknown)", 0) + 1

    by_event_type: Dict[str, int] = {}
    by_account: Dict[str, int] = {}
    for tx in transactions:
        add_count(by_event_type, tx.get("event_type"))
        add_count(by_account, planned_account_name(tx))

    if settings.app_mode == "mock":
        actions = {"mocked": len(transactions)}
        return {
            "status": "mocked",
            "total": len(transactions),
            "by_event_type": by_event_type,
            "by_account": by_account,
            "actions": actions,
            "external_transfers": len(external_transfers),
            "matched_existing_counterpart": 0,
            "unmatched_external_transfers": len(external_transfers),
            "depot_transfers": len(depot_transfers),
            "duplicates": 0,
            "transfer_account_configured": bool(settings.actual_transfer_account_name),
            "report": [
                {
                    "source_id": tx.get("source_id"),
                    "date": tx.get("date"),
                    "payee": tx.get("payee"),
                    "event_type": tx.get("event_type"),
                    "account": planned_account_name(tx),
                    "amount": tx.get("amount"),
                    "planned_action": "mocked",
                }
                for tx in transactions
            ],
        }

    try:
        from actual import Actual
        from actual.queries import get_account
    except ImportError as e:
        raise NotImplementedError(tr("actual.package_required", error=e))

    url = settings.actual_url
    password = settings.actual_password
    encryption_password = settings.actual_encryption_password
    budget_id = settings.actual_budget_id
    transfer_account_name = settings.actual_transfer_account_name

    if not url:
        raise NotImplementedError(tr("actual.setting_missing", setting="ACTUAL_URL"))

    matched = 0
    duplicates = 0
    report = []
    actions: Dict[str, int] = {}

    with Actual(
        base_url=url,
        password=password or None,
        file=budget_id or None,
        encryption_password=encryption_password or None,
    ) as actual:
        session = actual.session
        cash_account = get_account(session, cash_account_name)
        depot_account = get_account(session, depot_account_name)
        transfer_account = get_account(session, transfer_account_name) if transfer_account_name else None

        for tx in external_transfers:
            imported_id = tx.get("source_id")
            date = datetime.date.fromisoformat(tx["date"]) if tx.get("date") else None
            amount_eur = (tx.get("amount") or 0) / 100
            duplicate_match = _find_transaction_by_financial_id(session, imported_id)
            if duplicate_match is None and transfer_account is not None and date and amount_eur:
                duplicate_match = _find_existing_linked_transfer_duplicate(
                    session,
                    transfer_account,
                    depot_account if tx.get("account_key") == "depot" else cash_account,
                    date,
                    amount_eur,
                )
            duplicate = duplicate_match is not None
            if duplicate:
                duplicates += 1

            matched_counterpart = False
            actual_match = _serialize_transfer_match(duplicate_match) if duplicate_match else None
            searched_existing_counterpart = False
            if not duplicate and transfer_account is not None and date and amount_eur:
                searched_existing_counterpart = True
                counterpart_amount = -amount_eur if amount_eur > 0 else abs(amount_eur)
                counterpart = _find_matching_transfer_counterpart(
                    session,
                    transfer_account,
                    date,
                    counterpart_amount,
                )
                matched_counterpart = counterpart is not None
                if matched_counterpart:
                    matched += 1
                    actual_match = _serialize_transfer_match(counterpart, transfer_account_name)

            if duplicate:
                planned_action = "duplicate"
            elif matched_counterpart:
                planned_action = "link_existing_transfer"
            elif settings.autocreate_transfer and transfer_account is not None:
                planned_action = "create_external_transfer_pair"
            else:
                planned_action = "import_cash_without_counterpart"
            add_count(actions, planned_action)

            report.append({
                "source_id": imported_id,
                "date": tx.get("date"),
                "payee": tx.get("payee"),
                "event_type": tx.get("event_type"),
                "account": planned_account_name(tx),
                "transfer_account": transfer_account_name if tx.get("transfer_kind") == "external" else None,
                "amount": tx.get("amount"),
                "transfer_kind": tx.get("transfer_kind"),
                "planned_action": planned_action,
                "duplicate": duplicate,
                "searched_existing_counterpart": searched_existing_counterpart,
                "matched_existing_counterpart": matched_counterpart,
                "actual_match": actual_match,
            })

        for tx in depot_transfers:
            duplicate_match = _find_transaction_by_financial_id(session, tx.get("source_id"))
            duplicate = duplicate_match is not None
            if duplicate:
                duplicates += 1
            planned_action = "duplicate" if duplicate else "create_cash_depot_transfer"
            add_count(actions, planned_action)
            report.append({
                "source_id": tx.get("source_id"),
                "date": tx.get("date"),
                "payee": tx.get("payee"),
                "event_type": tx.get("event_type"),
                "account": cash_account_name,
                "transfer_account": depot_account_name,
                "amount": tx.get("amount"),
                "transfer_kind": tx.get("transfer_kind"),
                "planned_action": planned_action,
                "duplicate": duplicate,
                "matched_existing_counterpart": True,
                "actual_match": _serialize_transfer_match(duplicate_match) if duplicate_match else None,
            })

        for tx in transactions:
            if tx.get("transfer_kind") is not None:
                continue
            duplicate_match = _find_transaction_by_financial_id(session, tx.get("source_id"))
            duplicate = duplicate_match is not None
            if duplicate:
                duplicates += 1
            planned_action = "duplicate" if duplicate else "insert_transaction"
            add_count(actions, planned_action)
            report.append({
                "source_id": tx.get("source_id"),
                "date": tx.get("date"),
                "payee": tx.get("payee"),
                "event_type": tx.get("event_type"),
                "account": planned_account_name(tx),
                "amount": tx.get("amount"),
                "transfer_kind": None,
                "planned_action": planned_action,
                "duplicate": duplicate,
                "matched_existing_counterpart": False,
                "actual_match": _serialize_transfer_match(duplicate_match) if duplicate_match else None,
            })

    return {
        "status": "ok",
        "total": len(transactions),
        "by_event_type": by_event_type,
        "by_account": by_account,
        "actions": actions,
        "external_transfers": len(external_transfers),
        "matched_existing_counterpart": matched,
        "unmatched_external_transfers": max(0, len(external_transfers) - matched - duplicates),
        "depot_transfers": len(depot_transfers),
        "duplicates": duplicates,
        "transfer_account_configured": bool(transfer_account_name),
        "report": report,
    }


def reset_imported_transactions(dry_run: bool = True) -> Dict[str, Any]:
    """Delete imported Trade Republic rows and unlink matched external counterparts.

    Only transactions inside the configured Trade Republic cash/depot accounts are
    deleted. Transactions in other accounts are kept and merely detached when they
    point at one of the deleted rows.
    """
    if settings.app_mode == "mock":
        return {
            "status": "mocked",
            "dry_run": dry_run,
            "matched_for_delete": 0,
            "matched_for_unlink": 0,
            "deleted": 0,
            "unlinked": 0,
            "transactions": [],
            "counterparts": [],
        }

    try:
        from actual import Actual
        from actual.database import Transactions
        from actual.queries import get_account
        from sqlmodel import select
    except ImportError as e:
        raise NotImplementedError(tr("actual.package_required", error=e))

    url = settings.actual_url
    password = settings.actual_password
    encryption_password = settings.actual_encryption_password
    budget_id = settings.actual_budget_id

    if not url:
        raise NotImplementedError(tr("actual.setting_missing", setting="ACTUAL_URL"))

    with Actual(
        base_url=url,
        password=password or None,
        file=budget_id or None,
        encryption_password=encryption_password or None,
    ) as actual:
        session = actual.session
        configured_names = [
            name
            for name in {
                settings.actual_cash_account_name,
                settings.actual_depot_account_name,
            }
            if name
        ]
        accounts = []
        missing_accounts = []
        for name in configured_names:
            account = get_account(session, name)
            if account is None:
                missing_accounts.append(name)
            else:
                accounts.append(account)

        if not accounts:
            return {
                "status": "ok",
                "dry_run": dry_run,
                "matched_for_delete": 0,
                "matched_for_unlink": 0,
                "deleted": 0,
                "unlinked": 0,
                "accounts": [],
                "missing_accounts": missing_accounts,
                "transactions": [],
                "counterparts": [],
                "warnings": ["Keine konfigurierten Trade-Republic-Accounts in Actual gefunden."],
            }

        account_ids = {account.id for account in accounts}
        account_name_by_id = {account.id: account.name for account in accounts}
        txs = session.exec(
            select(Transactions)
            .where(Transactions.acct.in_(account_ids))
            .where(Transactions.tombstone == 0)
            .where(Transactions.financial_id.is_not(None))
            .where(Transactions.is_parent == 0)
        ).all()

        delete_ids = {tx.id for tx in txs}
        unique_counterparts = {}
        for tx in txs:
            if not tx.transferred_id:
                continue
            counterpart = session.get(Transactions, tx.transferred_id)
            if counterpart is None or counterpart.tombstone:
                continue
            if counterpart.id in delete_ids:
                continue
            unique_counterparts[counterpart.id] = counterpart

        result = {
            "status": "ok",
            "dry_run": dry_run,
            "matched_for_delete": len(txs),
            "matched_for_unlink": len(unique_counterparts),
            "deleted": 0 if dry_run else len(txs),
            "unlinked": 0 if dry_run else len(unique_counterparts),
            "accounts": [account.name for account in accounts],
            "missing_accounts": missing_accounts,
            "transactions": [
                _serialize_transaction_for_reset(tx, account_name_by_id.get(tx.acct))
                for tx in txs
            ],
            "counterparts": [
                _serialize_transaction_for_reset(counterpart)
                for counterpart in unique_counterparts.values()
            ],
            "warnings": [
                "Externe Gegenbuchungen werden nur entlinkt, nicht gelöscht. "
                "Payee/Kategorie/Notes, die beim ursprünglichen Match angepasst wurden, "
                "können nicht automatisch auf den alten Wert zurückgesetzt werden."
            ],
        }

        if dry_run:
            return result

        for counterpart in unique_counterparts.values():
            counterpart.transferred_id = None

        for tx in txs:
            tx.transferred_id = None
            tx.tombstone = 1

        actual.commit()
        return result


def _find_last_depot_valuation(session, depot_account):
    try:
        from actual.database import Payees, Transactions
        from sqlmodel import select
    except ImportError:
        return None

    return session.exec(
        select(Transactions)
        .outerjoin(Payees, Transactions.payee_id == Payees.id)
        .where(Transactions.acct == depot_account.id)
        .where(Transactions.tombstone == 0)
        .where(Transactions.is_parent == 0)
        .where(
            (Transactions.financial_id.startswith(DEPOT_VALUATION_IMPORT_PREFIX))
            | (Payees.name == DEPOT_VALUATION_PAYEE)
            | (Payees.name == "TR Market valuation adjustment")
        )
        .order_by(Transactions.date.desc(), Transactions.sort_order.desc())
    ).first()


def _valuation_date_or_none(transaction) -> str | None:
    if transaction is None:
        return None
    try:
        from actual.utils.conversions import int_to_date
        return int_to_date(transaction.date).isoformat()
    except Exception:
        return str(getattr(transaction, "date", "")) or None


def adjust_depot_balance(
    target_value_eur: float | int | str,
    date: str | None = None,
    dry_run: bool = False,
    account_name: str | None = None,
    offbudget: bool | None = None,
) -> Dict[str, Any]:
    """Preview or create one explicit depot valuation adjustment transaction.

    account_name/offbudget default to the main Trade Republic depot account so
    existing callers keep working unchanged. Pass a different account_name to
    adjust a sub-depot account instead (see adjust_sub_depot_balances).
    """
    account_name = account_name or settings.actual_depot_account_name
    offbudget = settings.actual_depot_account_offbudget if offbudget is None else offbudget

    target = Decimal(str(target_value_eur))
    adjustment_date = datetime.date.fromisoformat(date) if date else datetime.date.today()

    if settings.app_mode == "mock":
        return {
            "status": "mocked",
            "account": account_name,
            "payee": DEPOT_VALUATION_PAYEE,
            "date": adjustment_date.isoformat(),
            "last_valuation_date": None,
            "current_balance": 0.0,
            "target_balance": float(target),
            "delta": float(target),
            "dry_run": dry_run,
            "would_insert": target != 0,
            "inserted": not dry_run and target != 0,
        }

    try:
        from actual import Actual
        from actual.database import Transactions
        from actual.queries import create_transaction, get_or_create_account
        from actual.utils.conversions import cents_to_decimal, decimal_to_cents
        from sqlalchemy import func
        from sqlmodel import select
    except ImportError as e:
        raise NotImplementedError(tr("actual.package_required", error=e))

    url = settings.actual_url
    password = settings.actual_password
    encryption_password = settings.actual_encryption_password
    budget_id = settings.actual_budget_id

    if not url:
        raise NotImplementedError(tr("actual.setting_missing", setting="ACTUAL_URL"))
    if not account_name:
        raise NotImplementedError(tr("actual.setting_missing", setting="ACTUAL_DEPOT_ACCOUNT_NAME"))

    with Actual(
        base_url=url,
        password=password or None,
        file=budget_id or None,
        encryption_password=encryption_password or None,
    ) as actual:
        session = actual.session
        depot_account = _configure_account_budget_status(
            get_or_create_account(session, account_name),
            offbudget,
        )
        current_cents = session.exec(
            select(func.coalesce(func.sum(Transactions.amount), 0))
            .where(Transactions.acct == depot_account.id)
            .where(Transactions.tombstone == 0)
            .where(Transactions.is_parent == 0)
        ).one()
        last_valuation = _find_last_depot_valuation(session, depot_account)
        last_valuation_date = _valuation_date_or_none(last_valuation)
        target_cents = decimal_to_cents(target)
        delta_cents = target_cents - int(current_cents or 0)
        current = cents_to_decimal(current_cents)
        delta = cents_to_decimal(delta_cents)

        result = {
            "status": "ok",
            "account": depot_account.name,
            "payee": DEPOT_VALUATION_PAYEE,
            "date": adjustment_date.isoformat(),
            "last_valuation_date": last_valuation_date,
            "current_balance": float(current),
            "target_balance": float(cents_to_decimal(target_cents)),
            "delta": float(delta),
            "dry_run": dry_run,
            "would_insert": delta_cents != 0,
            "inserted": False,
        }

        if delta_cents == 0 or dry_run:
            return result

        notes = (
            "Trade Republic Depotwert-Anpassung\n"
            f"Actual balance before adjustment: {current}\n"
            f"Target Trade Republic depot value: {cents_to_decimal(target_cents)}\n"
            f"Adjustment delta: {delta}\n"
            f"Last depot valuation: {last_valuation_date or 'none'}\n"
            "Reason: Kursgewinn/-verlust seit letzter Depotbewertung oder sonstige Bewertungsdifferenz.\n"
            "This is an explicit market-value correction, not a Trade Republic cashflow."
        )
        tx = create_transaction(
            session,
            date=adjustment_date,
            account=depot_account,
            payee=DEPOT_VALUATION_PAYEE,
            notes=notes,
            amount=delta,
            imported_id=f"{DEPOT_VALUATION_IMPORT_PREFIX}{datetime.datetime.utcnow().isoformat()}",
            cleared=True,
            imported_payee=DEPOT_VALUATION_PAYEE,
        )
        tx.pending = 0
        actual.commit()
        result["inserted"] = True
        result["transaction_id"] = tx.id
        return result


def adjust_sub_depot_balances(
    sub_depot_values: Dict[str, float],
    date: str | None = None,
    dry_run: bool = False,
) -> Dict[str, Dict[str, Any]]:
    """Adjust every configured sub-depot account to its computed value.

    sub_depot_values is expected to come from fetch_depot_value()'s
    "sub_depot_values" field (account name -> EUR value of the ISINs assigned
    to it). Every account configured via ACTUAL_SUB_DEPOTS is adjusted, even
    if it currently has no matching positions (value 0.0), so it stays in
    sync rather than being silently skipped.
    """
    results: Dict[str, Dict[str, Any]] = {}
    for account_name in settings.actual_sub_depots:
        value = sub_depot_values.get(account_name, 0.0)
        results[account_name] = adjust_depot_balance(
            value,
            date=date,
            dry_run=dry_run,
            account_name=account_name,
            offbudget=settings.actual_sub_depot_offbudget,
        )
    return results


def reset_sync_and_compact() -> Dict[str, Any]:
    """Equivalent of "Reset sync" in the Actual frontend.

    Actual stores one base database plus an append-only list of CRDT change
    messages. Soft-deleted (tombstone=1) rows and their change history are only
    discarded when the file is cleaned and re-uploaded as a new base database,
    which is what actually shrinks the server-side SQLite file.

    Two steps, in this order:

    1. `Actual.cleanup()` runs the same statements as the frontend's reset.ts:
       it clears messages_crdt/messages_clock, deletes every tombstone=1 row
       and runs ANALYZE + VACUUM on the local copy.
    2. `Actual.reupload_budget()` resets the file on the server and uploads the
       cleaned copy as the new base.

    Deliberately manual (no schedule): every other client of this budget must
    re-download it afterwards, and changes made elsewhere during the operation
    are lost.
    """
    if settings.app_mode == "mock":
        return {
            "status": "mocked",
            "reset": False,
            "tombstoned_transactions": 0,
            "active_transactions": 0,
        }

    try:
        from actual import Actual
        from actual.database import Transactions
        from sqlalchemy import func
        from sqlmodel import select
    except ImportError as e:
        raise NotImplementedError(tr("actual.package_required", error=e))

    if not settings.actual_url:
        raise NotImplementedError(tr("actual.setting_missing", setting="ACTUAL_URL"))

    with Actual(
        base_url=settings.actual_url,
        password=settings.actual_password or None,
        file=settings.actual_budget_id or None,
        encryption_password=settings.actual_encryption_password or None,
    ) as actual:
        session = actual.session
        tombstoned = session.exec(
            select(func.count()).select_from(Transactions).where(Transactions.tombstone == 1)
        ).one()
        active = session.exec(
            select(func.count()).select_from(Transactions).where(Transactions.tombstone == 0)
        ).one()

        # cleanup() opens its own SQLite connection and runs VACUUM against the
        # same file, so release the SQLAlchemy session first to avoid lock
        # contention on the database.
        session.commit()
        session.close()
        actual.engine.dispose()

        actual.cleanup()
        actual.reupload_budget()

        return {
            "status": "ok",
            "reset": True,
            "tombstoned_transactions": int(tombstoned),
            "active_transactions": int(active),
        }


def push_transactions(transactions: List[Dict]) -> Dict:
    """Push mapped transactions to Actual Budget."""
    if settings.app_mode == "mock":
        return {
            "status": "mocked",
            "accepted": len(transactions),
            "report": [
                {
                    "source_id": tx.get("source_id"),
                    "date": tx.get("date"),
                    "payee": tx.get("payee"),
                    "event_type": tx.get("event_type"),
                    "action": "mocked",
                }
                for tx in transactions
            ],
        }

    if not transactions:
        return {"status": "ok", "inserted": 0, "skipped": 0, "report": []}

    try:
        from actual import Actual
        from actual.queries import get_or_create_account, reconcile_transaction, get_ruleset
    except ImportError as e:
        raise NotImplementedError(tr("actual.package_required", error=e))

    url = settings.actual_url
    password = settings.actual_password
    encryption_password = settings.actual_encryption_password
    budget_id = settings.actual_budget_id
    cash_account_name = settings.actual_cash_account_name
    depot_account_name = settings.actual_depot_account_name
    transfer_account_name = settings.actual_transfer_account_name

    if not url:
        raise NotImplementedError(tr("actual.setting_missing", setting="ACTUAL_URL"))
    if not cash_account_name:
        raise NotImplementedError(tr("actual.setting_missing", setting="ACTUAL_CASH_ACCOUNT_NAME"))

    inserted = 0
    skipped = 0
    duplicates = 0
    errors = []
    report = []

    try:
        with Actual(
            base_url=url,
            password=password or None,
            file=budget_id or None,
            encryption_password=encryption_password or None,
        ) as actual:
            session = actual.session
            cash_account = _configure_account_budget_status(
                get_or_create_account(session, cash_account_name),
                settings.actual_cash_account_offbudget,
            )
            depot_account = _configure_account_budget_status(
                get_or_create_account(session, depot_account_name),
                settings.actual_depot_account_offbudget,
            )
            transfer_account = (
                get_or_create_account(session, transfer_account_name)
                if transfer_account_name
                else None
            )
            already_matched = []
            newly_created_transactions = []

            for tx in transactions:
                date_str = tx.get("date") or ""
                if not date_str:
                    log.warning("Skipping transaction without a date: %s", tx)
                    skipped += 1
                    report.append({
                        "source_id": tx.get("source_id"),
                        "payee": tx.get("payee"),
                        "action": "skipped",
                        "reason": "missing date",
                    })
                    continue

                try:
                    date = datetime.date.fromisoformat(date_str)
                except ValueError:
                    log.warning("Skipping transaction with invalid date '%s'", date_str)
                    skipped += 1
                    report.append({
                        "source_id": tx.get("source_id"),
                        "date": date_str,
                        "payee": tx.get("payee"),
                        "action": "skipped",
                        "reason": "invalid date",
                    })
                    continue

                payee = tx.get("payee") or "(unknown)"
                notes = tx.get("memo") or ""
                # The mapper stores integer cents; actualpy expects floating-point euros.
                amount_eur = (tx.get("amount") or 0) / 100
                imported_id = tx.get("source_id") or None
                cleared = bool(tx.get("cleared"))
                pending = _is_truthy(tx.get("pending"))
                is_transfer = _is_truthy(tx.get("is_transfer"))
                transfer_kind = tx.get("transfer_kind")
                account = depot_account if tx.get("account_key") == "depot" else cash_account

                try:
                    if is_transfer and transfer_kind == "external" and transfer_account is not None and amount_eur:
                        duplicate_match = _find_transaction_by_financial_id(session, imported_id)
                        if duplicate_match is None:
                            duplicate_match = _find_existing_linked_transfer_duplicate(
                                session,
                                transfer_account,
                                account,
                                date,
                                amount_eur,
                            )
                        if duplicate_match is not None:
                            duplicates += 1
                            log.debug("Skipping duplicate transfer (imported_id=%s)", imported_id)
                            report.append({
                                "source_id": imported_id,
                                "date": date_str,
                                "payee": payee,
                                "event_type": tx.get("event_type"),
                                "transfer_kind": transfer_kind,
                                "action": "duplicate",
                            })
                            continue

                        result_tx, counterpart_tx, linked_existing = _create_or_link_transfer(
                            session,
                            date=date,
                            account=account,
                            transfer_account=transfer_account,
                            amount_eur=amount_eur,
                            notes=notes,
                            imported_id=imported_id,
                            payee=payee,
                            cleared=cleared,
                            pending=pending,
                            allow_create_pair=settings.autocreate_transfer,
                        )
                        inserted += 1
                        newly_created_transactions.append(result_tx)
                        if counterpart_tx is not None:
                            newly_created_transactions.append(counterpart_tx)
                        action = "linked_transfer" if linked_existing else (
                            "created_transfer" if counterpart_tx is not None else "imported_without_counterpart"
                        )
                        report.append({
                            "source_id": imported_id,
                            "date": date_str,
                            "payee": payee,
                            "event_type": tx.get("event_type"),
                            "account": account.name,
                            "transfer_account": transfer_account.name,
                            "transfer_kind": transfer_kind,
                            "amount": amount_eur,
                            "action": action,
                        })
                        if linked_existing:
                            log.info(
                                "Linked transfer to an existing transaction in the counter-account (imported_id=%s)",
                                imported_id,
                            )
                        continue

                    if is_transfer and transfer_kind == "depot" and amount_eur:
                        duplicate_match = _find_transaction_by_financial_id(session, imported_id)
                        if duplicate_match is not None:
                            duplicates += 1
                            log.debug("Skipping duplicate trade transfer (imported_id=%s)", imported_id)
                            report.append({
                                "source_id": imported_id,
                                "date": date_str,
                                "payee": payee,
                                "event_type": tx.get("event_type"),
                                "transfer_kind": transfer_kind,
                                "action": "duplicate",
                            })
                            continue

                        result_tx, counterpart_tx, linked_existing = _create_or_link_transfer(
                            session,
                            date=date,
                            account=cash_account,
                            transfer_account=depot_account,
                            amount_eur=amount_eur,
                            notes=notes,
                            imported_id=imported_id,
                            payee=payee,
                            cleared=cleared,
                            pending=pending,
                            allow_create_pair=True,
                        )
                        inserted += 1
                        newly_created_transactions.append(result_tx)
                        if counterpart_tx is not None:
                            newly_created_transactions.append(counterpart_tx)
                        report.append({
                            "source_id": imported_id,
                            "date": date_str,
                            "payee": payee,
                            "event_type": tx.get("event_type"),
                            "account": cash_account.name,
                            "transfer_account": depot_account.name,
                            "transfer_kind": transfer_kind,
                            "amount": amount_eur,
                            "action": "created_transfer",
                        })
                        continue

                    duplicate_match = _find_transaction_by_financial_id(session, imported_id)
                    if duplicate_match is not None:
                        duplicates += 1
                        log.debug("Skipping cross-source duplicate transaction (imported_id=%s)", imported_id)
                        report.append({
                            "source_id": imported_id,
                            "date": date_str,
                            "payee": payee,
                            "event_type": tx.get("event_type"),
                            "account": account.name,
                            "amount": amount_eur,
                            "action": "duplicate",
                        })
                        continue

                    result_tx = reconcile_transaction(
                        session,
                        date=date,
                        account=account,
                        payee=payee,
                        notes=notes,
                        amount=amount_eur,
                        imported_id=imported_id,
                        cleared=cleared,
                        imported_payee=payee,
                        update_existing=False,
                        already_matched=already_matched,
                    )
                    result_tx.pending = int(pending)
                    result_tx.cleared = int(cleared)
                    already_matched.append(result_tx)

                    # Transactions in session.new were just created; otherwise an
                    # existing transaction was reconciled as a duplicate.
                    if result_tx in session.new:
                        inserted += 1
                        newly_created_transactions.append(result_tx)
                        report.append({
                            "source_id": imported_id,
                            "date": date_str,
                            "payee": payee,
                            "event_type": tx.get("event_type"),
                            "account": account.name,
                            "amount": amount_eur,
                            "action": "inserted",
                        })
                    else:
                        duplicates += 1
                        log.debug("Skipping duplicate transaction (imported_id=%s)", imported_id)
                        report.append({
                            "source_id": imported_id,
                            "date": date_str,
                            "payee": payee,
                            "event_type": tx.get("event_type"),
                            "account": account.name,
                            "amount": amount_eur,
                            "action": "duplicate",
                        })

                except Exception as e:
                    log.error("Failed to reconcile transaction %s: %s", imported_id, e)
                    errors.append({"source_id": imported_id, "error": str(e)})
                    report.append({
                        "source_id": imported_id,
                        "date": date_str,
                        "payee": payee,
                        "event_type": tx.get("event_type"),
                        "action": "error",
                        "error": str(e),
                    })
                    skipped += 1

            if settings.run_rules_after_sync:
                ruleset = get_ruleset(session)
                if settings.run_rules_on_all_transactions:
                    # Apply rules to every active, previously imported transaction as well,
                    # not just the ones inserted in this run. This ensures rule changes
                    # (e.g. a new delete-transaction rule) also retroactively affect
                    # transactions that were already synced before the rule existed.
                    from actual.database import Transactions
                    from sqlmodel import select

                    rule_targets = session.exec(
                        select(Transactions)
                        .where(Transactions.financial_id.is_not(None))
                        .where(Transactions.tombstone == 0)
                        .where(Transactions.is_parent == 0)
                    ).all()
                else:
                    rule_targets = newly_created_transactions

                if rule_targets:
                    ruleset.run(rule_targets)

            actual.commit()

    except NotImplementedError:
        raise
    except Exception as e:
        # List files to provide a more useful diagnostic.
        available = []
        try:
            available = list_budget_files()
        except Exception:
            pass

        hint = ""
        if available:
            names = [f"{f['name']} (file_id={f['file_id']})" for f in available]
            hint = tr("actual.files_available", files=", ".join(names))
        else:
            hint = tr("actual.files_hint")

        raise NotImplementedError(tr("actual.connection_failed", hint=hint, error=e))

    result: Dict = {
        "status": "ok",
        "inserted": inserted,
        "skipped": skipped,
        "duplicates": duplicates,
        "report": report,
    }
    if errors:
        result["errors"] = errors
    return result