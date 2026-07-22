import asyncio

from app.fund_service import FundService, _parse_fund_script, _parse_holdings, _parse_quotes


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
    assert result["navDate"] == "2026-07-20"


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


def test_cached_reads_share_one_quote_request():
    service = FundService()
    quote_calls = 0

    async def fake_info(code):
        return {
            "code": code,
            "name": f"基金{code}",
            "officialNav": 1.0,
            "officialChangePct": 0,
            "navDate": "2026-07-21",
            "stockPositionPct": 80,
            "allocationDate": "2026-06-30",
        }

    async def fake_holdings(code):
        return ([{
            "code": "600000",
            "name": "浦发银行",
            "weightPct": 10,
            "market": "1",
            "sector": "银行",
        }], "2026-06-30")

    async def fake_quotes(holdings):
        nonlocal quote_calls
        quote_calls += 1
        assert len(holdings) == 2
        return {"600000": {"name": "浦发银行", "price": 10, "changePct": 1}}

    service._fund_info = fake_info
    service._holdings = fake_holdings
    service._quotes = fake_quotes

    async def run():
        try:
            concurrent_results = await asyncio.gather(*(
                service.get_estimates(["000001", "000002"])
                for _ in range(20)
            ))
            result = concurrent_results[0]
            assert all(item == result for item in concurrent_results)
            cached_result = await service.get_estimates(["000001", "000002"])
            assert len(result) == 2
            assert cached_result == result
            assert all(item["estimatedChangePct"] == 0.8 for item in result)
        finally:
            await service.close()

    asyncio.run(run())
    assert quote_calls == 1


def test_failed_quote_refresh_keeps_last_snapshot():
    service = FundService()
    previous = {"code": "000001", "estimatedNav": 1.2345}

    async def fake_info(code):
        return {
            "code": code,
            "name": "测试基金",
            "officialNav": 1.0,
            "officialChangePct": 0,
            "navDate": "2026-07-21",
            "stockPositionPct": 80,
            "allocationDate": "2026-06-30",
        }

    async def fake_holdings(code):
        return ([{
            "code": "600000",
            "name": "浦发银行",
            "weightPct": 10,
            "market": "1",
            "sector": "银行",
        }], "2026-06-30")

    async def failed_quotes(holdings):
        raise RuntimeError("quote provider unavailable")

    service._fund_info = fake_info
    service._holdings = fake_holdings
    service._quotes = failed_quotes

    async def run():
        try:
            service._snapshots["000001"] = previous
            service._snapshot_times["000001"] = 1
            await service._refresh_codes(["000001"], force=True)
            assert service._snapshots["000001"] is previous
        finally:
            await service.close()

    asyncio.run(run())


def test_partial_empty_quotes_do_not_replace_complete_snapshot():
    service = FundService()
    previous = {
        "code": "000001",
        "estimatedNav": 1.2345,
        "quoteCoveragePct": 100,
    }

    async def fake_info(code):
        return {
            "code": code,
            "name": "测试基金",
            "officialNav": 1.0,
            "officialChangePct": 0,
            "navDate": "2026-07-21",
            "stockPositionPct": 80,
            "allocationDate": "2026-06-30",
        }

    async def fake_holdings(code):
        return ([{
            "code": "600000",
            "name": "浦发银行",
            "weightPct": 10,
            "market": "1",
            "sector": "银行",
        }], "2026-06-30")

    async def partial_quotes(holdings):
        return {}

    service._fund_info = fake_info
    service._holdings = fake_holdings
    service._quotes = partial_quotes

    async def run():
        try:
            service._snapshots["000001"] = previous
            service._snapshot_times["000001"] = 1
            await service._refresh_codes(["000001"], force=True)
            assert service._snapshots["000001"] is previous
        finally:
            await service.close()

    asyncio.run(run())
