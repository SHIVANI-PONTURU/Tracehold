"""Phase 1 step 8: the data quality report, rebuilt from the stages' outputs.

Writes work/reports/data_quality.md. It reads work/clean/stats.json, work/reports/validation_raw.csv,
the evaluation set, and the graph and engine reports when they exist. Totals must reconcile or it stops.

  python -m data.report
"""
import argparse
import json
import os
import time

import pandas as pd

from data import ibm, settings


def rupees(paise):
    r = paise / 100
    if r >= 1e7:
        return f"₹{r / 1e7:,.2f} crore"
    if r >= 1e5:
        return f"₹{r / 1e5:,.2f} lakh"
    return f"₹{r:,.0f}"


def count_lines(path):
    with open(path, "rb") as f:
        return max(sum(1 for _ in f) - 1, 0)


def load_json(path):
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def run():
    stats = load_json(os.path.join(settings.CLEAN_DIR, "stats.json"))
    if stats is None:
        raise FileNotFoundError("work/clean/stats.json is missing: run the clean stage first")
    dropped = sum(r["rows"] for r in stats["rules"] if r["effect"] == "dropped")
    if dropped != stats["rows_dropped"] or stats["rows_in"] - dropped != stats["rows_out"]:
        raise RuntimeError("cleaning totals do not reconcile")
    L = ["# TraceHold data quality report\n",
         f"Built {ibm.fmt_ist(time.time())}. Rows in {stats['rows_in']:,}, dropped {stats['rows_dropped']:,}, "
         f"out {stats['rows_out']:,}; the totals reconcile across every rule.\n"]

    L.append("## Sources\n\n| File | SHA-256 |\n| --- | --- |")
    raw = settings.raw_paths()
    for k, v in stats["inputs_sha256"].items():
        L.append(f"| `{os.path.basename(raw[k])}` | `{v}` |")

    L.append("\n## Validation of the raw files\n")
    vs = load_json(os.path.join(settings.REPORT_DIR, "validation_summary.json"))
    if vs:
        L.append("Every row was read: " + "; ".join(f"`{k}` {v['rows_read']:,} rows read of {v['lines_in_file']:,} "
                                                    f"lines" for k, v in vs["row_counts"].items())
                 + f"; `HI-Small_Patterns.txt` {vs['attempts']:,} attempts with {vs['pattern_rows']:,} transfer lines.\n")
    vpath = os.path.join(settings.REPORT_DIR, "validation_raw.csv")
    if os.path.exists(vpath):
        v = pd.read_csv(vpath, dtype=str, keep_default_na=False)
        if len(v):
            L.append("| File | Rule | Column | Count | Example rows |\n| --- | --- | --- | --- | --- |")
            for r in v.itertuples(index=False):
                L.append(f"| `{r.file}` | {r.rule} | {r.column} | {int(r.count):,} | {r.example_rows} |")
        else:
            L.append("No violations in any of the three files.")
    else:
        L.append("Not run yet: `python -m data.validate`.")

    L.append("\n## Cleaning rules\n\n| Code | Rule | Effect | Rows |\n| --- | --- | --- | --- |")
    for r in stats["rules"]:
        L.append(f"| {r['code']} | {r['rule']} | {r['effect']} | {r['rows']:,} |")
    if stats.get("schema_checks"):
        L.append(f"\nThe cleaned table passes {' and '.join(stats['schema_checks'])} with zero violations.")
    sl = f"first {stats['slice_days']} days" if stats["slice_days"] else "whole file (no slice)"
    L.append(f"\nSlice: {sl}. Data runs from {stats['data_range_ist'][0]} to {stats['data_range_ist'][1]}. "
             f"{stats['own_account_transfers']:,} transfers are between two accounts of the same owner "
             f"(`own_account`); {stats['duplicate_groups']:,} groups of exact duplicates are kept.\n")

    L.append(f"## Money\n\nRupees per US dollar: {stats['inr_per_usd']:.4f} (IBM's own Rupee rate). "
             f"{stats['cross_currency_transfers']:,} transfers change currency; "
             f"{next(r['rows'] for r in stats['rules'] if r['code'] == 'C04'):,} of them convert more than the "
             f"tolerance apart.\n")
    total = stats["total_value_paise"]
    L.append("| Paid in | Transfers | Share of value |\n| --- | --- | --- |")
    for c in stats["currency_mix"]:
        L.append(f"| {c['currency']} | {c['transfers']:,} | {c['value_paise'] / total:.1%} |")
    L.append("\n| Payment format | Transfers |\n| --- | --- |")
    for k, n in stats["payment_formats"].items():
        L.append(f"| {k} | {n:,} |")

    L.append(f"\n## Banks, owners and accounts\n\n{stats['banks']:,} banks, {stats['owners']:,} owners and "
             f"{stats['accounts_in_accounts_file']:,} accounts in `HI-Small_accounts.csv`; "
             f"{stats['accounts_in_transfers']:,} accounts appear in the transfers.\n")
    L.append("| Bank ID | Accounts |\n| --- | --- |")
    for b in stats["top_banks_by_accounts"][:10]:
        L.append(f"| {b['bank_id']} | {b['accounts']:,} |")

    lt = stats["laundering_transfers"]
    L.append(f"\n## Laundering in IBM's data\n\n{lt:,} transfers are laundering-labelled "
             f"({lt / stats['rows_out']:.3%} of transfers, {stats['laundering_value_paise'] / total:.3%} of value). "
             f"{stats['labelled_outside_patterns']:,} of them ({stats['labelled_outside_patterns'] / max(lt, 1):.0%}) are "
             f"not in any attempt in the patterns file, so labels come from the transfer flag. "
             f"{stats['pattern_rows_matched']:,} of {stats['pattern_rows']:,} pattern rows matched a transfer.\n")
    L.append("| Pattern | Attempts |\n| --- | --- |")
    for k, n in stats["attempts_by_type"].items():
        L.append(f"| {k} | {n} |")
    a = stats["attempt_size"]
    L.append(f"\nTransfers per attempt: min {a['min']}, median {a['median']:.0f}, max {a['max']}.\n")

    lab_path = os.path.join(settings.LABELS_DIR, "account_labels.csv")
    truth_path = os.path.join(settings.LABELS_DIR, "complaints_truth.csv")
    if os.path.exists(lab_path) and os.path.exists(truth_path):
        al = pd.read_csv(lab_path, usecols=["label", "laundering_share", "is_victim"])
        t = pd.read_csv(truth_path)
        L.append("## Ground truth (evaluation set)\n")
        L.append("| Mule threshold | Accounts labelled mule |\n| --- | --- |")
        for thr in (0.3, 0.5, 0.7):
            L.append(f"| over {thr:.0%} | {int((al['laundering_share'] > thr).sum()):,} |")
        L.append(f"\n{len(t)} complaints: {int((t['split'] == 'dev').sum())} dev, {int((t['split'] == 'test').sum())} test.\n")
        L.append("| Depth (trail) | Complaints |\n| --- | --- |")
        for d, n in t["depth_trail"].value_counts().sort_index().items():
            L.append(f"| {d} | {n} |")
        L.append("\n| Depth bucket | Dev | Test |\n| --- | --- | --- |")
        for b in ("shallow", "medium", "deep"):
            g = t[t["depth_bucket"] == b]
            L.append(f"| {b} | {int((g['split'] == 'dev').sum())} | {int((g['split'] == 'test').sum())} |")
        L.append(f"\nDisputed amounts: median {rupees(t['disputed_amount_paise'].median())}, "
                 f"largest {rupees(t['disputed_amount_paise'].max())}. Details in `eval/REPORT.md`.\n")

    L.append("## Synthetic overlay\n\n| File | Rows |\n| --- | --- |")
    for f in sorted(os.listdir(settings.OVERLAY_OUTPUT_DIR)):
        if f.endswith(".csv"):
            L.append(f"| `synthetic/output/{f}` | {count_lines(os.path.join(settings.OVERLAY_OUTPUT_DIR, f)):,} |")
    L.append("\nRates and checks in `synthetic/REPORT.md`.\n")

    eng = load_json(os.path.join(settings.REPORT_DIR, "engine_check.json"))
    L.append("## FalkorDB engine check\n")
    if eng:
        L.append(f"Image `{eng.get('image', '?')}`. {eng['passed']} of {eng['total']} checks pass.\n")
        L.append("| Check | Result | Detail |\n| --- | --- | --- |")
        for c in eng["checks"]:
            L.append(f"| {c['check']} | {c['result']} | {c['detail']} |")
    else:
        L.append("Not run yet: `python -m graph.engine_check`.")

    gl = load_json(os.path.join(settings.REPORT_DIR, "graph_load.json"))
    L.append("\n## Network graph\n")
    if gl:
        took = f"{gl['seconds'] / 60:.1f} minutes" if gl.get('seconds') else "an earlier run"
        L.append(f"Loaded ({gl.get('mode', '?')}) in {took}; memory "
                 f"{gl.get('memory_summary', 'not reported')}. {gl['passed']} of {gl['total']} checks pass.\n")
        L.append("| Element | In the graph | Expected |\n| --- | --- | --- |")
        for k, v in gl["counts"].items():
            L.append(f"| {k} | {v['graph']:,} | {v['expected']:,} |")
        L.append("\n| Check | Result | Detail |\n| --- | --- | --- |")
        for c in gl["checks"]:
            L.append(f"| {c['check']} | {c['result']} | {c['detail']} |")
    else:
        L.append("Not loaded yet: `python -m graph.load --rebuild`.")

    out = os.path.join(settings.REPORT_DIR, "data_quality.md")
    settings.ensure_dirs(settings.REPORT_DIR)
    with open(out, "w", encoding="utf-8") as f:
        f.write("\n".join(L) + "\n")
    ibm.log(f"quality report -> {os.path.relpath(out, settings.ROOT)}")
    return out


def main():
    argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter).parse_args()
    run()


if __name__ == "__main__":
    main()
