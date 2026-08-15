import datetime

from actual.database import Accounts, Transactions
from actual.utils.conversions import date_to_int
from sqlmodel import SQLModel, Session, create_engine

from app.services.actual import (
    adjust_depot_balance,
    _find_existing_linked_transfer_duplicate,
)
from app.core.config import settings


def test_depot_adjustment_dry_run_does_not_insert():
    original_mode = settings.app_mode
    settings.app_mode = "mock"
    try:
        result = adjust_depot_balance("3000.00", dry_run=True)
    finally:
        settings.app_mode = original_mode

    assert result["dry_run"] is True
    assert result["would_insert"] is True
    assert result["delta"] == 3000.0
    assert result["payee"] == "TR Depotwert-Anpassung seit letzter Bewertung"
    assert result["inserted"] is False



def test_existing_linked_transfer_is_detected_across_booking_dates():
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)

    with Session(engine) as session:
        cash = Accounts(id="cash", name="Trade Republic Cash", offbudget=0, closed=0)
        bank = Accounts(id="bank", name="DKB", offbudget=0, closed=0)
        cash_side = Transactions(
            id="cash-side",
            acct=cash.id,
            date=date_to_int(datetime.date(2026, 6, 2)),
            amount=60000,
            transferred_id="bank-side",
            tombstone=0,
            is_parent=0,
        )
        bank_side = Transactions(
            id="bank-side",
            acct=bank.id,
            date=date_to_int(datetime.date(2026, 6, 2)),
            amount=-60000,
            transferred_id="cash-side",
            tombstone=0,
            is_parent=0,
        )
        session.add(cash)
        session.add(bank)
        session.add(cash_side)
        session.add(bank_side)
        session.commit()

        match = _find_existing_linked_transfer_duplicate(
            session,
            bank,
            cash,
            datetime.date(2026, 6, 3),
            600,
        )

        assert match.id == cash_side.id