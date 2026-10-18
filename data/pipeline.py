"""TraceHold Phase 1 data pipeline: one command, named stages.

Data stages (no database needed):
  python -m data.pipeline all             validate, clean, overlay, eval, report (about 3 minutes)
  python -m data.pipeline validate        check IBM's raw files (work/reports/validation_raw.csv)
  python -m data.pipeline clean [--days N]   cleaned transfers (work/clean/)
  python -m data.pipeline overlay         synthetic overlay (synthetic/output/)
  python -m data.pipeline eval            evaluation set (eval/labels/)
  python -m data.pipeline report          quality report (work/reports/data_quality.md)

Graph stages (FalkorDB must be running: powershell -ExecutionPolicy Bypass -File deploy\\falkordb.ps1 start):
  python -m data.pipeline graph           engine check, load the network graph, report, snapshot (10-20 minutes)
  python -m data.pipeline engine          python -m graph.engine_check
  python -m data.pipeline load            python -m graph.load --rebuild
  python -m data.pipeline snapshot        python -m data.snapshot save
  python -m data.pipeline restore [--force] [--rebuild]   python -m data.snapshot restore

The implementation plan's other stage names map here: ingest is part of validate and clean, complaints
are made by overlay, and labels by eval.
"""
import argparse
import os
import subprocess
import sys
import time

from data import clean, ibm, report, settings, validate


def run_script(path):
    ibm.log(f"running {os.path.relpath(path, settings.ROOT)}")
    subprocess.run([sys.executable, path], cwd=os.path.dirname(path), check=True)


def engine(a):
    from graph import engine_check
    r = engine_check.run()
    if r["passed"] != r["total"]:
        raise SystemExit(f"engine check: {r['total'] - r['passed']} of {r['total']} checks failed")


def load(a):
    from graph import load as loader
    args = argparse.Namespace(rebuild=True, batch_size=a.batch_size, limit=None, days=a.days, dry_run=False,
                              verify_only=False)
    r = loader.run(args)
    if r["passed"] != r["total"]:
        raise SystemExit(f"graph load: {r['total'] - r['passed']} of {r['total']} checks failed")


def snapshot(a):
    from data import snapshot as snap
    snap.save()


def restore(a):
    from data import snapshot as snap
    if snap.restore(a.force, a.rebuild)["result"] != "PASS":
        raise SystemExit("restore: counts do not match the snapshot manifest")


STAGES = {
    "validate": lambda a: validate.run(),
    "clean": lambda a: clean.run(a.days),
    "overlay": lambda a: run_script(os.path.join(settings.SYNTHETIC_DIR, "generate_overlay.py")),
    "eval": lambda a: run_script(os.path.join(settings.EVAL_DIR, "build_eval_set.py")),
    "report": lambda a: report.run(),
    "engine": engine,
    "load": load,
    "snapshot": snapshot,
    "restore": restore,
}
GROUPS = {
    "all": ["validate", "clean", "overlay", "eval", "report"],
    "graph": ["engine", "load", "report", "snapshot"],
}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("stage", choices=list(STAGES) + list(GROUPS))
    ap.add_argument("--days", type=int, default=None, help="clean/load: keep only the first N days")
    ap.add_argument("--batch-size", type=int, default=10_000, help="load: rows per UNWIND batch")
    ap.add_argument("--force", action="store_true", help="restore: replace an existing `network` graph")
    ap.add_argument("--rebuild", action="store_true", help="restore: rebuild with the loader instead of RESTORE")
    args = ap.parse_args()
    t0 = time.time()
    for name in GROUPS.get(args.stage, [args.stage]):
        ibm.log(f"=== stage: {name}")
        STAGES[name](args)
    ibm.log(f"pipeline finished in {time.time() - t0:.0f} s")


if __name__ == "__main__":
    main()
