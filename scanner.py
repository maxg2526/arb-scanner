"""
Kalshi <-> Polymarket US arbitrage scanner.

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
"""

from __future__ import annotations

import json
import math
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

import requests
from rapidfuzz import fuzz, process

KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"
POLY_BASE = "https://gateway.polymarket.us"

# Taker fee coefficients: fee per contract ~= COEF * p * (1 - p)
KALSHI_FEE_COEF = 0.07     # Kalshi general taker fee (rounded UP to the cent per order)
POLY_FEE_COEF = 0.0695     # Polymarket US taker fee (in effect since Sept 17, 2026)

MIN_EDGE = float(os.getenv("MIN_EDGE", "0.01"))
MAX_EDGE = float(os.getenv("MAX_EDGE", "0.08"))
MIN_MATCH_SCORE = float(os.getenv("MIN_MATCH_SCORE", "85"))
MAX_DAYS_APART = float(os.getenv("MAX_DAYS_APART", "3"))
MIN_SIZE = float(os.getenv("MIN_SIZE", "10"))
STATE_FILE = os.getenv("STATE_FILE", "state/alerted.json")
NTFY_TOPIC = os.getenv("NTFY_TOPIC", "").strip()
DRY_RUN = os.getenv("DRY_RUN") == "1"
# Manual runs ("Run workflow" button) also print sample raw data for tuning the matcher
SAMPLE_MODE = os.getenv("GITHUB_EVENT_NAME") == "workflow_dispatch" or os.getenv("SAMPLES") == "1"
KALSHI_SAMPLES: list[dict] = []
POLY_SAMPLES: list[dict] = []

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
            if SAMPLE_MODE and len(KALSHI_SAMPLES) < 400:
                KALSHI_SAMPLES.append(ev)
            for m in ev.get("markets") or []:
                q = kalshi_quote(m, ev)
                if q:
                    out.append(q)
        cursor = data.get("cursor") or ""
        pages += 1
        if pages % 10 == 0:
            print(f"  page {pages}: {len(out)} priced so far", flush=True)
        if not cursor or pages >= 500:
            break
    return out


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
        url=f"https://kalshi.com/markets/{event.split('-')[0].lower()}" if event else "https://kalshi.com",
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
            if SAMPLE_MODE and len(POLY_SAMPLES) < 3000:
                POLY_SAMPLES.append(m)
            q = poly_quote(m)
            if q:
                out.append(q)
        if len(markets) < limit or offset > 20000:
            break
        offset += limit
        time.sleep(1.1)  # public API allows ~60 requests/minute
    return out


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
    return Quote(
        platform="Polymarket US",
        id=slug,
        title=poly_title(m, slug),
        close=parse_time(m.get("endDate")),
        yes_ask=yes_ask,
        no_ask=no_ask,
        url=f"https://polymarket.us/market/{slug}",
        sides=[yes_lbl, no_lbl],
    )


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

    @property
    def annualized(self) -> float | None:
        if not self.days or self.days <= 0:
            return None
        return (1 + self.edge / self.cost) ** (365 / max(self.days, 1)) - 1


def price_pair(k: Quote, p: Quote, score: float) -> list[Opp]:
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
        if k_side == "YES" and k.yes_ask_size is not None and k.yes_ask_size < MIN_SIZE:
            continue
        fees = taker_fee(KALSHI_FEE_COEF, kp) + taker_fee(POLY_FEE_COEF, pp)
        cost = kp + pp
        edge = 1 - cost - fees
        opps.append(Opp(f"{k.id}|{p.id}|{k_side}", k, p, score, k_side, p_side,
                        kp, pp, cost, fees, edge, days))
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


def send(title: str, body: str, url: str = "", priority: str = "default"):
    if DRY_RUN or not NTFY_TOPIC:
        print(f"\n[ALERT] {title}\n{body}\n")
        return
    headers = {"Title": title.encode("utf-8"), "Priority": priority, "Tags": "moneybag"}
    if url:
        headers["Click"] = url
    try:
        requests.post(f"https://ntfy.sh/{NTFY_TOPIC}", data=body.encode("utf-8"),
                      headers=headers, timeout=15).raise_for_status()
    except requests.RequestException as e:
        print(f"  ntfy send failed: {e}")


def format_opp(o: Opp) -> tuple[str, str]:
    ann = o.annualized
    ann_txt = f" (~{ann:.0%}/yr)" if ann is not None and ann < 50 else ""
    days_txt = f"{o.days:.1f} days" if o.days is not None else "unknown"
    title = f"Arb {o.edge*100:.1f}¢/contract{ann_txt}"
    body = (
        f"BUY Kalshi {o.k_side} @ {o.k_price:.2f}\n"
        f"  {o.k.title}\n  [{o.k.id}]\n"
        f"BUY Polymarket {o.p_side} @ {o.p_price:.2f}\n"
        f"  {o.p.title}\n  [{o.p.id}]\n"
        f"Cost {o.cost:.3f} + fees {o.fees:.3f} -> net {o.edge:.3f} per $1\n"
        f"Resolves in {days_txt} | title match {o.score:.0f}/100\n"
        f"CHECK BOTH RULEBOOKS MATCH BEFORE TRADING.\n"
        f"Kalshi: {o.k.url}"
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

    games = [e for e in KALSHI_SAMPLES if (e.get("category") or "").lower() == "sports"
             and any(soon(m.get("expected_expiration_time") or m.get("close_time"))
                     for m in e.get("markets") or [])]
    print(f"\n-- Kalshi sports events settling within 10 days ({len(games)}) --")
    for e in games[:20]:
        print(f"  {e.get('event_ticker')} | {e.get('title')!r} | sub={e.get('sub_title')!r}")
        for m in (e.get("markets") or [])[:3]:
            print(f"      {m.get('ticker')} yes={m.get('yes_sub_title')!r} "
                  f"ask={m.get('yes_ask_dollars')} exp={m.get('expected_expiration_time')}")

    types = Counter(str(m.get("sportsMarketTypeV2") or m.get("sportsMarketType")) for m in POLY_SAMPLES)
    print("\nPolymarket US sports market types:", dict(types.most_common(15)))
    pgames = [m for m in POLY_SAMPLES if m.get("gameStartTime")]
    print(f"\n-- Polymarket US markets with a game start time ({len(pgames)}) --")
    for m in pgames[:20]:
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

    if SAMPLE_MODE:
        print_samples()

    pairs = list(match(kalshi, poly))
    print(f"Matched {len(pairs)} candidate pairs")

    all_opps = [o for k, p, s in pairs for o in price_pair(k, p, s)]
    all_opps.sort(key=lambda o: -o.edge)

    print("\nTop 10 closest-to-arb pairs this run:")
    for o in all_opps[:10]:
        print(f"  net {o.edge:+.3f} | K {o.k_side} {o.k_price:.2f} + P {o.p_side} {o.p_price:.2f}"
              f" | {o.k.title[:45]!r} <-> {o.p.title[:45]!r} ({o.score:.0f})")

    state = load_state()
    sent = 0
    for o in all_opps:
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
        title, body = format_opp(o)
        send(title, body, o.p.url, "high" if o.edge >= 0.03 else "default")
        state[o.key] = {"edge": o.edge, "t": time.time()}
        sent += 1

    save_state(state)
    print(f"\nSent {sent} new alert(s). Took {time.time() - t0:.0f}s.")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # make failures visible in the Actions log
        print(f"ERROR: {e}", file=sys.stderr)
        raise
