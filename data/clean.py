"""Phase 1 step 7: clean IBM's transfers into the table the graph loader reads.

Every rule has a reason code, counted in work/clean/stats.json and the quality report:
  C01  bank codes normalised; account ID = "<bank code>-<account number>"            all rows changed
  C02  times read as IST wall-clock, stored as UTC epoch seconds                       all rows changed
  C03  amount in paise with the fixed rate table; one amount per transfer (paid side)  all rows changed
  C04  paid and received convert more than the tolerance apart                         kept, fx_mismatch
  C05  amount rounds to 0 paise                                                        dropped
  C06  exact duplicate rows (every field equal)                                         kept, dup_group
  C07  transfer from an account to itself                                               kept, self_loop
  C08  amount missing or not positive                                                   dropped
  C09  at or after the slice cutoff (--days)                                            dropped
own_account marks a transfer between two different accounts with the same owner (Entity ID).

Writes work/clean/transfers.(parquet|csv.gz), work/clean/dropped.csv and work/clean/stats.json.
The raw IBM file is not copied: transfer_id "T000000123" is data row 123 of HI-Small_Trans.csv.

  python -m data.clean            # whole file (recommended: dev complaints' trails run past day 5)
  python -m data.clean --days 5   # time-prefix slice
"""
import argparse
import json
import os
import time

import numpy as np
import pandas as pd

from data import clean_schema, contract, ibm, settings, store

ACCOUNT_RE = r"^\d+-[0-9A-F]+$"
OUT_COLUMNS = ["transfer_id", "from_account_id", "to_account_id", "amount_paise", "ts", "format",
               "self_loop", "own_account", "dup_group", "fx_mismatch"]


def check_clean(table, ts_min, ts_max):
    """The stricter schema for the cleaned table. Returns a list of problems (empty = pass)."""
    problems = []
    if list(table.columns) != OUT_COLUMNS:
        problems.append(f"columns are {list(table.columns)}, expected {OUT_COLUMNS}")
    if table.isna().any().any():
        problems.append(f"empty values in {list(table.columns[table.isna().any()])}")
    if not table["transfer_id"].is_unique:
        problems.append("transfer IDs are not unique")
    for col in ("from_account_id", "to_account_id"):
        bad = int((~table[col].str.match(ACCOUNT_RE)).sum())
        if bad:
            problems.append(f"{bad} {col} values are not <bank>-<account>")
    if not pd.api.types.is_integer_dtype(table["amount_paise"]) or int((table["amount_paise"] < 1).sum()):
        problems.append("amount_paise must be integers of at least 1")
    if not pd.api.types.is_integer_dtype(table["ts"]) or not table["ts"].between(ts_min, ts_max).all():
        problems.append("ts must be integer epoch seconds inside the data's time range")
    bad_cols = contract.forbidden(table.columns)
    if bad_cols:
        problems.append(f"ground-truth columns present: {bad_cols}")
    return problems


def run(days=None):
    t0 = time.time()
    settings.ensure_dirs(settings.CLEAN_DIR)
    raw = settings.raw_paths()
    ocfg = settings.overlay_config()
    offset = settings.time_offset_minutes()

    ibm.log("reading IBM transfers")
    tx, reg, cur_names, fmt_names = ibm.read_transactions(raw["transactions"], None, offset)
    n_in = len(tx["row"])
    af = pd.read_csv(raw["accounts"], dtype=str)
    af["bank_int"] = af["Bank ID"].astype(int)
    af["idx"] = [reg.get(b, a) for b, a in zip(af["bank_int"], af["Account Number"])]
    owner = np.full(len(reg.ids), -1, dtype=np.int64)
    owner[af["idx"].to_numpy()] = pd.factorize(af["Entity ID"])[0]

    # C03 rate table: derived from IBM's own cross-currency transfers; must equal the overlay's table
    fx, inr_per_usd = ibm.derive_rates(tx, cur_names, ocfg["currency"]["usd_name"], ocfg["currency"]["rupee_name"])
    saved_path = os.path.join(settings.SYNTHETIC_DIR, "fx_rates.csv")
    if os.path.exists(saved_path):
        saved = (pd.read_csv(saved_path, float_precision="round_trip")      # exact floats, not the fast parser
                 .set_index("currency")["paise_per_unit"].sort_index())
        mine = fx.set_index("currency")["paise_per_unit"].sort_index()
        if not (saved.index.equals(mine.index) and np.allclose(saved.to_numpy(), mine.to_numpy(), rtol=1e-12, atol=0)):
            raise RuntimeError("the rate table differs from synthetic/fx_rates.csv: rerun the overlay stage")
    ppu = fx.set_index("currency").loc[cur_names, "paise_per_unit"].to_numpy()
    src, dst, ts, row = tx["src"], tx["dst"], tx["ts"], tx["row"]
    paid_p = np.floor(tx["paid"] * ppu[tx["pcur"]] + 0.5)
    recv_p = tx["recv"] * ppu[tx["rcur"]]
    if np.nanmax(paid_p) >= 9.0e18:
        raise OverflowError("a transfer amount does not fit in int64 paise")

    tol = float(ocfg["currency"]["mismatch_tolerance"])
    cross = tx["pcur"] != tx["rcur"]
    bad_amount = ~(tx["paid"] > 0) | ~(tx["recv"] > 0)                                    # C08
    zero = ~bad_amount & (paid_p < 1)                                                      # C05
    fx_mismatch = cross & (np.abs(recv_p - paid_p) > tol * np.maximum(paid_p, 1))          # C04
    self_loop = src == dst                                                                 # C07
    own_account = ~self_loop & (owner[src] >= 0) & (owner[src] == owner[dst])

    ibm.log("finding exact duplicates")                                                   # C06
    dcols = ["ts", "src", "dst", "paid", "pcur", "recv", "rcur", "fmt", "lab"]
    dkey = pd.DataFrame({k: tx[k] for k in dcols})
    dup = dkey.duplicated(keep=False).to_numpy()
    dup_group = np.full(n_in, -1, dtype=np.int64)
    if dup.any():
        sub = dkey[dup].copy()
        sub["row"] = row[dup]
        dup_group[dup] = sub.groupby(dcols, sort=False)["row"].transform("min").to_numpy()
    del dkey

    ts_min, ts_max = int(ts.min()), int(ts.max())
    cutoff = ts_min + int(days) * 86400 if days else None
    out_of_slice = (ts >= cutoff) if cutoff is not None else np.zeros(n_in, dtype=bool)   # C09
    drop = bad_amount | zero | out_of_slice
    reason = np.where(bad_amount, "C08", np.where(zero, "C05", "C09"))
    keep = ~drop

    order = np.lexsort((row[keep], ts[keep]))
    k_rows = row[keep][order]
    ids = np.array(reg.ids, dtype=object)
    fmt_arr = np.array(fmt_names, dtype=object)
    table = pd.DataFrame({
        "transfer_id": [ibm.transfer_id(r) for r in k_rows],
        "from_account_id": ids[src[keep][order]],
        "to_account_id": ids[dst[keep][order]],
        "amount_paise": paid_p[keep][order].astype(np.int64),
        "ts": ts[keep][order],
        "format": fmt_arr[tx["fmt"][keep][order]],
        "self_loop": self_loop[keep][order],
        "own_account": own_account[keep][order],
        "dup_group": dup_group[keep][order],
        "fx_mismatch": fx_mismatch[keep][order],
    })
    ibm.log("checking the cleaned table against the strict schema")
    problems = check_clean(table, ts_min, ts_max)
    pandera_problems = clean_schema.problems(table, ts_min, ts_max)
    problems += pandera_problems or []
    schema_checks = ["pandas rules (clean.check_clean)"]
    if pandera_problems is not None:
        schema_checks.append(f"pandera {clean_schema.version()} schema (data/clean_schema.py)")
    if problems:
        raise RuntimeError("cleaned table fails the schema: " + "; ".join(problems))

    ibm.log("writing the cleaned table")
    out_path = store.write_table(table, os.path.join(settings.CLEAN_DIR, "transfers"))
    dropped = pd.DataFrame({"transfer_id": [ibm.transfer_id(r) for r in row[drop]], "reason": reason[drop]})
    dropped.to_csv(os.path.join(settings.CLEAN_DIR, "dropped.csv"), index=False, lineterminator="\n")

    # ---- facts for the quality report -----------------------------------------------------
    attempts, prows = ibm.read_patterns(raw["patterns"])
    pm = ibm.match_patterns(prows, tx, reg, cur_names, fmt_names, offset)
    lab = tx["lab"].astype(bool)
    in_pattern = np.zeros(n_in, dtype=bool)
    in_pattern[pm.loc[pm["row"] >= 0, "row"].to_numpy()] = True
    pay_cur = np.array(cur_names, dtype=object)[tx["pcur"]]
    cur_mix = (pd.DataFrame({"currency": pay_cur[keep], "paise": paid_p[keep]})
               .groupby("currency")["paise"].agg(["size", "sum"]).sort_values("size", ascending=False))
    banks = af.groupby("Bank ID").size().sort_values(ascending=False)
    rules = [
        ("C01", "Normalise bank codes; build account IDs", "changed", n_in),
        ("C02", "Timestamps read as IST, stored as UTC epoch seconds", "changed", n_in),
        ("C03", "Convert to paise with the fixed rate table (paid side)", "changed", n_in),
        ("C04", "Paid and received convert more than the tolerance apart", "kept, fx_mismatch", int(fx_mismatch.sum())),
        ("C05", "Rounds to 0 paise", "dropped", int((drop & (reason == "C05")).sum())),
        ("C06", "Exact duplicate rows", "kept, dup_group", int(dup.sum())),
        ("C07", "Transfer from an account to itself", "kept, self_loop", int(self_loop.sum())),
        ("C08", "Amount missing or not positive", "dropped", int((drop & (reason == "C08")).sum())),
        ("C09", "At or after the slice cutoff", "dropped", int((drop & (reason == "C09")).sum())),
    ]
    n_dropped = int(drop.sum())
    if n_in - n_dropped != len(table):
        raise RuntimeError("rows in minus rows dropped does not equal rows out")
    stats = {
        "rows_in": n_in, "rows_out": len(table), "rows_dropped": n_dropped,
        "schema_checks": schema_checks, "schema_violations": 0,
        "slice_days": days, "cutoff_ts": cutoff, "ts_min": ts_min, "ts_max": ts_max,
        "data_range_ist": [ibm.fmt_ist(ts_min), ibm.fmt_ist(ts_max)],
        "rules": [{"code": c, "rule": r, "effect": e, "rows": n} for c, r, e, n in rules],
        "own_account_transfers": int(own_account[keep].sum()),
        "duplicate_groups": int(len(np.unique(dup_group[dup]))) if dup.any() else 0,
        "cross_currency_transfers": int(cross.sum()),
        "inr_per_usd": inr_per_usd,
        "currency_mix": [{"currency": c, "transfers": int(r["size"]), "value_paise": int(r["sum"])}
                         for c, r in cur_mix.iterrows()],
        "payment_formats": {k: int(v) for k, v in pd.Series(np.array(fmt_names, dtype=object)[tx["fmt"][keep]])
                            .value_counts().items()},
        "accounts_in_accounts_file": int(len(af)), "accounts_in_transfers": int(len(np.unique(np.concatenate([src, dst])))),
        "banks": int(len(banks)), "owners": int(af["Entity ID"].nunique()),
        "top_banks_by_accounts": [{"bank_id": str(int(b)), "accounts": int(n)} for b, n in banks.head(20).items()],
        "laundering_transfers": int(lab[keep].sum()),
        "laundering_value_paise": int(paid_p[keep & lab].sum()),
        "total_value_paise": int(paid_p[keep].sum()),
        "attempts": int(len(attempts)), "pattern_rows": int(len(pm)),
        "pattern_rows_matched": int((pm["status"] != "unmatched").sum()),
        "attempts_by_type": {k: int(v) for k, v in attempts["pattern_type"].value_counts().items()},
        "attempt_size": {"min": int(pm.groupby("attempt_id").size().min()),
                         "median": float(pm.groupby("attempt_id").size().median()),
                         "max": int(pm.groupby("attempt_id").size().max())},
        "labelled_outside_patterns": int((lab & ~in_pattern).sum()),
        "output": os.path.relpath(out_path, settings.ROOT).replace(os.sep, "/"),
        "output_sha256": ibm.sha256_file(out_path),
        "inputs_sha256": {k: ibm.sha256_file(v) for k, v in raw.items()},
        "seconds": round(time.time() - t0, 1),
    }
    with open(os.path.join(settings.CLEAN_DIR, "stats.json"), "w", encoding="utf-8") as f:
        json.dump(stats, f, indent=2)
    ibm.log(f"clean done in {time.time() - t0:.0f} s: {n_in:,} in, {n_dropped:,} dropped, {len(table):,} out "
            f"-> {stats['output']}")
    return stats


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=None, help="keep only transfers in the first N days")
    run(ap.parse_args().days)


if __name__ == "__main__":
    main()
