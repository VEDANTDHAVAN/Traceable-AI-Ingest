import typer


app = typer.Typer(
    help="Source-traceable AI ecosystem ingestion pipeline.",
    no_args_is_help=True,
)


@app.callback()
def main() -> None:
    """Manage ingestion, validation, and export tasks."""


@app.command()
def status() -> None:
    """Show project status without contacting external services."""
    typer.echo(
        "Skeleton ready. "
        "No sources configured for live ingestion yet."
    )


if __name__ == "__main__":
    app()