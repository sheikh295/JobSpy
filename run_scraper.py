#!/usr/bin/env python3
"""Run JobSpy and save scraped jobs to a JSON file."""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Any

from jobspy import scrape_jobs


def json_serializer(obj: Any) -> str:
    if isinstance(obj, (date, datetime)):
        return obj.isoformat()
    if hasattr(obj, "item"):
        return obj.item()
    raise TypeError(f"Type {type(obj).__name__} is not JSON serializable")


def save_jobs_to_json(payload: dict, output_path: Path) -> None:
    """Save the recommended schema payload to a JSON file."""
    output_path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, default=json_serializer),
        encoding="utf-8",
    )


def main() -> None:
    args = {
        "site_name": ["indeed"],
        "search_term": "Software Engineer",
        # "location": "United States",
        "results_wanted": 100,
        "country_indeed": "us",
        "hours_old": 24,
        "verbose": True,
        "output_path": Path("scraped_jobs_indeed.json"),
    }

    print("Starting scrape:")
    print(f"  sites: {args['site_name']}")
    print(f"  term: {args['search_term']}")
    # print(f"  location: {args['location']}")
    print(f"  results wanted: {args['results_wanted']}")
    print(f"  hours old: {args['hours_old']}")
    print(f"  output: {args['output_path']}")

    payload = scrape_jobs(
        site_name=args['site_name'],
        search_term=args['search_term'],
        # location=args['location'],
        results_wanted=args['results_wanted'],
        country_indeed=args['country_indeed'],
        hours_old=args['hours_old'],
        verbose=args['verbose'],
    )

    if not payload or payload.get("count", 0) == 0:
        print("No jobs were scraped. Output file will not be created.")
        return

    save_jobs_to_json(payload, args['output_path'])
    print(f"Saved {payload.get('count', 0)} jobs to {args['output_path']}")


if __name__ == "__main__":
    main()
