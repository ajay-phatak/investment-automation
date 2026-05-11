# Market Research Agent

Thesis-driven market research. You write down what you think the market is mispricing; the agent steel-mans and devil's-advocates each thesis, suggests indexes and stocks with potential upside/downside if your thesis is right, enriches every ticker with live price + 52-week range context, and proposes one or two new theses worth considering.

It is **not** a portfolio tracker — it never sees your positions. Inputs are beliefs about the market; outputs are research notes you can act on.

## How it works

1. You maintain `theses.md`, a plain Markdown file with one `## Thesis: …` block per belief.
2. The script invokes the Claude Code CLI (`claude -p`) once per thesis with WebSearch enabled, asking for a structured response.
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

## What's gitignored

`theses.md` and `reports/` are gitignored by default — the example theses file is the only thing in the repo. Your real beliefs and generated research stay local.
