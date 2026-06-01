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

CLAUDE_MODEL = "claude-opus-4-8"
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

### Mock Portfolio (~${MOCK_PORTFOLIO_USD:,} hypothetical — educational, not financial advice)
Construct ONE balanced hypothetical ${MOCK_PORTFOLIO_USD:,} allocation that expresses this thesis. Build it from the tickers you surfaced in Indexes/Stocks above (their live price and 52-week context is in the tables there). Present it as a **Markdown table** — NOT a yaml block — with columns: `Ticker | Role | Allocation | Weight | Rationale`. End with a **Total** row that sums to ≈ ${MOCK_PORTFOLIO_USD:,}.
- `Role` is one of: `core`, `speculative`, `hedge`, `overlay`.
  - `core` = liquid, lower-variance expression of the thesis (the anchor).
  - `speculative` = concentrated, catalyst-direct, higher-variance satellite.
  - `hedge` = offsets a specific risk in the book.
  - `overlay` = an OPTIONAL small options sleeve (e.g. calls/puts on a core name) — at most one, kept modest.
- Aim for genuine balance: a core anchor, one or two speculative satellites, and an optional small overlay — sized to each leg's conviction and risk, not split evenly by default.

### Scenario Matrix
A **Markdown table** with columns: `Scenario | What it looks like | Portfolio impact`. Cover at least three rows: the thesis plays out, a partial / delayed outcome, and the thesis failing or being invalidated.

### Trigger Logic
The key catalyst date(s) and the specific conditions that would make you ADD, TRIM, or EXIT each leg. Be concrete about dates and price/event thresholds wherever the thesis provides them.

CRITICAL FORMATTING RULES:
- The yaml blocks must use plain ASCII tickers (e.g. NVDA, SMH, TLRY). For non-US listings, use the full Yahoo-Finance-style symbol (e.g. TLRY.TO, SHOP.TO, ASML.AS).
- Do not put commentary inside the yaml blocks — only the ticker lists.
- If a side is empty (e.g. no clear downside indexes), use an empty list `[]`, do not omit the key.
- The Mock Portfolio and Scenario Matrix MUST be Markdown tables, never yaml — only the Indexes and Stocks lists use yaml.
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


def save_manifest(path: Path, manifest: dict) -> None:
    """Write atomically (temp + replace) so an interrupted scheduled run can't
    leave a half-written manifest."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    os.replace(tmp, path)


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
            "raw_response": None,
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
        done = sum(1 for u in manifest["units"] if u["status"] == "done")
        total = len(manifest["units"])
        print(f"[{_ts()}] Nothing pending ({done}/{total} done) — slot is a no-op.")
        return

    allow_web = not args.no_web
    label = unit["title"] or "new-thesis scan"
    print(f"[{_ts()}] Researching {unit['id']}: {label!r} "
          f"(attempt {unit['attempts'] + 1}/{MAX_ATTEMPTS})")

    if unit["kind"] == "thesis":
        prompt = build_thesis_prompt(
            manifest["strategy_preamble"],
            {"title": unit["title"], "body": unit["body"]},
            allow_web,
        )
    else:
        existing = [u["title"] for u in manifest["units"] if u["kind"] == "thesis"]
        prompt = build_new_thesis_prompt(manifest["strategy_preamble"], existing, allow_web)

    unit["attempts"] += 1
    try:
        response = call_claude(prompt, allow_web=allow_web)
    except Exception as e:
        unit["last_error"] = str(e)[:1000]
        save_manifest(mpath, manifest)
        print(f"[{_ts()}] Claude call failed — {unit['id']} left pending for a later slot.")
        print(f"[{_ts()}] Error: {e}")
        sys.exit(1)

    unit["status"] = "done"
    unit["raw_response"] = response
    unit["researched_at"] = datetime.now(ET).isoformat()
    unit["last_error"] = None
    save_manifest(mpath, manifest)
    remaining = sum(1 for u in manifest["units"] if u["status"] == "pending")
    print(f"[{_ts()}] {unit['id']} done. {remaining} unit(s) still pending.")


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

    # Enrich every ticker across the whole batch in one pass — all prices share
    # one Monday snapshot rather than drifting across the weekend's runs.
    all_tickers: list[str] = []
    for u in done:
        all_tickers += extract_tickers_from_response(u["raw_response"])
    enrichment = enrich_tickers(all_tickers)

    sections: list[str] = []
    for u in units:
        if u["status"] == "done":
            rendered = replace_yaml_blocks_with_tables(u["raw_response"], enrichment)
            if u["kind"] == "thesis":
                sections.append(f"## Thesis: {u['title']}\n\n{rendered}")
            else:
                sections.append(rendered)
        else:
            err = f" Last error: {u['last_error']}" if u.get("last_error") else ""
            placeholder = (f"> ⚠ Not researched this batch — "
                           f"{u['attempts']} attempt(s).{err}")
            if u["kind"] == "thesis":
                sections.append(f"## Thesis: {u['title']}\n\n{placeholder}")
            else:
                sections.append(f"## Suggested new theses\n\n{placeholder}")

    batch_date = manifest["batch_date"]
    header = (f"# Market Research — {batch_date}\n\n"
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
    out = REPORTS_DIR / f"{batch_date}_research.md"
    out.write_text(report, encoding="utf-8")
    print(f"[{_ts()}] Report saved → {out}")

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
