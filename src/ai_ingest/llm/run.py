import argparse
import asyncio
import json
import os

from datetime import datetime, timezone
from pathlib import Path

import aiohttp

from ai_ingest.llm.engine import ExtractionEngine, ExtractionFailed
from ai_ingest.llm.providers import HTTPProvider
from ai_ingest.config import load_environment

async def run_extraction(
    *,
    input_path: Path,
    source_url: str,
    output_dir: Path,
    chunk_bytes: int,
) -> dict:
    text = input_path.read_text(encoding="utf-8")

    # Keep the first live test deliberately small.
    if len(text.encode("utf-8")) > 20_000:
        raise ValueError(
            "Smoke-test input must be no larger than 20 KB."
        )

    configured = []

    for kind in ("gemini", "groq", "deepseek"):
        prefix = kind.upper()

        key = os.environ.get(
            f"{prefix}_API_KEY", ""
        ).strip()

        model = os.environ.get(
            f"{prefix}_MODEL", ""
        ).strip()

        if bool(key) != bool(model):
            raise ValueError(
                f"Configure both {prefix}_API_KEY "
                f"and {prefix}_MODEL, or neither."
            )

        if key and model:
            configured.append((kind, model, key))

    if not configured:
        raise ValueError(
            "No provider configured in the process environment."
        )

    run_dir = output_dir / datetime.now(
        timezone.utc
    ).strftime("%Y%m%dT%H%M%S%fZ")

    run_dir.mkdir(parents=True, exist_ok=False)

    (run_dir / "source.txt").write_text(
        text,
        encoding="utf-8",
    )

    configuration = {
        "source_url": source_url,
        "input_path": str(input_path.resolve()),
        "provider_order": [
            {"provider": kind, "model": model}
            for kind, model, _key in configured
        ],
        "chunk_bytes": chunk_bytes,
        "purpose": "SMOKE_TEST",
        "canonical_records_updated": False,
    }

    (run_dir / "configuration.json").write_text(
        json.dumps(configuration, indent=2),
        encoding="utf-8",
    )

    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=75),
    ) as session:
        providers = [
            HTTPProvider(
                kind=kind,
                model=model,
                api_key=key,
                session=session,
                evidence_dir=run_dir / "provider_responses",
            )
            for kind, model, key in configured
        ]

        engine = ExtractionEngine(
            providers,
            chunk_bytes=chunk_bytes,
            max_requests=12,
            max_wait_seconds=30,
        )

        try:
            result = await engine.extract(text)

        except ExtractionFailed as exc:
            failure_path = run_dir / "failure.json"

            failure = {
                "error": str(exc),
                "events": exc.events,
                "canonical_records_updated": False,
            }

            failure_path.write_text(
                json.dumps(failure, indent=2),
                encoding="utf-8",
            )

            raise RuntimeError(
                f"{exc} Diagnostics saved to {failure_path}"
            ) from exc

    result.update(
        {
            "source_url": source_url,
            "purpose": "SMOKE_TEST",
            "canonical_records_updated": False,
        }
    )

    (run_dir / "extraction.json").write_text(
        json.dumps(result, indent=2),
        encoding="utf-8",
    )

    return {
        "configured_provider_order": [
            provider["provider"]
            for provider in configuration["provider_order"]
        ],
        "requests_used": result["requests_used"],
        "chunks_completed": len(result["chunks"]),
        "providers_used": sorted(
            {
                chunk["provider"]
                for chunk in result["chunks"]
            }
        ),
        "events": result["events"],
        "output_dir": str(run_dir),
        "canonical_records_updated": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run a small evidence-grounded LLM smoke test."
    )

    parser.add_argument(
        "--input",
        required=True,
        type=Path,
    )

    parser.add_argument(
        "--source-url",
        required=True,
    )

    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("data/llm"),
    )

    parser.add_argument(
        "--chunk-bytes",
        type=int,
        default=6000,
    )

    parser.add_argument(
        "--allow-api-calls",
        action="store_true",
        help="Explicitly permit potentially billable API requests.",
    )

    parser.add_argument(
        "--env-file",
        type=Path,
        default=Path(".env"),
        help="Environment file; its values override existing variables.",
    )

    args = parser.parse_args()

    if not args.allow_api_calls:
        parser.error(
            "Live requests require --allow-api-calls."
        )

    try:
        load_environment(args.env_file)
        
        result = asyncio.run(
            run_extraction(
                input_path=args.input,
                source_url=args.source_url,
                output_dir=args.output_dir,
                chunk_bytes=args.chunk_bytes,
            )
        )
    except Exception as exc:
        parser.exit(
            status=1,
            message=f"LLM extraction failed: {exc}\n",
        )

    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()