#!/usr/bin/env python3
"""TraceHold overlay v1: synthetic data generator.

Builds the synthetic overlay on top of IBM's HI-Small files, following the
implementation plan (Phase 1 steps 10-11, Appendix C step 5).

Writes only what is not already in IBM's three files (banks, owners and owner
types stay in HI-Small_accounts.csv; attempts stay in HI-Small_Patterns.txt).

Generated : account opening dates, opening-balance buffers, devices, phones,
            IPs, household and ring sharing, complaint report times.
Calculated: minimum feasible opening balances, registered merchants, demo
            banks, complaints (from IBM's attempts).

Writes, next to the config file:
  output/       data the graph loader reads together with IBM's files (no labels)
  ../eval/labels/ring_devices.csv  ground truth for the evaluation set
  fx_rates.csv  the rate table the balances were computed with
  REPORT.md     counts, measured rates, checks and file fingerprints

Everything random comes from the config's seed, so the same seed and inputs
give byte-identical files.

Usage:
  python3 generate_overlay.py
  python3 generate_overlay.py --limit-rows 300000 --out-root /tmp/overlay_test
"""
import argparse
import math
import os
import sys
import time

import numpy as np
import pandas as pd
import yaml

HERE = os.path.dirname(os.path.abspath(__file__))

sys.path.insert(0, os.path.dirname(HERE))         # project root, for the shared data package
from data.contract import FORBIDDEN_COLUMN_WORDS    # noqa: E402
from data.ibm import (fmt_ist, log, match_patterns, read_patterns,  # noqa: E402
                      read_transactions, derive_rates, sha256_file)

def account_flows(tx, n_acc):
    """Per account: first transfer time, minimum feasible opening balance, typical payment, distinct payers."""
    src, dst, ts, amt = tx["src"], tx["dst"], tx["ts"], tx["amt"]
    first = pd.Series(np.concatenate([ts, ts])).groupby(np.concatenate([src, dst])).min()
    first_ts = np.full(n_acc, -1, dtype=np.int64)
    first_ts[first.index.to_numpy()] = first.to_numpy()
    keep = src != dst                                       # same-account transfers move no money
    s, d, t, a = src[keep], dst[keep], ts[keep], amt[keep]
    k = len(s)
    ev_acct = np.concatenate([s, d])
    ev_ts = np.concatenate([t, t])
    ev_in = np.concatenate([np.zeros(k, np.int8), np.ones(k, np.int8)])   # outflows first within a minute
    ev_amt = np.concatenate([-a, a])
    order = np.lexsort((ev_in, ev_ts, ev_acct))
    ev = pd.DataFrame({"acct": ev_acct[order], "delta": ev_amt[order]})
    del order, ev_ts, ev_in
    gross = pd.Series(np.abs(ev["delta"].to_numpy()).astype(np.float64)).groupby(ev["acct"].to_numpy()).sum()
    if len(gross) and gross.max() >= 9.0e18:
        raise OverflowError("an account's total flow does not fit in int64 paise")
    run = ev.groupby("acct", sort=False)["delta"].cumsum()
    min_run = run.groupby(ev["acct"].to_numpy()).min()
    min_run_arr = np.zeros(n_acc, dtype=np.int64)
    min_run_arr[min_run.index.to_numpy()] = min_run.to_numpy()
    min_feasible = np.maximum(0, -min_run_arr)
    typical = pd.Series(np.abs(ev["delta"].to_numpy())).groupby(ev["acct"].to_numpy()).median()
    typ = np.full(n_acc, np.nan)
    typ[typical.index.to_numpy()] = typical.to_numpy()
    pay_keys = np.unique((d.astype(np.int64) << 32) | s.astype(np.int64))
    payers = np.bincount((pay_keys >> 32).astype(np.int64), minlength=n_acc)
    return first_ts, min_feasible, min_run_arr, typ, payers


def first_seen(rng, opened, first):
    lo = opened.astype(np.int64)
    hi = np.maximum(first.astype(np.int64), lo + 1)
    return lo + np.floor(rng.random(len(lo)) * (hi - lo)).astype(np.int64)


def write_csv(df, path):
    df.to_csv(path, index=False, lineterminator="\n")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=os.path.join(HERE, "overlay_config.yaml"))
    ap.add_argument("--limit-rows", type=int, default=None, help="read only the first N transfers (testing)")
    ap.add_argument("--out-root", default=None, help="write under this folder instead of next to the config")
    ap.add_argument("--skip-input-hash", action="store_true", help="do not hash the input files (faster tests)")
    args = ap.parse_args()
    t_start = time.time()

    cfg_path = os.path.abspath(args.config)
    with open(cfg_path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    base = os.path.dirname(cfg_path)
    out_root = os.path.abspath(args.out_root) if args.out_root else base
    out_dir = os.path.join(out_root, cfg["output"]["dir"])
    lab_dir = os.path.join(out_root, cfg["output"]["labels_dir"])
    os.makedirs(out_dir, exist_ok=True)
    os.makedirs(lab_dir, exist_ok=True)
    inp = {k: os.path.normpath(os.path.join(base, v)) for k, v in cfg["input"].items()}
    seed = int(cfg["seed"])
    offset = int(cfg["time"]["source_utc_offset_minutes"])

    def rng(stream):                       # one independent random stream per component
        return np.random.default_rng([seed, stream])

    # ---- read IBM files ---------------------------------------------------------------
    log("reading transfers")
    tx, reg, cur_names, fmt_names = read_transactions(inp["transactions"], args.limit_rows, offset)
    n_tx_accounts = len(reg.ids)
    log(f"{len(tx['row']):,} transfers, {n_tx_accounts:,} accounts in transfers")

    af = pd.read_csv(inp["accounts"], dtype=str)
    af["bank_int"] = af["Bank ID"].astype(int)
    af["idx"] = [reg.get(b, a) for b, a in zip(af["bank_int"], af["Account Number"])]
    n_acc = len(reg.ids)
    log(f"accounts file: {len(af):,} rows; {n_acc - n_tx_accounts:,} accounts have no transfers")

    attempts, prows = read_patterns(inp["patterns"])
    pm = match_patterns(prows, tx, reg, cur_names, fmt_names, offset)
    log(f"patterns: {len(attempts)} attempts, {len(pm):,} rows, "
        f"{(pm['status'] != 'unmatched').mean():.2%} matched to transfers")

    # ---- amounts in paise -------------------------------------------------------------
    fx, inr_per_usd = derive_rates(tx, cur_names, cfg["currency"]["usd_name"], cfg["currency"]["rupee_name"])
    ppu = fx.set_index("currency").loc[cur_names, "paise_per_unit"].to_numpy()
    amt = np.floor(tx["paid"] * ppu[tx["pcur"]] + 0.5)
    if amt.max() >= 9.0e18:
        raise OverflowError("a transfer amount does not fit in int64 paise")
    tx["amt"] = amt.astype(np.int64)
    cross = tx["pcur"] != tx["rcur"]
    recv_paise = tx["recv"] * ppu[tx["rcur"]]
    tol = float(cfg["currency"]["mismatch_tolerance"])
    mism = cross & (np.abs(recv_paise - amt) > tol * np.maximum(amt, 1))
    del amt, recv_paise

    # ---- per-account flows ------------------------------------------------------------
    log("computing per-account balances and payers")
    first_ts, min_feasible, min_run, typical, payers = account_flows(tx, n_acc)
    data_start, data_end = int(tx["ts"].min()), int(tx["ts"].max())

    acc = pd.DataFrame({"idx": np.arange(n_acc), "account_id": reg.ids, "bank_int": reg.bank})
    ent = pd.Series(index=af["idx"].to_numpy(), data=af["Entity ID"].to_numpy())
    ent_name = pd.Series(index=af["idx"].to_numpy(), data=af["Entity Name"].to_numpy())
    acc["entity_id"] = ent.reindex(acc["idx"]).to_numpy()
    acc["entity_type"] = (ent_name.reindex(acc["idx"]).str.replace(r"\s*#\d+$", "", regex=True)
                          .fillna("Unknown").to_numpy())
    # customers are IBM's owners (Entity ID); an account missing from the accounts file gets its own owner
    acc["customer_id"] = np.where(acc["entity_id"].isna(), "U-" + acc["account_id"], acc["entity_id"].fillna(""))
    acc = acc.sort_values("account_id", kind="stable").reset_index(drop=True)
    ix = acc["idx"].to_numpy()
    acc["has_transfers"] = first_ts[ix] >= 0
    acc["first_ts"] = np.where(acc["has_transfers"], first_ts[ix], data_start)
    acc["min_feasible_paise"] = min_feasible[ix]
    acc["distinct_payers"] = payers[ix]
    typ = typical[ix]
    typ = np.where(np.isnan(typ), np.nanmedian(typical), typ)

    # ---- opening dates and balances ---------------------------------------------------
    r = rng(1)
    ac = cfg["account_age"]
    age = np.clip(np.exp(r.normal(math.log(ac["median_days"]), ac["sigma"], len(acc))),
                  ac["min_days"], ac["max_days"])
    acc["opened_at"] = acc["first_ts"].to_numpy() - np.round(age * 86400).astype(np.int64)

    r = rng(2)
    ob = cfg["opening_balance"]
    mult = np.clip(np.exp(r.normal(math.log(ob["buffer_median_multiple"]), ob["buffer_sigma"], len(acc))),
                   0, ob["buffer_max_multiple"])
    buf = np.maximum(ob["buffer_floor_rupees"] * 100.0, np.floor(typ * mult + 0.5))
    if (buf + acc["min_feasible_paise"].to_numpy().astype(np.float64)).max() >= 9.0e18:
        raise OverflowError("an opening balance does not fit in int64 paise")
    acc["buffer_paise"] = buf.astype(np.int64)
    acc["opening_balance_paise"] = acc["min_feasible_paise"] + acc["buffer_paise"]

    # ---- customers and merchant status ------------------------------------------------
    person_types = set(cfg["customers"]["person_entity_types"])
    cust = (acc.groupby("customer_id", sort=True)
            .agg(entity_type=("entity_type", "first"), n_accounts=("account_id", "size"),
                 distinct_payers=("distinct_payers", "sum"))
            .reset_index())
    cust["kind"] = np.where(cust["entity_type"].isin(person_types), "person", "business")
    mc = cfg["merchant"]
    bus = cust[cust["kind"] == "business"].sort_values(["distinct_payers", "customer_id"],
                                                        ascending=[False, True], kind="stable")
    top = bus.head(math.ceil(mc["top_share_by_distinct_payers"] * len(bus)))
    top = top[top["distinct_payers"] >= mc["min_distinct_payers"]]
    cust["registered_merchant"] = cust["customer_id"].isin(top["customer_id"])
    ncust = len(cust)
    cust_pos = pd.Series(np.arange(ncust), index=cust["customer_id"])
    acc["cust_pos"] = cust_pos.reindex(acc["customer_id"]).to_numpy()
    acct_pos = pd.Series(np.arange(len(acc)), index=acc["account_id"])
    accounts_of = acc.groupby("cust_pos", sort=True).indices    # customer position -> account positions

    # ---- personal devices and phones --------------------------------------------------
    r = rng(3)
    ndev = 1 + (r.random(ncust) < cfg["devices"]["second_device_share"]).astype(np.int64)
    dev_first = np.concatenate([[0], np.cumsum(ndev)[:-1]])
    n_dev = int(ndev.sum())
    a_ndev = ndev[acc["cust_pos"].to_numpy()]
    rep = np.repeat(np.arange(len(acc)), a_ndev)
    within = np.arange(len(rep)) - np.repeat(np.cumsum(a_ndev) - a_ndev, a_ndev)
    dev_links = [pd.DataFrame({"apos": rep, "dev": dev_first[acc["cust_pos"].to_numpy()][rep] + within})]
    phone_links = [pd.DataFrame({"cpos": np.arange(ncust), "phone": np.arange(ncust)})]
    n_phone = ncust

    # ---- households (label-independent noise) -----------------------------------------
    r = rng(4)
    hc = cfg["households"]
    chosen = r.choice(ncust, size=int(round(hc["share_of_customers"] * ncust)), replace=False)
    groups, i = [], 0
    while i < len(chosen):
        size = int(r.integers(hc["size_min"], hc["size_max"] + 1))
        g = chosen[i:i + size]
        i += size
        if len(g) < hc["size_min"]:
            if groups:
                groups[-1] = np.concatenate([groups[-1], g])
            continue
        groups.append(g)
    hh_phone_draw = r.random(len(groups))
    hh_ip_links = []
    for gi, g in enumerate(groups):
        dev = n_dev
        n_dev += 1
        phone = None
        if hh_phone_draw[gi] < hc["shared_phone_probability"]:
            phone = n_phone
            n_phone += 1
        for cpos in sorted(g.tolist()):
            apos = accounts_of[cpos]
            dev_links.append(pd.DataFrame({"apos": apos, "dev": dev}))
            hh_ip_links.append(pd.DataFrame({"apos": apos, "hh": gi}))
            if phone is not None:
                phone_links.append(pd.DataFrame({"cpos": [cpos], "phone": [phone]}))

    # ---- complaints (from IBM's laundering attempts) ----------------------------------
    r = rng(8)
    cc = cfg["complaints"]
    lo, hi = math.log(cc["report_delay_min_minutes"]), math.log(cc["report_delay_max_minutes"])
    delay_draw = r.random(len(attempts))
    matched = pm[(pm["row"] >= 0) & (pm["src"] != pm["dst"])]   # a transfer to the same account moves no money
    complaints, victims = [], {}
    for k, att in enumerate(attempts.itertuples(index=False)):
        rows = matched[matched["attempt_id"] == att.attempt_id].sort_values(["ts", "row"], kind="stable")
        if rows.empty:
            continue
        victim = int(rows["src"].iloc[0])
        fraud_rows = np.unique(rows.loc[rows["src"] == victim, "row"].to_numpy())
        victims[att.attempt_id] = victim
        cid = f"CMP-{att.attempt_id:04d}"
        last = int(tx["ts"][fraud_rows].max())
        delay = int(round(math.exp(lo + (hi - lo) * delay_draw[k]) * 60))
        complaints.append({"complaint_id": cid, "victim_account_id": reg.ids[victim],
                           "fraud_transfer_ids": ";".join(f"T{x:09d}" for x in fraud_rows),
                           "disputed_amount_paise": int(tx["amt"][fraud_rows].sum()),
                           "reported_at": last + delay})
    complaints = pd.DataFrame(complaints)

    # ---- injected signal: devices and phones shared within a laundering attempt -------
    r = rng(5)
    inj = cfg["injected_signals"]
    ring_rows = []
    if inj["enabled"]:
        for att in attempts.itertuples(index=False):
            ar = pm[(pm["attempt_id"] == att.attempt_id)]
            members = np.unique(np.concatenate([ar["src"].to_numpy(), ar["dst"].to_numpy()]))
            members = members[members >= 0]
            if att.attempt_id in victims:
                members = members[members != victims[att.attempt_id]]
            mids = sorted(reg.ids[m] for m in members)
            apos = acct_pos.reindex(mids).to_numpy()
            dev_hit = r.random(len(apos)) < inj["ring_device_probability"]
            cposs = np.unique(acc["cust_pos"].to_numpy()[apos])
            ph_hit = r.random(len(cposs)) < inj["ring_phone_probability"]
            dev = phone = None
            if dev_hit.any():
                dev = n_dev
                n_dev += 1
                dev_links.append(pd.DataFrame({"apos": apos[dev_hit], "dev": dev}))
            if ph_hit.any():
                phone = n_phone
                n_phone += 1
                phone_links.append(pd.DataFrame({"cpos": cposs[ph_hit], "phone": phone}))
            ring_rows.append({"attempt_id": att.attempt_id,
                              "ring_device_id": None if dev is None else f"DV-{dev + 1:07d}",
                              "ring_phone_id": None if phone is None else f"PH-{phone + 1:07d}",
                              "member_accounts": len(apos), "accounts_on_device": int(dev_hit.sum()),
                              "member_customers": len(cposs), "customers_on_phone": int(ph_hit.sum())})
    rings = pd.DataFrame(ring_rows)

    # ---- IPs --------------------------------------------------------------------------
    r = rng(6)
    ic = cfg["ips"]
    nip = r.integers(ic["min_per_account"], ic["max_per_account"] + 1, len(acc))
    n_personal_ip = int(nip.sum())
    total_ip = n_personal_ip + len(groups)
    addr = r.choice(2 ** 24 - 2, size=total_ip, replace=False) + 1
    ip_ids = ("10." + pd.Series((addr >> 16) & 255).astype(str) + "." + pd.Series((addr >> 8) & 255).astype(str)
              + "." + pd.Series(addr & 255).astype(str)).to_numpy()
    ip_links = [pd.DataFrame({"apos": np.repeat(np.arange(len(acc)), nip), "ip": np.arange(n_personal_ip)})]
    for df in hh_ip_links:
        ip_links.append(pd.DataFrame({"apos": df["apos"].to_numpy(), "ip": n_personal_ip + df["hh"].to_numpy()}))

    # ---- assemble link tables ---------------------------------------------------------
    opened = acc["opened_at"].to_numpy()
    firsts = acc["first_ts"].to_numpy()
    used = pd.concat(dev_links, ignore_index=True).drop_duplicates(["apos", "dev"])
    used = used.sort_values(["apos", "dev"], kind="stable").reset_index(drop=True)
    used["first_seen"] = first_seen(rng(9), opened[used["apos"]], firsts[used["apos"]])
    used_device = pd.DataFrame({"account_id": acc["account_id"].to_numpy()[used["apos"]],
                                "device_id": [f"DV-{d + 1:07d}" for d in used["dev"]],
                                "first_seen": used["first_seen"]})
    ipl = pd.concat(ip_links, ignore_index=True).drop_duplicates(["apos", "ip"])
    ipl = ipl.sort_values(["apos", "ip"], kind="stable").reset_index(drop=True)
    ipl["first_seen"] = first_seen(rng(10), opened[ipl["apos"]], firsts[ipl["apos"]])
    logged_in_from = pd.DataFrame({"account_id": acc["account_id"].to_numpy()[ipl["apos"]],
                                   "ip_id": ip_ids[ipl["ip"]], "first_seen": ipl["first_seen"]})
    phl = pd.concat(phone_links, ignore_index=True).drop_duplicates(["cpos", "phone"])
    phl = phl.sort_values(["cpos", "phone"], kind="stable").reset_index(drop=True)
    has_phone = pd.DataFrame({"entity_id": cust["customer_id"].to_numpy()[phl["cpos"]],
                              "phone_id": [f"PH-{p + 1:07d}" for p in phl["phone"]]})

    # ---- demo banks (the banks themselves come from IBM's accounts file) -------------
    chain = pm[["src", "dst"]].to_numpy().ravel()
    chain = np.unique(chain[chain >= 0])
    chain_banks = pd.Series([reg.bank[c] for c in chain]).value_counts()
    chain_banks = chain_banks.rename_axis("bank_int").reset_index(name="n").sort_values(
        ["n", "bank_int"], ascending=[False, True], kind="stable")
    demo_banks = chain_banks["bank_int"].head(int(cfg["banks"]["demo_count"])).tolist()
    ibm_name = af.groupby("bank_int")["Bank Name"].first()
    demo_out = pd.DataFrame({"bank_id": [str(b) for b in demo_banks],
                             "demo_slot": list(range(1, len(demo_banks) + 1))})

    # ---- output tables: only what is not already in IBM's files ----------------------
    account_overlay = pd.DataFrame({"account_id": acc["account_id"], "opened_at": acc["opened_at"],
                                    "opening_balance_paise": acc["opening_balance_paise"],
                                    "min_feasible_paise": acc["min_feasible_paise"],
                                    "buffer_paise": acc["buffer_paise"]})
    merchants = (cust.loc[cust["registered_merchant"], ["customer_id"]]
                 .rename(columns={"customer_id": "entity_id"}).reset_index(drop=True))
    if len(rings):
        rings.insert(0, "complaint_id", [f"CMP-{a:04d}" if a in victims else "" for a in rings["attempt_id"]])
    out_tables = {"account_overlay.csv": account_overlay, "registered_merchants.csv": merchants,
                  "demo_banks.csv": demo_out, "used_device.csv": used_device, "has_phone.csv": has_phone,
                  "logged_in_from.csv": logged_in_from, "complaints.csv": complaints}
    lab_tables = {"ring_devices.csv": rings}       # ground truth, written into the evaluation set

    # ---- checks -----------------------------------------------------------------------
    checks = []

    def check(name, ok, detail=""):
        checks.append({"check": name, "result": "PASS" if ok else "FAIL", "detail": detail})

    neg = int(((acc["opening_balance_paise"].to_numpy() + min_run[ix]) < 0).sum())
    check("No running balance goes below zero", neg == 0,
          f"{neg} accounts below zero (outflows processed before inflows within each minute)")
    bad_open = int((acc["opened_at"] >= acc["first_ts"]).sum())
    check("Every account opens before its first transfer", bad_open == 0, f"{bad_open} accounts open too late")
    fs_bad = int((used["first_seen"] < opened[used["apos"]]).sum() + (ipl["first_seen"] < opened[ipl["apos"]]).sum())
    check("Device and IP first_seen never before the account opens", fs_bad == 0, f"{fs_bad} links violate it")
    if len(rings):
        dev_rate = rings["accounts_on_device"].sum() / max(rings["member_accounts"].sum(), 1)
        ph_rate = rings["customers_on_phone"].sum() / max(rings["member_customers"].sum(), 1)
        t = inj["rate_tolerance"]
        check("Accounts in an attempt share its device at the set rate",
              abs(dev_rate - inj["ring_device_probability"]) <= t,
              f"measured {dev_rate:.3f}, set {inj['ring_device_probability']}")
        check("Owners in an attempt share its phone at the set rate",
              abs(ph_rate - inj["ring_phone_probability"]) <= t,
              f"measured {ph_rate:.3f}, set {inj['ring_phone_probability']}")
    else:
        dev_rate = ph_rate = float("nan")
    ring_dev_nums = set(int(x[3:]) - 1 for x in rings["ring_device_id"].dropna()) if len(rings) else set()
    dev_owner = pd.DataFrame({"dev": used["dev"], "cpos": acc["cust_pos"].to_numpy()[used["apos"]]})
    owners_per_dev = dev_owner.groupby("dev")["cpos"].nunique()
    noise_devs = set(owners_per_dev[owners_per_dev > 1].index) - ring_dev_nums
    noise_rate = dev_owner[dev_owner["dev"].isin(noise_devs)]["cpos"].nunique() / max(ncust, 1)
    check("Owners in households share a device at the noise rate",
          abs(noise_rate - hc["share_of_customers"]) <= 0.005,
          f"measured {noise_rate:.4f}, set {hc['share_of_customers']}")
    ring_apos = set(used.loc[used["dev"].isin(ring_dev_nums), "apos"])
    noise_apos = set(used.loc[used["dev"].isin(noise_devs), "apos"]) - ring_apos
    nonring_shared_rate = len(noise_apos) / max(len(acc) - len(ring_apos), 1)
    if len(complaints):
        ok_sum, ok_src = True, True
        for c in complaints.itertuples(index=False):
            rows = np.array([int(x[1:]) for x in c.fraud_transfer_ids.split(";")])
            ok_sum &= int(tx["amt"][rows].sum()) == c.disputed_amount_paise
            ok_src &= bool((tx["src"][rows] == reg.index[c.victim_account_id]).all())
        check("Each disputed amount equals the sum of its fraud transfers", ok_sum, f"{len(complaints)} complaints")
        check("Every fraud transfer leaves the victim's account", ok_src, f"{len(complaints)} complaints")
    match_rate = float((pm["status"] != "unmatched").mean())
    check("Pattern rows matched to transfers", match_rate >= 0.99 or args.limit_rows is not None,
          f"{match_rate:.2%} of {len(pm):,} rows" + (" (sample run)" if args.limit_rows else ""))
    dupes = (int(used_device.duplicated(["account_id", "device_id"]).sum())
             + int(logged_in_from.duplicated(["account_id", "ip_id"]).sum()) + int(has_phone.duplicated().sum()))
    check("Account IDs are unique and no link is listed twice",
          account_overlay["account_id"].is_unique and dupes == 0, f"{dupes} duplicate links")
    n_missing = int(acc["entity_id"].isna().sum())
    check("Every account is in IBM's accounts file", n_missing == 0, f"{n_missing} accounts missing")
    bad_cols = [f"{f}:{c}" for f, df in out_tables.items() for c in df.columns
                if any(w in c.lower() for w in FORBIDDEN_COLUMN_WORDS)]
    check("No label or ground-truth columns in output/", not bad_cols, ", ".join(bad_cols) or "none found")

    # ---- write ------------------------------------------------------------------------
    log("writing files")
    for fname, df in out_tables.items():
        write_csv(df, os.path.join(out_dir, fname))
    for fname, df in lab_tables.items():
        write_csv(df, os.path.join(lab_dir, fname))
    write_csv(fx, os.path.join(out_root, "fx_rates.csv"))
    written = ([(os.path.join(out_dir, f), df) for f, df in out_tables.items()]
               + [(os.path.join(lab_dir, f), df) for f, df in lab_tables.items()]
               + [(os.path.join(out_root, "fx_rates.csv"), fx)])
    fingerprints = [(os.path.relpath(p, out_root).replace(os.sep, "/"), len(df), sha256_file(p)) for p, df in written]
    input_hash = {k: ("not hashed" if args.skip_input_hash else sha256_file(v)) for k, v in inp.items()}

    # ---- report -----------------------------------------------------------------------
    n_fail = sum(c["result"] == "FAIL" for c in checks)
    kinds = cust["kind"].value_counts()
    person_list = " and ".join(sorted(person_types))
    L = []
    L.append("# TraceHold synthetic data report (overlay v1)\n")
    L.append(f"Generated {fmt_ist(time.time())} by `generate_overlay.py` with seed {seed} "
             f"in {time.time() - t_start:.0f} s. {len(checks) - n_fail} of {len(checks)} checks pass."
             + (f" Sample run: first {args.limit_rows:,} transfers only." if args.limit_rows else "") + "\n")
    L.append("Only data that is not already in IBM's three files is written here. Banks, owners and owner types "
             "come straight from `HI-Small_accounts.csv`; laundering attempts from `HI-Small_Patterns.txt`.\n")
    L.append("## Inputs\n")
    L.append("| File | Rows | Notes | SHA-256 |\n| --- | --- | --- | --- |")
    L.append(f"| `HI-Small_Trans.csv` | {len(tx['row']):,} transfers | {fmt_ist(data_start)} to {fmt_ist(data_end)}; "
             f"{n_tx_accounts:,} accounts | `{input_hash['transactions']}` |")
    L.append(f"| `HI-Small_accounts.csv` | {len(af):,} accounts | {af['Bank ID'].nunique():,} banks; "
             f"{af['Entity ID'].nunique():,} owners; {n_acc - n_tx_accounts:,} accounts never transfer | "
             f"`{input_hash['accounts']}` |")
    L.append(f"| `HI-Small_Patterns.txt` | {len(attempts)} attempts, {len(pm):,} rows | {match_rate:.2%} of rows "
             f"matched to a transfer | `{input_hash['patterns']}` |\n")
    L.append("## What was created\n")
    L.append("| File | Rows | What it holds |\n| --- | --- | --- |")
    L.append(f"| `output/account_overlay.csv` | {len(account_overlay):,} | Opening date and opening balance per account: "
             f"a generated buffer on top of the calculated minimum |")
    L.append(f"| `output/registered_merchants.csv` | {len(merchants):,} | Owners (`Entity ID`) that are registered "
             f"merchants: business owners in the top {mc['top_share_by_distinct_payers']:.0%} by distinct payers |")
    L.append(f"| `output/demo_banks.csv` | {len(demo_out)} | The demo banks: IBM banks with the most accounts in "
             f"laundering attempts |")
    L.append(f"| `output/used_device.csv` | {len(used_device):,} | Which account used which device, from when "
             f"(generated) |")
    L.append(f"| `output/has_phone.csv` | {len(has_phone):,} | Which owner uses which phone (generated) |")
    L.append(f"| `output/logged_in_from.csv` | {len(logged_in_from):,} | Which account logged in from which IP, "
             f"from when (generated, private 10.0.0.0/8 range) |")
    L.append(f"| `output/complaints.csv` | {len(complaints):,} | One complaint per IBM attempt; report times "
             f"generated |")
    L.append(f"| `../eval/labels/ring_devices.csv` | {len(rings):,} | The shared device and phone of each laundering "
             f"attempt. Ground truth, part of the evaluation set: never load into the graph |")
    L.append(f"| `fx_rates.csv` | {len(fx)} | Exchange rates used for the balances; the cleaning step must use "
             f"the same table |\n")
    L.append("## How the graph uses these with IBM's files\n")
    L.append("- `Bank`: one per `Bank ID` in `HI-Small_accounts.csv`, named with its `Bank Name`. Demo banks from "
             "`demo_banks.csv`.")
    L.append(f"- `Customer`: one per `Entity ID` in `HI-Small_accounts.csv` ({ncust:,} owners). `kind` is person "
             f"for {person_list} owners ({kinds.get('person', 0):,}) and business otherwise "
             f"({kinds.get('business', 0):,}); `registered_merchant` is true for owners in "
             f"`registered_merchants.csv`.")
    L.append("- `Account`: one per row of `HI-Small_accounts.csv`, with ID `<Bank ID>-<Account Number>`; "
             "`opened_at` and `opening_balance` from `account_overlay.csv`.")
    L.append("- `Device`, `Phone`, `IP`: the distinct IDs in `used_device.csv`, `has_phone.csv` and "
             "`logged_in_from.csv`.")
    L.append("- `Complaint`: `complaints.csv`. Complaint `CMP-0007` is attempt 7 (the 7th block) in "
             "`HI-Small_Patterns.txt`.\n")
    L.append("## Measured rates\n")
    L.append("| Signal | Setting | Measured |\n| --- | --- | --- |")
    L.append(f"| Accounts in an attempt on its shared device | {inj['ring_device_probability']} | {dev_rate:.3f} |")
    L.append(f"| Owners in an attempt on its shared phone | {inj['ring_phone_probability']} | {ph_rate:.3f} |")
    L.append(f"| Owners sharing a household device | {hc['share_of_customers']} | {noise_rate:.4f} |")
    L.append(f"| Accounts outside attempts on a shared household device | n/a | {nonring_shared_rate:.4f} |")
    L.append(f"| Owners with a second device | {cfg['devices']['second_device_share']} | {(ndev == 2).mean():.3f} |")
    L.append(f"| Cross-currency transfers whose converted amounts differ by more than {tol:.0%} | n/a | "
             f"{int(mism.sum()):,} of {int(cross.sum()):,} |\n")
    L.append("## Checks\n")
    L.append("| Check | Result | Detail |\n| --- | --- | --- |")
    for c in checks:
        L.append(f"| {c['check']} | {c['result']} | {c['detail']} |")
    L.append("")
    L.append("## Demo banks\n")
    L.append("| Slot | Bank ID | IBM name | Accounts in laundering attempts |\n| --- | --- | --- | --- |")
    for slot, b in enumerate(demo_banks, 1):
        n = int(chain_banks.loc[chain_banks["bank_int"] == b, "n"].iloc[0])
        L.append(f"| {slot} | {b} | {ibm_name.get(b, '')} | {n} |")
    L.append("")
    L.append(f"## Exchange rates\n\nOne amount per transfer, from the amount paid. Rates are the median implied "
             f"rate per currency from IBM's cross-currency transfers, via US dollars; rupees per US dollar "
             f"= {inr_per_usd:.4f}, from IBM's own Rupee rate.\n")
    L.append("## Notes\n")
    L.append("- Transfer IDs are `T` + the zero-based row number in `HI-Small_Trans.csv` (header excluded), "
             "zero-padded to 9 digits. Times are UTC epoch seconds; IBM's times are read as IST wall-clock.")
    L.append("- Minimum feasible balances are computed over the whole file with outflows before inflows in the "
             "same minute, so balances stay non-negative for any time-prefix slice and either same-minute order.")
    L.append("- Ring devices and phones are the only label-derived data; their attempts are recorded only in "
             "`../eval/labels/ring_devices.csv`. Set `injected_signals.enabled: false` for the ablation.")
    L.append("- Complaints: the victim is the sender of the attempt's earliest transfer between two different accounts; "
             "fraud transfers are the victim's transfers to other accounts in that attempt.\n")
    L.append("## File fingerprints\n")
    L.append("| File | Rows | SHA-256 |\n| --- | --- | --- |")
    for rel, n, h in fingerprints:
        L.append(f"| `{rel}` | {n:,} | `{h}` |")
    L.append("\n## Rerun\n\n`python3 generate_overlay.py` from this folder. The same seed and inputs give identical "
             "files and fingerprints.")
    with open(os.path.join(out_root, "REPORT.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(L) + "\n")

    log(f"done in {time.time() - t_start:.0f} s: {len(checks) - n_fail}/{len(checks)} checks pass")
    for c in checks:
        log(f"  {c['result']}  {c['check']}  {c['detail']}")


if __name__ == "__main__":
    main()
