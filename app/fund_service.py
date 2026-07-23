from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx


EASTMONEY_FUND_SCRIPT = "https://fund.eastmoney.com/pingzhongdata/{code}.js"
EASTMONEY_HOLDINGS = "https://fundmobapi.eastmoney.com/FundMNewApi/FundMNInverstPosition"
EASTMONEY_QUOTES = "https://push2.eastmoney.com/api/qt/ulist.np/get"
USER_AGENT = "Mozilla/5.0 (compatible; FundLens/1.0; personal fund dashboard)"
CHINA_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")
FUND_INFO_TTL = 21600
HOLDINGS_TTL = 86400
QUOTES_TTL = 20
REFRESH_INTERVAL = 30
ACTIVE_CODE_TTL = 600
MAX_FUNDS_PER_REQUEST = 100
QUOTE_BATCH_SIZE = 100

logger = logging.getLogger(__name__)


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
                # Keep expired entries for stale-if-error fallback. Successful
                # refreshes overwrite them; inactive fund snapshots are pruned
                # separately by the service registry.
                return None
            return entry.value

    async def set(self, key: str, value: Any, ttl: int) -> None:
        async with self._lock:
            self._items[key] = CacheEntry(time.time() + ttl, value)

    async def get_stale(self, key: str) -> Any | None:
        async with self._lock:
            entry = self._items.get(key)
            return entry.value if entry else None


cache = TTLCache()


def _is_trading_time(now: datetime) -> bool:
    if now.weekday() >= 5:
        return False
    minute = now.hour * 60 + now.minute
    return 570 <= minute <= 690 or 780 <= minute < 900


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
    # East Money timestamps represent China calendar dates. Render containers run
    # in UTC, so using the server's local timezone would display the prior day.
    nav_date = datetime.fromtimestamp(latest["x"] / 1000, tz=CHINA_TZ).strftime("%Y-%m-%d")
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
    fetched_at = datetime.now(CHINA_TZ).isoformat(timespec="seconds")
    for row in ((payload.get("data") or {}).get("diff") or []):
        code = str(row.get("f12") or "")
        raw_change = row.get("f3")
        raw_price = row.get("f2")
        if not code or raw_change in (None, "-"):
            continue
        market = str(row.get("f13") or "")
        raw_quote_time = row.get("f124")
        quote_time = fetched_at
        if raw_quote_time not in (None, "-", 0, "0"):
            try:
                quote_time = datetime.fromtimestamp(
                    int(raw_quote_time), tz=CHINA_TZ
                ).isoformat(timespec="seconds")
            except (TypeError, ValueError, OSError):
                pass
        quote = {
            "name": row.get("f14") or code,
            "price": None if raw_price in (None, "-") else round(float(raw_price) / 100, 4),
            "changePct": round(float(raw_change) / 100, 4),
            "quoteTime": quote_time,
        }
        result[code] = quote
        if market:
            result[f"{market}.{code}"] = quote
    return result


class FundService:
    def __init__(self) -> None:
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(15.0, connect=8.0),
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT},
        )
        self._active_codes: dict[str, float] = {}
        self._snapshots: dict[str, dict[str, Any]] = {}
        self._snapshot_times: dict[str, float] = {}
        self._last_errors: dict[str, str] = {}
        self._state_lock = asyncio.Lock()
        self._refresh_lock = asyncio.Lock()
        self._metadata_semaphore = asyncio.Semaphore(8)
        self._quote_semaphore = asyncio.Semaphore(8)
        self._background_task: asyncio.Task | None = None
        self._last_background_refresh_at: str | None = None
        self._last_background_duration_ms = 0
        self._last_background_error: str | None = None

    async def start(self) -> None:
        if self._background_task is None:
            self._background_task = asyncio.create_task(
                self._refresh_loop(), name="fund-snapshot-refresh"
            )

    async def close(self) -> None:
        if self._background_task:
            self._background_task.cancel()
            try:
                await self._background_task
            except asyncio.CancelledError:
                pass
        await self.client.aclose()

    async def _get_with_retry(
        self,
        url: str,
        *,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        attempts: int = 3,
        timeout: float | None = None,
    ) -> httpx.Response:
        last_error: Exception | None = None
        for attempt in range(attempts):
            try:
                response = await self.client.get(
                    url, params=params, headers=headers, timeout=timeout
                )
                response.raise_for_status()
                return response
            except (httpx.HTTPError, httpx.TimeoutException) as exc:
                last_error = exc
                if attempt < attempts - 1:
                    await asyncio.sleep(0.35 * (2 ** attempt))
        assert last_error is not None
        raise last_error

    async def _fund_info(self, code: str) -> dict[str, Any]:
        key = f"fund:{code}"
        cached = await cache.get(key)
        if cached:
            return cached
        stale = await cache.get_stale(key)
        try:
            response = await self._get_with_retry(EASTMONEY_FUND_SCRIPT.format(code=code))
            if "notfound" in str(response.url):
                raise FundDataError("基金代码不存在或官方净值源暂不可用")
            info = _parse_fund_script(response.text)
            # The script is large and official NAV changes only once a day.
            await cache.set(key, info, FUND_INFO_TTL)
            return info
        except Exception:
            if stale:
                logger.warning("fund info refresh failed; using stale cache for %s", code)
                return stale
            raise

    async def _holdings(self, code: str) -> tuple[list[dict[str, Any]], str | None]:
        key = f"holdings:{code}"
        cached = await cache.get(key)
        if cached:
            return cached
        stale = await cache.get_stale(key)
        try:
            response = await self._get_with_retry(
                EASTMONEY_HOLDINGS,
                params={
                "FCODE": code,
                "deviceid": "Wap",
                "plat": "Wap",
                "product": "EFund",
                "version": "2.0.0",
                },
            )
            value = _parse_holdings(response.json())
            # Check once a day; Expansion carries the latest report date.
            await cache.set(key, value, HOLDINGS_TTL)
            return value
        except Exception:
            if stale:
                logger.warning("holdings refresh failed; using stale cache for %s", code)
                return stale
            raise

    async def _tencent_quotes(
        self, secids: list[str]
    ) -> dict[str, dict[str, Any]]:
        symbols: list[str] = []
        for secid in secids:
            market, code = secid.split(".", 1)
            if market == "1":
                symbols.append(f"sh{code}")
            elif market == "0":
                symbols.append(f"sz{code}")
            elif market == "116":
                symbols.append(f"hk{code.zfill(5)}")
        if not symbols:
            return {}

        response = await self._get_with_retry(
            "https://qt.gtimg.cn/q=" + ",".join(symbols),
            headers={"Referer": "https://gu.qq.com"},
            attempts=1,
            timeout=5.0,
        )
        text = response.content.decode("gbk", errors="ignore")
        result: dict[str, dict[str, Any]] = {}
        for line in text.splitlines():
            if '="' not in line:
                continue
            symbol = line.split('="', 1)[0].removeprefix("v_")
            parts = line.split('="', 1)[1].rstrip('";').split("~")
            if len(parts) <= 32 or not parts[2] or not parts[3]:
                continue
            if symbol.startswith("sh"):
                market = "1"
            elif symbol.startswith("sz"):
                market = "0"
            elif symbol.startswith("hk"):
                market = "116"
            else:
                continue
            code = parts[2]
            try:
                price = float(parts[3])
                change = float(parts[32])
            except (TypeError, ValueError):
                continue
            quote_time = datetime.now(CHINA_TZ)
            raw_time = parts[30]
            for fmt in ("%Y%m%d%H%M%S", "%Y/%m/%d %H:%M:%S"):
                try:
                    quote_time = datetime.strptime(raw_time, fmt).replace(tzinfo=CHINA_TZ)
                    break
                except ValueError:
                    continue
            quote = {
                "name": parts[1] or code,
                "price": price,
                "changePct": change,
                "quoteTime": quote_time.isoformat(timespec="seconds"),
                "source": "tencent",
            }
            result[code] = quote
            result[f"{market}.{code}"] = quote
        return result

    async def _quotes(self, holdings: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        if not holdings:
            return {}
        unique = sorted({f"{item['market']}.{item['code']}" for item in holdings})
        secids = ",".join(unique)
        key = f"quotes:{secids}"
        cached = await cache.get(key)
        if cached is not None:
            return cached
        chunks = [unique[index:index + QUOTE_BATCH_SIZE] for index in range(0, len(unique), QUOTE_BATCH_SIZE)]

        async def fetch_chunk(chunk: list[str]) -> dict[str, dict[str, Any]]:
            chunk_ids = ",".join(chunk)
            chunk_key = f"quote-chunk:{chunk_ids}"
            chunk_cached = await cache.get(chunk_key)
            if chunk_cached is not None:
                return chunk_cached
            chunk_stale = await cache.get_stale(chunk_key)
            try:
                async with self._quote_semaphore:
                    response = await self._get_with_retry(
                        EASTMONEY_QUOTES,
                        params={"secids": chunk_ids, "fields": "f2,f3,f12,f13,f14,f124"},
                        attempts=1,
                        timeout=5.0,
                    )
                value = _parse_quotes(response.json())
                for quote in value.values():
                    quote["source"] = "eastmoney"
                await cache.set(chunk_key, value, QUOTES_TTL)
                return value
            except Exception:
                try:
                    async with self._quote_semaphore:
                        value = await self._tencent_quotes(chunk)
                    if value:
                        await cache.set(chunk_key, value, QUOTES_TTL)
                        logger.info("eastmoney quote failed; switched to tencent")
                        return value
                except Exception:
                    logger.warning("tencent quote fallback also failed")
                if chunk_stale:
                    logger.warning("quote chunk refresh failed; using stale chunk")
                    return chunk_stale
                logger.warning("quote chunk refresh failed; skipping %s symbols", len(chunk))
                return {}

        parts = await asyncio.gather(*(fetch_chunk(chunk) for chunk in chunks))
        value: dict[str, dict[str, Any]] = {}
        for part in parts:
            value.update(part)
        if value:
            await cache.set(key, value, QUOTES_TTL)
        return value

    def _build_estimate(
        self,
        info: dict[str, Any],
        holdings: list[dict[str, Any]],
        holding_date: str | None,
        quotes: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        weighted_change = 0.0
        quoted_weight = 0.0
        quote_times: list[str] = []
        quote_sources: set[str] = set()
        detailed: list[dict[str, Any]] = []
        for holding in holdings:
            quote = quotes.get(f"{holding['market']}.{holding['code']}") or quotes.get(holding["code"])
            item = {**holding, "price": None, "changePct": None, "contributionPct": None}
            if quote:
                weight = holding["weightPct"]
                change = quote["changePct"]
                quoted_weight += weight
                weighted_change += weight * change
                if quote.get("quoteTime"):
                    quote_times.append(quote["quoteTime"])
                if quote.get("source"):
                    quote_sources.add(quote["source"])
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
        now = datetime.now(CHINA_TZ)
        is_trading = _is_trading_time(now)
        quote_updated_at = min(quote_times) if quote_times else None
        quote_age_seconds = None
        if quote_updated_at:
            quote_age_seconds = max(
                0,
                int((now - datetime.fromisoformat(quote_updated_at)).total_seconds()),
            )
        warnings = ["估值依据最近一期披露持仓推算，不等于基金公司最终净值。"]
        if not holdings:
            warnings.append("未获取到股票持仓，当前仅展示官方净值。")
        elif quoted_weight == 0:
            warnings.append("实时行情暂不可用，当前估算净值暂按最新官方净值显示。")
        if stock_position < 30:
            warnings.append("股票仓位较低，债券、现金及其他资产未实时估值。")
        if any(item["market"] not in {"0", "1"} for item in holdings):
            warnings.append("含港股或海外资产，交易时段与汇率差异可能扩大误差。")

        return {
            **info,
            "estimatedNav": round(estimated_nav, 4),
            "estimatedChangePct": round(estimated_change, 4),
            "realtimeAvailable": quoted_weight > 0,
            "quoteUpdatedAt": quote_updated_at,
            "quoteSource": "+".join(sorted(quote_sources)) or None,
            "quoteAgeSeconds": quote_age_seconds,
            "realtimeStale": is_trading and (
                quote_age_seconds is None or quote_age_seconds > 180
            ),
            "holdingDate": holding_date,
            "holdingCount": len(holdings),
            "disclosedWeightPct": round(disclosed_weight, 2),
            "quoteCoveragePct": round(quote_coverage, 1),
            "portfolioCoveragePct": round(min(portfolio_coverage, 100), 1),
            "confidence": confidence,
            "updatedAt": now.isoformat(timespec="seconds"),
            "marketStatus": "交易中" if is_trading else "非交易时段",
            "holdings": detailed,
            "warnings": warnings,
            "method": "最近披露股票持仓加权涨跌 × 股票仓位",
        }

    @staticmethod
    def _normalise_codes(
        codes: list[str], limit: int | None = MAX_FUNDS_PER_REQUEST
    ) -> list[str]:
        normalised = list(
            dict.fromkeys(code.strip() for code in codes if code.strip())
        )
        return normalised if limit is None else normalised[:limit]

    async def _register_active(self, codes: list[str]) -> None:
        now = time.monotonic()
        async with self._state_lock:
            for code in codes:
                self._active_codes[code] = now

    async def _active_code_list(self) -> list[str]:
        cutoff = time.monotonic() - ACTIVE_CODE_TTL
        async with self._state_lock:
            expired = [code for code, seen_at in self._active_codes.items() if seen_at < cutoff]
            for code in expired:
                self._active_codes.pop(code, None)
                self._snapshots.pop(code, None)
                self._snapshot_times.pop(code, None)
                self._last_errors.pop(code, None)
            return list(self._active_codes)

    async def _refresh_codes(
        self,
        codes: list[str],
        *,
        force: bool = False,
        limit: int | None = MAX_FUNDS_PER_REQUEST,
    ) -> None:
        """Refresh snapshots once for a set of funds.

        A global refresh lock prevents a burst of users from duplicating the
        same upstream work. Quote requests are shared across every fund in this
        refresh and split into URL-safe chunks.
        """
        unique_codes = self._normalise_codes(codes, limit=limit)
        if not unique_codes:
            return

        async with self._refresh_lock:
            if not force:
                cutoff = time.monotonic() - REFRESH_INTERVAL
                async with self._state_lock:
                    unique_codes = [
                        code for code in unique_codes
                        if self._snapshot_times.get(code, 0) < cutoff
                    ]
                if not unique_codes:
                    return

            prepared: dict[str, tuple[dict[str, Any], list[dict[str, Any]], str | None]] = {}
            errors: dict[str, str] = {}

            async def prepare(code: str) -> None:
                if not re.fullmatch(r"\d{6}", code):
                    errors[code] = "请输入正确的 6 位基金代码"
                    return
                try:
                    async with self._metadata_semaphore:
                        info, holding_result = await asyncio.gather(
                            self._fund_info(code), self._holdings(code)
                        )
                    holdings, holding_date = holding_result
                    prepared[code] = (info, holdings, holding_date)
                except Exception as exc:
                    logger.exception("fund preparation failed for %s", code)
                    errors[code] = str(exc) or "基金资料暂时不可用"

            await asyncio.gather(*(prepare(code) for code in unique_codes))
            all_holdings = [
                holding
                for _, holdings, _ in prepared.values()
                for holding in holdings
            ]
            quote_refresh_failed = False
            try:
                quotes = await self._quotes(all_holdings)
            except Exception:
                logger.exception("shared quote refresh failed")
                quotes = {}
                quote_refresh_failed = True

            refreshed_at = time.monotonic()
            async with self._state_lock:
                snapshots = {}
                for code, (info, holdings, holding_date) in prepared.items():
                    # If real-time quotes fail, keep the last known good
                    # snapshot. Only brand-new funds receive an official-NAV
                    # fallback until the next background refresh succeeds.
                    if quote_refresh_failed and holdings and code in self._snapshots:
                        continue
                    candidate = self._build_estimate(
                        info, holdings, holding_date, quotes
                    )
                    previous = self._snapshots.get(code)
                    if (
                        previous
                        and holdings
                        and candidate.get("quoteCoveragePct", 0) < 50
                        and previous.get("quoteCoveragePct", 0) > candidate.get("quoteCoveragePct", 0)
                    ):
                        continue
                    snapshots[code] = candidate
                for code, snapshot in snapshots.items():
                    self._snapshots[code] = snapshot
                    self._snapshot_times[code] = refreshed_at
                    self._last_errors.pop(code, None)
                for code, error in errors.items():
                    self._last_errors[code] = error

    async def get_estimates(self, codes: list[str]) -> list[dict[str, Any]]:
        """Return cached snapshots; initialise only previously unseen funds."""
        unique_codes = self._normalise_codes(codes)
        await self._register_active(unique_codes)
        async with self._state_lock:
            missing = [code for code in unique_codes if code not in self._snapshots]
        if missing:
            await self._refresh_codes(missing)

        async with self._state_lock:
            return [
                self._snapshots.get(code)
                or {"code": code, "error": self._last_errors.get(code, "正在初始化估值，请稍后刷新")}
                for code in unique_codes
            ]

    async def estimate_many(self, codes: list[str]) -> list[dict[str, Any]]:
        """Force an immediate shared refresh, mainly for tests and administration."""
        unique_codes = self._normalise_codes(codes)
        await self._register_active(unique_codes)
        await self._refresh_codes(unique_codes, force=True)
        return await self.get_estimates(unique_codes)

    async def estimate(self, code: str) -> dict[str, Any]:
        result = (await self.get_estimates([code]))[0]
        if result.get("error"):
            raise FundDataError(result["error"])
        return result

    async def _refresh_loop(self) -> None:
        while True:
            await asyncio.sleep(REFRESH_INTERVAL)
            codes = await self._active_code_list()
            if not codes:
                continue
            started_at = time.monotonic()
            try:
                # Refresh the whole active universe together. Metadata work is
                # concurrency-limited, while all holdings share one deduplicated
                # quote universe. This avoids repeating the same stock request
                # for different users and fund groups.
                await self._refresh_codes(codes, force=True, limit=None)
                self._last_background_refresh_at = datetime.now(CHINA_TZ).isoformat(
                    timespec="seconds"
                )
                self._last_background_duration_ms = int(
                    (time.monotonic() - started_at) * 1000
                )
                self._last_background_error = None
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._last_background_error = str(exc)
                logger.exception("background snapshot refresh failed")

    async def stats(self) -> dict[str, Any]:
        async with self._state_lock:
            return {
                "activeFunds": len(self._active_codes),
                "cachedSnapshots": len(self._snapshots),
                "lastBackgroundRefreshAt": self._last_background_refresh_at,
                "lastBackgroundDurationMs": self._last_background_duration_ms,
                "lastBackgroundError": self._last_background_error,
            }
