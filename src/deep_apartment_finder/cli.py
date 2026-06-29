"""CLI entrypoints.

`migrate`              — apply pending SQL migrations.
`run`                  — invoke the orchestrator end-to-end and print
                         a summary, including the deterministic
                         ranker + notifier steps (Sprint 2).
`validate-quality`     — dump database stats + 3 sample rows
                         (acceptance criterion 4 from SPRINT1).
`list-dangerous`       — print the bootstrapped dangerous-neighborhoods
                         table (Sprint 2 operator inspection).
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import uuid
from typing import Any

import typer

from deep_apartment_finder.adapters.postgres.migrations import apply_migrations
from deep_apartment_finder.config import get_settings
from deep_apartment_finder.main import (
    _MIGRATIONS_DIR,
    build_app,
    build_notifier_for_cli,
    build_orchestrator_for_cli,
)

app = typer.Typer(
    name="deep-apartment-finder",
    help=(
        "Agent-driven Zaragoza apartment finder (Sprint 2: "
        "ranking + soft filters + notification)."
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
    """Apply pending Postgres migrations."""
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
    verbose: bool = typer.Option(False, "--verbose", "-v", help="DEBUG logging"),
) -> None:
    """Run the orchestrator end-to-end. Prints a summary when done.

    The flow is: researcher (only if `dangerous_neighborhoods` is
    empty) -> fotocasa_scraper (LLM) -> ranker (Python) -> notifier
    (Python). On the first run the orchestrator stops after the
    researcher has bootstrapped the constants table; the operator
    is asked to re-run.
    """
    _configure_logging(verbose)
    settings = get_settings()

    async def _run() -> dict[str, Any]:
        ctx = await build_app(settings)
        try:
            orchestrator = build_orchestrator_for_cli(
                ctx, notifier=build_notifier_for_cli(ctx)
            )

            # 1. First-run gate: check if the dangerous-neighborhoods
            #    table is empty. If so, the LLM has to decide whether
            #    to call the researcher subagent. We short-circuit
            #    here when the gate has already been satisfied AND
            #    the user passed --skip-llm.
            count = await ctx.dangerous_repo.count()
            if count == 0 and not skip_llm:
                # Let the LLM drive the first run (it will call
                # `researcher` via `task`, then stop).
                run_id = str(uuid.uuid4())
                config = {"configurable": {"thread_id": run_id}}
                prompt = _build_first_run_prompt(settings)
                result = await orchestrator.ainvoke(
                    {"messages": [{"role": "user", "content": prompt}]},
                    config=config,
                )
                return _summarize_llm_result(result, deterministic=None)

            if count == 0 and skip_llm:
                typer.echo(
                    "dangerous_neighborhoods is empty; the researcher "
                    "subagent must run first. Re-run without --skip-llm.",
                    err=True,
                )
                raise typer.Exit(code=2)

            # 2. LLM part (scraper) — unless --skip-llm.
            llm_result = None
            if not skip_llm:
                run_id = str(uuid.uuid4())
                config = {"configurable": {"thread_id": run_id}}
                prompt = _build_subsequent_run_prompt(settings)
                llm_result = await orchestrator.ainvoke(
                    {"messages": [{"role": "user", "content": prompt}]},
                    config=config,
                )

            # 3. Deterministic part (ranker + notifier).
            deterministic = await orchestrator.deterministic.run()

            return _summarize_llm_result(llm_result, deterministic=deterministic)
        finally:
            await ctx.scraper.close()
            await ctx.pool.close()

    try:
        summary = asyncio.run(_run())
        typer.echo(json.dumps(summary, indent=2, default=str))
    except typer.Exit:
        raise
    except Exception as exc:  # noqa: BLE001
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc


def _build_first_run_prompt(settings: Any) -> str:
    return (
        "First run of deep-apartment-finder. "
        "The `dangerous_neighborhoods` table is empty. "
        "Delegate to the `researcher` subagent with the brief "
        "'bootstrap the dangerous-neighborhoods constants table "
        "for Zaragoza, then report how many rows you wrote.' "
        "After the researcher returns, write a short report to "
        "/orchestrator/reports/first-run.md and stop. Do NOT call "
        "fotocasa_scraper on this first run; the operator will "
        "re-run the CLI after eyeballing the bootstrapped list. "
        "Print a one-paragraph summary that includes the number "
        "of neighborhoods the researcher wrote and a reminder to "
        "re-run the CLI."
    )


def _build_subsequent_run_prompt(settings: Any) -> str:
    return (
        f"Plan a Sprint 2 Fotocasa run for Zaragoza. "
        f"Apply the hard filters (rooms >= 2, bathrooms >= 2, "
        f"size >= 50 m^2, price <= 1200 EUR). Cap at "
        f"{settings.ingest_max_listings} listings. "
        "Delegate to the fotocasa_scraper subagent; remember to "
        "extract `pet_policy` and `furnished` from each listing's "
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
    """Make the deterministic result JSON-friendly."""
    ranking = d.get("ranking")
    notif = d.get("notification")
    return {
        "apartments_scored": d.get("apartments_scored", 0),
        "ranking": {
            "ranking_run_id": str(ranking["ranking_run_id"]) if ranking else None,
            "scores_written": ranking.get("scores_written", 0) if ranking else 0,
            "top_n_returned": len(ranking.get("top", [])) if ranking else 0,
            "top": ranking.get("top", []) if ranking else [],
        }
        if ranking
        else None,
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
    """Print the bootstrapped dangerous-neighborhoods table.

    Sprint 2 operator tool: lets the operator eyeball the
    researcher's work and (manually, via SQL) override rows.
    """
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
    """Dump database counts and three sample rows.

    Acceptance criterion (4) from SPRINT1.md: prints total / new vs
    duplicate, and three sample rows with the key fields populated.
    """
    _configure_logging(verbose)
    settings = get_settings()

    async def _run() -> dict[str, Any]:
        ctx = await build_app(settings)
        try:
            total = await ctx.repo.count()
            duplicates = await ctx.repo.duplicate_key_count()
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
                        "scraped_at": apt.scraped_at.isoformat() if apt.scraped_at else None,
                    }
                )
            return {
                "counts": {
                    "total": total,
                    "new": max(total - duplicates, 0),
                    "duplicates": duplicates,
                },
                "total": total,
                "new": max(total - duplicates, 0),
                "duplicates": duplicates,
                "sampled": len(samples),
                "samples": samples,
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


if __name__ == "__main__":
    sys.exit(app())
