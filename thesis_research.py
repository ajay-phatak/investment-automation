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
import time
import urllib.request
from collections import Counter
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


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader (KEY=VALUE lines, # comments, optional quotes).
    Values already present in the environment win, so a shell-exported key
    overrides the file. Scheduled-task runs don't inherit user shell exports,
    so without this the Alpaca keys were never seen by unattended runs."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip("'\"")
        if key and value and key not in os.environ:
            os.environ[key] = value


_load_dotenv(SCRIPT_DIR / ".env")

ALPACA_KEY = os.environ.get("ALPACA_API_KEY")
ALPACA_SECRET = os.environ.get("ALPACA_API_SECRET")
ALPACA_FEED = os.environ.get("ALPACA_FEED", "iex").lower()

# Preferred model, with a fallback for when it isn't available on the
# subscription (Fable 5 comes and goes week to week). Every call tries the
# preferred model first and retries once on the fallback — see call_claude.
CLAUDE_MODEL = "claude-fable-5"
CLAUDE_FALLBACK_MODEL = "claude-opus-4-8"
# Web research is materially slower than the price-only brief — give it room.
CLAUDE_TIMEOUT_SEC = 900

# Each thesis gets a hypothetical, educational mock portfolio of this size.
# One-line edit to change it — applies to both the all-at-once and weekend runs.
MOCK_PORTFOLIO_USD = 5000

# Benchmark for performance comparison. Always enriched alongside thesis tickers;
# each sidecar record stores its price + a running benchmark index so every
# weekly/since-inception number can be read against the market.
BENCHMARK_TICKER = "SPY"

# Optional Obsidian delivery: when OBSIDIAN_VAULT_DIR is set (usually via .env),
# every saved report is also written into the vault. Plain file write — Obsidian
# indexes disk changes itself, so this works unattended with no plugins/API.
OBSIDIAN_VAULT_DIR = os.environ.get("OBSIDIAN_VAULT_DIR")
OBSIDIAN_REPORTS_SUBDIR = "claude reports/market research agent"

# ── Auth health & alerting ──────────────────────────────────────────────────
# The weekend worker strips ANTHROPIC_API_KEY (see call_claude), so every call
# rides on the Claude Code *subscription* OAuth token stored here. When that
# token expires or is revoked the CLI returns HTTP 401 and the only fix is an
# interactive `claude` → /login. Path is overridable via env for tests.
CREDENTIALS_PATH = Path(os.environ.get(
    "CLAUDE_CREDENTIALS_PATH", Path.home() / ".claude" / ".credentials.json"))
# Optional push channels — set either in .env to get phone/desktop pushes.
# NOTIFY_NTFY_TOPIC: a private, random ntfy.sh topic (or a full URL); install the
# ntfy app and subscribe to it. NOTIFY_WEBHOOK_URL: a Slack/Discord/generic webhook.
NOTIFY_NTFY_TOPIC = os.environ.get("NOTIFY_NTFY_TOPIC")
NOTIFY_WEBHOOK_URL = os.environ.get("NOTIFY_WEBHOOK_URL")
# Written when an auth alert fires so the ~16 weekend slots don't each re-push;
# cleared automatically by the next successful Claude call.
AUTH_ALERT_SENTINEL = PARTIAL_DIR / "_auth_alert.flag"
# The long-lived `claude setup-token` token is opaque (no embedded expiry), so
# record its expiry date by hand (YYYY-MM-DD via CLAUDE_CODE_OAUTH_TOKEN_EXPIRES
# in .env) when you generate it. Reports count down to it; the pre-flight warns
# once it's within this many days.
TOKEN_WARN_DAYS = 30


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


class AuthError(RuntimeError):
    """A Claude CLI call failed authentication (HTTP 401). Unlike a transient
    error this won't self-heal on retry — the subscription token is expired or
    revoked and needs an interactive `claude` → /login to refresh."""


def _looks_like_auth_failure(*chunks: str) -> bool:
    blob = "\n".join(c for c in chunks if c).lower()
    return ("401" in blob and "auth" in blob) or "invalid authentication" in blob \
        or "failed to authenticate" in blob


def _looks_like_model_unavailable(*chunks: str) -> bool:
    """The CLI rejected the requested model outright (pulled from the
    subscription, no access, bad ID) — as opposed to a transient failure.
    Heuristic; call_claude retries on the fallback for any non-auth error
    anyway, this only decides whether to stop trying the preferred model
    for the rest of the process."""
    blob = "\n".join(c for c in chunks if c).lower()
    return ("not_found" in blob or "404" in blob
            or ("model" in blob and any(s in blob for s in (
                "not available", "no access", "does not exist",
                "invalid", "unknown", "not supported"))))


def _clear_auth_sentinel() -> None:
    """A successful call proves auth is healthy — drop any stale alert flag so
    the dedupe resets for next time."""
    try:
        AUTH_ALERT_SENTINEL.unlink(missing_ok=True)
    except OSError:
        pass


# Demoted to CLAUDE_FALLBACK_MODEL once the CLI rejects the preferred model,
# so later calls in the same process skip the doomed first attempt. Weekend
# slots are one call per process, so each slot re-probes the preferred model —
# exactly what we want when Fable's availability changes between weekends.
_active_model = CLAUDE_MODEL


def _run_claude(prompt: str, model: str, allow_web: bool) -> "subprocess.CompletedProcess[str]":
    exe = find_claude_exe()
    cmd = [exe, "-p", "--model", model]
    if allow_web:
        cmd += ["--allowedTools", "WebSearch,WebFetch"]
    child_env = os.environ.copy()
    child_env.pop("ANTHROPIC_API_KEY", None)
    return subprocess.run(
        cmd,
        input=prompt,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=CLAUDE_TIMEOUT_SEC,
        env=child_env,
    )


def call_claude(prompt: str, allow_web: bool = True) -> str:
    """Run prompt through `claude -p`. Prompt goes via stdin to dodge the
    8191-char Windows command-line limit.

    Tries CLAUDE_MODEL first and falls back to CLAUDE_FALLBACK_MODEL on any
    non-auth failure, so unattended slots still complete on weekends when the
    preferred model isn't available on the subscription.

    Strips ANTHROPIC_API_KEY from just the subprocess env so the CLI falls
    back to Claude Code subscription auth (no API credits used). The parent
    shell keeps the key for whatever else needs it."""
    global _active_model
    model = _active_model
    result = _run_claude(prompt, model, allow_web)

    if result.returncode != 0 and model != CLAUDE_FALLBACK_MODEL:
        if _looks_like_auth_failure(result.stdout, result.stderr):
            pass  # fall through — a model switch won't fix a dead token
        else:
            if _looks_like_model_unavailable(result.stdout, result.stderr):
                # Preferred model is gone — stop probing it for this process.
                _active_model = CLAUDE_FALLBACK_MODEL
                print(f"[{_ts()}] Model {model} unavailable — using "
                      f"{CLAUDE_FALLBACK_MODEL} for the rest of this run.")
            else:
                print(f"[{_ts()}] {model} call failed (exit "
                      f"{result.returncode}) — retrying once on "
                      f"{CLAUDE_FALLBACK_MODEL}.")
            model = CLAUDE_FALLBACK_MODEL
            result = _run_claude(prompt, model, allow_web)

    if result.returncode != 0:
        detail = (f"model: {model}\n"
                  f"stdout:\n{result.stdout}\n"
                  f"stderr:\n{result.stderr}")
        if _looks_like_auth_failure(result.stdout, result.stderr):
            raise AuthError(
                "Claude CLI authentication failed (HTTP 401) — the subscription "
                "token is expired or revoked. Run `claude` then /login to refresh.\n"
                + detail)
        raise RuntimeError(f"claude -p exited {result.returncode}\n" + detail)
    _clear_auth_sentinel()   # success ⇒ auth is healthy
    return result.stdout.strip()


# ── Alerting & auth pre-flight ──────────────────────────────────────────────

def _push_ntfy(title: str, message: str) -> None:
    if not NOTIFY_NTFY_TOPIC:
        return
    url = (NOTIFY_NTFY_TOPIC if NOTIFY_NTFY_TOPIC.startswith("http")
           else f"https://ntfy.sh/{NOTIFY_NTFY_TOPIC}")
    try:
        req = urllib.request.Request(
            url, data=message.encode("utf-8"),
            headers={"Title": title, "Priority": "high", "Tags": "warning"})
        urllib.request.urlopen(req, timeout=15)
        print(f"[{_ts()}] Alert pushed via ntfy.")
    except Exception as e:
        print(f"[{_ts()}] ntfy push failed: {e}")


def _push_webhook(title: str, message: str) -> None:
    if not NOTIFY_WEBHOOK_URL:
        return
    text = f"{title}: {message}"
    # Slack reads {"text"}, Discord reads {"content"} — send both; each ignores the other.
    payload = json.dumps({"text": text, "content": text}).encode("utf-8")
    try:
        req = urllib.request.Request(
            NOTIFY_WEBHOOK_URL, data=payload,
            headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=15)
        print(f"[{_ts()}] Alert pushed via webhook.")
    except Exception as e:
        print(f"[{_ts()}] webhook push failed: {e}")


def _desktop_toast(title: str, message: str) -> None:
    """Best-effort Windows balloon — handy for the Friday pre-flight when you're
    likely at the machine. Non-blocking; silently no-ops if it can't render."""
    if os.name != "nt":
        return
    t, m = title.replace("'", "''"), message.replace("'", "''")
    ps = (
        "[reflection.assembly]::loadwithpartialname('System.Windows.Forms')|Out-Null;"
        "$n=New-Object System.Windows.Forms.NotifyIcon;"
        "$n.Icon=[System.Drawing.SystemIcons]::Warning;$n.Visible=$true;"
        f"$n.ShowBalloonTip(20000,'{t}','{m}',"
        "[System.Windows.Forms.ToolTipIcon]::Warning);Start-Sleep -Seconds 6;$n.Dispose()"
    )
    try:
        subprocess.Popen(
            ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", ps],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def notify(title: str, message: str, *, dedupe: bool = False) -> None:
    """Best-effort alert across every configured channel. Never raises — a
    failed alert must not take down the run. With dedupe=True it fires at most
    once per cycle (sentinel-guarded), so the ~16 weekend slots don't each push;
    the sentinel clears on the next successful Claude call."""
    print(f"[{_ts()}] ALERT: {title} — {message}")
    if dedupe:
        if AUTH_ALERT_SENTINEL.exists():
            print(f"[{_ts()}] (already alerted this cycle — suppressing push.)")
            return
        try:
            AUTH_ALERT_SENTINEL.parent.mkdir(parents=True, exist_ok=True)
            AUTH_ALERT_SENTINEL.write_text(
                f"{_ts()}  {title} — {message}\n", encoding="utf-8")
        except OSError:
            pass
    _push_ntfy(title, message)
    _push_webhook(title, message)
    _desktop_toast(title, message)


def _read_token_expiry() -> tuple[datetime | None, float | None]:
    """Best-effort (expiry_datetime, days_remaining) from the stored OAuth
    creds, or (None, None). Reads ONLY the timestamp — never token material.
    Caveat: this is whatever Claude Code persists as `expiresAt`; if that
    tracks the short-lived access token it reads near-future every time, so
    treat the live check in run_preflight as the authoritative signal."""
    try:
        data = json.loads(CREDENTIALS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, None
    oauth = data.get("claudeAiOauth", data) if isinstance(data, dict) else {}
    exp = oauth.get("expiresAt") or oauth.get("expires_at")
    if not isinstance(exp, (int, float)):
        return None, None
    ts = exp / 1000 if exp > 1e11 else exp     # ms vs s epoch
    try:
        when = datetime.fromtimestamp(ts, ET)
    except (OverflowError, OSError, ValueError):
        return None, None
    return when, (when - datetime.now(ET)).total_seconds() / 86400


def token_expiry() -> tuple[date | None, int | None]:
    """Parse CLAUDE_CODE_OAUTH_TOKEN_EXPIRES (YYYY-MM-DD) → (date, days_left), or
    (None, None) if unset/malformed. This is the recorded expiry of the opaque
    long-lived setup-token, not anything read from the credential store."""
    raw = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN_EXPIRES", "").strip()
    if not raw:
        return None, None
    try:
        d = date.fromisoformat(raw)
    except ValueError:
        return None, None
    return d, (d - datetime.now(ET).date()).days


def token_countdown_line() -> str:
    """One-line report footer for the long-lived auth token's countdown, or ""
    when no expiry is configured (so reports are unchanged without it)."""
    d, days = token_expiry()
    if d is None:
        return ""
    warn = (" ⚠ regenerate soon with `claude setup-token`"
            if days is not None and days <= TOKEN_WARN_DAYS else "")
    return f"_Auth token expires {d.isoformat()} ({days} day(s) left){warn}_\n"


def run_preflight(args) -> None:
    """Manual, on-demand auth check — handy right after `claude setup-token` to
    confirm the new long-lived token authenticates (key stripped, same model as
    the weekend run) and to print its recorded expiry. Not scheduled: the weekend
    run's in-run AuthError backstop and the assembly task's expiry countdown cover
    the unattended path. Exits 0 if healthy, 1 if it raised an alert."""
    # Logged purely as a breadcrumb. The stored `expiresAt` tracks the SHORT-LIVED
    # access token (reads only hours out and refreshes on every call), so it can't
    # forewarn the refresh-token expiry that actually forces a re-login — hence no
    # alert keys off it. Over time this line still shows the access token rolling.
    when, days = _read_token_expiry()
    if when is not None:
        print(f"[{_ts()}] Stored access-token expiresAt: {when.isoformat()} "
              f"({days:+.1f} day(s) from now; informational only).")
    else:
        print(f"[{_ts()}] Token expiry not readable — relying on the live check.")

    # The authoritative test: mirror the weekend worker exactly (key stripped,
    # same model) so a pass here means the weekend run will authenticate too.
    status, err = "ok", None
    try:
        call_claude("Reply with the single word: OK", allow_web=False)
        print(f"[{_ts()}] Live auth check passed — token is valid.")
    except AuthError as e:
        status, err = "dead", e
    except Exception as e:                       # exe missing, timeout, etc.
        status, err = "error", e

    if status == "dead":
        print(f"[{_ts()}] Live auth check FAILED (401).")
        notify("Thesis agent: re-login needed",
               "Subscription auth is DEAD (401). Run `claude` then /login before "
               "the weekend, or the run will produce no report.")
        sys.exit(1)
    if status == "error":
        print(f"[{_ts()}] Pre-flight inconclusive (non-auth error): {err}")
        sys.exit(1)

    # Long-lived token countdown. Unlike the access-token field above this is a
    # real ~1-year expiry, so warning off it is meaningful — regenerate before
    # it lapses and the weekend run loses its fallback auth.
    exp_date, days_left = token_expiry()
    if exp_date is not None:
        print(f"[{_ts()}] Long-lived auth token expires {exp_date.isoformat()} "
              f"({days_left} day(s) left).")
        if days_left is not None and days_left <= TOKEN_WARN_DAYS:
            notify("Thesis agent: auth token expiring",
                   f"The long-lived token expires in {days_left} day(s) "
                   f"({exp_date.isoformat()}). Regenerate with `claude setup-token` "
                   "and update CLAUDE_CODE_OAUTH_TOKEN(_EXPIRES) in .env.")
            sys.exit(1)
    else:
        print(f"[{_ts()}] No CLAUDE_CODE_OAUTH_TOKEN_EXPIRES set — skipping token countdown.")
    print(f"[{_ts()}] Pre-flight OK — token authenticates.")
    sys.exit(0)


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

def _alpaca_invalid_symbol(error_text: str, symbols: list[str]) -> str | None:
    """Extract the symbol Alpaca rejected from an 'invalid symbol: X' error,
    but only if it's actually one we asked for (guards against misparsing)."""
    m = re.search(r"invalid symbol:?\s*([A-Za-z0-9.\-]+)", error_text, re.IGNORECASE)
    if m and m.group(1).upper() in symbols:
        return m.group(1).upper()
    return None


def _fetch_alpaca_52w(tickers: list[str]) -> dict:
    """Pull ~1Y of daily bars from Alpaca for all tickers in one batched
    request. Returns {ticker: {price, low_52w, high_52w, source}} for tickers
    that came back with data."""
    out: dict[str, dict] = {}
    if not (ALPACA_KEY and ALPACA_SECRET):
        print("  Alpaca credentials not set (ALPACA_API_KEY / ALPACA_API_SECRET, "
              "via env or .env) — using yfinance for all tickers")
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

    # One non-US symbol (e.g. SAAB-B.ST) makes Alpaca reject the ENTIRE batched
    # request, which would silently push every US ticker onto yfinance and invite
    # rate limiting. Alpaca names the offender in the error — drop it and retry
    # so the rest of the batch still gets served.
    symbols = list(tickers)
    bars = None
    while symbols:
        try:
            request = StockBarsRequest(
                symbol_or_symbols=symbols,
                timeframe=TimeFrame.Day,
                start=start,
                end=end,
                feed=feed,
            )
            bars = client.get_stock_bars(request)
            break
        except Exception as e:
            bad = _alpaca_invalid_symbol(str(e), symbols)
            if bad:
                print(f"  Alpaca rejected {bad} — retrying batch without it")
                symbols.remove(bad)
                continue
            print(f"Alpaca fetch failed: {e} — falling back to yfinance for all tickers")
            return out
    if bars is None:
        return out

    for symbol in symbols:
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


YF_ATTEMPTS = 3          # per-ticker tries before recording an error
YF_BACKOFF_BASE_SEC = 2  # sleeps 2s, then 4s between retries


def _yf_history_1y(ticker: str):
    """Fetch ~1y of daily bars from yfinance with retry + backoff. Yahoo
    intermittently rate-limits or returns empty frames; a couple of spaced
    retries recovers most of those. Returns (history_df_or_None, error_or_None)."""
    last_err = None
    for attempt in range(YF_ATTEMPTS):
        try:
            hist = yf.Ticker(ticker).history(period="1y")
            if not hist.empty:
                return hist, None
            last_err = "no data (delisted, OTC, or invalid?)"
        except Exception as e:
            last_err = str(e)
        if attempt < YF_ATTEMPTS - 1:
            time.sleep(YF_BACKOFF_BASE_SEC * (attempt + 1))
    return None, last_err


def _fetch_yfinance_52w(tickers: list[str]) -> dict:
    """yfinance fallback. Slower (one request per ticker) but covers OTC,
    foreign, and anything Alpaca rejects."""
    out: dict[str, dict] = {}
    for ticker in tickers:
        hist, err = _yf_history_1y(ticker)
        if hist is None:
            out[ticker] = {"error": err}
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
    failed = sorted(t for t, info in out.items() if "error" in info)
    if failed:
        print(f"  yfinance returned no usable data for: {', '.join(failed)}")
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
TICKER_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")
SCAN_THESIS_RE = re.compile(r"^###\s*Thesis:\s*(.+?)\s*$", re.MULTILINE)


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
            if t and TICKER_RE.match(t):
                found.append(t)
    return found


def parse_scan_suggestions(response: str) -> list[dict]:
    """Parse a new-thesis-scan response into [{title, tickers}], pairing each
    '### Thesis:' heading with the tickers_to_research block in its segment."""
    out: list[dict] = []
    matches = list(SCAN_THESIS_RE.finditer(response))
    for i, m in enumerate(matches):
        seg_end = matches[i + 1].start() if i + 1 < len(matches) else len(response)
        segment = response[m.start():seg_end]
        tickers: list[str] = []
        for ym in YAML_BLOCK_RE.finditer(segment):
            try:
                data = yaml.safe_load(ym.group(1))
            except yaml.YAMLError:
                continue
            if isinstance(data, dict) and "tickers_to_research" in data:
                tickers = [t.strip().upper() for t in _walk_strings(data)
                           if t.strip() and TICKER_RE.match(t.strip().upper())]
        out.append({"title": m.group(1).strip(), "tickers": tickers})
    return out


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
                          prior_context: str | None = None,
                          past_cats: list[dict] | None = None) -> str:
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

    # When previously flagged catalysts have come due, demand a structured verdict
    # on each so resolution accuracy can be tracked over time (not just prose).
    outcomes_block, outcomes_rule = "", ""
    if past_cats:
        listed = "; ".join(f"{c['date']} {c['what']}" for c in past_cats)
        outcomes_block = (
            "\ncatalyst_outcomes:       # one verdict per past-due catalyst from the continuity block\n"
            '  - {date: 2026-06-29, what: "the event as originally flagged", '
            'outcome: for, note: "one line on what actually happened"}'
        )
        outcomes_rule = (
            f"\n- `catalyst_outcomes` must contain exactly one entry per past-due catalyst "
            f"({listed}). `outcome` is exactly one of: for | against | mixed | pending. "
            "for/against = the event resolved in favor of / against the thesis; mixed = ambiguous; "
            "pending = postponed or hasn't actually happened yet — re-list pending events under "
            "`catalysts` with the new expected date."
        )

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
  - {{date: 2026-08-15, what: "what happens on/around then"}}{outcomes_block}
```

CRITICAL FORMATTING RULES:
- The Indexes/Stocks yaml blocks must use plain ASCII tickers (e.g. NVDA, SMH, TLRY). For non-US listings, use the full Yahoo-Finance-style symbol (e.g. TLRY.TO, SHOP.TO, ASML.AS).
- Do not put commentary inside the yaml blocks — only the ticker lists.
- If a side is empty (e.g. no clear downside indexes), use an empty list `[]`, do not omit the key.
- Only the Indexes/Stocks lists and the final metadata block use yaml. Do NOT output a mock portfolio, scenario matrix, or trigger logic in this pass — those are built in a second pass once live prices are available.
- The metadata block must have an integer `conviction` and ISO-format (YYYY-MM-DD) `catalysts` dates.
- `catalysts` is forward-looking: upcoming events only — do not re-list catalysts that have already resolved.{outcomes_rule}
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


SCAN_PROMPT_SUGGESTION_LIMIT = 12  # most recent prior suggestions fed back for dedup


def build_new_thesis_prompt(strategy_preamble: str, existing_titles: list[str], allow_web: bool,
                            prior_suggestions: list[dict] | None = None) -> str:
    today = datetime.now(ET).strftime("%A, %B %d, %Y")
    existing = "\n".join(f"- {t}" for t in existing_titles) or "(none)"
    prior_block = ""
    if prior_suggestions:
        recent = prior_suggestions[-SCAN_PROMPT_SUGGESTION_LIMIT:]
        listed = "\n".join(f"- {s.get('date', '?')}: {s.get('title', '')}" for s in recent)
        prior_block = f"""

IDEAS YOUR PREVIOUS SCANS ALREADY SUGGESTED (the investor reviewed these and did not adopt them — do NOT re-suggest them or close variants; bring genuinely fresh angles):
{listed}"""
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
{existing}{prior_block}

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
# per thesis: conviction (1-5) + direction, catalysts (plus structured verdicts on
# catalysts that came due), the mock-portfolio holdings with entry prices, the
# realized weekly return of the PRIOR week's holdings, a running time-weighted
# equity index (100 at inception), and a period-paired benchmark (BENCHMARK_TICKER)
# price + index so performance reads against the market. This single store powers
# week-over-week continuity (#3), the backtest ledger / calibration (#5), and the
# catalyst calendar / resolution tracking.
# Theses are matched across weeks by a normalized title key.

CALIBRATION_MIN_PAIRS = 8  # need this many conviction→next-week-return pairs before reporting calibration

# Structured verdicts on past-due catalysts (see build_analysis_prompt). Anything
# else the model emits is coerced to "unscored" rather than polluting the stats.
CATALYST_OUTCOMES = {"for", "against", "mixed", "pending"}


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
    outcomes = []
    for c in (meta.get("catalyst_outcomes") or []):
        if isinstance(c, dict):
            verdict = str(c.get("outcome", "")).strip().lower()
            outcomes.append({
                "date": str(c.get("date", "")),
                "what": str(c.get("what", "")),
                "outcome": verdict if verdict in CATALYST_OUTCOMES else "unscored",
                "note": str(c.get("note", "") or ""),
            })
    return {
        "conviction": conv,
        "direction": str(meta.get("direction", "n/a")).lower(),
        "conviction_note": str(meta.get("conviction_note", "") or ""),
        "catalysts": cats,
        "catalyst_outcomes": outcomes,
    }


def past_catalysts(rec: dict, today: str) -> list[dict]:
    """Prior-flagged catalysts whose date has come due (date <= today) — the ones
    the next analysis pass must deliver a structured verdict on."""
    out = []
    for c in (rec.get("catalysts") or []):
        d = str(c.get("date", ""))
        if d and d <= today:
            out.append({"date": d, "what": str(c.get("what", ""))})
    return out


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


def load_scan_suggestions(before_date: str) -> list[dict]:
    """Every past new-thesis-scan suggestion from sidecars dated < before_date,
    oldest first, deduped by normalized title (the original suggestion wins so
    its track record starts from the first time the idea surfaced)."""
    seen: set[str] = set()
    out: list[dict] = []
    for p in sorted(REPORTS_DIR.glob("*_research.json")):
        d = p.name[:10]
        if not (len(d) == 10 and d < before_date):
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        for s in data.get("scan_suggestions", []):
            key = s.get("key") or thesis_key(s.get("title", ""))
            if key and key not in seen:
                seen.add(key)
                out.append({**s, "key": key, "date": s.get("date") or d})
    return out


def build_suggestion_records(suggestions: list[dict], enrichment: dict, report_date: str) -> list[dict]:
    """Sidecar records for this run's scan suggestions, snapshotting the price of
    every priceable suggested ticker so future runs can mark the basket."""
    records = []
    for s in suggestions:
        prices = {t: enrichment[t]["price"] for t in s.get("tickers", [])
                  if _is_priced(enrichment.get(t))}
        records.append({"key": thesis_key(s["title"]), "title": s["title"],
                        "date": report_date, "tickers": s.get("tickers", []),
                        "prices": prices})
    return records


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


def _md_cell(text: str) -> str:
    """Make free text safe for a markdown table cell (pipes would split it)."""
    return str(text).replace("|", "/").replace("\n", " ").strip()


def render_catalyst_calendar(records: list[dict], report_date: str) -> str | None:
    """Aggregate every thesis's upcoming catalysts into one forward-looking
    timeline, nearest first. Returns None when there's nothing to show."""
    try:
        anchor = date.fromisoformat(report_date)
    except ValueError:
        return None
    rows = []
    for rec in records:
        for c in (rec.get("catalysts") or []):
            try:
                d = date.fromisoformat(str(c.get("date", "")))
            except ValueError:
                continue  # unparseable date — already visible in the thesis section
            if d >= anchor:
                rows.append((d, rec.get("title", ""), str(c.get("what", ""))))
    if not rows:
        return None
    rows.sort(key=lambda r: (r[0], r[1]))
    lines = ["## Catalyst Calendar", "",
             "_Every upcoming catalyst across the book, nearest first._", "",
             "| Date | In | Thesis | Catalyst |",
             "|------|-----|--------|----------|"]
    for d, title, what in rows:
        days = (d - anchor).days
        when = "today" if days == 0 else f"{days}d"
        lines.append(f"| {d.isoformat()} | {when} | {_md_cell(title)} | {_md_cell(what)} |")
    return "\n".join(lines)


OUTCOME_MARKS = {"for": "✓", "against": "✗", "mixed": "~", "pending": "⏳", "unscored": "?"}


def render_outcome_scorecard(outcomes: list[dict]) -> str:
    """Compact one-liner of this week's catalyst verdicts for a thesis section."""
    parts = []
    for o in outcomes:
        mark = OUTCOME_MARKS.get(o["outcome"], "?")
        part = f"{mark} {o['what']} ({o['date']}) — **{o['outcome']}**"
        if o["note"]:
            part += f": _{o['note']}_"
        parts.append(part)
    return "**Catalyst verdicts:** " + " · ".join(parts)


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


def render_performance_block(prior_date, legs, weekly_return, equity_index, inception_date,
                             bench_weekly=None, bench_index=None) -> str:
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
    wk_b = f" (vs {BENCHMARK_TICKER} {bench_weekly * 100:+.1f}%)" if bench_weekly is not None else ""
    si_b = (f" vs {BENCHMARK_TICKER} {bench_index - 100:+.1f}%"
            if isinstance(bench_index, (int, float)) else "")
    summary = (f"\n\n**Thesis weekly return:** {wk}{wk_b} (prior portfolio from {prior_date}) · "
               f"**Since inception ({inception_date}):** {equity_index - 100:+.1f}%{si_b} "
               f"(index {equity_index:.1f})")
    return "### Portfolio performance\n\n" + "\n".join(head + rows) + summary


def compute_and_render(results: list[dict], enrichment: dict, report_date: str) -> tuple[list[str], list[dict]]:
    """Shared post-processing for both flows. For each normalized thesis result
    {key, title, meta, holdings, rendered_analysis, portfolio_text}, compute the
    week-over-week performance + running equity index (and the matching benchmark
    index), render the full section, and build the sidecar record. Returns
    (sections aligned to results, sidecar records)."""
    bench_info = enrichment.get(BENCHMARK_TICKER)
    bench_now = bench_info["price"] if _is_priced(bench_info) else None
    if bench_now is None:
        print(f"[{_ts()}]   Warning: no live {BENCHMARK_TICKER} price — "
              "benchmark comparison unavailable this run.")

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
            # Benchmark over the same span: prior report's benchmark price → now.
            # Only advance the benchmark index when the equity index also advanced,
            # so the two stay period-paired and comparable.
            prior_bench = (prec.get("benchmark_price")
                           if prec.get("benchmark_ticker") == BENCHMARK_TICKER else None)
            bench_index = prec.get("benchmark_index") or 100.0
            bench_weekly = (bench_now / prior_bench - 1.0
                            if bench_now and prior_bench else None)
            if weekly is not None and bench_weekly is not None:
                bench_index *= 1 + bench_weekly
        else:
            prior_date, legs, weekly = None, [], None
            equity_index, inception_date, prior_conv = 100.0, report_date, None
            bench_weekly, bench_index = None, 100.0

        perf = render_performance_block(prior_date, legs, weekly, equity_index, inception_date,
                                        bench_weekly, bench_index)
        conv_line = render_conviction_line(meta, prior_conv)
        verdicts = (render_outcome_scorecard(meta["catalyst_outcomes"]) + "\n\n"
                    if meta["catalyst_outcomes"] else "")
        sections.append(
            f"## Thesis: {title}\n\n{conv_line}\n\n{verdicts}"
            f"{r['rendered_analysis']}\n\n{r['portfolio_text']}\n\n{perf}"
        )
        records.append({
            "key": key, "title": title, "inception_date": inception_date,
            "conviction": meta["conviction"], "direction": meta["direction"],
            "conviction_note": meta["conviction_note"], "catalysts": meta["catalysts"],
            "catalyst_outcomes": meta["catalyst_outcomes"],
            "holdings": r["holdings"], "weekly_return": weekly,
            "equity_index": round(equity_index, 4), "prior_report_date": prior_date,
            "benchmark_ticker": BENCHMARK_TICKER,
            "benchmark_price": round(bench_now, 4) if bench_now is not None else None,
            "benchmark_index": round(bench_index, 4),
        })
    return sections, records


def render_calibration_section(current_records: list[dict], report_date: str,
                               scan_suggestions: list[dict] | None = None,
                               enrichment: dict | None = None) -> str:
    """Book-level ledger + conviction calibration, reading all past sidecars plus
    this run's in-memory records. Calibration pairs conviction[T] with the realized
    weekly_return[T+1] of the same thesis. When scan_suggestions is given (past
    suggestions + current prices in enrichment), appends the scanner's track record."""
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
        bi = rec.get("benchmark_index")
        wk_s = f"{wk * 100:+.1f}%" if isinstance(wk, (int, float)) else "—"
        si_s = f"{eq - 100:+.1f}%" if isinstance(eq, (int, float)) else "—"
        rel_s = (f"{eq - bi:+.1f} pp"
                 if isinstance(eq, (int, float)) and isinstance(bi, (int, float)) else "—")
        conv = rec.get("conviction")
        ledger.append(f"| {rec.get('title', k)} | {conv if conv is not None else '—'}/5 | "
                      f"{wk_s} | {si_s} | {rel_s} |")

    buckets: dict[int, list[float]] = {c: [] for c in range(1, 6)}
    pairs = 0
    for items in timeline.values():
        for (_, r0), (_, r1) in zip(items, items[1:]):
            c, ret = r0.get("conviction"), r1.get("weekly_return")
            if isinstance(c, int) and c in buckets and isinstance(ret, (int, float)):
                buckets[c].append(ret)
                pairs += 1

    # Catalyst resolutions across all history. A verdict can be re-reported in a
    # later week (the model re-assessing); dedup on (thesis, event date, slugged
    # event text) keeps the latest verdict since reports are walked in date order.
    resolutions: dict[tuple, dict] = {}
    for k, items in timeline.items():
        for d, rec in items:
            for o in (rec.get("catalyst_outcomes") or []):
                dedup = (k, o.get("date", ""), thesis_key(o.get("what", ""))[:40])
                resolutions[dedup] = {"thesis": rec.get("title", k), "reported": d, **o}

    out = ["## Conviction Calibration & Ledger", "",
           "_Educational backtest of the hypothetical mock portfolios — not financial advice._", "",
           "### Ledger (latest per thesis)", "",
           f"| Thesis | Conviction | Last weekly return | Since inception | vs {BENCHMARK_TICKER} |",
           "|--------|-----------|--------------------|-----------------|--------|"]
    out += ledger or ["| _no theses tracked yet_ | — | — | — | — |"]
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

    out += ["", "### Catalyst resolutions", ""]
    if not resolutions:
        out.append("_No flagged catalysts have come due yet — verdicts accrue as catalyst dates pass._")
    else:
        res = sorted(resolutions.values(), key=lambda r: r.get("date", ""), reverse=True)
        counts = Counter(r["outcome"] for r in res)
        decided = counts["for"] + counts["against"] + counts["mixed"]
        rate = (f" — **{counts['for'] / decided * 100:.0f}%** broke the thesis's way"
                if decided else "")
        out += [f"{len(res)} verdict(s): {counts['for']} for · {counts['against']} against · "
                f"{counts['mixed']} mixed · {counts['pending']} pending{rate}", "",
                "| Event date | Thesis | Catalyst | Outcome | What happened |",
                "|-----------|--------|----------|---------|---------------|"]
        for r in res[:12]:
            out.append(f"| {r.get('date', '—')} | {_md_cell(r['thesis'])} | {_md_cell(r['what'])} | "
                       f"{r['outcome']} | {_md_cell(r['note']) or '—'} |")
        if len(res) > 12:
            out.append(f"\n_…and {len(res) - 12} earlier verdicts._")

    if scan_suggestions is not None:
        out += ["", "### New-thesis scan track record", ""]
        if not scan_suggestions:
            out.append("_No prior scan suggestions tracked yet — each weekly scan's ideas "
                       "are recorded and marked to market from here on._")
        else:
            out += ["_Equal-weight basket of each past suggestion's tickers, marked from "
                    "the suggestion date to now — a track record for the scanner, not advice._", "",
                    "| Suggested | Idea | Basket since | Priced |",
                    "|-----------|------|--------------|--------|"]
            newest_first = sorted(scan_suggestions, key=lambda s: s.get("date", ""), reverse=True)
            for s in newest_first[:15]:
                rets = []
                for t, p0 in (s.get("prices") or {}).items():
                    info = (enrichment or {}).get(t)
                    if _is_priced(info) and p0:
                        rets.append(info["price"] / p0 - 1.0)
                basket = f"{sum(rets) / len(rets) * 100:+.1f}%" if rets else "—"
                out.append(f"| {s.get('date', '—')} | {_md_cell(s.get('title', ''))} | {basket} | "
                           f"{len(rets)}/{len(s.get('tickers') or [])} |")
            if len(newest_first) > 15:
                out.append(f"\n_…and {len(newest_first) - 15} earlier suggestions._")
    return "\n".join(out)


def write_sidecar(report_date: str, records: list[dict],
                  scan_suggestions: list[dict] | None = None) -> None:
    """Persist the structured per-thesis history sidecar next to the report."""
    payload = {"report_date": report_date,
               "generated_at": datetime.now(ET).isoformat(),
               "theses": records,
               "scan_suggestions": scan_suggestions or []}
    _write_json_atomic(REPORTS_DIR / f"{report_date}_research.json", payload)


def deliver_to_obsidian(report_date: str, report_text: str) -> None:
    """Mirror a saved report into the Obsidian vault with minimal frontmatter.
    Best-effort: delivery problems are logged but never fail the run — the
    canonical copy in reports/ is already on disk by the time this runs."""
    if not OBSIDIAN_VAULT_DIR:
        return
    try:
        vault = Path(OBSIDIAN_VAULT_DIR)
        if not vault.exists():
            print(f"[{_ts()}] Obsidian vault not found at {vault} — skipping delivery.")
            return
        dest_dir = vault / OBSIDIAN_REPORTS_SUBDIR
        dest_dir.mkdir(parents=True, exist_ok=True)
        frontmatter = f"---\ndate: {report_date}\ntags: [market-research]\n---\n\n"
        dest = dest_dir / f"{report_date}_research.md"
        dest.write_text(frontmatter + report_text, encoding="utf-8")
        print(f"[{_ts()}] Report delivered to Obsidian → {dest}")
    except OSError as e:
        print(f"[{_ts()}] Obsidian delivery failed (report still saved locally): {e}")


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
        past_cats = past_catalysts(prior[1], report_date) if prior else []
        if prior_context:
            print(f"[{_ts()}]   Continuity: threading in prior take from {prior[0]}"
                  + (f" ({len(past_cats)} catalyst(s) due a verdict)." if past_cats else "."))
        analysis_raw = call_claude(
            build_analysis_prompt(preamble, thesis, allow_web, prior_context, past_cats),
            allow_web=allow_web)
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

    # Benchmark for performance comparison — enrich it unless a thesis already did.
    if not _is_priced(combined_enrichment.get(BENCHMARK_TICKER)):
        combined_enrichment.update(enrich_tickers([BENCHMARK_TICKER]))

    sections, records = compute_and_render(results, combined_enrichment, report_date)
    calendar = render_catalyst_calendar(records, report_date)
    if calendar:
        sections.insert(0, calendar)

    prior_suggestions = load_scan_suggestions(report_date)

    scan_records: list[dict] = []
    if not args.skip_new_thesis_scan and not args.thesis:
        print(f"[{_ts()}] Running new-thesis scan...")
        prompt = build_new_thesis_prompt(preamble, [t["title"] for t in theses], allow_web,
                                         prior_suggestions)
        response = call_claude(prompt, allow_web=allow_web)
        scan_enrichment = enrich_tickers(extract_tickers_from_response(response))
        sections.append(replace_yaml_blocks_with_tables(response, scan_enrichment))
        scan_records = build_suggestion_records(parse_scan_suggestions(response),
                                                scan_enrichment, report_date)

    # Mark past scan suggestions to current prices for the track record.
    sugg_missing = sorted({t for s in prior_suggestions for t in (s.get("tickers") or [])
                           if not _is_priced(combined_enrichment.get(t))})
    if sugg_missing:
        combined_enrichment.update(enrich_tickers(sugg_missing))

    sections.append(render_calibration_section(records, report_date,
                                               prior_suggestions, combined_enrichment))

    header = (f"# Market Research — {report_date}\n\n"
              f"_Generated {datetime.now(ET).strftime('%Y-%m-%d %H:%M %Z')} from {theses_path.name}_\n"
              f"{token_countdown_line()}")
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
            write_sidecar(report_date, records, scan_records)
            print(f"[{_ts()}] Report saved → {out} (+ history sidecar).")
        deliver_to_obsidian(report_date, report)

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
            past_cats = past_catalysts(prior[1], report_date) if prior else []
            analysis_raw = call_claude(
                build_analysis_prompt(preamble, thesis, allow_web, prior_context, past_cats),
                allow_web=allow_web)
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
                build_new_thesis_prompt(preamble, existing, allow_web,
                                        load_scan_suggestions(report_date)),
                allow_web=allow_web)
    except AuthError as e:
        # Global auth failure, not this thesis's fault — undo the attempt bump so
        # a dead token can't exhaust the per-thesis budget, and alert once.
        unit["attempts"] -= 1
        unit["last_error"] = str(e)[:1000]
        save_manifest(mpath, manifest)
        notify("Thesis agent: weekend run blocked",
               f"Auth died mid-batch (401) on {unit['id']}. Re-login with "
               "`claude` /login; the remaining slots will resume automatically.",
               dedupe=True)
        print(f"[{_ts()}] AUTH FAILURE — {unit['id']} left pending; attempt budget preserved.")
        print(f"[{_ts()}] {e}")
        sys.exit(1)
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
    all_tickers.append(BENCHMARK_TICKER)  # benchmark shares the same price snapshot
    prior_suggestions = load_scan_suggestions(report_date)
    all_tickers += [t for s in prior_suggestions for t in (s.get("tickers") or [])]
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
    scan_records: list[dict] = []
    for u in units:
        if u["status"] == "done":
            if u["kind"] == "thesis":
                sections.append(sec_by_id[u["id"]])
            else:
                raw = u.get("raw_response") or ""
                sections.append(replace_yaml_blocks_with_tables(raw, enrichment))
                scan_records = build_suggestion_records(parse_scan_suggestions(raw),
                                                        enrichment, report_date)
        else:
            err = f" Last error: {u['last_error']}" if u.get("last_error") else ""
            placeholder = f"> ⚠ Not researched this batch — {u['attempts']} attempt(s).{err}"
            if u["kind"] == "thesis":
                sections.append(f"## Thesis: {u['title']}\n\n{placeholder}")
            else:
                sections.append(f"## Suggested new theses\n\n{placeholder}")

    calendar = render_catalyst_calendar(records, report_date)
    if calendar:
        sections.insert(0, calendar)

    sections.append(render_calibration_section(records, report_date,
                                               prior_suggestions, enrichment))

    header = (f"# Market Research — {report_date}\n\n"
              f"_Weekend batch assembled {datetime.now(ET).strftime('%Y-%m-%d %H:%M %Z')} "
              f"from {manifest['theses_source']} · "
              f"{len(done)}/{len(units)} units researched_\n"
              f"{token_countdown_line()}")
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
    write_sidecar(report_date, records, scan_records)
    print(f"[{_ts()}] Report saved → {out} (+ history sidecar).")
    deliver_to_obsidian(report_date, report)

    # Forward-looking auth warning, folded into the weekly assembly: the report
    # header already shows the countdown; push it when the long-lived token is
    # within TOKEN_WARN_DAYS so a yearly regen never sneaks up. Weekly cadence
    # means a few nudges before expiry — no dedupe (each is a fresh reminder).
    exp_date, days_left = token_expiry()
    if exp_date is not None and days_left is not None and days_left <= TOKEN_WARN_DAYS:
        notify("Thesis agent: auth token expiring",
               f"The long-lived token expires in {days_left} day(s) "
               f"({exp_date.isoformat()}). Regenerate with `claude setup-token` "
               "and update CLAUDE_CODE_OAUTH_TOKEN(_EXPIRES) in .env.")

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
    mode.add_argument("--preflight", action="store_true",
                      help="Manual auth check: verify the token authenticates and print its expiry (e.g. after `claude setup-token`)")
    args = parser.parse_args()

    print(f"\n{'=' * 60}")
    if args.preflight:
        print(f"  THESIS RESEARCH AGENT — AUTH PRE-FLIGHT")
    elif args.research_next:
        print(f"  THESIS RESEARCH AGENT — WEEKEND WORKER")
    elif args.assemble:
        print(f"  THESIS RESEARCH AGENT — ASSEMBLE BATCH")
    else:
        print(f"  THESIS RESEARCH AGENT")
    print(f"{'=' * 60}\n")

    if args.preflight:
        run_preflight(args)
    elif args.research_next:
        run_research_next(args)
    elif args.assemble:
        run_assemble(args)
    else:
        report = run(args)
        print("\n" + "=" * 60)
        print(report)
        print("=" * 60 + "\n")
