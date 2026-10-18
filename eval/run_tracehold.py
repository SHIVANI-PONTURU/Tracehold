"""Phase 4 step 5: TraceHold's live runs on the evaluation sample (Claude, through the three sandboxed agents).

  python -m eval.run_tracehold banks       the TRACEHOLD_BANK_IDS the workers need (the demo banks + the sample's)
  python -m eval.run_tracehold start       start the sample's investigations at each victim's bank (skips started)
  python -m eval.run_tracehold status      how many are at the gate, running or failed
  python -m eval.run_tracehold collect     save each run's trail, proposal, verdict, spend and time for scoring
  python -m eval.run_tracehold retry       start a new run for each failed one (the failed run is kept as `previous`)

The sample (eval/methods.sample, fixed seed, the user's pick on 17 Oct): 12 complaints from each depth bucket of the
test split and 10 of the held-out pattern (CYCLE), 46 runs at about $0.3 each. Each complaint is investigated by its
victim's bank, so the workers must serve those banks: restart the sandboxes with the `banks` output first, e.g.

  export TRACEHOLD_BANK_IDS=$(python -m eval.run_tracehold banks)
  bash deploy/sandboxes.sh stop && bash deploy/sandboxes.sh start

The gate stays in placeholder mode and nobody decides it: scoring takes the liens the Verifier approved, which is
what the gate would execute without a human override. Runs are recorded in work/reports/evaluation/tracehold_runs.json.
"""
from __future__ import annotations

import argparse
import json
import os
import time

from agents.common import clients
from agents.common.config import load
from eval import methods as M
from eval import truth as TR
from eval.run_eval import OUT_DIR, TH_DIR, run_name

RUNS = os.path.join(OUT_DIR, "tracehold_runs.json")
SAMPLE, TAG = "a", "v1"                    # set by --sample and --tag
DEMO_BANKS = ["11", "20", "12"]
USD_PER_RUN = 0.30


def _runs_path():
    return os.path.join(OUT_DIR, f"tracehold_runs{run_name(SAMPLE, TAG)}.json")


def _runs():
    if os.path.exists(_runs_path()):
        with open(_runs_path(), encoding="utf-8") as f:
            return json.load(f)
    return {}


def _save(runs):
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(_runs_path(), "w", encoding="utf-8") as f:
        json.dump(runs, f, indent=1)


def banks(truth=None):
    truth = truth or TR.complaints()
    extra = sorted({str(truth[c]["victim_bank_id"]) for s in ("a", "b") for c in M.sample_set(truth, s)}
                   - set(DEMO_BANKS), key=int)
    return DEMO_BANKS + extra


def start(base_url, dry_run=False):
    truth = TR.complaints()
    runs = _runs()
    todo = [c for c in M.sample_set(truth, SAMPLE) if c not in runs]
    print(f"{len(todo)} to start ({len(runs)} already started), about ${USD_PER_RUN * len(todo):.2f}")
    for cid in todo:
        bank = str(truth[cid]["victim_bank_id"])
        if dry_run:
            print(cid, bank)
            continue
        r = clients.HandoffClient(base_url, bank).start(cid)
        runs[cid] = {"bank_id": bank, "investigation_id": r["investigation_id"], "started": int(time.time())}
        _save(runs)
        print(cid, bank, r["investigation_id"])
    return runs


def retry(base_url):
    """A new investigation for each run that failed (after a fix); the failed one stays on record."""
    runs = _runs()
    for cid, r in sorted(runs.items()):
        h = clients.HandoffClient(base_url, r["bank_id"])
        if h.detail(r["investigation_id"])["investigation"]["status"] != "failed":
            continue
        new = h.start(cid)["investigation_id"]
        runs[cid] = {**r, "investigation_id": new, "started": int(time.time()),
                     "previous": r.get("previous", []) + [r["investigation_id"]]}
        _save(runs)
        print(cid, r["bank_id"], "retried as", new, "after", r["investigation_id"])
    return runs


def status(base_url):
    out = {}
    for cid, r in sorted(_runs().items()):
        inv = clients.HandoffClient(base_url, r["bank_id"]).detail(r["investigation_id"])["investigation"]
        out.setdefault(inv["status"], []).append(cid)
        if inv["status"] == "failed":
            print(cid, "failed:", inv.get("failure") or "")
    print({k: len(v) for k, v in out.items()})
    return out


def _latest(handoffs, kind):
    hs = [h for h in handoffs if h["kind"] == kind]
    return hs[-1]["payload"] if hs else None


def seconds_of(steps):
    """Wall time from the first agent step to the end of the last (the gate excluded)."""
    s = [x for x in steps if x.get("agent") in ("tracer", "adjudicator", "verifier") and x.get("started_ms")]
    if not s:
        return None
    end = max(x["started_ms"] + (x.get("latency_ms") or 0) for x in s)
    return round((end - min(x["started_ms"] for x in s)) / 1000, 1)


def collect(base_url):
    out = TH_DIR + run_name(SAMPLE, TAG)
    os.makedirs(out, exist_ok=True)
    n = 0
    for cid, r in sorted(_runs().items()):
        h = clients.HandoffClient(base_url, r["bank_id"])
        d = h.detail(r["investigation_id"])
        st = d["investigation"]["status"]
        if st not in ("at_gate", "executed", "closed", "failed"):
            print(cid, "still", st)
            continue
        spend = h.spend(r["investigation_id"])
        trace = _latest(d["handoffs"], "trace") or {}
        rec = {"complaint_id": cid, "bank_id": r["bank_id"], "investigation_id": r["investigation_id"], "status": st,
               "previous": r.get("previous", []),
               "failed_reason": d["investigation"].get("failure"),
               "accounts": [{k: a.get(k) for k in ("account_id", "layer", "tainted_paise", "balance_paise", "branch_end")}
                            for a in trace.get("accounts", [])],
               "proposal": _latest(d["handoffs"], "proposal"), "verdict": _latest(d["handoffs"], "verdict"),
               "usd": spend.get("investigation", {}).get("usd"), "calls": spend.get("investigation", {}).get("calls"),
               "by_agent": spend.get("by_agent"), "seconds": seconds_of(d["steps"])}
        with open(os.path.join(out, f"{cid}.json"), "w", encoding="utf-8") as f:
            json.dump(rec, f)
        n += 1
    print(f"collected {n}")
    return n


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["banks", "start", "status", "collect", "retry"])
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--sample", choices=["a", "b"], default="a", help="a: the first sample; b: the second, disjoint")
    ap.add_argument("--tag", default="v1", help="rubric version these runs use (v1, v2); keeps their files apart")
    a = ap.parse_args()
    global SAMPLE, TAG
    SAMPLE, TAG = a.sample, a.tag
    base = load()["tool_server_url"]
    if a.cmd == "banks":
        print(",".join(banks()))
    elif a.cmd == "start":
        start(base, a.dry_run)
    elif a.cmd == "status":
        status(base)
    elif a.cmd == "retry":
        retry(base)
    else:
        collect(base)


if __name__ == "__main__":
    main()
