"""Pure-Python ranker (no LLM).

Per `docs/SPRINT2.md`, the ranker is **deterministic Python** — no
LLM at rank time. The subagent's role is documented in
`subagents/prompts/ranker.md` for the agent's vocabulary, but the
real work is in this function.

The orchestrator calls `compute_ranking(...)` directly. The
`compute_scores` tool exists so a subagent *could* drive the same
function (e.g. in a future run where the orchestrator wants the
LLM to choose a custom weight), but the hot path is the direct
call.

Sprint 3: top-N dedup by `dedup_key`. When two apartments from
different portals share a `dedup_key` (Pillar F), only the
higher-scoring one survives in the top-N; the lower-scoring
sibling is dropped. Sprint 1/2 rows that have `dedup_key=NULL`
skip the dedup pass and are not dropped.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from deep_apartment_finder.domain.apartment import Apartment
from deep_apartment_finder.domain.geo import DangerousNeighborhood
from deep_apartment_finder.domain.soft_criteria import SoftCriterion
from deep_apartment_finder.domain.soft_criteria.registry import default_criteria
from deep_apartment_finder.ports.ranking_repository import (
    RankingRepository,
    ScoreRow,
)


@dataclass(frozen=True, slots=True)
class RankableApartment:
    """An `Apartment` paired with its DB id, ready for ranking.

    The `Apartment` value object is intentionally not aware of the
    database (Sprint 1). The ranker needs the DB id to write
    `apartment_scores` rows; we attach it here, in the ranker's own
    type, instead of mutating the frozen dataclass.
    """

    apartment: Apartment
    db_id: int


def _dedup_key_of(apartment: Apartment) -> str | None:
    """Pull the `dedup_key` from the raw blob. Returns `None` when
    the apartment was ingested before Sprint 3 (or has no key)."""
    raw = apartment.raw
    if not isinstance(raw, dict):
        return None
    key = raw.get("dedup_key")
    return str(key) if key else None


def _dedup_top_n_by_key(
    sorted_top: list[dict[str, Any]],
    rankables_by_id: dict[int, RankableApartment],
) -> tuple[list[dict[str, Any]], int]:
    """Drop the lower-scoring sibling from any pair that shares a
    `dedup_key`. Returns the trimmed top-N and the count of
    dropped siblings.

    `sorted_top` is expected to be sorted by `score` DESC. We walk
    it once; the first apartment we see for a given `dedup_key`
    is the highest-scoring one, so it stays. Subsequent
    apartments with the same key are dropped.
    """
    kept: list[dict[str, Any]] = []
    seen_keys: set[str] = set()
    dropped = 0
    for row in sorted_top:
        apt_id = int(row["apartment_id"])
        rankable = rankables_by_id.get(apt_id)
        if rankable is None:
            # Defensive: rankable was deleted between scoring and
            # dedup. Keep the row as-is.
            kept.append(row)
            continue
        key = _dedup_key_of(rankable.apartment)
        if key is None:
            kept.append(row)
            continue
        if key in seen_keys:
            dropped += 1
            continue
        seen_keys.add(key)
        kept.append(row)
    return kept, dropped


async def compute_ranking(
    *,
    rankables: list[RankableApartment],
    neighborhoods: list[DangerousNeighborhood],
    ranking_repo: RankingRepository,
    weight_distance: float,
    weight_pet_policy: float,
    weight_furnished: float,
    max_distance_m: float,
    top_n: int,
    ranking_run_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    """Score every apartment, write trace rows, return top-N.

    Returns a dict with:
        - `ranking_run_id`: UUID4 (or the one passed in, for tests)
        - `scores`: list of `{apartment_id, score, breakdown: [...]}`
        - `top`: list of `{apartment_id, score}` (length <= top_n)
        - `dedup_dropped`: int count of top-N siblings dropped
          because they shared a `dedup_key` with a higher-scoring
          sibling (Pillar F).
    """
    criteria: list[SoftCriterion] = default_criteria(
        neighborhoods=neighborhoods,
        weight_distance=weight_distance,
        weight_pet_policy=weight_pet_policy,
        weight_furnished=weight_furnished,
        max_distance_m=max_distance_m,
    )

    ranking_run_id = ranking_run_id or uuid.uuid4()
    trace_rows: list[ScoreRow] = []
    per_apartment: list[dict[str, Any]] = []
    rankables_by_id: dict[int, RankableApartment] = {r.db_id: r for r in rankables}

    for r in rankables:
        breakdown: list[dict[str, Any]] = []
        weighted_sum = 0.0
        weight_sum = 0.0
        for crit in criteria:
            score = crit.score(r.apartment)
            weighted_sum += score.score * score.weight
            weight_sum += score.weight
            breakdown.append(
                {
                    "criterion": crit.name,
                    "score": score.score,
                    "weight": score.weight,
                    "details": score.details,
                }
            )
            trace_rows.append(
                ScoreRow(
                    apartment_id=r.db_id,
                    criterion=crit.name,
                    score=score.score,
                    weight=score.weight,
                    details=score.details,
                )
            )
        final = (weighted_sum / weight_sum) if weight_sum else 0.0
        per_apartment.append(
            {
                "apartment_id": r.db_id,
                "score": round(final, 4),
                "breakdown": breakdown,
            }
        )

    written = await ranking_repo.write_scores(ranking_run_id, trace_rows)

    per_apartment.sort(key=lambda r2: r2["score"], reverse=True)
    raw_top = per_apartment[:top_n]
    top, dedup_dropped = _dedup_top_n_by_key(raw_top, rankables_by_id)

    return {
        "ranking_run_id": ranking_run_id,
        "apartments_scored": len(per_apartment),
        "scores_written": written,
        "scores": per_apartment,
        "top": top,
        "dedup_dropped": dedup_dropped,
    }


__all__ = ["RankableApartment", "compute_ranking"]
