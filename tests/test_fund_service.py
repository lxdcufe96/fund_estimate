from app.fund_service import _parse_fund_script, _parse_holdings, _parse_quotes


def test_parse_fund_script():
    text = '''
    var fS_name = "测试基金";var fS_code = "000001";
    var Data_netWorthTrend = [{"x":1784476800000,"y":1.2345,"equityReturn":1.2}];
    var Data_assetAllocation = {"series":[{"name":"股票占净比","data":[65.5]}],"categories":["2026-06-30"]};
    '''
    result = _parse_fund_script(text)
    assert result["name"] == "测试基金"
    assert result["officialNav"] == 1.2345
    assert result["stockPositionPct"] == 65.5


def test_parse_holdings():
    payload = {"Datas": {"fundStocks": [{"GPDM": "600000", "GPJC": "浦发银行", "JZBL": "8.2", "NEWTEXCH": "1"}]}, "Expansion": "2026-06-30"}
    rows, date = _parse_holdings(payload)
    assert rows[0]["market"] == "1"
    assert rows[0]["weightPct"] == 8.2
    assert date == "2026-06-30"


def test_parse_quotes_scaling():
    payload = {"data": {"diff": [{"f2": 1234, "f3": -56, "f12": "600000", "f14": "浦发银行"}]}}
    result = _parse_quotes(payload)
    assert result["600000"]["price"] == 12.34
    assert result["600000"]["changePct"] == -0.56

