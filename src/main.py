"""
Full-Self-Crawl-Agent Alpha — Entry Point

Usage:
    python -m src.main <url> [--requirement "..."] [--mode full_site|single_page]

Example:
    python -m src.main https://books.toscrape.com --requirement "Extract all book titles, prices, and ratings"
"""

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path


def setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )


async def main():
    # Load .env early
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    parser = argparse.ArgumentParser(
        description="Full-Self-Crawl-Agent Alpha — LLM-as-Controller web crawling agent"
    )
    parser.add_argument("url", help="Target URL to crawl")
    parser.add_argument("--requirement", "-r", default="",
                        help="Natural language description of what to extract")
    parser.add_argument("--mode", "-m", default="full_site",
                        choices=["full_site", "single_page"],
                        help="Crawl mode (default: full_site)")
    parser.add_argument("--model", default="",
                        help="LLM model to use (default: from config/settings.json or LLM_MODEL env)")
    parser.add_argument("--api-key", default="",
                        help="LLM API key (or set LLM_API_KEY env var)")
    parser.add_argument("--base-url", default="",
                        help="LLM API base URL (or set LLM_BASE_URL env var)")
    parser.add_argument("--output", "-o", default="output.json",
                        help="Output file path (default: output.json)")
    parser.add_argument("--log-level", default="INFO",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    parser.add_argument("--max-steps", type=int, default=30,
                        help="Max steps per controller loop")
    parser.add_argument("--max-time", type=int, default=300,
                        help="Max time in seconds per controller loop")

    args = parser.parse_args()
    setup_logging(args.log_level)
    logger = logging.getLogger("main")

    logger.info(f"Starting crawl: {args.url}")
    logger.info(f"Mode: {args.mode}, Model: {args.model}")

    from .management.orchestrator import Orchestrator
    from .tools.database import HistoryDB
    import uuid

    config = {
        "llm": {
            "api_key": args.api_key,
            "base_url": args.base_url,
            "model": args.model,
        },
        "max_steps": args.max_steps,
        "max_time": args.max_time,
    }

    db = HistoryDB()
    await db.connect()

    run_id = str(uuid.uuid4())
    await db.begin_run(run_id, args.url, args.mode, args.model, args.requirement)

    orchestrator = Orchestrator(config=config)

    try:
        result = await orchestrator.run(
            start_url=args.url,
            requirement=args.requirement,
            mode=args.mode,
        )

        records = result.get("data", [])
        await db.save_records(run_id, records)
        await db.complete_run(run_id, result.get("success", False), len(records))
        await db.close()

        # Save output
        output_path = Path(args.output)
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2, ensure_ascii=False, default=str)

        # Also save to artifacts/report.json if artifacts dir exists
        artifacts_dir = result.get("artifacts_dir")
        if artifacts_dir:
            report_path = Path(artifacts_dir) / "report.json"
            with open(report_path, "w", encoding="utf-8") as f:
                json.dump(result, f, indent=2, ensure_ascii=False, default=str)

        logger.info(f"Results saved to {output_path}")

        # Print summary
        if result.get("success"):
            print(f"\n✅ Success: {len(result.get('data', []))} records extracted")
        else:
            print(f"\n❌ Failed: {result.get('error', result.get('stop_reason', 'Unknown'))}")

        if result.get("summary"):
            print(f"Summary: {result['summary']}")

        return 0 if result.get("success") else 1

    except KeyboardInterrupt:
        logger.info("Interrupted by user")
        await db.complete_run(run_id, False, 0)
        await db.close()
        return 130


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
