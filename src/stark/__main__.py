from __future__ import annotations

import argparse
import logging
import sys

from .errors import StarkError
from .listeners import SUPPORTED
from .logger import configure_logging, logger
from .runtime import run
from .types import DEFAULT_INSTRUCTIONS


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="stark",
        description="Discover Markdown-defined agents and serve them over a listener.",
    )
    parser.add_argument("--agents", default="./agents", help="agents directory (default: ./agents)")
    parser.add_argument(
        "--listener",
        default="cli",
        choices=list(SUPPORTED),
        help="input listener (default: cli)",
    )
    parser.add_argument(
        "--exclude",
        action="append",
        default=[],
        metavar="NAME",
        help="agent directory to skip; repeatable",
    )
    parser.add_argument(
        "--instructions",
        default=DEFAULT_INSTRUCTIONS,
        help="master system prompt for the orchestration loop",
    )
    parser.add_argument("--verbose", action="store_true", help="enable debug logging")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configure_logging(logging.DEBUG if args.verbose else logging.INFO)

    try:
        run(
            agents=args.agents,
            listener=args.listener,
            exclude_agents=args.exclude,
            instructions=args.instructions,
        )
    except StarkError as exc:
        logger.error("%s", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
