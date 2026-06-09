#!/usr/bin/env python3
"""
Thesis-Based Market Research Agent

Reads a Markdown file of investment theses and, for each one, produces a
steel-man / devil's-advocate analysis plus suggested indexes and stocks with
upside/downside if the thesis is right. Suggested tickers are enriched with
live price + 52-week range so you can see where each name sits relative to
its own history.

Drives Claude via the local Claude Code CLI (`claude -p`) with WebSearch
enabled, so research is grounded in current information rather than the
model's training cutoff. No Anthropic API credits used — billed against your
Claude Code subscription.

Designed to be safely public: never reads or references portfolio positions.
Inputs are beliefs; outputs are research notes.

Usage:
    python thesis_research.py                          # all theses + new-thesis scan
    python thesis_research.py --theses path/to/x.md    # override input path
    python thesis_research.py --skip-new-thesis-scan   # only refresh existing
    python thesis_research.py --thesis "AI infra"      # filter by title substring
    python thesis_research.py --test                   # print, don't save
    python thesis_research.py --no-web                 # disable WebSearch

Weekend-batch mode (spreads the Claude calls across many scheduled runs so a
single 5-hour quota window is never spiked):
    python thesis_research.py --research-next          # research ONE pending unit
    python thesis_research.py --assemble               # stitch the batch into a report
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml
import yfinance as yf

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# ── Config ──────────────────────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).parent
DEFAULT_THESES_FILE = SCRIPT_DIR / "theses.md"
EXAMPLE_THESES_FILE = SCRIPT_DIR / "theses.example.md"
REPORTS_DIR = SCRIPT_DIR / "reports"
# Weekend-batch state: one manifest per upcoming-Monday batch lives here.
PARTIAL_DIR = REPORTS_DIR / "partial"
ARCHIVE_DIR = PARTIAL_DIR / "archive"
# A unit that keeps failing is dropped after this many attempts so weekend
# slots don't burn forever retrying a genuinely broken thesis.
MAX_ATTEMPTS = 3
ET = ZoneInfo("America/New_York")

ALPACA_KEY = os.environ.get("ALPACA_API_KEY")
ALPACA_SECRET = os.environ.get("ALPACA_API_SECRET")
ALPACA_FEED = os.environ.get("ALPACA_FEED", "iex").lower()

CLAUDE_MODEL = "claude-fable-5"
# Web research is materially slower than the price-only brief — give it room.
CLAUDE_TIMEOUT_SEC = 900

# Each thesis gets a hypothetical, educational mock portfolio of this size.
# One-line edit to change it — applies to both the all-at-once and weekend runs.
MOCK_PORTFOLIO_USD = 5000


# ── Claude Code CLI plumbing ────────────────────────────────────────────────

def find_claude_exe() -> str:
    """Locate the Claude Code CLI. Try PATH first, then scan known Windows
    install dirs for the highest-versioned claude.exe so updates don't break."""
    on_path = shutil.which("claude")
    if on_path:
        return on_path
    candidates = [
        Path(os.environ.get("LOCALAPPDATA", "")) / "Packages" / "Claude_pzs8sxrjxfjjc" / "LocalCache" / "Roaming" / "Claude" / "claude-code",
        Path(os.environ.get("APPDATA", "")) / "Claude" / "claude-code",
    ]
    for base in candidates:
        if not base.exists():
            continue
        versions = sorted(
            (p for p in base.iterdir() if p.is_dir() and (p / "claude.exe").exists()),
            key=lambda p: tuple(int(x) for x in p.name.split(".") if x.isdigit()),
            reverse=True,
        )
        if versions:
            return str(versions[0] / "claude.exe")
    raise RuntimeError("Could not find claude.exe. Install Claude Code or add it to PATH.")


def call_claude(prompt: str, allow_web: bool = True) -> str:
    """Run prompt through `claude -p`. Prompt goes via stdin to dodge the
    8191-char Windows command-line limit.

    Strips ANTHROPIC_API_KEY from just the subprocess env so the CLI falls
    back to Claude Code subscription auth (no API credits used). The parent
    shell keeps the key for whatever else needs it."""
    exe = find_claude_exe()
    cmd = [exe, "-p", "--model", CLAUDE_MODEL]
    if allow_web:
        cmd += ["--allowedTools", "WebSearch,WebFetch"]
    child_env = os.environ.copy()
    child_env.pop("ANTHROPIC_API_KEY", None)
    result = subprocess.run(
        cmd,
        input=prompt,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=CLAUDE_TIMEOUT_SEC,
        env=child_env,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"claude -p exited {result.returncode}\n"
            f"exe: {exe}\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )
    return result.stdout.strip()


# ── Theses parsing ──────────────────────────────────────────────────────────

THESIS_HEADING_RE = re.compile(r"^##\s*Thesis:\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE)


def parse_theses(path: Path) -> tuple[str, list[dict]]:
    """Parse a theses Markdown file into (strategy_preamble, [{title, body}]).
    Anything before the first `## Thesis:` heading is the preamble."""
    text = path.read_text(encoding="utf-8")
    matches = list(THESIS_HEADING_RE.finditer(text))
    if not matches:
        raise ValueError(
            f"No '## Thesis:' headings found in {path}. "
            "See theses.example.md for the expected format."
        )
    preamble = text[: matches[0].start()].strip()
    theses = []
    for i, m in enumerate(matches):
        title = m.group(1).strip()
        body_start = m.end()
        body_end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[body_start:body_end].strip()
        theses.append({"title": title, "body": body})
    return preamble, theses


# ── Price enrichment ────────────────────────────────────────────────────────

def _fetch_alpaca_52w(tickers: list[str]) -> dict:
    """Pull ~1Y of daily bars from Alpaca for all tickers in one batched
    request. Returns {ticker: {price, low_52w, high_52w, source}} for tickers
    that came back with data."""
    out: dict[str, dict] = {}
    if not (ALPACA_KEY and ALPACA_SECRET):
        return out
    try:
        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame
        from alpaca.data.enums import DataFeed
    except ImportError:
        print("alpaca-py not installed — skipping Alpaca, using yfinance for everything")
        return out

    client = StockHistoricalDataClient(ALPACA_KEY, ALPACA_SECRET)
    end = datetime.now(ET)
    # 380 calendar days buffers around weekends/holidays to guarantee 252 trading days.
    start = end - timedelta(days=380)
    feed = DataFeed.SIP if ALPACA_FEED == "sip" else DataFeed.IEX
    try:
        request = StockBarsRequest(
            symbol_or_symbols=tickers,
            timeframe=TimeFrame.Day,
            start=start,
            end=end,
            feed=feed,
        )
        bars = client.get_stock_bars(request)
    except Exception as e:
        print(f"Alpaca fetch failed: {e} — falling back to yfinance for all tickers")
        return out

    for symbol in tickers:
        bar_list = bars.data.get(symbol, [])
        if not bar_list:
            continue
        # Trim to last 252 trading days for a true rolling 52-week window.
        recent = bar_list[-252:]
        price = recent[-1].close
        high_52w = max(b.high for b in recent)
        low_52w = min(b.low for b in recent)
        out[symbol] = {
            "price": round(price, 4),
            "low_52w": round(low_52w, 4),
            "high_52w": round(high_52w, 4),
            "source": f"alpaca-{ALPACA_FEED}",
        }
    return out


def _fetch_yfinance_52w(tickers: list[str]) -> dict:
    """yfinance fallback. Slower (one request per ticker) but covers OTC,
    foreign, and anything Alpaca rejects."""
    out: dict[str, dict] = {}
    for ticker in tickers:
        try:
            hist = yf.Ticker(ticker).history(period="1y")
            if hist.empty:
                out[ticker] = {"error": "no data (delisted, OTC, or invalid?)"}
                continue
            price = hist["Close"].iloc[-1]
            high_52w = hist["High"].max()
            low_52w = hist["Low"].min()
            out[ticker] = {
                "price": round(float(price), 4),
                "low_52w": round(float(low_52w), 4),
                "high_52w": round(float(high_52w), 4),
                "source": "yfinance",
            }
        except Exception as e:
            out[ticker] = {"error": str(e)}
    return out


def enrich_tickers(tickers: list[str]) -> dict:
    """Enrich each ticker with live price + 52-week high/low. Alpaca primary,
    yfinance fallback for anything Alpaca didn't return."""
    if not tickers:
        return {}
    unique = sorted(set(t.strip().upper() for t in tickers if t and t.strip()))
    print(f"  Enriching {len(unique)} tickers: {', '.join(unique)}")
    data = _fetch_alpaca_52w(unique)
    missing = [t for t in unique if t not in data]
    if missing:
        print(f"  Falling back to yfinance for: {', '.join(missing)}")
        data.update(_fetch_yfinance_52w(missing))
    return data


def position_pct(price: float, low: float, high: float) -> str:
    """Where in the 52w range does the current price sit? 0% = at low, 100% = at high."""
    if high <= low:
        return "n/a"
    pct = (price - low) / (high - low) * 100
    return f"{pct:.0f}%"


def _is_priced(info: dict | None) -> bool:
    """True only if enrichment carries a usable, finite price. Filters out error
    entries and NaN prices (yfinance occasionally returns NaN for some listings)."""
    if not info or "error" in info:
        return False
    price = info.get("price")
    return isinstance(price, (int, float)) and price == price  # NaN != NaN


# ── Ticker extraction & rendering ───────────────────────────────────────────

YAML_BLOCK_RE = re.compile(r"```yaml\s*\n(.*?)\n```", re.DOTALL)


def extract_tickers_from_response(response: str) -> list[str]:
    """Pull every ticker symbol out of every yaml block in the response.
    Tolerates dict-of-lists ({upside: [...], downside: [...]}), flat lists,
    and the new-thesis 'tickers_to_research' shape."""
    found: list[str] = []
    for m in YAML_BLOCK_RE.finditer(response):
        try:
            data = yaml.safe_load(m.group(1))
        except yaml.YAMLError:
            continue
        for ticker in _walk_strings(data):
            t = ticker.strip().upper()
            if t and re.match(r"^[A-Z][A-Z0-9.\-]{0,9}$", t):
                found.append(t)
    return found


def _walk_strings(node):
    """Yield every string leaf in an arbitrary nested dict/list structure."""
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for v in node.values():
            yield from _walk_strings(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk_strings(v)


def render_ticker_table(yaml_text: str, enrichment: dict) -> str:
    """Replace a yaml block (already parsed shape) with a markdown table.
    Handles the {upside, downside} shape and the {tickers_to_research} shape."""
    try:
        data = yaml.safe_load(yaml_text)
    except yaml.YAMLError:
        return f"```yaml\n{yaml_text}\n```"

    rows: list[tuple[str, str]] = []  # (side_label, ticker)
    if isinstance(data, dict):
        for key, val in data.items():
            label = str(key)
            if isinstance(val, list):
                for t in val:
                    if isinstance(t, str):
                        rows.append((label, t.strip().upper()))
            elif isinstance(val, str):
                rows.append((label, val.strip().upper()))
    elif isinstance(data, list):
        for t in data:
            if isinstance(t, str):
                rows.append(("", t.strip().upper()))

    if not rows:
        return f"```yaml\n{yaml_text}\n```"

    lines = [
        "| Ticker | Side | Price | 52w Low | 52w High | % of Range |",
        "|--------|------|-------|---------|----------|------------|",
    ]
    for side, ticker in rows:
        info = enrichment.get(ticker)
        if not _is_priced(info):
            err = (info or {}).get("error", "no data")
            lines.append(f"| {ticker} | {side} | _{err}_ | — | — | — |")
            continue
        lines.append(
            f"| {ticker} | {side} | ${info['price']:.2f} | "
            f"${info['low_52w']:.2f} | ${info['high_52w']:.2f} | "
            f"{position_pct(info['price'], info['low_52w'], info['high_52w'])} |"
        )
    return "\n".join(lines)


def replace_yaml_blocks_with_tables(response: str, enrichment: dict) -> str:
    """Walk every ```yaml block in the response and swap it for a rendered table."""
    def _sub(match: re.Match) -> str:
        return render_ticker_table(match.group(1), enrichment)
    return YAML_BLOCK_RE.sub(_sub, response)


def format_enrichment_table(enrichment: dict) -> str:
    """Render enriched tickers into a compact Markdown price table for feeding into
    the portfolio (pass-2) prompt. Skips tickers that errored or have no data."""
    rows = [
        f"| {t} | ${info['price']:.2f} | ${info['low_52w']:.2f} | ${info['high_52w']:.2f} | "
        f"{position_pct(info['price'], info['low_52w'], info['high_52w'])} |"
        for t, info in sorted(enrichment.items())
        if _is_priced(info)
    ]
    if not rows:
        return "(no live prices available)"
    header = [
        "| Ticker | Price | 52w Low | 52w High | % of Range |",
        "|--------|-------|---------|----------|------------|",
    ]
    return "\n".join(header + rows)


# ── Prompt builders ─────────────────────────────────────────────────────────

def build_analysis_prompt(strategy_preamble: str, thesis: dict, allow_web: bool,
                          prior_context: str | None = None) -> str:
    today = datetime.now(ET).strftime("%A, %B %d, %Y")
    web_instruction = (
        "Use WebSearch and WebFetch aggressively to ground your analysis in current "
        "news, filings, analyst notes, and price action from the last few weeks. "
        "Prefer primary sources (company filings, central bank statements, regulatory "
        "announcements) over commentary."
        if allow_web
        else "WebSearch is disabled for this run — work from your training knowledge "
             "and acknowledge any areas where current information would change the picture."
    )
    continuity = f"\n\n{prior_context}\n" if prior_context else ""
    since_section = (
        "### Since last week\n"
        "3-6 bullets on what has changed since your prior take: did the catalysts you "
        "flagged resolve (and how), did price action confirm or contradict the thesis, and "
        "what is the net effect on conviction. Be specific — cite dates and numbers.\n\n"
        if prior_context else ""
    )
    direction_values = "up | down | flat (versus your prior conviction)" if prior_context else "n/a"

    return f"""You are an investment research analyst helping a sophisticated retail investor stress-test one of their investment theses. Today is {today}.

INVESTOR'S STRATEGY CONTEXT (use this to keep ticker suggestions aligned with how they actually invest):
{strategy_preamble or "(no strategy preamble provided)"}

THE THESIS UNDER REVIEW:
## Thesis: {thesis['title']}

{thesis['body']}{continuity}

YOUR JOB:
{web_instruction}

Produce a critical analysis of this thesis. Bear in mind: by definition, a thesis like this is one where the investor disagrees with the market, so you SHOULD expect to find more weight of evidence on the "against" side — that's fine, the goal is to give them ammunition for both sides so they can re-examine their conviction.

OUTPUT FORMAT — follow exactly. Do not add a top-level header; the script wraps your output.

{since_section}### Steel man (best arguments FOR the thesis)
3-6 bullets. Be concrete and specific — cite numbers, names, dates, recent events. Skip generic platitudes.

### Devil's advocate (best arguments AGAINST the thesis)
3-6 bullets. Same standard. The strongest counter-arguments, not the easiest ones.

### Indexes
ETFs / index products that would have meaningful upside or downside if the thesis plays out. Keep it tight: 2-5 total. Use the `upside` and `downside` keys.

```yaml
upside: [TICKER1, TICKER2]
downside: [TICKER3]
```

### Stocks
Single names with concentrated exposure to the thesis outcome. Prefer ones with clear catalyst geometry (earnings dates, regulatory milestones). 3-8 total.

```yaml
upside: [TICKER4, TICKER5]
downside: [TICKER6]
```

### Notes
One short line per ticker you suggested above, explaining why it's on the list. Format: `**TICKER**: rationale.` Keep each line under 25 words.

Finally, end your output with a SINGLE fenced yaml metadata block — no heading, nothing after it. The script parses this, so keep it machine-clean:

```yaml
conviction: 3            # integer 1 (weak / would barely hold) to 5 (high conviction)
direction: {direction_values}
conviction_note: "one short line on your conviction and what moved it"
catalysts:
  - {{date: 2026-08-15, what: "what happens on/around then"}}
```

CRITICAL FORMATTING RULES:
- The Indexes/Stocks yaml blocks must use plain ASCII tickers (e.g. NVDA, SMH, TLRY). For non-US listings, use the full Yahoo-Finance-style symbol (e.g. TLRY.TO, SHOP.TO, ASML.AS).
- Do not put commentary inside the yaml blocks — only the ticker lists.
- If a side is empty (e.g. no clear downside indexes), use an empty list `[]`, do not omit the key.
- Only the Indexes/Stocks lists and the final metadata block use yaml. Do NOT output a mock portfolio, scenario matrix, or trigger logic in this pass — those are built in a second pass once live prices are available.
- The metadata block must have an integer `conviction` and ISO-format (YYYY-MM-DD) `catalysts` dates.
"""


def build_portfolio_prompt(strategy_preamble: str, thesis: dict, analysis_text: str, price_table: str) -> str:
    today = datetime.now(ET).strftime("%A, %B %d, %Y")
    return f"""You are an investment research analyst constructing a hypothetical, educational mock portfolio for one of a sophisticated retail investor's theses. Today is {today}. This is the SECOND pass: the analysis and candidate tickers already exist, and you now have LIVE prices to size positions accurately.

INVESTOR'S STRATEGY CONTEXT:
{strategy_preamble or "(no strategy preamble provided)"}

THE THESIS:
## Thesis: {thesis['title']}

{thesis['body']}

YOUR PRIOR ANALYSIS OF THIS THESIS (steel man, devil's advocate, suggested indexes/stocks, notes):
{analysis_text}

LIVE PRICES for the tickers surfaced above — these are real, fetched just now:
{price_table}

YOUR JOB: produce ONLY the three sections below. Do not repeat the analysis. Build everything from the tickers in the LIVE PRICES table — do not introduce tickers that aren't priced there.

### Mock Portfolio (~${MOCK_PORTFOLIO_USD:,} hypothetical — educational, not financial advice)
Construct ONE balanced hypothetical ${MOCK_PORTFOLIO_USD:,} allocation that expresses this thesis, sized against the live prices above. Present it as a **Markdown table** with columns: `Ticker | Role | Shares | Price | Allocation | Weight | Rationale`.
- Compute `Shares = floor(Allocation / Price)` and set `Allocation = Shares × Price` (a small uninvested cash remainder is expected and fine).
- End with a **Total** row that sums the actual Allocation column to ≈ ${MOCK_PORTFOLIO_USD:,}, and note any leftover cash.
- `Role` is one of: `core`, `speculative`, `hedge`, `overlay`.
  - `core` = liquid, lower-variance expression of the thesis (the anchor).
  - `speculative` = concentrated, catalyst-direct, higher-variance satellite.
  - `hedge` = offsets a specific risk in the book.
  - `overlay` = an OPTIONAL small options sleeve (e.g. calls/puts on a core name) — at most one, kept modest. An options overlay has no live share price; size it by dollar premium, put "—" in the Shares/Price columns, and note the premium is an estimate.
- Aim for genuine balance: a core anchor, one or two speculative satellites, and an optional small overlay — sized to each leg's conviction and risk, not split evenly by default.
- If a ticker from your analysis has no row in the LIVE PRICES table, do not use it; if that leaves a gap, say so in one line under the table.

### Scenario Matrix
A **Markdown table** with columns: `Scenario | What it looks like | Portfolio impact`. Cover at least three rows: the thesis plays out, a partial / delayed outcome, and the thesis failing or being invalidated.

### Trigger Logic
The key catalyst date(s) and the specific conditions that would make you ADD, TRIM, or EXIT each leg. Be concrete about dates and price/event thresholds, using the live prices above as reference levels where useful.

CRITICAL: The Mock Portfolio and Scenario Matrix MUST be Markdown tables, never yaml.
"""


def build_new_thesis_prompt(strategy_preamble: str, existing_titles: list[str], allow_web: bool) -> str:
    today = datetime.now(ET).strftime("%A, %B %d, %Y")
    existing = "\n".join(f"- {t}" for t in existing_titles) or "(none)"
    web_instruction = (
        "Use WebSearch to scan recent news, macro data releases, central bank "
        "communications, regulatory developments, earnings reactions, and price action "
        "across major sectors. Look for places where the market's reaction (or lack of "
        "reaction) implies a probability distribution that you'd argue with."
        if allow_web
        else "WebSearch is disabled — propose theses based on your training knowledge, "
             "and flag that current data would refine the picks."
    )

    return f"""You are an investment research analyst helping a sophisticated retail investor expand their thesis book. Today is {today}.

INVESTOR'S STRATEGY CONTEXT:
{strategy_preamble or "(no strategy preamble provided)"}

THESES THEY ALREADY HOLD (do NOT re-suggest variants of these):
{existing}

YOUR JOB:
{web_instruction}

Propose **1 or 2 new investment theses** that fit this investor's strategy and that the broader market appears to be mispricing right now. A good thesis here is:
- Specific and falsifiable, not "I think tech goes up"
- Has a clear gap between consensus and the proposer's view
- Has a plausible catalyst path within 12-24 months (not pure long-term macro)
- Distinct from anything in the existing list above

OUTPUT FORMAT — follow exactly:

## Suggested new theses

### Thesis: <one-line declarative claim>

**Rationale:** 2-4 sentences on why you believe this and where the consensus gap is.

**Why now:** 1-2 sentences on what recent development or data point makes this timely (vs. the same thesis 6 months ago).

**Suggested tickers to research further:**

```yaml
tickers_to_research: [TICKER1, TICKER2, TICKER3]
```

(Repeat the structure for the second thesis if you have one. One strong thesis is better than two weak ones — don't pad.)

CRITICAL FORMATTING RULES:
- The yaml block must contain only plain ASCII tickers, no commentary.
- Use Yahoo-Finance-style symbols for non-US listings.
"""


# ── History, performance & calibration ──────────────────────────────────────
#
# Each report writes a structured sidecar (reports/{date}_research.json) capturing,
# per thesis: conviction (1-5) + direction, catalysts, the mock-portfolio holdings
# with entry prices, the realized weekly return of the PRIOR week's holdings, and a
# running time-weighted equity index (100 at inception). This single store powers
# both week-over-week continuity (#3) and the backtest ledger / calibration (#5).
# Theses are matched across weeks by a normalized title key.

CALIBRATION_MIN_PAIRS = 8  # need this many conviction→next-week-return pairs before reporting calibration


def thesis_key(title: str) -> str:
    """Normalize a thesis title into a stable key for week-over-week matching."""
    return re.sub(r"[^a-z0-9]+", "-", (title or "").lower()).strip("-")


def _to_float(s):
    """Lenient float parse: strips $ , % and whitespace; returns None on failure."""
    try:
        return float(str(s).replace("$", "").replace(",", "").replace("%", "").strip())
    except (TypeError, ValueError):
        return None


def split_meta_block(response: str) -> tuple[dict | None, str]:
    """Find the ```yaml block carrying conviction metadata (the one with a
    'conviction' key), parse it, and strip it from the text. Returns
    (meta_or_None, cleaned_text). Run BEFORE ticker extraction/rendering so the
    metadata never pollutes ticker parsing or the visible report."""
    for m in YAML_BLOCK_RE.finditer(response):
        try:
            data = yaml.safe_load(m.group(1))
        except yaml.YAMLError:
            continue
        if isinstance(data, dict) and "conviction" in data:
            cleaned = (response[: m.start()] + response[m.end():]).strip()
            return data, cleaned
    return None, response


def normalize_meta(meta: dict | None) -> dict:
    """Coerce a raw meta block into a clean, JSON-safe record."""
    meta = meta or {}
    try:
        conv = int(meta.get("conviction"))
    except (TypeError, ValueError):
        conv = None
    cats = []
    for c in (meta.get("catalysts") or []):
        if isinstance(c, dict):
            cats.append({"date": str(c.get("date", "")), "what": str(c.get("what", ""))})
    return {
        "conviction": conv,
        "direction": str(meta.get("direction", "n/a")).lower(),
        "conviction_note": str(meta.get("conviction_note", "") or ""),
        "catalysts": cats,
    }


def parse_portfolio_table(portfolio_md: str) -> list[dict]:
    """Parse holdings out of the pass-2 Mock Portfolio markdown table. Returns
    [{ticker, role, shares, entry_price, weight_pct, is_option}]. Skips the header,
    separator, and Total rows. Rows with '—'/blank shares or price are options
    (excluded from P&L)."""
    m = re.search(r"#{2,4}\s*Mock Portfolio.*?\n(.*?)(?:\n#{2,4}\s|\Z)",
                  portfolio_md, re.DOTALL | re.IGNORECASE)
    region = m.group(1) if m else portfolio_md
    holdings: list[dict] = []
    for line in region.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 6:
            continue
        first = cells[0].lower()
        if first in ("ticker", "") or set(cells[0]) <= set("-: "):  # header / separator
            continue
        if "total" in first:
            continue
        shares_s, price_s = cells[2].strip("* `"), cells[3].strip("* `")
        is_option = (shares_s in ("—", "-", "") or price_s in ("—", "-", ""))
        entry_price = _to_float(price_s)
        ticker_raw = cells[0].strip("* `").strip()
        holdings.append({
            "ticker": ticker_raw.upper() if not is_option else ticker_raw,
            "role": cells[1].strip("* `").lower(),
            "shares": _to_float(shares_s),
            "entry_price": entry_price,
            "weight_pct": _to_float(cells[5].strip("* `")),
            "is_option": bool(is_option) or entry_price is None,
        })
    return holdings


def load_prior_state(key: str, before_date: str) -> tuple[str, dict] | None:
    """Most recent sidecar (date < before_date) that contains this thesis key.
    Returns (prior_report_date, thesis_record) or None."""
    dated = sorted(
        (p.name[:10], p) for p in REPORTS_DIR.glob("*_research.json")
        if len(p.name[:10]) == 10 and p.name[:10] < before_date
    )
    for d, p in reversed(dated):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        for rec in data.get("theses", []):
            if rec.get("key") == key:
                return d, rec
    return None


def compute_weekly_return(prior_holdings: list[dict], enrichment: dict) -> tuple[list[dict], float | None]:
    """Mark prior holdings to current prices. Returns (legs, weekly_return).
    Excludes option legs and legs with no current/entry price; renormalizes weights
    across the priced equity legs. weekly_return is None if nothing is priceable."""
    legs, total_w = [], 0.0
    for h in prior_holdings:
        entry = h.get("entry_price")
        info = enrichment.get((h.get("ticker") or "").upper())
        if h.get("is_option"):
            legs.append({**h, "status": "option (excluded)"})
        elif entry in (None, 0) or not _is_priced(info):
            legs.append({**h, "status": "no current price (excluded)"})
        else:
            w = h.get("weight_pct") or 0.0
            total_w += w
            legs.append({**h, "now": info["price"], "ret": info["price"] / entry - 1.0, "status": "ok"})
    if total_w <= 0:
        return legs, None
    weekly = sum(l["ret"] * (l["weight_pct"] or 0.0) for l in legs if l["status"] == "ok") / total_w
    return legs, weekly


def build_prior_context(prior_date: str, rec: dict, today: str) -> str:
    """Compact continuity block injected into the pass-1 analysis prompt."""
    lines = [f"CONTINUITY — your prior take on this thesis (from {prior_date}):",
             f"- Prior conviction: {rec.get('conviction')}/5 — \"{rec.get('conviction_note') or ''}\""]
    cats = rec.get("catalysts") or []
    if cats:
        parts = []
        for c in cats:
            d, what = str(c.get("date", "")), c.get("what", "")
            tag = " (NOW PAST — assess what happened)" if d and d <= today else " (upcoming)"
            parts.append(f"{d} {what}{tag}".strip())
        lines.append("- Catalysts you flagged: " + "; ".join(parts))
    held = [f"{h['ticker']} ${h['entry_price']:.2f} ({h.get('role', '')})"
            for h in (rec.get("holdings") or []) if not h.get("is_option") and h.get("entry_price")]
    if held:
        lines.append("- Prior mock-portfolio holdings (entry prices): " + ", ".join(held))
    lines.append("Use WebSearch to determine what actually changed since then.")
    return "\n".join(lines)


def render_conviction_line(meta: dict, prior_conv: int | None) -> str:
    conv = meta.get("conviction")
    conv_s = f"{conv}/5" if conv is not None else "n/a"
    if conv is not None and prior_conv is not None:
        tag = (f"↑ from {prior_conv} last week" if conv > prior_conv
               else f"↓ from {prior_conv} last week" if conv < prior_conv
               else f"unchanged at {prior_conv}")
    else:
        tag = "first read"
    note = meta.get("conviction_note") or ""
    return f"**Conviction: {conv_s}** ({tag})" + (f" — _{note}_" if note else "")


def render_performance_block(prior_date, legs, weekly_return, equity_index, inception_date) -> str:
    if not legs:
        return ("### Portfolio performance\n\n"
                "_First tracked week — week-over-week performance starts next report._")
    head = ["| Ticker | Entry | Now | Δ% | Weight |", "|--------|-------|-----|-----|--------|"]
    rows = []
    for l in legs:
        w = l.get("weight_pct") or 0.0
        if l.get("status") == "ok":
            rows.append(f"| {l['ticker']} | ${l['entry_price']:.2f} | ${l['now']:.2f} | "
                        f"{l['ret'] * 100:+.1f}% | {w:.0f}% |")
        else:
            entry = f"${l['entry_price']:.2f}" if l.get("entry_price") else "—"
            rows.append(f"| {l['ticker']} | {entry} | — | _{l.get('status', '')}_ | {w:.0f}% |")
    wk = f"{weekly_return * 100:+.1f}%" if weekly_return is not None else "n/a"
    summary = (f"\n\n**Thesis weekly return:** {wk} (prior portfolio from {prior_date}) · "
               f"**Since inception ({inception_date}):** {equity_index - 100:+.1f}% "
               f"(index {equity_index:.1f})")
    return "### Portfolio performance\n\n" + "\n".join(head + rows) + summary


def compute_and_render(results: list[dict], enrichment: dict, report_date: str) -> tuple[list[str], list[dict]]:
    """Shared post-processing for both flows. For each normalized thesis result
    {key, title, meta, holdings, rendered_analysis, portfolio_text}, compute the
    week-over-week performance + running equity index, render the full section, and
    build the sidecar record. Returns (sections aligned to results, sidecar records)."""
    sections, records = [], []
    for r in results:
        key, title, meta = r["key"], r["title"], r["meta"]
        prior = load_prior_state(key, report_date)
        if prior:
            prior_date, prec = prior
            legs, weekly = compute_weekly_return(prec.get("holdings", []), enrichment)
            prior_index = prec.get("equity_index") or 100.0
            equity_index = prior_index * (1 + weekly) if weekly is not None else prior_index
            inception_date = prec.get("inception_date") or prior_date
            prior_conv = prec.get("conviction")
        else:
            prior_date, legs, weekly = None, [], None
            equity_index, inception_date, prior_conv = 100.0, report_date, None

        perf = render_performance_block(prior_date, legs, weekly, equity_index, inception_date)
        conv_line = render_conviction_line(meta, prior_conv)
        sections.append(
            f"## Thesis: {title}\n\n{conv_line}\n\n"
            f"{r['rendered_analysis']}\n\n{r['portfolio_text']}\n\n{perf}"
        )
        records.append({
            "key": key, "title": title, "inception_date": inception_date,
            "conviction": meta["conviction"], "direction": meta["direction"],
            "conviction_note": meta["conviction_note"], "catalysts": meta["catalysts"],
            "holdings": r["holdings"], "weekly_return": weekly,
            "equity_index": round(equity_index, 4), "prior_report_date": prior_date,
        })
    return sections, records


def render_calibration_section(current_records: list[dict], report_date: str) -> str:
    """Book-level ledger + conviction calibration, reading all past sidecars plus
    this run's in-memory records. Calibration pairs conviction[T] with the realized
    weekly_return[T+1] of the same thesis."""
    timeline: dict[str, list[tuple[str, dict]]] = {}

    def _add(d, recs):
        for rec in recs:
            if rec.get("key"):
                timeline.setdefault(rec["key"], []).append((d, rec))

    for p in sorted(REPORTS_DIR.glob("*_research.json")):
        d = p.name[:10]
        if not (len(d) == 10 and d < report_date):
            continue
        try:
            _add(d, json.loads(p.read_text(encoding="utf-8")).get("theses", []))
        except (json.JSONDecodeError, OSError):
            continue
    _add(report_date, current_records)
    for items in timeline.values():
        items.sort(key=lambda t: t[0])

    ledger = []
    for k, items in sorted(timeline.items()):
        _, rec = items[-1]
        wk, eq = rec.get("weekly_return"), rec.get("equity_index")
        wk_s = f"{wk * 100:+.1f}%" if isinstance(wk, (int, float)) else "—"
        si_s = f"{eq - 100:+.1f}%" if isinstance(eq, (int, float)) else "—"
        conv = rec.get("conviction")
        ledger.append(f"| {rec.get('title', k)} | {conv if conv is not None else '—'}/5 | {wk_s} | {si_s} |")

    buckets: dict[int, list[float]] = {c: [] for c in range(1, 6)}
    pairs = 0
    for items in timeline.values():
        for (_, r0), (_, r1) in zip(items, items[1:]):
            c, ret = r0.get("conviction"), r1.get("weekly_return")
            if isinstance(c, int) and c in buckets and isinstance(ret, (int, float)):
                buckets[c].append(ret)
                pairs += 1

    out = ["## Conviction Calibration & Ledger", "",
           "_Educational backtest of the hypothetical mock portfolios — not financial advice._", "",
           "### Ledger (latest per thesis)", "",
           "| Thesis | Conviction | Last weekly return | Since inception |",
           "|--------|-----------|--------------------|-----------------|"]
    out += ledger or ["| _no theses tracked yet_ | — | — | — |"]
    out += ["", "### Conviction calibration", ""]
    if pairs < CALIBRATION_MIN_PAIRS:
        out.append(f"_Insufficient history to assess calibration ({pairs}/{CALIBRATION_MIN_PAIRS} "
                   "conviction→return pairs). Builds up as more weekly reports accrue._")
    else:
        out += ["Average *subsequent-week* return of the prior portfolio, bucketed by the "
                "conviction recorded the week before:", "",
                "| Conviction | Avg next-week return | Samples |",
                "|-----------|----------------------|---------|"]
        for c in range(5, 0, -1):
            vals = buckets[c]
            if vals:
                out.append(f"| {c}/5 | {sum(vals) / len(vals) * 100:+.1f}% | {len(vals)} |")
        out.append(f"\n_Based on {pairs} conviction→return pairs across {len(timeline)} theses._")
    return "\n".join(out)


def write_sidecar(report_date: str, records: list[dict]) -> None:
    """Persist the structured per-thesis history sidecar next to the report."""
    payload = {"report_date": report_date,
               "generated_at": datetime.now(ET).isoformat(),
               "theses": records}
    _write_json_atomic(REPORTS_DIR / f"{report_date}_research.json", payload)


# ── Main flow ───────────────────────────────────────────────────────────────

def build_portfolio_section(strategy_preamble: str, thesis: dict, analysis_text: str,
                            enrichment: dict) -> str:
    """Pass 2: build the price-grounded portfolio / scenario / trigger section from the
    pass-1 analysis and freshly enriched prices. Returns a placeholder (no Claude call)
    when nothing priced, so a thesis with all-bad tickers still renders cleanly."""
    priced = sum(1 for info in enrichment.values() if _is_priced(info))
    if not priced:
        return ("### Mock Portfolio\n\n"
                "> ⚠ No live prices were available for the suggested tickers, so a "
                "price-grounded portfolio could not be built this run.")
    print(f"[{_ts()}]   Pass 2: sizing portfolio from {priced} priced ticker(s)")
    price_table = format_enrichment_table(enrichment)
    return call_claude(
        build_portfolio_prompt(strategy_preamble, thesis, analysis_text, price_table),
        allow_web=False,
    )


def run(args: argparse.Namespace) -> str:
    theses_path = Path(args.theses) if args.theses else DEFAULT_THESES_FILE
    if not theses_path.exists():
        if theses_path == DEFAULT_THESES_FILE and EXAMPLE_THESES_FILE.exists():
            print(
                f"No theses.md found. Copy theses.example.md to theses.md and edit it, "
                f"or pass --theses {EXAMPLE_THESES_FILE.name} to run against the example."
            )
            sys.exit(1)
        print(f"Theses file not found: {theses_path}")
        sys.exit(1)

    print(f"[{_ts()}] Parsing {theses_path.name}...")
    preamble, theses = parse_theses(theses_path)
    print(f"[{_ts()}] Found {len(theses)} thesis/theses, "
          f"{len(preamble)} chars of strategy preamble.")

    if args.thesis:
        needle = args.thesis.lower()
        theses = [t for t in theses if needle in t["title"].lower()]
        if not theses:
            print(f"No theses matched filter: {args.thesis!r}")
            sys.exit(1)
        print(f"[{_ts()}] Filter matched {len(theses)} thesis/theses.")

    allow_web = not args.no_web
    report_date = date.today().isoformat()
    results: list[dict] = []
    combined_enrichment: dict = {}

    for i, thesis in enumerate(theses, 1):
        key = thesis_key(thesis["title"])
        print(f"[{_ts()}] [{i}/{len(theses)}] Researching: {thesis['title']!r}")
        prior = load_prior_state(key, report_date)
        prior_context = build_prior_context(prior[0], prior[1], report_date) if prior else None
        if prior_context:
            print(f"[{_ts()}]   Continuity: threading in prior take from {prior[0]}.")
        analysis_raw = call_claude(
            build_analysis_prompt(preamble, thesis, allow_web, prior_context), allow_web=allow_web)
        meta, analysis = split_meta_block(analysis_raw)
        tickers = extract_tickers_from_response(analysis)
        prior_tickers = [h["ticker"] for h in (prior[1].get("holdings") if prior else [])
                         if not h.get("is_option")]
        enrichment = enrich_tickers(tickers + prior_tickers)
        combined_enrichment.update(enrichment)
        rendered = replace_yaml_blocks_with_tables(analysis, enrichment)
        portfolio = build_portfolio_section(preamble, thesis, analysis, enrichment)
        results.append({
            "key": key, "title": thesis["title"], "meta": normalize_meta(meta),
            "holdings": parse_portfolio_table(portfolio),
            "rendered_analysis": rendered, "portfolio_text": portfolio,
        })

    sections, records = compute_and_render(results, combined_enrichment, report_date)

    if not args.skip_new_thesis_scan and not args.thesis:
        print(f"[{_ts()}] Running new-thesis scan...")
        prompt = build_new_thesis_prompt(preamble, [t["title"] for t in theses], allow_web)
        response = call_claude(prompt, allow_web=allow_web)
        enrichment = enrich_tickers(extract_tickers_from_response(response))
        sections.append(replace_yaml_blocks_with_tables(response, enrichment))

    sections.append(render_calibration_section(records, report_date))

    header = (f"# Market Research — {report_date}\n\n"
              f"_Generated {datetime.now(ET).strftime('%Y-%m-%d %H:%M %Z')} from {theses_path.name}_\n")
    report = f"{header}\n" + "\n\n---\n\n".join(sections) + "\n"

    if args.test:
        print(f"[{_ts()}] Test mode — not saving.")
    else:
        REPORTS_DIR.mkdir(exist_ok=True)
        out = REPORTS_DIR / f"{report_date}_research.md"
        out.write_text(report, encoding="utf-8")
        if args.thesis:
            print(f"[{_ts()}] Report saved → {out} (filtered run — history sidecar not written).")
        else:
            write_sidecar(report_date, records)
            print(f"[{_ts()}] Report saved → {out} (+ history sidecar).")

    return report


def _ts() -> str:
    return datetime.now(ET).strftime("%H:%M:%S")


# ── Weekend-batch mode ──────────────────────────────────────────────────────
#
# The default `run()` does every Claude call in one process, spiking the
# 5-hour subscription quota. Weekend-batch mode breaks the work into one
# Claude call per scheduled invocation, sharing state through a JSON manifest:
#
#   --research-next : research the next pending unit (one Claude call), persist
#   --assemble      : stitch all researched units into the Monday report
#
# A "unit" is one thesis, or the final new-thesis scan. The manifest snapshots
# theses.md at batch creation, so editing theses.md mid-weekend can't corrupt
# an in-progress batch.

def upcoming_monday(d: date | None = None) -> date:
    """The Monday this batch belongs to — today if today is Monday, else the
    next Monday. Weekend research runs and the Monday assembly all resolve to
    the same date, so they share one manifest."""
    d = d or date.today()
    return d + timedelta(days=(0 - d.weekday()) % 7)


def manifest_path(monday: date) -> Path:
    return PARTIAL_DIR / f"batch_{monday.isoformat()}.json"


def load_manifest(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json_atomic(path: Path, obj) -> None:
    """Write JSON via temp file + os.replace so an interrupted run can't leave a
    half-written file behind."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / (path.name + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def save_manifest(path: Path, manifest: dict) -> None:
    """Write the batch manifest atomically (see _write_json_atomic)."""
    _write_json_atomic(path, manifest)


def create_manifest(theses_path: Path, monday: date) -> dict:
    """Build a fresh batch manifest from theses.md. The new-thesis scan is
    always appended as the final unit so it runs after the user's own theses."""
    preamble, theses = parse_theses(theses_path)
    units = []
    for i, t in enumerate(theses, 1):
        units.append({
            "id": f"thesis-{i}",
            "kind": "thesis",
            "title": t["title"],
            "body": t["body"],
            "status": "pending",       # pending | done
            "attempts": 0,
            "last_error": None,
            "raw_response": None,        # legacy single-pass field; unused for theses now
            "analysis_response": None,   # pass 1: steel-man/devil's/indexes/stocks/notes
            "portfolio_response": None,  # pass 2: price-grounded portfolio/scenario/triggers
            "researched_at": None,
        })
    units.append({
        "id": "new-thesis-scan",
        "kind": "scan",
        "title": None,
        "body": None,
        "status": "pending",
        "attempts": 0,
        "last_error": None,
        "raw_response": None,
        "researched_at": None,
    })
    return {
        "batch_date": monday.isoformat(),
        "created_at": datetime.now(ET).isoformat(),
        "theses_source": theses_path.name,
        "strategy_preamble": preamble,
        "units": units,
    }


def _next_unit(manifest: dict) -> dict | None:
    """First unit still worth working on: pending and under the attempt cap."""
    return next(
        (u for u in manifest["units"]
         if u["status"] == "pending" and u["attempts"] < MAX_ATTEMPTS),
        None,
    )


def run_research_next(args: argparse.Namespace) -> None:
    """Weekend worker. Researches exactly ONE pending unit, then exits — so
    each scheduled slot makes a single Claude call. Idempotent and retry-safe:
    a failed unit stays pending for a later slot; extra slots no-op cleanly."""
    theses_path = Path(args.theses) if args.theses else DEFAULT_THESES_FILE
    monday = upcoming_monday()
    mpath = manifest_path(monday)

    # Completion guard: once this week's report exists the batch is finished.
    # Auto-assembly (below) archives the manifest when the last unit completes,
    # so without this a later slot would create a fresh manifest and re-research
    # everything. This makes surplus weekend slots no-op after the report is out.
    report_out = REPORTS_DIR / f"{monday.isoformat()}_research.md"
    if report_out.exists():
        print(f"[{_ts()}] Report for {monday.isoformat()} already assembled — slot is a no-op.")
        return

    if mpath.exists():
        manifest = load_manifest(mpath)
        print(f"[{_ts()}] Resuming batch {manifest['batch_date']} ({mpath.name}).")
    else:
        if not theses_path.exists():
            print(f"[{_ts()}] Theses file not found: {theses_path} — cannot start a batch.")
            sys.exit(1)
        manifest = create_manifest(theses_path, monday)
        save_manifest(mpath, manifest)
        print(f"[{_ts()}] Created batch {manifest['batch_date']} "
              f"with {len(manifest['units'])} units → {mpath.name}")

    unit = _next_unit(manifest)
    if unit is None:
        # Nothing left worth attempting but the report isn't out yet (e.g. a prior
        # slot finished the last unit but crashed before assembling). Recover by
        # assembling now instead of idling until Monday.
        done = sum(1 for u in manifest["units"] if u["status"] == "done")
        total = len(manifest["units"])
        if args.test:
            print(f"[{_ts()}] Nothing pending ({done}/{total} done) — slot is a no-op (test).")
        else:
            print(f"[{_ts()}] Nothing pending ({done}/{total} done) — assembling now.")
            run_assemble(args)
        return

    allow_web = not args.no_web
    label = unit["title"] or "new-thesis scan"
    print(f"[{_ts()}] Researching {unit['id']}: {label!r} "
          f"(attempt {unit['attempts'] + 1}/{MAX_ATTEMPTS})")

    unit["attempts"] += 1
    preamble = manifest["strategy_preamble"]
    report_date = manifest["batch_date"]
    try:
        if unit["kind"] == "thesis":
            # In-slot two-pass with week-over-week continuity:
            #   prior context → research (web) → enrich → price-grounded portfolio.
            thesis = {"title": unit["title"], "body": unit["body"]}
            prior = load_prior_state(thesis_key(unit["title"]), report_date)
            prior_context = build_prior_context(prior[0], prior[1], report_date) if prior else None
            analysis_raw = call_claude(
                build_analysis_prompt(preamble, thesis, allow_web, prior_context), allow_web=allow_web)
            _, analysis = split_meta_block(analysis_raw)
            tickers = extract_tickers_from_response(analysis)
            prior_tickers = [h["ticker"] for h in (prior[1].get("holdings") if prior else [])
                             if not h.get("is_option")]
            enrichment = enrich_tickers(tickers + prior_tickers)
            portfolio = build_portfolio_section(preamble, thesis, analysis, enrichment)
            unit["analysis_response"] = analysis_raw   # raw (incl. meta block); assemble re-splits
            unit["portfolio_response"] = portfolio
        else:
            existing = [u["title"] for u in manifest["units"] if u["kind"] == "thesis"]
            unit["raw_response"] = call_claude(
                build_new_thesis_prompt(preamble, existing, allow_web), allow_web=allow_web)
    except Exception as e:
        unit["last_error"] = str(e)[:1000]
        save_manifest(mpath, manifest)
        print(f"[{_ts()}] Claude call failed — {unit['id']} left pending for a later slot.")
        print(f"[{_ts()}] Error: {e}")
        sys.exit(1)

    unit["status"] = "done"
    unit["researched_at"] = datetime.now(ET).isoformat()
    unit["last_error"] = None
    save_manifest(mpath, manifest)
    remaining = sum(1 for u in manifest["units"] if u["status"] == "pending")
    print(f"[{_ts()}] {unit['id']} done. {remaining} unit(s) still pending.")

    # The new-thesis scan is always the last unit, so finishing the batch means
    # the whole report is ready. Assemble immediately rather than waiting for the
    # Monday task — a 2-thesis book is done Saturday evening. (Monday stays a
    # fallback for batches a weekend slot never managed to complete.)
    if _next_unit(manifest) is None and not args.test:
        if all(u["status"] == "done" for u in manifest["units"]):
            print(f"[{_ts()}] Batch complete — assembling now (not waiting for Monday).")
        else:
            print(f"[{_ts()}] No retriable units left — assembling available units now.")
        run_assemble(args)


def run_assemble(args: argparse.Namespace) -> None:
    """Monday step. Stitches every researched unit in this week's batch into
    one report, with a single consistent ticker-price snapshot taken now.
    Units that never completed render as a clear placeholder rather than
    blocking the report."""
    monday = upcoming_monday()
    mpath = manifest_path(monday)
    if not mpath.exists():
        print(f"[{_ts()}] No batch manifest for {monday.isoformat()} ({mpath.name}). "
              "Nothing to assemble — did the weekend job run?")
        return

    manifest = load_manifest(mpath)
    units = manifest["units"]
    done = [u for u in units if u["status"] == "done"]
    print(f"[{_ts()}] Assembling batch {manifest['batch_date']}: "
          f"{len(done)}/{len(units)} units researched.")

    report_date = manifest["batch_date"]

    # Pass A — parse stored responses and gather every ticker (current + prior
    # holdings + scan) so the whole batch shares one Monday price snapshot.
    all_tickers: list[str] = []
    parsed: dict[str, dict] = {}
    for u in done:
        if u["kind"] == "thesis":
            meta, cleaned = split_meta_block(u.get("analysis_response") or u.get("raw_response") or "")
            prior = load_prior_state(thesis_key(u["title"]), report_date)
            parsed[u["id"]] = {
                "meta": meta, "cleaned": cleaned,
                "holdings": parse_portfolio_table(u.get("portfolio_response") or ""), "prior": prior,
            }
            all_tickers += extract_tickers_from_response(cleaned)
            all_tickers += [h["ticker"] for h in (prior[1].get("holdings") if prior else [])
                            if not h.get("is_option")]
        else:
            all_tickers += extract_tickers_from_response(u.get("raw_response") or "")
    enrichment = enrich_tickers(all_tickers)

    # Pass B — normalize done thesis units (unit order) and render their sections.
    results: list[dict] = []
    for u in units:
        if u["status"] == "done" and u["kind"] == "thesis":
            p = parsed[u["id"]]
            portfolio = u.get("portfolio_response") or ""
            if u.get("researched_at"):
                portfolio = f"_Portfolio priced as of {u['researched_at']}._\n\n{portfolio}"
            results.append({
                "unit_id": u["id"], "key": thesis_key(u["title"]), "title": u["title"],
                "meta": normalize_meta(p["meta"]), "holdings": p["holdings"],
                "rendered_analysis": replace_yaml_blocks_with_tables(p["cleaned"], enrichment),
                "portfolio_text": portfolio,
            })
    thesis_sections, records = compute_and_render(results, enrichment, report_date)
    sec_by_id = {r["unit_id"]: s for r, s in zip(results, thesis_sections)}

    # Pass C — stitch in unit order, weaving placeholders + scan back in.
    sections: list[str] = []
    for u in units:
        if u["status"] == "done":
            if u["kind"] == "thesis":
                sections.append(sec_by_id[u["id"]])
            else:
                sections.append(replace_yaml_blocks_with_tables(u.get("raw_response") or "", enrichment))
        else:
            err = f" Last error: {u['last_error']}" if u.get("last_error") else ""
            placeholder = f"> ⚠ Not researched this batch — {u['attempts']} attempt(s).{err}"
            if u["kind"] == "thesis":
                sections.append(f"## Thesis: {u['title']}\n\n{placeholder}")
            else:
                sections.append(f"## Suggested new theses\n\n{placeholder}")

    sections.append(render_calibration_section(records, report_date))

    header = (f"# Market Research — {report_date}\n\n"
              f"_Weekend batch assembled {datetime.now(ET).strftime('%Y-%m-%d %H:%M %Z')} "
              f"from {manifest['theses_source']} · "
              f"{len(done)}/{len(units)} units researched_\n")
    report = f"{header}\n" + "\n\n---\n\n".join(sections) + "\n"

    if args.test:
        print(f"[{_ts()}] Test mode — not saving, manifest left in place.")
        print("\n" + "=" * 60)
        print(report)
        print("=" * 60 + "\n")
        return

    REPORTS_DIR.mkdir(exist_ok=True)
    out = REPORTS_DIR / f"{report_date}_research.md"
    out.write_text(report, encoding="utf-8")
    write_sidecar(report_date, records)
    print(f"[{_ts()}] Report saved → {out} (+ history sidecar).")

    # Archive the manifest so next weekend starts a fresh batch.
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    archived = ARCHIVE_DIR / mpath.name
    os.replace(mpath, archived)
    print(f"[{_ts()}] Manifest archived → {archived}")


# ── Entry point ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Thesis-Based Market Research Agent")
    parser.add_argument("--theses", help="Path to theses Markdown file (default: theses.md)")
    parser.add_argument("--thesis", help="Run only theses whose title contains this substring (case-insensitive)")
    parser.add_argument("--skip-new-thesis-scan", action="store_true", help="Skip the closing 'suggest new theses' pass")
    parser.add_argument("--test", action="store_true", help="Print report, don't save")
    parser.add_argument("--no-web", action="store_true", help="Disable WebSearch/WebFetch (faster, shallower)")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--research-next", action="store_true",
                      help="Weekend worker: research the next pending unit in this week's batch, then exit")
    mode.add_argument("--assemble", action="store_true",
                      help="Stitch this week's researched batch into the Monday report")
    args = parser.parse_args()

    print(f"\n{'=' * 60}")
    if args.research_next:
        print(f"  THESIS RESEARCH AGENT — WEEKEND WORKER")
    elif args.assemble:
        print(f"  THESIS RESEARCH AGENT — ASSEMBLE BATCH")
    else:
        print(f"  THESIS RESEARCH AGENT")
    print(f"{'=' * 60}\n")

    if args.research_next:
        run_research_next(args)
    elif args.assemble:
        run_assemble(args)
    else:
        report = run(args)
        print("\n" + "=" * 60)
        print(report)
        print("=" * 60 + "\n")
