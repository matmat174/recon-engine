"""Command line interface.

    python -m recon demo                       full run on generated data + HTML report
    python -m recon run --ledger a.csv --external b.csv
    python -m recon bench --seeds 20           stability of the metrics across books
    python -m recon export --out data/         write the synthetic CSVs
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from datetime import date
from pathlib import Path

from .engine import reconcile
from .generate import FX_RATES, build_dataset
from .io_csv import load_dataset, write_dataset
from .metrics import evaluate
from .report import render

BOLD, DIM, GREEN, YELLOW, RESET = "\033[1m", "\033[2m", "\033[32m", "\033[33m", "\033[0m"


def _supports_color() -> bool:
    return sys.stdout.isatty()


def _c(text: str, code: str) -> str:
    return f"{code}{text}{RESET}" if _supports_color() else text


def _print_summary(metrics, runtime_ms: float, ccy: str = "EUR") -> None:
    lines = metrics.total_ledger + metrics.total_external
    print()
    print(_c("  Reconciliation summary", BOLD))
    print(f"  {'-' * 58}")
    print(f"  lines processed          {lines:>12,}   ({runtime_ms:.0f} ms)")
    print(
        f"  auto-matched             {metrics.auto_match_rate:>11.2%}   "
        f"({metrics.matched_ledger + metrics.matched_external:,} lines)"
    )
    print(f"  auto-matched by value    {metrics.auto_match_rate_value:>11.2%}")
    if metrics.classification_total:
        flag = _c("OK", GREEN) if metrics.fp == 0 else _c("CHECK", YELLOW)
        print(
            f"  precision                {metrics.precision:>11.4%}   "
            f"({metrics.fp} false links)  {flag}"
        )
        print(
            f"  recall                   {metrics.recall:>11.2%}   "
            f"({metrics.recall_operational:.2%} excl. policy-withheld)"
        )
        print(
            f"  break classification     {metrics.classification_accuracy:>11.2%}   "
            f"({metrics.classification_correct}/{metrics.classification_total})"
        )
    print(f"  exceptions               {metrics.exceptions:>12,}")
    print(f"  needs human review       {metrics.review_queue:>12,}")
    print(f"  value at risk            {float(metrics.value_at_risk):>12,.0f} {ccy}")
    print(f"  analyst time saved       {metrics.analyst_hours_saved:>12,.0f} h")
    print()


def _run(dataset, out_html: Path | None, out_json: Path | None) -> int:
    t0 = time.perf_counter()
    result = reconcile(dataset)
    runtime_ms = (time.perf_counter() - t0) * 1000
    metrics = evaluate(result)
    _print_summary(metrics, runtime_ms, dataset.base_currency)

    if out_html:
        out_html.parent.mkdir(parents=True, exist_ok=True)
        out_html.write_text(render(result, metrics, runtime_ms), encoding="utf-8")
        print(f"  report  -> {out_html}")
    if out_json:
        out_json.parent.mkdir(parents=True, exist_ok=True)
        payload = dict(metrics.as_dict(), runtime_ms=round(runtime_ms, 1))
        out_json.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(f"  metrics -> {out_json}")
    if out_html or out_json:
        print()
    return 0


def cmd_demo(args: argparse.Namespace) -> int:
    ds = build_dataset(events=args.events, seed=args.seed)
    return _run(ds, Path(args.out), Path(args.json) if args.json else None)


def cmd_run(args: argparse.Namespace) -> int:
    ds = load_dataset(
        args.ledger,
        args.external,
        fx_rates=FX_RATES,
        base_currency=args.base_currency,
        as_of=date.fromisoformat(args.as_of) if args.as_of else None,
    )
    return _run(
        ds,
        Path(args.out) if args.out else None,
        Path(args.json) if args.json else None,
    )


def cmd_export(args: argparse.Namespace) -> int:
    ds = build_dataset(events=args.events, seed=args.seed)
    paths = write_dataset(ds, args.out)
    for name, path in paths.items():
        print(f"  {name:9} -> {path}")
    return 0


def cmd_bench(args: argparse.Namespace) -> int:
    """Metrics on one book prove nothing. Run many and report the spread."""
    rows = []
    for seed in range(1, args.seeds + 1):
        ds = build_dataset(events=args.events, seed=seed)
        t0 = time.perf_counter()
        result = reconcile(ds)
        ms = (time.perf_counter() - t0) * 1000
        m = evaluate(result)
        rows.append(
            {
                "seed": seed,
                "lines": m.total_ledger + m.total_external,
                "precision": m.precision,
                "recall": m.recall,
                "auto": m.auto_match_rate,
                "classification": m.classification_accuracy,
                "false_links": m.fp,
                "ms": ms,
            }
        )

    def spread(key: str) -> str:
        vals = [r[key] for r in rows]
        lo, hi = min(vals), max(vals)
        return f"{statistics.mean(vals):.4%}  [{lo:.4%} .. {hi:.4%}]"

    total_false = sum(r["false_links"] for r in rows)
    total_lines = sum(r["lines"] for r in rows)
    print()
    print(_c(f"  Benchmark over {args.seeds} independent books", BOLD))
    print(f"  {'-' * 58}")
    print(f"  lines processed          {total_lines:,}")
    print(f"  auto-match rate          {spread('auto')}")
    print(f"  precision                {spread('precision')}")
    print(f"  recall                   {spread('recall')}")
    print(f"  break classification     {spread('classification')}")
    print(f"  false links (total)      {total_false}")
    print(
        f"  runtime                  "
        f"{statistics.mean(r['ms'] for r in rows):.0f} ms/book "
        f"({total_lines / max(sum(r['ms'] for r in rows) / 1000, 1e-9):,.0f} lines/s)"
    )
    print()
    if args.json:
        Path(args.json).write_text(json.dumps(rows, indent=2), encoding="utf-8")
        print(f"  detail -> {args.json}\n")
    return 1 if total_false > args.max_false_links else 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="recon",
        description="Transaction reconciliation with explainable break classification.",
    )
    sub = p.add_subparsers(dest="command", required=True)

    d = sub.add_parser("demo", help="generate a book, reconcile it, write the report")
    d.add_argument("--events", type=int, default=1200)
    d.add_argument("--seed", type=int, default=7)
    d.add_argument("--out", default="docs/report.html")
    d.add_argument("--json", default=None)
    d.set_defaults(func=cmd_demo)

    r = sub.add_parser("run", help="reconcile two CSV extracts")
    r.add_argument("--ledger", required=True)
    r.add_argument("--external", required=True)
    r.add_argument("--out", default=None, help="write an HTML report here")
    r.add_argument("--json", default=None)
    r.add_argument("--base-currency", default="EUR")
    r.add_argument("--as-of", default=None, help="YYYY-MM-DD; defaults to latest date seen")
    r.set_defaults(func=cmd_run)

    e = sub.add_parser("export", help="write the synthetic book to CSV")
    e.add_argument("--events", type=int, default=1200)
    e.add_argument("--seed", type=int, default=7)
    e.add_argument("--out", default="data")
    e.set_defaults(func=cmd_export)

    b = sub.add_parser("bench", help="run many books and report the metric spread")
    b.add_argument("--seeds", type=int, default=20)
    b.add_argument("--events", type=int, default=1200)
    b.add_argument("--json", default=None)
    b.add_argument(
        "--max-false-links",
        type=int,
        default=0,
        help="exit non-zero above this many false links (CI gate)",
    )
    b.set_defaults(func=cmd_bench)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
