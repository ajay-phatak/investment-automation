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


# ── thesis identity: control block, aliases, lifecycle fields ───────────────

def _thesis_file(tmp_path, body):
    f = tmp_path / "theses.md"
    f.write_text("Strategy preamble.\n\n" + body, encoding="utf-8")
    return f


def _ledger_rows(section):
    """Data rows of the '### Ledger (latest per thesis)' table only."""
    body = section.split("### Ledger (latest per thesis)")[1]
    body = body.split("### Conviction calibration")[0]
    return [l for l in body.splitlines()
            if l.startswith("|") and not l.startswith("| Thesis |")
            and not set(l) <= set("-:| ")]


def test_parse_theses_without_control_block_keeps_old_behaviour(tmp_path):
    f = _thesis_file(tmp_path, "## Thesis: Plain old thesis\nJust a body.\n")
    _, theses = tr.parse_theses(f)
    t = theses[0]
    assert t["title"] == "Plain old thesis"
    assert t["body"] == "Just a body."
    assert t["id"] == "plain-old-thesis"          # falls back to the title slug
    assert t["aliases"] == [] and t["amendments"] == []
    assert (t["status"], t["mode"], t["version"]) == ("active", "standard", 1)


def test_parse_thesis_control_strips_block_and_amendments(tmp_path):
    f = _thesis_file(tmp_path,
                     "## Thesis: Reframed idea\n\n"
                     "```yaml\nid: reframed\naliases: [old-slug, older-slug]\n"
                     "status: watch\nmode: residual\nversion: 3\n"
                     "spent: [snow, ddog]\n```\n\n"
                     "The actual framing.\n\n"
                     "### Amendments\n"
                     "- 2026-08-31 (v3): Narrowed onto adjacent\n  industries.\n"
                     "- 2026-06-15 (v2): Dropped the Europe angle.\n")
    _, theses = tr.parse_theses(f)
    t = theses[0]
    assert t["body"] == "The actual framing."        # block + amendments stripped
    assert "```yaml" not in t["body"] and "Amendments" not in t["body"]
    assert t["id"] == "reframed"
    assert t["aliases"] == ["old-slug", "older-slug"]
    assert (t["status"], t["mode"], t["version"]) == ("watch", "residual", 3)
    assert t["spent"] == ["SNOW", "DDOG"]           # normalized to ticker case
    assert [a["date"] for a in t["amendments"]] == ["2026-08-31", "2026-06-15"]
    assert t["amendments"][0]["version"] == 3
    # a wrapped bullet is folded back into one amendment
    assert t["amendments"][0]["text"] == "Narrowed onto adjacent industries."


def test_parse_thesis_control_coerces_bad_values(tmp_path, capsys):
    f = _thesis_file(tmp_path,
                     "## Thesis: Sloppy\n\n```yaml\nid: sloppy\nstatus: paused\n"
                     "mode: turbo\nversion: many\nnonsense: 1\n```\n\nBody.\n")
    _, theses = tr.parse_theses(f)
    t = theses[0]
    assert (t["status"], t["mode"], t["version"]) == ("active", "standard", 1)
    assert t["id"] == "sloppy" and t["body"] == "Body."
    warned = capsys.readouterr().out
    assert "unknown thesis status" in warned and "unknown thesis mode" in warned
    assert "non-integer thesis version" in warned and "nonsense" in warned


def test_parse_thesis_control_leaves_unrelated_yaml_in_body(tmp_path):
    f = _thesis_file(tmp_path,
                     "## Thesis: Not a control block\n\n"
                     "```yaml\nsome_data: 1\n```\n\nBody.\n")
    _, theses = tr.parse_theses(f)
    t = theses[0]
    assert "some_data" in t["body"]                  # left alone, not swallowed
    assert t["id"] == "not-a-control-block"


def test_parse_thesis_control_survives_malformed_yaml(tmp_path, capsys):
    f = _thesis_file(tmp_path,
                     "## Thesis: Broken\n\n```yaml\nid: [unclosed\n```\n\nBody.\n")
    _, theses = tr.parse_theses(f)
    assert theses[0]["id"] == "broken"               # defaults, no exception
    assert "unparseable thesis control block" in capsys.readouterr().out


def test_resolve_keys_orders_id_then_aliases():
    t = {"id": "new-id", "aliases": ["Old Title Slug", "new-id", ""],
         "title": "Ignored When Id Present"}
    assert tr.resolve_keys(t) == ["new-id", "old-title-slug"]   # deduped, normalized
    assert tr.resolve_keys({"title": "No Id Here"}) == ["no-id-here"]


def test_build_alias_map_points_every_key_at_the_canonical_id():
    theses = [{"id": "a", "aliases": ["a-old"]}, {"id": "b", "aliases": []}]
    assert tr.build_alias_map(theses) == {"a": "a", "a-old": "a", "b": "b"}


def test_load_prior_state_finds_history_under_an_alias(tmp_path, monkeypatch):
    monkeypatch.setattr(tr, "REPORTS_DIR", tmp_path)
    _write_sidecar(tmp_path, "2026-06-08", {
        "key": "old-title-slug", "title": "Old Title", "equity_index": 118.0,
        "conviction": 4, "inception_date": "2026-05-11"})
    thesis = {"id": "new-id", "aliases": ["old-title-slug"], "title": "New Title"}

    got = tr.load_prior_state(tr.resolve_keys(thesis), "2026-06-15")
    assert got is not None and got[0] == "2026-06-08"
    assert got[1]["equity_index"] == 118.0 and got[1]["inception_date"] == "2026-05-11"
    # the new id on its own has no history yet — the alias is what carries it
    assert tr.load_prior_state(["new-id"], "2026-06-15") is None


def test_load_prior_state_prefers_id_over_alias_and_accepts_a_bare_string(tmp_path, monkeypatch):
    monkeypatch.setattr(tr, "REPORTS_DIR", tmp_path)
    (tmp_path / "2026-06-08_research.json").write_text(json.dumps({"theses": [
        {"key": "old-slug", "title": "Old", "equity_index": 90.0},
        {"key": "new-id", "title": "New", "equity_index": 110.0}]}), encoding="utf-8")

    assert tr.load_prior_state(["new-id", "old-slug"], "2026-06-15")[1]["equity_index"] == 110.0
    assert tr.load_prior_state("old-slug", "2026-06-15")[1]["equity_index"] == 90.0


def test_ledger_merges_a_renamed_thesis_into_one_row(tmp_path, monkeypatch):
    monkeypatch.setattr(tr, "REPORTS_DIR", tmp_path)
    _write_sidecar(tmp_path, "2026-06-08", {
        "key": "old-slug", "title": "Old Framing", "conviction": 4,
        "weekly_return": 0.02, "equity_index": 102.0, "benchmark_index": 101.0})
    current = [{"key": "new-id", "title": "New Framing", "conviction": 3,
                "weekly_return": 0.01, "equity_index": 103.0, "benchmark_index": 101.5}]
    theses = [{"id": "new-id", "aliases": ["old-slug"], "title": "New Framing"}]

    merged = tr.render_calibration_section(current, "2026-06-15",
                                           alias_map=tr.build_alias_map(theses))
    rows = _ledger_rows(merged)
    assert len(rows) == 1 and "New Framing" in rows[0]

    # without the alias map the abandoned key orphans into its own frozen row
    assert len(_ledger_rows(tr.render_calibration_section(current, "2026-06-15"))) == 2


def test_unit_thesis_falls_back_for_a_pre_schema_manifest():
    unit = {"id": "thesis-1", "kind": "thesis", "title": "Old Style Thesis",
            "body": "B", "status": "pending", "attempts": 0}
    t = tr.unit_thesis(unit)
    assert t["id"] == "old-style-thesis" and t["body"] == "B"
    assert t["status"] == "active"      # the thesis status, not the unit's "pending"
    assert t["aliases"] == [] and t["version"] == 1
    assert tr.resolve_keys(t) == ["old-style-thesis"]


def test_create_manifest_carries_thesis_identity(tmp_path):
    f = _thesis_file(tmp_path,
                     "## Thesis: Renamed idea\n\n```yaml\nid: stable-id\n"
                     "aliases: [older-slug]\nstatus: watch\nmode: residual\n"
                     "version: 2\nspent: [snow]\n```\n\nBody.\n")
    manifest = tr.create_manifest(f, tr.date(2026, 6, 15))
    unit = manifest["units"][0]
    assert unit["id"] == "thesis-1" and unit["status"] == "pending"   # unit fields intact

    t = tr.unit_thesis(unit)
    assert t["id"] == "stable-id" and t["aliases"] == ["older-slug"]
    assert (t["status"], t["mode"], t["version"]) == ("watch", "residual", 2)
    assert t["spent"] == ["SNOW"] and t["body"] == "Body."
    assert tr.resolve_keys(t) == ["stable-id", "older-slug"]


def test_compute_and_render_continues_the_equity_index_through_a_rename(tmp_path, monkeypatch):
    """The performance math does its own prior-state lookup — if it ignored the
    alias keys, renaming a thesis would silently restart its equity index at 100
    and reset inception to today, which is the whole thing aliases exist to stop."""
    monkeypatch.setattr(tr, "REPORTS_DIR", tmp_path)
    _write_sidecar(tmp_path, "2026-06-08", {
        "key": "old-slug", "title": "Old Framing", "inception_date": "2026-05-11",
        "conviction": 4, "equity_index": 118.0, "benchmark_index": 105.0,
        "benchmark_ticker": tr.BENCHMARK_TICKER, "benchmark_price": 400.0,
        "holdings": [_h("AAA", 100.0, 100.0)]})
    enrichment = {"AAA": {"price": 110.0, "low_52w": 50, "high_52w": 150},
                  tr.BENCHMARK_TICKER: {"price": 420.0, "low_52w": 300, "high_52w": 500}}

    renamed = {**_result(key="new-id", title="New Framing"),
               "keys": ["new-id", "old-slug"]}
    _, records = tr.compute_and_render([renamed], enrichment, "2026-06-15")
    rec = records[0]
    assert rec["prior_report_date"] == "2026-06-08"
    assert rec["inception_date"] == "2026-05-11"            # carried, not restarted
    assert rec["weekly_return"] == pytest.approx(0.10)
    assert rec["equity_index"] == pytest.approx(129.8)      # 118.0 * 1.10

    # the same result without alias keys is a fresh thesis — the bug this guards
    _, plain = tr.compute_and_render([_result(key="new-id", title="New Framing")],
                                     enrichment, "2026-06-15")
    assert plain[0]["equity_index"] == 100.0
    assert plain[0]["inception_date"] == "2026-06-15"


# ── amendments: detection, framing, versioned track record ─────────────────

AMENDED = {"id": "reframed", "version": 3, "amendments": [
    {"date": "2026-08-31", "version": 3, "text": "Rotated onto the adjacency."},
    {"date": "2026-06-15", "version": 2, "text": "Dropped the Europe angle."}]}


def test_detect_amendment_only_fires_on_a_version_bump():
    assert tr.detect_amendment(AMENDED, None) is None            # first tracked week
    assert tr.detect_amendment(AMENDED, {"version": 3}) is None  # unchanged
    assert tr.detect_amendment(AMENDED, {"version": 4}) is None  # never goes backwards

    got = tr.detect_amendment(AMENDED, {"version": 2})
    assert (got["from"], got["to"]) == (2, 3)
    assert [a["version"] for a in got["entries"]] == [3]          # only what's new


def test_detect_amendment_reads_a_pre_schema_sidecar_as_v1():
    """Records written before the schema carry no version. They must read as v1
    so an untouched thesis never shows a phantom amendment banner."""
    assert tr.detect_amendment({"version": 1, "amendments": []}, {"conviction": 3}) is None
    got = tr.detect_amendment(AMENDED, {"conviction": 3})
    assert (got["from"], got["to"]) == (1, 3)
    assert [a["version"] for a in got["entries"]] == [3, 2]       # both are unseen


def test_amendments_since_falls_back_when_the_log_is_untagged():
    untagged = {"amendments": [{"date": "2026-08-31", "version": None, "text": "Newest."},
                               {"date": "2026-06-15", "version": None, "text": "Older."}]}
    assert [a["text"] for a in tr.amendments_since(untagged, 1)] == ["Newest."]
    assert tr.amendments_since({"amendments": []}, 1) == []


def test_prior_context_reframes_the_prompt_when_amended():
    rec = {"conviction": 3, "conviction_note": "held", "catalysts": [], "holdings": []}
    amendment = {"from": 1, "to": 2,
                 "entries": [{"date": "2026-08-31", "version": 2, "text": "Rotated."}]}

    plain = tr.build_prior_context("2026-08-24", rec, "2026-08-31")
    assert "AMENDED SINCE THAT TAKE" not in plain

    framed = tr.build_prior_context("2026-08-24", rec, "2026-08-31", amendment)
    assert "AMENDED SINCE THAT TAKE (v1 -> v2)" in framed
    assert "2026-08-31: Rotated." in framed
    assert "do not relitigate the framing the investor has already dropped" in framed


def test_render_amendment_banner():
    assert tr.render_amendment_banner(None) == ""
    banner = tr.render_amendment_banner(
        {"from": 2, "to": 3, "entries": [{"date": "2026-08-31", "text": "Rotated."}]})
    assert banner.startswith("> ") and "v2 → v3" in banner
    assert "> 2026-08-31: Rotated." in banner


def test_conviction_line_flags_an_amended_framing():
    meta = {"conviction": 4, "conviction_note": "n"}
    assert "under an amended framing" not in tr.render_conviction_line(meta, 3)
    amended = tr.render_conviction_line(meta, 3, amended=True)
    assert "↑ from 3 last week, under an amended framing" in amended
    # nothing to compare against on a first read
    assert tr.render_conviction_line(meta, None, amended=True).count("first read") == 1


def test_compute_and_render_stamps_a_new_chapter_on_reframe(tmp_path, monkeypatch):
    monkeypatch.setattr(tr, "REPORTS_DIR", tmp_path)
    _write_sidecar(tmp_path, "2026-06-08", {
        "key": "t", "title": "T", "inception_date": "2026-05-11", "conviction": 3,
        "equity_index": 118.0, "version": 1, "version_started": "2026-05-11",
        "holdings": [_h("AAA", 100.0, 100.0)]})
    enrichment = {"AAA": {"price": 110.0, "low_52w": 50, "high_52w": 150},
                  tr.BENCHMARK_TICKER: {"price": 420.0, "low_52w": 300, "high_52w": 500}}

    reframed = {**_result(key="t", title="T"), "version": 2,
                "amendments": [{"date": "2026-06-15", "version": 2, "text": "Rotated."}]}
    sections, records = tr.compute_and_render([reframed], enrichment, "2026-06-15")
    rec = records[0]
    assert rec["version"] == 2
    assert rec["version_started"] == "2026-06-15"       # the new chapter starts now
    assert rec["inception_date"] == "2026-05-11"        # the record itself continues
    assert rec["equity_index"] == pytest.approx(129.8)  # index keeps compounding
    assert "⚑ Amended (v1 → v2)" in sections[0]
    assert "Rotated." in sections[0]

    # an untouched thesis keeps its chapter stamp and shows no banner
    same = {**_result(key="t", title="T"), "version": 1}
    sections, records = tr.compute_and_render([same], enrichment, "2026-06-15")
    assert records[0]["version_started"] == "2026-05-11"
    assert "Amended" not in sections[0]


def test_first_tracked_week_starts_a_chapter_without_a_banner(tmp_path, monkeypatch):
    monkeypatch.setattr(tr, "REPORTS_DIR", tmp_path)
    enrichment = {tr.BENCHMARK_TICKER: {"price": 500.0, "low_52w": 400, "high_52w": 520}}
    sections, records = tr.compute_and_render(
        [{**_result(), "version": 3, "status": "watch"}], enrichment, "2026-06-08")
    assert records[0]["version"] == 3
    assert records[0]["version_started"] == "2026-06-08"
    assert records[0]["status"] == "watch"
    assert "Amended" not in sections[0]


def test_resolutions_relabel_to_the_current_title_after_a_rename(tmp_path, monkeypatch):
    monkeypatch.setattr(tr, "REPORTS_DIR", tmp_path)
    _write_sidecar(tmp_path, "2026-06-08", {
        "key": "old-slug", "title": "Old Framing", "conviction": 3,
        "catalyst_outcomes": [{"date": "2026-06-01", "what": "The event",
                               "outcome": "for", "note": "Resolved."}]})
    current = [{"key": "new-id", "title": "New Framing", "conviction": 3,
                "weekly_return": 0.01, "equity_index": 101.0}]

    section = tr.render_calibration_section(
        current, "2026-06-15",
        alias_map=tr.build_alias_map([{"id": "new-id", "aliases": ["old-slug"]}]))
    row = [l for l in section.splitlines() if l.startswith("| 2026-06-01 |")][0]
    assert "New Framing" in row and "Old Framing" not in row


# ── lifecycle: watch, retire, and the entry-price roll ─────────────────────

def _px(**prices):
    """Enrichment for the given tickers. Note _is_priced rejects on the mere
    PRESENCE of an 'error' key, so priced entries must not carry one."""
    return {t: {"price": p, "low_52w": 1.0, "high_52w": 9999.0}
            for t, p in prices.items()}


def test_roll_holdings_rebases_entries_and_remembers_the_original():
    rolled = tr.roll_holdings([_h("AAA", 100.0, 60.0), _h("BBB", 200.0, 40.0)],
                              _px(AAA=110.0, BBB=180.0))
    assert [h["entry_price"] for h in rolled] == [110.0, 180.0]
    assert [h["original_entry"] for h in rolled] == [100.0, 200.0]
    assert [h["weight_pct"] for h in rolled] == [60.0, 40.0]

    # rolling twice keeps the FIRST basis as the original
    again = tr.roll_holdings(rolled, _px(AAA=120.0, BBB=170.0))
    assert [h["original_entry"] for h in again] == [100.0, 200.0]


def test_roll_holdings_leaves_options_and_unpriceable_legs_alone():
    legs = [_h("OPT", None, 10.0, is_option=True), _h("GONE", 50.0, 20.0)]
    rolled = tr.roll_holdings(legs, _px(AAA=1.0))       # neither is priceable
    assert rolled[0]["entry_price"] is None
    assert rolled[1]["entry_price"] == 50.0             # nothing to re-base against
    assert "original_entry" not in rolled[1]


def test_a_watched_thesis_carries_its_book_without_recompounding(tmp_path, monkeypatch):
    """The bug this guards: a dormant thesis is never re-sized, so if its entry
    prices are not re-based each week its 'weekly' return is measured from the
    original entry every time and the same move compounds into the equity index
    week after week."""
    monkeypatch.setattr(tr, "REPORTS_DIR", tmp_path)

    def week(date_s, price, carry, holdings=None):
        r = {**_result(key="t", title="T"), "status": "watch" if carry else "active",
             "carry_holdings": carry, "holdings": holdings or []}
        sections, records = tr.compute_and_render(
            [r], _px(AAA=price, **{tr.BENCHMARK_TICKER: 500.0}), date_s)
        (tmp_path / f"{date_s}_research.json").write_text(
            json.dumps({"theses": records}), encoding="utf-8")
        return sections[0], records[0]

    week("2026-06-01", 100.0, False, [_h("AAA", 100.0, 100.0)])   # active, built at 100
    _, r2 = week("2026-06-08", 110.0, True)                       # +10% week
    _, r3 = week("2026-06-15", 110.0, True)                       # flat week
    _, r4 = week("2026-06-22", 99.0, True)                        # -10% week

    assert r2["equity_index"] == pytest.approx(110.0)
    assert r2["holdings"][0]["entry_price"] == 110.0    # re-based
    assert r2["holdings"][0]["original_entry"] == 100.0

    assert r3["weekly_return"] == pytest.approx(0.0)    # not +10% all over again
    assert r3["equity_index"] == pytest.approx(110.0)   # would be 121.0 unrolled
    assert r4["equity_index"] == pytest.approx(99.0)    # tracks AAA exactly


def test_watch_section_carries_a_badge_and_a_carried_note(tmp_path, monkeypatch):
    monkeypatch.setattr(tr, "REPORTS_DIR", tmp_path)
    _write_sidecar(tmp_path, "2026-06-08", {
        "key": "t", "title": "T", "inception_date": "2026-06-08", "conviction": 3,
        "equity_index": 100.0, "holdings": [_h("AAA", 100.0, 100.0)]})
    r = {**_result(key="t", title="T"), "status": "watch", "carry_holdings": True}
    sections, _ = tr.compute_and_render(
        [r], _px(AAA=110.0, **{tr.BENCHMARK_TICKER: 500.0}), "2026-06-15")

    assert "◔ **Watch**" in sections[0]
    assert "not re-sized" in sections[0]
    # an active thesis shows neither
    plain, _ = tr.compute_and_render(
        [_result(key="t", title="T")],
        _px(AAA=110.0, **{tr.BENCHMARK_TICKER: 500.0}), "2026-06-15")
    assert "Watch" not in plain[0] and "carried unchanged" not in plain[0]


def test_create_manifest_skips_retired_and_marks_watch_units(tmp_path):
    f = _thesis_file(tmp_path,
                     "## Thesis: Live one\n\n```yaml\nid: live\n```\n\nA.\n\n"
                     "## Thesis: Watched one\n\n```yaml\nid: watched\n"
                     "status: watch\n```\n\nB.\n\n"
                     "## Thesis: Old one\n\n```yaml\nid: old\nstatus: retired\n"
                     "retired_on: 2026-06-29\nretired_note: Closed.\n```\n\nC.\n")
    m = tr.create_manifest(f, tr.date(2026, 6, 29))

    assert [(u["id"], u["kind"]) for u in m["units"]] == [
        ("thesis-1", "thesis"), ("thesis-2", "watch"), ("new-thesis-scan", "scan")]
    assert [r["id"] for r in m["retired"]] == ["old"]
    assert m["retired"][0]["retired_on"] == "2026-06-29"
    assert m["retired"][0]["retired_note"] == "Closed."


def test_retired_thesis_moves_from_the_live_ledger_to_a_closed_table(tmp_path, monkeypatch):
    monkeypatch.setattr(tr, "REPORTS_DIR", tmp_path)
    _write_sidecar(tmp_path, "2026-06-08", {
        "key": "old", "title": "Old Idea", "conviction": 2, "weekly_return": 0.01,
        "equity_index": 112.0, "benchmark_index": 104.0})
    retired = [{"id": "old", "title": "Old Idea", "retired_on": "2026-06-29",
                "retired_note": "The mispricing closed."}]

    section = tr.render_calibration_section([], "2026-06-29", retired=retired)
    live = section.split("### Ledger")[1].split("###")[0]
    assert "Old Idea" not in live
    assert "### Closed theses" in section

    closed = section.split("### Closed theses")[1].split("###")[0]
    assert "Old Idea" in closed and "2026-06-29" in closed
    assert "+12.0%" in closed and "+8.0 pp" in closed      # final numbers kept
    assert "The mispricing closed." in closed


def test_retired_thesis_that_was_never_tracked_still_closes_cleanly(tmp_path, monkeypatch):
    monkeypatch.setattr(tr, "REPORTS_DIR", tmp_path)
    section = tr.render_calibration_section(
        [], "2026-06-29",
        retired=[{"id": "never", "title": "Never Tracked", "retired_on": "2026-06-29"}])
    closed = section.split("### Closed theses")[1]
    assert "Never Tracked" in closed and "| — | — |" in closed


def test_ledger_reports_thesis_status(tmp_path, monkeypatch):
    monkeypatch.setattr(tr, "REPORTS_DIR", tmp_path)
    section = tr.render_calibration_section(
        [{"key": "a", "title": "A", "status": "watch", "conviction": 2,
          "weekly_return": 0.0, "equity_index": 100.0},
         {"key": "b", "title": "B", "conviction": 4,
          "weekly_return": 0.0, "equity_index": 100.0}], "2026-06-29")
    assert "| Thesis | Status | Conviction |" in section
    rows = _ledger_rows(section)
    assert any(r.startswith("| A | watch |") for r in rows)
    assert any(r.startswith("| B | active |") for r in rows)   # default


def test_watch_prompt_forbids_new_names_and_allows_retirement():
    prompt = tr.build_watch_prompt("preamble", {"title": "T", "body": "B"},
                                   allow_web=False)
    assert "### Indexes" not in prompt and "### Stocks" not in prompt
    assert "Do NOT output an Indexes section" in prompt
    assert "recommend retirement" in prompt
    assert "conviction:" in prompt                     # still yields tracked metadata

    # past-due catalysts still demand structured verdicts, same contract as analysis
    due = [{"date": "2026-06-01", "what": "The event"}]
    with_due = tr.build_watch_prompt("p", {"title": "T", "body": "B"}, False,
                                     prior_context="CONTINUITY — ...", past_cats=due)
    assert "catalyst_outcomes" in with_due and "for | against | mixed | pending" in with_due


def test_scan_prompt_lists_retired_theses_with_a_reopen_condition():
    retired = [{"title": "Old Idea", "retired_on": "2026-06-29",
                "retired_note": "Closed."}]
    prompt = tr.build_new_thesis_prompt("p", ["Live one"], allow_web=False,
                                        retired_theses=retired)
    assert "PREVIOUSLY HELD AND RETIRED" in prompt
    assert "Old Idea (retired 2026-06-29) — Closed." in prompt
    assert "unless something material has changed" in prompt
    # retired theses are not held theses — they must not land in the do-not-suggest list
    held = prompt.split("THESES THEY ALREADY HOLD")[1].split("THESES THEY PREVIOUSLY")[0]
    assert "Old Idea" not in held

    assert "PREVIOUSLY HELD AND RETIRED" not in tr.build_new_thesis_prompt(
        "p", ["Live one"], allow_web=False)


# ── residual mode ──────────────────────────────────────────────────────────

def test_residual_mode_rewrites_the_job_and_names_the_spent():
    thesis = {"title": "T", "body": "B", "mode": "residual",
              "spent": ["SNOW", "DDOG"]}
    prompt = tr.build_analysis_prompt("preamble", thesis, allow_web=False)

    assert "RESIDUAL MODE" in prompt
    assert "largely priced in already" in prompt
    assert "do NOT put them forward again: SNOW, DDOG" in prompt
    assert "why it has not moved yet" in prompt
    assert "if the residual is empty, say that plainly" in prompt
    # it augments the normal pass rather than replacing it
    assert "### Steel man" in prompt and "### Stocks" in prompt


def test_standard_mode_carries_no_residual_block():
    prompt = tr.build_analysis_prompt("preamble", {"title": "T", "body": "B"},
                                      allow_web=False)
    assert "RESIDUAL MODE" not in prompt
    # a thesis dict from a pre-schema caller has no mode key at all
    assert "RESIDUAL" not in tr.build_analysis_prompt(
        "p", {"title": "T", "body": "B", "mode": "standard"}, allow_web=False)


def test_residual_mode_without_a_spent_list_still_reframes():
    prompt = tr.build_analysis_prompt("p", {"title": "T", "body": "B",
                                            "mode": "residual", "spent": []},
                                      allow_web=False)
    assert "RESIDUAL MODE" in prompt
    assert "considers these names spent" not in prompt   # no empty list dangling


def test_warn_spent_suggestions_logs_only_a_real_reappearance(capsys):
    thesis = {"spent": ["SNOW", "DDOG"]}
    tr.warn_spent_suggestions(thesis, ["MDB", "ESTC"])
    assert capsys.readouterr().out == ""

    tr.warn_spent_suggestions(thesis, ["MDB", "snow"])       # case-insensitive
    out = capsys.readouterr().out
    assert "SNOW" in out and "listed spent" in out
    assert "DDOG" not in out

    tr.warn_spent_suggestions({}, ["SNOW"])                  # no spent list
    assert capsys.readouterr().out == ""


def test_residual_section_carries_a_badge(tmp_path, monkeypatch):
    monkeypatch.setattr(tr, "REPORTS_DIR", tmp_path)
    enrichment = _px(**{tr.BENCHMARK_TICKER: 500.0})

    sections, records = tr.compute_and_render(
        [{**_result(), "mode": "residual"}], enrichment, "2026-06-08")
    assert "⌕ **Residual**" in sections[0]
    assert records[0]["mode"] == "residual"

    plain, plain_recs = tr.compute_and_render([_result()], enrichment, "2026-06-08")
    assert "Residual" not in plain[0]
    assert plain_recs[0]["mode"] == "standard"

    # watch wins the badge slot — a watched thesis suggests nothing either way
    watched, _ = tr.compute_and_render(
        [{**_result(), "mode": "residual", "status": "watch", "carry_holdings": True}],
        enrichment, "2026-06-08")
    assert "◔ **Watch**" in watched[0] and "Residual" not in watched[0]
