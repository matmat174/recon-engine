"""CSV in/out, so the engine can be pointed at real extracts.

Column contract (header row required):
    txn_id, booking_date, amount, currency, counterparty, reference, method

`method` is optional and defaults to "sepa". Amounts are parsed as `Decimal`
from the raw string — never through `float` — and both `1 234,56` and
`1,234.56` are accepted, because bank exports use both.
"""

from __future__ import annotations

import csv
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .models import EXTERNAL, LEDGER, Dataset, Transaction, TruthGroup

REQUIRED = ["txn_id", "booking_date", "amount", "currency", "counterparty", "reference"]

_DATE_FORMATS = ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%d.%m.%Y", "%Y/%m/%d")


def parse_date(value: str) -> date:
    value = value.strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"unrecognised date: {value!r}")


def parse_amount(value: str) -> Decimal:
    raw = (value or "").strip().replace(" ", "").replace(" ", "")
    if not raw:
        raise ValueError("empty amount")
    negative = raw.startswith("(") and raw.endswith(")")
    if negative:
        raw = raw[1:-1]
    raw = re.sub(r"[^\d,.\-+]", "", raw)
    if "," in raw and "." in raw:
        # Whichever separator comes last is the decimal one.
        if raw.rfind(",") > raw.rfind("."):
            raw = raw.replace(".", "").replace(",", ".")
        else:
            raw = raw.replace(",", "")
    elif "," in raw:
        # A single comma is decimal unless it looks like a thousands group.
        raw = raw.replace(",", "") if re.search(r",\d{3}$", raw) else raw.replace(",", ".")
    try:
        amount = Decimal(raw)
    except InvalidOperation as exc:  # pragma: no cover - defensive
        raise ValueError(f"unrecognised amount: {value!r}") from exc
    return -amount if negative else amount


def read_side(path: str | Path, side: str) -> list[Transaction]:
    rows: list[Transaction] = []
    with open(path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        missing = [c for c in REQUIRED if c not in (reader.fieldnames or [])]
        if missing:
            raise ValueError(f"{path}: missing column(s) {', '.join(missing)}")
        for i, row in enumerate(reader, start=2):
            try:
                rows.append(
                    Transaction(
                        txn_id=row["txn_id"].strip(),
                        side=side,
                        booking_date=parse_date(row["booking_date"]),
                        amount=parse_amount(row["amount"]),
                        currency=row["currency"].strip().upper(),
                        counterparty=row["counterparty"].strip(),
                        reference=(row["reference"] or "").strip(),
                        method=(row.get("method") or "sepa").strip() or "sepa",
                        account=(row.get("account") or "MAIN").strip() or "MAIN",
                    )
                )
            except ValueError as exc:
                raise ValueError(f"{path} line {i}: {exc}") from exc
    return rows


def load_dataset(
    ledger_path: str | Path,
    external_path: str | Path,
    fx_rates: dict[str, Decimal] | None = None,
    base_currency: str = "EUR",
    as_of: date | None = None,
) -> Dataset:
    ledger = read_side(ledger_path, LEDGER)
    external = read_side(external_path, EXTERNAL)
    latest = max(
        [t.booking_date for t in ledger + external] or [date.today()]
    )
    return Dataset(
        ledger=ledger,
        external=external,
        truth=[],
        fx_rates=dict(fx_rates or {}),
        base_currency=base_currency,
        as_of=as_of or latest,
    )


def _write(path: Path, rows: list[Transaction]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(REQUIRED + ["method", "account"])
        for t in rows:
            writer.writerow(
                [
                    t.txn_id,
                    t.booking_date.isoformat(),
                    f"{t.amount:.2f}",
                    t.currency,
                    t.counterparty,
                    t.reference,
                    t.method,
                    t.account,
                ]
            )


def write_dataset(dataset: Dataset, directory: str | Path) -> dict[str, Path]:
    out = Path(directory)
    out.mkdir(parents=True, exist_ok=True)
    paths = {
        "ledger": out / "ledger.csv",
        "external": out / "external.csv",
        "truth": out / "truth.csv",
    }
    _write(paths["ledger"], dataset.ledger)
    _write(paths["external"], dataset.external)
    with open(paths["truth"], "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["scenario", "ledger_ids", "external_ids"])
        for g in dataset.truth:
            writer.writerow([g.scenario, "|".join(g.ledger_ids), "|".join(g.external_ids)])
    return paths


def read_truth(path: str | Path) -> list[TruthGroup]:
    groups: list[TruthGroup] = []
    with open(path, newline="", encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            groups.append(
                TruthGroup(
                    ledger_ids=tuple(x for x in row["ledger_ids"].split("|") if x),
                    external_ids=tuple(x for x in row["external_ids"].split("|") if x),
                    scenario=row["scenario"],
                )
            )
    return groups
