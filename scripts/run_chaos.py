from __future__ import annotations

import argparse
import random

from reliability_lab.chaos import load_queries, run_simulation
from reliability_lab.config import load_config


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="configs/default.yaml")
    parser.add_argument("--out", default="reports/metrics.json")
    parser.add_argument(
        "--seed",
        type=int,
        default=None,
        help="Seed the global random module for reproducible runs (fixes provider "
        "failure/latency rolls and query selection).",
    )
    args = parser.parse_args()
    if args.seed is not None:
        random.seed(args.seed)
    config = load_config(args.config)
    metrics = run_simulation(config, load_queries())
    metrics.write_json(args.out)
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
