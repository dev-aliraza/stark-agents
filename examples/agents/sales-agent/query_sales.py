#!/usr/bin/env python3
"""Return sales figures as JSON.

A stand-in for a real warehouse query, so the example runs with no database. The
agent calls this through the built-in `file_run` tool.

    python query_sales.py            # every region
    python query_sales.py emea       # one region
"""

import json
import sys

SALES = {
    "emea": {"q1_usd": 4_120_000, "q2_usd": 4_480_000, "top_product": "Atlas Pro"},
    "apac": {"q1_usd": 2_640_000, "q2_usd": 3_310_000, "top_product": "Atlas Lite"},
    "namer": {"q1_usd": 7_890_000, "q2_usd": 7_640_000, "top_product": "Atlas Pro"},
}


def main() -> int:
    region = (sys.argv[1] if len(sys.argv) > 1 else "").strip().lower()

    if not region:
        print(json.dumps({"regions": SALES}, indent=2))
        return 0

    if region not in SALES:
        print(
            json.dumps(
                {"error": f"unknown region '{region}'", "available": sorted(SALES)},
                indent=2,
            )
        )
        return 1

    print(json.dumps({"region": region, **SALES[region]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
