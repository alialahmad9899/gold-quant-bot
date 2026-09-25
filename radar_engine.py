from __future__ import annotations

import json
import math
import os
import re
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, asdict
from datetime import datetime, timezone, timedelta
from typing import Any

import requests


DEX_BASE = "https://api.dexscreener.com"
MORALIS_DEEP = "https://deep-index.moralis.io/api/v2.2"
GOPLUS = "https://api.gopluslabs.io/api/v1"

EVM_ALIASES = {
    "ethereum": "ethereum",
    "eth": "ethereum",
    "bsc": "binance",
    "binance": "binance",
    "base": "base",
    "arbitrum": "arbitrum",
    "polygon": "polygon",
    "optimism": "optimism",
    "avalanche": "avalanche",
}


def _num(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _age_hours(pair_created_at: Any) -> float | None:
    ts = _num(pair_created_at, 0)
    if not ts:
        return None
    return max(0.0, (time.time() - ts / 1000.0) / 3600.0)


def _result_rows(data: Any) -> list[dict]:
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        for key in ("result", "data", "tokens", "pairs", "wallets"):
            value = data.get(key)
            if isinstance(value, list):
                return [x for x in value if isinstance(x, dict)]
    return []


class HTTPStatusError(RuntimeError):
    def __init__(self, status_code: int, url: str, message: str = ""):
        self.status_code = status_code
        self.url = url
        self.message = message
        detail = f"HTTP {status_code}"
        if message:
            detail += f": {message[:220]}"
        super().__init__(detail)


def _provider_error(exc: Exception) -> tuple[str, str]:
    status = getattr(exc, "status_code", None)
    if status == 401:
        return "UNAUTHORIZED", "المفتاح مرفوض (401)"
    if status == 403:
        return "FORBIDDEN", "الوصول مرفوض (403)"
    if status == 404:
        return "NOT_FOUND", "المسار غير موجود (404)"
    if status == 429:
        return "RATE_LIMIT", "تم بلوغ حد الطلبات (429)"
    return "ERROR", str(exc)[:220]


@dataclass
class MarketSnapshot:
    chain: str
    address: str
    pair_address: str
    symbol: str
    name: str
    price_usd: float
    market_cap: float
    fdv: float
    liquidity_usd: float
    volume_5m: float
    volume_1h: float
    volume_6h: float
    volume_24h: float
    buys_1h: int
    sells_1h: int
    buys_24h: int
    sells_24h: int
    price_m5: float
    price_h1: float
    price_h6: float
    price_h24: float
    pair_age_hours: float | None
    boosted: int = 0

    @property
    def buyer_ratio_1h(self) -> float:
        total = self.buys_1h + self.sells_1h
        return self.buys_1h / total if total else 0.5

    @property
    def volume_acceleration(self) -> float:
        hourly_baseline = self.volume_24h / 24.0
        if hourly_baseline <= 0:
            return 0.0
        return self.volume_1h / hourly_baseline


class HTTP:
    def __init__(self, timeout: float = 12.0):
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "CryptoRadar/1.0"})

    def get_json(self, url: str, params: dict[str, Any] | None = None, headers: dict[str, str] | None = None) -> Any:
        last: Exception | None = None
        for attempt in range(3):
            try:
                r = self.session.get(url, params=params, headers=headers, timeout=self.timeout)
                if r.status_code == 429:
                    last = HTTPStatusError(429, r.url, r.text)
                    if attempt < 2:
                        time.sleep(1.5 * (attempt + 1))
                        continue
                    raise last
                if r.status_code >= 400:
                    raise HTTPStatusError(r.status_code, r.url, r.text)
                try:
                    return r.json()
                except ValueError as exc:
                    raise RuntimeError(f"JSON غير صالح من {r.url}: {exc}") from exc
            except HTTPStatusError:
                raise
            except Exception as exc:
                last = exc
                if attempt < 2:
                    time.sleep(0.8 * (attempt + 1))
        raise RuntimeError(str(last) if last else "HTTP request failed")

class DexScreenerClient:
    def __init__(self, http: HTTP):
        self.http = http

    def _latest(self, endpoint: str) -> list[dict]:
        data = self.http.get_json(f"{DEX_BASE}{endpoint}")
        if isinstance(data, list):
            return [x for x in data if isinstance(x, dict)]
        if isinstance(data, dict):
            for key in ("tokens", "profiles", "pairs", "data"):
                if isinstance(data.get(key), list):
                    return [x for x in data[key] if isinstance(x, dict)]
            return [data]
        return []

    def discover_addresses(self) -> list[tuple[str, str, int]]:
        seen: set[tuple[str, str]] = set()
        found: list[tuple[str, str, int]] = []
        sources = (
            ("/token-profiles/latest/v1", 1),
            ("/token-boosts/latest/v1", 2),
            ("/token-boosts/top/v1", 1),
            ("/community-takeovers/latest/v1", 2),
        )
        for endpoint, weight in sources:
            try:
                for item in self._latest(endpoint):
                    chain = str(item.get("chainId") or "").strip().lower()
                    addr = str(item.get("tokenAddress") or "").strip()
                    if not chain or not addr or len(addr) < 10:
                        continue
                    key = (chain, addr.lower())
                    if key not in seen:
                        seen.add(key)
                        found.append((chain, addr, weight))
            except Exception:
                continue
        return found

    def fetch_pairs(self, chain: str, addresses: list[str]) -> list[dict]:
        out: list[dict] = []
        for i in range(0, len(addresses), 30):
            batch = addresses[i:i + 30]
            joined = ",".join(batch)
            try:
                data = self.http.get_json(f"{DEX_BASE}/tokens/v1/{chain}/{joined}")
                if isinstance(data, list):
                    out.extend(x for x in data if isinstance(x, dict))
            except Exception:
                continue
        return out


class GoPlusClient:
    def __init__(self, http: HTTP, api_key: str):
        self.http = http
        self.api_key = api_key.strip()
        self.status = {"state": "CONFIGURED" if self.api_key else "MISSING", "detail": "جاهز للفحص"}

    @staticmethod
    def _extract_token_result(data: Any, address: str) -> dict[str, Any]:
        if not isinstance(data, dict):
            return {}
        raw = data.get("result")
        if not isinstance(raw, dict):
            return {}
        for key in (address, address.lower(), address.upper()):
            value = raw.get(key)
            if isinstance(value, dict):
                return value
        values = [v for v in raw.values() if isinstance(v, dict)]
        if len(values) == 1:
            return values[0]
        flat_keys = {"is_honeypot", "blacklist", "is_open_source", "buy_tax", "sell_tax"}
        if flat_keys.intersection(raw):
            return raw
        return {}

    def check(self, chain: str, address: str) -> dict[str, Any]:
        if not self.api_key:
            self.status = {"state": "MISSING", "detail": "GOPLUS_API_KEY غير مضبوط"}
            return {"status": "UNKNOWN", "reason": "api_key_missing"}

        token = self.api_key[7:].strip() if self.api_key.lower().startswith("bearer ") else self.api_key\n        headers = {"Authorization": f"Bearer {token}"}
        if chain == "solana":
            url = f"{GOPLUS}/solana/token_security"
        else:
            chain_id = {
                "ethereum": "1", "eth": "1", "bsc": "56", "binance": "56",
                "base": "8453", "arbitrum": "42161", "polygon": "137",
                "optimism": "10", "avalanche": "43114",
            }.get(chain)
            if not chain_id:
                self.status = {"state": "UNSUPPORTED", "detail": f"السلسلة {chain} غير مدعومة"}
                return {"status": "UNKNOWN", "reason": "chain_not_supported"}
            url = f"{GOPLUS}/token_security/{chain_id}"

        started = time.monotonic()
        try:
            data = self.http.get_json(url, params={"contract_addresses": address}, headers=headers)
            elapsed_ms = round((time.monotonic() - started) * 1000)
            if isinstance(data, dict) and data.get("code") not in (None, 1, "1"):
                detail = str(data.get("message") or f"GoPlus code={data.get('code')}")
                self.status = {"state": "REJECTED", "detail": detail, "latency_ms": elapsed_ms}
                return {"status": "UNKNOWN", "reason": "goplus_rejected", "provider_message": detail}

            result = self._extract_token_result(data, address)
            if not result:
                self.status = {"state": "EMPTY", "detail": "GoPlus أعاد نتيجة بدون بيانات توكن", "latency_ms": elapsed_ms}
                return {"status": "UNKNOWN", "reason": "no_token_result"}

            def flag(name: str) -> bool:
                return str(result.get(name)).lower() in {"1", "true", "yes"}

            blockers = []
            for field, reason in (
                ("is_honeypot", "honeypot"),
                ("blacklist", "blacklist"),
                ("is_blacklisted", "blacklisted"),
                ("can_take_back_ownership", "take_back_ownership"),
                ("owner_change_balance", "owner_can_change_balance"),
                ("hidden_owner", "hidden_owner"),
                ("selfdestruct", "selfdestruct"),
            ):
                if flag(field):
                    blockers.append(reason)

            buy_tax = _num(result.get("buy_tax"))
            sell_tax = _num(result.get("sell_tax"))
            if buy_tax > 10:
                blockers.append("high_buy_tax")
            if sell_tax > 10:
                blockers.append("high_sell_tax")

            soft_flags = []
            if not flag("is_open_source"):
                soft_flags.append("not_open_source")
            if flag("is_proxy"):
                soft_flags.append("proxy")
            if flag("is_mintable"):
                soft_flags.append("mintable")

            status = "FAIL" if blockers else "PASS"
            self.status = {
                "state": "OK",
                "detail": f"فحص أمني ناجح: {status}",
                "latency_ms": elapsed_ms,
            }
            return {
                "status": status,
                "blockers": blockers,
                "soft_flags": soft_flags,
                "buy_tax": buy_tax,
                "sell_tax": sell_tax,
                "open_source": flag("is_open_source"),
                "proxy": flag("is_proxy"),
                "mintable": flag("is_mintable"),
                "raw": result,
            }
        except Exception as exc:
            state, detail = _provider_error(exc)
            self.status = {"state": state, "detail": detail}
            return {
                "status": "UNKNOWN",
                "reason": "provider_error",
                "provider_state": state,
                "provider_detail": detail,
            }


class MoralisClient:
    """Optional on-chain intelligence layer using the current Moralis Token/Wallet APIs."""

    def __init__(self, http: HTTP, api_key: str):
        self.http = http
        self.api_key = api_key.strip()
        self.status = {"state": "CONFIGURED" if self.api_key else "MISSING", "detail": "جاهز للفحص"}

    @staticmethod
    def chain_alias(chain: str) -> str | None:
        return EVM_ALIASES.get(chain)

    def trending(self) -> list[dict]:
        if not self.api_key:
            self.status = {"state": "MISSING", "detail": "MORALIS_API_KEY غير مضبوط"}
            return []
        try:
            data = self.http.get_json(
                f"{MORALIS_DEEP}/tokens/trending",
                params={"limit": 100},
                headers={"X-API-Key": self.api_key},
            )
            rows = _result_rows(data)
            self.status = {"state": "OK", "detail": f"Trending: {len(rows)} توكن"}
            return rows
        except Exception as exc:
            state, detail = _provider_error(exc)
            self.status = {"state": state, "detail": detail}
            return []

    def top_traders(self, chain: str, address: str) -> list[dict]:
        alias = self.chain_alias(chain)
        if not self.api_key:
            self.status = {"state": "MISSING", "detail": "MORALIS_API_KEY غير مضبوط"}
            return []
        if not alias:
            self.status = {"state": "UNSUPPORTED", "detail": f"السلسلة {chain} غير مدعومة في Moralis"}
            return []
        try:
            data = self.http.get_json(
                f"{MORALIS_DEEP}/erc20/{address}/top-gainers",
                params={"chain": alias, "limit": 10},
                headers={"X-API-Key": self.api_key},
            )
            rows = _result_rows(data)
            self.status = {"state": "OK", "detail": f"Top Traders: {len(rows)} محفظة"}
            return rows
        except Exception as exc:
            state, detail = _provider_error(exc)
            self.status = {"state": state, "detail": detail}
            return []

    def recent_wallet_swaps(self, chain: str, wallet: str, token: str) -> list[dict]:
        alias = self.chain_alias(chain)
        if not self.api_key or not alias:
            return []
        try:
            data = self.http.get_json(
                f"{MORALIS_DEEP}/wallets/{wallet}/swaps",
                params={
                    "chain": alias,
                    "tokenAddress": token,
                    "limit": 20,
                    "order": "DESC",
                },
                headers={"X-API-Key": self.api_key},
            )
            return _result_rows(data)
        except Exception as exc:
            state, detail = _provider_error(exc)
            self.status = {"state": state, "detail": detail}
            return []

    def analyze(self, chain: str, address: str) -> dict[str, Any]:
        rows = self.top_traders(chain, address)
        if not rows:
            return {
                "available": False,
                "smart_money_count": 0,
                "current_buy_usd": 0.0,
                "current_sell_usd": 0.0,
                "net_flow_usd": 0.0,
                "wallets": [],
            }

        good = []
        for row in rows[:10]:
            pnl = _num(row.get("totalPnlUsd", row.get("realizedPnlUsd")))
            roi = _num(row.get("roi", row.get("totalPnlPercent")))
            wallet = str(row.get("address") or row.get("walletAddress") or row.get("wallet") or "").strip()
            if wallet and (pnl > 0 or roi > 0):
                good.append((wallet, pnl, roi))

        buy_usd = sell_usd = 0.0
        wallets = []
        for wallet, pnl, roi in good[:5]:
            swaps = self.recent_wallet_swaps(chain, wallet, address)
            last_action = None
            for swap in swaps[:20]:
                action = str(swap.get("transactionType") or swap.get("type") or swap.get("side") or "").lower()
                value = _num(swap.get("totalValueUsd") or swap.get("valueUsd") or swap.get("value"))
                if action in {"buy", "swap_buy"}:
                    buy_usd += value
                    last_action = "BUY"
                elif action in {"sell", "swap_sell"}:
                    sell_usd += value
                    last_action = "SELL"
            wallets.append({
                "wallet": wallet,
                "roi": roi,
                "pnl_usd": pnl,
                "recent_action": last_action,
            })

        self.status["state"] = "OK"
        self.status["detail"] = f"Smart Money: {len(good)} محافظ مربحة"
        return {
            "available": True,
            "smart_money_count": len(good),
            "current_buy_usd": buy_usd,
            "current_sell_usd": sell_usd,
            "net_flow_usd": buy_usd - sell_usd,
            "wallets": wallets,
        }


class NewsClient:
    DEFAULT_FEEDS = (
        "https://www.coindesk.com/arc/outboundfeeds/rss/",
        "https://cointelegraph.com/rss",
        "https://decrypt.co/feed",
    )

    def __init__(self, feeds: str | None = None):
        configured = tuple(x.strip() for x in (feeds or "").split(",") if x.strip())
        self.feeds = configured or self.DEFAULT_FEEDS
        self.timeout = 8.0

    def _fetch(self, url: str) -> bytes:
        req = urllib.request.Request(url, headers={"User-Agent": "CryptoRadar-News/1.0"})
        with urllib.request.urlopen(req, timeout=self.timeout) as response:
            return response.read()

    def _parse_rss(self, raw: bytes) -> list[dict]:
        try:
            root = ET.fromstring(raw)
        except ET.ParseError:
            return []
        out = []
        for item in root.findall(".//item"):
            title = re.sub(r"\s+", " ", item.findtext("title") or "").strip()
            link = re.sub(r"\s+", " ", item.findtext("link") or "").strip()
            desc = re.sub(r"<[^>]+>", " ", item.findtext("description") or "")
            desc = re.sub(r"\s+", " ", desc).strip()
            if title and link:
                out.append({"title": title, "url": link, "summary": desc})
        return out

    def latest(self) -> list[dict]:
        articles = []
        for feed in self.feeds:
            try:
                articles.extend(self._parse_rss(self._fetch(feed)))
            except Exception:
                continue
        try:
            q = urllib.parse.quote('(crypto OR cryptocurrency OR bitcoin OR ethereum OR solana OR token)')
            url = (
                "https://api.gdeltproject.org/api/v2/doc/doc"
                f"?query={q}&mode=artlist&format=json&maxrecords=25&sort=datedesc"
            )
            raw = self._fetch(url)
            data = json.loads(raw.decode("utf-8"))
            for item in data.get("articles", []):
                title = str(item.get("title") or "").strip()
                link = str(item.get("url") or "").strip()
                if title and link:
                    articles.append({"title": title, "url": link, "summary": "", "source": item.get("domain", "GDELT")})
        except Exception:
            pass
        return articles[:100]

    @staticmethod
    def match(articles: list[dict], symbol: str, name: str) -> dict[str, Any]:
        terms = [x.lower() for x in (symbol, name) if x and len(x) >= 3]
        if not terms:
            return {"mentions": 0, "headlines": []}
        hits = []
        for article in articles:
            text = f"{article.get('title','')} {article.get('summary','')}".lower()
            if any(re.search(rf"\b{re.escape(term)}\b", text) for term in terms):
                hits.append(article)
        return {"mentions": len(hits), "headlines": hits[:3]}


def market_score(m: MarketSnapshot) -> float:
    # This is an opportunity/risk score, not a prediction of future return.
    score = 0.0

    liq = m.liquidity_usd
    if 25_000 <= liq <= 750_000:
        score += 14
    elif liq > 750_000:
        score += 10
    elif liq >= 10_000:
        score += 5

    cap = m.market_cap or m.fdv
    if 100_000 <= cap <= 10_000_000:
        score += 10
    elif 10_000_000 < cap <= 25_000_000:
        score += 6
    elif 25_000_000 < cap:
        score += 2

    accel = m.volume_acceleration
    score += _clamp((accel - 0.5) * 5.0, 0.0, 14.0)

    buyer_edge = (m.buyer_ratio_1h - 0.5) * 2.0
    score += _clamp(buyer_edge * 12.0, 0.0, 12.0)

    # Prefer early positive momentum, but penalize already-vertical moves.
    if 2 <= m.price_h1 <= 35:
        score += 10
    elif m.price_h1 > 80:
        score -= 8
    elif m.price_h1 > 35:
        score += 4

    if m.price_m5 > 0 and m.price_m5 < 8:
        score += 5
    elif m.price_m5 < -8:
        score -= 5

    if m.price_h6 > 0:
        score += 3
    if m.pair_age_hours is not None and m.pair_age_hours <= 720:
        score += 4

    if m.boosted:
        # Paid boosts are visibility signals, not quality signals.
        score += _clamp(m.boosted, 0, 3)

    return _clamp(score, 0.0, 60.0)


def candidate_signal(candidate: dict[str, Any]) -> str:
    m = candidate.get("market", {})
    score = _num(candidate.get("score"))
    sec = candidate.get("security_status")
    completeness = _num(candidate.get("data_completeness"))
    buyers = _num(m.get("buys_1h"))
    sells = _num(m.get("sells_1h"))
    total = buyers + sells
    buyer_ratio = buyers / total if total else 0.5
    accel = _num(m.get("volume_acceleration"))
    h1 = _num(m.get("price_h1"))
    liquidity = _num(m.get("liquidity_usd"))
    risk_flags = set(candidate.get("risk_flags", []))
    sm = candidate.get("smart_money", {}) or {}

    if sec == "FAIL" or liquidity < 20_000:
        return "AVOID"
    if "sell_pressure" in risk_flags or "already_vertical" in risk_flags:
        return "WATCH"
    if (
        sec == "PASS"
        and completeness >= 0.75
        and score >= 78
        and buyer_ratio >= 0.58
        and accel >= 1.10
        and 1.0 <= h1 <= 35
        and sm.get("available")\n        and sm.get("net_flow_usd", 0) >= 0
    ):
        return "BUY_WATCH"
    if score >= 52 and completeness >= 0.5:
        return "WATCH"
    return "AVOID"


class RadarEngine:
    def __init__(self):
        self.http = HTTP()
        self.dex = DexScreenerClient(self.http)
        self.moralis = MoralisClient(self.http, os.getenv("MORALIS_API_KEY", ""))
        goplus_key = os.getenv("GOPLUS_API_KEY", "")
        self.goplus = GoPlusClient(self.http, goplus_key) if goplus_key else None
        self.news = NewsClient(os.getenv("NEWS_FEEDS", ""))
        self.min_liquidity = _num(os.getenv("MIN_LIQUIDITY_USD"), 20_000)
        self.min_market_cap = _num(os.getenv("MIN_MARKET_CAP_USD"), 50_000)
        self.max_market_cap = _num(os.getenv("MAX_MARKET_CAP_USD"), 25_000_000)
        self.max_candidates = int(os.getenv("MAX_CANDIDATES", "12"))
        self.security_limit = int(os.getenv("SECURITY_CHECK_LIMIT", "25"))
        self.smart_money_limit = int(os.getenv("SMART_MONEY_CHECK_LIMIT", "10"))
        self.last_scan = {"discovered": 0, "market_rows": 0, "candidates": 0}
        self.last_provider_status = {}

    def _market_rows(self) -> list[MarketSnapshot]:
        discovered = self.dex.discover_addresses()

        # Moralis trending is a second discovery path when its key is configured.
        trending = self.moralis.trending()
        for item in trending[:100]:
            chain = str(item.get("chainId") or "").lower()
            address = str(item.get("tokenAddress") or "")
            if chain and address:
                discovered.append((chain, address, 0))

        by_chain: dict[str, list[tuple[str, int]]] = {}
        seen: set[tuple[str, str]] = set()
        for chain, address, weight in discovered:
            key = (chain, address.lower())
            if key in seen:
                continue
            seen.add(key)
            by_chain.setdefault(chain, []).append((address, weight))

        rows: list[MarketSnapshot] = []
        for chain, values in by_chain.items():
            addresses = [x[0] for x in values]
            boost_map = {x[0].lower(): x[1] for x in values}
            for pair in self.dex.fetch_pairs(chain, addresses):
                base = pair.get("baseToken") or {}
                address = str(base.get("address") or "")
                if not address or address.lower() not in boost_map:
                    continue
                quote = pair.get("quoteToken") or {}
                price = _num(pair.get("priceUsd"))
                if price <= 0:
                    continue
                liq = _num((pair.get("liquidity") or {}).get("usd"))
                cap = _num(pair.get("marketCap"))
                fdv = _num(pair.get("fdv"))
                if liq < self.min_liquidity:
                    continue
                if cap and (cap < self.min_market_cap or cap > self.max_market_cap):
                    continue
                volume = pair.get("volume") or {}
                chg = pair.get("priceChange") or {}
                tx = pair.get("txns") or {}
                t1 = tx.get("h1") or {}
                t24 = tx.get("h24") or {}
                row = MarketSnapshot(
                    chain=chain,
                    address=address,
                    pair_address=str(pair.get("pairAddress") or ""),
                    symbol=str(base.get("symbol") or "?"),
                    name=str(base.get("name") or "?"),
                    price_usd=price,
                    market_cap=cap,
                    fdv=fdv,
                    liquidity_usd=liq,
                    volume_5m=_num(volume.get("m5")),
                    volume_1h=_num(volume.get("h1")),
                    volume_6h=_num(volume.get("h6")),
                    volume_24h=_num(volume.get("h24")),
                    buys_1h=int(_num(t1.get("buys"))),
                    sells_1h=int(_num(t1.get("sells"))),
                    buys_24h=int(_num(t24.get("buys"))),
                    sells_24h=int(_num(t24.get("sells"))),
                    price_m5=_num(chg.get("m5")),
                    price_h1=_num(chg.get("h1")),
                    price_h6=_num(chg.get("h6")),
                    price_h24=_num(chg.get("h24")),
                    pair_age_hours=_age_hours(pair.get("pairCreatedAt")),
                    boosted=boost_map.get(address.lower(), 0),
                )
                rows.append(row)

        # Use the most liquid pair for each token.
        rows.sort(key=lambda x: (x.liquidity_usd, market_score(x)), reverse=True)
        dedup: dict[tuple[str, str], MarketSnapshot] = {}
        for row in rows:
            dedup.setdefault((row.chain, row.address.lower()), row)
        return list(dedup.values())

    def diagnose(self) -> dict[str, Any]:
        result = {
            "dex": dict(self.dex.status),
            "moralis": dict(self.moralis.status),
            "goplus": (
                dict(self.goplus.status)
                if self.goplus
                else {"state": "MISSING", "detail": "GOPLUS_API_KEY غير مضبوط"}
            ),
        }

        try:
            discovered = self.dex.discover_addresses()
            result["dex"] = {
                "state": self.dex.status.get("state"),
                "detail": f"اكتشاف DEX: {len(discovered)} عنوان",
            }
        except Exception:
            result["dex"] = dict(self.dex.status)

        if self.moralis.api_key:
            self.moralis.trending()
            result["moralis"] = dict(self.moralis.status)

        if self.goplus:
            probe = self.goplus.check(
                "ethereum",
                "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
            )
            result["goplus"] = dict(self.goplus.status)
            result["goplus"]["probe_status"] = probe.get("status")

        self.last_provider_status = result
        return result

    def scan(self) -> list[dict[str, Any]]:
        rows = self._market_rows()
        self.last_scan["discovered"] = self.dex.status.get("discovered", 0)
        self.last_scan["market_rows"] = len(rows)

        rows.sort(key=market_score, reverse=True)
        rows = rows[: max(self.security_limit, self.smart_money_limit, self.max_candidates)]

        articles = self.news.latest()
        candidates: list[dict[str, Any]] = []
        for idx, m in enumerate(rows):
            base = market_score(m)

            security = {"status": "UNKNOWN"}
            if self.goplus and idx < self.security_limit:
                security = self.goplus.check(m.chain, m.address)

            sm = {
                "available": False,
                "smart_money_count": 0,
                "net_flow_usd": 0.0,
                "current_buy_usd": 0.0,
                "current_sell_usd": 0.0,
                "wallets": [],
            }
            if self.moralis.api_key and idx < self.smart_money_limit:
                sm = self.moralis.analyze(m.chain, m.address)

            news = NewsClient.match(articles, m.symbol, m.name)

            score = base
            if security.get("status") == "PASS":
                score += 22
            elif security.get("status") == "FAIL":
                score -= 35
            if sm.get("available"):
                score += _clamp(sm.get("smart_money_count", 0) * 2.0, 0, 10)
                net = sm.get("net_flow_usd", 0.0)
                score += 9 if net > 50_000 else 5 if net > 10_000 else 0
            if news.get("mentions", 0) > 0:
                score += min(4, news["mentions"])

            available_components = 4
            present = (
                1
                + int(security.get("status") != "UNKNOWN")
                + int(sm.get("available"))
                + int(news.get("mentions", 0) > 0)
            )
            completeness = round(present / available_components, 2)

            risk_flags = list(security.get("blockers", []))
            risk_flags.extend(security.get("soft_flags", [])[:3])
            if m.liquidity_usd < 50_000:
                risk_flags.append("low_liquidity")
            if m.volume_acceleration > 10:
                risk_flags.append("extreme_volume_acceleration")
            if m.price_h1 > 80:
                risk_flags.append("already_vertical")
            if m.sells_1h > m.buys_1h * 1.5:
                risk_flags.append("sell_pressure")

            candidate = {
                "token_key": f"{m.chain}:{m.address.lower()}",
                "chain": m.chain,
                "address": m.address,
                "symbol": m.symbol,
                "name": m.name,
                "url": f"https://dexscreener.com/{m.chain}/{m.pair_address}",
                "score": round(_clamp(score, 0, 100), 1),
                "market_score": round(base, 1),
                "security_status": security.get("status", "UNKNOWN"),
                "security": security,
                "smart_money": sm,
                "news": news,
                "data_completeness": completeness,
                "risk_flags": risk_flags,
                "market": asdict(m),
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            candidate["signal"] = candidate_signal(candidate)
            candidates.append(candidate)

        candidates.sort(
            key=lambda x: (
                x["signal"] != "BUY_WATCH",
                -x["score"],
                x["security_status"] != "PASS",
            )
        )
        self.last_scan["candidates"] = min(len(candidates), self.max_candidates)
        self.last_provider_status = {
            "dex": dict(self.dex.status),
            "moralis": dict(self.moralis.status),
            "goplus": (
                dict(self.goplus.status)
                if self.goplus
                else {"state": "MISSING", "detail": "GOPLUS_API_KEY غير مضبوط"}
            ),
        }
        return candidates[: self.max_candidates]
