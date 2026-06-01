# Market Research Agent

Thesis-driven market research. You write down what you think the market is mispricing; the agent steel-mans and devil's-advocates each thesis, suggests indexes and stocks with potential upside/downside if your thesis is right, enriches every ticker with live price + 52-week range context, builds a balanced hypothetical mock portfolio expressing the thesis, and proposes one or two new theses worth considering.

It is **not** a portfolio tracker — it never sees your positions. Inputs are beliefs about the market; outputs are research notes.

> **Mock portfolios are hypothetical and educational — not financial advice.** Each thesis gets an illustrative ~$5,000 allocation (core / speculative / hedge / optional options overlay) with a scenario matrix and trigger logic, purely as a sizing-and-thinking exercise. Do your own diligence. Change the size by editing `MOCK_PORTFOLIO_USD` in `thesis_research.py`.

## How it works

1. You maintain `theses.md`, a plain Markdown file with one `## Thesis: …` block per belief.
2. The script invokes the Claude Code CLI (`claude -p`) once per thesis with WebSearch enabled, asking for a structured response: steel man, devil's advocate, suggested indexes/stocks, a mock ~$5,000 portfolio, a scenario matrix, and trigger logic.
3. Tickers the model returns are deduplicated and enriched with live price + 52-week high/low via Alpaca (with yfinance fallback).
4. A consolidated Markdown report is saved to `reports/{date}_research.md`.

## Setup

```bash
pip install -r requirements.txt
```

The script needs the Claude Code CLI on `PATH` (or installed in a standard Windows location — it auto-discovers).

Optional but recommended for ticker enrichment, set as user environment variables:

```
ALPACA_API_KEY     # paper / read-only key
ALPACA_API_SECRET
ALPACA_FEED        # "iex" (default, ~15min delayed, free) or "sip"
```

Without Alpaca keys the script falls back to yfinance for everything — slower but works.

## Writing theses

Copy `theses.example.md` to `theses.md` and edit. Format:

```markdown
# Investment Theses

## Strategy Context
(Optional preamble — your investment style, risk tolerance, what kinds of bets
you're looking for. This is threaded into every prompt as context, so the
suggested tickers stay aligned with how you actually invest.)

## Thesis: Short, declarative title here
Free-form body. State what the market believes, what you believe, and why
the gap exists. A few sentences is plenty.

## Thesis: Next thesis title
...
```

Only `## Thesis:` headings are structural. Anything before the first one is treated as the strategy preamble.

## Usage

```bash
python thesis_research.py                          # all theses + new-thesis scan
python thesis_research.py --theses path/to/x.md    # override input file
python thesis_research.py --skip-new-thesis-scan   # only refresh existing
python thesis_research.py --thesis "AI infra"      # filter by title substring
python thesis_research.py --test                   # print, don't save
python thesis_research.py --no-web                 # disable WebSearch (faster, shallower)
```

A typical run takes 5–15 minutes depending on thesis count and web-search depth.

## Weekend-batch mode (recommended for quota-friendly runs)

Running everything at once does one Claude call per thesis back-to-back, which
spikes the Claude Code subscription's rolling 5-hour quota hard. Weekend-batch
mode spreads the work out: each scheduled run researches **one** thesis, saves
the result, and a Monday step assembles the finished report. Same model, same
prompts, same web research — only the *timing* changes.

```bash
python thesis_research.py --research-next   # research the next pending unit, then exit
python thesis_research.py --assemble        # stitch this week's batch into the report
```

How it works:

- `--research-next` creates a **batch manifest** (`reports/partial/batch_{Monday}.json`)
  on its first run of the weekend, snapshotting `theses.md` so mid-weekend edits
  can't corrupt an in-progress batch. Each subsequent run picks the next pending
  thesis (the new-thesis scan runs last), makes one Claude call, and records it.
  A failed call stays pending and is retried by a later run.
- `--assemble` reads the manifest, enriches every ticker in one pass (so all
  prices share one Monday snapshot), writes `reports/{Monday}_research.md`, and
  archives the manifest. Theses that never completed are flagged in the report
  rather than blocking it.

### Scheduling it

Run `setup_schedule.ps1` once to register two Windows Task Scheduler tasks:

```powershell
powershell -ExecutionPolicy Bypass -File setup_schedule.ps1
```

| Task | When | Runs |
|------|------|------|
| `ThesisResearch-Weekend` | Sat 06:00, 09:00, 12:00, 15:00, 18:00, 21:00; Sun 09:00, 12:00 | `run_research_next.bat` |
| `ThesisResearch-Assemble` | Mon 07:00 | `run_assemble.bat` |

Eight weekend slots, ~3 hours apart — any rolling 5-hour quota window holds at
most ~2 calls. Surplus slots no-op cleanly (or retry a failed thesis). **If your
book ever exceeds 8 theses**, add more triggers to `setup_schedule.ps1` and
re-run it (re-running is safe — it replaces the tasks).

Logs land in `logs/weekend.log` and `logs/assemble.log`. Verify the tasks with
`Get-ScheduledTask -TaskName 'ThesisResearch-*'`.

## What's gitignored

`theses.md` and `reports/` are gitignored by default — the example theses file is the only thing in the repo. Your real beliefs and generated research stay local.
