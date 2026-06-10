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


# ── Alpaca batch-poisoning guard ────────────────────────────────────────────

def test_alpaca_invalid_symbol_extraction():
    symbols = ["LNG", "SAAB-B.ST", "TLT"]
    err = '{"message":"invalid symbol: SAAB-B.ST"}'
    assert tr._alpaca_invalid_symbol(err, symbols) == "SAAB-B.ST"
    # symbol not in our request -> no match (don't misparse unrelated errors)
    assert tr._alpaca_invalid_symbol('invalid symbol: OTHER', symbols) is None
    assert tr._alpaca_invalid_symbol("rate limit exceeded", symbols) is None


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

META = {"conviction": 3, "direction": "up", "conviction_note": "", "catalysts": [],
        "catalyst_outcomes": []}


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


# ── catalysts: past-due detection, verdict parsing, calendar ────────────────

def test_past_catalysts_filters_by_date():
    rec = {"catalysts": [
        {"date": "2026-06-01", "what": "already happened"},
        {"date": "2026-06-15", "what": "today counts as due"},
        {"date": "2026-07-01", "what": "still upcoming"},
        {"date": "", "what": "undated"},
    ]}
    due = tr.past_catalysts(rec, "2026-06-15")
    assert [c["what"] for c in due] == ["already happened", "today counts as due"]
    assert tr.past_catalysts({}, "2026-06-15") == []


def test_normalize_meta_parses_catalyst_outcomes():
    out = tr.normalize_meta({
        "conviction": 3,
        "catalyst_outcomes": [
            {"date": "2026-06-29", "what": "DEA hearing", "outcome": "FOR", "note": "confirmed"},
            {"date": "2026-06-15", "what": "Sherritt update", "outcome": "thesis-confirmed!"},
            "not-a-dict",
        ],
    })
    assert out["catalyst_outcomes"][0]["outcome"] == "for"
    assert out["catalyst_outcomes"][1]["outcome"] == "unscored"  # unknown verdict coerced
    assert len(out["catalyst_outcomes"]) == 2


def test_analysis_prompt_demands_verdicts_only_when_catalysts_due():
    thesis = {"title": "T", "body": "B"}
    with_due = tr.build_analysis_prompt(
        "", thesis, allow_web=False, prior_context="CONTINUITY — prior take",
        past_cats=[{"date": "2026-06-01", "what": "an event"}])
    without = tr.build_analysis_prompt("", thesis, allow_web=False,
                                       prior_context="CONTINUITY — prior take")
    assert "catalyst_outcomes" in with_due
    assert "2026-06-01 an event" in with_due
    assert "catalyst_outcomes" not in without


def test_render_catalyst_calendar_orders_and_filters():
    records = [
        {"title": "Thesis B", "catalysts": [
            {"date": "2026-07-01", "what": "later | with pipe"},
            {"date": "2026-06-01", "what": "already past — excluded"},
            {"date": "garbage", "what": "bad date — skipped"},
        ]},
        {"title": "Thesis A", "catalysts": [
            {"date": "2026-06-15", "what": "fires today"},
        ]},
    ]
    cal = tr.render_catalyst_calendar(records, "2026-06-15")
    assert cal.startswith("## Catalyst Calendar")
    lines = [l for l in cal.splitlines() if l.startswith("| 2026")]
    assert len(lines) == 2
    assert "fires today" in lines[0] and "| today |" in lines[0]
    assert "later / with pipe" in lines[1] and "| 16d |" in lines[1]
    assert "excluded" not in cal and "skipped" not in cal


def test_render_catalyst_calendar_empty_returns_none():
    assert tr.render_catalyst_calendar([], "2026-06-15") is None
    assert tr.render_catalyst_calendar(
        [{"title": "T", "catalysts": [{"date": "2026-01-01", "what": "all past"}]}],
        "2026-06-15") is None


def test_render_outcome_scorecard():
    line = tr.render_outcome_scorecard([
        {"date": "2026-06-29", "what": "DEA hearing", "outcome": "for", "note": "confirmed"},
        {"date": "2026-06-15", "what": "Sherritt", "outcome": "pending", "note": ""},
    ])
    assert line.startswith("**Catalyst verdicts:**")
    assert "✓ DEA hearing (2026-06-29) — **for**: _confirmed_" in line
    assert "⏳ Sherritt (2026-06-15) — **pending**" in line


def test_compute_and_render_carries_outcomes_into_record_and_section(tmp_path, monkeypatch):
    monkeypatch.setattr(tr, "REPORTS_DIR", tmp_path)
    result = _result()
    result["meta"]["catalyst_outcomes"] = [
        {"date": "2026-06-10", "what": "an event", "outcome": "against", "note": "it missed"}]
    result["meta"]["catalysts"] = []
    enrichment = {tr.BENCHMARK_TICKER: {"price": 500.0, "low_52w": 400, "high_52w": 520}}
    sections, records = tr.compute_and_render([result], enrichment, "2026-06-15")
    assert records[0]["catalyst_outcomes"][0]["outcome"] == "against"
    assert "**Catalyst verdicts:**" in sections[0]
    assert "✗ an event" in sections[0]


def test_resolutions_dedup_keeps_latest_and_counts(tmp_path, monkeypatch):
    monkeypatch.setattr(tr, "REPORTS_DIR", tmp_path)
    # Week 1 calls the verdict "pending"; week 2 re-reports the same event as "for".
    _write_sidecar(tmp_path, "2026-06-15", {
        "key": "test-thesis", "title": "Test Thesis", "conviction": 3,
        "catalyst_outcomes": [
            {"date": "2026-06-10", "what": "DEA hearing", "outcome": "pending", "note": ""}],
    })
    current = [{
        "key": "test-thesis", "title": "Test Thesis", "conviction": 3,
        "equity_index": 100.0,
        "catalyst_outcomes": [
            {"date": "2026-06-10", "what": "DEA hearing", "outcome": "for", "note": "confirmed"},
            {"date": "2026-06-12", "what": "earnings", "outcome": "against", "note": "missed"},
        ],
    }]
    out = tr.render_calibration_section(current, "2026-06-22")
    assert "2 verdict(s): 1 for · 1 against · 0 mixed · 0 pending" in out
    assert "**50%** broke the thesis's way" in out
    # The deduped row shows the latest verdict, not the stale "pending".
    assert out.count("DEA hearing") == 1


def test_resolutions_empty_message(tmp_path, monkeypatch):
    monkeypatch.setattr(tr, "REPORTS_DIR", tmp_path)
    out = tr.render_calibration_section([], "2026-06-15")
    assert "No flagged catalysts have come due yet" in out


# ── new-thesis scan suggestion ledger ───────────────────────────────────────

SCAN_RESPONSE = """## Suggested new theses

### Thesis: Uranium supply deficit is underpriced

**Rationale:** Some reasons.

**Why now:** A development.

**Suggested tickers to research further:**

```yaml
tickers_to_research: [CCJ, URA, lowercase_junk_that_is_too_long]
```

### Thesis: Second idea with no valid block

**Rationale:** More reasons.
"""


def test_parse_scan_suggestions_pairs_titles_with_tickers():
    suggestions = tr.parse_scan_suggestions(SCAN_RESPONSE)
    assert len(suggestions) == 2
    assert suggestions[0]["title"] == "Uranium supply deficit is underpriced"
    assert suggestions[0]["tickers"] == ["CCJ", "URA"]
    assert suggestions[1]["tickers"] == []
    assert tr.parse_scan_suggestions("no suggestions here") == []


def test_build_suggestion_records_snapshots_only_priced():
    enrichment = {"CCJ": {"price": 55.0, "low_52w": 35, "high_52w": 62},
                  "URA": {"error": "no data"}}
    recs = tr.build_suggestion_records(
        [{"title": "Uranium supply deficit", "tickers": ["CCJ", "URA"]}],
        enrichment, "2026-06-15")
    assert recs[0]["key"] == "uranium-supply-deficit"
    assert recs[0]["prices"] == {"CCJ": 55.0}
    assert recs[0]["tickers"] == ["CCJ", "URA"]


def test_load_scan_suggestions_dedups_and_orders(tmp_path, monkeypatch):
    monkeypatch.setattr(tr, "REPORTS_DIR", tmp_path)
    (tmp_path / "2026-06-08_research.json").write_text(json.dumps({
        "theses": [], "scan_suggestions": [
            {"key": "idea-a", "title": "Idea A", "date": "2026-06-08",
             "tickers": ["AAA"], "prices": {"AAA": 100.0}}]}), encoding="utf-8")
    (tmp_path / "2026-06-15_research.json").write_text(json.dumps({
        "theses": [], "scan_suggestions": [
            {"key": "idea-a", "title": "Idea A", "date": "2026-06-15",
             "tickers": ["AAA"], "prices": {"AAA": 120.0}},   # re-suggested — ignored
            {"key": "idea-b", "title": "Idea B", "date": "2026-06-15",
             "tickers": ["BBB"], "prices": {"BBB": 50.0}}]}), encoding="utf-8")

    suggestions = tr.load_scan_suggestions("2026-06-22")
    assert [s["key"] for s in suggestions] == ["idea-a", "idea-b"]
    assert suggestions[0]["prices"]["AAA"] == 100.0  # original snapshot kept
    # before_date excludes same-day and later sidecars
    assert [s["key"] for s in tr.load_scan_suggestions("2026-06-15")] == ["idea-a"]


def test_scan_prompt_feeds_back_prior_suggestions():
    prior = [{"date": "2026-06-08", "title": "Idea A"}]
    with_prior = tr.build_new_thesis_prompt("", ["Held thesis"], allow_web=False,
                                            prior_suggestions=prior)
    without = tr.build_new_thesis_prompt("", ["Held thesis"], allow_web=False)
    assert "2026-06-08: Idea A" in with_prior
    assert "PREVIOUS SCANS" in with_prior
    assert "PREVIOUS SCANS" not in without


def test_track_record_basket_math(tmp_path, monkeypatch):
    monkeypatch.setattr(tr, "REPORTS_DIR", tmp_path)
    suggestions = [
        {"key": "idea-a", "title": "Idea A", "date": "2026-06-08",
         "tickers": ["AAA", "BBB", "CCC"],
         "prices": {"AAA": 100.0, "BBB": 200.0}},  # CCC never priced
        {"key": "idea-b", "title": "Idea B", "date": "2026-06-01",
         "tickers": ["DDD"], "prices": {}},
    ]
    enrichment = {"AAA": {"price": 110.0, "low_52w": 90, "high_52w": 120},
                  "BBB": {"price": 210.0, "low_52w": 150, "high_52w": 250}}
    out = tr.render_calibration_section([], "2026-06-15", suggestions, enrichment)
    assert "### New-thesis scan track record" in out
    # (+10% + +5%) / 2 = +7.5%, over 2 of 3 suggested tickers
    assert "| 2026-06-08 | Idea A | +7.5% | 2/3 |" in out
    assert "| 2026-06-01 | Idea B | — | 0/1 |" in out
    # newest suggestion listed first
    assert out.index("Idea A") < out.index("Idea B")


def test_track_record_empty_and_omitted(tmp_path, monkeypatch):
    monkeypatch.setattr(tr, "REPORTS_DIR", tmp_path)
    with_empty = tr.render_calibration_section([], "2026-06-15", [], {})
    assert "No prior scan suggestions tracked yet" in with_empty
    without = tr.render_calibration_section([], "2026-06-15")
    assert "scan track record" not in without


# ── Obsidian delivery ───────────────────────────────────────────────────────

def test_deliver_to_obsidian_writes_with_frontmatter(tmp_path, monkeypatch):
    monkeypatch.setattr(tr, "OBSIDIAN_VAULT_DIR", str(tmp_path))
    tr.deliver_to_obsidian("2026-06-15", "# Market Research — 2026-06-15\n\nbody")
    dest = tmp_path / tr.OBSIDIAN_REPORTS_SUBDIR / "2026-06-15_research.md"
    assert dest.exists()
    text = dest.read_text(encoding="utf-8")
    assert text.startswith("---\ndate: 2026-06-15\ntags: [market-research]\n---\n")
    assert "# Market Research — 2026-06-15" in text


def test_deliver_to_obsidian_disabled_or_missing_vault(tmp_path, monkeypatch):
    # Unset -> silent no-op
    monkeypatch.setattr(tr, "OBSIDIAN_VAULT_DIR", None)
    tr.deliver_to_obsidian("2026-06-15", "body")  # must not raise
    # Set but vault folder doesn't exist -> warns, does NOT create the vault
    ghost = tmp_path / "no-such-vault"
    monkeypatch.setattr(tr, "OBSIDIAN_VAULT_DIR", str(ghost))
    tr.deliver_to_obsidian("2026-06-15", "body")  # must not raise
    assert not ghost.exists()


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
