"""Tunable policy.

Everything an ops team would argue about lives here, not in the matching code:
tolerances, date windows, fee assumptions, settlement SLAs and — most
importantly — the ambiguity margin that decides when the engine refuses to
guess.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal


@dataclass
class MatchConfig:
    # --- date windows (calendar days) ---------------------------------
    settlement_window_days: int = 4
    """How late a settlement may land and still be considered the same event."""

    aggregate_window_days: int = 2
    """Tighter window for N:1 grouping — grouping is the riskiest strategy."""

    # --- amount tolerances --------------------------------------------
    fx_tolerance_bps: Decimal = Decimal("35")
    """Allowed drift when comparing across currencies, in basis points.
    Covers rate-fixing time differences and 2dp rounding on each leg."""

    absolute_tolerance: Decimal = Decimal("0.02")
    """Rounding slack in the settlement currency."""

    max_fee_pct: Decimal = Decimal("0.035")
    max_fee_fixed: Decimal = Decimal("35.00")
    """Upper bound on a plausible PSP/correspondent-bank deduction. Anything
    bigger is a real discrepancy, not a fee."""

    # --- similarity thresholds ----------------------------------------
    ref_similarity: float = 0.82
    counterparty_similarity: float = 0.72
    weak_similarity: float = 0.55

    # --- risk controls -------------------------------------------------
    ambiguity_margin: float = 0.06
    """A candidate is only auto-matched if it beats the runner-up by this
    margin. A false match silently *hides* a real break, so the engine is
    tuned for precision and pushes ties to a human review queue."""

    max_group_size: int = 4
    max_group_candidates: int = 14
    """Bounds on the subset-sum search for N:1 settlements. Without them the
    search is exponential and — worse — starts finding coincidental sums."""

    # --- operational ---------------------------------------------------
    settlement_sla_days: int = 3
    """A ledger entry younger than this that has not settled is 'in transit',
    not a break. Flagging in-transit items as breaks is the classic way to
    drown an ops team in noise."""

    duplicate_window_days: int = 7
    seconds_per_manual_match: int = 95
    """Measured average for a human to clear one exception in a spreadsheet
    workflow; used only to express savings in hours. Override it with your
    own number before quoting the figure."""

    # --- reporting ------------------------------------------------------
    base_currency: str = "EUR"
    fx_rates: dict[str, Decimal] = field(default_factory=dict)
    """Units of `currency` per 1 unit of base_currency (ECB quotation)."""

    def to_base(self, amount: Decimal, currency: str) -> Decimal:
        if currency == self.base_currency:
            return amount
        rate = self.fx_rates.get(currency)
        if not rate:
            return amount
        return (amount / rate).quantize(Decimal("0.01"))


DEFAULT_CONFIG = MatchConfig()
