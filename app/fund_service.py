from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx


EASTMONEY_FUND_SCRIPT = "https://fund.eastmoney.com/pingzhongdata/{code}.js"
EASTMONEY_HOLDINGS = "https://fundmobapi.eastmoney.com/FundMNewApi/FundMNInverstPosition"
EASTMONEY_QUOTES = "https://push2.eastmoney.com/api/qt/ulist.np/get"
USER_AGENT = "Mozilla/5.0 (compatible; FundLens/1.0; personal fund dashboard)"


class FundDataError(RuntimeError):
    """Raised when a fund cannot be evaluated with the available public data."""


@dataclass
class CacheEntry:
    expires_at: float
    value: Any


class TTLCache:
    def __init__(self) -> None:
        self._items: dict[str, CacheEntry] = {}
        self._lock = asyncio.Lock()

    async def get(self, key: str) -> Any | None:
        async with self._lock:
            entry = self._items.get(key)
            if not entry or entry.expires_at <= time.time():
                self._items.pop(key, None)
                return None
            return entry.value

    async def set(self, key: str, value: Any, ttl: int) -> None:
        async with self._lock:
            self._items[key] = CacheEntry(time.time() + ttl, value)


cache = TTLCache()


def _extract_json_variable(text: str, variable: str) -> Any:
    match = re.search(rf"var\s+{re.escape(variable)}\s*=\s*", text)
    if not match:
        raise FundDataError(f"缺少数据字段：{variable}")
    start = match.end()
    while start < len(text) and text[start].isspace():
        start += 1
    if start >= len(text) or text[start] not in "[{":
        raise FundDataError(f"数据字段格式异常：{variable}")

    pairs = {"[": "]", "{": "}"}
    stack = [text[start]]
    in_string = False
    escaped = False
    end = start + 1
    while end < len(text) and stack:
        char = text[end]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char in pairs:
            stack.append(char)
        elif char in "]}":
            if not stack or pairs[stack[-1]] != char:
                raise FundDataError(f"数据字段括号异常：{variable}")
            stack.pop()
        end += 1
    if stack:
        raise FundDataError(f"数据字段不完整：{variable}")

    try:
        return json.loads(text[start:end])
    except json.JSONDecodeError as exc:
        raise FundDataError(f"数据字段解析失败：{variable}") from exc


def _extract_string_variable(text: str, variable: str) -> str:
    match = re.search(rf'var\s+{re.escape(variable)}\s*=\s*"(.*?)"\s*;', text, re.DOTALL)
    if not match:
        raise FundDataError(f"缺少数据字段：{variable}")
    return match.group(1)


def _parse_fund_script(text: str) -> dict[str, Any]:
    name = _extract_string_variable(text, "fS_name")
    code = _extract_string_variable(text, "fS_code")
    nav_trend = _extract_json_variable(text, "Data_netWorthTrend")
    if not nav_trend:
        raise FundDataError("暂无官方净值记录")

    latest = nav_trend[-1]
    nav_date = datetime.fromtimestamp(latest["x"] / 1000).strftime("%Y-%m-%d")
    official_change = float(latest.get("equityReturn") or 0)

    stock_position = 0.0
    allocation_date = nav_date
    try:
        allocation = _extract_json_variable(text, "Data_assetAllocation")
        categories = allocation.get("categories") or []
        for series in allocation.get("series") or []:
            if series.get("name") == "股票占净比" and series.get("data"):
                stock_position = float(series["data"][-1] or 0)
                allocation_date = categories[-1] if categories else nav_date
                break
    except FundDataError:
        pass

    if stock_position <= 0:
        try:
            positions = _extract_json_variable(text, "Data_fundSharesPositions")
            if positions:
                stock_position = float(positions[-1][1] or 0)
        except FundDataError:
            pass

    return {
        "code": code,
        "name": name,
        "officialNav": float(latest["y"]),
        "officialChangePct": round(official_change, 4),
        "navDate": nav_date,
        "stockPositionPct": round(stock_position, 2),
        "allocationDate": allocation_date,
    }


def _normalise_market(stock: dict[str, Any]) -> str:
    market = str(stock.get("NEWTEXCH") or stock.get("TEXCH") or "")
    code = str(stock.get("GPDM") or "")
    if market in {"0", "1", "105", "106", "116"}:
        return market
    if code.startswith(("5", "6", "9")):
        return "1"
    return "0"


def _parse_holdings(payload: dict[str, Any]) -> tuple[list[dict[str, Any]], str | None]:
    data = payload.get("Datas") or {}
    rows = data.get("fundStocks") or []
    holdings: list[dict[str, Any]] = []
    for row in rows:
        code = str(row.get("GPDM") or "").strip()
        try:
            weight = float(row.get("JZBL") or 0)
        except (TypeError, ValueError):
            continue
        if not code or weight <= 0:
            continue
        holdings.append(
            {
                "code": code,
                "name": row.get("GPJC") or code,
                "weightPct": round(weight, 4),
                "market": _normalise_market(row),
                "sector": row.get("INDEXNAME") or "",
            }
        )
    return holdings, payload.get("Expansion")


def _parse_quotes(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for row in ((payload.get("data") or {}).get("diff") or []):
        code = str(row.get("f12") or "")
        raw_change = row.get("f3")
        raw_price = row.get("f2")
        if not code or raw_change in (None, "-"):
            continue
        result[code] = {
            "name": row.get("f14") or code,
            "price": None if raw_price in (None, "-") else round(float(raw_price) / 100, 4),
            "changePct": round(float(raw_change) / 100, 4),
        }
    return result


class FundService:
    def __init__(self) -> None:
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(15.0, connect=8.0),
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT},
        )

    async def close(self) -> None:
        await self.client.aclose()

    async def _fund_info(self, code: str) -> dict[str, Any]:
        key = f"fund:{code}"
        cached = await cache.get(key)
        if cached:
            return cached
        response = await self.client.get(EASTMONEY_FUND_SCRIPT.format(code=code))
        if response.status_code != 200 or "notfound" in str(response.url):
            raise FundDataError("基金代码不存在或官方净值源暂不可用")
        info = _parse_fund_script(response.text)
        await cache.set(key, info, 3600)
        return info

    async def _holdings(self, code: str) -> tuple[list[dict[str, Any]], str | None]:
        key = f"holdings:{code}"
        cached = await cache.get(key)
        if cached:
            return cached
        response = await self.client.get(
            EASTMONEY_HOLDINGS,
            params={
                "FCODE": code,
                "deviceid": "Wap",
                "plat": "Wap",
                "product": "EFund",
                "version": "2.0.0",
            },
        )
        response.raise_for_status()
        value = _parse_holdings(response.json())
        await cache.set(key, value, 21600)
        return value

    async def _quotes(self, holdings: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        if not holdings:
            return {}
        secids = ",".join(f"{item['market']}.{item['code']}" for item in holdings)
        response = await self.client.get(
            EASTMONEY_QUOTES,
            params={"secids": secids, "fields": "f2,f3,f12,f14"},
        )
        response.raise_for_status()
        return _parse_quotes(response.json())

    async def estimate(self, code: str) -> dict[str, Any]:
        if not re.fullmatch(r"\d{6}", code):
            raise FundDataError("请输入正确的 6 位基金代码")

        info, holding_result = await asyncio.gather(
            self._fund_info(code), self._holdings(code)
        )
        holdings, holding_date = holding_result
        quotes = await self._quotes(holdings)

        weighted_change = 0.0
        quoted_weight = 0.0
        detailed: list[dict[str, Any]] = []
        for holding in holdings:
            quote = quotes.get(holding["code"])
            item = {**holding, "price": None, "changePct": None, "contributionPct": None}
            if quote:
                weight = holding["weightPct"]
                change = quote["changePct"]
                quoted_weight += weight
                weighted_change += weight * change
                item.update(
                    price=quote["price"],
                    changePct=change,
                    contributionPct=round(weight * change / 100, 4),
                )
            detailed.append(item)

        disclosed_weight = sum(item["weightPct"] for item in holdings)
        stock_position = float(info["stockPositionPct"] or 0)
        if quoted_weight > 0 and stock_position > 0:
            top_holdings_average = weighted_change / quoted_weight
            estimated_change = top_holdings_average * stock_position / 100
        elif quoted_weight > 0:
            estimated_change = weighted_change / 100
        else:
            estimated_change = 0.0

        quote_coverage = (quoted_weight / disclosed_weight * 100) if disclosed_weight else 0
        portfolio_coverage = (disclosed_weight / stock_position * 100) if stock_position else 0
        confidence = "较高" if quote_coverage >= 95 and portfolio_coverage >= 65 else "中等"
        if quote_coverage < 70 or portfolio_coverage < 35 or not holdings or stock_position < 10:
            confidence = "较低"

        estimated_nav = info["officialNav"] * (1 + estimated_change / 100)
        now = datetime.now().astimezone()
        warnings = ["估值依据最近一期披露持仓推算，不等于基金公司最终净值。"]
        if not holdings:
            warnings.append("未获取到股票持仓，当前仅展示官方净值。")
        if stock_position < 30:
            warnings.append("股票仓位较低，债券、现金及其他资产未实时估值。")
        if any(item["market"] not in {"0", "1"} for item in holdings):
            warnings.append("含港股或海外资产，交易时段与汇率差异可能扩大误差。")

        return {
            **info,
            "estimatedNav": round(estimated_nav, 4),
            "estimatedChangePct": round(estimated_change, 4),
            "holdingDate": holding_date,
            "holdingCount": len(holdings),
            "disclosedWeightPct": round(disclosed_weight, 2),
            "quoteCoveragePct": round(quote_coverage, 1),
            "portfolioCoveragePct": round(min(portfolio_coverage, 100), 1),
            "confidence": confidence,
            "updatedAt": now.isoformat(timespec="seconds"),
            "marketStatus": "交易中" if now.weekday() < 5 and 9 <= now.hour < 15 else "非交易时段",
            "holdings": detailed,
            "warnings": warnings,
            "method": "最近披露股票持仓加权涨跌 × 股票仓位",
        }
