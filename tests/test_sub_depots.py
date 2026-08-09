from decimal import Decimal

from app.core.config import settings
from app.services.actual import adjust_depot_balance, adjust_sub_depot_balances
from app.services.trade_republic import split_sub_depot_values


# --- split_sub_depot_values (pure function, no TR API access needed) --------

def _position(isin: str, value: float) -> dict:
    return {"instrument_id": isin, "name": isin, "value": value}


def test_split_no_sub_depots_configured_excludes_nothing():
    positions = [_position("LU0908500753", 1234.56)]

    excluded_total, sub_values = split_sub_depot_values(positions, sub_depots={})

    assert excluded_total == Decimal("0")
    assert sub_values == {}


def test_split_single_isin_assigned_to_one_account():
    positions = [
        _position("LU0908500753", 500.00),   # Notgroschen
        _position("IE00B3YCGJ38", 1000.00),  # not assigned -> stays in main depot
    ]
    sub_depots = {"Notgroschen": ["LU0908500753"]}

    excluded_total, sub_values = split_sub_depot_values(positions, sub_depots=sub_depots)

    assert excluded_total == Decimal("500.00")
    assert sub_values == {"Notgroschen": 500.00}


def test_split_multiple_isins_same_account_are_summed():
    positions = [
        _position("LU0908500753", 300.00),
        _position("IE00B4L5Y983", 200.50),
    ]
    sub_depots = {"Notgroschen": ["LU0908500753", "IE00B4L5Y983"]}

    excluded_total, sub_values = split_sub_depot_values(positions, sub_depots=sub_depots)

    assert excluded_total == Decimal("500.50")
    assert sub_values == {"Notgroschen": 500.50}


def test_split_multiple_accounts_independent():
    positions = [
        _position("LU0908500753", 500.00),   # Notgroschen
        _position("IE00B3YCGJ38", 750.00),   # Rente
        _position("US0000000001", 100.00),   # unassigned -> main depot
    ]
    sub_depots = {
        "Notgroschen": ["LU0908500753"],
        "Rente": ["IE00B3YCGJ38"],
    }

    excluded_total, sub_values = split_sub_depot_values(positions, sub_depots=sub_depots)

    assert excluded_total == Decimal("1250.00")
    assert sub_values == {"Notgroschen": 500.00, "Rente": 750.00}


def test_split_isin_lookup_is_case_insensitive():
    positions = [_position("lu0908500753", 500.00)]
    sub_depots = {"Notgroschen": ["LU0908500753"]}

    excluded_total, sub_values = split_sub_depot_values(positions, sub_depots=sub_depots)

    assert excluded_total == Decimal("500.00")
    assert sub_values == {"Notgroschen": 500.00}


def test_split_configured_account_with_no_matching_position_reports_zero():
    # Notgroschen ISIN currently not held (e.g. fully sold) -> should still be
    # reported with 0.0 so the Actual account gets reconciled down to 0, not
    # silently skipped.
    positions = [_position("IE00B3YCGJ38", 1000.00)]
    sub_depots = {"Notgroschen": ["LU0908500753"]}

    excluded_total, sub_values = split_sub_depot_values(positions, sub_depots=sub_depots)

    assert excluded_total == Decimal("0")
    assert sub_values == {"Notgroschen": 0.0}


def test_split_position_without_value_is_ignored():
    positions = [{"instrument_id": "LU0908500753", "name": "x", "value": None}]
    sub_depots = {"Notgroschen": ["LU0908500753"]}

    excluded_total, sub_values = split_sub_depot_values(positions, sub_depots=sub_depots)

    assert excluded_total == Decimal("0")
    assert sub_values == {"Notgroschen": 0.0}


def test_split_defaults_to_settings_when_sub_depots_omitted(monkeypatch):
    monkeypatch.setattr(settings, "actual_sub_depots", {"Notgroschen": ["LU0908500753"]})
    positions = [_position("LU0908500753", 500.00)]

    excluded_total, sub_values = split_sub_depot_values(positions)

    assert excluded_total == Decimal("500.00")
    assert sub_values == {"Notgroschen": 500.00}


# --- adjust_depot_balance / adjust_sub_depot_balances (mock mode) -----------

def test_adjust_depot_balance_uses_custom_account_name(monkeypatch):
    monkeypatch.setattr(settings, "app_mode", "mock")

    result = adjust_depot_balance("500.00", account_name="Notgroschen", dry_run=True)

    assert result["account"] == "Notgroschen"
    assert result["target_balance"] == 500.0
    assert result["dry_run"] is True


def test_adjust_depot_balance_defaults_to_main_depot_account(monkeypatch):
    monkeypatch.setattr(settings, "app_mode", "mock")
    monkeypatch.setattr(settings, "actual_depot_account_name", "Trade Republic Depot")

    result = adjust_depot_balance("500.00", dry_run=True)

    assert result["account"] == "Trade Republic Depot"


def test_adjust_sub_depot_balances_covers_every_configured_account(monkeypatch):
    monkeypatch.setattr(settings, "app_mode", "mock")
    monkeypatch.setattr(
        settings,
        "actual_sub_depots",
        {"Notgroschen": ["LU0908500753"], "Rente": ["IE00B3YCGJ38"]},
    )

    results = adjust_sub_depot_balances(
        {"Notgroschen": 500.0, "Rente": 750.0}, dry_run=True
    )

    assert set(results.keys()) == {"Notgroschen", "Rente"}
    assert results["Notgroschen"]["account"] == "Notgroschen"
    assert results["Notgroschen"]["target_balance"] == 500.0
    assert results["Rente"]["account"] == "Rente"
    assert results["Rente"]["target_balance"] == 750.0


def test_adjust_sub_depot_balances_defaults_missing_values_to_zero(monkeypatch):
    # An account is configured but currently holds none of its assigned ISINs
    # (e.g. fully sold) -> it must still be adjusted, down to 0, not skipped.
    monkeypatch.setattr(settings, "app_mode", "mock")
    monkeypatch.setattr(settings, "actual_sub_depots", {"Notgroschen": ["LU0908500753"]})

    results = adjust_sub_depot_balances({}, dry_run=True)

    assert results["Notgroschen"]["target_balance"] == 0.0