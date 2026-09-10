from datetime import date
from decimal import Decimal

import pytest

from recon.config import DEFAULT_CONFIG
from recon.matchers import ref_conflict
from recon.models import EXTERNAL, LEDGER, Transaction
from recon.normalize import (
    canonical_counterparty,
    canonical_reference,
    counterparty_score,
    normalize,
    reference_digits,
)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("INV-123456", "123456"),
        ("inv 123456", "123456"),
        ("REF-0000123456", "123456"),
        ("PMT/INV-123456", "123456"),
        ("  INV-123456 /RFB/", "123456RFB"),
        ("123456", "123456"),
        ("", ""),
        ("0000", "0"),
    ],
)
def test_canonical_reference_collapses_bank_noise(raw, expected):
    assert canonical_reference(raw) == expected


def test_canonical_reference_keeps_distinct_references_distinct():
    """The whole engine leans on this: canonicalisation must not merge two
    genuinely different payments."""
    assert canonical_reference("INV-100001") != canonical_reference("INV-100002")


def test_reference_digits_survives_prefix_changes():
    assert reference_digits("INV-000442") == reference_digits("PMT/442")


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Aldebaran Logistics SAS", "ALDEBARAN LOGISTICS"),
        ("ALDEBARAN LOGISTICS S.A.S.", "ALDEBARAN LOGISTICS"),
        ("Orion Digital GmbH", "ORION DIGITAL"),
    ],
)
def test_canonical_counterparty_drops_legal_form(raw, expected):
    assert canonical_counterparty(raw) == expected


def test_counterparty_score_is_order_insensitive():
    assert counterparty_score("ACME EUROPE", "EUROPE ACME") == 1.0


def _txn(ref: str, side: str = LEDGER) -> Transaction:
    return Transaction(
        txn_id="T1" if side == LEDGER else "T2",
        side=side,
        booking_date=date(2026, 9, 1),
        amount=Decimal("100.00"),
        currency="EUR",
        counterparty="Acme SAS",
        reference=ref,
    )


@pytest.mark.parametrize(
    "a,b,conflict",
    [
        ("INV-100001", "INV-100002", True),   # different payments
        ("INV-100001", "PMT/INV-100001", False),  # same, reprefixed
        ("INV-100001", "INV-1000", False),    # truncated by SWIFT
        ("INV-100001", "", False),            # statement dropped the reference
        ("", "", False),
    ],
)
def test_ref_conflict_vetoes_only_real_contradictions(a, b, conflict):
    left = normalize(_txn(a, LEDGER))
    right = normalize(_txn(b, EXTERNAL))
    assert ref_conflict(left, right, DEFAULT_CONFIG) is conflict
