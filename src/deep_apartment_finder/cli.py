"""CLI entrypoints.

`migrate`              — apply pending SQL migrations.
`run`                  — invoke the orchestrator end-to-end and print
                         a summary, including the deterministic
                         ranker + notifier steps (Sprint 2). The
                         Sprint 3 observability layer prints
                         per-phase progress to stderr in real time
                         and persists a structured run report.
`validate-quality`     — dump database stats, 3 sample rows, and
                         per-source field-coverage report (Pillar D).
`list-dangerous`       — print the bootstrapped dangerous-neighborhoods
                         table (Sprint 2 operator inspection).
`show-run`             — re-print a persisted run report from the
                         `run_reports` table (Pillar A).
`backfill-dedup-keys`  — compute `dedup_key` for every existing
                         apartment row that has `NULL` (Pillar F).
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import uuid
from typing import Any

import typer

from deep_apartment_finder.adapters.observability.cli_observer import (
    CliRunObserver,
)
from deep_apartment_finder.adapters.observability.recording_observer import (
    RecordingRunObserver,
)
from deep_apartment_finder.adapters.observability.tracing import (
    configure_langsmith_from_settings,
    current_trace_url,
    langsmith_tracing_enabled,
    root_trace,
)
from deep_apartment_finder.adapters.postgres.migrations import apply_migrations
from deep_apartment_finder.adapters.scrapers.idealista.detail_client import (
    install_playwright_chromium,
)
from deep_apartment_finder.config import get_settings
from deep_apartment_finder.domain.geo import compute_dedup_key
from deep_apartment_finder.main import (
    _MIGRATIONS_DIR,
    build_app,
    build_notifier_for_cli,
    build_orchestrator_for_cli,
)
from deep_apartment_finder.ports.run_observer import RunObserver

app = typer.Typer(
    name="deep-apartment-finder",
    help=(
        "Agent-driven Zaragoza apartment finder (Sprint 4: "
        "Idealista detail-page upgrade + parallel scraper execution)."
    ),
    no_args_is_help=True,
)


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


# --- migrate ---------------------------------------------------------------


@app.command("migrate")
def migrate(
    verbose: bool = typer.Option(False, "--verbose", "-v", help="DEBUG logging"),
) -> None:
    """Apply pending Postgres migrations (Sprint 1 + 2 + 3)."""
    _configure_logging(verbose)
    settings = get_settings()

    async def _run() -> None:
        from deep_apartment_finder.adapters.postgres.connection import get_pool

        pool = await get_pool(settings)
        try:
            applied = await apply_migrations(pool, _MIGRATIONS_DIR)
            if applied:
                typer.echo(
                    f"applied {len(applied)} migration(s): "
                    + ", ".join(m.version for m in applied)
                )
            else:
                typer.echo("no pending migrations")
        finally:
            await pool.close()

    try:
        asyncio.run(_run())
    except Exception as exc:  # noqa: BLE001
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc


# --- run -------------------------------------------------------------------


@app.command("install-browsers")
def install_browsers() -> None:
    """Install browser binaries needed by Playwright-backed scrapers."""
    try:
        install_playwright_chromium()
    except Exception as exc:  # noqa: BLE001
        typer.echo(f"error: failed to install Playwright Chromium: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo("Playwright Chromium is installed.")


# --- run -------------------------------------------------------------------


@app.command("run")
def run(
    skip_llm: bool = typer.Option(
        False,
        "--skip-llm",
        help=(
            "Skip the LLM part of the run (scraper subagent) and go "
            "straight to the deterministic ranker + notifier. Used by "
            "the cron path to avoid an extra LLM call when the "
            "scraper has already run today."
        ),
    ),
    no_detail_fetch: bool = typer.Option(
        False,
        "--no-detail-fetch",
        help=(
            "Sprint 4: disable the playwright-based Idealista detail "
            "page fetch for this run. The scraper falls back to the "
            "search-card path; `bathrooms` stays `None` on every "
            "Idealista row. Equivalent to `IDEALISTA_DETAIL_FETCH=disabled`."
        ),
    ),
    trace: bool = typer.Option(
        False,
        "--trace",
        help=(
            "Compatibility flag. LangSmith tracing is enabled automatically "
            "when LANGSMITH_API_KEY is configured; without that key tracing "
            "stays off."
        ),
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="DEBUG logging"),
) -> None:
    """Run the orchestrator end-to-end. Prints a summary when done.

    The Sprint 4 flow is: researcher (only if
    `dangerous_neighborhoods` is empty) -> fotocasa_scraper +
    idealista_scraper (LLM, run concurrently via `run_scrapers`) ->
    ranker (Python) -> notifier (Python). On the first run the
    orchestrator stops after the researcher has bootstrapped the
    constants table; the operator is asked to re-run.

    Observability (Sprint 3 Pillar A + B): phase headers and
    counters are written to **stderr** in real time by the
    `CliRunObserver`; a structured `RunReport` is persisted to
    `/orchestrator/reports/<run-uuid>.json` and the `run_reports`
    Postgres table by the `RecordingRunObserver`. The
    LangSmith trace URL is printed at the end of the run when
    tracing is on.
    """
    _configure_logging(verbose)
    settings = get_settings()
    if no_detail_fetch:
        settings = settings.model_copy(
            update={"idealista_detail_fetch": False}
        )
    configure_langsmith_from_settings(settings, force=trace)

    async def _run() -> dict[str, Any]:
        run_id = str(uuid.uuid4())
        cli_observer = CliRunObserver()
        recording_observer = RecordingRunObserver(run_id=run_id)
        # Fan-out: every event lands in both observers.
        observers: list[RunObserver] = [cli_observer, recording_observer]
        observer = _FanOutObserver(observers)
        with root_trace("cli.run", metadata={"run_id": run_id, "skip_llm": skip_llm}):
            await observer.phase_start("setup", run_id=run_id)
            await observer.decision(
                "LangSmith tracing",
                "on" if langsmith_tracing_enabled() else "off",
            )

            ctx = await build_app(settings)
            try:
                orchestrator = build_orchestrator_for_cli(
                    ctx,
                    notifier=build_notifier_for_cli(ctx),
                    observer=observer,
                )
                await observer.phase_end("setup", duration_ms=0)

                # 1. First-run gate: check if the dangerous-neighborhoods
                #    table is empty. If so, the LLM has to decide whether
                #    to call the researcher subagent. We short-circuit
                #    here when the gate has already been satisfied AND
                #    the user passed --skip-llm.
                count = await ctx.dangerous_repo.count()
                if count == 0 and not skip_llm:
                    # Let the LLM drive the first run (it will call
                    # `researcher` via `task`, then stop).
                    await observer.phase_start(
                        "researcher",
                        note="first run: bootstrapping dangerous_neighborhoods",
                    )
                    config = {"configurable": {"thread_id": run_id}}
                    prompt = _build_first_run_prompt(settings)
                    result = await orchestrator.ainvoke(
                        {"messages": [{"role": "user", "content": prompt}]},
                        config=config,
                    )
                    await observer.decision(
                        "researcher",
                        "first run: operator must re-run after eyeballing the list",
                    )
                    await observer.phase_end(
                        "researcher", duration_ms=0, errors=0
                    )
                    summary = _summarize_llm_result(result, deterministic=None)
                    return await _finalize_run(
                        summary=summary,
                        cli_observer=cli_observer,
                        recording_observer=recording_observer,
                        ctx=ctx,
                    )

                if count == 0 and skip_llm:
                    typer.echo(
                        "dangerous_neighborhoods is empty; the researcher "
                        "subagent must run first. Re-run without --skip-llm.",
                        err=True,
                    )
                    raise typer.Exit(code=2)

                # 2. LLM part (scrapers) — unless --skip-llm.
                if not skip_llm:
                    await observer.phase_start("scraper")
                    await observer.waiting("LLM")
                    config = {"configurable": {"thread_id": run_id}}
                    prompt = _build_subsequent_run_prompt(settings)
                    await orchestrator.ainvoke(
                        {"messages": [{"role": "user", "content": prompt}]},
                        config=config,
                    )
                    await observer.phase_end("scraper", duration_ms=0)

                # 3. Deterministic part (ranker + notifier).
                await observer.phase_start("deterministic_tail")
                deterministic = await orchestrator.deterministic.run()
                await observer.phase_end("deterministic_tail", duration_ms=0)
                summary = _summarize_llm_result(None, deterministic=deterministic)
                return await _finalize_run(
                    summary=summary,
                    cli_observer=cli_observer,
                    recording_observer=recording_observer,
                    ctx=ctx,
                )
            finally:
                await ctx.scraper.close()
                if ctx.idealista_scraper is not None:
                    try:
                        await ctx.idealista_scraper.close()
                    except Exception:  # noqa: BLE001
                        pass
                await ctx.pool.close()

    try:
        summary = asyncio.run(_run())
        typer.echo(json.dumps(summary, indent=2, default=str))
    except typer.Exit:
        raise
    except Exception as exc:  # noqa: BLE001
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc


class _FanOutObserver(RunObserver):
    """A `RunObserver` that forwards every event to a list of observers.

    The CLI builds one of these to wire the same event into the
    `CliRunObserver` (stderr) and the `RecordingRunObserver`
    (persisted JSON) without coupling them.
    """

    def __init__(self, observers: list[RunObserver]) -> None:
        self._observers = list(observers)

    async def phase_start(self, name: str, **meta: Any) -> None:
        for o in self._observers:
            await o.phase_start(name, **meta)

    async def phase_end(
        self,
        name: str,
        *,
        duration_ms: int,
        counts: dict[str, int] | None = None,
        errors: int = 0,
    ) -> None:
        for o in self._observers:
            await o.phase_end(
                name,
                duration_ms=duration_ms,
                counts=counts,
                errors=errors,
            )

    async def count(self, name: str, n: int = 1) -> None:
        for o in self._observers:
            await o.count(name, n)

    async def waiting(self, label: str) -> None:
        for o in self._observers:
            await o.waiting(label)

    async def decision(self, label: str, value: str) -> None:
        for o in self._observers:
            await o.decision(label, value)

    async def warning(self, msg: str) -> None:
        for o in self._observers:
            await o.warning(msg)

    async def error(self, msg: str, *, exc: BaseException | None = None) -> None:
        for o in self._observers:
            await o.error(msg, exc=exc)


async def _finalize_run(
    *,
    summary: dict[str, Any],
    cli_observer: CliRunObserver,
    recording_observer: RecordingRunObserver,
    ctx: Any,
) -> dict[str, Any]:
    """Decorate the summary with the run report + LangSmith URL.

    Persists the report to disk and (when a Postgres pool is up) to
    the `run_reports` table. Stamps the report's top-N with the
    enriched apartment fields (Pillar C). Prints the trace URL when
    LangSmith is on.
    """
    # Pillar C: enrich the top-N in the deterministic block with
    # the apartment fields the operator wants to see.
    deterministic = summary.get("deterministic")
    if deterministic and deterministic.get("ranking"):
        await _enrich_top_n(
            ctx, recording_observer, deterministic["ranking"]
        )
        # Also stamp the dedup_dropped count + the ranking_run_id
        # on the recording observer's report.
        report = recording_observer.report
        ranking = deterministic["ranking"]
        report.ranking_run_id = str(ranking.get("ranking_run_id", ""))
        report.set_dedup_dropped(ranking.get("dedup_dropped", 0))
        if deterministic.get("notification"):
            n = deterministic["notification"]
            report.notification_sent = bool(n.get("sent"))
            report.notification_skipped_reason = n.get("skipped_reason")
            report.notification_subject = n.get("subject")

    # Capture the LangSmith trace URL before we tear down the run.
    report = recording_observer.report
    url = current_trace_url()
    if url:
        report.trace_url = url

    # Persist to /orchestrator/reports/<run-uuid>.json.
    report_path = f"/orchestrator/reports/{report.run_id}.json"
    try:

        await recording_observer.finalize(
            backend=ctx.observability_backend  # type: ignore[arg-type]
            if hasattr(ctx, "observability_backend")
            else None,
            report_path=report_path,
        )
    except Exception as exc:  # noqa: BLE001
        typer.echo(f"warning: run report persist failed: {exc}", err=True)

    # Persist to the run_reports Postgres table.
    try:
        from deep_apartment_finder.adapters.postgres.run_report_repository import (
            PostgresRunReportRepository,
        )

        await PostgresRunReportRepository(ctx.pool).upsert(report)
    except Exception as exc:  # noqa: BLE001
        typer.echo(f"warning: run_reports row persist failed: {exc}", err=True)

    # Decorate the summary with run report metadata.
    summary["run_report_path"] = report.report_path
    summary["trace_url"] = report.trace_url
    return summary


async def _enrich_top_n(
    ctx: Any,
    recording_observer: RecordingRunObserver,
    ranking: dict[str, Any],
) -> None:
    """Join the top-N apartment ids with their stored fields so the
    CLI stdout, the persisted run report, and the email body all
    show the same `title`, `price_eur`, `rooms`, `bathrooms`,
    `size_m2`, `address`, `url`, `final_score`, and per-criterion
    `breakdown`.

    The orchestrator's `_DeterministicSteps` already builds the
    `apartments_by_id` map; we don't re-query Postgres for the
    data the ranker already saw. The repo's `list_all` here is
    only used as a fallback for the in-memory tests; the hot path
    pulls from the orchestrator's payload.
    """
    from deep_apartment_finder.domain.ranking import RankableApartment

    top = list(ranking.get("top") or [])
    if not top:
        return
    db_ids = {int(r["apartment_id"]) for r in top}
    try:
        rows = await ctx.repo.list_all(limit=5000)
    except Exception:  # noqa: BLE001
        rows = []
    by_id: dict[int, RankableApartment] = {
        db_id: RankableApartment(apartment=apt, db_id=db_id)
        for db_id, apt in rows
        if db_id in db_ids
    }

    enriched: list[dict[str, Any]] = []
    for row in top:
        apt_id = int(row["apartment_id"])
        rankable = by_id.get(apt_id)
        if rankable is None:
            continue
        apt = rankable.apartment
        enriched.append(
            {
                "apartment_id": apt_id,
                "title": apt.title,
                "price_eur": float(apt.price_eur) if apt.price_eur is not None else None,
                "rooms": apt.rooms,
                "bathrooms": apt.bathrooms,
                "size_m2": float(apt.size_m2) if apt.size_m2 is not None else None,
                "address": apt.address,
                "url": apt.url,
                "source": apt.source.value,
                "final_score": row.get("score"),
                "breakdown": row.get("breakdown") or [],
            }
        )
    if enriched:
        ranking["top"] = enriched
        recording_observer.report.set_top_n(enriched)


def _build_first_run_prompt(settings: Any) -> str:
    return (
        "First run of deep-apartment-finder. "
        "The `dangerous_neighborhoods` table is empty. "
        "Delegate to the `researcher` subagent with the brief "
        "'bootstrap the dangerous-neighborhoods constants table "
        "for Zaragoza, then report how many rows you wrote.' "
        "After the researcher returns, write a short report to "
        "/orchestrator/reports/first-run.md and stop. Do NOT call "
        "any scraper on this first run; the operator will re-run "
        "the CLI after eyeballing the bootstrapped list. "
        "Print a one-paragraph summary that includes the number "
        "of neighborhoods the researcher wrote and a reminder to "
        "re-run the CLI."
    )


def _build_subsequent_run_prompt(settings: Any) -> str:
    return (
        f"Plan a Sprint 3 run for Zaragoza (Fotocasa + Idealista). "
        f"Apply the hard filters (rooms >= 2, bathrooms >= 2, "
        f"size >= 50 m^2, price <= 1200 EUR). Cap at "
        f"{settings.ingest_max_listings} listings per portal. "
        "Delegate to BOTH `fotocasa_scraper` and "
        "`idealista_scraper` subagents; remember to extract "
        "`pet_policy` and `furnished` from each listing's "
        "description before calling `ingest_apartment`. Then "
        f"write a report to /orchestrator/reports/<run-uuid>.md "
        "and return a one-paragraph summary. The deterministic "
        "ranker + notifier will run after your turn."
    )


def _summarize_llm_result(
    result: Any, *, deterministic: dict[str, Any] | None
) -> dict[str, Any]:
    """Build the JSON summary the CLI prints."""
    out: dict[str, Any] = {}
    if isinstance(result, dict):
        messages = result.get("messages", [])
        summary_text = ""
        for m in reversed(messages):
            if getattr(m, "type", None) == "ai":
                summary_text = (
                    m.content if isinstance(m.content, str) else str(m.content)
                )
                break
        out["llm"] = {
            "messages": len(messages),
            "todos": result.get("todos"),
            "summary": summary_text,
        }
    if deterministic is not None:
        out["deterministic"] = _format_deterministic(deterministic)
    return out


def _format_deterministic(d: dict[str, Any]) -> dict[str, Any]:
    """Make the deterministic result JSON-friendly.

    Pillar C (Sprint 3): the top-N rows are already enriched with
    apartment fields by `_enrich_top_n`; we just re-pack them for
    the JSON output.
    """
    ranking = d.get("ranking")
    notif = d.get("notification")
    return {
        "apartments_scored": d.get("apartments_scored", 0),
        "ranking": (
            {
                "ranking_run_id": str(ranking["ranking_run_id"]) if ranking else None,
                "scores_written": ranking.get("scores_written", 0) if ranking else 0,
                "dedup_dropped": ranking.get("dedup_dropped", 0) if ranking else 0,
                "top_n_returned": len(ranking.get("top", [])) if ranking else 0,
                "top": ranking.get("top", []) if ranking else [],
            }
            if ranking
            else None
        ),
        "notification": (
            {
                "sent": notif.sent,
                "skipped_reason": notif.skipped_reason,
                "subject": notif.subject,
                "ranking_run_id": notif.ranking_run_id,
                "apartment_ids": notif.apartment_ids,
                "outbox_txt_path": notif.outbox_txt_path,
                "outbox_html_path": notif.outbox_html_path,
            }
            if notif
            else None
        ),
    }


# --- list-dangerous --------------------------------------------------------


@app.command("list-dangerous")
def list_dangerous(
    verbose: bool = typer.Option(False, "--verbose", "-v", help="DEBUG logging"),
) -> None:
    """Print the bootstrapped dangerous-neighborhoods table."""
    _configure_logging(verbose)

    async def _run() -> list[dict[str, Any]]:
        ctx = await build_app()
        try:
            return [
                {
                    "name": n.name,
                    "center_lat": n.center_lat,
                    "center_lng": n.center_lng,
                    "radius_m": n.radius_m,
                }
                for n in await ctx.dangerous_repo.list_all()
            ]
        finally:
            await ctx.scraper.close()
            await ctx.pool.close()

    try:
        rows = asyncio.run(_run())
        typer.echo(json.dumps({"count": len(rows), "rows": rows}, indent=2))
    except Exception as exc:  # noqa: BLE001
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc


# --- validate-quality ------------------------------------------------------


@app.command("validate-quality")
def validate_quality(
    verbose: bool = typer.Option(False, "--verbose", "-v", help="DEBUG logging"),
) -> None:
    """Dump database counts, 3 sample rows, and per-source field coverage.

    Sprint 3 (Pillar D + F) added:
    - per-source null rate for `lat`, `lng`, `pet_policy`,
      `furnished`, `description`
    - count of rows with invalid `(0, 0)` / out-of-bbox coordinates
      (expected 0 after Sprint 3 normalisation)
    - count of cross-portal dedup-key collisions
    """
    _configure_logging(verbose)
    settings = get_settings()

    async def _run() -> dict[str, Any]:
        ctx = await build_app(settings)
        try:
            total = await ctx.repo.count()
            duplicates = await ctx.repo.duplicate_key_count()
            cross_dup = await ctx.repo.cross_portal_dup_count()
            recent = await ctx.repo.recent(limit=3)
            samples = []
            for apt in recent:
                samples.append(
                    {
                        "source": apt.source.value,
                        "external_id": apt.external_id,
                        "url": apt.url,
                        "title": apt.title,
                        "price_eur": float(apt.price_eur) if apt.price_eur is not None else None,
                        "rooms": apt.rooms,
                        "bathrooms": apt.bathrooms,
                        "size_m2": float(apt.size_m2) if apt.size_m2 is not None else None,
                        "address": apt.address,
                        "lat": float(apt.lat) if apt.lat is not None else None,
                        "lng": float(apt.lng) if apt.lng is not None else None,
                        "description": (apt.description or "")[:200],
                        "pet_policy": apt.pet_policy,
                        "furnished": apt.furnished,
                        "dedup_key": (apt.raw or {}).get("dedup_key"),
                        "scraped_at": apt.scraped_at.isoformat() if apt.scraped_at else None,
                    }
                )
            field_coverage = await ctx.repo.field_coverage()
            return {
                "counts": {
                    "total": total,
                    "new": max(total - duplicates, 0),
                    "duplicates": duplicates,
                    "cross_portal_dups": cross_dup,
                },
                "total": total,
                "new": max(total - duplicates, 0),
                "duplicates": duplicates,
                "cross_portal_dups": cross_dup,
                "sampled": len(samples),
                "samples": samples,
                "field_coverage": field_coverage,
            }
        finally:
            await ctx.scraper.close()
            await ctx.pool.close()

    try:
        report = asyncio.run(_run())
        typer.echo(json.dumps(report, indent=2, default=str))
    except Exception as exc:  # noqa: BLE001
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc


# --- show-run --------------------------------------------------------------


@app.command("show-run")
def show_run(
    run_id: str = typer.Argument(..., help="UUID of the run to display"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="DEBUG logging"),
) -> None:
    """Re-print a persisted run report (Pillar A).

    Reads the `run_reports` Postgres table for the given
    `run_id` and pretty-prints the structured record. Used by
    operators to inspect a past run without `jq` on the JSON
    file.
    """
    _configure_logging(verbose)
    settings = get_settings()

    async def _run() -> dict[str, Any]:
        from deep_apartment_finder.adapters.postgres.connection import get_pool
        from deep_apartment_finder.adapters.postgres.run_report_repository import (
            PostgresRunReportRepository,
        )

        pool = await get_pool(settings)
        try:
            repo = PostgresRunReportRepository(pool)
            row = await repo.fetch(run_id)
            if row is None:
                typer.echo(
                    f"no run report with run_id={run_id!r}",
                    err=True,
                )
                raise typer.Exit(code=2)
            return row
        finally:
            await pool.close()

    try:
        row = asyncio.run(_run())
        typer.echo(json.dumps(row, indent=2, default=str))
    except typer.Exit:
        raise
    except Exception as exc:  # noqa: BLE001
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc


# --- backfill-dedup-keys ---------------------------------------------------


@app.command("backfill-dedup-keys")
def backfill_dedup_keys(
    verbose: bool = typer.Option(False, "--verbose", "-v", help="DEBUG logging"),
) -> None:
    """Compute `dedup_key` for every existing apartment row that has NULL.

    Sprint 3 (Pillar F): the `003_sprint3.sql` migration added a
    nullable `dedup_key` column. Existing rows have `NULL`. This
    command backfills them.

    Re-running is a no-op: rows that already have a non-NULL
    `dedup_key` are left alone. Rows whose computed key collides
    with an already-taken key are logged and left NULL for the
    operator to inspect.
    """
    _configure_logging(verbose)
    settings = get_settings()

    async def _run() -> dict[str, Any]:
        from deep_apartment_finder.adapters.postgres.connection import get_pool

        pool = await get_pool(settings)
        try:
            async with pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT id, address, rooms, size_m2, price_eur, dedup_key "
                    "FROM apartments WHERE dedup_key IS NULL"
                )
            updated = 0
            collided = 0
            skipped = 0
            for row in rows:
                key = compute_dedup_key(
                    address=row["address"],
                    rooms=row["rooms"],
                    size_m2=row["size_m2"],
                    price_eur=row["price_eur"],
                )
                if key is None:
                    skipped += 1
                    continue
                # Check collision before writing.
                async with pool.acquire() as conn:
                    existing = await conn.fetchval(
                        "SELECT 1 FROM apartments WHERE dedup_key = $1 AND id <> $2",
                        key,
                        row["id"],
                    )
                    if existing is not None:
                        collided += 1
                        typer.echo(
                            f"  id={row['id']}: collided key={key[:8]}...; left NULL"
                        )
                        continue
                    await conn.execute(
                        "UPDATE apartments SET dedup_key = $1 WHERE id = $2",
                        key,
                        row["id"],
                    )
                updated += 1
            return {
                "considered": len(rows),
                "updated": updated,
                "collided": collided,
                "skipped": skipped,
            }
        finally:
            await pool.close()

    try:
        report = asyncio.run(_run())
        typer.echo(json.dumps(report, indent=2))
    except Exception as exc:  # noqa: BLE001
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc


if __name__ == "__main__":
    sys.exit(app())
