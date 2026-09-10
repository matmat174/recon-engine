"""Grading the engine against ground truth.

An auto-match rate on its own is a vanity metric — you can push it to 100% by
matching everything to anything. The number that matters is precision: of the
links the engine created without a human, how many were right? A false match
removes an item from the exception queue and marks it cleared, so its cost is
not "a bit of rework", it is a break that nobody ever looks at again.

Everything here is pair-level: each truth group is expanded into (ledger,
external) pairs, and so is each produced match. Set arithmetic then gives
precision, recall and F1 without any judgement calls.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal

from .config import MatchConfig
from .models import EXTERNAL, LEDGER, ReconResult

ZERO = Decimal("0")

# What the engine is *supposed* to output for scenarios that must not be
# auto-matched. Used to grade the exception classifier, not the matcher.
EXPECTED_CATEGORY = {
    "not_settled": "NOT_SETTLED",
    "in_transit": "IN_TRANSIT",
    "unexpected_credit": "UNIDENTIFIED_RECEIPT",
    "duplicate_extra": "DUPLICATE_SUSPECTED",
    "amount_mismatch": "AMOUNT_MISMATCH",
}

# Scenarios where the two rows *are* the same payment but the amounts disagree
# materially. Auto-clearing those would bury a real discrepancy, so the engine
# is designed to miss them. Counting that as a recall failure would reward the
# wrong behaviour, so it is reported separately rather than quietly excluded.
POLICY_WITHHOLD_SCENARIOS = {"amount_mismatch"}


def _safe_div(a: float, b: float) -> float:
    return a / b if b else 0.0


@dataclass
class Metrics:
    total_ledger: int
    total_external: int
    matched_ledger: int
    matched_external: int
    value_total: Decimal
    value_matched: Decimal
    value_at_risk: Decimal
    tp: int
    fp: int
    fn: int
    policy_withheld: int
    exceptions: int
    review_queue: int
    classification_correct: int
    classification_total: int
    per_scenario: list[dict] = field(default_factory=list)
    per_strategy: list[dict] = field(default_factory=list)
    per_category: list[dict] = field(default_factory=list)
    analyst_hours_saved: float = 0.0

    # -- headline ratios --------------------------------------------------
    @property
    def auto_match_rate(self) -> float:
        return _safe_div(
            self.matched_ledger + self.matched_external,
            self.total_ledger + self.total_external,
        )

    @property
    def auto_match_rate_value(self) -> float:
        return _safe_div(float(self.value_matched), float(self.value_total))

    @property
    def precision(self) -> float:
        return _safe_div(self.tp, self.tp + self.fp)

    @property
    def recall(self) -> float:
        return _safe_div(self.tp, self.tp + self.fn)

    @property
    def recall_operational(self) -> float:
        """Recall over the links the engine is actually meant to create.

        Excludes pairs it withheld on purpose (see POLICY_WITHHOLD_SCENARIOS).
        Quote `recall` for the honest headline and this one when explaining
        where the gap comes from.
        """
        return _safe_div(self.tp, self.tp + self.fn - self.policy_withheld)

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return _safe_div(2 * p * r, p + r)

    @property
    def classification_accuracy(self) -> float:
        return _safe_div(self.classification_correct, self.classification_total)

    def as_dict(self) -> dict:
        return {
            "auto_match_rate": round(self.auto_match_rate, 4),
            "auto_match_rate_value": round(self.auto_match_rate_value, 4),
            "precision": round(self.precision, 6),
            "recall": round(self.recall, 6),
            "recall_operational": round(self.recall_operational, 6),
            "f1": round(self.f1, 6),
            "true_positive_links": self.tp,
            "false_positive_links": self.fp,
            "false_negative_links": self.fn,
            "policy_withheld_links": self.policy_withheld,
            "exceptions": self.exceptions,
            "review_queue": self.review_queue,
            "value_total": str(self.value_total),
            "value_matched": str(self.value_matched),
            "value_at_risk": str(self.value_at_risk),
            "classification_accuracy": round(self.classification_accuracy, 4),
            "analyst_hours_saved": round(self.analyst_hours_saved, 1),
            "per_scenario": self.per_scenario,
            "per_strategy": self.per_strategy,
            "per_category": self.per_category,
        }


def evaluate(result: ReconResult, cfg: MatchConfig | None = None) -> Metrics:
    ds = result.dataset
    cfg = cfg or MatchConfig(fx_rates=dict(ds.fx_rates), base_currency=ds.base_currency)

    produced = result.matched_pairs()
    truth = ds.truth_pairs()

    tp_pairs = produced & truth
    fp_pairs = produced - truth
    fn_pairs = truth - produced

    by_id = ds.by_id()

    def base(txn_id: str) -> Decimal:
        t = by_id[txn_id]
        return abs(cfg.to_base(t.amount, t.currency))

    value_total = sum((base(t.txn_id) for t in ds.ledger), ZERO)
    matched_ledger_ids = result.matched_ids(LEDGER)
    matched_external_ids = result.matched_ids(EXTERNAL)
    value_matched = sum((base(i) for i in matched_ledger_ids), ZERO)

    at_risk = sum(
        (abs(b.amount_base) for b in result.breaks if b.severity in ("critical", "high")),
        ZERO,
    )

    # -- per scenario -----------------------------------------------------
    per_scenario: list[dict] = []
    class_correct = class_total = 0
    categories_by_txn: dict[str, str] = {}
    for b in result.breaks + result.review_queue:
        for tid in b.txn_ids:
            categories_by_txn[tid] = b.category

    scenario_rows: dict[str, dict] = defaultdict(
        lambda: {"groups": 0, "recovered": 0, "classified_ok": 0, "expects_match": False}
    )
    policy_withheld = 0
    for group in ds.truth:
        row = scenario_rows[group.scenario]
        row["groups"] += 1
        pairs = group.pairs()
        if pairs:
            row["expects_match"] = True
            if pairs <= produced:
                row["recovered"] += 1
            elif group.scenario in POLICY_WITHHOLD_SCENARIOS:
                policy_withheld += len(pairs - produced)
        expected = EXPECTED_CATEGORY.get(group.scenario)
        if expected:
            class_total += 1
            ids = group.ledger_ids + group.external_ids
            got = {categories_by_txn.get(i) for i in ids}
            if expected in got:
                class_correct += 1
                row["classified_ok"] += 1
    for name, row in sorted(scenario_rows.items()):
        per_scenario.append(
            {
                "scenario": name,
                "groups": row["groups"],
                "recovered": row["recovered"],
                "recovery_rate": round(_safe_div(row["recovered"], row["groups"]), 4)
                if row["expects_match"]
                else None,
                "expected_category": EXPECTED_CATEGORY.get(name),
                "classified_ok": row["classified_ok"] if EXPECTED_CATEGORY.get(name) else None,
            }
        )

    # -- per strategy -----------------------------------------------------
    strat: dict[str, dict] = defaultdict(
        lambda: {"matches": 0, "pairs": 0, "correct": 0, "value": ZERO}
    )
    for m in result.matches:
        s = strat[m.strategy]
        s["matches"] += 1
        for p in m.pairs():
            s["pairs"] += 1
            if p in truth:
                s["correct"] += 1
        s["value"] += sum((base(i) for i in m.ledger_ids), ZERO)
    per_strategy = [
        {
            "strategy": k,
            "matches": v["matches"],
            "pairs": v["pairs"],
            "precision": round(_safe_div(v["correct"], v["pairs"]), 6),
            "false_links": v["pairs"] - v["correct"],
            "value": str(v["value"]),
        }
        for k, v in sorted(strat.items(), key=lambda kv: -kv[1]["matches"])
    ]

    # -- per break category ----------------------------------------------
    cat: dict[str, dict] = defaultdict(lambda: {"count": 0, "value": ZERO})
    for b in result.breaks + result.review_queue:
        c = cat[b.category]
        c["count"] += 1
        c["value"] += abs(b.amount_base)
    per_category = [
        {"category": k, "count": v["count"], "value": str(v["value"])}
        for k, v in sorted(cat.items(), key=lambda kv: -kv[1]["count"])
    ]

    hours = (
        (len(matched_ledger_ids) + len(matched_external_ids))
        * cfg.seconds_per_manual_match
        / 3600.0
    )

    return Metrics(
        total_ledger=len(ds.ledger),
        total_external=len(ds.external),
        matched_ledger=len(matched_ledger_ids),
        matched_external=len(matched_external_ids),
        value_total=value_total,
        value_matched=value_matched,
        value_at_risk=at_risk,
        tp=len(tp_pairs),
        fp=len(fp_pairs),
        fn=len(fn_pairs),
        policy_withheld=policy_withheld,
        exceptions=len(result.breaks),
        review_queue=len(result.review_queue),
        classification_correct=class_correct,
        classification_total=class_total,
        per_scenario=per_scenario,
        per_strategy=per_strategy,
        per_category=per_category,
        analyst_hours_saved=hours,
    )
