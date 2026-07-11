"""``python -m evals``: run behavioral evals, compare two runs.

Examples::

    PYTHONPATH=src python -m evals run --driver scripted
    PYTHONPATH=src python -m evals run --case no_duplicate_on_existing_character
    PYTHONPATH=src python -m evals run --trials 3 --label candidate --judge
    PYTHONPATH=src python -m evals compare evals/runs/A evals/runs/B

Exit codes: 0 all cases ok, 1 at least one case failed (or a regression in
``compare``), 2 the run itself was misconfigured.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from evals.dataset import DatasetError
from evals.runner import DRIVERS, RunConfig, RunnerError, run_evals


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m evals", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="run eval cases and write a report")
    run.add_argument("--cases", default="evals/cases",
                     help="a case .toml file or a directory of them")
    run.add_argument("--case", default="", dest="case_filter",
                     help="run only this case id")
    run.add_argument("--driver", default="claude", choices=DRIVERS,
                     help="claude = real model (subscription); scripted = playback")
    run.add_argument("--model", default="", help="model override (claude driver)")
    run.add_argument("--effort", default="",
                     choices=("", "low", "medium", "high", "xhigh", "max"),
                     help="reasoning effort (claude driver)")
    run.add_argument("--trials", default=0, type=int, dest="trials_override",
                     help="override every case's trial count")
    run.add_argument("--judge", action="store_true",
                     help="run the LLM judge over cases that define a rubric")
    run.add_argument("--label", default="", help="suffix for the run directory name")
    run.add_argument("--out", default="evals/runs", dest="out_root",
                     help="where run directories are created")
    sub.add_parser("compare", help="diff two runs' results.json").add_argument(
        "runs", nargs=2, help="baseline and candidate run directories"
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    try:
        if args.command == "run":
            result = asyncio.run(run_evals(RunConfig(
                cases=args.cases,
                case_filter=args.case_filter,
                driver=args.driver,
                model=args.model,
                effort=args.effort,
                trials_override=args.trials_override,
                judge=args.judge,
                label=args.label,
                out_root=args.out_root,
            )))
            print((result.run_dir / "report.md").read_text(encoding="utf-8"))
            return 0 if result.ok else 1
        from evals.compare import compare_runs  # phase 3; import here keeps run lean

        report, regressed = compare_runs(*args.runs)
        print(report)
        return 1 if regressed else 0
    except (DatasetError, RunnerError, FileNotFoundError) as err:
        print(f"error: {err}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
