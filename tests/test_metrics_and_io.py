from datetime import date
from decimal import Decimal

import pytest

from recon import build_dataset, evaluate, reconcile
from recon.io_csv import load_dataset, parse_amount, parse_date, write_dataset
from recon.metrics import Metrics
from recon.models import EXTERNAL, LEDGER, Dataset, Match, ReconResult, Transaction, TruthGroup
from recon.report import render

D = Decimal


# --------------------------------------------------------------------------
# Metric arithmetic on a hand-built case
# --------------------------------------------------------------------------


def _txn(tid, side, amount="100.00"):
    return Transaction(
        txn_id=tid,
        side=side,
        booking_date=date(2026, 9, 1),
        amount=D(amount),
        currency="EUR",
        counterparty="Acme SAS",
        reference=tid,
    )


def test_precision_and_recall_are_pair_level():
    ds = Dataset(
        ledger=[_txn("L1", LEDGER), _txn("L2", LEDGER), _txn("L3", LEDGER)],
        external=[_txn("X1", EXTERNAL), _txn("X2", EXTERNAL), _txn("X3", EXTERNAL)],
        truth=[
            TruthGroup(("L1",), ("X1",), "clean"),
            TruthGroup(("L2",), ("X2",), "clean"),
            TruthGroup(("L3",), ("X3",), "clean"),
        ],
        base_currency="EUR",
        as_of=date(2026, 9, 1),
    )
    # One right, one wrong, one missed.
    result = ReconResult(
        matches=[
            Match(("L1",), ("X1",), "s", 1.0, ("ok",)),
            Match(("L2",), ("X3",), "s", 1.0, ("wrong",)),
        ],
        breaks=[],
        review_queue=[],
        pass_stats=[],
        dataset=ds,
    )
    m = evaluate(result)
    assert (m.tp, m.fp, m.fn) == (1, 1, 2)
    assert m.precision == 0.5
    assert m.recall == pytest.approx(1 / 3)


def test_grouped_match_expands_into_every_pair():
    ds = Dataset(
        ledger=[_txn("L1", LEDGER, "40"), _txn("L2", LEDGER, "60")],
        external=[_txn("X1", EXTERNAL, "100")],
        truth=[TruthGroup(("L1", "L2"), ("X1",), "batch_settlement")],
        base_currency="EUR",
        as_of=date(2026, 9, 1),
    )
    result = ReconResult(
        matches=[Match(("L1", "L2"), ("X1",), "batch_aggregate", 0.86, ("ok",))],
        breaks=[],
        review_queue=[],
        pass_stats=[],
        dataset=ds,
    )
    m = evaluate(result)
    assert (m.tp, m.fp, m.fn) == (2, 0, 0)
    assert m.precision == 1.0 and m.recall == 1.0


def test_metrics_are_safe_on_an_empty_book():
    m = Metrics(0, 0, 0, 0, D("0"), D("0"), D("0"), 0, 0, 0, 0, 0, 0, 0, 0)
    assert m.precision == 0.0 and m.recall == 0.0 and m.f1 == 0.0


# --------------------------------------------------------------------------
# CSV parsing — bank exports are not tidy
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("1234.56", "1234.56"),
        ("1,234.56", "1234.56"),
        ("1 234,56", "1234.56"),
        ("1.234,56", "1234.56"),
        ("-99.00", "-99.00"),
        ("(99.00)", "-99.00"),
        ("EUR 1,000.00", "1000.00"),
    ],
)
def test_parse_amount_handles_locale_variants(raw, expected):
    assert parse_amount(raw) == D(expected)


def test_parse_amount_returns_decimal_not_float():
    assert isinstance(parse_amount("0.10"), Decimal)
    assert parse_amount("0.10") + parse_amount("0.20") == D("0.30")


@pytest.mark.parametrize(
    "raw", ["2026-09-08", "08/09/2026", "08.09.2026", "2026/09/08"]
)
def test_parse_date_handles_common_formats(raw):
    parsed = parse_date(raw)
    assert parsed.year == 2026 and parsed.day in (8, 9)


def test_csv_round_trip_preserves_the_reconciliation(tmp_path):
    original = build_dataset(events=200, seed=11)
    paths = write_dataset(original, tmp_path)
    reloaded = load_dataset(
        paths["ledger"],
        paths["external"],
        fx_rates=original.fx_rates,
        as_of=original.as_of,
    )
    assert reconcile(original).matched_pairs() == reconcile(reloaded).matched_pairs()


# --------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------


def test_report_is_self_contained_and_complete():
    ds = build_dataset(events=200, seed=3)
    result = reconcile(ds)
    html = render(result, evaluate(result), 12.3)
    assert html.startswith("<!doctype html>")
    assert "</html>" in html
    for forbidden in ("<script src", "http://", "https://cdn"):
        assert forbidden not in html, f"report reaches out to the network: {forbidden}"
    for expected in ("Auto-matched", "Match precision", "Top exceptions", "prefers-color-scheme"):
        assert expected in html
