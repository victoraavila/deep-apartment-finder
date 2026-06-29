"""Orchestrator — the agent the user invokes from the CLI.

Built with `create_deep_agent(...)`. Owns:
- the reasoning LLM (with the opencode-go fallback already wrapped in),
- three LLM-driven subagents (`researcher`, `fotocasa_scraper`,
  `idealista_scraper`),
- the deterministic `ranker` and `notifier` Python steps,
- a `CompositeBackend` that routes `/researcher/`, `/fotocasa_scraper/`,
  `/idealista_scraper/`, `/orchestrator/`, `/ranker/`, `/notifier/`
  to the persistent store and everything else to ephemeral state.

The orchestrator does not own a repository or a scraper directly. The
*subagent* owns the scraper. The orchestrator is a planner and a
summarizer, plus the entry point that runs the deterministic ranker
and notifier steps.

The flow:
1. `researcher` (LLM) — only if `dangerous_neighborhoods` is empty.
   On the first run the operator is asked to re-run after the
   researcher has populated the table.
2. `fotocasa_scraper` + `idealista_scraper` (LLM) — Sprint 1 + 3
   parallel ingestion, both extracting `pet_policy` and `furnished`
   at ingest. The orchestrator's prompt delegates to BOTH in a
   single run (Pillar E).
3. `compute_ranking` (Python) — deterministic scoring.
4. `send_notification` (Python) — render + send + dedup-per-day.

Sprint 3 (Pillar A) added a `RunObserver` injection point: every
phase transition the deterministic steps make is reported through
the observer, so the CLI's stderr shows progress in real time
and the recording observer persists the report at the end of the
run.
"""

from __future__ import annotations

import logging
import time
import uuid
from pathlib import Path
from typing import Any

from deepagents import create_deep_agent
from langchain_core.language_models import BaseChatModel

from deep_apartment_finder.adapters.observability.tracing import trace
from deep_apartment_finder.domain.filters.hard import HardFilters
from deep_apartment_finder.domain.notifier import send_notification
from deep_apartment_finder.domain.ranking import RankableApartment, compute_ranking
from deep_apartment_finder.filesystem.routes import build_backend
from deep_apartment_finder.ports.apartment_repository import ApartmentRepository
from deep_apartment_finder.ports.dangerous_neighborhood_repository import (
    DangerousNeighborhoodRepository,
)
from deep_apartment_finder.ports.notifier import Notifier
from deep_apartment_finder.ports.ranking_repository import RankingRepository
from deep_apartment_finder.ports.run_observer import RunObserver
from deep_apartment_finder.ports.scraper import ScraperPort
from deep_apartment_finder.subagents.fotocasa_scraper import build_fotocasa_scraper_subagent
from deep_apartment_finder.subagents.idealista_scraper import build_idealista_scraper_subagent
from deep_apartment_finder.subagents.researcher import build_researcher_subagent
from deep_apartment_finder.tools.researcher.count_neighborhoods import (
    make_count_dangerous_neighborhoods_tool,
)
from deep_apartment_finder.tools.researcher.web_search import SearchBackend

logger = logging.getLogger(__name__)

_PROMPTS_DIR = Path(__file__).parent.parent / "subagents" / "prompts"


def _load_orchestrator_prompt() -> str:
    return (_PROMPTS_DIR / "orchestrator.md").read_text(encoding="utf-8")


def build_orchestrator(
    *,
    llm: BaseChatModel,
    fotocasa_scraper: ScraperPort,
    idealista_scraper: ScraperPort | None = None,
    repo: ApartmentRepository,
    dangerous_repo: DangerousNeighborhoodRepository,
    ranking_repo: RankingRepository,
    notifier: Notifier | None,
    from_address: str | None = None,
    to_address: str | None = None,
    weight_distance: float = 0.5,
    weight_pet_policy: float = 0.3,
    weight_furnished: float = 0.2,
    max_distance_m: float = 2000.0,
    top_n: int = 5,
    researcher_search_backend: SearchBackend | None = None,
    observer: RunObserver | None = None,
) -> Any:
    """Build the compiled orchestrator graph.

    The deterministic ranker / notifier steps are exposed as plain
    async methods on the returned object via `run_deterministic_steps`
    (see `Orchestrator` below). The LLM part is the LangGraph
    `CompiledStateGraph` you can `.ainvoke(...)`.

    `idealista_scraper` is optional for backward compatibility: when
    `None`, only `fotocasa_scraper` is registered (Sprint 1/2
    behaviour). When provided, the orchestrator delegates to both
    subagents in a single run and their listings are merged in
    `ingest_apartment` (cross-portal dedup is Sprint 3 Pillar F).

    `observer` is the optional `RunObserver` the deterministic
    steps emit events through. The CLI passes a fan-out of
    `CliRunObserver` + `RecordingRunObserver` so the operator sees
    progress in stderr *and* the run report is persisted. When
    `None`, the deterministic steps are silent on the observer
    channel (the existing S1/S2 behaviour).
    """
    backend = build_backend()
    subagents: list[dict[str, Any]] = [
        build_fotocasa_scraper_subagent(
            scraper=fotocasa_scraper,
            repo=repo,
            backend=backend,
        ),
        build_researcher_subagent(
            repo=dangerous_repo,
            backend=backend,
            search_backend=researcher_search_backend,
        ),
    ]
    if idealista_scraper is not None:
        subagents.append(
            build_idealista_scraper_subagent(
                scraper=idealista_scraper,
                repo=repo,
                backend=backend,
            )
        )
    orchestrator_tools = [
        make_count_dangerous_neighborhoods_tool(dangerous_repo),
    ]
    graph = create_deep_agent(
        model=llm,
        tools=orchestrator_tools,
        system_prompt=_load_orchestrator_prompt(),
        subagents=subagents,  # type: ignore[arg-type]
        backend=backend,
        # No interrupt_on: this is an automated daily run, not a HITL
        # workflow. Sprint 5 may add approval for notifications.
    )
    deterministic = _DeterministicSteps(
        repo=repo,
        dangerous_repo=dangerous_repo,
        ranking_repo=ranking_repo,
        notifier=notifier,
        backend=backend,
        from_address=from_address,
        to_address=to_address,
        weight_distance=weight_distance,
        weight_pet_policy=weight_pet_policy,
        weight_furnished=weight_furnished,
        max_distance_m=max_distance_m,
        top_n=top_n,
        observer=observer,
    )
    return Orchestrator(graph=graph, deterministic=deterministic)


class _DeterministicSteps:
    """The Python side of the orchestrator: ranker + notifier.

    The orchestrator's LLM does the *planning*; the deterministic
    ranker and notifier are pure async functions. We expose them as
    methods on a small object so the CLI can call them directly.
    """

    def __init__(
        self,
        *,
        repo: ApartmentRepository,
        dangerous_repo: DangerousNeighborhoodRepository,
        ranking_repo: RankingRepository,
        notifier: Notifier | None,
        backend: Any,
        from_address: str | None,
        to_address: str | None,
        weight_distance: float,
        weight_pet_policy: float,
        weight_furnished: float,
        max_distance_m: float,
        top_n: int,
        observer: RunObserver | None = None,
    ) -> None:
        self._repo = repo
        self._dangerous_repo = dangerous_repo
        self._ranking_repo = ranking_repo
        self._notifier = notifier
        self._backend = backend
        self._from_address = from_address
        self._to_address = to_address
        self._weight_distance = weight_distance
        self._weight_pet_policy = weight_pet_policy
        self._weight_furnished = weight_furnished
        self._max_distance_m = max_distance_m
        self._top_n = top_n
        self._observer = observer

    @trace("deterministic.ranker", metadata={"phase": "ranker"})
    async def _phase_ranker(
        self,
        rankables: list[RankableApartment],
        neighborhoods: list,
    ) -> dict[str, Any]:
        if self._observer is not None:
            await self._observer.waiting("Postgres")
            await self._observer.phase_start("ranker")
        start = time.monotonic()
        ranking: dict[str, Any] | None = None
        try:
            ranking = await compute_ranking(
                rankables=rankables,
                neighborhoods=neighborhoods,
                ranking_repo=self._ranking_repo,
                weight_distance=self._weight_distance,
                weight_pet_policy=self._weight_pet_policy,
                weight_furnished=self._weight_furnished,
                max_distance_m=self._max_distance_m,
                top_n=self._top_n,
                ranking_run_id=uuid.uuid4(),
            )
        finally:
            if self._observer is not None:
                duration_ms = int((time.monotonic() - start) * 1000)
                counts: dict[str, int] = {}
                errors = 1
                if ranking is not None:
                    errors = 0
                    counts = {
                        "apartments_scored": ranking["apartments_scored"],
                        "scores_written": ranking["scores_written"],
                        "top_n_returned": len(ranking["top"]),
                        "dedup_dropped": ranking.get("dedup_dropped", 0),
                    }
                await self._observer.phase_end(
                    "ranker",
                    duration_ms=duration_ms,
                    counts=counts,
                    errors=errors,
                )
        assert ranking is not None
        return ranking

    @trace("deterministic.notifier", metadata={"phase": "notifier"})
    async def _phase_notifier(
        self,
        ranking: dict[str, Any],
        apartments_by_id: dict[int, RankableApartment],
    ) -> Any:
        if self._observer is not None:
            await self._observer.phase_start("notifier")
        start = time.monotonic()
        notification = None
        errors = 0
        try:
            if self._notifier is not None and self._from_address and self._to_address:
                if self._observer is not None:
                    await self._observer.waiting("SMTP")
                notification = await send_notification(
                    ranking=ranking,
                    apartments_by_id=apartments_by_id,
                    ranking_repo=self._ranking_repo,
                    notifier=self._notifier,
                    backend=self._backend,
                    from_address=self._from_address,
                    to_address=self._to_address,
                )
                if self._observer is not None:
                    if notification.sent:
                        await self._observer.decision(
                            "notifier", f"sent (ranking_run_id={ranking['ranking_run_id']})"
                        )
                    else:
                        await self._observer.decision(
                            "notifier", f"skipped: {notification.skipped_reason}"
                        )
            else:
                if self._observer is not None:
                    await self._observer.decision(
                        "notifier", "skipped: notifier not configured"
                    )
        except Exception:
            errors = 1
            raise
        finally:
            if self._observer is not None:
                duration_ms = int((time.monotonic() - start) * 1000)
                counts: dict[str, int] = {}
                if notification is not None:
                    counts["apartment_ids"] = len(notification.apartment_ids)
                await self._observer.phase_end(
                    "notifier", duration_ms=duration_ms, counts=counts, errors=errors
                )
        return notification

    async def run(self) -> dict[str, Any]:
        """Run the deterministic steps in order.

        Returns a dict with `ranking` and `notification` keys (either
        may be missing if the run was short-circuited). When an
        observer is wired in, the returned dict also carries the
        per-criterion score distribution so the run report can
        include it.
        """
        if self._observer is not None:
            await self._observer.phase_start("ranker_setup", phase="ranker_setup")

        # 1. Load the dangerous neighborhoods (used by the ranker).
        neighborhoods = await self._dangerous_repo.list_all()
        if not neighborhoods:
            logger.info(
                "deterministic: dangerous_neighborhoods is empty; "
                "the ranker will use a neutral 0.5 for the distance "
                "criterion"
            )
            if self._observer is not None:
                await self._observer.decision(
                    "ranker",
                    "dangerous_neighborhoods is empty; "
                    "distance criterion uses neutral 0.5",
                )

        # 2. Load all stored apartments (with their DB ids).
        rows = await self._repo.list_all(limit=5000)
        hard_filters = HardFilters()
        filtered_out = len(rows)
        rows = [(db_id, apt) for db_id, apt in rows if hard_filters.passes(apt)]
        filtered_out -= len(rows)
        if filtered_out:
            logger.info(
                "deterministic: skipped %d stored apartments that fail "
                "Sprint 1 hard filters",
                filtered_out,
            )
            if self._observer is not None:
                await self._observer.count("filtered_hard_filters", filtered_out)

        if self._observer is not None:
            await self._observer.count("rows_loaded", len(rows))
            await self._observer.phase_end(
                "ranker_setup",
                duration_ms=0,
                counts={"rows_loaded": len(rows), "filtered_hard_filters": filtered_out},
            )

        if not rows:
            return {
                "ranking": None,
                "notification": None,
                "apartments_scored": 0,
                "note": "no apartments passing hard filters to rank",
            }
        rankables = [RankableApartment(apartment=apt, db_id=db_id) for db_id, apt in rows]

        # 3. Run the ranker.
        ranking = await self._phase_ranker(rankables, neighborhoods)

        # 4. Run the notifier (no-op when notifier is not configured).
        notification = await self._phase_notifier(
            ranking, {r.db_id: r for r in rankables}
        )

        # 5. Persist a per-run report under /ranker/reports/ so the
        #    operator can read the breakdown without a SQL query.
        report_path = f"/ranker/reports/{ranking['ranking_run_id']}.json"
        try:
            import json as _json

            await self._backend.awrite(
                report_path,
                _json.dumps(
                    {
                        "ranking_run_id": str(ranking["ranking_run_id"]),
                        "apartments_scored": ranking["apartments_scored"],
                        "scores_written": ranking["scores_written"],
                        "top": ranking["top"],
                        "dedup_dropped": ranking.get("dedup_dropped", 0),
                        "weights": {
                            "distance": self._weight_distance,
                            "pet_policy": self._weight_pet_policy,
                            "furnished": self._weight_furnished,
                        },
                        "max_distance_m": self._max_distance_m,
                        "notification": (
                            {
                                "sent": notification.sent,
                                "skipped_reason": notification.skipped_reason,
                                "subject": notification.subject,
                            }
                            if notification
                            else None
                        ),
                    },
                    indent=2,
                    default=str,
                ),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("orchestrator: ranker report write failed: %s", exc)

        return {
            "ranking": ranking,
            "notification": notification,
            "apartments_scored": ranking["apartments_scored"],
        }


class Orchestrator:
    """The composite orchestrator: LLM graph + deterministic steps.

    The CLI drives both: it calls `graph.ainvoke(...)` for the LLM
    part (planning + delegating to subagents), then
    `deterministic.run()` for the ranker + notifier.
    """

    def __init__(self, *, graph: Any, deterministic: _DeterministicSteps) -> None:
        self._graph = graph
        self._deterministic = deterministic

    @property
    def graph(self) -> Any:
        return self._graph

    @property
    def deterministic(self) -> _DeterministicSteps:
        return self._deterministic

    async def ainvoke(self, *args: Any, **kwargs: Any) -> Any:
        return await self._graph.ainvoke(*args, **kwargs)


__all__ = ["Orchestrator", "build_orchestrator"]
