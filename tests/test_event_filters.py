import pytest

from app.core.config import settings
from app.mapping import event_types
from app.mapping.event_types import (
    EVENT_TYPE_GROUPS,
    KNOWN_EVENT_TYPES,
    get_excluded_event_types,
    normalize_event_types,
)
from app.mapping.mapper import get_last_filter_meta, map_pytr_to_actual


@pytest.fixture
def no_env_file(monkeypatch, tmp_path):
    """No .env file present: the blocklist comes from the process environment
    captured at startup."""
    monkeypatch.setenv("TR_ENV_FILE", str(tmp_path / "missing.env"))
    monkeypatch.setattr(settings, "tr_excluded_event_types", [])
    return tmp_path


@pytest.fixture
def env_file(monkeypatch, tmp_path):
    """A readable .env file: it is authoritative and re-read on every call."""
    path = tmp_path / ".env"
    monkeypatch.setenv("TR_ENV_FILE", str(path))
    monkeypatch.setattr(settings, "tr_excluded_event_types", [])
    return path


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


# --- resolution from environment / .env --------------------------------------

def test_falls_back_to_process_environment_without_env_file(no_env_file, monkeypatch):
    monkeypatch.setattr(settings, "tr_excluded_event_types", ["CARD_VERIFICATION"])

    assert get_excluded_event_types() == ["CARD_VERIFICATION"]
    assert event_types.excluded_event_types_source() == "environment"


def test_env_file_takes_precedence_over_process_environment(env_file, monkeypatch):
    monkeypatch.setattr(settings, "tr_excluded_event_types", ["CARD_VERIFICATION"])
    env_file.write_text("TR_EXCLUDED_EVENT_TYPES=SAVEBACK_AGGREGATE\n")

    assert get_excluded_event_types() == ["SAVEBACK_AGGREGATE"]
    assert event_types.excluded_event_types_source().startswith("env-file:")


def test_env_file_is_reread_on_every_call(env_file):
    """The whole point of the .env mount: editing it must take effect without
    restarting the container."""
    env_file.write_text("TR_EXCLUDED_EVENT_TYPES=CARD_VERIFICATION\n")
    assert get_excluded_event_types() == ["CARD_VERIFICATION"]

    env_file.write_text("TR_EXCLUDED_EVENT_TYPES=SAVEBACK_AGGREGATE,CARD_OCT\n")
    assert get_excluded_event_types() == ["SAVEBACK_AGGREGATE", "CARD_OCT"]


def test_removing_the_key_from_env_file_disables_filtering(env_file, monkeypatch):
    """A missing key in an existing .env means "exclude nothing" - it must not
    silently fall back to the startup environment, or deleting the line would
    look like it had no effect."""
    monkeypatch.setattr(settings, "tr_excluded_event_types", ["CARD_VERIFICATION"])
    env_file.write_text("ACTUAL_URL=http://example\n")

    assert get_excluded_event_types() == []


def test_empty_value_means_no_filtering(env_file):
    env_file.write_text("TR_EXCLUDED_EVENT_TYPES=\n")

    assert get_excluded_event_types() == []


def test_env_file_values_are_normalized(env_file):
    env_file.write_text("TR_EXCLUDED_EVENT_TYPES=card_verification, SAVEBACK_AGGREGATE\n")

    assert get_excluded_event_types() == ["CARD_VERIFICATION", "SAVEBACK_AGGREGATE"]


# --- filtering in the mapper -------------------------------------------------

def test_excluded_event_type_is_dropped(env_file):
    env_file.write_text("TR_EXCLUDED_EVENT_TYPES=CARD_VERIFICATION\n")

    mapped = map_pytr_to_actual([
        _tx("CARD_TRANSACTION", "keep-1"),
        _tx("CARD_VERIFICATION", "drop-1", amount=0.0),
    ])

    assert [tx["source_id"] for tx in mapped] == ["keep-1"]


def test_unknown_event_types_are_always_imported(env_file):
    """Blocklist semantics: a new event type Trade Republic introduces must
    never be silently dropped."""
    env_file.write_text("TR_EXCLUDED_EVENT_TYPES=CARD_VERIFICATION\n")

    mapped = map_pytr_to_actual([_tx("SOME_BRAND_NEW_TYPE", "keep-1")])

    assert [tx["source_id"] for tx in mapped] == ["keep-1"]


def test_no_filter_configured_imports_everything(env_file):
    env_file.write_text("TR_EXCLUDED_EVENT_TYPES=\n")

    mapped = map_pytr_to_actual([
        _tx("CARD_TRANSACTION", "a"),
        _tx("CARD_VERIFICATION", "b"),
    ])

    assert len(mapped) == 2


def test_filter_matching_is_case_insensitive(env_file):
    env_file.write_text("TR_EXCLUDED_EVENT_TYPES=card_verification\n")

    assert map_pytr_to_actual([_tx("CARD_VERIFICATION", "drop-1")]) == []


def test_filter_meta_reports_counts(env_file):
    env_file.write_text("TR_EXCLUDED_EVENT_TYPES=CARD_VERIFICATION\n")

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


def test_non_executed_status_counted_separately(env_file):
    env_file.write_text("TR_EXCLUDED_EVENT_TYPES=\n")
    cancelled = _tx("CARD_TRANSACTION", "cancelled")
    cancelled["status"] = "CANCELED"

    map_pytr_to_actual([cancelled, _tx("CARD_TRANSACTION", "ok")])
    meta = get_last_filter_meta()

    assert meta["skipped_by_status"] == 1
    assert meta["excluded_by_event_type"] == 0