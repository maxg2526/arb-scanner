"""
Kalshi <-> Polymarket US <-> Novig arbitrage scanner.

Each run:
  1. Pulls open markets from Kalshi and Polymarket US (public data, no login needed).
  2. Matches markets that look like the same event (fuzzy title match + close dates).
  3. For each match, prices both hedged combos:
        A) Kalshi YES + Polymarket NO
        B) Kalshi NO  + Polymarket YES
     If the combo costs less than $1 after taker fees, it's a locked-in profit
     (assuming both markets resolve the same way -- ALWAYS check the rules).
  4. Sends new opportunities to your phone via ntfy.

Settings come from environment variables (set in the GitHub workflow):
  NTFY_TOPIC        your private ntfy topic name (required to get alerts)
  MIN_EDGE          minimum net profit per $1 contract to alert (default 0.01 = 1 cent)
  MAX_EDGE          edges above this are almost always mismatched markets; skipped (default 0.08)
  MIN_MATCH_SCORE   0-100 title similarity needed to call two markets "the same" (default 85)
  MAX_DAYS_APART    max gap between the two markets' close dates (default 3)
  MIN_SIZE          minimum contracts available at the quoted price on each side (default 10)
  STATE_FILE        where already-alerted opportunities are remembered (default state/alerted.json)
  TEST_ALERT=1      just send a test notification and exit
  DRY_RUN=1         print alerts instead of sending them
  NOVIG_KEY_ID      Novig read key id (optional -- Novig is skipped without it)
  NOVIG_PRIVATE_KEY Novig read key, PEM text (optional)
"""

from __future__ import annotations

import json
import math
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import requests
from rapidfuzz import fuzz, process

KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"
POLY_BASE = "https://gateway.polymarket.us"
NOVIG_BASE = "https://api.novig.com"
NOVIG_KEY_ID = os.getenv("NOVIG_KEY_ID", "").strip()
NOVIG_PRIVATE_KEY = os.getenv("NOVIG_PRIVATE_KEY", "").strip()

# Taker fee coefficients: fee per contract ~= COEF * p * (1 - p)
KALSHI_FEE_COEF = 0.07     # Kalshi general taker fee (rounded UP to the cent per order)
POLY_FEE_COEF = 0.0695     # Polymarket US taker fee (in effect since Sept 17, 2026)
NOVIG_FEE_COEF = 0.03      # Novig game-market taker fee (0.06 on futures); often 0 before a game starts

MIN_EDGE = float(os.getenv("MIN_EDGE", "0.01"))
MAX_EDGE = float(os.getenv("MAX_EDGE", "0.08"))
MIN_MATCH_SCORE = float(os.getenv("MIN_MATCH_SCORE", "85"))
MAX_DAYS_APART = float(os.getenv("MAX_DAYS_APART", "3"))
MIN_SIZE = float(os.getenv("MIN_SIZE", "10"))
# Your usual trade size in dollars -- alerts show how many contracts per side this buys.
BUDGET = float(os.getenv("BUDGET", "100"))
STATE_FILE = os.getenv("STATE_FILE", "state/alerted.json")
NTFY_TOPIC = os.getenv("NTFY_TOPIC", "").strip()
DRY_RUN = os.getenv("DRY_RUN") == "1"
# Manual runs ("Run workflow" button) also print sample raw data for tuning the matcher
SAMPLE_MODE = os.getenv("GITHUB_EVENT_NAME") == "workflow_dispatch" or os.getenv("SAMPLES") == "1"
KALSHI_SAMPLES: list[dict] = []
POLY_SAMPLES: list[dict] = []
NOVIG_SAMPLES: list[dict] = []

session = requests.Session()
session.headers["User-Agent"] = "arb-scanner/1.0"


# ---------------------------------------------------------------- helpers

def num(x) -> float | None:
    """Parse numbers that may arrive as '0.5600', 56, {'value': '0.56'}, or None."""
    if x is None:
        return None
    if isinstance(x, dict):
        x = x.get("value")
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def parse_time(s) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None


STOPWORDS = {"will", "the", "a", "an", "be", "to", "of", "in", "on", "by", "at",
             "win", "wins", "winner", "vs", "v", "versus", "game", "match", "market"}


def norm(text: str) -> str:
    text = (text or "").lower().replace("&", " and ")
    text = re.sub(r"[^a-z0-9 ]+", " ", text)
    return " ".join(w for w in text.split() if w not in STOPWORDS)


def get_json(url, params=None, tries=4):
    for i in range(tries):
        try:
            r = session.get(url, params=params, timeout=30)
            if r.status_code == 429:  # rate limited: back off
                time.sleep(2 * (i + 1))
                continue
            r.raise_for_status()
            return r.json()
        except requests.RequestException as e:
            if i == tries - 1:
                raise
            print(f"  retrying {url}: {e}")
            time.sleep(2 * (i + 1))


def taker_fee(coef: float, p: float) -> float:
    return coef * p * (1 - p)


# ---------------------------------------------------------------- data model

@dataclass
class Quote:
    platform: str
    id: str               # ticker / slug
    title: str
    close: datetime | None
    yes_ask: float | None   # cost to buy YES now
    no_ask: float | None    # cost to buy NO now
    yes_ask_size: float | None = None
    no_ask_size: float | None = None
    url: str = ""
    sides: list[str] = field(default_factory=list)  # [YES label, NO label] if known
    base: str = ""        # title without the outcome label appended
    # Individual games (matched by team names + start time instead of titles)
    teams: list[str] = field(default_factory=list)
    game_time: datetime | None = None
    drawable: bool = False  # soccer-style: YES = one named team wins, a draw is possible
    fee_coef: float = KALSHI_FEE_COEF
    sport: str = ""       # "football", "baseball", "mma"... blank if unknown
    extra: dict = field(default_factory=dict)  # platform-specific ids for live rechecks


# ---------------------------------------------------------------- Kalshi

def fetch_kalshi() -> list[Quote]:
    """Pull open events with their markets, so each market gets the full event title."""
    out, cursor, pages = [], "", 0
    while True:
        params = {"status": "open", "limit": 200, "with_nested_markets": "true"}
        if cursor:
            params["cursor"] = cursor
        data = get_json(f"{KALSHI_BASE}/events", params)
        for ev in data.get("events", []):
            game = is_kalshi_game(ev)
            if SAMPLE_MODE and (len(KALSHI_SAMPLES) < 400 or game):
                KALSHI_SAMPLES.append(ev)
            teams = kalshi_teams(ev) if game else []
            for m in ev.get("markets") or []:
                q = kalshi_quote(m, ev)
                if q:
                    if len(teams) == 2:
                        q.teams, q.game_time = teams, q.close
                        q.sport = sport_of(ev.get("series_ticker") or "")
                    out.append(q)
        cursor = data.get("cursor") or ""
        pages += 1
        if pages % 10 == 0:
            print(f"  page {pages}: {len(out)} priced so far", flush=True)
        if not cursor or pages >= 500:
            break
    return out


DRAW_WORDS = {"tie", "draw", "tie game"}


SPORT_CODES = [  # (substring in a series ticker / slug / question, sport)
    ("NCAAF", "football"), ("NFL", "football"), ("CFB", "football"),
    ("MLB", "baseball"), ("NPB", "baseball"), ("KBO", "baseball"),
    ("WNBA", "basketball"), ("NBA", "basketball"), ("NCAAMB", "basketball"), ("NCAAWB", "basketball"),
    ("NCAAB", "basketball"), ("BBL", "basketball"), ("LNBP", "basketball"), ("ARGNACB", "basketball"),
    ("NHL", "hockey"), ("KHL", "hockey"), ("UFC", "mma"), ("MMA", "mma"), ("BOXING", "boxing"),
    ("CS2", "esports"), ("LOL", "esports"), ("DOTA", "esports"), ("R6", "esports"), ("VALORANT", "esports"),
    ("EPL", "soccer"), ("UEFA", "soccer"), ("UECL", "soccer"), ("UCL", "soccer"), ("MLS", "soccer"),
    ("USL", "soccer"), ("SERIE", "soccer"), ("LALIGA", "soccer"), ("BUNDES", "soccer"), ("LIGUE", "soccer"),
    ("BRASILEIRO", "soccer"), ("AFCON", "soccer"), ("CONCACAF", "soccer"), ("FRIENDLY", "soccer"),
    ("ENGNL", "soccer"), ("SOCCER", "soccer"), ("TENNIS", "tennis"), ("ATP", "tennis"), ("WTA", "tennis"),
]
QUESTION_SPORTS = {"mma event": "mma", "football event": "football", "boxing": "boxing",
                   "baseball": "baseball", "basketball": "basketball", "hockey": "hockey"}


def sport_of(code: str, text: str = "") -> str:
    code = (code or "").upper()
    for key, sport in SPORT_CODES:
        if key in code:
            return sport
    low = (text or "").lower()
    for key, sport in QUESTION_SPORTS.items():
        if key in low:
            return sport
    return ""


def is_kalshi_game(ev: dict) -> bool:
    """
    Head-to-head events: team games (KXNFLGAME, KXMLBGAME...) and fights (UFC, boxing).
    Recognised by the series name, or by a "A vs B" title with 2-3 outcome markets.
    """
    series = ev.get("series_ticker") or ""
    if re.search(r"(GAME|FIGHT|BOUT|MATCH)$", series):
        return True
    title = f"{ev.get('title') or ''} {ev.get('sub_title') or ''}"
    n = len(ev.get("markets") or [])
    return 2 <= n <= 3 and bool(re.search(r"\bvs\.?\b", title, re.I))


def kalshi_teams(ev: dict) -> list[str]:
    """Kalshi game events have one market per team; the YES label is the team name."""
    names = []
    for m in ev.get("markets") or []:
        lbl = (m.get("yes_sub_title") or "").strip()
        if lbl and norm(lbl) not in DRAW_WORDS and lbl not in names:
            names.append(lbl)
    return names


def kalshi_quote(m: dict, ev: dict | None = None) -> Quote | None:
    if m.get("market_type", "binary") != "binary":
        return None
    yes_ask = num(m.get("yes_ask_dollars"))
    no_ask = num(m.get("no_ask_dollars"))
    # Kalshi reports 0 / 1 when there's no real offer
    yes_ask = yes_ask if yes_ask and 0 < yes_ask < 1 else None
    no_ask = no_ask if no_ask and 0 < no_ask < 1 else None
    if yes_ask is None and no_ask is None:
        return None
    ev = ev or {}
    title = " ".join(x for x in (ev.get("title"), ev.get("sub_title")) if x) or m.get("title") or ""
    yes_label = m.get("yes_sub_title") or ""
    full = f"{title} {yes_label}".strip() if yes_label and yes_label.lower() not in title.lower() else title
    close = parse_time(m.get("expected_expiration_time")) or parse_time(m.get("close_time"))
    event = m.get("event_ticker", "")
    return Quote(
        platform="Kalshi",
        id=m["ticker"],
        title=full,
        close=close,
        yes_ask=yes_ask,
        no_ask=no_ask,
        # Kalshi's list endpoint gives size at the best YES ask; NO-ask size would need the order book
        yes_ask_size=num(m.get("yes_ask_size_fp")),
        # Kalshi event pages live at kalshi.com/markets/<series>/-/<event ticker>
        url=(f"https://kalshi.com/markets/{event.split('-')[0].lower()}/-/{event.lower()}"
             if event else "https://kalshi.com"),
        sides=[yes_label, m.get("no_sub_title") or ""],
        base=title,
    )


# ---------------------------------------------------------------- Polymarket US

def fetch_poly() -> list[Quote]:
    out, offset, limit = [], 0, 200
    while True:
        data = get_json(f"{POLY_BASE}/v1/markets",
                        {"limit": limit, "offset": offset, "active": "true", "closed": "false"})
        markets = data.get("markets", []) if isinstance(data, dict) else data
        for m in markets:
            if SAMPLE_MODE:
                POLY_SAMPLES.append(m)
            q = poly_quote(m)
            if q:
                out.append(q)
        if len(markets) < limit or offset > 20000:
            break
        offset += limit
        time.sleep(1.1)  # public API allows ~60 requests/minute
    return out


_POLY_EVENT_SLUGS: dict[str, str] | None = None


def poly_event_url(market_slug: str) -> str:
    """
    Polymarket US pages are per *event* (polymarket.us/event/<event slug>), and the
    markets list doesn't say which event a market belongs to. So the first time we
    need a link, pull the events list once and map market slug -> event slug.
    Only runs when there's an alert to send.
    """
    global _POLY_EVENT_SLUGS
    if _POLY_EVENT_SLUGS is None:
        _POLY_EVENT_SLUGS = {}
        try:
            offset = 0
            while offset <= 20000:
                data = get_json(f"{POLY_BASE}/v1/events",
                                {"limit": 200, "offset": offset, "active": "true", "closed": "false"})
                events = data.get("events", []) if isinstance(data, dict) else data
                for ev in events:
                    for m in ev.get("markets") or []:
                        if m.get("slug") and ev.get("slug"):
                            _POLY_EVENT_SLUGS[m["slug"]] = ev["slug"]
                if len(events) < 200:
                    break
                offset += 200
                time.sleep(1.1)
        except Exception as e:  # links are a nice-to-have; never block the alert
            print(f"  couldn't load Polymarket event list: {e}")
    slug = _POLY_EVENT_SLUGS.get(market_slug)
    if not slug:
        # Fallback: "tec-mlb-champ-2026-09-27-atl" -> "mlb-champ-2026-09-27"
        hit = re.match(r"^[a-z]+-(.+?\d{4}-\d{2}-\d{2})", market_slug)
        slug = hit.group(1) if hit else market_slug
    return f"https://polymarket.us/event/{slug}"


def side_label(side: dict) -> str:
    for k in ("description", "title", "name", "outcome", "label"):
        v = side.get(k)
        if isinstance(v, str) and v:
            return v
    team = side.get("team")
    if isinstance(team, dict):
        return team.get("name") or team.get("abbreviation") or ""
    return ""


def poly_title(m: dict, slug: str) -> str:
    """Join question/title/subtitle, skipping repeats. The question alone is often generic."""
    parts = []
    for k in ("question", "title", "subtitle", "titleShort"):
        v = m.get(k)
        if isinstance(v, str) and v.strip() and norm(v) not in [norm(x) for x in parts]:
            parts.append(v.strip())
    return " | ".join(parts) or slug


def poly_quote(m: dict) -> Quote | None:
    status = str(m.get("status", "")).upper()
    if status and "OPEN" not in status:
        return None
    best_bid = num(m.get("bestBidQuote"))   # highest bid for YES (long)
    best_ask = num(m.get("bestAskQuote"))   # lowest offer for YES (long)
    yes_ask = best_ask if best_ask and 0 < best_ask < 1 else None
    # Buying NO (short) = selling YES at the best bid, so it costs 1 - bid
    no_ask = (1 - best_bid) if best_bid and 0 < best_bid < 1 else None
    if yes_ask is None and no_ask is None:
        return None

    # Try to learn which side is YES (long) and which is NO (short), e.g. team names
    yes_lbl, no_lbl = "", ""
    for s in m.get("marketSides") or []:
        if not isinstance(s, dict):
            continue
        lbl = side_label(s)
        is_long = s.get("long")
        if is_long is None:
            is_long = str(s.get("side", s.get("identifier", ""))).upper() in ("LONG", "YES")
        if is_long:
            yes_lbl = yes_lbl or lbl
        else:
            no_lbl = no_lbl or lbl

    slug = m.get("slug") or str(m.get("id"))
    q = Quote(
        platform="Polymarket US",
        id=slug,
        title=poly_title(m, slug),
        close=parse_time(m.get("endDate")),
        yes_ask=yes_ask,
        no_ask=no_ask,
        url=f"https://polymarket.us/market/{slug}",
        sides=[yes_lbl, no_lbl],
        fee_coef=POLY_FEE_COEF,
    )

    # Individual games: moneylines (two named sides) and soccer-style "Will A win against B"
    mtype = str(m.get("sportsMarketTypeV2") or m.get("sportsMarketType") or "")
    start = parse_time(m.get("gameStartTime"))
    if start and "MONEYLINE" in mtype and yes_lbl and no_lbl and norm(yes_lbl) not in GENERIC_LABELS:
        q.teams, q.game_time = [yes_lbl, no_lbl], start
    elif start and "DRAWABLE" in mtype:
        hit = re.search(r"will (.+?) win against (.+?) in ", m.get("question") or "", re.I)
        if hit:
            q.teams, q.game_time, q.drawable = [hit.group(1), hit.group(2)], start, True
            q.sides = [hit.group(1), f"not {hit.group(1)}"]
    if q.game_time:
        q.close = q.game_time + timedelta(hours=4)  # pays out right after the game, not at endDate
        parts = slug.split("-")
        q.sport = sport_of(parts[1] if len(parts) > 1 else "", m.get("question") or "")
    return q


# ---------------------------------------------------------------- Novig

_novig_key = None


def novig_get(path: str, query: dict | None = None) -> dict:
    """Signed GET to Novig (every Novig request, even reading prices, must be signed)."""
    global _novig_key
    import base64, hashlib
    from urllib.parse import urlencode
    from cryptography.hazmat.primitives.serialization import load_pem_private_key
    if _novig_key is None:
        _novig_key = load_pem_private_key(NOVIG_PRIVATE_KEY.replace("\\n", "\n").encode(), None)
    qs = urlencode({k: v for k, v in (query or {}).items() if v is not None})
    for i in range(4):
        ts = str(int(time.time() * 1000))
        text = "\n".join(["NOVIG-V3", ts, "GET", path, qs, hashlib.sha256(b"").hexdigest()])
        sig = base64.b64encode(_novig_key.sign(text.encode())).decode()
        r = session.get(NOVIG_BASE + path + (f"?{qs}" if qs else ""), timeout=30, headers={
            "Novig-Key-Id": NOVIG_KEY_ID, "Novig-Timestamp": ts, "Novig-Signature": sig})
        if r.status_code == 429:
            time.sleep(float(r.headers.get("Retry-After", "1")))
            continue
        r.raise_for_status()
        return r.json()
    r.raise_for_status()
    return {}


def novig_book(market_id: str) -> dict:
    return novig_get(f"/v3/catalog/markets/{market_id}/book").get("orders") or {}


def novig_take(book: dict, resting_outcome: str) -> tuple[float | None, float | None]:
    """
    Every Novig order is a *buy* of one outcome. So the cheapest way to buy outcome A
    right now is to match the best resting buy on outcome B: you pay 1 - B's price.
    Novig contracts pay 1 cent, so 100 of them = one $1 contract on Kalshi/Polymarket.
    """
    orders = book.get(resting_outcome) or []
    if not orders:
        return None, None
    best = num(orders[0].get("price"))
    if best is None:
        return None, None
    qty = sum(o.get("qty", 0) for o in orders if num(o.get("price")) == best)
    return 1 - best, qty / 100


HOME_AWAY = {"home", "away", "home team", "away team"}


def novig_teams(m: dict) -> list[str]:
    names = [str(o.get("name") or "").strip() for o in m.get("outcomes") or []]
    if len(names) == 2 and all(n and n.lower() not in HOME_AWAY for n in names):
        return names
    # Outcomes may be generic ("Home"/"Away"); take team names from "Away @ Home" text.
    # Only "@"/"at" says which side is home -- "A vs B" is ambiguous, so skip those
    # rather than risk flipping the teams (that would create fake arbs).
    hit = re.search(r"(.+?)\s+(?:@|at)\s+(.+)", m.get("description") or "", re.I)
    if hit and len(names) == 2:
        away, home = hit.group(1).strip(), hit.group(2).strip()
        return [home if n.lower().startswith("home") else away for n in names]
    return []


def fetch_novig() -> list[Quote]:
    if not (NOVIG_KEY_ID and NOVIG_PRIVATE_KEY):
        print("  (no Novig key set -- skipping Novig)")
        return []
    now_ms = int(time.time() * 1000)
    markets, after = [], None
    while True:
        data = novig_get("/v3/catalog/markets", {
            "limit": 5000, "after": after,
            "startsAfter": now_ms - 6 * 3600 * 1000,          # includes games in progress
            "startsBefore": now_ms + 10 * 86400 * 1000})
        markets += data.get("items") or []
        after = data.get("next")
        if not after:
            break
    if SAMPLE_MODE:
        NOVIG_SAMPLES.extend(markets[:400])
        from collections import Counter
        print("  Novig market types:", dict(Counter(m.get("marketType") for m in markets).most_common(15)))

    out = []
    for m in markets:
        mtype = str(m.get("marketType") or "").upper()
        outs = m.get("outcomes") or []
        if m.get("status") != "OPEN" or "MONEY" not in mtype or "3_WAY" in mtype or len(outs) != 2:
            continue
        teams = novig_teams(m)
        if len(teams) != 2:
            continue
        try:
            book = novig_book(m["marketId"])
        except Exception as e:
            print(f"  Novig book failed for {m.get('marketId')}: {e}")
            continue
        a, b = outs[0]["outcomeId"], outs[1]["outcomeId"]
        buy_a, size_a = novig_take(book, b)
        buy_b, size_b = novig_take(book, a)
        if buy_a is None and buy_b is None:
            continue
        fee = m.get("fee") or {}
        coef = num(fee.get("coefficient"))
        coef = NOVIG_FEE_COEF if coef is None else coef
        if fee.get("charged") is False:
            coef = 0.0
        start = datetime.fromtimestamp(m["startsTs"] / 1000, tz=timezone.utc) if m.get("startsTs") else None
        out.append(Quote(
            platform="Novig",
            id=m["marketId"],
            title=m.get("description") or " vs ".join(teams),
            close=(start + timedelta(hours=4)) if start else None,
            yes_ask=buy_a, no_ask=buy_b,                 # "YES" = outcome A, "NO" = outcome B
            yes_ask_size=size_a, no_ask_size=size_b,
            url="https://novig.com",
            sides=teams, teams=teams, game_time=start,
            fee_coef=coef,
            sport=sport_of(str(m.get("league") or m.get("sport") or ""), str(m.get("sport") or "")),
            extra={"outcomes": [a, b]},
        ))
        time.sleep(0.03)  # stay well under Novig's 50 requests/second
    return out


# ---------------------------------------------------------------- matching

def orientation(k: Quote, p: Quote) -> int:
    """
    +1 if Kalshi YES == Polymarket YES, -1 if Kalshi YES == Polymarket NO,
    0 if we can't tell (then we skip -- guessing wrong creates fake "arbs").
    """
    k_yes = norm(k.sides[0])
    p_yes, p_no = norm(p.sides[0]), norm(p.sides[1])
    if not (p_yes and p_no) or p_yes == p_no or {p_yes, p_no} == {"yes", "no"}:
        return 1  # plain Yes/No market: YES means the same thing on both
    if not k_yes:
        return 0
    s_yes = fuzz.token_set_ratio(k_yes, p_yes)
    s_no = fuzz.token_set_ratio(k_yes, p_no)
    if s_yes >= 80 and s_yes > s_no + 15:
        return 1
    if s_no >= 80 and s_no > s_yes + 15:
        return -1
    return 0


GENERIC_LABELS = {"", "yes", "no"}


def outcome_agrees(k: Quote, p: Quote, p_text: str) -> bool:
    """
    Kalshi often has one market per outcome under a shared event title
    (e.g. "Undefeated season" -> Notre Dame / Miami / Texas ...). Matching the
    event title isn't enough: the specific outcome (team, player, threshold)
    must also appear in the Polymarket market, or it's a different bet.
    """
    label = norm(k.sides[0] if k.sides else "")
    if label in GENERIC_LABELS or (k.base and label in norm(k.base)):
        return True
    return fuzz.partial_ratio(label, p_text) >= 90


def similarity(a: str, b: str, **kwargs) -> float:
    """
    Average of two fuzzy scores. token_set alone gives 100 whenever one title's
    words are a subset of the other ("sweden" vs "prime minister of sweden..."),
    so we blend in token_sort, which punishes big length differences.
    """
    return (fuzz.token_set_ratio(a, b) + fuzz.token_sort_ratio(a, b)) / 2


def team_score(a: str, b: str) -> float:
    return fuzz.token_set_ratio(norm(a), norm(b))


def match_games(kalshi: list[Quote], poly: list[Quote]):
    """
    Pair single-game markets by BOTH team names and start time. Titles are useless here
    (Kalshi: "Los Angeles L", Polymarket: "Los Angeles Lakers"), so we compare each
    team name separately and require both to line up.
    Yields (kalshi, poly, score, orientation).
    """
    import bisect
    pg = sorted((p.game_time.timestamp(), i) for i, p in enumerate(poly))
    times = [t for t, _ in pg]
    for k in kalshi:
        if not k.game_time:
            continue
        t = k.game_time.timestamp()
        # Kalshi's expected settlement is a few hours to a few days after the game starts
        lo = bisect.bisect_left(times, t - 4 * 86400)
        hi = bisect.bisect_right(times, t + 86400)
        best, best_score = None, 0.0
        for _, i in pg[lo:hi]:
            p = poly[i]
            if k.sport and p.sport and k.sport != p.sport:
                continue  # e.g. baseball "Lions" vs college-football "Lions"
            k1, k2 = k.teams
            p1, p2 = p.teams
            straight = min(team_score(k1, p1), team_score(k2, p2))
            crossed = min(team_score(k1, p2), team_score(k2, p1))
            sc = max(straight, crossed)
            if sc > best_score and abs(straight - crossed) >= 15:
                best, best_score = p, sc
        if not best or best_score < MIN_MATCH_SCORE:
            continue
        # Which Polymarket side is the team this Kalshi market's YES is on?
        k_yes = k.sides[0]
        s_yes, s_other = team_score(k_yes, best.teams[0]), team_score(k_yes, best.teams[1])
        if s_yes >= MIN_MATCH_SCORE and s_yes > s_other + 15:
            yield k, best, best_score, 1
        elif s_other >= MIN_MATCH_SCORE and s_other > s_yes + 15 and not best.drawable:
            # moneyline: Kalshi "B wins" == Polymarket NO on "A wins" (no draws possible)
            yield k, best, best_score, -1


def match(kalshi: list[Quote], poly: list[Quote]):
    import bisect
    names = [norm(p.title + " " + " ".join(p.sides)) for p in poly]
    # Sort Polymarket markets by close time so each Kalshi market only compares
    # against the handful closing within MAX_DAYS_APART (fast binary search).
    dated = sorted((p.close.timestamp(), i) for i, p in enumerate(poly) if p.close)
    times = [t for t, _ in dated]
    undated = [i for i, p in enumerate(poly) if not p.close]
    window = MAX_DAYS_APART * 86400
    cache: dict[tuple, tuple | None] = {}

    for n, k in enumerate(kalshi):
        if n and n % 10000 == 0:
            print(f"  matched {n}/{len(kalshi)} Kalshi markets...", flush=True)
        if k.close:
            t = k.close.timestamp()
            lo, hi = bisect.bisect_left(times, t - window), bisect.bisect_right(times, t + window)
            idx = [i for _, i in dated[lo:hi]] + undated
        else:
            idx = list(range(len(poly)))
        if not idx:
            continue
        # Many Kalshi markets share a title (e.g. strike ladders) -- only score each once
        ck = (norm(k.title), idx[0], idx[-1], len(idx))
        if ck not in cache:
            best = process.extractOne(ck[0], {i: names[i] for i in idx},
                                      scorer=similarity, score_cutoff=MIN_MATCH_SCORE)
            cache[ck] = (best[2], best[1]) if best else None
        hit = cache[ck]
        if hit and outcome_agrees(k, poly[hit[0]], names[hit[0]]):
            yield k, poly[hit[0]], hit[1]


# ---------------------------------------------------------------- pricing

@dataclass
class Opp:
    key: str
    k: Quote
    p: Quote
    score: float
    k_side: str
    p_side: str
    k_price: float
    p_price: float
    cost: float
    fees: float
    edge: float
    days: float | None
    k_size: float | None = None   # contracts available at the quoted Kalshi price
    p_size: float | None = None   # contracts available at the quoted Polymarket price

    @property
    def annualized(self) -> float | None:
        if not self.days or self.days <= 0:
            return None
        return (1 + self.edge / self.cost) ** (365 / max(self.days, 1)) - 1


def price_pair(k: Quote, p: Quote, score: float, orient: int | None = None) -> list[Opp]:
    if orient is None:
        orient = orientation(k, p)
    if orient == 0:
        return []
    # p_yes/p_no expressed in terms of the *Kalshi* YES outcome
    p_same_yes, p_same_no = (p.yes_ask, p.no_ask) if orient == 1 else (p.no_ask, p.yes_ask)
    p_names = ("YES", "NO") if orient == 1 else ("NO", "YES")

    combos = [("YES", k.yes_ask, p_names[1], p_same_no),
              ("NO", k.no_ask, p_names[0], p_same_yes)]
    now = datetime.now(timezone.utc)
    close = max([c for c in (k.close, p.close) if c], default=None)
    days = (close - now).total_seconds() / 86400 if close else None

    opps = []
    for k_side, kp, p_side, pp in combos:
        if kp is None or pp is None:
            continue
        ks = k.yes_ask_size if k_side == "YES" else k.no_ask_size
        ps = p.yes_ask_size if p_side == "YES" else p.no_ask_size
        if any(x is not None and x < MIN_SIZE for x in (ks, ps)):
            continue
        fees = taker_fee(k.fee_coef, kp) + taker_fee(p.fee_coef, pp)
        cost = kp + pp
        edge = 1 - cost - fees
        opps.append(Opp(f"{k.id}|{p.id}|{k_side}", k, p, score, k_side, p_side,
                        kp, pp, cost, fees, edge, days, k_size=ks, p_size=ps))
    return opps


# ---------------------------------------------------------------- alerts

def load_state() -> dict:
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(state: dict):
    os.makedirs(os.path.dirname(STATE_FILE) or ".", exist_ok=True)
    cutoff = time.time() - 3 * 86400  # forget after 3 days
    state = {k: v for k, v in state.items() if v.get("t", 0) > cutoff}
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)


def send(title: str, body: str, url: str = "", priority: str = "default", poly_url: str = ""):
    if DRY_RUN or not NTFY_TOPIC:
        print(f"\n[ALERT] {title}\n{body}\n")
        return
    headers = {"Title": title.encode("utf-8"), "Priority": priority, "Tags": "moneybag"}
    if url:
        headers["Click"] = url
        actions = [f"view, Kalshi, {url}"]
        if poly_url:
            actions.append(f"view, Polymarket, {poly_url}")
        headers["Actions"] = "; ".join(actions)
    try:
        requests.post(f"https://ntfy.sh/{NTFY_TOPIC}", data=body.encode("utf-8"),
                      headers=headers, timeout=15).raise_for_status()
    except requests.RequestException as e:
        print(f"  ntfy send failed: {e}")


def side_text(side: str, subject: str) -> str:
    """'YES on Kansas City' -- names what the YES/NO is about when it's a team or outcome."""
    if subject and norm(subject) not in GENERIC_LABELS:
        return f"{side} on {subject}"
    return side


def fmt_count(x: float | None, approx: bool = False) -> str:
    if x is None:
        return "unknown"
    return f"{'~' if approx else ''}{int(x):,}"


def size_lines(o: Opp) -> str:
    """
    Available: 340 on Kalshi, ~120 on Polymarket
    Max size at these prices: 120 contracts (~$114 in, ~$2 profit)
    $500 budget: 120 contracts each side (capped by available size)
    """
    per = o.cost + o.fees  # all-in cost of one contract on each side
    lines = [f"Available: {fmt_count(o.k_size, approx=o.k.platform == 'Polymarket US')} on {short(o.k)}, "
             f"{fmt_count(o.p_size, approx=o.p.platform == 'Polymarket US')} on {short(o.p)}"]
    known = [x for x in (o.k_size, o.p_size) if x is not None]
    max_n = int(min(known)) if known else None
    if max_n is not None:
        lines.append(f"Max size at these prices: {max_n:,} contracts "
                     f"(~${max_n * per:,.0f} in, ~${max_n * o.edge:,.2f} profit)")
    want = int(BUDGET // per) if per > 0 else 0
    n = want
    if max_n is not None and max_n < want:
        n = max_n
        lines.append(f"${BUDGET:,.0f} budget: {n:,} contracts each side (capped by available size)")
    else:
        lines.append(f"${BUDGET:,.0f} budget: {n:,} contracts each side "
                     f"(~${n * o.edge:,.2f} profit)")
    # Dollar amounts too -- some apps (Novig) take a dollar amount rather than a count
    lines.append(f"Spend: ${n * o.k_price:,.2f} on {short(o.k)}, ${n * o.p_price:,.2f} on {short(o.p)} "
                 f"(pays ${n:,} either way)")
    return "\n".join(lines)


def short(q: Quote) -> str:
    return "Polymarket" if q.platform == "Polymarket US" else q.platform


def leg_text(q: Quote, side: str) -> str:
    if q.platform == "Novig":  # Novig has no YES/NO -- you buy one of the outcomes
        return f"BUY {q.sides[0] if side == 'YES' else q.sides[1]}"
    if q.platform == "Kalshi":
        return side_text(side, q.sides[0] if q.sides else "")
    return side_text(side, q.sides[0] if q.teams and q.sides else "")


def grade(o: Opp) -> str:
    """
    Letter grade from profit per contract (more is better) and days to payout (fewer is better).

                       <= 7 days   8-60 days   > 60 days
        3¢+ /contract      A           B           C
        2-3¢               B           C           D
        1-2¢               C           D           D
    """
    cents = o.edge * 100
    r = 3 if cents >= 3 else 2 if cents >= 2 else 1
    t = 3 if o.days is not None and o.days <= 7 else 2 if o.days is not None and o.days <= 60 else 1
    return {6: "A", 5: "B", 4: "C"}.get(r + t, "D")


def format_opp(o: Opp) -> tuple[str, str]:
    """
    Phone notification layout:
      title:  1.1¢ / contract | 5% / yr | 72 days
      body:   <name of trade>
              (blank line)
              Kalshi - NO - $0.82
              <Kalshi ticker>
              (blank line)
              Polymarket - YES - $0.15
              <Polymarket slug>
    """
    ann = o.annualized
    ann_txt = f"{ann:.0%} / yr" if ann is not None and ann < 50 else "n/a / yr"
    days_txt = f"{o.days:.0f} days" if o.days is not None else "? days"
    title = f"Grade {grade(o)} | {o.edge * 100:.1f}¢ / contract | {ann_txt} | {days_txt}"
    body = (
        f"{o.k.title}\n"
        f"\n"
        f"{short(o.k)} - {leg_text(o.k, o.k_side)} - ${o.k_price:.2f}\n"
        f"{o.k.id}\n"
        f"\n"
        f"{short(o.p)} - {leg_text(o.p, o.p_side)} - ${o.p_price:.2f}\n"
        f"{o.p.id}\n"
        f"\n"
        f"{size_lines(o)}\n"
        f"\n"
        f"Title match: {o.score:.0f}%\n"
        f"Check both rulebooks match before trading.\n"
        f"\n"
        f"{short(o.k)}: {o.k.url}\n"
        f"{short(o.p)}: {o.p.url}"
    )
    return title, body


# ---------------------------------------------------------------- samples

def print_samples():
    from collections import Counter
    now = datetime.now(timezone.utc)
    print("\n================ SAMPLE DATA (manual runs only) ================")
    series = Counter((e.get("series_ticker") or "?") for e in KALSHI_SAMPLES
                     if (e.get("category") or "").lower() == "sports")
    print("Kalshi sports series in sample:", dict(series.most_common(25)))

    def soon(ts, days=10):
        t = parse_time(ts)
        return t is not None and (t - now).total_seconds() < days * 86400

    games = [e for e in KALSHI_SAMPLES if is_kalshi_game(e)]
    fights = [e for e in games if re.search("UFC|MMA|BOX|FIGHT", e.get("series_ticker") or "")]
    print(f"\n-- Kalshi fight events ({len(fights)}) --")
    for e in fights[:8]:
        print(f"  {e.get('event_ticker')} | {e.get('title')!r} | sub={e.get('sub_title')!r}")
        for m in (e.get("markets") or [])[:2]:
            print(f"      {m.get('ticker')} yes={m.get('yes_sub_title')!r} ask={m.get('yes_ask_dollars')} "
                  f"exp={m.get('expected_expiration_time')}")
    print(f"\nKalshi game series: {dict(Counter(e.get('series_ticker') for e in games).most_common(20))}")
    print(f"\n-- Kalshi game events ({len(games)}) --")
    for e in games[:12]:
        print(f"  {e.get('event_ticker')} | {e.get('title')!r} | sub={e.get('sub_title')!r}")
        for m in (e.get("markets") or [])[:3]:
            print(f"      {m.get('ticker')} yes={m.get('yes_sub_title')!r} "
                  f"ask={m.get('yes_ask_dollars')} exp={m.get('expected_expiration_time')}")

    types = Counter(str(m.get("sportsMarketTypeV2") or m.get("sportsMarketType")) for m in POLY_SAMPLES)
    print("\nPolymarket US sports market types:", dict(types.most_common(15)))
    pgames = [m for m in POLY_SAMPLES
              if re.search("MONEYLINE|DRAWABLE", str(m.get("sportsMarketTypeV2") or m.get("sportsMarketType")))]
    print(f"\n-- Polymarket US moneyline / match-winner markets ({len(pgames)}) --")
    for m in pgames[:12]:
        sides = [(sd.get("description"), sd.get("long"), sd.get("teamId"))
                 for sd in (m.get("marketSides") or []) if isinstance(sd, dict)]
        print(f"  {m.get('slug')} | type={m.get('sportsMarketTypeV2') or m.get('sportsMarketType')}")
        print(f"      q={m.get('question')!r} title={m.get('title')!r} sub={m.get('subtitle')!r} "
              f"short={m.get('titleShort')!r}")
        print(f"      start={m.get('gameStartTime')} end={m.get('endDate')} outcomes={m.get('outcomes')} "
              f"sides={sides} bid={num(m.get('bestBidQuote'))} ask={num(m.get('bestAskQuote'))}")
    print("\n-- Polymarket US non-sports examples --")
    for m in [m for m in POLY_SAMPLES if m.get("category") != "sports"][:10]:
        print(f"  {m.get('slug')} | q={m.get('question')!r} title={m.get('title')!r} "
              f"sub={m.get('subtitle')!r} end={m.get('endDate')}")
    print(f"\n-- Novig markets ({len(NOVIG_SAMPLES)} sampled) --")
    for m in NOVIG_SAMPLES[:15]:
        print(f"  {m.get('marketType')} | {m.get('description')!r} | status={m.get('status')} "
              f"| outcomes={[o.get('name') for o in m.get('outcomes') or []]} | fee={m.get('fee')}")
    print("================================================================\n")


# ---------------------------------------------------------------- main

def main():
    sys.stdout.reconfigure(line_buffering=True)  # show log lines live in GitHub
    if os.getenv("TEST_ALERT") == "1":
        send("Arb scanner test", "If you can read this on your phone, alerts work.")
        print("test alert sent" if NTFY_TOPIC else "NTFY_TOPIC not set; printed instead")
        return

    t0 = time.time()
    print("Fetching Kalshi...")
    kalshi = fetch_kalshi()
    print(f"  {len(kalshi)} priced Kalshi markets")
    print("Fetching Polymarket US...")
    poly = fetch_poly()
    print(f"  {len(poly)} priced Polymarket US markets")
    print("Fetching Novig...")
    try:
        novig = fetch_novig()
    except Exception as e:  # Novig trouble shouldn't stop the Kalshi/Polymarket scan
        print(f"  Novig failed: {e}")
        novig = []
    print(f"  {len(novig)} priced Novig game markets")

    if SAMPLE_MODE:
        print_samples()

    state = load_state()
    sent = 0

    # 1) Fights/games first: they match in seconds and their gaps close fastest,
    #    so alert on them before the slower futures matching even starts.
    k_games = [q for q in kalshi if q.game_time]
    p_games = [q for q in poly if q.game_time]
    print(f"Games: {len(k_games)} Kalshi team markets, {len(p_games)} Polymarket US game markets")
    game_pairs = list(match_games(k_games, p_games))
    print(f"Matched {len(game_pairs)} Kalshi-Polymarket game pairs")
    if novig:
        kn = list(match_games(k_games, novig))
        pn = list(match_games([q for q in p_games if not q.drawable], novig))
        print(f"Matched {len(kn)} Kalshi-Novig and {len(pn)} Polymarket-Novig game pairs")
        game_pairs += kn + pn
    if SAMPLE_MODE:
        for k, p, sc, o in game_pairs[:40]:
            print(f"  GAME [{short(k)}-{short(p)}] {k.id} yes={k.sides[0]!r} {k.teams} <-> {p.id} {p.teams} "
                  f"({sc:.0f}, {'same' if o == 1 else 'opposite'} side)")
    game_opps = [o for k, p, s, orient in game_pairs for o in price_pair(k, p, s, orient)]
    sent += alert(game_opps, state)
    save_state(state)  # saved right away so a later crash can't cause repeat alerts

    # 2) Futures, elections, everything else
    other_pairs = [(k, p, s, None) for k, p, s in match(
        [q for q in kalshi if not q.game_time], [q for q in poly if not q.game_time])]
    print(f"Matched {len(other_pairs)} other pairs")
    other_opps = [o for k, p, s, orient in other_pairs for o in price_pair(k, p, s, orient)]
    sent += alert(other_opps, state)
    save_state(state)

    all_opps = sorted(game_opps + other_opps, key=lambda o: -o.edge)
    print("\nTop 10 closest-to-arb pairs this run:")
    for o in all_opps[:10]:
        print(f"  net {o.edge:+.3f} | K {o.k_side} {o.k_price:.2f} + P {o.p_side} {o.p_price:.2f}"
              f" | {o.k.title[:45]!r} <-> {o.p.title[:45]!r} ({o.score:.0f})")
    print(f"\nSent {sent} new alert(s). Took {time.time() - t0:.0f}s.")


def live_leg(q: Quote, side: str) -> tuple[float | None, float | None]:
    """Fresh (price, contracts available) for buying `side` of quote q, from that platform."""
    if q.platform == "Kalshi":
        km = get_json(f"{KALSHI_BASE}/markets/{q.id}", tries=2).get("market") or {}
        price = num(km.get("yes_ask_dollars" if side == "YES" else "no_ask_dollars"))
        # Buying NO on Kalshi fills against YES bids, so NO size = size at the best YES bid
        size = num(km.get("yes_ask_size_fp" if side == "YES" else "yes_bid_size_fp"))
        return price, size
    if q.platform == "Polymarket US":
        bbo = get_json(f"{POLY_BASE}/v1/markets/{q.id}/bbo", tries=2).get("marketData") or {}
        if side == "YES":
            return num(bbo.get("bestAsk")), num(bbo.get("askShares"))
        bid = num(bbo.get("bestBid"))
        return (1 - bid if bid else None), num(bbo.get("bidShares"))  # NO = sell into YES bids
    if q.platform == "Novig":
        book = novig_book(q.id)
        a, b = q.extra["outcomes"]
        # To buy outcome A you take a resting order on outcome B (and vice versa)
        return novig_take(book, b if side == "YES" else a)
    return None, None


def refresh(o: Opp) -> bool | None:
    """
    Re-price both legs from live quotes right before alerting (the bulk download can be
    a few minutes old by now). Returns True if the arb still clears MIN_EDGE, False if
    it's gone, None if the recheck itself failed.
    """
    try:
        kp, o.k_size = live_leg(o.k, o.k_side)
        pp, o.p_size = live_leg(o.p, o.p_side)
    except Exception as e:
        print(f"  recheck failed for {o.k.id}: {e}")
        return None
    if not kp or not pp or not (0 < kp < 1) or not (0 < pp < 1):
        return False
    o.k_price, o.p_price = kp, pp
    o.cost = kp + pp
    o.fees = taker_fee(o.k.fee_coef, kp) + taker_fee(o.p.fee_coef, pp)
    o.edge = 1 - o.cost - o.fees
    return o.edge >= MIN_EDGE


def alert(opps: list[Opp], state: dict) -> int:
    sent = 0
    for o in sorted(opps, key=lambda o: -o.edge):
        if o.edge < MIN_EDGE:
            break
        if o.edge > MAX_EDGE:
            print(f"  skip (edge {o.edge:.2f} too big -> probably not the same market): "
                  f"{o.k.title[:50]!r} <-> {o.p.title[:50]!r}")
            continue
        prev = state.get(o.key)
        # re-alert only if the edge improved by at least 1 cent
        if prev and o.edge < prev["edge"] + 0.01:
            continue
        live = None if DRY_RUN else refresh(o)
        if live is False:
            print(f"  gone on recheck: {o.k.id} <-> {o.p.id}")
            continue
        for q in (o.k, o.p):
            if q.platform == "Polymarket US" and "/event/" not in q.url:
                q.url = poly_event_url(q.id)
        title, body = format_opp(o)
        if live is None and not DRY_RUN:
            body += "\n(Couldn't recheck live prices -- verify before trading.)"
        send(title, body, o.k.url, "high" if o.edge >= 0.03 else "default", o.p.url)
        state[o.key] = {"edge": o.edge, "t": time.time()}
        sent += 1
    return sent


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # make failures visible in the Actions log
        print(f"ERROR: {e}", file=sys.stderr)
        raise
