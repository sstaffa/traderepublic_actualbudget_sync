from app.mapping.mapper import map_pytr_to_actual


# Preprocessed mock format (legacy format with external_id/date/amount string)
SAMPLE_MOCK = [
    {
        "id_externe": "1",
        "date": "2026-04-20T08:52:15.398+0000",
        "amount": "-4.87",
        "currency": "EUR",
        "status": "EXECUTED",
        "title": "Electra Paris",
        "subtitle": "",
        "raw": {"id": "1", "timestamp": "2026-04-20T08:52:15.398+0000", "amount": {"value": -4.87, "currency": "EUR"}},
    },
    {
        "id_externe": "2",
        "date": "2026-04-21T10:00:00.000+0000",
        "amount": "10.00",
        "currency": "EUR",
        "status": "CANCELED",
        "title": "Should be ignored",
        "raw": {"id": "2", "timestamp": "2026-04-21T10:00:00.000+0000", "amount": {"value": 10.0, "currency": "EUR"}},
    },
]

# Real Trade Republic API format returned by timeline_transactions
SAMPLE_TR_REAL = [
    {
        "id": "2d3d0883-00b0-43aa-ad98-396f9bd5db6d",
        "timestamp": "2026-05-11T07:35:47.510+0000",
        "title": "Core Stoxx Europe 600 EUR (Acc)",
        "subtitle": "Sparplan ausgeführt",
        "amount": {"currency": "EUR", "value": -37, "fractionDigits": 2},
        "status": "EXECUTED",
        "eventType": "TRADING_SAVINGSPLAN_EXECUTED",
    },
    {
        "id": "3511b9d2-37dd-5af0-ad8b-6d17d253d07b",
        "timestamp": "2026-05-10T21:18:26.078+0000",
        "title": "Maxoutil",
        "subtitle": None,
        "amount": {"currency": "EUR", "value": -164.68, "fractionDigits": 2},
        "status": "EXECUTED",
        "eventType": "CARD_TRANSACTION",
    },
    {
        "id": "pending-1",
        "timestamp": "2026-05-11T00:05:21.378+0000",
        "title": "S&P 500 USD (Acc)",
        "subtitle": "Sparplan ausstehend",
        "amount": {"currency": "EUR", "value": -37, "fractionDigits": 2},
        "status": "PENDING",
        "eventType": "TRADING_SAVINGSPLAN_EXECUTION_PENDING",
    },
]


def test_map_filters_and_amounts():
    """Test the preprocessed mock format."""
    mapped = map_pytr_to_actual(SAMPLE_MOCK)
    assert isinstance(mapped, list)
    # Only first tx should be present (second is CANCELED)
    assert len(mapped) == 1
    m = mapped[0]
    assert m["date"] == "2026-04-20"
    assert m["payee"] == "Electra Paris"
    assert m["amount"] == -487


def test_map_real_tr_format():
    """Test the real format returned by the Trade Republic API."""
    mapped = map_pytr_to_actual(SAMPLE_TR_REAL)
    # A pending transaction can be imported as uncleared.
    assert len(mapped) == 3

    etf = mapped[0]
    assert etf["date"] == "2026-05-11"
    assert etf["payee"] == "Core Stoxx Europe 600 EUR (Acc)"
    assert etf["amount"] == -3700   # -37 EUR → -3700 centimes
    assert etf["currency"] == "EUR"
    assert etf["source_id"] == "2d3d0883-00b0-43aa-ad98-396f9bd5db6d"
    assert etf["is_transfer"] is False
    assert etf["transfer_kind"] is None

    card = mapped[1]
    assert card["date"] == "2026-05-10"
    assert card["payee"] == "Maxoutil"
    assert card["amount"] == -16468  # -164.68 EUR → -16468 centimes
    assert card["source_id"] == "3511b9d2-37dd-5af0-ad8b-6d17d253d07b"

    pending = mapped[2]
    assert pending["pending"] is True
    assert pending["cleared"] is False


def test_bank_transactions_are_external_transfers_only():
    mapped = map_pytr_to_actual([
        {
            "id": "bank-in",
            "timestamp": "2026-05-12T10:00:00.000+0000",
            "title": "SEPA Einzahlung",
            "amount": {"currency": "EUR", "value": 100, "fractionDigits": 2},
            "status": "EXECUTED",
            "eventType": "BANK_TRANSACTION_INCOMING",
        },
        {
            "id": "trade",
            "timestamp": "2026-05-12T11:00:00.000+0000",
            "title": "ETF Kauf",
            "amount": {"currency": "EUR", "value": -50, "fractionDigits": 2},
            "status": "EXECUTED",
            "eventType": "TRADING_TRADE_EXECUTED",
        },
    ])

    assert mapped[0]["account_key"] == "cash"
    assert mapped[0]["transfer_kind"] == "external"
    assert mapped[0]["is_transfer"] is True
    assert mapped[1]["account_key"] == "cash"
    assert mapped[1]["transfer_kind"] == "depot"
    assert mapped[1]["is_transfer"] is True


def test_map_real_tr_format_memo():
    """Le memo (subtitle) est correctement extrait."""
    mapped = map_pytr_to_actual(SAMPLE_TR_REAL)
    assert "Sparplan ausgeführt" in mapped[0]["memo"]
    assert "eventType: TRADING_SAVINGSPLAN_EXECUTED" in mapped[0]["memo"]
    assert "eventType: CARD_TRANSACTION" in mapped[1]["memo"]

def test_map_real_tr_format_memo_with_raw_enabled(monkeypatch):
    """Bei aktiviertem include_raw_in_notes landet das Raw-JSON im Memo."""
    from app.core.config import settings
    monkeypatch.setattr(settings, "include_raw_in_notes", True)
    monkeypatch.setattr(settings, "include_status_in_notes", True)

    mapped = map_pytr_to_actual(SAMPLE_TR_REAL)
    assert "TR status:" in mapped[0]["memo"]
    assert "Trade Republic raw:" in mapped[0]["memo"]