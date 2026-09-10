"""Self-contained HTML operations report.

No template engine, no chart library, no network: the output is one file you
can email to a controller. Colours come from a validated palette; every chart
is single-series with direct numeric labels, so nothing depends on colour
alone and the report stays readable printed, in dark mode, and on a phone.
"""

from __future__ import annotations

import html
from datetime import date, datetime
from decimal import Decimal

from .metrics import Metrics
from .models import Break, ReconResult

# Status palette — fixed, never themed. Always shipped with a text label.
SEVERITY_COLOR = {
    "critical": "#d03b3b",
    "high": "#ec835a",
    "medium": "#fab219",
    "low": "#8a8a86",
    "info": "#2a78d6",
}

CATEGORY_BLURB = {
    "NOT_SETTLED": "Booked, past SLA, no statement line. Cash exposure.",
    "IN_TRANSIT": "Booked inside the SLA. Expected, no action.",
    "UNIDENTIFIED_RECEIPT": "Money in with nothing to apply it to.",
    "DUPLICATE_SUSPECTED": "Statement line repeated. Withheld from matching.",
    "AMOUNT_MISMATCH": "Same payment, materially different amount.",
    "FX_RATE_DISPUTE": "Cross-currency drift beyond tolerance.",
    "LATE_SETTLEMENT": "Amounts agree, settled outside the window.",
    "NEEDS_REVIEW": "Ambiguous — auto-matching would be a guess.",
}


def _e(value: object) -> str:
    return html.escape(str(value), quote=True)


def _money(amount: Decimal, currency: str = "EUR") -> str:
    return f"{amount:,.0f} {currency}"


def _pct(value: float, digits: int = 1) -> str:
    return f"{value * 100:.{digits}f}%"


# --------------------------------------------------------------------------
# Components
# --------------------------------------------------------------------------


def _tile(label: str, value: str, sub: str = "", tone: str = "") -> str:
    tone_cls = f" tile--{tone}" if tone else ""
    sub_html = f'<div class="tile__sub">{_e(sub)}</div>' if sub else ""
    return (
        f'<div class="tile{tone_cls}">'
        f'<div class="tile__label">{_e(label)}</div>'
        f'<div class="tile__value">{_e(value)}</div>'
        f"{sub_html}</div>"
    )


def _bars(rows: list[tuple[str, float, str, str]], color: str | None = None) -> str:
    """rows: (label, value, display_value, tooltip). Single series, direct labels."""
    if not rows:
        return '<p class="muted">Nothing to show.</p>'
    peak = max((r[1] for r in rows), default=0) or 1
    out = ['<div class="bars">']
    for label, value, display, tip in rows:
        width = max(1.2, value / peak * 100)
        style = f"width:{width:.2f}%"
        if color:
            style += f";background:{color}"
        out.append(
            f'<div class="bars__row" title="{_e(tip)}">'
            f'<div class="bars__label">{_e(label)}</div>'
            f'<div class="bars__track"><div class="bars__fill" style="{style}"></div></div>'
            f'<div class="bars__value">{_e(display)}</div>'
            f"</div>"
        )
    out.append("</div>")
    return "".join(out)


def _severity_bars(counts: dict[str, int]) -> str:
    order = ["critical", "high", "medium", "low", "info"]
    peak = max(counts.values(), default=0) or 1
    out = ['<div class="bars">']
    for sev in order:
        n = counts.get(sev, 0)
        if not n:
            continue
        width = max(1.2, n / peak * 100)
        out.append(
            f'<div class="bars__row" title="{_e(sev)}: {n} items">'
            f'<div class="bars__label"><span class="dot" '
            f'style="background:{SEVERITY_COLOR[sev]}"></span>{_e(sev)}</div>'
            f'<div class="bars__track"><div class="bars__fill" '
            f'style="width:{width:.2f}%;background:{SEVERITY_COLOR[sev]}"></div></div>'
            f'<div class="bars__value">{n}</div></div>'
        )
    out.append("</div>")
    return "".join(out)


def _exception_rows(breaks: list[Break], limit: int) -> str:
    out = []
    for b in breaks[:limit]:
        rationale = "".join(f"<li>{_e(r)}</li>" for r in b.rationale)
        out.append(
            f'<tr class="exc">'
            f'<td class="mono">{_e(", ".join(b.txn_ids))}</td>'
            f'<td><span class="pill" style="--pill:{SEVERITY_COLOR[b.severity]}">'
            f"{_e(b.severity)}</span></td>"
            f'<td class="cat">{_e(b.category)}</td>'
            f'<td class="num">{_e(_money(abs(b.amount_base)))}</td>'
            f'<td class="why"><ul>{rationale}</ul>'
            f'<div class="action"><strong>Action:</strong> {_e(b.suggested_action)}</div>'
            f"</td></tr>"
        )
    return "".join(out)


# --------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------


def render(result: ReconResult, metrics: Metrics, runtime_ms: float = 0.0) -> str:
    ds = result.dataset
    ccy = ds.base_currency
    as_of = ds.as_of or date.today()

    # --- cascade waterfall ---------------------------------------------
    total_lines = metrics.total_ledger + metrics.total_external
    cumulative = 0
    cascade_rows: list[tuple[str, float, str, str]] = []
    for st in result.pass_stats:
        cleared = st["ledger_cleared"] + st["external_cleared"]
        if st["kind"] != "match" and cleared == 0:
            continue
        cumulative += cleared
        cascade_rows.append(
            (
                st["name"].replace("_", " "),
                float(cleared),
                f"{cleared:,}",
                f"{cleared} lines cleared in {st['ms']}ms — "
                f"{cumulative / total_lines:.1%} of the book cleared after this pass",
            )
        )

    # --- categories ------------------------------------------------------
    cat_rows: list[tuple[str, float, str, str]] = []
    for row in metrics.per_category:
        blurb = CATEGORY_BLURB.get(row["category"], "")
        cat_rows.append(
            (
                row["category"].replace("_", " ").title(),
                float(row["count"]),
                f"{row['count']:,}",
                f"{row['count']} items · {_money(Decimal(row['value']), ccy)} · {blurb}",
            )
        )

    severity_counts: dict[str, int] = {}
    for b in result.breaks + result.review_queue:
        severity_counts[b.severity] = severity_counts.get(b.severity, 0) + 1

    strategy_rows = "".join(
        f"<tr><td class='cat'>{_e(r['strategy'].replace('_', ' '))}</td>"
        f"<td class='num'>{r['matches']:,}</td>"
        f"<td class='num'>{r['pairs']:,}</td>"
        f"<td class='num'>{_pct(r['precision'], 2)}</td>"
        f"<td class='num'>{r['false_links']}</td>"
        f"<td class='num'>{_money(Decimal(r['value']), ccy)}</td></tr>"
        for r in metrics.per_strategy
    )

    scenario_rows = "".join(
        f"<tr><td class='cat'>{_e(r['scenario'].replace('_', ' '))}</td>"
        f"<td class='num'>{r['groups']:,}</td>"
        f"<td class='num'>"
        + (_pct(r["recovery_rate"], 1) if r["recovery_rate"] is not None else "—")
        + "</td>"
        f"<td class='cat muted'>{_e(r['expected_category'] or '')}</td>"
        f"<td class='num'>"
        + (
            f"{r['classified_ok']}/{r['groups']}"
            if r["classified_ok"] is not None
            else "—"
        )
        + "</td></tr>"
        for r in metrics.per_scenario
    )

    exceptions = result.all_exceptions()
    actionable = [b for b in exceptions if b.category != "IN_TRANSIT"]

    precision_tone = "good" if metrics.precision >= 0.999 else "warn"

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Reconciliation report — {_e(as_of.isoformat())}</title>
<style>
:root {{
  color-scheme: light;
  --plane:#f9f9f7; --surface:#fcfcfb; --ink:#0b0b0b; --ink-2:#52514e;
  --muted:#898781; --grid:#e1e0d9; --line:#c3c2b7; --accent:#2a78d6;
  --track:#eeedea; --ring:rgba(11,11,11,.10); --good:#006300;
}}
@media (prefers-color-scheme: dark) {{
  :root:not([data-theme="light"]) {{
    color-scheme: dark;
    --plane:#0d0d0d; --surface:#1a1a19; --ink:#fff; --ink-2:#c3c2b7;
    --muted:#898781; --grid:#2c2c2a; --line:#383835; --accent:#3987e5;
    --track:#252523; --ring:rgba(255,255,255,.10); --good:#0ca30c;
  }}
}}
:root[data-theme="dark"] {{
  color-scheme: dark;
  --plane:#0d0d0d; --surface:#1a1a19; --ink:#fff; --ink-2:#c3c2b7;
  --muted:#898781; --grid:#2c2c2a; --line:#383835; --accent:#3987e5;
  --track:#252523; --ring:rgba(255,255,255,.10); --good:#0ca30c;
}}
* {{ box-sizing:border-box; }}
body {{
  margin:0; background:var(--plane); color:var(--ink);
  font:14px/1.55 system-ui,-apple-system,"Segoe UI",sans-serif;
}}
.wrap {{ max-width:1080px; margin:0 auto; padding-block:32px; padding-inline:20px; }}
header h1 {{ font-size:1.5rem; margin:0 0 4px; letter-spacing:-.01em; }}
header p {{ margin:0; color:var(--ink-2); }}
.meta {{ color:var(--muted); font-size:.8rem; margin-top:6px; }}
section {{ margin-top:28px; }}
h2 {{ font-size:1rem; margin:0 0 4px; letter-spacing:-.005em; }}
h2 + .lede {{ margin:0 0 14px; color:var(--ink-2); font-size:.88rem; max-width:74ch; }}
.card {{
  background:var(--surface); border:1px solid var(--ring); border-radius:12px;
  padding:18px; overflow-x:auto;
}}
.tiles {{ display:grid; gap:10px; grid-template-columns:repeat(auto-fit,minmax(168px,1fr)); }}
.tile {{
  background:var(--surface); border:1px solid var(--ring); border-radius:12px; padding:14px 15px;
}}
.tile__label {{ color:var(--muted); font-size:.72rem; text-transform:uppercase; letter-spacing:.05em; }}
.tile__value {{ font-size:1.55rem; font-weight:600; margin-top:5px; letter-spacing:-.02em; }}
.tile__sub {{ color:var(--ink-2); font-size:.78rem; margin-top:3px; }}
.tile--good .tile__value {{ color:var(--good); }}
.tile--warn .tile__value {{ color:#d03b3b; }}
.bars {{ display:flex; flex-direction:column; gap:2px; }}
.bars__row {{
  display:grid; grid-template-columns:minmax(120px,1.1fr) minmax(90px,3fr) auto;
  gap:12px; align-items:center; padding:3px 0;
}}
.bars__row:hover {{ background:var(--track); border-radius:6px; }}
.bars__label {{ color:var(--ink-2); font-size:.82rem; display:flex; align-items:center; gap:7px; }}
.bars__track {{ background:var(--track); border-radius:4px; height:16px; }}
.bars__fill {{ height:16px; border-radius:0 4px 4px 0; background:var(--accent); }}
.bars__value {{ font-variant-numeric:tabular-nums; font-size:.82rem; color:var(--ink); min-width:52px; text-align:right; }}
.dot {{ width:9px; height:9px; border-radius:50%; display:inline-block; flex:none; }}
table {{ width:100%; border-collapse:collapse; font-size:.84rem; }}
th {{
  text-align:left; color:var(--muted); font-weight:500; font-size:.72rem;
  text-transform:uppercase; letter-spacing:.05em; padding:0 10px 8px 0;
  border-bottom:1px solid var(--grid); white-space:nowrap;
}}
td {{ padding:9px 10px 9px 0; border-bottom:1px solid var(--grid); vertical-align:top; }}
.num {{ text-align:right; font-variant-numeric:tabular-nums; white-space:nowrap; }}
th.num {{ text-align:right; }}
.mono {{ font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:.78rem; white-space:nowrap; }}
.cat {{ white-space:nowrap; }}
.muted {{ color:var(--muted); }}
.pill {{
  display:inline-block; padding:1px 8px; border-radius:999px; font-size:.7rem;
  border:1px solid var(--pill); color:var(--pill); text-transform:uppercase; letter-spacing:.04em;
}}
.why ul {{ margin:0; padding-left:16px; color:var(--ink-2); }}
.why li {{ margin:1px 0; }}
.action {{ margin-top:6px; font-size:.82rem; }}
.cols {{ display:grid; gap:16px; grid-template-columns:repeat(auto-fit,minmax(300px,1fr)); }}
footer {{ margin-top:32px; padding-top:16px; border-top:1px solid var(--grid); color:var(--muted); font-size:.78rem; }}
@media (max-width:560px) {{
  .bars__row {{ grid-template-columns:minmax(96px,1.2fr) 1fr auto; gap:8px; }}
  .tile__value {{ font-size:1.3rem; }}
}}
</style>
</head>
<body>
<div class="wrap">

<header>
  <h1>Reconciliation report</h1>
  <p>Internal ledger vs. settlement statements — cycle ending {_e(as_of.isoformat())}</p>
  <div class="meta">
    {metrics.total_ledger:,} ledger lines · {metrics.total_external:,} statement lines ·
    engine ran in {runtime_ms:.0f} ms · generated {_e(datetime.now().strftime('%Y-%m-%d %H:%M'))}
  </div>
</header>

<section>
  <div class="tiles">
    {_tile("Auto-matched", _pct(metrics.auto_match_rate),
           f"{metrics.matched_ledger + metrics.matched_external:,} of {total_lines:,} lines")}
    {_tile("Auto-matched by value", _pct(metrics.auto_match_rate_value),
           f"of {_money(metrics.value_total, ccy)} booked")}
    {_tile("Match precision", _pct(metrics.precision, 2),
           f"{metrics.fp} false links out of {metrics.tp + metrics.fp:,}",
           tone=precision_tone)}
    {_tile("Recall", _pct(metrics.recall, 1),
           f"{_pct(metrics.recall_operational, 1)} excl. withheld by policy")}
    {_tile("Exceptions", f"{len(actionable):,}",
           f"{metrics.review_queue:,} need a human decision")}
    {_tile("Value at risk", _money(metrics.value_at_risk, ccy),
           "high & critical severity only")}
    {_tile("Analyst time saved", f"{metrics.analyst_hours_saved:.0f} h",
           "vs. clearing every line by hand")}
    {_tile("Classifier accuracy", _pct(metrics.classification_accuracy, 1),
           f"{metrics.classification_correct}/{metrics.classification_total} breaks correctly typed")}
  </div>
</section>

<section>
  <h2>How the cascade cleared the book</h2>
  <p class="lede">Each pass runs only on what the previous ones could not resolve.
  Cheap exact-key strategies clear the bulk first, so the expensive fuzzy and
  combinatorial passes see a small, genuinely difficult residual.</p>
  <div class="card">{_bars(cascade_rows)}</div>
</section>

<section>
  <h2>Strategy quality</h2>
  <p class="lede">Precision is measured per strategy against ground truth. A
  strategy that produces false links is worse than one that produces none and
  matches less, because a false link marks a genuine break as cleared.</p>
  <div class="card">
    <table>
      <thead><tr>
        <th>Strategy</th><th class="num">Matches</th><th class="num">Links</th>
        <th class="num">Precision</th><th class="num">False links</th><th class="num">Value</th>
      </tr></thead>
      <tbody>{strategy_rows}</tbody>
    </table>
  </div>
</section>

<section>
  <div class="cols">
    <div>
      <h2>Exception queue</h2>
      <p class="lede">Grouped by root cause, not by "unmatched".</p>
      <div class="card">{_bars(cat_rows)}</div>
    </div>
    <div>
      <h2>Severity</h2>
      <p class="lede">Banded on exposure in {_e(ccy)}, floored by category.</p>
      <div class="card">{_severity_bars(severity_counts)}</div>
    </div>
  </div>
</section>

<section>
  <h2>Recovery by injected scenario</h2>
  <p class="lede">The generator knows what it created, so every break type can be
  scored independently. Recovery is the share of settlement events fully
  reconstructed; the last column grades the break classifier on the events that
  are <em>meant</em> to stay unmatched.</p>
  <div class="card">
    <table>
      <thead><tr>
        <th>Scenario</th><th class="num">Events</th><th class="num">Recovered</th>
        <th>Expected break</th><th class="num">Correctly typed</th>
      </tr></thead>
      <tbody>{scenario_rows}</tbody>
    </table>
  </div>
</section>

<section>
  <h2>Top exceptions</h2>
  <p class="lede">Ranked by severity then exposure. Every line carries the evidence
  the engine used and the action it implies — an exception nobody can act on is
  just a number.</p>
  <div class="card">
    <table>
      <thead><tr>
        <th>Reference</th><th>Severity</th><th>Category</th>
        <th class="num">Amount</th><th>Why it did not match</th>
      </tr></thead>
      <tbody>{_exception_rows(actionable, 25)}</tbody>
    </table>
  </div>
</section>

<footer>
  Generated by <strong>recon-engine</strong> on synthetic data with known ground
  truth. Precision, recall and classifier accuracy are measured against that
  ground truth, not estimated. FX conversions use ECB euro reference rates.
  Analyst-time savings assume 95 s to clear one line manually — substitute your
  own figure before quoting it.
</footer>

</div>
</body>
</html>
"""
