# Market Research Agent

Thesis-driven market research. You write down what you think the market is mispricing; the agent steel-mans and devil's-advocates each thesis, suggests indexes and stocks with potential upside/downside if your thesis is right, enriches every ticker with live price + 52-week range context, builds a balanced hypothetical mock portfolio expressing the thesis, and proposes one or two new theses worth considering.

It runs weekly, tracks how your conviction in each thesis evolves, and backtests its own hypothetical mock portfolios over time.

It is **not** a portfolio tracker — it never sees your real positions. Inputs are beliefs about the market; outputs are research notes.

> **Mock portfolios are hypothetical and educational — not financial advice.** Each thesis gets an illustrative ~$5,000 allocation (core / speculative / hedge / optional options overlay) with a scenario matrix and trigger logic, purely as a sizing-and-thinking exercise. Do your own diligence. Change the size by editing `MOCK_PORTFOLIO_USD` in `thesis_research.py`.

## How it works

1. You maintain `theses.md`, a plain Markdown file with one `## Thesis: …` block per belief.
2. For each thesis the script makes **two** Claude Code CLI (`claude -p`) calls. The first **analysis** pass (WebSearch on) returns the steel man, devil's advocate, and suggested indexes/stocks. Those tickers are then priced (step 3) and fed into a second **portfolio** pass that sizes a mock ~$5,000 portfolio against the *live* prices — with real per-share prices and share counts — plus a scenario matrix and trigger logic.
3. Tickers the model returns are deduplicated and enriched with live price + 52-week high/low via Alpaca (with yfinance fallback).
4. A consolidated Markdown report is saved to `reports/{date}_research.md`.

> Why two passes: enrichment can only happen *after* the model names its tickers, so a single call would have to size the portfolio without knowing any prices. The second pass closes that gap so dollar allocations and share counts are grounded in real quotes.

## Week-over-week tracking & backtest ledger

Every report also writes a structured **history sidecar** next to it
(`reports/{date}_research.json`) capturing, per thesis: a conviction score (1–5) +
direction, the catalyst dates flagged, the mock-portfolio holdings with entry prices,
the realized return of the *prior* week's portfolio, and a running equity index. This
turns each weekly report from a standalone essay into a continuous record:

- **Continuity.** When a prior sidecar exists for a thesis, last week's take is threaded
  into the analysis prompt, so the report opens with a **"Since last week"** section —
  what changed, whether the catalysts you flagged actually fired, and whether price
  action confirmed or contradicted the thesis — plus a conviction line showing the move
  (e.g. `Conviction: 3/5 (↓ from 4 last week)`).
- **Performance.** A per-thesis **Portfolio performance** block marks last week's mock
  portfolio to current prices (week-over-week return) and tracks a since-inception equity
  curve — both shown against SPY over the same period, so "+2%" reads differently in a
  +5% tape than a -3% one. Options overlays and any ticker with no current price are
  excluded and noted. (Change the benchmark by editing `BENCHMARK_TICKER`.)
- **Calibration.** A book-level **Conviction Calibration & Ledger** section summarizes
  every thesis and, once enough history accrues, reports whether higher-conviction weeks
  were actually followed by better subsequent returns.
- **Catalyst calendar.** Each report opens with a forward-looking timeline aggregating
  every thesis's flagged catalysts — what fires next, when, and for which thesis.
- **Catalyst resolutions.** When a flagged catalyst's date passes, the next analysis pass
  must return a structured verdict (`for | against | mixed | pending`) on what actually
  happened. Verdicts surface as a per-thesis scorecard line and accrue into a book-level
  hit-rate ("how often do the catalysts I flag break my way") — an event-level
  calibration that's far less noisy than weekly price returns.
- **Scan accountability.** Every idea the weekly new-thesis scan proposes is recorded in
  the sidecar with its suggested tickers priced at suggestion time. Past suggestions are
  fed back into the scan prompt (so it stops re-pitching ideas you passed on), and each
  suggestion's equal-weight ticker basket is marked to market weekly in a **scan track
  record** table — over time you learn how seriously to take the scanner.

Theses are matched across weeks by their normalized title, so **substantially renaming a
thesis resets its history** (it's treated as new). This adds no extra Claude calls — the
continuity folds into the existing analysis pass; performance and calibration are computed
mechanically. Ad-hoc runs (`--test`, or `--thesis` filters) read prior history but don't
overwrite the sidecar.

## Setup

```bash
pip install -r requirements.txt
```

The script needs the Claude Code CLI on `PATH` (or installed in a standard Windows location — it auto-discovers).

Optional but recommended for ticker enrichment: copy `.env.example` to `.env` and fill in your Alpaca keys (free paper / read-only keys work):

```
ALPACA_API_KEY     # paper / read-only key
ALPACA_API_SECRET
ALPACA_FEED        # "iex" (default, ~15min delayed, free) or "sip"
```

The script loads `.env` itself, so scheduled Task Scheduler runs see the keys too (they don't inherit shell exports — plain user environment variables also work). Values exported in your shell override the file. Without Alpaca keys the script logs a notice and falls back to yfinance for everything — slower but works; transient yfinance failures are retried with backoff.

### Obsidian delivery (optional)

Set `OBSIDIAN_VAULT_DIR` in `.env` to your vault's absolute path and every saved report is also written to `<vault>/claude reports/market research agent/{date}_research.md` with minimal frontmatter (`date`, `tags: [market-research]`). It's a plain file write — Obsidian indexes disk changes itself, so it works unattended with no plugins or API. Delivery is best-effort: a missing vault (e.g. OneDrive offline) logs a warning and never fails the run; `reports/` remains the canonical copy.

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

### Thesis identity (optional)

A heading may be followed by a yaml control block. It is entirely optional —
without one, a thesis is identified by its title, exactly as before.

```yaml
id: iran-energy-shock     # stable identity; history is keyed on this, not the title
aliases: []               # keys this thesis was tracked under before a rename
status: active            # active | watch | retired
version: 1                # bump when you amend the framing
```

The point is to let you **rename or reframe a thesis without losing its track
record**. History (continuity, the equity index, conviction calibration) resolves
through `id` plus `aliases` rather than the title slug, so when you rewrite a
thesis, move its old title slug into `aliases` and every past week folds into the
same timeline instead of orphaning a frozen row in the ledger.

A trailing `### Amendments` list documents why the framing changed:

```markdown
### Amendments
- 2026-08-31 (v3): Narrowed from "shift to renewables" to the cost pass-through
  into refrigeration and industrial HVAC — renewables didn't respond, the
  adjacency did.
```

**Bumping `version` is what makes a reframe a first-class event.** When the
version is higher than the one in last week's history, the run treats the thesis
as amended and:

- tells the analyst its prior take was written under the old framing, to assess
  the thesis as it now stands rather than relitigating what you dropped, and to
  say what the reframe means for the positions it was holding;
- prints an `⚑ Amended (v1 → v2)` banner above the conviction line, with your
  amendment text, so the change reads as part of the story;
- tags the conviction delta as "under an amended framing", since the two numbers
  aren't measuring quite the same claim;
- keeps the equity index and inception date running, but stamps `version_started`
  so performance can be read per-framing as well as since inception.

Records written before this schema existed have no version and read as v1, so an
untouched thesis never shows a phantom banner.

Both the control block and the amendments list are stripped from the body before
it reaches the model, so only your actual framing is analyzed.

### Lifecycle: `active`, `watch`, `retired`

`status` decides how much work a thesis gets — so keeping an eye on a played-out
idea, or closing one properly, stops being all-or-nothing.

| status | Claude calls | Mock portfolio | In the report |
|--------|--------------|----------------|---------------|
| `active` | 2 (analysis + portfolio) | rebuilt and re-sized weekly | full section |
| `watch` | 1 (status check) | carried and marked, never re-sized | short section with a **Watch** badge |
| `retired` | 0 | — | one row in **Closed theses** |

**`watch`** is for a thesis whose main mispricing you think is played out but
whose story you still want followed. It gets a short "what changed / does this
still deserve watching" pass, suggests no new tickers, and is explicitly told
that recommending retirement is a useful answer. Its book keeps being marked to
market so the track record continues.

> Entry prices on a carried book are re-based to the current price every week. A
> watched thesis is never re-sized, so without that its weekly return would be
> measured from the original entry each time and the same move would compound
> into the equity index week after week.

**`retired`** costs nothing — no prompt, no prices, no new history. Add
`retired_on` and `retired_note` and the thesis leaves the live ledger for a
**Closed theses** table carrying its final since-inception and vs-benchmark
numbers plus your one-line reason. Its past catalyst verdicts stay in the record
and still count toward conviction calibration. The new-thesis scan is told what
you retired and why, and may only re-propose it by saying what has changed since.

```yaml
id: saas-reacceleration
status: watch
mode: residual
spent: [SNOW, DDOG]       # names whose mispricing you consider closed
```

### Residual mode

`mode: residual` is for the other half of a played-out thesis: you still believe
the mechanism, but you think the obvious trade is done and what's left is
wherever the effect hasn't finished propagating.

It rewrites the analysis pass to hunt second-order and adjacent exposure —
suppliers, customers, input costs, competitors forced to respond, sectors that
inherit the effect late — and to explain, for each name, *why it hasn't moved
yet*. It's told that a short list of genuinely un-re-rated names beats a padded
one, and that "the residual is empty" is an acceptable answer. The rest of the
pass (steel man, devil's advocate, tickers, portfolio, conviction) is unchanged,
and the section carries a **Residual** badge.

`spent` lists names whose re-rating you consider finished. They're named in the
prompt as off-limits, but this stays guidance rather than a filter — the analyst
may have a real reason to raise one again, and silently stripping it would
desync the analysis text from the portfolio pass that reads it. A reappearance
is logged instead:

```
[21:27:47]   Note: SNOW suggested again despite being listed spent — see the Notes for the stated reason.
```

Spent names already held in the mock portfolio keep being marked — `spent`
governs new suggestions, not existing positions.

`mode` and `status` are independent. `active` + `residual` is the usual pairing:
still worth a full weekly work-up, just pointed at the residual rather than the
original trade. On a `watch` thesis the mode is moot, since it suggests no names
at all.

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
mode spreads the work out: each scheduled run researches **one** thesis and saves
the result. The slot that finishes the **last** unit (the new-thesis scan always
runs last) assembles the finished report on the spot — so a small book is done the
same evening rather than waiting for Monday. A Monday step remains as a fallback
for batches a weekend slot never managed to complete. Same model, same prompts,
same web research — only the *timing* changes.

```bash
python thesis_research.py --research-next   # research the next pending unit, then exit
python thesis_research.py --assemble        # stitch this week's batch into the report
```

How it works:

- `--research-next` creates a **batch manifest** (`reports/partial/batch_{Monday}.json`)
  on its first run of the weekend, snapshotting `theses.md` so mid-weekend edits
  can't corrupt an in-progress batch. Each subsequent run picks the next pending
  thesis (the new-thesis scan runs last) and fully finishes it — a web research
  pass plus a price-grounded portfolio pass — recording both. A failed call leaves
  the unit pending and a later run retries it from the top. **When that run
  finishes the last unit it assembles the report automatically** — no need to wait
  for Monday. Once the report exists, surplus weekend slots see it and no-op.
- `--assemble` (run automatically by the finishing slot, and again by the Monday
  fallback task) reads the manifest, enriches every ticker in one pass (so all
  prices share one snapshot), writes `reports/{Monday}_research.md`, and archives
  the manifest. Theses that never completed are flagged in the report rather than
  blocking it. If the report is already assembled it's a clean no-op.

### Scheduling it

Run `setup_schedule.ps1` once to register two Windows Task Scheduler tasks:

```powershell
powershell -ExecutionPolicy Bypass -File setup_schedule.ps1
```

| Task | When | Runs |
|------|------|------|
| `ThesisResearch-Weekend` | Every 3h on Sat **and** Sun, 00:00–21:00 (16 slots) | `run_research_next.bat` |
| `ThesisResearch-Assemble` | Mon 07:00 (fallback) | `run_assemble.bat` |

The report normally assembles itself the moment the last weekend slot finishes;
the Monday task only does work if no weekend slot completed the batch (e.g. the
machine was asleep for the finishing slot).

Sixteen weekend slots, 3 hours apart — a full day's worth on **each** of Saturday
and Sunday. Any rolling 5-hour quota window touches at most ~2 theses; each thesis
makes two Claude calls (research + portfolio; the portfolio pass runs with
WebSearch off, so it's the cheaper of the two), so budget ~4 calls per window.
Because the batch auto-assembles once the last unit finishes, every slot after
that no-ops — so the dense schedule costs nothing once the report is out. The
duplicated second day is deliberate: if the machine is off for all of Saturday,
Sunday alone still has 8 slots, enough to research every thesis plus the new-thesis
scan. **If your book ever exceeds ~7 theses** (8 units incl. the scan, the per-day
slot count), add more triggers to `setup_schedule.ps1` and re-run it (re-running
is safe — it replaces the tasks).

Logs land in `logs/weekend.log` and `logs/assemble.log`. Verify the tasks with
`Get-ScheduledTask -TaskName 'ThesisResearch-*'`.

## Tests

```bash
python -m pytest tests/ -q
```

Covers the parsing/math core (ticker extraction, portfolio-table parsing, weekly-return and benchmark-index math, `.env` loading) with no network or Claude calls.

## Privacy: what's gitignored (and the tripwire)

`theses*.md` (except the example), `reports/`, `logs/`, and `.env*` (except the template) are gitignored — the example theses file is the only thesis content in the repo. Your real beliefs, generated research, and the history sidecars (`reports/*_research.json`) all stay local.

As a backstop against forced adds or renamed copies slipping through, `hooks/pre-commit` blocks any commit that stages those private paths. Enable it once per clone:

```bash
git config core.hooksPath hooks
```
