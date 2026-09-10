from datetime import date
from decimal import Decimal

from recon.config import DEFAULT_CONFIG, MatchConfig
from recon.matchers import (
    Candidate,
    _allowed_fee,
    _subset_sums,
    exact_reference,
    make_context,
    resolve,
)
from recon.models import EXTERNAL, LEDGER, Transaction
from recon.normalize import normalize_all

D = Decimal


def txn(tid, side, amount, ref="INV-1", cp="Acme SAS", day=1, ccy="EUR"):
    return Transaction(
        txn_id=tid,
        side=side,
        booking_date=date(2026, 9, day),
        amount=D(amount),
        currency=ccy,
        counterparty=cp,
        reference=ref,
    )


# --------------------------------------------------------------------------
# Subset-sum search
# --------------------------------------------------------------------------


def test_subset_sums_finds_the_exact_group():
    items = [("a", D("100")), ("b", D("250")), ("c", D("75")), ("d", D("999"))]
    sols = _subset_sums(items, D("175"), D("0.02"), 4)
    assert [set(s) for s in sols] == [{"a", "c"}]


def test_subset_sums_requires_at_least_two_members():
    """A single item equal to the target is a 1:1 match, not a grouping — it
    must be left to the earlier, higher-precision strategies."""
    items = [("a", D("100")), ("b", D("50"))]
    assert _subset_sums(items, D("100"), D("0.02"), 4) == []


def test_subset_sums_reports_ambiguity_rather_than_picking_one():
    """Two different groups reaching the same total is not evidence — the
    caller must be able to see that and refuse."""
    items = [("a", D("50")), ("b", D("50")), ("c", D("25")), ("d", D("25"))]
    sols = _subset_sums(items, D("100"), D("0.02"), 4)
    assert len(sols) > 1


def test_subset_sums_respects_max_group_size():
    items = [(chr(97 + i), D("10")) for i in range(8)]
    sols = _subset_sums(items, D("70"), D("0.02"), 3)
    assert sols == []


# --------------------------------------------------------------------------
# Fee envelope
# --------------------------------------------------------------------------


def test_allowed_fee_scales_with_ticket_size():
    small = _allowed_fee(D("200"), DEFAULT_CONFIG)
    large = _allowed_fee(D("50000"), DEFAULT_CONFIG)
    assert small < D("35")  # a flat EUR 30 wire fee on EUR 200 is not credible
    assert large == D("50000") * DEFAULT_CONFIG.max_fee_pct


def test_allowed_fee_never_swallows_a_material_discrepancy():
    for amount in (D("500"), D("5000"), D("50000")):
        # The generator's amount_mismatch scenario starts at a 7% shortfall.
        assert _allowed_fee(amount, DEFAULT_CONFIG) < amount * D("0.07")


# --------------------------------------------------------------------------
# Resolution policy
# --------------------------------------------------------------------------


def _ctx(ledger, external, cfg=None):
    return make_context(cfg or DEFAULT_CONFIG, normalize_all(ledger), normalize_all(external))


def test_resolver_refuses_a_tie():
    """Two ledger rows, one statement row, nothing to tell them apart."""
    ctx = _ctx(
        [txn("L1", LEDGER, "100.00"), txn("L2", LEDGER, "100.00")],
        [txn("X1", EXTERNAL, "100.00")],
    )
    matches, contested = resolve(exact_reference(ctx), ctx)
    assert matches == []
    assert contested == {"L1", "L2", "X1"}


def test_resolver_accepts_a_clear_winner():
    ctx = _ctx([txn("L1", LEDGER, "100.00")], [txn("X1", EXTERNAL, "100.00")])
    matches, contested = resolve(exact_reference(ctx), ctx)
    assert len(matches) == 1
    assert matches[0].ledger_ids == ("L1",)
    assert matches[0].external_ids == ("X1",)
    assert contested == set()
    assert matches[0].evidence, "every accepted match must justify itself"


def test_resolver_accepts_when_the_margin_is_cleared():
    cfg = MatchConfig(ambiguity_margin=0.05)
    ctx = _ctx([txn("L1", LEDGER, "100.00")], [txn("X1", EXTERNAL, "100.00")], cfg)
    strong = Candidate(("L1",), ("X1",), "s", 1.0, 0.90, ("strong",))
    weak = Candidate(("L1",), ("X1",), "s", 1.0, 0.40, ("weak",))
    matches, _ = resolve([strong, weak], ctx)
    assert len(matches) == 1
    assert matches[0].evidence == ("strong",)


def test_resolver_never_consumes_a_transaction_twice():
    ctx = _ctx(
        [txn("L1", LEDGER, "100.00")],
        [txn("X1", EXTERNAL, "100.00"), txn("X2", EXTERNAL, "100.00", ref="INV-2")],
    )
    a = Candidate(("L1",), ("X1",), "s", 1.0, 0.95, ("a",))
    b = Candidate(("L1",), ("X2",), "s", 1.0, 0.10, ("b",))
    matches, _ = resolve([a, b], ctx)
    assert len(matches) == 1
    assert "L1" not in ctx.open_ledger
    assert "X2" in ctx.open_external
