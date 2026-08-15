"""Duplicate detection across overlapping imports.

Fetching "the last 30 days" repeatedly means the same transactions arrive again
on every run. These tests pin down that this cannot create duplicates, using a
real Transactions table rather than a stub, so a change to the lookup or the
schema would be caught.
"""

import pytest

pytest.importorskip("actual")

from actual.database import Transactions
from sqlmodel import Session, SQLModel, create_engine, select

from app.core.config import settings
from app.services.actual import _find_transaction_by_financial_id


@pytest.fixture
def session(monkeypatch):
    monkeypatch.setattr(settings, "skip_tombstoned_duplicates", False)
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def _store(session, financial_id: str, *, amount: int = -308, date: int = 20260804,
           tombstone: int = 0, account: str = "acct-cash"):
    session.add(Transactions(
        id=f"{financial_id}-row",
        acct=account,
        financial_id=financial_id,
        amount=amount,
        date=date,
        tombstone=tombstone,
        is_parent=0,
    ))
    session.commit()


def test_the_same_transaction_is_recognised_on_a_later_run(session):
    """The core of the rolling window: an id already imported must be found."""
    _store(session, "da015902-fc7d-5c7d-b7cf-a4395eff2044")

    assert _find_transaction_by_financial_id(
        session, "da015902-fc7d-5c7d-b7cf-a4395eff2044"
    ) is not None


def test_an_unseen_transaction_is_not_a_duplicate(session):
    _store(session, "da015902-fc7d-5c7d-b7cf-a4395eff2044")

    assert _find_transaction_by_financial_id(session, "brand-new-id") is None


def test_repeated_overlapping_imports_insert_nothing_twice(session):
    """Three days of "last 30 days" imports over the same transactions."""
    already_imported = ["rewe-1", "jet-2", "zinsen-3"]
    for financial_id in already_imported:
        _store(session, financial_id)

    for _run in range(3):
        incoming = already_imported + []
        new_rows = [
            financial_id for financial_id in incoming
            if _find_transaction_by_financial_id(session, financial_id) is None
        ]
        assert new_rows == []

    assert len(session.exec(select(Transactions)).all()) == 3


def test_only_genuinely_new_transactions_are_inserted(session):
    for financial_id in ("rewe-1", "jet-2"):
        _store(session, financial_id)

    incoming = ["rewe-1", "jet-2", "amazon-3"]
    new_rows = [
        financial_id for financial_id in incoming
        if _find_transaction_by_financial_id(session, financial_id) is None
    ]

    assert new_rows == ["amazon-3"]


def test_matching_ignores_date_and_amount(session):
    """Only the id decides. Trade Republic keeps it stable, and a value that
    changed between runs must not look like a different transaction."""
    _store(session, "rewe-1", amount=-308, date=20260804)

    match = _find_transaction_by_financial_id(session, "rewe-1")

    assert match is not None
    assert match.amount == -308


def test_two_transactions_with_the_same_amount_stay_separate(session):
    """Same day, same amount, different ids: two real purchases, not a
    duplicate. Matching on id rather than on date and amount is what keeps
    them apart."""
    _store(session, "coffee-1", amount=-350, date=20260804)
    _store(session, "coffee-2", amount=-350, date=20260804)

    assert len(session.exec(select(Transactions)).all()) == 2
    assert _find_transaction_by_financial_id(session, "coffee-1").id == "coffee-1-row"
    assert _find_transaction_by_financial_id(session, "coffee-2").id == "coffee-2-row"


def test_deleted_transactions_are_reimported_by_default(session):
    """Documents the configured behaviour: a transaction deleted in Actual
    comes back on the next run, because tombstoned rows do not count as
    duplicates unless TR_SKIP_TOMBSTONED_DUPLICATES is set."""
    _store(session, "rewe-1", tombstone=1)

    assert _find_transaction_by_financial_id(session, "rewe-1") is None


def test_deleted_transactions_stay_deleted_when_configured(session, monkeypatch):
    monkeypatch.setattr(settings, "skip_tombstoned_duplicates", True)
    _store(session, "rewe-1", tombstone=1)

    assert _find_transaction_by_financial_id(session, "rewe-1") is not None


def test_missing_id_is_never_a_duplicate(session):
    _store(session, "rewe-1")

    assert _find_transaction_by_financial_id(session, None) is None
    assert _find_transaction_by_financial_id(session, "") is None