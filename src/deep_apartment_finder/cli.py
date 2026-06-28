"""CLI entrypoints.

`migrate` — apply pending SQL migrations.
`run`      — invoke the orchestrator end-to-end and print a summary.
`validate-quality` — dump database stats + 3 sample rows (acceptance criterion 4).
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
    build_orchestrator_for_cli,
)

app = typer.Typer(
    name="deep-apartment-finder",
    help="Agent-driven Zaragoza apartment finder (Sprint 1: Fotocasa ingestion MVP).",
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
    verbose: bool = typer.Option(False, "--verbose", "-v", help="DEBUG logging"),
) -> None:
    """Run the orchestrator end-to-end. Prints a summary when done."""
    _configure_logging(verbose)
    settings = get_settings()

    async def _run() -> dict[str, Any]:
        ctx = await build_app(settings)
        try:
            agent = build_orchestrator_for_cli(ctx)
            run_id = str(uuid.uuid4())
            config = {"configurable": {"thread_id": run_id}}
            prompt = (
                "Plan a Sprint 1 Fotocasa run for Zaragoza. "
                "Apply the hard filters (rooms >= 2, bathrooms >= 2, "
                "size >= 50 m^2, price <= 1200 EUR). Cap at "
                f"{settings.ingest_max_listings} listings. "
                "Delegate to the fotocasa_scraper subagent, then write a "
                f"report to /orchestrator/reports/{run_id}.md and return a "
                "one-paragraph summary."
            )
            result = await agent.ainvoke(
                {"messages": [{"role": "user", "content": prompt}]},
                config=config,
            )
            return _summarize_result(result)
        finally:
            await ctx.scraper.close()
            await ctx.pool.close()

    try:
        summary = asyncio.run(_run())
        typer.echo(json.dumps(summary, indent=2, default=str))
    except Exception as exc:  # noqa: BLE001
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc


def _summarize_result(result: Any) -> dict[str, Any]:
    """Extract a one-paragraph summary from the orchestrator's final state.

    The shape of `result` is a dict with `messages` (a list of base
    messages) and possibly `todos` (the TodoList). We pull the last
    AIMessage as the user-facing summary.
    """
    messages = result.get("messages", []) if isinstance(result, dict) else []
    summary_text = ""
    for m in reversed(messages):
        if getattr(m, "type", None) == "ai":
            summary_text = m.content if isinstance(m.content, str) else str(m.content)
            break
    return {
        "messages": len(messages),
        "todos": result.get("todos") if isinstance(result, dict) else None,
        "summary": summary_text,
    }


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
