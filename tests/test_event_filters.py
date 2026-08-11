import pytest

from app.core.config import settings
from app.mapping.event_types import (
    EVENT_TYPE_GROUPS,
    KNOWN_EVENT_TYPES,
    get_excluded_event_types,
    normalize_event_types,
)
from app.mapping.mapper import get_last_filter_meta, map_pytr_to_actual


@pytest.fixture
def excluded(monkeypatch):
    """Set the blocklist the way the process sees it at startup."""
    def _set(values):
        monkeypatch.setattr(settings, "tr_excluded_event_types", list(values))
    _set([])
    return _set


def _tx(event_type: str, source_id: str = "id-1", amount: float = -10.0) -> dict:
    return {
        "id": source_id,
        "timestamp": "2026-08-04T10:00:00.000+0000",
        "title": "Test",
        "status": "EXECUTED",
        "eventType": event_type,
        "amount": {"currency": "EUR", "value": amount, "fractionDigits": 2},
    }


# --- catalogue ---------------------------------------------------------------

def test_catalogue_has_no_duplicates_across_groups():
    flat = [e for group in EVENT_TYPE_GROUPS.values() for e in group]

    assert len(flat) == len(set(flat))
    assert len(KNOWN_EVENT_TYPES) == len(set(flat))


def test_catalogue_contains_types_observed_in_production():
    for event_type in (
        "CARD_TRANSACTION",
        "CARD_VERIFICATION",
        "CARD_OCT",
        "SAVEBACK_AGGREGATE",
        "TRADING_SAVINGSPLAN_EXECUTED",
        "PAYMENT_INBOUND_SEPA_DIRECT_DEBIT",
        "SSP_CORPORATE_ACTION_CASH",
        "INTEREST_PAYOUT",
    ):
        assert event_type in KNOWN_EVENT_TYPES


def test_normalize_handles_csv_and_casing():
    assert normalize_event_types(" card_verification , CARD_VERIFICATION ,, x ") == [
        "CARD_VERIFICATION",
        "X",
    ]


# --- resolution from the environment ----------------------------------------

def test_reads_blocklist_from_environment(excluded):
    excluded(["CARD_VERIFICATION"])

    assert get_excluded_event_types() == ["CARD_VERIFICATION"]


def test_empty_configuration_means_no_filtering(excluded):
    excluded([])

    assert get_excluded_event_types() == []


def test_values_are_normalized(excluded):
    excluded([" card_verification ", "CARD_VERIFICATION"])

    assert get_excluded_event_types() == ["CARD_VERIFICATION"]


# --- filtering in the mapper -------------------------------------------------

def test_excluded_event_type_is_dropped(excluded):
    excluded(["CARD_VERIFICATION"])

    mapped = map_pytr_to_actual([
        _tx("CARD_TRANSACTION", "keep-1"),
        _tx("CARD_VERIFICATION", "drop-1", amount=0.0),
    ])

    assert [tx["source_id"] for tx in mapped] == ["keep-1"]


def test_unknown_event_types_are_always_imported(excluded):
    """Blocklist semantics: a new event type Trade Republic introduces must
    never be silently dropped."""
    excluded(["CARD_VERIFICATION"])

    mapped = map_pytr_to_actual([_tx("SOME_BRAND_NEW_TYPE", "keep-1")])

    assert [tx["source_id"] for tx in mapped] == ["keep-1"]


def test_no_filter_configured_imports_everything(excluded):
    excluded([])

    mapped = map_pytr_to_actual([
        _tx("CARD_TRANSACTION", "a"),
        _tx("CARD_VERIFICATION", "b"),
    ])

    assert len(mapped) == 2


def test_filter_matching_is_case_insensitive(excluded):
    excluded(["card_verification"])

    assert map_pytr_to_actual([_tx("CARD_VERIFICATION", "drop-1")]) == []


def test_filter_meta_reports_counts(excluded):
    excluded(["CARD_VERIFICATION"])

    map_pytr_to_actual([
        _tx("CARD_TRANSACTION", "a"),
        _tx("CARD_VERIFICATION", "b"),
        _tx("CARD_VERIFICATION", "c"),
    ])
    meta = get_last_filter_meta()

    assert meta["input_count"] == 3
    assert meta["mapped_count"] == 1
    assert meta["excluded_by_event_type"] == 2
    assert meta["excluded_breakdown"] == {"CARD_VERIFICATION": 2}
    assert meta["seen_event_types"] == ["CARD_TRANSACTION", "CARD_VERIFICATION"]


def test_non_executed_status_counted_separately(excluded):
    excluded([])
    cancelled = _tx("CARD_TRANSACTION", "cancelled")
    cancelled["status"] = "CANCELED"

    map_pytr_to_actual([cancelled, _tx("CARD_TRANSACTION", "ok")])
    meta = get_last_filter_meta()

    assert meta["skipped_by_status"] == 1
    assert meta["excluded_by_event_type"] == 0