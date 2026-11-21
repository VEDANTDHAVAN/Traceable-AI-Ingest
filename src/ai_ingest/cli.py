import typer

app = typer.Typer(help="AI ecosystem ingestion pipeline")

@app.callback()
def main() -> None:
    """Manage ingestion tasks."""

@app.command()
def status() -> None:
    """Show skeleton status; does not crawl or contact external services."""
    typer.echo("Skeleton ready. No sources configured for live ingestion yet.")

if __name__ == "__main__":
    app()
