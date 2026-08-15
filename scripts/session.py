#!/usr/bin/env python3
"""
Runs one trading session.

  fetch quotes  ->  ask Claude what to do  ->  apply trades  ->  write docs/book.json

Everything is paper money. Nothing here touches a real broker.
"""

import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import yfinance as yf

ROOT = Path(__file__).resolve().parent.parent
BOOK = ROOT / "docs" / "book.json"
HISTORY = ROOT / "docs" / "history.jsonl"
UNIVERSE = ROOT / "scripts" / "universe.json"

MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-5")
API_KEY = os.environ.get("ANTHROPIC_API_KEY")
DRY_RUN = os.environ.get("DRY_RUN") == "1"

RETRY_ATTEMPTS = 4
RETRY_BASE_DELAY = 5  # seconds; doubles each attempt

INDICES = {
    "^FTSE": "FTSE 100",
    "^IXIC": "Nasdaq Comp",
    "^NYA": "NYSE Comp",
    "^GSPC": "S&P 500",
    "^DJI": "Dow Jones",
    "^GDAXI": "DAX",
    "^N225": "Nikkei 225",
    "^HSI": "Hang Seng",
}

SECTOR_LABELS = {
    "energy": "Energy", "healthcare": "Healthcare", "financials": "Financials",
    "industrials_defence": "Industrials", "consumer_staples": "Staples",
    "utilities_infra": "Utilities", "materials_precious": "Materials",
    "real_estate": "Property", "technology": "Technology", "broad_hedges": "Index/Hedge",
}


# Symbols quoted in pence rather than pounds on Yahoo.
def is_pence(sym: str) -> bool:
    return sym.endswith(".L")


def sector_for(sym, universe):
    for key, names in universe.items():
        if not key.startswith("_") and sym in names:
            return SECTOR_LABELS.get(key, key.title())
    return "Other"


# ---------------------------------------------------------------- quotes

def fetch_quotes(symbols):
    """Return {symbol: {price_gbp, price_native, currency, change_pct, name}}."""
    out = {}
    if not symbols:
        return out
    data = yf.download(
        tickers=" ".join(symbols),
        period="5d",
        interval="1d",
        group_by="ticker",
        auto_adjust=False,
        progress=False,
        threads=True,
    )
    for sym in symbols:
        try:
            frame = data[sym] if len(symbols) > 1 else data
            closes = frame["Close"].dropna()
            if len(closes) == 0:
                continue
            last = float(closes.iloc[-1])
            prev = float(closes.iloc[-2]) if len(closes) > 1 else last
            out[sym] = {
                "native": round(last, 4),
                "change_pct": round((last - prev) / prev * 100, 2) if prev else 0.0,
                "pence": is_pence(sym),
            }
        except Exception as exc:  # a single bad ticker must not kill the session
            print(f"  ! no quote for {sym}: {exc}", file=sys.stderr)
    return out


def fetch_fx():
    try:
        fx = yf.Ticker("GBPUSD=X").history(period="5d")["Close"].dropna()
        return round(float(fx.iloc[-1]), 4)
    except Exception:
        return 1.35


def to_gbp(sym, native, fx):
    """LSE tickers quote in pence. US tickers quote in USD."""
    if is_pence(sym):
        return native / 100.0
    return native / fx


# ---------------------------------------------------------------- valuation

def value_book(book, quotes, fx):
    holdings = []
    invested = 0.0
    for h in book["holdings"]:
        q = quotes.get(h["ticker"])
        now_native = q["native"] if q else h["last_native"]
        now_gbp = to_gbp(h["ticker"], now_native, fx)
        value = h["shares"] * now_gbp
        cost = h["cost_gbp"]
        holdings.append({
            **h,
            "last_native": now_native,
            "value_gbp": round(value, 2),
            "cost_gbp": round(cost, 2),
            "pl_gbp": round(value - cost, 2),
            "pl_pct": round((value - cost) / cost * 100, 2) if cost else 0.0,
        })
        invested += value
    book["holdings"] = holdings
    book["invested_gbp"] = round(invested, 2)
    book["total_gbp"] = round(invested + book["cash_gbp"], 2)
    book["pl_gbp"] = round(book["total_gbp"] - book["starting_capital"], 2)
    book["pl_pct"] = round(book["pl_gbp"] / book["starting_capital"] * 100, 2)
    return book


# ---------------------------------------------------------------- the model

SYSTEM = """You run a £1,000 paper portfolio. Real prices, simulated money, no broker.

MANDATE
- Total return over roughly one month, with a medium-term eye. Not a day-trading book.
- Sector-agnostic. Do NOT default to technology or AI. Energy, healthcare, financials,
  industrials, staples, utilities, materials and property all count equally.
- Concentration is allowed but position sizes above 25% of the book need a stated reason.
- Holding cash is a legitimate position. Say so when you mean it.
- You may trade only tickers in the supplied universe. If you want something outside it,
  put the request in `universe_requests` — do not invent a ticker.

STYLE
- Every trade carries a written argument that names the specific thing you believe and the
  specific thing that would prove you wrong. No filler, no hedging into meaninglessness.
- If nothing is worth doing, return an empty trades list and say why. Churn is not activity.
- You will be read back your own past reasoning. When you were wrong, say so plainly.

OUTPUT
Return ONLY a JSON object, no markdown fences, no preamble:

{
  "market_read": "2-4 sentences on what actually matters right now",
  "trades": [
    {"action":"buy"|"sell", "ticker":"SHEL.L", "amount_gbp": 180.0,
     "why":"the argument, and what would falsify it"}
  ],
  "universe_requests": ["TICKER — why you want it"],
  "watchlist": [{"ticker":"XOM","note":"what you're waiting for"}]
}

For sells, amount_gbp is the approximate GBP value to sell; use the full position value to exit.
Never spend more cash than the book holds."""


def _request_decision(payload):
    """One attempt: call the API and parse a decision out of the reply."""
    body = {
        "model": MODEL,
        "max_tokens": 4000,
        "system": SYSTEM,
        "messages": [{"role": "user", "content": json.dumps(payload, indent=2)}],
    }
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=json.dumps(body).encode(),
        headers={
            "content-type": "application/json",
            "x-api-key": API_KEY,
            "anthropic-version": "2023-06-01",
        },
    )
    with urllib.request.urlopen(req, timeout=300) as resp:
        data = json.loads(resp.read())

    blocks = data.get("content", [])
    text = "\n".join(b["text"] for b in blocks if b.get("type") == "text")
    text = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    if not text:
        raise ValueError(
            f"empty reply (stop_reason={data.get('stop_reason')!r}, "
            f"content block types={[b.get('type') for b in blocks]})"
        )
    # Guard against stray preamble text; take the outermost JSON object.
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"no JSON in model reply:\n{text[:600]}")
    candidate = text[start:end + 1]
    try:
        return json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise ValueError(f"model reply was not valid JSON ({exc}):\n{candidate}") from exc


def ask_claude(payload):
    """Call the API, retrying on failure with exponential backoff."""
    last_error = None
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            return _request_decision(payload)
        except Exception as exc:
            last_error = exc
            print(f"  ! attempt {attempt}/{RETRY_ATTEMPTS} failed: {exc}", file=sys.stderr)
            if attempt < RETRY_ATTEMPTS:
                delay = RETRY_BASE_DELAY * (2 ** (attempt - 1))
                print(f"  retrying in {delay}s…", file=sys.stderr)
                time.sleep(delay)

    raise RuntimeError(f"ask_claude failed after {RETRY_ATTEMPTS} attempts") from last_error


# ---------------------------------------------------------------- execution

def apply_trades(book, decision, quotes, fx, today):
    filled = []
    for t in decision.get("trades", []):
        sym, action = t["ticker"], t["action"]
        amount = float(t["amount_gbp"])
        q = quotes.get(sym)
        if not q:
            print(f"  ! skipping {action} {sym}: no quote", file=sys.stderr)
            continue
        price_gbp = to_gbp(sym, q["native"], fx)
        if price_gbp <= 0:
            continue

        if action == "buy":
            amount = min(amount, book["cash_gbp"])
            if amount < 1:
                print(f"  ! skipping buy {sym}: insufficient cash", file=sys.stderr)
                continue
            shares = amount / price_gbp
            existing = next((h for h in book["holdings"] if h["ticker"] == sym), None)
            if existing:  # weighted-average the display price, accumulate actual GBP cost
                total_sh = existing["shares"] + shares
                existing["entry_native"] = round(
                    (existing["entry_native"] * existing["shares"] + q["native"] * shares) / total_sh, 4)
                existing["cost_gbp"] = round(existing["cost_gbp"] + amount, 2)
                existing["shares"] = round(total_sh, 6)
            else:
                book["holdings"].append({
                    "ticker": sym,
                    "shares": round(shares, 6),
                    "entry_native": q["native"],
                    "cost_gbp": round(amount, 2),
                    "last_native": q["native"],
                    "opened": today,
                })
            book["cash_gbp"] = round(book["cash_gbp"] - amount, 2)

        elif action == "sell":
            h = next((x for x in book["holdings"] if x["ticker"] == sym), None)
            if not h:
                print(f"  ! skipping sell {sym}: not held", file=sys.stderr)
                continue
            held_value = h["shares"] * price_gbp
            amount = min(amount, held_value)
            shares = amount / price_gbp
            sold_fraction = shares / h["shares"] if h["shares"] else 0
            h["cost_gbp"] = round(h["cost_gbp"] * (1 - sold_fraction), 2)
            h["shares"] = round(h["shares"] - shares, 6)
            if h["shares"] < 1e-6:
                book["holdings"].remove(h)
            book["cash_gbp"] = round(book["cash_gbp"] + amount, 2)
        else:
            continue

        entry = {
            "date": today, "action": action, "ticker": sym,
            "amount_gbp": round(amount, 2), "price_native": q["native"],
            "why": t.get("why", ""),
        }
        book["trades"].insert(0, entry)
        filled.append(entry)
        print(f"  {action.upper():4} {sym:9} £{amount:7.2f} @ {q['native']}")
    return filled


# ---------------------------------------------------------------- main

def main():
    if not API_KEY and not DRY_RUN:
        sys.exit("ANTHROPIC_API_KEY is not set")

    book = json.loads(BOOK.read_text())
    universe = json.loads(UNIVERSE.read_text())
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    tradable = [t for sector, names in universe.items()
                if not sector.startswith("_") for t in names]
    held = [h["ticker"] for h in book["holdings"]]

    print("Fetching quotes…")
    fx = fetch_fx()
    quotes = fetch_quotes(sorted(set(tradable + held)))
    index_q = fetch_quotes(list(INDICES))
    print(f"  {len(quotes)} quotes, GBP/USD {fx}")

    book = value_book(book, quotes, fx)
    book["indices"] = [
        {"name": INDICES[s], "level": f"{q['native']:,.2f}", "change_pct": q["change_pct"]}
        for s, q in index_q.items()
    ]
    book["fx"] = fx
    book["as_of"] = today

    payload = {
        "today": today,
        "book": {
            "starting_capital": book["starting_capital"],
            "cash_gbp": book["cash_gbp"],
            "total_gbp": book["total_gbp"],
            "pl_pct": book["pl_pct"],
            "holdings": [{k: h[k] for k in
                          ("ticker", "shares", "entry_native", "last_native", "value_gbp", "pl_pct", "opened")}
                         for h in book["holdings"]],
        },
        "indices": book["indices"],
        "gbpusd": fx,
        "universe": universe,
        "quotes": {s: {"price": q["native"], "day_change_pct": q["change_pct"],
                       "quoted_in": "pence" if q["pence"] else "USD"}
                   for s, q in quotes.items()},
        "your_previous_reasoning": book["trades"][:12],
        "note": "Prices are last daily closes. LSE tickers are in pence.",
    }

    if DRY_RUN:
        print("\nDRY RUN — payload built, not calling the API.")
        print(json.dumps({k: payload[k] for k in ("today", "book", "indices", "gbpusd")}, indent=2))
        return

    print("Asking Claude…")
    decision = ask_claude(payload)
    print(f"  read: {decision.get('market_read', '')[:200]}")

    filled = apply_trades(book, decision, quotes, fx, today)
    book = value_book(book, quotes, fx)  # re-mark after trading

    for h in book["holdings"]:
        h["sector"] = sector_for(h["ticker"], universe)

    book["session"] += 1
    book["market_read"] = decision.get("market_read", "")
    book["watchlist"] = decision.get("watchlist", [])
    book["universe_requests"] = decision.get("universe_requests", [])

    BOOK.write_text(json.dumps(book, indent=2))
    with HISTORY.open("a") as fh:
        fh.write(json.dumps({
            "date": today, "session": book["session"],
            "total_gbp": book["total_gbp"], "pl_pct": book["pl_pct"],
            "cash_gbp": book["cash_gbp"], "invested_gbp": book["invested_gbp"],
            # Benchmarks, so the curve can answer "did this beat buying the index?"
            "ftse": index_q.get("^FTSE", {}).get("native"),
            "sp500": index_q.get("^GSPC", {}).get("native"),
            "trades": filled, "market_read": book["market_read"],
        }) + "\n")

    print(f"\nSession {book['session']}: £{book['total_gbp']:.2f} "
          f"({book['pl_pct']:+.2f}%), {len(filled)} trades, £{book['cash_gbp']:.2f} cash")


if __name__ == "__main__":
    main()
