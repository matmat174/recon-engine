"""Cascade orchestration."""

from __future__ import annotations

import time
from dataclasses import replace
from datetime import date

from .classify import classify_residual, scan_duplicates
from .config import DEFAULT_CONFIG, MatchConfig
from .matchers import STRATEGIES, make_context, resolve
from .models import Dataset, ReconResult
from .normalize import normalize_all


def _config_for(dataset: Dataset, cfg: MatchConfig | None) -> MatchConfig:
    """Default policy, but always wired to the dataset's own FX table."""
    if cfg is not None:
        return cfg
    return replace(
        DEFAULT_CONFIG,
        fx_rates=dict(dataset.fx_rates),
        base_currency=dataset.base_currency,
    )


def reconcile(dataset: Dataset, cfg: MatchConfig | None = None) -> ReconResult:
    cfg = _config_for(dataset, cfg)
    as_of = dataset.as_of or date.today()

    ctx = make_context(
        cfg, normalize_all(dataset.ledger), normalize_all(dataset.external)
    )

    total_ledger = len(ctx.open_ledger)
    total_external = len(ctx.open_external)

    pass_stats: list[dict] = []

    t0 = time.perf_counter()
    dup_breaks = scan_duplicates(ctx)
    pass_stats.append(
        {
            "name": "duplicate_scan",
            "kind": "control",
            "candidates": len(dup_breaks),
            "matches": 0,
            "ledger_cleared": 0,
            "external_cleared": len(dup_breaks),
            "contested": 0,
            "ms": round((time.perf_counter() - t0) * 1000, 1),
        }
    )

    matches = []
    contested: set[str] = set()
    for name, fn in STRATEGIES:
        t0 = time.perf_counter()
        before_l, before_x = len(ctx.open_ledger), len(ctx.open_external)
        candidates = fn(ctx)
        accepted, pass_contested = resolve(candidates, ctx)
        matches.extend(accepted)
        contested |= pass_contested
        pass_stats.append(
            {
                "name": name,
                "kind": "match",
                "candidates": len(candidates),
                "matches": len(accepted),
                "ledger_cleared": before_l - len(ctx.open_ledger),
                "external_cleared": before_x - len(ctx.open_external),
                "contested": len(pass_contested),
                "ms": round((time.perf_counter() - t0) * 1000, 1),
            }
        )

    # Anything that later found a match is no longer contested.
    settled: set[str] = set()
    for m in matches:
        settled |= set(m.ledger_ids) | set(m.external_ids)
    contested -= settled

    breaks, review = classify_residual(ctx, contested, as_of)
    breaks.extend(dup_breaks)

    pass_stats.append(
        {
            "name": "residual",
            "kind": "control",
            "candidates": 0,
            "matches": 0,
            "ledger_cleared": 0,
            "external_cleared": 0,
            "contested": len(review),
            "ms": 0.0,
            "note": (
                f"{total_ledger - len(ctx.open_ledger)}/{total_ledger} ledger and "
                f"{total_external - len(ctx.open_external)}/{total_external} statement "
                f"lines cleared"
            ),
        }
    )

    return ReconResult(
        matches=matches,
        breaks=breaks,
        review_queue=review,
        pass_stats=pass_stats,
        dataset=dataset,
    )
