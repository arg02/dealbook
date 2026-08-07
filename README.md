# The Dealing Book

A £1,000 paper portfolio that trades itself. A GitHub Actions job runs each weekday
evening, fetches real prices, asks Claude what to do, applies the trades and commits the
result. The dashboard is a static page on GitHub Pages.

Simulated money. No broker is connected and none can be.

---

## Setup

**1. Create the repo**

Push these files to a new **public** repo (public gets unlimited free Actions minutes;
private is capped at 2,000/month, which is still plenty).

**2. Add your API key**

Settings → Secrets and variables → Actions → New repository secret:

| Name | Value |
|---|---|
| `ANTHROPIC_API_KEY` | your key from console.anthropic.com |

Optionally add a repository *variable* `CLAUDE_MODEL` to override the default
(`claude-sonnet-5`). `claude-opus-5` reasons harder and costs more.

**3. Turn on Pages**

Settings → Pages → Source: **Deploy from a branch** → Branch `main`, folder **`/docs`**.

Your dashboard lands at `https://<you>.github.io/<repo>/`.

**4. Let the workflow write**

Settings → Actions → General → Workflow permissions → **Read and write permissions**.
Without this the job can fetch and decide but can't commit the result.

**5. Test it**

Actions → Trading session → Run workflow → tick **dry run**. That fetches prices and
prints what it found without calling the model or trading. If quotes come back, untick
dry run and run it for real. That opens the book.

---

## How it works

```
21:30 UTC weekdays
  │
  ├─ yfinance ──► prices for the universe, 8 indices, GBP/USD
  ├─ mark existing holdings to market
  ├─ POST api.anthropic.com  (with web search on, so it sees today's news)
  ├─ parse trades, validate against cash and holdings, fill at last close
  └─ commit docs/book.json + append docs/history.jsonl
```

Every session is a commit. `git log docs/book.json` is a tamper-evident record of what
was known and what was decided, which matters more here than the returns do.

## Files

| Path | What it is |
|---|---|
| `scripts/session.py` | Fetch, decide, execute, write |
| `scripts/universe.json` | What it's allowed to buy — edit to widen the mandate |
| `.github/workflows/session.yml` | The schedule |
| `docs/index.html` | Dashboard |
| `docs/book.json` | Current state — positions, cash, trade log |
| `docs/history.jsonl` | One line per session, for charting later |

## The mandate

Set in `SYSTEM` at the top of `session.py`. Currently: total return over roughly a month,
sector-agnostic, cash is a legitimate position, every trade needs a written argument
**and a stated falsifier**. It's explicitly told not to default to technology.

It can only buy tickers in `universe.json`. If it wants something else it has to ask, and
the request shows up in `book.json` under `universe_requests` for you to approve. This is
deliberate — an unconstrained model will occasionally invent a plausible-looking ticker.

## Things that will bite you

- **Scheduled Actions drift.** GitHub queues them best-effort; 5–20 minutes late is normal,
  worse on the hour. Irrelevant at daily cadence.
- **Scheduled workflows auto-disable after 60 days of repo inactivity.** The job's own
  commits count as activity, so a running book stays alive.
- **yfinance is unofficial** and occasionally rate-limits or changes shape. If quotes start
  coming back empty, that's the usual cause. Swapping in Alpha Vantage or Finnhub means
  rewriting `fetch_quotes` and nothing else.
- **Fills are last daily close**, not live. Slippage isn't modelled, nor is commission or
  the 0.15–0.5% FX spread a real platform charges. Returns are flattered slightly.
- **LSE tickers quote in pence.** Handled in `to_gbp()`. Watch it if you add new ones.

## Cost

A session is roughly 15–25k input tokens plus a few searches. On Sonnet that's pennies —
call it £1–3 for a month of weekday runs. Actions minutes are free on a public repo.

---

Not investment advice. Not a model portfolio. A record of one model's reasoning,
kept honestly, including when it's wrong.
