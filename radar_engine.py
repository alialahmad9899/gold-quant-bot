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


def _first_dict(data: Any) -> dict:
    if isinstance(data, dict):
        return data
    if isinstance(data, list) and data and isinstance(data[0], dict):
        return data[0]
    return {}


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
        last = None
        for attempt in range(3):
            try:
                r = self.session.get(url, params=params, headers=headers, timeout=self.timeout)
                if r.status_code == 429:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                r.raise_for_status()
                return r.json()
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

    def check(self, chain: str, address: str) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {self.api_key}"}
        if chain == "solana":
            url = f"{GOPLUS}/solana/token_security"
        else:
            chain_id = {
                "ethereum": "1", "eth": "1", "bsc": "56", "binance": "56",
                "base": "8453", "arbitrum": "42161", "polygon": "137",
                "optimism": "10", "avalanche": "43114",
            }.get(chain)
            if not chain_id:
                return {"status": "UNKNOWN", "reason": "chain_not_supported"}
            url = f"{GOPLUS}/token_security/{chain_id}"
        data = self.http.get_json(url, params={"contract_addresses": address}, headers=headers)
        result = _first_dict(data.get("result") if isinstance(data, dict) else {})
        if not result:
            return {"status": "UNKNOWN", "reason": "no_result"}

        def flag(name: str) -> bool:
            value = result.get(name)
            return str(value).lower() in {"1", "true", "yes"}

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

        status = "FAIL" if blockers else "PASS"
        return {
            "status": status,
            "blockers": blockers,
            "buy_tax": buy_tax,
            "sell_tax": sell_tax,
            "open_source": flag("is_open_source"),
            "proxy": flag("is_proxy"),
            "mintable": flag("is_mintable"),
            "raw": result,
        }


class MoralisClient:
    """Optional smart-money layer. It is deliberately not required for market scanning."""

    def __init__(self, http: HTTP, api_key: str):
        self.http = http
        self.api_key = api_key.strip()

    def trending(self) -> list[dict]:
        if not self.api_key:
            return []
        try:
            data = self.http.get_json(
                f"{MORALIS_DEEP}/tokens/trending",
                params={"limit": 100},
                headers={"X-API-Key": self.api_key},
            )
            return data if isinstance(data, list) else []
        except Exception:
            return []

    def top_traders(self, chain: str, address: str) -> dict[str, Any]:
        alias = EVM_ALIASES.get(chain)
        if not self.api_key or not alias:
            return {}
        try:
            return self.http.get_json(
                f"{MORALIS_UNIVERSAL}/chains/{alias}/tokens/{address}/top-traders",
                params={
                    "period": "30",
                    "sortBy": "totalPnl",
                    "excludeLowLiquidity": "true",
                    "minTradeCount": 3,
                    "limit": 10,
                },
                headers={"X-Api-Key": self.api_key},
            )
        except Exception:
            return {}

    def recent_wallet_swaps(self, chain: str, wallet: str, token: str) -> list[dict]:
        if not self.api_key or chain not in EVM_ALIASES:
            return []
        alias = {"ethereum": "eth", "bsc": "bsc", "binance": "bsc"}.get(chain, chain)
        since = (datetime.now(timezone.utc) - timedelta(hours=12)).strftime("%Y-%m-%dT%H:%M:%SZ")
        try:
            data = self.http.get_json(
                f"{MORALIS_DEEP}/wallets/{wallet}/swaps",
                params={
                    "chain": alias,
                    "tokenAddress": token,
                    "fromDate": since,
                    "limit": 20,
                    "order": "DESC",
                },
                headers={"X-API-Key": self.api_key},
            )
            return data.get("result", []) if isinstance(data, dict) else []
        except Exception:
            return []

    def analyze(self, chain: str, address: str) -> dict[str, Any]:
        data = self.top_traders(chain, address)
        rows = data.get("result", []) if isinstance(data, dict) else []
        if not rows:
            return {"available": False, "smart_money_count": 0, "current_buy_usd": 0.0,
                    "current_sell_usd": 0.0, "net_flow_usd": 0.0, "wallets": []}

        good = []
        for row in rows[:5]:
            pnl = _num(row.get("totalPnlUsd"))
            roi = _num(row.get("roi"))
            wallet = str(row.get("walletAddress") or "").strip()
            if wallet and (pnl > 0 or roi > 0):
                good.append((wallet, row))

        buy_usd = sell_usd = 0.0
        wallets = []
        for wallet, row in good[:3]:
            swaps = self.recent_wallet_swaps(chain, wallet, address)
            last_action = None
            for swap in swaps[:5]:
                action = str(swap.get("transactionType") or "").lower()
                value = _num(swap.get("totalValueUsd"))
                if action == "buy":
                    buy_usd += value
                    last_action = "BUY"
                elif action == "sell":
                    sell_usd += value
                    last_action = "SELL"
            wallets.append({
                "wallet": wallet,
                "roi": roi,
                "pnl_usd": pnl,
                "recent_action": last_action,
            })

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

    def scan(self) -> list[dict[str, Any]]:
        rows = self._market_rows()
        rows.sort(key=market_score, reverse=True)
        rows = rows[: max(self.security_limit, self.smart_money_limit, self.max_candidates)]

        articles = self.news.latest()
        candidates: list[dict[str, Any]] = []
        for idx, m in enumerate(rows):
            base = market_score(m)
            security = {"status": "UNKNOWN"}
            if self.goplus and idx < self.security_limit:
                try:
                    security = self.goplus.check(m.chain, m.address)
                except Exception as exc:
                    security = {"status": "UNKNOWN", "reason": str(exc)}

            sm = {"available": False, "smart_money_count": 0, "net_flow_usd": 0.0, "current_buy_usd": 0.0, "current_sell_usd": 0.0, "wallets": []}
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

            available_components = 4  # market, security, smart money, news
            present = 1 + int(security.get("status") != "UNKNOWN") + int(sm.get("available")) + int(news.get("mentions", 0) > 0)
            completeness = round(present / available_components, 2)

            risk_flags = list(security.get("blockers", []))
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
            candidates.append(candidate)

        candidates.sort(key=lambda x: x["score"], reverse=True)
        return candidates[: self.max_candidates]