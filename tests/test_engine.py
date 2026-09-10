"""End-to-end behaviour and the invariants that must hold on any book."""

from __future__ import annotations

import pytest

from recon import build_dataset, evaluate, reconcile

SEEDS = [1, 2, 3, 5, 8]


@pytest.fixture(scope="module")
def runs():
    out = {}
    for seed in SEEDS:
        ds = build_dataset(events=500, seed=seed)
        result = reconcile(ds)
        out[seed] = (ds, result, evaluate(result))
    return out


# --------------------------------------------------------------------------
# Invariants — these must hold whatever the data looks like
# --------------------------------------------------------------------------


def test_no_transaction_is_matched_twice(runs):
    for seed, (_, result, _) in runs.items():
        seen: set[str] = set()
        for match in result.matches:
            ids = set(match.ledger_ids) | set(match.external_ids)
            assert not (ids & seen), f"seed {seed}: {ids & seen} matched twice"
            seen |= ids


def test_every_line_is_either_matched_or_explained(runs):
    """Conservation. A line that is neither matched nor in the exception
    report has silently disappeared, which is the worst possible outcome for
    a reconciliation: nobody knows it exists."""
    for seed, (ds, result, _) in runs.items():
        matched = result.matched_ids("ledger") | result.matched_ids("external")
        explained: set[str] = set()
        for b in result.breaks + result.review_queue:
            explained |= set(b.txn_ids)
        everything = {t.txn_id for t in ds.ledger} | {t.txn_id for t in ds.external}
        assert matched | explained == everything, f"seed {seed}: lines vanished"


def test_matched_and_explained_do_not_overlap(runs):
    for seed, (_, result, _) in runs.items():
        matched = result.matched_ids("ledger") | result.matched_ids("external")
        explained: set[str] = set()
        for b in result.breaks + result.review_queue:
            explained |= set(b.txn_ids)
        assert not (matched & explained), f"seed {seed}: line both cleared and broken"


def test_every_break_carries_a_rationale_and_an_action(runs):
    for _, result, _ in runs.values():
        for b in result.breaks + result.review_queue:
            assert b.rationale, f"{b.category} has no rationale"
            assert b.suggested_action, f"{b.category} has no suggested action"


def test_engine_is_deterministic():
    a = reconcile(build_dataset(events=300, seed=42))
    b = reconcile(build_dataset(events=300, seed=42))
    assert a.matched_pairs() == b.matched_pairs()
    assert [x.category for x in a.all_exceptions()] == [
        x.category for x in b.all_exceptions()
    ]


# --------------------------------------------------------------------------
# Quality gates — the numbers quoted in the README
# --------------------------------------------------------------------------


def test_precision_is_perfect_on_every_seed(runs):
    """The engine's core promise. A regression here means it started guessing."""
    for seed, (_, _, m) in runs.items():
        assert m.fp == 0, f"seed {seed}: {m.fp} false links"
        assert m.precision == 1.0


def test_recall_stays_above_the_published_floor(runs):
    for seed, (_, _, m) in runs.items():
        assert m.recall >= 0.92, f"seed {seed}: recall {m.recall:.3%}"


def test_auto_match_rate_stays_above_the_published_floor(runs):
    for seed, (_, _, m) in runs.items():
        assert m.auto_match_rate >= 0.86, f"seed {seed}: {m.auto_match_rate:.3%}"


def test_break_classifier_types_essentially_every_break_correctly(runs):
    """Measured mean over 25 books is 99.97%; the floor here is deliberately
    a little below that so a single unlucky book does not fail CI."""
    for seed, (_, _, m) in runs.items():
        assert m.classification_accuracy >= 0.99, (
            f"seed {seed}: {m.classification_correct}/{m.classification_total}"
        )


def test_in_transit_items_are_never_reported_as_breaks(runs):
    """Flagging items that are simply not due yet is how an exception queue
    becomes noise nobody reads."""
    for _, result, _ in runs.values():
        for b in result.breaks:
            if b.category == "IN_TRANSIT":
                assert b.severity == "info"


def test_grouped_settlements_are_actually_found(runs):
    for seed, (_, _, m) in runs.items():
        by_name = {r["scenario"]: r for r in m.per_scenario}
        for scenario in ("batch_settlement", "split_settlement"):
            row = by_name.get(scenario)
            if row and row["groups"] >= 5:
                assert row["recovery_rate"] >= 0.75, f"seed {seed}: {scenario}"


def test_ambiguous_clusters_are_escalated_not_guessed(runs):
    """The engine is allowed to miss these. It is not allowed to invent links."""
    for _, result, m in runs.values():
        by_name = {r["scenario"]: r for r in m.per_scenario}
        row = by_name.get("ambiguous_cluster")
        if not row or row["groups"] < 3:
            continue
        assert row["recovery_rate"] < 0.6
        assert any(b.category == "NEEDS_REVIEW" for b in result.review_queue)
