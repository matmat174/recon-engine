"""The matching cascade.

Each strategy is a pure function over the *still-open* pool and returns
`Candidate` links; it never mutates state. Acceptance is centralised in
`resolve()` so that one policy — "only auto-match when the winner is
unambiguous" — is applied identically everywhere.

Ordering matters: cheap, high-precision, exact-key strategies run first and
shrink the pool, so the expensive fuzzy and combinatorial strategies see a
much smaller and more interesting residual.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal

from .config import MatchConfig
from .models import Match, NormalizedTxn
from .normalize import counterparty_score, similarity

ZERO = Decimal("0")


@dataclass(frozen=True)
class Candidate:
    ledger_ids: tuple[str, ...]
    external_ids: tuple[str, ...]
    strategy: str
    confidence: float
    score: float
    evidence: tuple[str, ...]
    residual_base: Decimal = ZERO

    @property
    def members(self) -> tuple[str, ...]:
        return self.ledger_ids + self.external_ids


@dataclass
class MatchContext:
    cfg: MatchConfig
    ledger: dict[str, NormalizedTxn]
    external: dict[str, NormalizedTxn]
    open_ledger: set[str] = field(default_factory=set)
    open_external: set[str] = field(default_factory=set)
    contested: set[str] = field(default_factory=set)

    def to_base(self, amount: Decimal, currency: str) -> Decimal:
        return self.cfg.to_base(amount, currency)

    def live(self, cand: Candidate) -> bool:
        return all(i in self.open_ledger for i in cand.ledger_ids) and all(
            i in self.open_external for i in cand.external_ids
        )

    def consume(self, cand: Candidate) -> None:
        self.open_ledger -= set(cand.ledger_ids)
        self.open_external -= set(cand.external_ids)

    def open_ledger_txns(self) -> list[NormalizedTxn]:
        return [self.ledger[i] for i in self.open_ledger]

    def open_external_txns(self) -> list[NormalizedTxn]:
        return [self.external[i] for i in self.open_external]


def make_context(
    cfg: MatchConfig,
    ledger: Sequence[NormalizedTxn],
    external: Sequence[NormalizedTxn],
) -> MatchContext:
    lmap = {n.txn_id: n for n in ledger}
    xmap = {n.txn_id: n for n in external}
    return MatchContext(
        cfg=cfg,
        ledger=lmap,
        external=xmap,
        open_ledger=set(lmap),
        open_external=set(xmap),
    )


# --------------------------------------------------------------------------
# Shared helpers
# --------------------------------------------------------------------------


def _days(a: NormalizedTxn, b: NormalizedTxn) -> int:
    return abs((a.booking_date - b.booking_date).days)


def _date_score(delta_days: int, window: int) -> float:
    return max(0.0, 1.0 - delta_days / float(window + 1))


def _amount_close(a: Decimal, b: Decimal, tol: Decimal) -> bool:
    return abs(a - b) <= tol


def _allowed_fee(amount: Decimal, cfg: MatchConfig) -> Decimal:
    """Largest deduction that is still credibly a fee rather than a break.

    Percentage-based for large tickets, but a flat correspondent-bank charge
    is only plausible if it is small relative to the payment — otherwise a
    EUR 200 payment short by EUR 30 would be silently written off as a fee.
    """
    return max(amount * cfg.max_fee_pct, min(cfg.max_fee_fixed, amount * Decimal("0.06")))


def _fmt(amount: Decimal, currency: str) -> str:
    return f"{amount:,.2f} {currency}"


def ref_conflict(a: NormalizedTxn, b: NormalizedTxn, cfg: MatchConfig) -> bool:
    """Veto: both sides carry a reference and the two disagree.

    This is the single highest-value guard in the engine. Amount, date and
    counterparty routinely coincide across unrelated payments — a mid-size
    book has hundreds of EUR 1,250.00 transfers to the same supplier. Two
    *different* references, however, are positive evidence of two different
    payments, and outrank any amount coincidence. A missing or mangled
    reference is not a conflict; a contradictory one is.
    """
    ra, rb = a.ref_canonical, b.ref_canonical
    if not ra or not rb:
        return False  # nothing to contradict
    da, db = a.ref_digits, b.ref_digits
    if da and db:
        # Numeric spines are compared exactly, never fuzzily: INV-100001 and
        # INV-100002 are 83% similar as strings and are two different
        # invoices. A prefix relationship is truncation, not contradiction.
        if da == db or da.startswith(db) or db.startswith(da):
            return False
        return True
    return similarity(ra, rb) < cfg.weak_similarity


# --------------------------------------------------------------------------
# Strategy 1 — exact reference + amount + currency
# --------------------------------------------------------------------------


def exact_reference(ctx: MatchContext) -> list[Candidate]:
    buckets: dict[tuple, list[NormalizedTxn]] = defaultdict(list)
    for n in ctx.open_external_txns():
        if n.txn.reference.strip():
            buckets[(n.txn.reference.strip(), n.currency, n.amount_abs)].append(n)
    out: list[Candidate] = []
    for led in ctx.open_ledger_txns():
        if not led.txn.reference.strip():
            continue
        key = (led.txn.reference.strip(), led.currency, led.amount_abs)
        for x in buckets.get(key, ()):
            out.append(
                Candidate(
                    (led.txn_id,),
                    (x.txn_id,),
                    "exact_reference",
                    1.0,
                    1.0,
                    (
                        f"Reference '{led.txn.reference}' identical on both sides",
                        f"Amount and currency identical ({_fmt(led.amount_abs, led.currency)})",
                    ),
                )
            )
    return out


# --------------------------------------------------------------------------
# Strategy 2 — canonical reference (survives prefixes, padding, separators)
# --------------------------------------------------------------------------


def canonical_reference(ctx: MatchContext) -> list[Candidate]:
    cfg = ctx.cfg
    buckets: dict[tuple[str, str], list[NormalizedTxn]] = defaultdict(list)
    for n in ctx.open_external_txns():
        if n.ref_canonical:
            buckets[(n.ref_canonical, n.currency)].append(n)
    out: list[Candidate] = []
    for led in ctx.open_ledger_txns():
        if not led.ref_canonical:
            continue
        for x in buckets.get((led.ref_canonical, led.currency), ()):
            if not _amount_close(led.amount_abs, x.amount_abs, cfg.absolute_tolerance):
                continue
            delta = _days(led, x)
            if delta > cfg.settlement_window_days:
                continue
            out.append(
                Candidate(
                    (led.txn_id,),
                    (x.txn_id,),
                    "canonical_reference",
                    0.97,
                    0.97 - 0.01 * delta,
                    (
                        f"References '{led.txn.reference}' / '{x.txn.reference}' "
                        f"normalise to the same key '{led.ref_canonical}'",
                        f"Amount matches within {cfg.absolute_tolerance}",
                        f"Settled {delta}d after booking",
                    ),
                )
            )
    return out


# --------------------------------------------------------------------------
# Strategy 3 — identical amount, fuzzy identity
# --------------------------------------------------------------------------


def amount_bucket_fuzzy(ctx: MatchContext) -> list[Candidate]:
    """Same amount and currency; decide identity from reference/counterparty.

    Bucketing on the exact amount keeps this O(n) rather than O(n^2) — the
    fuzzy comparison only ever runs inside a bucket of items that already
    agree on the money.
    """
    cfg = ctx.cfg
    buckets: dict[tuple[str, Decimal], list[NormalizedTxn]] = defaultdict(list)
    for n in ctx.open_external_txns():
        buckets[(n.currency, n.amount_abs)].append(n)

    out: list[Candidate] = []
    for led in ctx.open_ledger_txns():
        for x in buckets.get((led.currency, led.amount_abs), ()):
            delta = _days(led, x)
            if delta > cfg.settlement_window_days:
                continue
            ref_sim = similarity(led.ref_canonical, x.ref_canonical)
            cp_sim = counterparty_score(led.counterparty_canonical, x.counterparty_canonical)
            if ref_sim < cfg.ref_similarity and cp_sim < cfg.counterparty_similarity:
                continue
            if ref_conflict(led, x, cfg):
                continue
            date_sim = _date_score(delta, cfg.settlement_window_days)
            score = 0.50 * ref_sim + 0.35 * cp_sim + 0.15 * date_sim
            evidence = [
                f"Identical amount and currency ({_fmt(led.amount_abs, led.currency)})",
                f"Settled {delta}d after booking",
            ]
            if ref_sim >= cfg.ref_similarity:
                evidence.append(
                    f"Reference similarity {ref_sim:.0%} "
                    f"('{led.txn.reference}' vs '{x.txn.reference}')"
                )
            if cp_sim >= cfg.counterparty_similarity:
                evidence.append(
                    f"Counterparty similarity {cp_sim:.0%} "
                    f"('{led.txn.counterparty}' vs '{x.txn.counterparty}')"
                )
            out.append(
                Candidate(
                    (led.txn_id,),
                    (x.txn_id,),
                    "amount_and_identity",
                    round(0.80 + 0.15 * score, 4),
                    round(score, 6),
                    tuple(evidence),
                )
            )
    return out


# --------------------------------------------------------------------------
# Strategy 4 — FX translated settlement
# --------------------------------------------------------------------------


def fx_translated(ctx: MatchContext) -> list[Candidate]:
    cfg = ctx.cfg
    by_ref: dict[str, list[NormalizedTxn]] = defaultdict(list)
    by_cp: dict[str, list[NormalizedTxn]] = defaultdict(list)
    for led in ctx.open_ledger_txns():
        if led.ref_canonical:
            by_ref[led.ref_canonical].append(led)
        if led.counterparty_canonical:
            by_cp[led.counterparty_canonical].append(led)

    tol_ratio = cfg.fx_tolerance_bps / Decimal("10000")
    out: list[Candidate] = []
    for x in ctx.open_external_txns():
        pool: list[NormalizedTxn] = list(by_ref.get(x.ref_canonical, ()))
        seen = {n.txn_id for n in pool}
        for n in by_cp.get(x.counterparty_canonical, ()):
            if n.txn_id not in seen:
                pool.append(n)
        for led in pool:
            if led.currency == x.currency:
                continue
            delta = _days(led, x)
            if delta > cfg.settlement_window_days:
                continue
            l_base = ctx.to_base(led.amount_abs, led.currency)
            x_base = ctx.to_base(x.amount_abs, x.currency)
            if l_base <= 0:
                continue
            drift = abs(x_base - l_base) / l_base
            if drift > tol_ratio:
                continue
            if ref_conflict(led, x, cfg):
                continue
            implied = (x.amount_abs / led.amount_abs) if led.amount_abs else ZERO
            ref_sim = similarity(led.ref_canonical, x.ref_canonical)
            cp_sim = counterparty_score(led.counterparty_canonical, x.counterparty_canonical)
            if max(ref_sim, cp_sim) < cfg.weak_similarity:
                continue
            score = 0.6 * (1 - float(drift / tol_ratio)) + 0.25 * max(ref_sim, cp_sim) + 0.15 * _date_score(delta, cfg.settlement_window_days)
            out.append(
                Candidate(
                    (led.txn_id,),
                    (x.txn_id,),
                    "fx_translated",
                    round(0.78 + 0.12 * max(ref_sim, cp_sim), 4),
                    round(score, 6),
                    (
                        f"Cross-currency: {_fmt(led.amount_abs, led.currency)} booked vs "
                        f"{_fmt(x.amount_abs, x.currency)} settled",
                        f"Implied rate {implied:.5f}; drift vs ECB reference "
                        f"{drift * 10000:.1f} bps (tolerance {cfg.fx_tolerance_bps} bps)",
                        f"Identity confirmed at {max(ref_sim, cp_sim):.0%}",
                    ),
                    residual_base=(x_base - l_base).quantize(Decimal("0.01")),
                )
            )
    return out


# --------------------------------------------------------------------------
# Strategy 5 — fee netted at source
# --------------------------------------------------------------------------


def fee_adjusted(ctx: MatchContext) -> list[Candidate]:
    cfg = ctx.cfg
    by_ref: dict[str, list[NormalizedTxn]] = defaultdict(list)
    by_cp: dict[str, list[NormalizedTxn]] = defaultdict(list)
    for led in ctx.open_ledger_txns():
        if led.ref_canonical:
            by_ref[led.ref_canonical].append(led)
        if led.counterparty_canonical:
            by_cp[led.counterparty_canonical].append(led)

    out: list[Candidate] = []
    for x in ctx.open_external_txns():
        pool: list[NormalizedTxn] = list(by_ref.get(x.ref_canonical, ()))
        seen = {n.txn_id for n in pool}
        for n in by_cp.get(x.counterparty_canonical, ()):
            if n.txn_id not in seen:
                pool.append(n)
        for led in pool:
            if led.currency != x.currency:
                continue
            delta = _days(led, x)
            if delta > cfg.settlement_window_days:
                continue
            fee = led.amount_abs - x.amount_abs
            if fee <= cfg.absolute_tolerance:
                continue
            cap = _allowed_fee(led.amount_abs, cfg)
            if fee > cap:
                continue
            if ref_conflict(led, x, cfg):
                continue
            ref_sim = similarity(led.ref_canonical, x.ref_canonical)
            cp_sim = counterparty_score(led.counterparty_canonical, x.counterparty_canonical)
            identity = max(ref_sim, cp_sim)
            if identity < cfg.counterparty_similarity:
                continue
            bps = (fee / led.amount_abs) * Decimal("10000")
            score = 0.55 * identity + 0.30 * float(1 - fee / cap) + 0.15 * _date_score(
                delta, cfg.settlement_window_days
            )
            out.append(
                Candidate(
                    (led.txn_id,),
                    (x.txn_id,),
                    "fee_adjusted",
                    round(0.74 + 0.14 * identity, 4),
                    round(score, 6),
                    (
                        f"Settled short by {_fmt(fee, led.currency)} ({bps:.0f} bps), "
                        f"within the {cfg.max_fee_pct:.1%} / "
                        f"{_fmt(cfg.max_fee_fixed, led.currency)} fee envelope",
                        f"Method '{led.txn.method}' — deduction at source is expected",
                        f"Identity confirmed at {identity:.0%}",
                    ),
                    residual_base=ctx.to_base(-fee, led.currency),
                )
            )
    return out


# --------------------------------------------------------------------------
# Strategies 6 & 7 — grouped settlements (N:1 and 1:M)
# --------------------------------------------------------------------------


def _subset_sums(
    items: list[tuple[str, Decimal]],
    target: Decimal,
    tol: Decimal,
    max_size: int,
    stop_after: int = 3,
) -> list[tuple[str, ...]]:
    """All subsets (size >= 2) summing to `target` within `tol`.

    Sorted descending with an overshoot prune. We stop early once several
    solutions exist: more than one way to reach the same total means the
    grouping is not evidence of anything, and the caller will bail out.
    """
    ordered = sorted(items, key=lambda kv: -kv[1])
    n = len(ordered)
    found: list[tuple[str, ...]] = []

    def rec(start: int, chosen: list[str], total: Decimal) -> bool:
        if len(chosen) >= 2 and abs(total - target) <= tol:
            found.append(tuple(chosen))
            return len(found) >= stop_after
        if len(chosen) >= max_size:
            return False
        for k in range(start, n):
            tid, amt = ordered[k]
            if total + amt > target + tol:
                continue
            chosen.append(tid)
            if rec(k + 1, chosen, total + amt):
                chosen.pop()
                return True
            chosen.pop()
        return False

    rec(0, [], ZERO)
    return found


def _group_pool(
    anchor: NormalizedTxn,
    pool: Iterable[NormalizedTxn],
    cfg: MatchConfig,
    back: int,
    forward: int,
) -> list[NormalizedTxn]:
    """Candidate members for a grouped settlement.

    The window is *directional*: invoices are booked before a batch payout
    lands, and instalments settle after the invoice is booked. Using one
    symmetric window for both would either miss real groups or drag in twice
    as many coincidental ones.
    """
    out = [
        n
        for n in pool
        if n.currency == anchor.currency
        and n.counterparty_canonical == anchor.counterparty_canonical
        and -back <= (n.booking_date - anchor.booking_date).days <= forward
        and n.amount_abs <= anchor.amount_abs
    ]
    out.sort(key=lambda n: abs((n.booking_date - anchor.booking_date).days))
    return out[: cfg.max_group_candidates]


def batch_aggregate(ctx: MatchContext) -> list[Candidate]:
    """Several booked items settling as one payout."""
    cfg = ctx.cfg
    by_cp: dict[str, list[NormalizedTxn]] = defaultdict(list)
    for led in ctx.open_ledger_txns():
        by_cp[led.counterparty_canonical].append(led)

    out: list[Candidate] = []
    for x in ctx.open_external_txns():
        pool = _group_pool(
            x,
            by_cp.get(x.counterparty_canonical, ()),
            cfg,
            back=cfg.aggregate_window_days + 3,
            forward=1,
        )
        if len(pool) < 2:
            continue
        sols = _subset_sums(
            [(n.txn_id, n.amount_abs) for n in pool],
            x.amount_abs,
            cfg.absolute_tolerance,
            cfg.max_group_size,
        )
        if len(sols) != 1:
            continue  # zero solutions, or an ambiguous coincidence
        group = sols[0]
        members = [ctx.ledger[i] for i in group]
        span = max(abs((n.booking_date - x.booking_date).days) for n in members)
        out.append(
            Candidate(
                tuple(group),
                (x.txn_id,),
                "batch_aggregate",
                0.86,
                round(0.86 - 0.02 * len(group), 6),
                (
                    f"{len(group)} booked items for '{x.txn.counterparty}' sum exactly "
                    f"to the payout of {_fmt(x.amount_abs, x.currency)}",
                    "Unique subset — no other combination in the window reaches this total",
                    f"All booked within {span}d of the payout",
                ),
            )
        )
    return out


def split_aggregate(ctx: MatchContext) -> list[Candidate]:
    """One booked item settling as several instalments."""
    cfg = ctx.cfg
    by_cp: dict[str, list[NormalizedTxn]] = defaultdict(list)
    for x in ctx.open_external_txns():
        by_cp[x.counterparty_canonical].append(x)

    out: list[Candidate] = []
    for led in ctx.open_ledger_txns():
        pool = _group_pool(
            led,
            by_cp.get(led.counterparty_canonical, ()),
            cfg,
            back=1,
            forward=cfg.settlement_window_days + 1,
        )
        if len(pool) < 2:
            continue
        sols = _subset_sums(
            [(n.txn_id, n.amount_abs) for n in pool],
            led.amount_abs,
            cfg.absolute_tolerance,
            cfg.max_group_size,
        )
        if len(sols) != 1:
            continue
        group = sols[0]
        members = [ctx.external[i] for i in group]
        span = max(abs((n.booking_date - led.booking_date).days) for n in members)
        out.append(
            Candidate(
                (led.txn_id,),
                tuple(group),
                "split_aggregate",
                0.84,
                round(0.84 - 0.02 * len(group), 6),
                (
                    f"{len(group)} instalments for '{led.txn.counterparty}' sum exactly to "
                    f"the booked {_fmt(led.amount_abs, led.currency)}",
                    "Unique subset — no other combination in the window reaches this total",
                    f"All settled within {span}d of booking",
                ),
            )
        )
    return out


# --------------------------------------------------------------------------
# Resolution — the precision policy
# --------------------------------------------------------------------------


def resolve(
    candidates: list[Candidate], ctx: MatchContext
) -> tuple[list[Match], set[str]]:
    """Accept only unambiguous winners; everything else is escalated.

    A wrong auto-match is strictly worse than no auto-match: the item leaves
    the exception queue with a green tick and the underlying break is never
    investigated. So a candidate is accepted only when, for every transaction
    it touches, it beats the runner-up by `ambiguity_margin`.
    """
    margin = ctx.cfg.ambiguity_margin
    by_member: dict[str, list[Candidate]] = defaultdict(list)
    for c in candidates:
        for m in c.members:
            by_member[m].append(c)

    accepted: list[Match] = []
    contested: set[str] = set()
    for cand in sorted(candidates, key=lambda c: (-c.score, c.members)):
        if not ctx.live(cand):
            continue
        rivals_beat_margin = False
        for member in cand.members:
            for other in by_member[member]:
                if other is cand or not ctx.live(other):
                    continue
                if other.score > cand.score - margin:
                    rivals_beat_margin = True
                    break
            if rivals_beat_margin:
                break
        if rivals_beat_margin:
            contested.update(cand.members)
            continue
        ctx.consume(cand)
        accepted.append(
            Match(
                ledger_ids=cand.ledger_ids,
                external_ids=cand.external_ids,
                strategy=cand.strategy,
                confidence=cand.confidence,
                evidence=cand.evidence,
                residual_base=cand.residual_base,
            )
        )
    # An item that found a home later in this pass is no longer contested.
    settled: set[str] = set()
    for m in accepted:
        settled |= set(m.ledger_ids) | set(m.external_ids)
    return accepted, contested - settled


STRATEGIES = [
    ("exact_reference", exact_reference),
    ("canonical_reference", canonical_reference),
    ("amount_and_identity", amount_bucket_fuzzy),
    ("fx_translated", fx_translated),
    ("fee_adjusted", fee_adjusted),
    ("batch_aggregate", batch_aggregate),
    ("split_aggregate", split_aggregate),
]
