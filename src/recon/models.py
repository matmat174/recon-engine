"""Core domain types.

Money is `Decimal` everywhere. Floats are never used to hold an amount: a
reconciliation engine that is off by 0.01 because of binary floating point
produces a *false break*, which costs an analyst real time to investigate.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

# --------------------------------------------------------------------------
# Sides
# --------------------------------------------------------------------------

LEDGER = "ledger"
EXTERNAL = "external"


# --------------------------------------------------------------------------
# Transactions
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Transaction:
    """One line on one side of the reconciliation.

    `ledger` rows are what our own system of record believes happened.
    `external` rows are what the PSP / bank statement says actually settled.
    """

    txn_id: str
    side: str
    booking_date: date
    amount: Decimal
    currency: str
    counterparty: str
    reference: str
    method: str = "sepa"
    account: str = "MAIN"

    def __post_init__(self) -> None:
        if self.side not in (LEDGER, EXTERNAL):
            raise ValueError(f"unknown side: {self.side!r}")
        if not isinstance(self.amount, Decimal):
            raise TypeError("amount must be Decimal, not float — see module docstring")

    @property
    def is_ledger(self) -> bool:
        return self.side == LEDGER


@dataclass(frozen=True)
class NormalizedTxn:
    """A `Transaction` plus the derived keys the matchers actually index on."""

    txn: Transaction
    ref_canonical: str
    ref_digits: str
    counterparty_canonical: str
    amount_abs: Decimal

    @property
    def txn_id(self) -> str:
        return self.txn.txn_id

    @property
    def currency(self) -> str:
        return self.txn.currency

    @property
    def booking_date(self) -> date:
        return self.txn.booking_date


# --------------------------------------------------------------------------
# Ground truth (only known for generated data — used to grade the engine)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class TruthGroup:
    """What *really* happened, as emitted by the generator.

    A group is a settlement event: N ledger rows settling as M external rows.
    Either side may be empty (never settled / unexpected credit).
    """

    ledger_ids: tuple[str, ...]
    external_ids: tuple[str, ...]
    scenario: str

    def pairs(self) -> set[tuple[str, str]]:
        return {(led, e) for led in self.ledger_ids for e in self.external_ids}


@dataclass
class Dataset:
    ledger: list[Transaction]
    external: list[Transaction]
    truth: list[TruthGroup] = field(default_factory=list)
    fx_rates: dict[str, Decimal] = field(default_factory=dict)
    base_currency: str = "EUR"
    as_of: date | None = None

    def truth_pairs(self) -> set[tuple[str, str]]:
        out: set[tuple[str, str]] = set()
        for g in self.truth:
            out |= g.pairs()
        return out

    def by_id(self) -> dict[str, Transaction]:
        return {t.txn_id: t for t in list(self.ledger) + list(self.external)}


# --------------------------------------------------------------------------
# Engine output
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Match:
    """An accepted reconciliation link.

    `evidence` is the human-readable justification shown to the ops analyst.
    An unexplainable auto-match is worse than no auto-match, so every strategy
    is required to populate it.
    """

    ledger_ids: tuple[str, ...]
    external_ids: tuple[str, ...]
    strategy: str
    confidence: float
    evidence: tuple[str, ...]
    residual_base: Decimal = Decimal("0")

    def pairs(self) -> set[tuple[str, str]]:
        return {(led, e) for led in self.ledger_ids for e in self.external_ids}

    @property
    def cardinality(self) -> str:
        n, m = len(self.ledger_ids), len(self.external_ids)
        if n == 1 and m == 1:
            return "1:1"
        if m == 1:
            return f"{n}:1"
        if n == 1:
            return f"1:{m}"
        return f"{n}:{m}"


SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}


@dataclass(frozen=True)
class Break:
    """An item the engine refused to auto-match, with a root cause."""

    txn_ids: tuple[str, ...]
    side: str
    category: str
    severity: str
    amount_base: Decimal
    rationale: tuple[str, ...]
    suggested_action: str
    near_miss: str | None = None

    @property
    def severity_rank(self) -> int:
        return SEVERITY_ORDER.get(self.severity, 9)


@dataclass
class ReconResult:
    matches: list[Match]
    breaks: list[Break]
    review_queue: list[Break]
    pass_stats: list[dict]
    dataset: Dataset

    # -- convenience ------------------------------------------------------
    def matched_pairs(self) -> set[tuple[str, str]]:
        out: set[tuple[str, str]] = set()
        for m in self.matches:
            out |= m.pairs()
        return out

    def matched_ids(self, side: str) -> set[str]:
        out: set[str] = set()
        for m in self.matches:
            out |= set(m.ledger_ids if side == LEDGER else m.external_ids)
        return out

    def all_exceptions(self) -> list[Break]:
        return sorted(
            self.breaks + self.review_queue,
            key=lambda b: (b.severity_rank, -abs(b.amount_base)),
        )


def total(amounts: Iterable[Decimal]) -> Decimal:
    out = Decimal("0")
    for a in amounts:
        out += a
    return out
