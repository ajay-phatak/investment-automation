"""Unit tests for the pure / parsing core of thesis_research.py.

No network, no Claude calls: everything here exercises parsing, math, and
rendering with synthetic inputs. Run with:
    python -m pytest tests/ -q
"""

import json
import os

import pytest

import thesis_research as tr


# ── thesis_key ──────────────────────────────────────────────────────────────

def test_thesis_key_normalizes_case_punctuation_and_unicode():
    assert tr.thesis_key("SaaS Re-acceleration — Misplaced AI Fear!") == \
        "saas-re-acceleration-misplaced-ai-fear"
    assert tr.thesis_key("  Spaces   everywhere  ") == "spaces-everywhere"
    assert tr.thesis_key("") == ""
    assert tr.thesis_key(None) == ""


def test_thesis_key_stable_across_cosmetic_edits():
    a = tr.thesis_key("GLP-1 Market Size Is Massively Underpriced")
    b = tr.thesis_key("glp-1 market size is massively underpriced")
    assert a == b


# ── _to_float ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw, expected", [
    ("$1,234.56", 1234.56),
    ("12%", 12.0),
    (" 95.85 ", 95.85),
    (42, 42.0),
    ("—", None),
    ("", None),
    (None, None),
])
def test_to_float(raw, expected):
    assert tr._to_float(raw) == expected


# ── _load_dotenv ────────────────────────────────────────────────────────────

def test_load_dotenv_sets_strips_and_skips(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# comment\n"
        "NEW_KEY=hello\n"
        "QUOTED_KEY='quoted value'\n"
        "EXISTING_KEY=from-file\n"
        "EMPTY_KEY=\n"
        "not a kv line\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("NEW_KEY", raising=False)
    monkeypatch.delenv("QUOTED_KEY", raising=False)
    monkeypatch.delenv("EMPTY_KEY", raising=False)
    monkeypatch.setenv("EXISTING_KEY", "from-shell")

    tr._load_dotenv(env_file)

    assert os.environ["NEW_KEY"] == "hello"
    assert os.environ["QUOTED_KEY"] == "quoted value"
    assert os.environ["EXISTING_KEY"] == "from-shell"  # shell wins
    assert "EMPTY_KEY" not in os.environ              # empty values skipped


def test_load_dotenv_missing_file_is_noop(tmp_path):
    tr._load_dotenv(tmp_path / "does-not-exist.env")  # must not raise


# ── split_meta_block / normalize_meta ───────────────────────────────────────

ANALYSIS_WITH_META = """### Steel man
- point

### Stocks

```yaml
upside: [NVDA, SMH]
downside: [TLRY]
```

```yaml
conviction: 4
direction: up
conviction_note: "getting stronger"
catalysts:
  - {date: 2026-08-15, what: "earnings"}
```
"""


def test_split_meta_block_finds_and_strips_only_conviction_block():
    meta, cleaned = tr.split_meta_block(ANALYSIS_WITH_META)
    assert meta["conviction"] == 4
    assert "conviction" not in cleaned
    assert "upside: [NVDA, SMH]" in cleaned  # ticker block untouched


def test_split_meta_block_absent_returns_none_and_original():
    text = "no yaml here at all"
    meta, cleaned = tr.split_meta_block(text)
    assert meta is None
    assert cleaned == text


def test_normalize_meta_coerces_and_defaults():
    out = tr.normalize_meta({
        "conviction": "4",
        "direction": "UP",
        "catalysts": [{"date": "2026-08-15", "what": "earnings"}, "not-a-dict"],
    })
    assert out["conviction"] == 4
    assert out["direction"] == "up"
    assert out["catalysts"] == [{"date": "2026-08-15", "what": "earnings"}]


def test_normalize_meta_bad_conviction_and_none():
    assert tr.normalize_meta({"conviction": "high"})["conviction"] is None
    assert tr.normalize_meta(None)["conviction"] is None


# ── extract_tickers_from_response ───────────────────────────────────────────

def test_extract_tickers_handles_all_shapes_and_filters_junk():
    response = """
```yaml
upside: [NVDA, tlry.to]
downside: []
```

```yaml
tickers_to_research: [SMH, "BRK.B", TOOLONGTICKERNAME]
```

```yaml
this is: [not, valid, yaml: ]:
```
"""
    tickers = tr.extract_tickers_from_response(response)
    assert "NVDA" in tickers
    assert "TLRY.TO" in tickers       # uppercased, suffix kept
    assert "BRK.B" in tickers
    assert "TOOLONGTICKERNAME" not in tickers
    assert "NOT" not in tickers       # broken yaml block skipped entirely


# ── parse_portfolio_table ───────────────────────────────────────────────────

PORTFOLIO_MD = """### Mock Portfolio (~$5,000 hypothetical)

| Ticker | Role | Shares | Price | Allocation | Weight | Rationale |
|--------|------|--------|-------|------------|--------|-----------|
| **IGV** | core | 15 | $95.85 | $1,437.75 | 28.8% | anchor |
| SNOW | speculative | 3 | $238.26 | $714.78 | 14.3% | satellite |
| ADBE calls | overlay | — | — | $250.00 | 5.0% | premium est. |
| **Total** | | | | $4,902.53 | 98.1% | |

Leftover cash: $97.47

### Scenario Matrix
| Scenario | What it looks like | Portfolio impact |
|---|---|---|
| base | fine | flat |
"""


def test_parse_portfolio_table_rows_options_and_total():
    holdings = tr.parse_portfolio_table(PORTFOLIO_MD)
    assert [h["ticker"] for h in holdings] == ["IGV", "SNOW", "ADBE calls"]
    igv = holdings[0]
    assert igv["shares"] == 15 and igv["entry_price"] == 95.85
    assert igv["weight_pct"] == 28.8 and igv["is_option"] is False
    option = holdings[2]
    assert option["is_option"] is True and option["entry_price"] is None
    # Scenario-matrix rows must not leak in as holdings
    assert all(h["ticker"].lower() != "base" for h in holdings)


def test_parse_portfolio_table_empty_input():
    assert tr.parse_portfolio_table("") == []


# ── _is_priced / position_pct ───────────────────────────────────────────────

def test_is_priced():
    assert tr._is_priced({"price": 10.0}) is True
    assert tr._is_priced({"price": float("nan")}) is False
    assert tr._is_priced({"error": "no data"}) is False
    assert tr._is_priced(None) is False
    assert tr._is_priced({}) is False


def test_position_pct():
    assert tr.position_pct(75, 50, 100) == "50%"
    assert tr.position_pct(50, 50, 50) == "n/a"  # degenerate range


# ── compute_weekly_return ───────────────────────────────────────────────────

def _h(ticker, entry, weight, is_option=False):
    return {"ticker": ticker, "role": "core", "shares": 1,
            "entry_price": entry, "weight_pct": weight, "is_option": is_option}


def test_compute_weekly_return_weights_and_exclusions():
    prior = [
        _h("AAA", 100.0, 60.0),
        _h("BBB", 200.0, 40.0),
        _h("CCC calls", None, 5.0, is_option=True),
        _h("DDD", 50.0, 10.0),  # no current price -> excluded
    ]
    enrichment = {
        "AAA": {"price": 110.0, "low_52w": 90, "high_52w": 120},
        "BBB": {"price": 190.0, "low_52w": 150, "high_52w": 250},
    }
    legs, weekly = tr.compute_weekly_return(prior, enrichment)
    # (+10% * 60 + -5% * 40) / (60 + 40) = +4%
    assert weekly == pytest.approx(0.04)
    statuses = {l["ticker"]: l["status"] for l in legs}
    assert statuses["AAA"] == "ok"
    assert "excluded" in statuses["CCC calls"]
    assert "excluded" in statuses["DDD"]


def test_compute_weekly_return_nothing_priceable():
    legs, weekly = tr.compute_weekly_return([_h("AAA", 100.0, 50.0)], {})
    assert weekly is None
    assert legs[0]["status"] == "no current price (excluded)"


# ── render_ticker_table ─────────────────────────────────────────────────────

def test_render_ticker_table_priced_and_error_rows():
    yaml_text = "upside: [AAA]\ndownside: [BBB]"
    enrichment = {
        "AAA": {"price": 110.0, "low_52w": 90.0, "high_52w": 120.0},
        "BBB": {"error": "no data"},
    }
    table = tr.render_ticker_table(yaml_text, enrichment)
    assert "| AAA | upside | $110.00 |" in table
    assert "_no data_" in table


def test_render_ticker_table_non_ticker_yaml_left_fenced():
    out = tr.render_ticker_table("just_a_key: 42", {})
    assert out.startswith("```yaml")


# ── parse_theses / upcoming_monday ──────────────────────────────────────────

def test_parse_theses_preamble_and_bodies(tmp_path):
    f = tmp_path / "theses.md"
    f.write_text(
        "# My book\nStrategy preamble here.\n\n"
        "## Thesis: First idea\nBody one.\n\n"
        "## Thesis: Second idea\nBody two.\n",
        encoding="utf-8",
    )
    preamble, theses = tr.parse_theses(f)
    assert "Strategy preamble" in preamble
    assert [t["title"] for t in theses] == ["First idea", "Second idea"]
    assert theses[0]["body"] == "Body one."


def test_parse_theses_no_headings_raises(tmp_path):
    f = tmp_path / "empty.md"
    f.write_text("nothing structural here", encoding="utf-8")
    with pytest.raises(ValueError):
        tr.parse_theses(f)


def test_upcoming_monday():
    from datetime import date
    assert tr.upcoming_monday(date(2026, 6, 13)) == date(2026, 6, 15)  # Sat -> Mon
    assert tr.upcoming_monday(date(2026, 6, 15)) == date(2026, 6, 15)  # Mon -> same


# ── benchmark math through compute_and_render ───────────────────────────────

META = {"conviction": 3, "direction": "up", "conviction_note": "", "catalysts": []}


def _result(key="test-thesis", title="Test Thesis"):
    return {"key": key, "title": title, "meta": dict(META), "holdings": [],
            "rendered_analysis": "(analysis)", "portfolio_text": "(portfolio)"}


def _write_sidecar(reports_dir, report_date, record):
    payload = {"report_date": report_date, "theses": [record]}
    (reports_dir / f"{report_date}_research.json").write_text(
        json.dumps(payload), encoding="utf-8")


def test_first_week_record_carries_benchmark_baseline(tmp_path, monkeypatch):
    monkeypatch.setattr(tr, "REPORTS_DIR", tmp_path)
    enrichment = {tr.BENCHMARK_TICKER: {"price": 500.0, "low_52w": 400, "high_52w": 520}}
    sections, records = tr.compute_and_render([_result()], enrichment, "2026-06-08")
    rec = records[0]
    assert rec["benchmark_ticker"] == tr.BENCHMARK_TICKER
    assert rec["benchmark_price"] == 500.0
    assert rec["benchmark_index"] == 100.0
    assert rec["equity_index"] == 100.0
    assert "First tracked week" in sections[0]


def test_second_week_advances_equity_and_benchmark_in_lockstep(tmp_path, monkeypatch):
    monkeypatch.setattr(tr, "REPORTS_DIR", tmp_path)
    _write_sidecar(tmp_path, "2026-06-08", {
        "key": "test-thesis", "title": "Test Thesis", "inception_date": "2026-06-08",
        "conviction": 3, "equity_index": 100.0,
        "holdings": [_h("AAA", 100.0, 60.0), _h("BBB", 200.0, 40.0)],
        "benchmark_ticker": tr.BENCHMARK_TICKER,
        "benchmark_price": 500.0, "benchmark_index": 100.0,
    })
    enrichment = {
        "AAA": {"price": 110.0, "low_52w": 90, "high_52w": 120},
        "BBB": {"price": 190.0, "low_52w": 150, "high_52w": 250},
        tr.BENCHMARK_TICKER: {"price": 510.0, "low_52w": 400, "high_52w": 520},
    }
    sections, records = tr.compute_and_render([_result()], enrichment, "2026-06-15")
    rec = records[0]
    assert rec["weekly_return"] == pytest.approx(0.04)
    assert rec["equity_index"] == pytest.approx(104.0)
    assert rec["benchmark_index"] == pytest.approx(102.0)  # 500 -> 510 = +2%
    assert rec["benchmark_price"] == 510.0
    assert f"vs {tr.BENCHMARK_TICKER} +2.0%" in sections[0]


def test_prior_sidecar_without_benchmark_fields_degrades_gracefully(tmp_path, monkeypatch):
    """A pre-benchmark sidecar (like the original 2026-06-08 one) must not break
    the run: equity advances, benchmark index stays at par and re-baselines."""
    monkeypatch.setattr(tr, "REPORTS_DIR", tmp_path)
    _write_sidecar(tmp_path, "2026-06-08", {
        "key": "test-thesis", "title": "Test Thesis", "inception_date": "2026-06-08",
        "conviction": 3, "equity_index": 100.0,
        "holdings": [_h("AAA", 100.0, 100.0)],
    })
    enrichment = {
        "AAA": {"price": 105.0, "low_52w": 90, "high_52w": 120},
        tr.BENCHMARK_TICKER: {"price": 510.0, "low_52w": 400, "high_52w": 520},
    }
    sections, records = tr.compute_and_render([_result()], enrichment, "2026-06-15")
    rec = records[0]
    assert rec["equity_index"] == pytest.approx(105.0)
    assert rec["benchmark_index"] == 100.0      # nothing to compare against yet
    assert rec["benchmark_price"] == 510.0      # baseline set for next week


def test_missing_benchmark_price_still_renders(tmp_path, monkeypatch):
    monkeypatch.setattr(tr, "REPORTS_DIR", tmp_path)
    _write_sidecar(tmp_path, "2026-06-08", {
        "key": "test-thesis", "title": "Test Thesis", "inception_date": "2026-06-08",
        "conviction": 3, "equity_index": 100.0,
        "holdings": [_h("AAA", 100.0, 100.0)],
        "benchmark_ticker": tr.BENCHMARK_TICKER,
        "benchmark_price": 500.0, "benchmark_index": 100.0,
    })
    enrichment = {"AAA": {"price": 105.0, "low_52w": 90, "high_52w": 120}}  # no SPY
    sections, records = tr.compute_and_render([_result()], enrichment, "2026-06-15")
    rec = records[0]
    assert rec["equity_index"] == pytest.approx(105.0)
    assert rec["benchmark_index"] == 100.0   # not advanced without a pairable return
    assert rec["benchmark_price"] is None
    assert "vs" not in sections[0].split("Thesis weekly return")[1].split("·")[0]


# ── ledger rendering ────────────────────────────────────────────────────────

def test_ledger_includes_vs_benchmark_column(tmp_path, monkeypatch):
    monkeypatch.setattr(tr, "REPORTS_DIR", tmp_path)
    current = [{
        "key": "test-thesis", "title": "Test Thesis", "conviction": 3,
        "weekly_return": 0.04, "equity_index": 104.0,
        "benchmark_ticker": tr.BENCHMARK_TICKER, "benchmark_index": 102.0,
    }]
    out = tr.render_calibration_section(current, "2026-06-15")
    assert f"vs {tr.BENCHMARK_TICKER}" in out
    assert "+2.0 pp" in out
