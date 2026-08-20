"""Live news intelligence for XAU/USD.

News is a separate information channel from Twelve Data. It never replaces the
Twelve Data gold feed. The engine is deliberately flexible: news can trigger an
entry only after material impact plus price confirmation, and active trades are
reassessed rather than automatically closed on headlines alone.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

DEFAULT_FEEDS = (
    "https://www.cnbc.com/id/100003114/device/rss/rss.html",
    "https://feeds.bbci.co.uk/news/business/rss.xml",
)
GDELT_URL = "https://api.gdeltproject.org/api/v2/doc/doc?query={query}&mode=artlist&format=json&maxrecords=25&sort=datedesc"

GOLD_TERMS = re.compile(r"\b(gold|xau|bullion|precious metals)\b", re.I)
MACRO_TERMS = re.compile(
    r"\b(fed|federal reserve|fomc|powell|cpi|inflation|pce|nfp|nonfarm|payroll|interest rate|rates|yield|treasury|dollar|dxy|jobs|unemployment|ecb|boj|central bank|war|sanction|geopolitical|tariff)\b",
    re.I,
)
BULLISH_GOLD = re.compile(r"\b(rate cut|cuts rates|dovish|easing|lower rates|weaker dollar|dollar falls|falling yields|safe haven|war|escalation|sanction|inflation rises)\b", re.I)
BEARISH_GOLD = re.compile(r"\b(rate hike|hikes rates|hawkish|higher rates|strong dollar|dollar rises|rising yields|yield rises|inflation cools|ceasefire)\b", re.I)
HIGH_IMPACT = re.compile(r"\b(fomc|fed|powell|cpi|pce|nfp|nonfarm|payroll|interest rate|rate decision|emergency|war|sanction)\b", re.I)


@dataclass(frozen=True)
class NewsArticle:
    title: str
    summary: str
    url: str
    source: str
    published_at: datetime

    @property
    def text(self) -> str:
        return f"{self.title} {self.summary}".strip()


@dataclass(frozen=True)
class NewsImpact:
    direction: str
    impact: int
    confidence: int
    urgency: str
    reasons: list[str]
    material: bool


@dataclass(frozen=True)
class NewsDecision:
    action: str
    direction: str
    impact: int
    confidence: int
    urgency: str
    reason: str
    conflict: bool = False
    article: dict[str, Any] | None = None


def _parse_date(value: str | None) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return datetime.now(timezone.utc)


def _strip(text: str | None) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text or "")).strip()


def _impact_score(text: str) -> NewsImpact:
    gold_direct = bool(GOLD_TERMS.search(text))
    macro = bool(MACRO_TERMS.search(text))
    bull = len(BULLISH_GOLD.findall(text))
    bear = len(BEARISH_GOLD.findall(text))
    high = bool(HIGH_IMPACT.search(text))
    if bull > bear:
        direction = "BULLISH_GOLD"
    elif bear > bull:
        direction = "BEARISH_GOLD"
    else:
        direction = "NEUTRAL"
    impact = min(100, (35 if gold_direct else 0) + (25 if macro else 0) + 18 * min(2, max(bull, bear)) + (15 if high else 0))
    if direction == "NEUTRAL":
        impact = min(impact, 25)
    confidence = min(95, max(20, 40 + (20 if gold_direct else 0) + (15 if macro else 0) + (15 if high else 0) + 10 * min(2, abs(bull - bear))))
    urgency = "HIGH" if high and impact >= 60 else "MEDIUM" if impact >= 45 else "LOW"
    reasons = []
    if gold_direct: reasons.append("ذكر مباشر للذهب/XAU")
    if macro: reasons.append("خبر اقتصادي مؤثر على الدولار/العوائد")
    if bull > bear: reasons.append("المحتوى يميل لدعم الذهب")
    if bear > bull: reasons.append("المحتوى يميل للضغط على الذهب")
    return NewsImpact(direction, impact, confidence, urgency, reasons, impact >= 45 and direction != "NEUTRAL")


def classify_gold_impact(article: NewsArticle) -> NewsImpact:
    return _impact_score(article.text)


class NewsIntelligence:
    def __init__(self, feeds: tuple[str, ...] | None = None):
        configured = os.getenv("NEWS_FEEDS", "").strip()
        self.feeds = feeds or tuple(x.strip() for x in configured.split(",") if x.strip()) or DEFAULT_FEEDS
        self.gdelt_enabled = os.getenv("NEWS_GDELT_ENABLED", "1") == "1"
        self.timeout = float(os.getenv("NEWS_HTTP_TIMEOUT", "8"))
        self.max_age_minutes = int(os.getenv("NEWS_MAX_AGE_MINUTES", "180"))
        self.poll_seconds = int(os.getenv("NEWS_POLL_SECONDS", "60"))
        self._seen: set[str] = set()
        self._lock = threading.RLock()
        self.last_poll = 0.0

    @staticmethod
    def _key(article: NewsArticle) -> str:
        return hashlib.sha256(f"{article.url}|{article.title}".encode("utf-8")).hexdigest()

    def _fetch(self, url: str) -> bytes:
        req = urllib.request.Request(url, headers={"User-Agent": "GoldQuantBot-News/1.0"})
        with urllib.request.urlopen(req, timeout=self.timeout) as response:
            return response.read()

    def _parse_rss(self, raw: bytes, source_url: str) -> list[NewsArticle]:
        root = ET.fromstring(raw)
        articles: list[NewsArticle] = []
        for item in root.findall(".//item"):
            title = _strip(item.findtext("title"))
            url = _strip(item.findtext("link"))
            summary = _strip(item.findtext("description"))
            published = _parse_date(_strip(item.findtext("pubDate")))
            if title and url:
                articles.append(NewsArticle(title, summary, url, urllib.parse.urlparse(source_url).netloc, published))
        return articles

    def _fetch_gdelt(self) -> list[NewsArticle]:
        query = urllib.parse.quote('(gold OR XAU OR bullion OR "Federal Reserve" OR FOMC OR CPI OR NFP OR Powell)')
        raw = self._fetch(GDELT_URL.format(query=query))
        data = json.loads(raw.decode("utf-8"))
        articles = []
        for item in data.get("articles", []):
            title = _strip(item.get("title")); url = str(item.get("url") or "").strip()
            if not title or not url: continue
            articles.append(NewsArticle(title, _strip(item.get("seendate")), url, str(item.get("domain") or "GDELT"), _parse_date(str(item.get("seendate") or ""))))
        return articles

    def fetch_latest(self) -> list[NewsArticle]:
        articles: list[NewsArticle] = []
        for feed in self.feeds:
            try:
                articles.extend(self._parse_rss(self._fetch(feed), feed))
            except Exception:
                continue
        if self.gdelt_enabled:
            try: articles.extend(self._fetch_gdelt())
            except Exception: pass
        now = datetime.now(timezone.utc)
        fresh: list[NewsArticle] = []
        with self._lock:
            for article in articles:
                if (now - article.published_at).total_seconds() > self.max_age_minutes * 60: continue
                key = self._key(article)
                if key in self._seen: continue
                self._seen.add(key); fresh.append(article)
            if len(self._seen) > 5000:
                self._seen = set(list(self._seen)[-2500:])
            self.last_poll = time.monotonic()
        return fresh

    def ingest(self, articles: list[NewsArticle]) -> list[NewsArticle]:
        fresh = []
        with self._lock:
            for article in articles:
                key = self._key(article)
                if key in self._seen: continue
                self._seen.add(key); fresh.append(article)
        return fresh

    def evaluate_news_entry(self, article: NewsArticle, price_change_pct: float, price_direction: str) -> NewsDecision:
        impact = classify_gold_impact(article)
        aligned = (impact.direction == "BULLISH_GOLD" and price_direction.upper() == "UP") or (impact.direction == "BEARISH_GOLD" and price_direction.upper() == "DOWN")
        if not impact.material:
            return NewsDecision("NO_TRADE", impact.direction, impact.impact, impact.confidence, impact.urgency, "الخبر غير مؤثر بما يكفي على الذهب.", article=asdict(article))
        if impact.impact >= 70 and not aligned:
            return NewsDecision("WAIT_CONFIRMATION", impact.direction, impact.impact, impact.confidence, impact.urgency, "الخبر قوي لكن حركة السعر لم تؤكد الاتجاه بعد.", article=asdict(article))
        if abs(float(price_change_pct)) < 0.20:
            return NewsDecision("WAIT_CONFIRMATION", impact.direction, impact.impact, impact.confidence, impact.urgency, "نحتاج تأكيداً سعرياً بسيطاً قبل الدخول الفوري.", article=asdict(article))
        action = "NEWS_BUY" if impact.direction == "BULLISH_GOLD" and aligned else "NEWS_SELL" if impact.direction == "BEARISH_GOLD" and aligned else "NO_TRADE"
        return NewsDecision(action, impact.direction, impact.impact, impact.confidence, impact.urgency, "خبر مؤثر مع تأكيد سعري متوافق.", article=asdict(article))

    def evaluate_active_trade(self, direction: str, article: NewsArticle, price_change_pct: float, price_direction: str) -> NewsDecision:
        impact = classify_gold_impact(article)
        trade_dir = direction.upper()
        conflict = (trade_dir == "BUY" and impact.direction == "BEARISH_GOLD") or (trade_dir == "SELL" and impact.direction == "BULLISH_GOLD")
        aligned_price = (trade_dir == "BUY" and price_direction.upper() == "DOWN") or (trade_dir == "SELL" and price_direction.upper() == "UP")
        if not conflict or impact.impact < 60:
            return NewsDecision("REASSESS", impact.direction, impact.impact, impact.confidence, impact.urgency, "الخبر لا يثبت وحده بطلان الصفقة؛ إعادة تقييم فقط.", False, asdict(article))
        if aligned_price and abs(float(price_change_pct)) >= 0.30 and impact.impact >= 75:
            return NewsDecision("EXIT", impact.direction, impact.impact, impact.confidence, impact.urgency, "خبر قوي ضد الصفقة مع تأكيد سعري معاكس.", True, asdict(article))
        return NewsDecision("REDUCE_RISK", impact.direction, impact.impact, impact.confidence, impact.urgency, "خبر مؤثر ضد الصفقة؛ تقليل المخاطرة وإعادة تقييم البنية.", True, asdict(article))
