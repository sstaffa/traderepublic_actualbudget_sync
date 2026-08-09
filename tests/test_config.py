import importlib

import pytest


@pytest.fixture
def reloadable_config():
    import app.core.config as config

    original_settings = config.settings
    yield config
    config.settings = original_settings


def test_account_budget_defaults(monkeypatch, reloadable_config):
    monkeypatch.delenv("ACTUAL_CASH_ACCOUNT_OFFBUDGET", raising=False)
    monkeypatch.delenv("ACTUAL_DEPOT_ACCOUNT_OFFBUDGET", raising=False)

    reloaded = importlib.reload(reloadable_config)

    assert reloaded.settings.actual_cash_account_offbudget is False
    assert reloaded.settings.actual_depot_account_offbudget is True


def test_account_budget_env_overrides(monkeypatch, reloadable_config):
    monkeypatch.setenv("ACTUAL_CASH_ACCOUNT_OFFBUDGET", "true")
    monkeypatch.setenv("ACTUAL_DEPOT_ACCOUNT_OFFBUDGET", "false")

    reloaded = importlib.reload(reloadable_config)

    assert reloaded.settings.actual_cash_account_offbudget is True
    assert reloaded.settings.actual_depot_account_offbudget is False


def test_sub_depots_default_empty(monkeypatch, reloadable_config):
    monkeypatch.delenv("ACTUAL_SUB_DEPOTS", raising=False)

    reloaded = importlib.reload(reloadable_config)

    assert reloaded.settings.actual_sub_depots == {}


def test_sub_depots_single_isin(monkeypatch, reloadable_config):
    monkeypatch.setenv("ACTUAL_SUB_DEPOTS", '{"Notgroschen": ["lu0908500753"]}')

    reloaded = importlib.reload(reloadable_config)

    # ISINs are normalized to uppercase so lookups are case-insensitive.
    assert reloaded.settings.actual_sub_depots == {"Notgroschen": ["LU0908500753"]}


def test_sub_depots_multiple_accounts_and_isins(monkeypatch, reloadable_config):
    monkeypatch.setenv(
        "ACTUAL_SUB_DEPOTS",
        '{"Notgroschen": ["LU0908500753"], "Rente": ["IE00B4L5Y983", "IE00B3YCGJ38"]}',
    )

    reloaded = importlib.reload(reloadable_config)

    assert reloaded.settings.actual_sub_depots == {
        "Notgroschen": ["LU0908500753"],
        "Rente": ["IE00B4L5Y983", "IE00B3YCGJ38"],
    }


def test_sub_depots_accepts_single_isin_as_plain_string(monkeypatch, reloadable_config):
    # A single ISIN given as a plain string (not a list) should still work.
    monkeypatch.setenv("ACTUAL_SUB_DEPOTS", '{"Notgroschen": "LU0908500753"}')

    reloaded = importlib.reload(reloadable_config)

    assert reloaded.settings.actual_sub_depots == {"Notgroschen": ["LU0908500753"]}


def test_sub_depots_invalid_json_falls_back_to_empty(monkeypatch, reloadable_config):
    monkeypatch.setenv("ACTUAL_SUB_DEPOTS", "{not valid json")

    reloaded = importlib.reload(reloadable_config)

    assert reloaded.settings.actual_sub_depots == {}


def test_sub_depots_non_object_json_falls_back_to_empty(monkeypatch, reloadable_config):
    # A JSON array (or any non-object) is not a valid account->ISINs mapping.
    monkeypatch.setenv("ACTUAL_SUB_DEPOTS", '["LU0908500753"]')

    reloaded = importlib.reload(reloadable_config)

    assert reloaded.settings.actual_sub_depots == {}


def test_depot_sync_defaults(monkeypatch, reloadable_config):
    monkeypatch.delenv("DEPOT_SYNC_CRON", raising=False)
    monkeypatch.delenv("DEPOT_SYNC_INTERVAL_DAYS", raising=False)

    reloaded = importlib.reload(reloadable_config)

    assert reloaded.settings.depot_sync_cron == "0 18 * * *"
    assert reloaded.settings.depot_sync_interval_days == 30