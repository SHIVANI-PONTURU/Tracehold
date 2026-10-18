"""Phase 4 step 5: the evaluation runner. Runs on the server, next to FalkorDB; spends no model money.

  python -m eval.run_eval trails [--split test|dev|all] [--workers 4] [--limit N] [--force]
  python -m eval.run_eval score

trails: for every complaint, the Tracer's own graph runs on the same tool functions the agents call (in this
process), with a stand-in model that follows every branch, so the trail is the one code alone finds; then the signal
record of each account a decision is made on. One JSON per complaint goes to work/reports/evaluation/raw/ (a rerun
skips complaints already there unless --force). Memory goes to test_eval_memory_<bank> graphs, deleted at the end, so the
banks' real memory is untouched.

score: chooses the rules-only thresholds on dev complaints (never test, never the held-out pattern), scores the four
methods per complaint (TraceHold from the runs `eval.run_tracehold collect` saved), aggregates by depth bucket and the
held-out pattern, over all test complaints and over TraceHold's sample, runs the overlap check over every complaint's
trail, and writes work/reports/evaluation/evaluation_<UTC time>.json. The Evaluation page reads the newest file.
"""
from __future__ import annotations

import argparse
import glob
import json
import logging
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from data import settings
from eval import methods as M
from eval import truth as TR

log = logging.getLogger("tracehold.eval")
OUT_DIR = os.path.join(settings.WORK_DIR, "reports", "evaluation")
RAW_DIR = os.path.join(OUT_DIR, "raw")
TH_DIR = os.path.join(OUT_DIR, "raw_tracehold")
ROW = re.compile(r"^([0-9]{1,6}-[0-9A-Z]{6,12}) \|", re.M)
EVAL_PREFIX = "test_eval_memory_"      # graph names must match graph/client.NAME_RE; tests delete only test_memory_*
KEEP = ("account_id", "layer", "tainted_paise", "balance_paise", "lien_amount_paise", "tainted_in_paise")


class FollowEvery:
    """Stands in for the model in the Tracer's judge step: follow every open branch (the baselines' trail)."""
    spent, tokens = 0.0, 0

    def decide(self, messages, schema, name="decide"):
        ids = ROW.findall(messages[-1][1])
        return schema.model_validate({"calls": [{"account_id": a, "decision": "follow",
                                                 "reason": "baseline: follow every branch"} for a in ids]})


class LocalTools:
    """The ToolClient interface (call(tool, **args)) over the tool functions in this process."""

    def __init__(self, ctx, bank_id, investigation_id):
        self.ctx, self.bank_id, self.investigation_id = ctx, str(bank_id), investigation_id

    def call(self, tool, **args):
        from mcp_server import tools as T
        return T.run_tool(T.READ_TOOLS[tool], self.ctx, self.bank_id, self.investigation_id, **args)


def trail_one(ctx, cfg, cid, row):
    """The code-only trail and signal records of one complaint (a dict ready to save)."""
    from agents.tracer import graph as tracer
    from mcp_server import tools as T
    from memory import store
    bank = str(row["victim_bank_id"])
    mem = ctx.memory(bank)
    inv = store.create_investigation(mem, cid)
    t0 = time.time()
    state = tracer.run(cfg, LocalTools(ctx, bank, inv), FollowEvery(), bank, inv, cid, store.trace_id_for(inv))
    store.add_handoff(mem, inv, "trace", "tracer", "adjudicator", state["handoff"])   # signal records read it
    accounts = [{k: (a.get(k) if a.get(k) is not None else 0) for k in KEEP} for a in state["taint"]["accounts"]]
    signals, errors = {}, {}
    for a in M.judged(accounts):
        s = T.run_tool(T.account_signals, ctx, bank, inv, a["account_id"])
        if "error" in s:
            errors[a["account_id"]] = s["error"]
        else:
            signals[a["account_id"]] = {k: v for k, v in s.items() if k not in ("prior_decisions",)}
    return {"complaint_id": cid, "bank_id": bank, "investigation_id": inv, "disputed_paise": row["disputed_amount_paise"],
            "reported_at": row["reported_at"], "victim_account_id": row["victim_account_id"], "accounts": accounts,
            "signals": signals, "signal_errors": errors,
            "unrecoverable_paise": state["taint"]["unrecoverable_paise"],
            "not_followed_paise": state["taint"]["not_followed_total_paise"],
            "rounds": state.get("rounds"), "seconds": round(time.time() - t0, 2), "error": None}


def trails(split="test", workers=4, limit=None, force=False, keep=False):
    from agents.common.config import load
    from graph import client, memory_graph
    from mcp_server import tools as T
    memory_graph.PREFIX = EVAL_PREFIX
    memory_graph._ready.clear()
    cfg = load()
    truth = TR.complaints()
    todo = [c for c, r in sorted(truth.items()) if split == "all" or r["split"] == split]
    os.makedirs(RAW_DIR, exist_ok=True)
    if not force:
        todo = [c for c in todo if not os.path.exists(os.path.join(RAW_DIR, f"{c}.json"))]
    todo = todo[:limit] if limit else todo
    ctx = T.Ctx()
    log.info("code-only trails for %d complaints, %d workers", len(todo), workers)

    def one(cid):
        try:
            rec = trail_one(ctx, cfg, cid, truth[cid])
        except Exception as e:                       # recorded; every method skips this complaint
            rec = {"complaint_id": cid, "error": f"{type(e).__name__}: {str(e)[:300]}"}
        with open(os.path.join(RAW_DIR, f"{cid}.json"), "w", encoding="utf-8") as f:
            json.dump(rec, f)
        return rec
    done = 0
    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for fut in as_completed([pool.submit(one, c) for c in todo]):
                rec = fut.result()
                done += 1
                log.info("%d/%d %s %s", done, len(todo), rec["complaint_id"],
                         rec["error"] or f"{len(rec['accounts'])} accounts, {rec['seconds']} s")
    finally:
        if not keep:
            for name in client.graph_names(ctx.db):
                if name.startswith(EVAL_PREFIX):
                    client.open_graph(ctx.db, name).delete()
    return done


def load_dir(path):
    out = {}
    for p in sorted(glob.glob(os.path.join(path, "*.json"))):
        with open(p, encoding="utf-8") as f:
            rec = json.load(f)
        out[rec["complaint_id"]] = rec
    return out


def label_table(cases, labels):
    """How a method's labels compare with IBM's, over the accounts it labelled: {method_label: {ibm_label: n}}."""
    out = {}
    for decisions in cases:
        for acc, d in decisions.items():
            if d.get("label"):
                row = out.setdefault(d["label"], {"mule": 0, "genuine": 0})
                row[TR.label_of(labels, acc)] += 1
    return out


def run_name(sample_name="a", tag="v1"):
    """Where a set of TraceHold runs is kept: the first sample under version 1 keeps the original names."""
    return "" if (sample_name, tag) == ("a", "v1") else f"_{sample_name}_{tag}"


def score(out_dir=OUT_DIR, sample_name="a", tag="v1"):
    from agents.common.config import load
    cfg = load()
    floor = int(cfg["adjudicator"]["likely_genuine_lien_floor_paise"])
    hold = cfg["adjudicator"].get("lien_whatever_label_share")
    hold_part = cfg["adjudicator"].get("lien_whatever_label_part_of_disputed") if tag != "v1" else None
    truth, labels, mules = TR.complaints(), TR.account_labels(), TR.trail_mules()
    raw = {c: r for c, r in load_dir(RAW_DIR).items() if not r.get("error")}
    failed_trails = sorted(c for c, r in load_dir(RAW_DIR).items() if r.get("error"))
    th_runs = load_dir(TH_DIR + run_name(sample_name, tag))
    sample = M.sample_set(truth, sample_name)
    dev = [r for c, r in raw.items() if truth[c]["split"] == "dev" and M.group_of(truth[c]) != "held_out"]
    th, f1 = M.calibrate(dev, labels, mules, floor, hold_share=hold, hold_part=hold_part)

    per_case, decisions_by_method = [], {m: [] for m in M.METHODS}
    for cid, rec in sorted(raw.items()):
        t = truth[cid]
        if t["split"] != "test":
            continue
        g, acc, mt = M.group_of(t), rec["accounts"], mules.get(cid, set())
        base = {"complaint_id": cid, "group": g, "pattern": t["pattern_type"], "depth": t["depth_trail"],
                "disputed": rec["disputed_paise"],      # once TraceHold has runs, the sample is the complaints it ran
                "in_sample": cid in sample and (not th_runs or cid in th_runs)}
        dec = {"full_freeze": M.full_freeze(acc), "greedy": M.greedy(acc, rec["disputed_paise"]),
               "rules_only": M.rules_only(acc, rec["signals"], floor, th, hold, rec["disputed_paise"], hold_part)}
        for m, d in dec.items():
            per_case.append({**base, "method": m, **M.score_case(d, acc, labels, mt)})
            decisions_by_method[m].append(d)
        run = th_runs.get(cid)
        if run:
            d = M.tracehold(run.get("proposal"), run.get("verdict")) if run.get("status") != "failed" else {}
            per_case.append({**base, "method": "tracehold", "status": run.get("status"), "usd": run.get("usd"),
                             "seconds": run.get("seconds"), **M.score_case(d, run.get("accounts", []), labels, mt)})
            decisions_by_method["tracehold"].append(d)

    rows = []
    for scope, keep in (("sample", lambda r: r["in_sample"]), ("all_test", lambda r: True)):
        for m in M.METHODS:
            for g in M.GROUPS + ("all_buckets",):
                sel = [r for r in per_case if r["method"] == m and keep(r)
                       and (r["group"] == g or (g == "all_buckets" and r["group"] != "held_out"))]
                if sel:
                    rows.append({"scope": scope, "method": m, "group": g, **M.aggregate(sel)})

    def pick(m, scope="sample", g="all_buckets"):
        return next((r for r in rows if r["scope"] == scope and r["method"] == m and r["group"] == g), None)
    ff, gr, th_row = pick("full_freeze"), pick("greedy"), pick("tracehold")
    headline = {}
    if th_row and ff and ff["innocent"]:
        headline = {"innocent_less_than_full_freeze": round(1 - th_row["innocent"] / ff["innocent"], 4),
                    "innocent_less_than_greedy": round(1 - th_row["innocent"] / gr["innocent"], 4) if gr["innocent"] else None,
                    "tracehold_recall": th_row["recall"], "tracehold_precision": th_row["precision"],
                    "complaints": th_row["complaints"]}
    everything = {c: r for c, r in raw.items()}
    for r in everything.values():
        for a in r["accounts"]:
            a["greedy_paise"] = min(a["balance_paise"], r["disputed_paise"])
    res = {"meta": {"written": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime()), "held_out_pattern": M.HELD_OUT,
                    "sample_name": sample_name, "rubric": tag,
                    "sample_seed": M.SEED if sample_name == "a" else M.SEED_B, "sample": sample, "lien_floor_paise": floor,
                    "trails": len(raw), "failed_trails": failed_trails,
                    "tracehold_runs": len(th_runs),
                    "tracehold_failed": sorted(c for c, r in th_runs.items() if r.get("status") == "failed")},
           "rules_thresholds": th, "rules_dev_f1": f1, "headline": headline, "by_group": rows,
           "overlap": M.overlap_check(everything),
           "overlap_greedy": M.overlap_check(everything, "greedy_paise"),
           "labels_vs_ibm": {m: label_table(decisions_by_method[m], labels) for m in ("rules_only", "tracehold")},
           "cases": per_case}
    lt = os.path.join(out_dir, "load_test.json")
    if os.path.exists(lt):
        with open(lt, encoding="utf-8") as f:
            res["load_test"] = json.load(f)
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"evaluation_{time.strftime('%Y%m%d_%H%M%S', time.gmtime())}{run_name(sample_name, tag)}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(res, f, indent=1)
    return path, res


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("trails")
    t.add_argument("--split", choices=["test", "dev", "all"], default="all")
    t.add_argument("--workers", type=int, default=4)
    t.add_argument("--limit", type=int)
    t.add_argument("--force", action="store_true")
    t.add_argument("--keep", action="store_true", help="keep the evaluation memory graphs")
    sc = sub.add_parser("score")
    sc.add_argument("--sample", choices=["a", "b"], default="a")
    sc.add_argument("--tag", default="v1", help="rubric version of the TraceHold runs (v1, v2)")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    if a.cmd == "trails":
        trails(a.split, a.workers, a.limit, a.force, a.keep)
    else:
        path, res = score(sample_name=a.sample, tag=a.tag)
        print(path)
        print(json.dumps({"headline": res["headline"], "rules_thresholds": res["rules_thresholds"],
                          "overlap": {k: res["overlap"][k] for k in ("overlapping_sets", "shared_accounts",
                                                                      "account_limit_holds", "complaint_limit_holds")}},
                         indent=1))


if __name__ == "__main__":
    main()
