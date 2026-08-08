from app.mapping.mapper import map_pytr_to_actual
from app.services.trade_republic_csv import parse_trade_republic_csv


CSV_TEXT = '''"datetime","date","account_type","category","type","asset_class","name","symbol","shares","price","amount","fee","tax","currency","original_amount","original_currency","fx_rate","description","transaction_id","counterparty_name","counterparty_iban","payment_reference","mcc_code"
"2021-03-15T14:45:37.934542Z","2021-03-15","DEFAULT","CASH","CUSTOMER_INBOUND","","Sebastian Keller","","","","30.000000","","","EUR","","","","zocken","cdc3f72a-3a90-4ec9-9327-1d5b0bb3995d","Sebastian Keller","DE76701695580000723215","",""
'''


def test_parse_trade_republic_csv_maps_cash_inbound_to_external_transfer():
    items = parse_trade_republic_csv(CSV_TEXT)

    assert len(items) == 1
    assert items[0]["eventType"] == "BANK_TRANSACTION_INCOMING"
    assert items[0]["amount"]["value"] == 30.0
    assert items[0]["raw"]["csv"]["counterparty_iban"] == "DE76701695580000723215"

    mapped = map_pytr_to_actual(items)

    assert mapped[0]["date"] == "2021-03-15"
    assert mapped[0]["amount"] == 3000
    assert mapped[0]["source_id"] == "cdc3f72a-3a90-4ec9-9327-1d5b0bb3995d"
    assert mapped[0]["transfer_kind"] == "external"


def test_parse_trade_republic_csv_maps_buy_to_cash_depot_transfer():
    csv_text = '''datetime,date,account_type,category,type,asset_class,name,symbol,shares,price,amount,fee,tax,currency,original_amount,original_currency,fx_rate,description,transaction_id,counterparty_name,counterparty_iban,payment_reference,mcc_code
2024-01-02T10:00:00Z,2024-01-02,DEFAULT,ORDER,BUY,stock,Acme Corp,US0000000001,2,10,20.00,1.00,0.00,EUR,,,,Buy order,trade-1,,,,'''

    items = parse_trade_republic_csv(csv_text)
    mapped = map_pytr_to_actual(items)

    assert items[0]["eventType"] == "TRADING_TRADE_EXECUTED"
    assert mapped[0]["amount"] == -2000
    assert mapped[0]["transfer_kind"] == "depot"


def test_parse_trade_republic_csv_normalizes_export_event_types():
    rows = [
        ("CUSTOMER_INPAYMENT", "BANK_TRANSACTION_INCOMING", 1000, "external"),
        ("TRANSFER_INBOUND", "BANK_TRANSACTION_INCOMING", 1000, "external"),
        ("TRANSFER_OUTBOUND", "BANK_TRANSACTION_OUTGOING", -1000, "external"),
        ("TRANSFER_INSTANT_OUTBOUND", "BANK_TRANSACTION_OUTGOING", -1000, "external"),
        ("INTEREST_PAYOUT", "INTEREST_PAYOUT", 1000, None),
        ("DIVIDEND", "SSP_CORPORATE_ACTION_CASH", 1000, None),
        ("CARD_TRANSACTION", "CARD_TRANSACTION", -1000, None),
        ("TAX_OPTIMIZATION", "TAX_OPTIMIZATION", 1000, None),
    ]

    for csv_type, expected_event_type, expected_amount, expected_transfer_kind in rows:
        csv_text = f'''datetime,date,account_type,category,type,asset_class,name,symbol,shares,price,amount,fee,tax,currency,original_amount,original_currency,fx_rate,description,transaction_id,counterparty_name,counterparty_iban,payment_reference,mcc_code
2024-01-02T10:00:00Z,2024-01-02,DEFAULT,CASH,{csv_type},,,,"","",10.00,,,EUR,,,,,{csv_type}-1,,,,'''

        items = parse_trade_republic_csv(csv_text)
        mapped = map_pytr_to_actual(items)

        assert items[0]["eventType"] == expected_event_type
        assert mapped[0]["event_type"] == expected_event_type
        assert mapped[0]["amount"] == expected_amount
        assert mapped[0]["transfer_kind"] == expected_transfer_kind
