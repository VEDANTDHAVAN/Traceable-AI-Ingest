import asyncio
from pathlib import Path

import typer

from ai_ingest.sources.arxiv import ArxivError, ingest_page


app = typer.Typer(
    help="Source-traceable AI ecosystem ingestion pipeline.",
    no_args_is_help=True,
)


@app.callback()
def main() -> None:
    """Manage ingestion, validation, and export tasks."""


@app.command()
def status() -> None:
    """Show available capabilities without contacting external services."""
    typer.echo(
        "arXiv single-page ingestion available. "
        "No sources configured for live ingestion yet."
    )


@app.command("arxiv-fetch")
def arxiv_fetch(
    user_agent: str = typer.Option(
        ...,
        "--user-agent",
        help="Application identifier with a real contact address.",
    ),
    query: str = typer.Option(
        "cat:cs.AI",
        help="arXiv search query.",
    ),
    start: int = typer.Option(
        0,
        min=0,
        help="Starting result offset.",
    ),
    page_size: int = typer.Option(
        10,
        min=1,
        max=100,
        help="Number of records requested.",
    ),
    output_dir: Path = typer.Option(
        Path("data/arxiv"),
        help="Local raw evidence and staging directory.",
    ),
) -> None:
    """Fetch and stage one page; no GitHub enrichment yet."""
    try:
        run_dir = asyncio.run(
            ingest_page(
                query=query,
                start=start,
                page_size=page_size,
                output_dir=output_dir,
                user_agent=user_agent,
            )
        )
    except (ArxivError, ValueError, OSError) as exc:
        typer.echo(f"Ingestion failed: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    typer.echo(f"Saved raw evidence and staged papers: {run_dir}")


if __name__ == "__main__":
    app()