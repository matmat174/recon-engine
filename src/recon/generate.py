"""Synthetic data generator with ground truth.

Why generate instead of shipping a CSV: because the generator knows the right
answer. Every settlement event it emits is recorded as a `TruthGroup`, so the
engine can be *graded* — precision, recall, and a per-scenario breakdown —
instead of being eyeballed. That is the difference between "the report looks
plausible" and "the engine auto-matches 96% of volume at 99.9% precision".

The break scenarios below are the ones that actually generate work in a
payments ops team: late settlement, fees netted at source, FX translation,
batched payouts, mangled references, and — the expensive one — clusters of
identical amounts where no field can tell two payments apart.
"""

from __future__ import annotations

import random
from collections.abc import Callable
from datetime import date, timedelta
from decimal import Decimal

from .models import EXTERNAL, LEDGER, Dataset, Transaction, TruthGroup

# ECB euro foreign exchange reference rates, 2026-09-08 (units per 1 EUR).
# Source: https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml
FX_RATES: dict[str, Decimal] = {
    "EUR": Decimal("1"),
    "USD": Decimal("1.1614"),
    "GBP": Decimal("0.85740"),
    "CHF": Decimal("0.9425"),
    "SEK": Decimal("11.1520"),
    "NOK": Decimal("10.7450"),
    "DKK": Decimal("7.4748"),
    "PLN": Decimal("4.3178"),
}

COUNTERPARTIES = [
    "Aldebaran Logistics SAS", "Northwind Trading Ltd", "Helvetia Components AG",
    "Meridian Foods BV", "Caravelle Retail SA", "Baltic Steel OY",
    "Orion Digital GmbH", "Pont-Neuf Media SARL", "Quaystone Partners LLP",
    "Ravenna Textiles SpA", "Solstice Energy AB", "Trident Marine NV",
    "Umbria Pharma Srl", "Verdant Agro SAS", "Westbrook Analytics Inc",
    "Zephyr Mobility GmbH", "Atlas Freight Ltd", "Bellevue Interiors SA",
    "Cormorant Shipping AS", "Delacroix Luxury SAS",
]

METHODS = ["sepa", "swift", "card", "wallet"]

# Scenario -> relative weight. Roughly calibrated on what a mid-size PSP
# integration actually throws at an ops team.
DEFAULT_MIX: dict[str, float] = {
    "clean": 46.0,
    "timing": 11.0,
    "reference_corrupted": 8.0,
    "fee_deducted": 7.0,
    "fx_translated": 6.0,
    "batch_settlement": 5.0,
    "split_settlement": 3.0,
    "not_settled": 4.0,
    "in_transit": 3.0,
    "unexpected_credit": 2.0,
    "duplicate": 2.0,
    "amount_mismatch": 2.0,
    "ambiguous_cluster": 1.0,
}


class _IdFactory:
    def __init__(self) -> None:
        self._n = {LEDGER: 0, EXTERNAL: 0}

    def next(self, side: str) -> str:
        self._n[side] += 1
        prefix = "L" if side == LEDGER else "X"
        return f"{prefix}{self._n[side]:06d}"


class Generator:
    def __init__(
        self,
        seed: int = 7,
        as_of: date | None = None,
        mix: dict[str, float] | None = None,
    ) -> None:
        self.rng = random.Random(seed)
        self.as_of = as_of or date(2026, 9, 8)
        self.mix = dict(mix or DEFAULT_MIX)
        self.ids = _IdFactory()
        self.ledger: list[Transaction] = []
        self.external: list[Transaction] = []
        self.truth: list[TruthGroup] = []

    # -- primitives -------------------------------------------------------

    def _amount(self) -> Decimal:
        """Log-normal-ish: lots of small payments, a long tail of big ones."""
        value = self.rng.lognormvariate(6.6, 1.15)
        value = min(value, 480_000.0)
        return Decimal(f"{value:.2f}")

    def _booking_date(self, max_age: int = 75, min_age: int = 4) -> date:
        return self.as_of - timedelta(days=self.rng.randint(min_age, max_age))

    def _reference(self) -> str:
        style = self.rng.random()
        n = self.rng.randint(100000, 999999)
        if style < 0.35:
            return f"INV-{n}"
        if style < 0.6:
            return f"REF{n}"
        if style < 0.8:
            return f"{self.rng.choice('ABCDEFGH')}{n}-{self.rng.randint(10, 99)}"
        return str(n)

    def _corrupt_reference(self, ref: str) -> str:
        """Apply one of the mangling patterns banks actually apply."""
        mode = self.rng.randrange(6)
        if mode == 0:  # SWIFT field truncation
            return ref[: max(4, len(ref) - self.rng.randint(2, 4))]
        if mode == 1:  # extra prefix bolted on by the corporate
            return f"PMT/{ref}"
        if mode == 2:  # separators lost, case flipped
            return ref.replace("-", "").replace("/", "").lower()
        if mode == 3:  # padded with spaces and a trailing tag
            return f" {ref} /RFB/"
        if mode == 4:  # zero padding on the numeric tail
            digits = "".join(c for c in ref if c.isdigit())
            return f"REF-{digits.zfill(10)}"
        return f"{ref}{self.rng.choice(' _.')}{self.rng.randint(1, 9)}"

    def _new_pair_fields(self) -> dict:
        return {
            "counterparty": self.rng.choice(COUNTERPARTIES),
            "reference": self._reference(),
            "method": self.rng.choice(METHODS),
            "currency": "EUR",
        }

    def _add(self, side: str, **kw) -> Transaction:
        txn = Transaction(txn_id=self.ids.next(side), side=side, **kw)
        (self.ledger if side == LEDGER else self.external).append(txn)
        return txn

    # -- scenarios --------------------------------------------------------

    def s_clean(self) -> None:
        f = self._new_pair_fields()
        amt, d = self._amount(), self._booking_date()
        led = self._add(LEDGER, booking_date=d, amount=amt, **f)
        x = self._add(EXTERNAL, booking_date=d, amount=amt, **f)
        self.truth.append(TruthGroup((led.txn_id,), (x.txn_id,), "clean"))

    def s_timing(self) -> None:
        f = self._new_pair_fields()
        amt, d = self._amount(), self._booking_date()
        led = self._add(LEDGER, booking_date=d, amount=amt, **f)
        x = self._add(
            EXTERNAL, booking_date=d + timedelta(days=self.rng.randint(1, 4)),
            amount=amt, **f,
        )
        self.truth.append(TruthGroup((led.txn_id,), (x.txn_id,), "timing"))

    def s_reference_corrupted(self) -> None:
        f = self._new_pair_fields()
        amt, d = self._amount(), self._booking_date()
        led = self._add(LEDGER, booking_date=d, amount=amt, **f)
        fx = dict(f, reference=self._corrupt_reference(f["reference"]))
        x = self._add(
            EXTERNAL, booking_date=d + timedelta(days=self.rng.randint(0, 2)),
            amount=amt, **fx,
        )
        self.truth.append(TruthGroup((led.txn_id,), (x.txn_id,), "reference_corrupted"))

    def s_fee_deducted(self) -> None:
        f = self._new_pair_fields()
        f["method"] = self.rng.choice(["card", "swift", "wallet"])
        amt, d = self._amount(), self._booking_date()
        if f["method"] == "swift" and amt > 2000:
            # Correspondent-bank charge: flat, and only ever levied on wires
            # large enough for the flat fee to be a plausible proportion.
            fee = Decimal(f"{self.rng.uniform(12, 30):.2f}")
        else:
            fee = (amt * Decimal(f"{self.rng.uniform(0.004, 0.029):.4f}")).quantize(
                Decimal("0.01")
            )
        led = self._add(LEDGER, booking_date=d, amount=amt, **f)
        x = self._add(
            EXTERNAL, booking_date=d + timedelta(days=self.rng.randint(0, 3)),
            amount=amt - fee, **f,
        )
        self.truth.append(TruthGroup((led.txn_id,), (x.txn_id,), "fee_deducted"))

    def s_fx_translated(self) -> None:
        f = self._new_pair_fields()
        ccy = self.rng.choice(["USD", "GBP", "CHF", "SEK", "NOK", "PLN"])
        amt, d = self._amount(), self._booking_date()
        rate = FX_RATES[ccy] * Decimal(f"{self.rng.uniform(0.9990, 1.0010):.6f}")
        converted = (amt * rate).quantize(Decimal("0.01"))
        led = self._add(LEDGER, booking_date=d, amount=amt, **f)
        fx = dict(f, currency=ccy)
        x = self._add(
            EXTERNAL, booking_date=d + timedelta(days=self.rng.randint(0, 2)),
            amount=converted, **fx,
        )
        self.truth.append(TruthGroup((led.txn_id,), (x.txn_id,), "fx_translated"))

    def s_batch_settlement(self) -> None:
        """N invoices paid out as a single net payout — the classic N:1."""
        cp = self.rng.choice(COUNTERPARTIES)
        d = self._booking_date()
        n = self.rng.randint(2, 4)
        method = self.rng.choice(["sepa", "wallet"])
        ledger_ids, total = [], Decimal("0")
        for _ in range(n):
            amt = self._amount()
            total += amt
            led = self._add(
                LEDGER, booking_date=d - timedelta(days=self.rng.randint(0, 2)),
                amount=amt, currency="EUR", counterparty=cp,
                reference=self._reference(), method=method,
            )
            ledger_ids.append(led.txn_id)
        x = self._add(
            EXTERNAL, booking_date=d + timedelta(days=self.rng.randint(0, 2)),
            amount=total, currency="EUR", counterparty=cp,
            reference=f"BATCH-{self.rng.randint(10000, 99999)}", method=method,
        )
        self.truth.append(TruthGroup(tuple(ledger_ids), (x.txn_id,), "batch_settlement"))

    def s_split_settlement(self) -> None:
        """One invoice settled in instalments — the 1:M."""
        f = self._new_pair_fields()
        amt, d = self._amount(), self._booking_date()
        led = self._add(LEDGER, booking_date=d, amount=amt, **f)
        n = self.rng.randint(2, 3)
        parts: list[Decimal] = []
        remaining = amt
        for i in range(n - 1):
            share = (amt * Decimal(f"{self.rng.uniform(0.25, 0.5):.4f}")).quantize(
                Decimal("0.01")
            )
            share = min(share, remaining - Decimal("0.01") * (n - i - 1))
            parts.append(share)
            remaining -= share
        parts.append(remaining)
        ext_ids = []
        for i, part in enumerate(parts):
            x = self._add(
                EXTERNAL, booking_date=d + timedelta(days=i + self.rng.randint(0, 1)),
                amount=part, **dict(f, reference=f"{f['reference']}/{i + 1}"),
            )
            ext_ids.append(x.txn_id)
        self.truth.append(TruthGroup((led.txn_id,), tuple(ext_ids), "split_settlement"))

    def s_not_settled(self) -> None:
        """Booked, past SLA, never arrived. This is money at risk."""
        f = self._new_pair_fields()
        led = self._add(
            LEDGER, booking_date=self._booking_date(max_age=70, min_age=12),
            amount=self._amount(), **f,
        )
        self.truth.append(TruthGroup((led.txn_id,), (), "not_settled"))

    def s_in_transit(self) -> None:
        """Booked yesterday, not settled yet. Expected — must NOT be a break."""
        f = self._new_pair_fields()
        led = self._add(
            LEDGER, booking_date=self.as_of - timedelta(days=self.rng.randint(0, 2)),
            amount=self._amount(), **f,
        )
        self.truth.append(TruthGroup((led.txn_id,), (), "in_transit"))

    def s_unexpected_credit(self) -> None:
        f = self._new_pair_fields()
        x = self._add(
            EXTERNAL, booking_date=self._booking_date(max_age=40),
            amount=self._amount(), **f,
        )
        self.truth.append(TruthGroup((), (x.txn_id,), "unexpected_credit"))

    def s_duplicate(self) -> None:
        """PSP posted the same settlement twice. One matches, one is a break."""
        f = self._new_pair_fields()
        amt, d = self._amount(), self._booking_date()
        led = self._add(LEDGER, booking_date=d, amount=amt, **f)
        x1 = self._add(EXTERNAL, booking_date=d, amount=amt, **f)
        x2 = self._add(
            EXTERNAL, booking_date=d + timedelta(days=self.rng.randint(0, 2)),
            amount=amt, **f,
        )
        self.truth.append(TruthGroup((led.txn_id,), (x1.txn_id,), "duplicate"))
        self.truth.append(TruthGroup((), (x2.txn_id,), "duplicate_extra"))

    def s_amount_mismatch(self) -> None:
        """Materially different amount — a real discrepancy, not a fee."""
        f = self._new_pair_fields()
        amt, d = self._amount(), self._booking_date()
        delta = (amt * Decimal(f"{self.rng.uniform(0.07, 0.4):.4f}")).quantize(
            Decimal("0.01")
        )
        led = self._add(LEDGER, booking_date=d, amount=amt, **f)
        x = self._add(
            EXTERNAL, booking_date=d + timedelta(days=self.rng.randint(0, 2)),
            amount=amt - delta, **f,
        )
        self.truth.append(TruthGroup((led.txn_id,), (x.txn_id,), "amount_mismatch"))

    def s_ambiguous_cluster(self) -> None:
        """Same amount, same day, near-identical counterparty names, and the
        references are gone on the statement side. Nothing distinguishes the
        items — an engine that auto-matches here is guessing, and a wrong
        guess hides a real break behind a green tick."""
        base = self.rng.choice(COUNTERPARTIES).rsplit(" ", 1)[0]
        variants = [f"{base} SAS", f"{base} SA", f"{base} Europe SAS"]
        amt, d = self._amount(), self._booking_date()
        method = "sepa"
        for name in variants:
            led = self._add(
                LEDGER, booking_date=d, amount=amt, currency="EUR",
                counterparty=name, reference=self._reference(), method=method,
            )
            x = self._add(
                EXTERNAL, booking_date=d, amount=amt, currency="EUR",
                counterparty=name, reference="", method=method,
            )
            self.truth.append(
                TruthGroup((led.txn_id,), (x.txn_id,), "ambiguous_cluster")
            )

    # -- driver -----------------------------------------------------------

    def _scenarios(self) -> dict[str, Callable[[], None]]:
        return {
            "clean": self.s_clean,
            "timing": self.s_timing,
            "reference_corrupted": self.s_reference_corrupted,
            "fee_deducted": self.s_fee_deducted,
            "fx_translated": self.s_fx_translated,
            "batch_settlement": self.s_batch_settlement,
            "split_settlement": self.s_split_settlement,
            "not_settled": self.s_not_settled,
            "in_transit": self.s_in_transit,
            "unexpected_credit": self.s_unexpected_credit,
            "duplicate": self.s_duplicate,
            "amount_mismatch": self.s_amount_mismatch,
            "ambiguous_cluster": self.s_ambiguous_cluster,
        }

    def build(self, events: int = 1200) -> Dataset:
        scenarios = self._scenarios()
        names = list(self.mix)
        weights = [self.mix[n] for n in names]
        for _ in range(events):
            scenarios[self.rng.choices(names, weights=weights, k=1)[0]]()
        # Statements never arrive sorted the way your ledger is.
        self.rng.shuffle(self.ledger)
        self.rng.shuffle(self.external)
        return Dataset(
            ledger=self.ledger,
            external=self.external,
            truth=self.truth,
            fx_rates=dict(FX_RATES),
            base_currency="EUR",
            as_of=self.as_of,
        )


def build_dataset(
    events: int = 1200, seed: int = 7, as_of: date | None = None
) -> Dataset:
    return Generator(seed=seed, as_of=as_of).build(events=events)
