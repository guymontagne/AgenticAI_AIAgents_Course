#!/usr/bin/env python3
"""Live Kalshi ↔ Polymarket cross-platform arbitrage detector.

Polls public market-data APIs, fuzzy-matches equivalent contracts, and reports
quote combinations where buying complementary outcomes would cost less than the
$1 payout (after optional fee estimates).

This script does not place orders.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from difflib import SequenceMatcher
from typing import Any, Iterable, Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

KALSHI_API = "https://external-api.kalshi.com/trade-api/v2"
POLY_GAMMA = "https://gamma-api.polymarket.com"
POLY_CLOB = "https://clob.polymarket.com"

USER_AGENT = "prediction-arb-scanner/1.0 (+educational; public market data)"
STOPWORDS = {
    "a",
    "an",
    "and",
    "at",
    "be",
    "before",
    "by",
    "for",
    "in",
    "of",
    "on",
    "or",
    "the",
    "to",
    "vs",
    "versus",
    "will",
    "with",
    "after",
}

logger = logging.getLogger("arb_scanner")


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


def _session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=4,
        backoff_factor=0.6,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET", "POST"),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=16, pool_maxsize=16)
    session.mount("https://", adapter)
    session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})
    return session


SESSION = _session()


def _get_json(url: str, params: Optional[dict] = None, timeout: float = 25.0) -> Any:
    response = SESSION.get(url, params=params, timeout=timeout)
    if response.status_code == 429:
        retry_after = float(response.headers.get("Retry-After", "2"))
        time.sleep(min(retry_after, 8.0))
        response = SESSION.get(url, params=params, timeout=timeout)
    response.raise_for_status()
    if not response.content:
        return {}
    return response.json()


def _post_json(url: str, payload: Any, timeout: float = 25.0) -> Any:
    response = SESSION.post(url, json=payload, timeout=timeout)
    if response.status_code == 429:
        time.sleep(2.0)
        response = SESSION.post(url, json=payload, timeout=timeout)
    response.raise_for_status()
    if not response.content:
        return {}
    return response.json()


def _parse_maybe_json(value: Any) -> Any:
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("[") or text.startswith("{"):
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return value
    return value


def _as_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(number) or math.isinf(number):
        return None
    return number


def _chunked(items: list, size: int) -> Iterable[list]:
    for index in range(0, len(items), size):
        yield items[index : index + size]


# ---------------------------------------------------------------------------
# Quotes and matching models
# ---------------------------------------------------------------------------


@dataclass
class SideQuote:
    bid: Optional[float] = None
    ask: Optional[float] = None
    bid_size: Optional[float] = None
    ask_size: Optional[float] = None

    def tradable_ask(self) -> bool:
        return self.ask is not None and 0.0 < self.ask < 1.0

    def tradable_bid(self) -> bool:
        return self.bid is not None and 0.0 < self.bid < 1.0


@dataclass
class VenueContract:
    venue: str
    title: str
    event_title: str
    outcome: str
    market_id: str
    url: str
    complement_id: str = ""
    yes: SideQuote = field(default_factory=SideQuote)
    no: SideQuote = field(default_factory=SideQuote)
    volume_24h: float = 0.0
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class MatchedPair:
    label: str
    score: float
    kalshi: VenueContract
    poly: VenueContract
    match_notes: str = ""


@dataclass
class Opportunity:
    kind: str
    pair: MatchedPair
    gross_edge: float
    net_edge: float
    cost: float
    fees: float
    size: Optional[float]
    buy_yes_venue: str
    buy_no_venue: str
    yes_ask: float
    no_ask: float
    detail: str


# ---------------------------------------------------------------------------
# Text matching
# ---------------------------------------------------------------------------


_NON_ALNUM = re.compile(r"[^a-z0-9\s]+")
_WS = re.compile(r"\s+")


def normalize_text(text: str) -> str:
    lowered = (text or "").lower().replace("&", " and ")
    lowered = _NON_ALNUM.sub(" ", lowered)
    return _WS.sub(" ", lowered).strip()


def significant_tokens(text: str) -> set[str]:
    return {tok for tok in normalize_text(text).split() if tok not in STOPWORDS and len(tok) > 1}


def similarity(left: str, right: str) -> float:
    a = normalize_text(left)
    b = normalize_text(right)
    if not a or not b:
        return 0.0
    seq = SequenceMatcher(None, a, b).ratio()
    ta, tb = significant_tokens(left), significant_tokens(right)
    if not ta or not tb:
        jaccard = 0.0
    else:
        jaccard = len(ta & tb) / len(ta | tb)
    # Light bonus when the shorter token set is almost contained in the longer.
    containment = 0.0
    if ta and tb:
        smaller, larger = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
        containment = len(smaller & larger) / len(smaller)
    return 0.50 * seq + 0.30 * jaccard + 0.20 * containment


def years_in(text: str) -> set[str]:
    return set(re.findall(r"\b(20\d{2})\b", text or ""))


def years_conflict(left: str, right: str) -> bool:
    ya, yb = years_in(left), years_in(right)
    return bool(ya and yb and ya.isdisjoint(yb))


# ---------------------------------------------------------------------------
# Fees (estimates; series/category schedules change)
# ---------------------------------------------------------------------------


def kalshi_taker_fee_per_contract(price: float, multiplier: float = 1.0) -> float:
    """Kalshi quadratic taker fee: 0.07 * M * P * (1-P), rounded up to 1¢ per fill.

    Per-contract estimate uses the unrounded term so a 1-contract round-up
    penalty is not baked into every size. Callers can treat this as a floor.
    """
    if price <= 0.0 or price >= 1.0:
        return 0.0
    return multiplier * 0.07 * price * (1.0 - price)


def poly_taker_fee_per_contract(price: float, fee_rate: float = 0.05) -> float:
    """Polymarket taker fee: C * feeRate * p * (1-p). Makers are not charged."""
    if price <= 0.0 or price >= 1.0:
        return 0.0
    return fee_rate * price * (1.0 - price)


# ---------------------------------------------------------------------------
# Kalshi catalog + quotes
# ---------------------------------------------------------------------------


def fetch_kalshi_events(
    max_events: int,
    include_sports: bool,
    page_limit: int = 200,
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    cursor: Optional[str] = None
    while len(events) < max_events:
        params: dict[str, Any] = {
            "limit": min(page_limit, max_events - len(events)),
            "status": "open",
            "with_nested_markets": "true",
        }
        if cursor:
            params["cursor"] = cursor
        payload = _get_json(f"{KALSHI_API}/events", params=params)
        batch = payload.get("events") or []
        for event in batch:
            ticker = (event.get("event_ticker") or "") + (event.get("series_ticker") or "")
            if "MVE" in ticker.upper():
                continue
            category = (event.get("category") or "").strip()
            if not include_sports and category.lower() == "sports":
                continue
            events.append(event)
            if len(events) >= max_events:
                break
        cursor = payload.get("cursor") or None
        if not batch or not cursor:
            break
        time.sleep(0.05)
    logger.info("Loaded %s Kalshi events", len(events))
    return events


def _kalshi_volume(market: dict[str, Any]) -> float:
    return _as_float(market.get("volume_24h_fp")) or _as_float(market.get("volume_fp")) or 0.0


def kalshi_contracts_from_events(events: list[dict[str, Any]]) -> list[VenueContract]:
    contracts: list[VenueContract] = []
    for event in events:
        event_title = event.get("title") or ""
        event_ticker = event.get("event_ticker") or ""
        for market in event.get("markets") or []:
            if (market.get("market_type") or "binary") not in ("binary", "", None):
                continue
            ticker = market.get("ticker") or ""
            if not ticker:
                continue
            outcome = (market.get("yes_sub_title") or "").strip() or "Yes"
            title = market.get("title") or event_title
            yes_bid = _as_float(market.get("yes_bid_dollars"))
            yes_ask = _as_float(market.get("yes_ask_dollars"))
            no_bid = _as_float(market.get("no_bid_dollars"))
            no_ask = _as_float(market.get("no_ask_dollars"))
            yes_bid_size = _as_float(market.get("yes_bid_size_fp"))
            yes_ask_size = _as_float(market.get("yes_ask_size_fp"))
            contracts.append(
                VenueContract(
                    venue="kalshi",
                    title=title,
                    event_title=event_title,
                    outcome=outcome,
                    market_id=ticker,
                    complement_id=ticker,
                    url=f"https://kalshi.com/markets/{event.get('series_ticker', '')}/{event_ticker}".rstrip("/"),
                    yes=SideQuote(bid=yes_bid, ask=yes_ask, bid_size=yes_bid_size, ask_size=yes_ask_size),
                    # Buying NO lifts implied NO asks (YES bids). Ask size ≈ YES bid size.
                    no=SideQuote(bid=no_bid, ask=no_ask, bid_size=yes_ask_size, ask_size=yes_bid_size),
                    volume_24h=_kalshi_volume(market),
                    extra={"event_ticker": event_ticker, "category": event.get("category")},
                )
            )
    return contracts


def refresh_kalshi_quotes(contracts: list[VenueContract]) -> None:
    tickers = list(dict.fromkeys(c.market_id for c in contracts))
    by_ticker: dict[str, dict[str, Any]] = {}
    for chunk in _chunked(tickers, 40):
        try:
            payload = _get_json(
                f"{KALSHI_API}/markets",
                params={"tickers": ",".join(chunk), "limit": len(chunk)},
            )
        except requests.RequestException as exc:
            logger.warning("Kalshi quote refresh failed: %s", exc)
            continue
        for market in payload.get("markets") or []:
            ticker = market.get("ticker")
            if ticker:
                by_ticker[ticker] = market
        missing = [t for t in chunk if t not in by_ticker]
        for ticker in missing:
            try:
                payload = _get_json(f"{KALSHI_API}/markets/{ticker}")
                market = payload.get("market") or payload
                if market.get("ticker"):
                    by_ticker[market["ticker"]] = market
            except requests.RequestException:
                continue
    for contract in contracts:
        market = by_ticker.get(contract.market_id)
        if not market:
            continue
        contract.yes.bid = _as_float(market.get("yes_bid_dollars"))
        contract.yes.ask = _as_float(market.get("yes_ask_dollars"))
        contract.no.bid = _as_float(market.get("no_bid_dollars"))
        contract.no.ask = _as_float(market.get("no_ask_dollars"))
        contract.yes.bid_size = _as_float(market.get("yes_bid_size_fp"))
        contract.yes.ask_size = _as_float(market.get("yes_ask_size_fp"))
        contract.no.bid_size = contract.yes.ask_size
        contract.no.ask_size = contract.yes.bid_size
        contract.volume_24h = _kalshi_volume(market) or contract.volume_24h


# ---------------------------------------------------------------------------
# Polymarket catalog + quotes
# ---------------------------------------------------------------------------


def _poly_outcomes_and_tokens(market: dict[str, Any]) -> tuple[list[str], list[str]]:
    outcomes = _parse_maybe_json(market.get("outcomes")) or []
    tokens = _parse_maybe_json(market.get("clobTokenIds") or market.get("clob_token_ids")) or []
    if not isinstance(outcomes, list):
        outcomes = []
    if not isinstance(tokens, list):
        tokens = []
    outcomes = [str(o) for o in outcomes]
    tokens = [str(t) for t in tokens]
    return outcomes, tokens


def _poly_event_title(market: dict[str, Any]) -> str:
    events = market.get("events") or []
    if events and isinstance(events, list) and isinstance(events[0], dict):
        return events[0].get("title") or market.get("question") or ""
    return market.get("question") or market.get("title") or ""


def _poly_slug(market: dict[str, Any]) -> str:
    events = market.get("events") or []
    if events and isinstance(events, list) and isinstance(events[0], dict):
        return events[0].get("slug") or market.get("slug") or ""
    return market.get("slug") or ""


def poly_contracts_from_markets(markets: list[dict[str, Any]]) -> list[VenueContract]:
    contracts: list[VenueContract] = []
    for market in markets:
        if market.get("closed") is True or market.get("active") is False:
            continue
        outcomes, tokens = _poly_outcomes_and_tokens(market)
        if len(outcomes) < 2 or len(tokens) < 2:
            continue
        question = market.get("question") or market.get("title") or ""
        event_title = _poly_event_title(market)
        slug = _poly_slug(market)
        url = f"https://polymarket.com/event/{slug}" if slug else "https://polymarket.com"
        volume = _as_float(market.get("volume24hr")) or _as_float(market.get("volumeNum")) or 0.0
        group_title = (market.get("groupItemTitle") or "").strip()

        lowered = [o.lower() for o in outcomes]
        if lowered[:2] == ["yes", "no"] or (len(outcomes) == 2 and "yes" in lowered and "no" in lowered):
            yes_idx = lowered.index("yes")
            no_idx = lowered.index("no")
            outcome_label = group_title or "Yes"
            contracts.append(
                VenueContract(
                    venue="polymarket",
                    title=question,
                    event_title=event_title,
                    outcome=outcome_label,
                    market_id=tokens[yes_idx],
                    complement_id=tokens[no_idx],
                    url=url,
                    extra={"yes_token": tokens[yes_idx], "no_token": tokens[no_idx], "condition_id": market.get("conditionId")},
                    volume_24h=volume,
                )
            )
            continue

        # Named multi-outcome market: each outcome vs the rest is not a clean
        # binary unless there are exactly two exhaustive outcomes.
        if len(outcomes) == 2 and len(tokens) >= 2:
            for index, name in enumerate(outcomes):
                other = 1 - index
                contracts.append(
                    VenueContract(
                        venue="polymarket",
                        title=question,
                        event_title=event_title,
                        outcome=name,
                        market_id=tokens[index],
                        complement_id=tokens[other],
                        url=url,
                        extra={
                            "yes_token": tokens[index],
                            "no_token": tokens[other],
                            "condition_id": market.get("conditionId"),
                            "named_binary": True,
                        },
                        volume_24h=volume,
                    )
                )
    return contracts


def fetch_poly_markets(max_markets: int, page_limit: int = 100) -> list[dict[str, Any]]:
    markets: list[dict[str, Any]] = []
    offset = 0
    while len(markets) < max_markets:
        limit = min(page_limit, max_markets - len(markets))
        try:
            batch = _get_json(
                f"{POLY_GAMMA}/markets",
                params={
                    "closed": "false",
                    "limit": limit,
                    "offset": offset,
                    "order": "volume24hr",
                    "ascending": "false",
                },
            )
        except requests.RequestException as exc:
            logger.warning("Polymarket catalog page failed: %s", exc)
            break
        if not isinstance(batch, list) or not batch:
            break
        markets.extend(batch)
        offset += len(batch)
        if len(batch) < limit:
            break
        time.sleep(0.05)
    logger.info("Loaded %s Polymarket markets", len(markets))
    return markets


def search_poly_events(query: str, limit_per_type: int = 8) -> list[dict[str, Any]]:
    try:
        payload = _get_json(
            f"{POLY_GAMMA}/public-search",
            params={"q": query, "events_status": "active", "limit_per_type": limit_per_type},
        )
    except requests.RequestException as exc:
        logger.debug("Polymarket search failed for %r: %s", query, exc)
        return []
    events = payload.get("events") or []
    markets: list[dict[str, Any]] = []
    for event in events:
        for market in event.get("markets") or []:
            market = dict(market)
            market.setdefault("events", [event])
            markets.append(market)
    return markets


def _best_book_levels(book: dict[str, Any]) -> SideQuote:
    bids = book.get("bids") or []
    asks = book.get("asks") or []

    def _px(level: dict[str, Any]) -> Optional[float]:
        return _as_float(level.get("price"))

    def _sz(level: dict[str, Any]) -> Optional[float]:
        return _as_float(level.get("size"))

    best_bid_level = max((lvl for lvl in bids if _px(lvl) is not None), key=_px, default=None)
    best_ask_level = min((lvl for lvl in asks if _px(lvl) is not None), key=_px, default=None)
    return SideQuote(
        bid=_px(best_bid_level) if best_bid_level else None,
        ask=_px(best_ask_level) if best_ask_level else None,
        bid_size=_sz(best_bid_level) if best_bid_level else None,
        ask_size=_sz(best_ask_level) if best_ask_level else None,
    )


def refresh_poly_quotes(contracts: list[VenueContract]) -> None:
    tokens: list[str] = []
    for contract in contracts:
        yes_token = contract.extra.get("yes_token") or contract.market_id
        no_token = contract.extra.get("no_token") or contract.complement_id
        if yes_token:
            tokens.append(str(yes_token))
        if no_token:
            tokens.append(str(no_token))
    tokens = list(dict.fromkeys(tokens))
    books: dict[str, dict[str, Any]] = {}
    for chunk in _chunked(tokens, 40):
        try:
            payload = _post_json(f"{POLY_CLOB}/books", [{"token_id": token} for token in chunk])
        except requests.RequestException as exc:
            logger.warning("Polymarket book refresh failed: %s", exc)
            continue
        if isinstance(payload, list):
            for book in payload:
                asset = book.get("asset_id") or book.get("token_id")
                if asset:
                    books[str(asset)] = book
    for contract in contracts:
        yes_token = str(contract.extra.get("yes_token") or contract.market_id)
        no_token = str(contract.extra.get("no_token") or contract.complement_id)
        yes_book = books.get(yes_token)
        no_book = books.get(no_token)
        if yes_book:
            contract.yes = _best_book_levels(yes_book)
        if no_book:
            contract.no = _best_book_levels(no_book)


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------


def _candidate_score(kalshi: VenueContract, poly: VenueContract) -> float:
    title_score = max(
        similarity(kalshi.title, poly.title),
        similarity(kalshi.event_title, poly.event_title),
        similarity(kalshi.title, poly.event_title),
        similarity(kalshi.event_title, poly.title),
    )
    outcome_score = 0.0
    kalshi_out = kalshi.outcome.strip().lower()
    poly_out = poly.outcome.strip().lower()
    if kalshi_out not in {"", "yes", "no"} and poly_out not in {"", "yes", "no"}:
        outcome_score = similarity(kalshi.outcome, poly.outcome)
        if kalshi_out == poly_out:
            outcome_score = max(outcome_score, 1.0)
    elif kalshi_out in {"", "yes"} and poly_out in {"", "yes"}:
        outcome_score = 0.85
    if kalshi_out not in {"", "yes", "no"} and poly_out not in {"", "yes", "no"}:
        if outcome_score < 0.72:
            return 0.0
        return 0.65 * title_score + 0.35 * outcome_score
    return title_score


def _contract_tokens(contract: VenueContract) -> set[str]:
    return significant_tokens(f"{contract.event_title} {contract.title} {contract.outcome}")


def match_contracts(
    kalshi_contracts: list[VenueContract],
    poly_contracts: list[VenueContract],
    min_score: float,
) -> list[MatchedPair]:
    pairs: list[MatchedPair] = []
    used_kalshi: set[str] = set()
    used_poly: set[str] = set()

    inverted: dict[str, list[int]] = {}
    poly_token_sets: list[set[str]] = []
    for index, poly in enumerate(poly_contracts):
        tokens = _contract_tokens(poly)
        poly_token_sets.append(tokens)
        for token in tokens:
            inverted.setdefault(token, []).append(index)

    scored: list[tuple[float, VenueContract, VenueContract]] = []
    for kalshi in kalshi_contracts:
        k_text = f"{kalshi.event_title} {kalshi.title} {kalshi.outcome}"
        k_tokens = _contract_tokens(kalshi)
        candidate_ids: set[int] = set()
        for token in k_tokens:
            candidate_ids.update(inverted.get(token, ()))
        for poly_index in candidate_ids:
            poly = poly_contracts[poly_index]
            p_text = f"{poly.event_title} {poly.title} {poly.outcome}"
            if years_conflict(k_text, p_text):
                continue
            shared = k_tokens & poly_token_sets[poly_index]
            if len(shared) < 2:
                continue
            score = _candidate_score(kalshi, poly)
            if score < min_score:
                continue
            scored.append((score, kalshi, poly))

    scored.sort(key=lambda row: row[0], reverse=True)
    for score, kalshi, poly in scored:
        k_key = f"{kalshi.market_id}|{kalshi.outcome.lower()}"
        p_key = f"{poly.market_id}|{poly.outcome.lower()}"
        if k_key in used_kalshi or p_key in used_poly:
            continue
        used_kalshi.add(k_key)
        used_poly.add(p_key)
        label = kalshi.title
        if kalshi.outcome and kalshi.outcome.lower() not in {"yes", "no"}:
            label = f"{kalshi.event_title} — {kalshi.outcome}"
        pairs.append(
            MatchedPair(
                label=label,
                score=score,
                kalshi=kalshi,
                poly=poly,
                match_notes=f"kalshi:{kalshi.market_id} poly:{poly.outcome}",
            )
        )
    return pairs


def enrich_with_search(
    kalshi_contracts: list[VenueContract],
    poly_contracts: list[VenueContract],
    min_score: float,
    search_limit: int = 40,
) -> list[VenueContract]:
    """Search Polymarket for high-volume Kalshi titles that have no match yet."""
    already = {pair.kalshi.market_id for pair in match_contracts(kalshi_contracts, poly_contracts, min_score)}
    unmatched = [c for c in kalshi_contracts if c.market_id not in already]
    unmatched.sort(key=lambda c: c.volume_24h, reverse=True)
    extra_markets: list[dict[str, Any]] = []
    seen_queries: set[str] = set()
    for contract in unmatched[:search_limit]:
        query = contract.event_title or contract.title
        query = re.sub(r"\s+", " ", query).strip()
        if len(query) < 8 or query.lower() in seen_queries:
            continue
        seen_queries.add(query.lower())
        extra_markets.extend(search_poly_events(query, limit_per_type=5))
        time.sleep(0.08)
    extra_contracts = poly_contracts_from_markets(extra_markets)
    known_ids = {c.market_id for c in poly_contracts}
    merged = list(poly_contracts)
    for contract in extra_contracts:
        if contract.market_id not in known_ids:
            merged.append(contract)
            known_ids.add(contract.market_id)
    logger.info("Polymarket search added %s extra contracts", len(merged) - len(poly_contracts))
    return merged


def pairs_from_mapping_file(path: str) -> list[MatchedPair]:
    with open(path, encoding="utf-8") as handle:
        raw = json.load(handle)
    pairs: list[MatchedPair] = []
    for item in raw:
        kalshi = VenueContract(
            venue="kalshi",
            title=item.get("label") or item.get("kalshi_ticker"),
            event_title=item.get("label") or "",
            outcome=item.get("kalshi_outcome") or "Yes",
            market_id=item["kalshi_ticker"],
            complement_id=item["kalshi_ticker"],
            url=item.get("kalshi_url") or f"https://kalshi.com/markets/{item['kalshi_ticker']}",
        )
        poly = VenueContract(
            venue="polymarket",
            title=item.get("label") or "Polymarket",
            event_title=item.get("label") or "",
            outcome=item.get("poly_outcome") or "Yes",
            market_id=item["poly_yes_token"],
            complement_id=item["poly_no_token"],
            url=item.get("poly_url") or "https://polymarket.com",
            extra={"yes_token": item["poly_yes_token"], "no_token": item["poly_no_token"]},
        )
        pairs.append(MatchedPair(label=item.get("label") or kalshi.market_id, score=1.0, kalshi=kalshi, poly=poly, match_notes="manual mapping"))
    return pairs


# ---------------------------------------------------------------------------
# Opportunity detection
# ---------------------------------------------------------------------------


def _min_size(*sizes: Optional[float]) -> Optional[float]:
    present = [s for s in sizes if s is not None and s > 0]
    if not present:
        return None
    return min(present)


def detect_opportunities(
    pairs: list[MatchedPair],
    min_edge: float,
    include_fees: bool,
    kalshi_fee_mult: float,
    poly_fee_rate: float,
    include_same_side: bool = False,
) -> list[Opportunity]:
    found: list[Opportunity] = []
    for pair in pairs:
        found.extend(
            _complementary(
                pair,
                buy_yes=pair.kalshi,
                buy_no=pair.poly,
                yes_venue="kalshi",
                no_venue="polymarket",
                min_edge=min_edge,
                include_fees=include_fees,
                kalshi_fee_mult=kalshi_fee_mult,
                poly_fee_rate=poly_fee_rate,
            )
        )
        found.extend(
            _complementary(
                pair,
                buy_yes=pair.poly,
                buy_no=pair.kalshi,
                yes_venue="polymarket",
                no_venue="kalshi",
                min_edge=min_edge,
                include_fees=include_fees,
                kalshi_fee_mult=kalshi_fee_mult,
                poly_fee_rate=poly_fee_rate,
            )
        )
        if include_same_side:
            found.extend(
                _same_side(
                    pair,
                    min_edge=min_edge,
                    include_fees=include_fees,
                    kalshi_fee_mult=kalshi_fee_mult,
                    poly_fee_rate=poly_fee_rate,
                )
            )
    found.sort(key=lambda opp: opp.net_edge, reverse=True)
    return found


def _leg_fee(venue: str, price: float, include_fees: bool, kalshi_fee_mult: float, poly_fee_rate: float) -> float:
    if not include_fees:
        return 0.0
    if venue == "kalshi":
        return kalshi_taker_fee_per_contract(price, kalshi_fee_mult)
    return poly_taker_fee_per_contract(price, poly_fee_rate)


def _complementary(
    pair: MatchedPair,
    buy_yes: VenueContract,
    buy_no: VenueContract,
    yes_venue: str,
    no_venue: str,
    min_edge: float,
    include_fees: bool,
    kalshi_fee_mult: float,
    poly_fee_rate: float,
) -> list[Opportunity]:
    if not buy_yes.yes.tradable_ask() or not buy_no.no.tradable_ask():
        return []
    yes_ask = buy_yes.yes.ask
    no_ask = buy_no.no.ask
    assert yes_ask is not None and no_ask is not None
    cost = yes_ask + no_ask
    fees = _leg_fee(yes_venue, yes_ask, include_fees, kalshi_fee_mult, poly_fee_rate) + _leg_fee(
        no_venue, no_ask, include_fees, kalshi_fee_mult, poly_fee_rate
    )
    gross = 1.0 - cost
    net = 1.0 - cost - fees
    if net < min_edge:
        return []
    size = _min_size(buy_yes.yes.ask_size, buy_no.no.ask_size)
    detail = (
        f"Buy {yes_venue} YES '{buy_yes.outcome}' @ ${yes_ask:.3f} + "
        f"buy {no_venue} NO/complement '{buy_no.outcome}' @ ${no_ask:.3f} "
        f"= ${cost:.3f} vs $1.00 payout"
    )
    return [
        Opportunity(
            kind="complementary",
            pair=pair,
            gross_edge=gross,
            net_edge=net,
            cost=cost,
            fees=fees,
            size=size,
            buy_yes_venue=yes_venue,
            buy_no_venue=no_venue,
            yes_ask=yes_ask,
            no_ask=no_ask,
            detail=detail,
        )
    ]


def _same_side(
    pair: MatchedPair,
    min_edge: float,
    include_fees: bool,
    kalshi_fee_mult: float,
    poly_fee_rate: float,
) -> list[Opportunity]:
    """Buy YES on the cheaper venue, sell YES on the richer bid. Not a locked $1 payout."""
    found: list[Opportunity] = []
    for buy, sell, buy_venue, sell_venue in (
        (pair.kalshi, pair.poly, "kalshi", "polymarket"),
        (pair.poly, pair.kalshi, "polymarket", "kalshi"),
    ):
        if not buy.yes.tradable_ask() or not sell.yes.tradable_bid():
            continue
        ask, bid = buy.yes.ask, sell.yes.bid
        assert ask is not None and bid is not None
        fees = _leg_fee(buy_venue, ask, include_fees, kalshi_fee_mult, poly_fee_rate)
        # Selling on Polymarket as a taker hits the bid; Kalshi sell YES ≈ lift NO asks / hit YES bids (maker or taker).
        # Treat the sell as a taker hit of the bid with a conservative same-venue taker fee.
        fees += _leg_fee(sell_venue, bid, include_fees, kalshi_fee_mult, poly_fee_rate)
        gross = bid - ask
        net = gross - fees
        if net < min_edge:
            continue
        size = _min_size(buy.yes.ask_size, sell.yes.bid_size)
        found.append(
            Opportunity(
                kind="same_side",
                pair=pair,
                gross_edge=gross,
                net_edge=net,
                cost=ask,
                fees=fees,
                size=size,
                buy_yes_venue=buy_venue,
                buy_no_venue=sell_venue,
                yes_ask=ask,
                no_ask=bid,
                detail=(
                    f"Buy YES on {buy_venue} @ ${ask:.3f} and sell YES on {sell_venue} @ ${bid:.3f}. "
                    "These are different instruments — not a guaranteed $1 lock."
                ),
            )
        )
    return found


# ---------------------------------------------------------------------------
# Display
# ---------------------------------------------------------------------------


def _cents(value: float) -> str:
    return f"{value * 100:+.2f}¢"


def format_opportunity(opp: Opportunity, index: int) -> str:
    pair = opp.pair
    size = f"{opp.size:.2f}" if opp.size is not None else "n/a"
    kind = "COMPLEMENT (YES+NO lock)" if opp.kind == "complementary" else "SAME-SIDE (not a locked payout)"
    lines = [
        f"{index:>2}. [{kind}] net {_cents(opp.net_edge)}/contract  gross {_cents(opp.gross_edge)}  fees {_cents(opp.fees)}",
        f"    {pair.label}",
        f"    Kalshi event: {pair.kalshi.event_title or pair.kalshi.title}",
        f"    Poly event  : {pair.poly.event_title or pair.poly.title}",
        f"    match {pair.score:.2f}  top-of-book size ≈ {size}  cost ${opp.cost:.3f}"
        + (
            "  ⚠ wording differs — verify resolution rules"
            if similarity(pair.kalshi.event_title or pair.kalshi.title, pair.poly.event_title or pair.poly.title) < 0.78
            else ""
        ),
        f"    {opp.detail}",
        f"    Kalshi : {pair.kalshi.url}  ({pair.kalshi.market_id})",
        f"    Poly   : {pair.poly.url}",
        (
            f"    quotes Kalshi YES bid/ask "
            f"{_fmt_px(pair.kalshi.yes.bid)}/{_fmt_px(pair.kalshi.yes.ask)}  "
            f"NO {_fmt_px(pair.kalshi.no.bid)}/{_fmt_px(pair.kalshi.no.ask)} | "
            f"Poly YES {_fmt_px(pair.poly.yes.bid)}/{_fmt_px(pair.poly.yes.ask)}  "
            f"NO {_fmt_px(pair.poly.no.bid)}/{_fmt_px(pair.poly.no.ask)}"
        ),
    ]
    return "\n".join(lines)


def _fmt_px(value: Optional[float]) -> str:
    return f"${value:.3f}" if value is not None else "—"


def opportunity_to_dict(opp: Opportunity) -> dict[str, Any]:
    return {
        "kind": opp.kind,
        "label": opp.pair.label,
        "kalshi_event": opp.pair.kalshi.event_title,
        "poly_event": opp.pair.poly.event_title,
        "match_score": round(opp.pair.score, 4),
        "net_edge": round(opp.net_edge, 6),
        "gross_edge": round(opp.gross_edge, 6),
        "fees": round(opp.fees, 6),
        "cost": round(opp.cost, 6),
        "size": opp.size,
        "detail": opp.detail,
        "kalshi_ticker": opp.pair.kalshi.market_id,
        "kalshi_url": opp.pair.kalshi.url,
        "poly_yes_token": opp.pair.poly.market_id,
        "poly_url": opp.pair.poly.url,
        "yes_ask": opp.yes_ask,
        "no_ask": opp.no_ask,
        "buy_yes_venue": opp.buy_yes_venue,
        "buy_no_venue": opp.buy_no_venue,
    }


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


def build_pairs(args: argparse.Namespace) -> list[MatchedPair]:
    pairs: list[MatchedPair] = []
    if args.pairs:
        pairs.extend(pairs_from_mapping_file(args.pairs))
        logger.info("Loaded %s manual mappings", len(pairs))
        if args.manual_only:
            return pairs

    kalshi_events = fetch_kalshi_events(args.kalshi_events, include_sports=args.include_sports)
    kalshi_contracts = kalshi_contracts_from_events(kalshi_events)
    poly_markets = fetch_poly_markets(args.poly_markets)
    poly_contracts = poly_contracts_from_markets(poly_markets)
    if args.search_unmatched:
        poly_contracts = enrich_with_search(
            kalshi_contracts,
            poly_contracts,
            min_score=args.min_match,
            search_limit=args.search_unmatched,
        )
    auto = match_contracts(kalshi_contracts, poly_contracts, min_score=args.min_match)
    # Keep manual pairs first; drop auto-pairs that collide on Kalshi ticker.
    manual_tickers = {p.kalshi.market_id for p in pairs}
    for pair in auto:
        if pair.kalshi.market_id not in manual_tickers:
            pairs.append(pair)
    logger.info("Matched %s cross-venue pairs", len(pairs))
    return pairs


def refresh_all_quotes(pairs: list[MatchedPair]) -> None:
    kalshi = [p.kalshi for p in pairs]
    poly = [p.poly for p in pairs]
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(refresh_kalshi_quotes, kalshi), pool.submit(refresh_poly_quotes, poly)]
        for future in as_completed(futures):
            future.result()


def run_scan(args: argparse.Namespace) -> None:
    print("Building Kalshi ↔ Polymarket catalog (public APIs, no keys)…", file=sys.stderr)
    pairs = build_pairs(args)
    if not pairs:
        print("No matched markets. Try --include-sports, a lower --min-match, or --pairs mappings.json.", file=sys.stderr)
        return

    poll = 0
    while True:
        poll += 1
        started = time.time()
        try:
            refresh_all_quotes(pairs)
            opps = detect_opportunities(
                pairs,
                min_edge=args.min_edge,
                include_fees=not args.no_fees,
                kalshi_fee_mult=args.kalshi_fee_mult,
                poly_fee_rate=args.poly_fee_rate,
                include_same_side=args.same_side,
            )
        except requests.RequestException as exc:
            logger.error("Quote refresh failed: %s", exc)
            if args.once:
                raise
            time.sleep(args.interval)
            continue

        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        header = (
            f"\n{'=' * 78}\n"
            f"SCAN {now}  poll={poll}  matched={len(pairs)}  hits={len(opps)}  "
            f"min_edge={args.min_edge:.3f}\n"
            f"{'-' * 78}"
        )
        print(header, file=sys.stderr if args.json else sys.stdout)
        if args.json:
            for opp in opps:
                print(json.dumps(opportunity_to_dict(opp)))
            if not opps:
                print(json.dumps({"hits": 0, "matched": len(pairs), "ts": now}))
        else:
            if not opps:
                print("No live edge above threshold. Quotes still moving; scanning is not a fill.")
                # Show a few tightest complementary books so the run is inspectable.
                preview = _tightest_complements(pairs, limit=5)
                if preview:
                    print("\nClosest complementary books (not necessarily profitable):")
                    for line in preview:
                        print(f"  {line}")
            else:
                for index, opp in enumerate(opps, start=1):
                    print(format_opportunity(opp, index))
                    print()

        if args.once:
            return
        elapsed = time.time() - started
        time.sleep(max(0.0, args.interval - elapsed))


def _tightest_complements(pairs: list[MatchedPair], limit: int) -> list[str]:
    rows: list[tuple[float, str]] = []
    for pair in pairs:
        for yes_c, no_c, yes_v, no_v in (
            (pair.kalshi, pair.poly, "K", "P"),
            (pair.poly, pair.kalshi, "P", "K"),
        ):
            if yes_c.yes.tradable_ask() and no_c.no.tradable_ask():
                cost = yes_c.yes.ask + no_c.no.ask  # type: ignore[operator]
                rows.append(
                    (
                        cost,
                        f"${cost:.3f}  buy {yes_v} YES @{yes_c.yes.ask:.3f} + {no_v} NO @{no_c.no.ask:.3f}  | {pair.label[:70]}",
                    )
                )
    rows.sort(key=lambda item: item[0])
    return [line for _, line in rows[:limit]]


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Detect live Kalshi vs Polymarket complementary-quote arbitrage (read-only)."
    )
    parser.add_argument("--interval", type=float, default=20.0, help="Seconds between quote polls (default 20)")
    parser.add_argument("--once", action="store_true", help="Run a single scan and exit")
    parser.add_argument("--min-edge", type=float, default=0.005, help="Minimum net edge per contract in dollars")
    parser.add_argument("--min-match", type=float, default=0.64, help="Fuzzy title match threshold 0-1")
    parser.add_argument("--kalshi-events", type=int, default=800, help="Max Kalshi events to download")
    parser.add_argument("--poly-markets", type=int, default=400, help="Max Polymarket markets by 24h volume")
    parser.add_argument("--include-sports", action="store_true", help="Include Kalshi Sports (noisier matching)")
    parser.add_argument("--search-unmatched", type=int, default=15, help="Polymarket keyword searches for unmatched Kalshi titles")
    parser.add_argument(
        "--same-side",
        action="store_true",
        help="Also report buy-YES-here / sell-YES-there dislocations (not a locked $1 payout)",
    )
    parser.add_argument("--pairs", help="JSON file of manual ticker/token mappings")
    parser.add_argument("--manual-only", action="store_true", help="Use only --pairs mappings")
    parser.add_argument("--json", action="store_true", help="Emit JSON lines for hits")
    parser.add_argument("--no-fees", action="store_true", help="Ignore estimated taker fees")
    parser.add_argument("--kalshi-fee-mult", type=float, default=1.0, help="Kalshi series fee multiplier M")
    parser.add_argument("--poly-fee-rate", type=float, default=0.05, help="Polymarket taker feeRate in fee=C*rate*p*(1-p)")
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )
    try:
        run_scan(args)
    except KeyboardInterrupt:
        print("\nStopped.", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
