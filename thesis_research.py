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
"""

import argparse
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
ET = ZoneInfo("America/New_York")

ALPACA_KEY = os.environ.get("ALPACA_API_KEY")
ALPACA_SECRET = os.environ.get("ALPACA_API_SECRET")
ALPACA_FEED = os.environ.get("ALPACA_FEED", "iex").lower()

CLAUDE_MODEL = "claude-opus-4-7"
# Web research is materially slower than the price-only brief — give it room.
CLAUDE_TIMEOUT_SEC = 900


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
        if not info or "error" in (info or {}):
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


# ── Prompt builders ─────────────────────────────────────────────────────────

def build_thesis_prompt(strategy_preamble: str, thesis: dict, allow_web: bool) -> str:
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

    return f"""You are an investment research analyst helping a sophisticated retail investor stress-test one of their investment theses. Today is {today}.

INVESTOR'S STRATEGY CONTEXT (use this to keep ticker suggestions aligned with how they actually invest):
{strategy_preamble or "(no strategy preamble provided)"}

THE THESIS UNDER REVIEW:
## Thesis: {thesis['title']}

{thesis['body']}

YOUR JOB:
{web_instruction}

Produce a critical analysis of this thesis. Bear in mind: by definition, a thesis like this is one where the investor disagrees with the market, so you SHOULD expect to find more weight of evidence on the "against" side — that's fine, the goal is to give them ammunition for both sides so they can re-examine their conviction.

OUTPUT FORMAT — follow exactly. Do not add a top-level header; the script wraps your output.

### Steel man (best arguments FOR the thesis)
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

CRITICAL FORMATTING RULES:
- The yaml blocks must use plain ASCII tickers (e.g. NVDA, SMH, TLRY). For non-US listings, use the full Yahoo-Finance-style symbol (e.g. TLRY.TO, SHOP.TO, ASML.AS).
- Do not put commentary inside the yaml blocks — only the ticker lists.
- If a side is empty (e.g. no clear downside indexes), use an empty list `[]`, do not omit the key.
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


# ── Main flow ───────────────────────────────────────────────────────────────

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
    sections: list[str] = []

    for i, thesis in enumerate(theses, 1):
        print(f"[{_ts()}] [{i}/{len(theses)}] Researching: {thesis['title']!r}")
        prompt = build_thesis_prompt(preamble, thesis, allow_web)
        response = call_claude(prompt, allow_web=allow_web)
        tickers = extract_tickers_from_response(response)
        enrichment = enrich_tickers(tickers)
        rendered = replace_yaml_blocks_with_tables(response, enrichment)
        sections.append(f"## Thesis: {thesis['title']}\n\n{rendered}")

    if not args.skip_new_thesis_scan and not args.thesis:
        print(f"[{_ts()}] Running new-thesis scan...")
        prompt = build_new_thesis_prompt(preamble, [t["title"] for t in theses], allow_web)
        response = call_claude(prompt, allow_web=allow_web)
        tickers = extract_tickers_from_response(response)
        enrichment = enrich_tickers(tickers)
        rendered = replace_yaml_blocks_with_tables(response, enrichment)
        sections.append(rendered)

    today = date.today().isoformat()
    header = f"# Market Research — {today}\n\n_Generated {datetime.now(ET).strftime('%Y-%m-%d %H:%M %Z')} from {theses_path.name}_\n"
    body = "\n\n---\n\n".join(sections)
    report = f"{header}\n{body}\n"

    if args.test:
        print(f"[{_ts()}] Test mode — not saving.")
    else:
        REPORTS_DIR.mkdir(exist_ok=True)
        out = REPORTS_DIR / f"{today}_research.md"
        out.write_text(report, encoding="utf-8")
        print(f"[{_ts()}] Report saved → {out}")

    return report


def _ts() -> str:
    return datetime.now(ET).strftime("%H:%M:%S")


# ── Entry point ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Thesis-Based Market Research Agent")
    parser.add_argument("--theses", help="Path to theses Markdown file (default: theses.md)")
    parser.add_argument("--thesis", help="Run only theses whose title contains this substring (case-insensitive)")
    parser.add_argument("--skip-new-thesis-scan", action="store_true", help="Skip the closing 'suggest new theses' pass")
    parser.add_argument("--test", action="store_true", help="Print report, don't save")
    parser.add_argument("--no-web", action="store_true", help="Disable WebSearch/WebFetch (faster, shallower)")
    args = parser.parse_args()

    print(f"\n{'=' * 60}")
    print(f"  THESIS RESEARCH AGENT")
    print(f"{'=' * 60}\n")

    report = run(args)

    print("\n" + "=" * 60)
    print(report)
    print("=" * 60 + "\n")
