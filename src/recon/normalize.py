"""Normalisation and similarity.

Real payment references arrive mangled in predictable ways: SWIFT truncates
them, corporates prepend their own prefixes, humans retype them with spaces
and O/0 confusion. Canonicalising *before* indexing turns a large slice of
what would otherwise be fuzzy work into exact-key lookups, which is both
faster and far safer than fuzzy matching.
"""

from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher
from functools import lru_cache

from .models import NormalizedTxn, Transaction

# Prefixes/suffixes banks and corporates routinely bolt onto a reference.
_NOISE_TOKENS = (
    "REF",
    "REFERENCE",
    "INV",
    "INVOICE",
    "PMT",
    "PAYMENT",
    "PAY",
    "TRF",
    "TRANSFER",
    "SEPA",
    "SWIFT",
    "RTGS",
    "ORDER",
    "ORD",
    "FACT",
    "FACTURE",
    "NO",
    "NUM",
    "ID",
)

_NOISE_RE = re.compile(rf"^(?:{'|'.join(_NOISE_TOKENS)})[\s\-_/.]*")
_NON_ALNUM = re.compile(r"[^A-Z0-9]")
_DIGITS = re.compile(r"\D")

_COMPANY_SUFFIXES = {
    "SA", "SAS", "SARL", "SASU", "LTD", "LIMITED", "LLC", "INC", "INCORPORATED",
    "PLC", "GMBH", "AG", "BV", "NV", "AB", "AS", "OY", "SPA", "SRL", "CO",
    "COMPANY", "CORP", "CORPORATION", "HOLDING", "HOLDINGS", "GROUP", "THE",
}


def strip_accents(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


@lru_cache(maxsize=8192)
def canonical_reference(raw: str) -> str:
    """Aggressively canonical form of a payment reference.

    Uppercase, accent-free, alphanumeric only, known noise prefixes removed,
    leading zeros on the numeric tail dropped.
    """
    if not raw:
        return ""
    value = strip_accents(raw).upper().strip()
    # Strip noise prefixes repeatedly: "REF INV-0012" -> "0012"
    for _ in range(3):
        stripped = _NOISE_RE.sub("", value)
        if stripped == value:
            break
        value = stripped.strip()
    value = _NON_ALNUM.sub("", value)
    if not value:
        return ""
    # Drop leading zeros but never return an empty string for "0000".
    return value.lstrip("0") or value[-1:]


@lru_cache(maxsize=8192)
def reference_digits(raw: str) -> str:
    """The numeric spine of a reference — survives most corruption."""
    digits = _DIGITS.sub("", raw or "")
    return digits.lstrip("0")


@lru_cache(maxsize=8192)
def canonical_counterparty(raw: str) -> str:
    """Company name reduced to its distinctive tokens."""
    if not raw:
        return ""
    value = strip_accents(raw).upper()
    # Drop periods first so dotted legal forms ("S.A.S.", "Inc.") collapse to
    # the tokens the suffix list actually contains.
    value = value.replace(".", "")
    value = re.sub(r"[^A-Z0-9 ]", " ", value)
    tokens = [t for t in value.split() if t and t not in _COMPANY_SUFFIXES]
    return " ".join(tokens)


@lru_cache(maxsize=65536)
def similarity(a: str, b: str) -> float:
    """Ratio in [0, 1]. `difflib` keeps this dependency-free; it is a
    Ratcliff/Obershelp ratio, which handles the truncation and transposition
    cases we actually see better than plain edit distance."""
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    return SequenceMatcher(None, a, b).ratio()


def token_overlap(a: str, b: str) -> float:
    """Jaccard overlap on whitespace tokens — robust to word reordering
    ("ACME EUROPE" vs "EUROPE ACME")."""
    ta, tb = set(a.split()), set(b.split())
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def counterparty_score(a: str, b: str) -> float:
    """Best of sequence similarity and token overlap."""
    return max(similarity(a, b), token_overlap(a, b))


def normalize(txn: Transaction) -> NormalizedTxn:
    return NormalizedTxn(
        txn=txn,
        ref_canonical=canonical_reference(txn.reference),
        ref_digits=reference_digits(txn.reference),
        counterparty_canonical=canonical_counterparty(txn.counterparty),
        amount_abs=abs(txn.amount),
    )


def normalize_all(txns: list[Transaction]) -> list[NormalizedTxn]:
    return [normalize(t) for t in txns]
