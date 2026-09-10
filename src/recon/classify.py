"""Break classification.

Leaving 400 rows in an "unmatched" bucket is not reconciliation — it is a
spreadsheet with extra steps. The value is in saying *why* each item did not
match and what to do about it, because that is what decides who picks it up:
treasury, the PSP account manager, or nobody at all (in-transit items).

The near-miss search is what makes this possible: for every unmatched item we
deliberately relax the tolerances that the matchers enforce, find the closest
thing on the other side, and report which constraint it violated.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date
from decimal import Decimal

from .matchers import MatchContext, _allowed_fee, _days, _fmt
from .models import EXTERNAL, LEDGER, Break, NormalizedTxn
from .normalize import counterparty_score, similarity

ZERO = Decimal("0")

CRITICAL_AT = Decimal("50000")
HIGH_AT = Decimal("5000")
MEDIUM_AT = Decimal("500")


def _severity(amount_base: Decimal, floor: str = "low") -> str:
    a = abs(amount_base)
    if a >= CRITICAL_AT:
        band = "critical"
    elif a >= HIGH_AT:
        band = "high"
    elif a >= MEDIUM_AT:
        band = "medium"
    else:
        band = "low"
    order = ["critical", "high", "medium", "low", "info"]
    return band if order.index(band) <= order.index(floor) else floor


# --------------------------------------------------------------------------
# Duplicate detection (runs before matching)
# --------------------------------------------------------------------------


def scan_duplicates(ctx: MatchContext) -> list[Break]:
    """Flag repeated statement lines before they can steal a match.

    Requires an identical, non-empty reference: without one, two payments of
    the same amount on the same day are far more likely to be two genuine
    payments than a double-post.
    """
    cfg = ctx.cfg
    groups: dict[tuple, list[NormalizedTxn]] = defaultdict(list)
    for n in ctx.open_external_txns():
        if not n.ref_canonical:
            continue
        groups[(n.ref_canonical, n.currency, n.amount_abs, n.counterparty_canonical)].append(n)

    breaks: list[Break] = []
    for members in groups.values():
        if len(members) < 2:
            continue
        members.sort(key=lambda n: (n.booking_date, n.txn_id))
        first = members[0]
        for dup in members[1:]:
            if _days(first, dup) > cfg.duplicate_window_days:
                continue
            amount_base = ctx.to_base(dup.amount_abs, dup.currency)
            ctx.open_external.discard(dup.txn_id)
            breaks.append(
                Break(
                    txn_ids=(dup.txn_id,),
                    side=EXTERNAL,
                    category="DUPLICATE_SUSPECTED",
                    severity=_severity(amount_base, floor="medium"),
                    amount_base=amount_base,
                    rationale=(
                        f"Same reference '{dup.txn.reference}', amount "
                        f"{_fmt(dup.amount_abs, dup.currency)} and counterparty as "
                        f"{first.txn_id} booked {_days(first, dup)}d earlier",
                        "Withheld from matching so it cannot consume the genuine item's match",
                    ),
                    suggested_action=(
                        f"Confirm with the PSP whether {dup.txn_id} is a re-post of "
                        f"{first.txn_id}; if so, raise a reversal."
                    ),
                    near_miss=first.txn_id,
                )
            )
    return breaks


# --------------------------------------------------------------------------
# Near-miss search
# --------------------------------------------------------------------------


def _identity(a: NormalizedTxn, b: NormalizedTxn) -> float:
    return max(
        similarity(a.ref_canonical, b.ref_canonical) if a.ref_canonical and b.ref_canonical else 0.0,
        counterparty_score(a.counterparty_canonical, b.counterparty_canonical),
    )


def _pair_residuals(
    ctx: MatchContext,
) -> tuple[list[tuple[NormalizedTxn, NormalizedTxn, float]], set[str]]:
    """Pair up leftovers that demonstrably refer to the same payment.

    The amount is deliberately ignored here — an amount discrepancy is exactly
    the finding we want to surface, so it cannot also be the thing that proves
    two rows belong together.

    That leaves the reference as the only admissible evidence. Counterparty
    alone is not enough and this is the mistake worth avoiding: with a supplier
    book of a few hundred names, "same counterparty, roughly the same week"
    pairs a genuinely-missing payment with an unrelated unapplied receipt and
    reports one confident AMOUNT_MISMATCH instead of two real, differently-owned
    breaks. Under-pairing costs a line in the report; over-pairing sends ops
    chasing a discrepancy that does not exist.
    """
    cfg = ctx.cfg
    wide_window = cfg.settlement_window_days * 3 + 7
    by_ref: dict[str, list[NormalizedTxn]] = defaultdict(list)
    by_digits: dict[str, list[NormalizedTxn]] = defaultdict(list)
    for led in ctx.open_ledger_txns():
        if led.ref_canonical:
            by_ref[led.ref_canonical].append(led)
        if len(led.ref_digits) >= 5:
            by_digits[led.ref_digits[:5]].append(led)

    scored: list[tuple[float, NormalizedTxn, NormalizedTxn]] = []
    for x in ctx.open_external_txns():
        seen: set[str] = set()
        pool: list[NormalizedTxn] = []
        for source in (
            by_ref.get(x.ref_canonical, ()) if x.ref_canonical else (),
            by_digits.get(x.ref_digits[:5], ()) if len(x.ref_digits) >= 5 else (),
        ):
            for n in source:
                if n.txn_id not in seen:
                    seen.add(n.txn_id)
                    pool.append(n)
        for led in pool:
            if _days(led, x) > wide_window:
                continue
            ref_sim = similarity(led.ref_canonical, x.ref_canonical)
            exact_ref = bool(led.ref_canonical) and led.ref_canonical == x.ref_canonical
            if not exact_ref and ref_sim < cfg.ref_similarity:
                continue
            ident = max(ref_sim, _identity(led, x))
            scored.append((ident + (0.2 if exact_ref else 0.0), led, x))

    scored.sort(key=lambda t: (-t[0], t[1].txn_id, t[2].txn_id))
    used_l: set[str] = set()
    used_x: set[str] = set()
    pairs: list[tuple[NormalizedTxn, NormalizedTxn, float]] = []
    for score, led, x in scored:
        if led.txn_id in used_l or x.txn_id in used_x:
            continue
        used_l.add(led.txn_id)
        used_x.add(x.txn_id)
        pairs.append((led, x, score))
    return pairs, used_l | used_x


# --------------------------------------------------------------------------
# Residual classification
# --------------------------------------------------------------------------


def _classify_pair(
    ctx: MatchContext, led: NormalizedTxn, x: NormalizedTxn, ident: float
) -> Break:
    cfg = ctx.cfg
    l_base = ctx.to_base(led.amount_abs, led.currency)
    x_base = ctx.to_base(x.amount_abs, x.currency)
    diff_base = (x_base - l_base).quantize(Decimal("0.01"))
    delta_days = _days(led, x)

    if led.currency != x.currency:
        drift_bps = (abs(diff_base) / l_base * Decimal("10000")) if l_base else ZERO
        return Break(
            txn_ids=(led.txn_id, x.txn_id),
            side="both",
            category="FX_RATE_DISPUTE",
            severity=_severity(diff_base, floor="medium"),
            amount_base=diff_base,
            rationale=(
                f"Booked {_fmt(led.amount_abs, led.currency)}, settled "
                f"{_fmt(x.amount_abs, x.currency)}",
                f"Implied rate differs from the ECB reference by {drift_bps:.0f} bps, "
                f"above the {cfg.fx_tolerance_bps} bps tolerance",
                f"Identity confirmed at {ident:.0%}",
            ),
            suggested_action=(
                "Pull the dealt rate and value date from the FX confirmation; if the "
                "spread is genuine, book it to FX P&L rather than leaving it open."
            ),
            near_miss=x.txn_id,
        )

    gap = led.amount_abs - x.amount_abs
    if abs(gap) > cfg.absolute_tolerance:
        cap = _allowed_fee(led.amount_abs, cfg)
        pct = (gap / led.amount_abs * 100) if led.amount_abs else ZERO
        return Break(
            txn_ids=(led.txn_id, x.txn_id),
            side="both",
            category="AMOUNT_MISMATCH",
            severity=_severity(diff_base, floor="high"),
            amount_base=diff_base,
            rationale=(
                f"Booked {_fmt(led.amount_abs, led.currency)} but settled "
                f"{_fmt(x.amount_abs, x.currency)} — short by {_fmt(gap, led.currency)} "
                f"({pct:.1f}%)",
                f"Exceeds the fee envelope of {_fmt(cap, led.currency)}, so this is a "
                f"discrepancy rather than a deduction at source",
                f"Identity confirmed at {ident:.0%}",
            ),
            suggested_action=(
                "Raise a query with the counterparty for the shortfall and hold the "
                "invoice open for the balance."
            ),
            near_miss=x.txn_id,
        )

    return Break(
        txn_ids=(led.txn_id, x.txn_id),
        side="both",
        category="LATE_SETTLEMENT",
        severity=_severity(l_base, floor="low"),
        amount_base=l_base,
        rationale=(
            f"Amounts agree exactly ({_fmt(led.amount_abs, led.currency)}) but settlement "
            f"landed {delta_days}d after booking",
            f"Outside the {cfg.settlement_window_days}d matching window, so it was not "
            f"auto-matched",
            f"Identity confirmed at {ident:.0%}",
        ),
        suggested_action=(
            "Safe to clear manually. If this counterparty is routinely this late, "
            "widen the window for it rather than reviewing every cycle."
        ),
        near_miss=x.txn_id,
    )


def classify_residual(
    ctx: MatchContext, contested: set[str], as_of: date
) -> tuple[list[Break], list[Break]]:
    cfg = ctx.cfg
    breaks: list[Break] = []
    review: list[Break] = []

    pairs, paired_ids = _pair_residuals(ctx)
    for led, x, ident in pairs:
        if led.txn_id in contested or x.txn_id in contested:
            continue
        breaks.append(_classify_pair(ctx, led, x, ident))

    for led in ctx.open_ledger_txns():
        if led.txn_id in paired_ids:
            continue
        amount_base = ctx.to_base(led.amount_abs, led.currency)
        age = (as_of - led.booking_date).days
        if led.txn_id in contested:
            review.append(_contested(led, amount_base, LEDGER))
            continue
        if age <= cfg.settlement_sla_days:
            breaks.append(
                Break(
                    txn_ids=(led.txn_id,),
                    side=LEDGER,
                    category="IN_TRANSIT",
                    severity="info",
                    amount_base=amount_base,
                    rationale=(
                        f"Booked {age}d ago, inside the {cfg.settlement_sla_days}d "
                        f"settlement SLA",
                        "No statement line yet — expected, not a break",
                    ),
                    suggested_action="No action. Re-runs will pick it up automatically.",
                )
            )
            continue
        breaks.append(
            Break(
                txn_ids=(led.txn_id,),
                side=LEDGER,
                category="NOT_SETTLED",
                severity=_severity(amount_base, floor="high"),
                amount_base=amount_base,
                rationale=(
                    f"Booked {age}d ago — {age - cfg.settlement_sla_days}d past the "
                    f"settlement SLA",
                    "No statement line and no near match on the counterparty",
                    f"Cash exposure: {_fmt(amount_base, cfg.base_currency)}",
                ),
                suggested_action=(
                    "Chase the counterparty and confirm the payment was actually "
                    "instructed. This is the bucket where real cash goes missing."
                ),
            )
        )

    for x in ctx.open_external_txns():
        if x.txn_id in paired_ids:
            continue
        amount_base = ctx.to_base(x.amount_abs, x.currency)
        if x.txn_id in contested:
            review.append(_contested(x, amount_base, EXTERNAL))
            continue
        breaks.append(
            Break(
                txn_ids=(x.txn_id,),
                side=EXTERNAL,
                category="UNIDENTIFIED_RECEIPT",
                severity=_severity(amount_base, floor="medium"),
                amount_base=amount_base,
                rationale=(
                    f"Statement line of {_fmt(x.amount_abs, x.currency)} from "
                    f"'{x.txn.counterparty}' with no booked counterpart",
                    f"Reference on the statement: '{x.txn.reference or '(none)'}'",
                ),
                suggested_action=(
                    "Identify and book, or return the funds. Unapplied cash is a "
                    "customer-experience problem before it is an accounting one."
                ),
            )
        )

    return breaks, review


def _contested(n: NormalizedTxn, amount_base: Decimal, side: str) -> Break:
    return Break(
        txn_ids=(n.txn_id,),
        side=side,
        category="NEEDS_REVIEW",
        severity=_severity(amount_base, floor="medium"),
        amount_base=amount_base,
        rationale=(
            f"Several candidates scored within the ambiguity margin for "
            f"{_fmt(n.amount_abs, n.currency)} on {n.booking_date.isoformat()}",
            "Auto-matching here would be a coin flip, and a wrong match would hide "
            "a genuine break behind a cleared status",
        ),
        suggested_action=(
            "Human decision required. Adding an order/customer id to the payment "
            "reference removes this class of exception at source."
        ),
    )
