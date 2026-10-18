#!/usr/bin/env python3
"""TraceHold evaluation set: ground truth for scoring the agents.

Maps the synthetic overlay (../synthetic/output/) onto IBM's real files and
builds the ground truth the agents are scored against (implementation plan:
Phase 1 steps 9 and 11, Phase 4; Appendix C step 6).

Writes into labels/ (never load anything there into the graph):
  account_labels.csv    every account with a laundering-labelled transfer: mule or genuine,
                        the shares behind the label, and whether it is a complaint victim.
                        An account not listed is genuine: none of its transfers is laundering-labelled.
  complaints_truth.csv  one row per complaint: IBM attempt, split, depth, depth bucket, trail size
  trail_accounts.csv    per complaint, every account on the true money trail: layer, label, labelled inflow
  trail_transfers.csv   per complaint, every transfer on the true money trail
  ring_devices.csv      written by ../synthetic/generate_overlay.py; checked here
and REPORT.md next to this script: definitions, counts, mapping checks and file fingerprints.

The true money trail of a complaint starts at its fraud transfers and follows IBM's
laundering-labelled transfers forward in time: a labelled transfer is on the trail when it
leaves an account the trail has already reached (same minute allowed). The victim's own
transfers count only when they are the complaint's fraud transfers.

Usage:
  python3 build_eval_set.py
  python3 build_eval_set.py --out-root /tmp/eval_test --synthetic-root /tmp/synthetic_test
"""
import argparse
import hashlib
import os
import sys
import time
from collections import defaultdict

import numpy as np
import pandas as pd
import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))          # project root, for the shared data package
sys.dont_write_bytecode = True
import data.ibm as gen                              # noqa: E402  IBM file readers


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 22), b""):
            h.update(block)
    return h.hexdigest()


def write_csv(df, path):
    df.to_csv(path, index=False, lineterminator="\n")


class Trail:
    """Follows laundering-labelled transfers forward in time from a complaint's fraud transfers."""

    def __init__(self, rows, src, dst, ts, max_depth):
        self.rows, self.src, self.dst, self.ts = rows, src, dst, ts
        self.max_depth = max_depth
        self.gts, self.gstart = np.unique(ts, return_index=True)   # transfers grouped by minute
        self.gend = np.append(self.gstart[1:], len(ts))

    def follow(self, victim, fraud_rows, t0, allowed=None):
        layer, arrival, hops_in, on = {victim: 0}, {}, defaultdict(int), {}
        fraud_rows = set(fraud_rows)
        g0 = int(np.searchsorted(self.gts, t0))
        for g in range(g0, len(self.gts)):
            t = int(self.gts[g])
            pending = [p for p in range(self.gstart[g], self.gend[g]) if allowed is None or allowed[p]]
            changed = True
            while changed and pending:                  # repeat within a minute: money may arrive and leave in it
                changed, rest = False, []
                for p in pending:
                    u = int(self.src[p])
                    ok = (int(self.rows[p]) in fraud_rows) if u == victim else (u in arrival)
                    if not ok:
                        rest.append(p)
                        continue
                    v = int(self.dst[p])
                    ly, hp = layer[u] + 1, min(hops_in[u] + 1, self.max_depth)
                    on[p] = (ly, hp)
                    if v != victim:
                        layer[v] = min(layer.get(v, 1 << 30), ly)
                        arrival.setdefault(v, t)
                        hops_in[v] = max(hops_in[v], hp)
                    changed = True
                pending = rest
        return on, layer, arrival


def complaint_overlaps_and_demo_pair(truth, trail_accounts, used_device, merchants, demo_banks, dcfg):
    """Pairs of complaints whose trails share accounts or devices, and the demo pair.

    Overlaps use every account on each trail except the victim, and the devices those accounts use.
    Demo pair: complaint 1 is deep (min_depth layers or more), passes through a registered merchant's
    account and disputes at most max_disputed_rupees; complaint 2 is reported later, disputes at most the
    same and shares at least one device with complaint 1's trail; both trails reach accounts at a common
    demo bank (the investigating bank, complaint 1's victim bank when possible). Ranked by shared devices,
    then complaint 1's depth, then complaint IDs, so the choice is deterministic.
    """
    min_depth = int(dcfg.get("min_depth", 4))
    max_paise = int(dcfg.get("max_disputed_rupees", 10_000_000)) * 100
    demo_ids = [str(b) for b in demo_banks.sort_values("demo_slot")["bank_id"]]
    nv = trail_accounts[trail_accounts["label"] != "victim"][["complaint_id", "account_id", "entity_id", "bank_id"]]
    acc_sets = nv.groupby("complaint_id")["account_id"].apply(set).to_dict()
    dev = nv.merge(used_device, on="account_id")[["complaint_id", "device_id"]].drop_duplicates()
    dev_sets = dev.groupby("complaint_id")["device_id"].apply(set).to_dict()
    pair_acc, pair_dev = defaultdict(int), defaultdict(int)
    for _, cs in nv.groupby("account_id")["complaint_id"]:
        cs = sorted(set(cs))
        for i in range(len(cs)):
            for j in range(i + 1, len(cs)):
                pair_acc[(cs[i], cs[j])] += 1
    for _, cs in dev.groupby("device_id")["complaint_id"]:
        cs = sorted(set(cs))
        for i in range(len(cs)):
            for j in range(i + 1, len(cs)):
                pair_dev[(cs[i], cs[j])] += 1
    keys = sorted(set(pair_acc) | set(pair_dev))
    overlaps = pd.DataFrame({"complaint_a": [k[0] for k in keys], "complaint_b": [k[1] for k in keys],
                             "shared_accounts": [pair_acc.get(k, 0) for k in keys],
                             "shared_devices": [pair_dev.get(k, 0) for k in keys]},
                            columns=["complaint_a", "complaint_b", "shared_accounts", "shared_devices"])

    t = truth.set_index("complaint_id")
    rec = t.to_dict("index")
    merchant_on_trail = set(nv.loc[nv["entity_id"].isin(set(merchants["entity_id"])), "complaint_id"])
    demo_banks_on = nv[nv["bank_id"].isin(demo_ids)].groupby("complaint_id")["bank_id"].apply(set).to_dict()
    cands = []
    for c1, r1 in rec.items():
        c1_ok = (r1["depth_trail"] >= min_depth and c1 in merchant_on_trail and r1["disputed_amount_paise"] <= max_paise)
        for c2, r2 in rec.items():
            if c2 == c1 or r2["reported_at"] <= r1["reported_at"]:
                continue
            shared = len(dev_sets.get(c1, set()) & dev_sets.get(c2, set()))
            if not shared:
                continue
            common = demo_banks_on.get(c1, set()) & demo_banks_on.get(c2, set())
            v1 = str(r1["victim_bank_id"])
            bank = v1 if v1 in common else next((b for b in demo_ids if b in common), None)
            all_ok = bool(c1_ok and r2["disputed_amount_paise"] <= max_paise and bank is not None)
            cands.append((not all_ok, bank != v1, -shared, -int(r1["depth_trail"]), c1, c2, shared, bank, all_ok))
    cols = ["order", "complaint_id", "pattern_type", "split", "depth_trail", "disputed_amount_paise",
            "victim_account_id", "shared_devices", "investigating_bank_id", "meets_all_criteria"]
    if not cands:
        return overlaps, pd.DataFrame(columns=cols)
    best = sorted(cands)[0]
    c1, c2, shared, bank, all_ok = best[4], best[5], best[6], best[7], best[8]
    rows = []
    for order, c in ((1, c1), (2, c2)):
        r = t.loc[c]
        rows.append({"order": order, "complaint_id": c, "pattern_type": r["pattern_type"], "split": r["split"],
                     "depth_trail": int(r["depth_trail"]), "disputed_amount_paise": int(r["disputed_amount_paise"]),
                     "victim_account_id": r["victim_account_id"], "shared_devices": shared,
                     "investigating_bank_id": bank if bank is not None else "", "meets_all_criteria": all_ok})
    return overlaps, pd.DataFrame(rows, columns=cols)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=os.path.join(HERE, "eval_config.yaml"))
    ap.add_argument("--out-root", default=None, help="write labels/ and REPORT.md under this folder")
    ap.add_argument("--synthetic-root", default=None,
                    help="folder holding the generator's output/ and fx_rates.csv (default: synthetic_dir)")
    ap.add_argument("--skip-input-hash", action="store_true", help="do not hash IBM's input files")
    args = ap.parse_args()
    t_start = time.time()

    cfg_path = os.path.abspath(args.config)
    with open(cfg_path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    base = os.path.dirname(cfg_path)
    syn = os.path.normpath(os.path.join(base, cfg["synthetic_dir"]))
    sroot = os.path.abspath(args.synthetic_root) if args.synthetic_root else syn
    out_root = os.path.abspath(args.out_root) if args.out_root else base
    lab_dir = os.path.join(out_root, cfg["labels_dir"])
    os.makedirs(lab_dir, exist_ok=True)

    with open(os.path.join(syn, "overlay_config.yaml"), encoding="utf-8") as f:
        ocfg = yaml.safe_load(f)
    inp = {k: os.path.normpath(os.path.join(syn, v)) for k, v in ocfg["input"].items()}
    offset = int(ocfg["time"]["source_utc_offset_minutes"])
    syn_out = os.path.join(sroot, ocfg["output"]["dir"])
    ring_path = os.path.normpath(os.path.join(sroot, ocfg["output"]["labels_dir"], "ring_devices.csv"))

    # ---- IBM's real files -------------------------------------------------------------
    log("reading IBM files")
    tx, reg, cur_names, fmt_names = gen.read_transactions(inp["transactions"], None, offset)
    af = pd.read_csv(inp["accounts"], dtype=str)
    af["bank_int"] = af["Bank ID"].astype(int)
    af["idx"] = [reg.get(b, a) for b, a in zip(af["bank_int"], af["Account Number"])]
    n_acc = len(reg.ids)
    attempts, prows = gen.read_patterns(inp["patterns"])
    pm = gen.match_patterns(prows, tx, reg, cur_names, fmt_names, offset)
    fx = pd.read_csv(os.path.join(sroot, "fx_rates.csv"))
    ppu = fx.set_index("currency").loc[cur_names, "paise_per_unit"].to_numpy()
    amt = np.floor(tx["paid"] * ppu[tx["pcur"]] + 0.5).astype(np.int64)
    ids = np.array(reg.ids, dtype=object)
    bank_of = np.array(reg.bank, dtype=np.int64)
    ent_of = np.full(n_acc, "", dtype=object)
    ent_of[af["idx"].to_numpy()] = af["Entity ID"].to_numpy()
    data_start = int(tx["ts"].min())

    # ---- the synthetic overlay ----------------------------------------------------------
    complaints = pd.read_csv(os.path.join(syn_out, "complaints.csv"))
    demo = pd.read_csv(os.path.join(syn_out, "demo_banks.csv"), dtype=str)
    demo_banks = set(int(b) for b in demo["bank_id"])
    complaints["attempt_id"] = complaints["complaint_id"].str[4:].astype(int)
    complaints["victim_idx"] = [reg.index.get(a, -1) for a in complaints["victim_account_id"]]
    complaints["fraud_rows"] = [[int(x[1:]) for x in s.split(";")] for s in complaints["fraud_transfer_ids"]]
    victims = set(complaints["victim_idx"])

    # ---- account labels (Appendix C step 6) ---------------------------------------------
    log("labelling accounts")
    src, dst, ts, row = tx["src"], tx["dst"], tx["ts"], tx["row"]
    lab = tx["lab"].astype(bool)
    ns = src != dst                                         # same-account transfers move no money
    s, d, a, lb = src[ns], dst[ns], amt[ns], lab[ns]
    touched = np.unique(np.concatenate([s[lb], d[lb]]))

    def total(keys, vals):
        return pd.Series(vals).groupby(keys).sum().reindex(touched).fillna(0).astype(np.int64).to_numpy()

    al = pd.DataFrame({"idx": touched})
    al["out_value_paise"] = total(s, a)
    al["out_laundering_paise"] = total(s[lb], a[lb])
    al["in_value_paise"] = total(d, a)
    al["in_laundering_paise"] = total(d[lb], a[lb])
    use_out = al["out_value_paise"] > 0
    share = np.where(use_out, al["out_laundering_paise"] / al["out_value_paise"].clip(lower=1),
                     al["in_laundering_paise"] / al["in_value_paise"].clip(lower=1))
    thr = float(cfg["labels"]["mule_share_threshold"])
    al["label"] = np.where(share > thr, "mule", "genuine")
    al["decided_on"] = np.where(use_out, "outgoing", "incoming")
    al["laundering_share"] = np.round(share, 6)
    al["is_victim"] = al["idx"].isin(victims)
    al["account_id"] = ids[al["idx"]]
    al["bank_id"] = bank_of[al["idx"]].astype(str)
    al["entity_id"] = ent_of[al["idx"]]
    al = al.sort_values("account_id", kind="stable").reset_index(drop=True)
    label_of = dict(zip(al["idx"], al["label"]))
    sens = {t: int((share > t).sum()) for t in cfg["labels"]["sensitivity_thresholds"]}
    account_labels = al[["account_id", "bank_id", "entity_id", "label", "is_victim", "decided_on",
                         "laundering_share", "out_value_paise", "out_laundering_paise",
                         "in_value_paise", "in_laundering_paise"]]

    # ---- true money trail per complaint -------------------------------------------------
    log("tracing the true money trail of each complaint")
    lmask = lab & ns
    order = np.lexsort((row[lmask], ts[lmask]))
    L_row, L_src, L_dst = row[lmask][order], src[lmask][order], dst[lmask][order]
    L_ts, L_amt = ts[lmask][order], amt[lmask][order]
    trail = Trail(L_row, L_src, L_dst, L_ts, int(cfg["trail"]["max_depth"]))
    matched = pm[pm["row"] >= 0]
    att_rows = matched.groupby("attempt_id")["row"].apply(lambda r: set(r.tolist())).to_dict()
    att_accts = {k: set(g["src"]).union(g["dst"]) for k, g in pm.groupby("attempt_id")}
    att_span = pm.groupby("attempt_id")["ts"].agg(["min", "max"])
    att_n = pm.groupby("attempt_id").size()
    meta = attempts.set_index("attempt_id")
    cut = data_start + int(cfg["split"]["dev_cutoff_days"]) * 86400
    sh_max, md_max = int(cfg["depth_buckets"]["shallow_max"]), int(cfg["depth_buckets"]["medium_max"])

    truth, t_acc, t_tr = [], [], []
    for c in complaints.itertuples(index=False):
        rows_a = att_rows.get(c.attempt_id, set())
        f_rows = c.fraud_rows
        t0 = int(ts[f_rows].min())
        on, layer, arrival = trail.follow(c.victim_idx, f_rows, t0)
        allowed = np.isin(L_row, np.fromiter(rows_a, dtype=np.int64, count=len(rows_a)))
        on_a, _, _ = trail.follow(c.victim_idx, f_rows, t0, allowed)
        depth_trail = max((hp for _, hp in on.values()), default=0)
        depth_att = max((hp for _, hp in on_a.values()), default=0)
        inflow = defaultdict(int)
        for p, (ly, hp) in sorted(on.items()):
            inflow[int(L_dst[p])] += int(L_amt[p])
            t_tr.append({"complaint_id": c.complaint_id, "transfer_id": f"T{int(L_row[p]):09d}", "layer": ly,
                         "chain_hops": hp, "from_account_id": ids[L_src[p]], "to_account_id": ids[L_dst[p]],
                         "amount_paise": int(L_amt[p]), "ts": int(L_ts[p]), "in_attempt": int(L_row[p]) in rows_a})
        accts = sorted(layer, key=lambda v: (layer[v], ids[v]))
        n_mule = n_gen = 0
        for v in accts:
            if v == c.victim_idx:
                lbl = "victim"
            else:
                lbl = label_of.get(v, "genuine")
                n_mule += lbl == "mule"
                n_gen += lbl == "genuine"
            t_acc.append({"complaint_id": c.complaint_id, "account_id": ids[v], "bank_id": str(bank_of[v]),
                          "entity_id": ent_of[v], "layer": layer[v], "label": lbl,
                          "first_reached_ts": arrival.get(v), "labelled_inflow_paise": inflow.get(v, 0),
                          "in_attempt": v in att_accts.get(c.attempt_id, set()),
                          "on_demo_bank": int(bank_of[v]) in demo_banks})
        bucket = "shallow" if depth_trail <= sh_max else ("medium" if depth_trail <= md_max else "deep")
        end = int(att_span.loc[c.attempt_id, "max"])
        truth.append({"complaint_id": c.complaint_id, "attempt_id": c.attempt_id,
                      "pattern_type": meta.loc[c.attempt_id, "pattern_type"],
                      "pattern_detail": meta.loc[c.attempt_id, "detail"],
                      "split": "dev" if end < cut else "test",
                      "victim_account_id": c.victim_account_id, "victim_bank_id": str(bank_of[c.victim_idx]),
                      "victim_entity_id": ent_of[c.victim_idx],
                      "victim_on_demo_bank": int(bank_of[c.victim_idx]) in demo_banks,
                      "disputed_amount_paise": int(c.disputed_amount_paise), "n_fraud_transfers": len(f_rows),
                      "attempt_start_ts": int(att_span.loc[c.attempt_id, "min"]), "attempt_end_ts": end,
                      "reported_at": int(c.reported_at), "depth_attempt": depth_att, "depth_trail": depth_trail,
                      "depth_bucket": bucket, "trail_accounts": len(accts) - 1, "trail_mules": n_mule,
                      "trail_genuine": n_gen, "trail_transfers": len(on),
                      "attempt_transfers": int(att_n.get(c.attempt_id, 0)),
                      "attempt_transfers_on_trail": len(on_a)})
    truth = pd.DataFrame(truth)
    trail_accounts = pd.DataFrame(t_acc)
    trail_accounts["first_reached_ts"] = trail_accounts["first_reached_ts"].astype("Int64")   # empty for the victim
    trail_transfers = pd.DataFrame(t_tr)

    # ---- mapping: every synthetic key must match a real IBM record ---------------------
    log("checking the synthetic overlay maps onto IBM's records")
    ibm_accounts = set(af["bank_int"].astype(str) + "-" + af["Account Number"])
    ibm_entities = set(af["Entity ID"])
    ibm_banks = set(af["bank_int"].astype(str))
    ov = pd.read_csv(os.path.join(syn_out, "account_overlay.csv"), usecols=["account_id"])
    ud = pd.read_csv(os.path.join(syn_out, "used_device.csv"), usecols=["account_id", "device_id"])
    li = pd.read_csv(os.path.join(syn_out, "logged_in_from.csv"), usecols=["account_id"])
    hp_ = pd.read_csv(os.path.join(syn_out, "has_phone.csv"), usecols=["entity_id"], dtype=str)
    rm = pd.read_csv(os.path.join(syn_out, "registered_merchants.csv"), dtype=str)
    rings = pd.read_csv(ring_path)
    fr_all = np.array([r for rows_ in complaints["fraud_rows"] for r in rows_], dtype=np.int64)
    fr_ok = int(sum(1 for c in complaints.itertuples(index=False) for r in c.fraud_rows
                    if 0 <= r < len(row) and src[r] == c.victim_idx and lab[r] and r in att_rows.get(c.attempt_id, ())))

    def m(name, key, real, ok, total, note=""):
        return {"synthetic": name, "key": key, "real": real, "matched": f"{ok:,} of {total:,}",
                "ok": ok == total, "note": note}

    ov_ids = set(ov["account_id"])
    mapping = [
        m("output/account_overlay.csv", "account_id", "HI-Small_accounts.csv: Bank ID + Account Number",
          len(ov_ids & ibm_accounts), len(ov), f"covers {len(ov_ids & ibm_accounts):,} of {len(ibm_accounts):,} IBM accounts"),
        m("output/used_device.csv", "account_id", "HI-Small_accounts.csv", int(ud["account_id"].isin(ibm_accounts).sum()), len(ud)),
        m("output/logged_in_from.csv", "account_id", "HI-Small_accounts.csv", int(li["account_id"].isin(ibm_accounts).sum()), len(li)),
        m("output/has_phone.csv", "entity_id", "HI-Small_accounts.csv: Entity ID", int(hp_["entity_id"].isin(ibm_entities).sum()), len(hp_),
          f"covers {hp_['entity_id'].nunique():,} of {len(ibm_entities):,} IBM owners"),
        m("output/registered_merchants.csv", "entity_id", "HI-Small_accounts.csv: Entity ID", int(rm["entity_id"].isin(ibm_entities).sum()), len(rm)),
        m("output/demo_banks.csv", "bank_id", "HI-Small_accounts.csv: Bank ID", int(demo["bank_id"].isin(ibm_banks).sum()), len(demo)),
        m("output/complaints.csv", "complaint_id", "HI-Small_Patterns.txt: attempt (block) number",
          int(complaints["attempt_id"].isin(attempts["attempt_id"]).sum()), len(complaints),
          f"{complaints['attempt_id'].nunique():,} of {len(attempts):,} attempts have a complaint"),
        m("output/complaints.csv", "victim_account_id", "HI-Small_accounts.csv", int(complaints["victim_account_id"].isin(ibm_accounts).sum()), len(complaints)),
        m("output/complaints.csv", "fraud_transfer_ids", "HI-Small_Trans.csv: data row number", fr_ok, len(fr_all),
          "each is laundering-labelled, leaves the victim and belongs to the complaint's attempt"),
        m("labels/ring_devices.csv", "ring_device_id", "output/used_device.csv",
          int(rings["ring_device_id"].dropna().isin(set(ud["device_id"])).sum()), int(rings["ring_device_id"].notna().sum())),
        m("labels/ring_devices.csv", "attempt_id", "HI-Small_Patterns.txt", int(rings["attempt_id"].isin(attempts["attempt_id"]).sum()), len(rings)),
    ]
    mapping = pd.DataFrame(mapping)

    # ---- complaint overlaps (Phase 4 overlap tests) and the demo pair (Phase 3 memory test, Phase 6 demo) ----
    log("finding complaints that share accounts or devices, and the demo pair")
    overlaps, demo_pair = complaint_overlaps_and_demo_pair(truth, trail_accounts, ud, rm, demo, cfg.get("demo", {}))

    # ---- checks -----------------------------------------------------------------------
    checks = []

    def check(name, ok, detail=""):
        checks.append({"check": name, "result": "PASS" if ok else "FAIL", "detail": detail})

    check("Every synthetic key matches a real IBM record", bool(mapping["ok"].all()),
          f"{int(mapping['ok'].sum())} of {len(mapping)} mappings complete")
    check("One truth row per complaint and per IBM attempt",
          truth["complaint_id"].is_unique and truth["attempt_id"].is_unique and len(truth) == len(complaints),
          f"{len(truth)} complaints")
    on_rows = trail_transfers["transfer_id"].str[1:].astype(np.int64).to_numpy() if len(trail_transfers) else np.array([], dtype=np.int64)
    check("Every trail transfer is laundering-labelled", bool(lab[on_rows].all()), f"{len(on_rows):,} trail transfers")
    f_first = trail_transfers[trail_transfers["layer"] == 1].groupby("complaint_id")["transfer_id"].apply(set)
    f_ok = all(set(f"T{r:09d}" for r in c.fraud_rows) <= f_first.get(c.complaint_id, set())
               for c in complaints.itertuples(index=False))
    check("Every fraud transfer is on its trail at layer 1", f_ok)
    reach = trail_accounts.set_index(["complaint_id", "account_id"])["first_reached_ts"]
    tt = trail_transfers.merge(complaints[["complaint_id", "victim_account_id"]], on="complaint_id")
    tt = tt[tt["from_account_id"] != tt["victim_account_id"]]
    src_reach = reach.reindex(list(zip(tt["complaint_id"], tt["from_account_id"]))).astype("float64").to_numpy()
    late = int((tt["ts"].to_numpy() < src_reach).sum()) if len(tt) else 0
    check("Trail transfers only leave accounts the money had already reached", late == 0, f"{late} violations")
    in_labels = trail_accounts[trail_accounts["label"] != "victim"]["account_id"].isin(set(account_labels["account_id"]))
    check("Every trail account has a label", bool(in_labels.all()), f"{int((~in_labels).sum())} missing")
    check("Every complaint victim is flagged in account_labels",
          set(complaints["victim_account_id"]) <= set(account_labels.loc[account_labels["is_victim"], "account_id"]))
    check("No account is both mule and genuine", account_labels["account_id"].is_unique)
    pair_ok = len(demo_pair) == 2 and bool(demo_pair["meets_all_criteria"].all())
    check("A demo pair meets every criterion", pair_ok,
          " then ".join(demo_pair["complaint_id"]) + f" at bank {demo_pair['investigating_bank_id'].iloc[0]}"
          if len(demo_pair) else "no pair found")
    ov_ok = bool(len(overlaps) and (overlaps["complaint_a"] < overlaps["complaint_b"]).all()
                 and not overlaps.duplicated(["complaint_a", "complaint_b"]).any())
    check("Complaint overlaps list each pair once", ov_ok, f"{len(overlaps):,} pairs")

    # ---- write ------------------------------------------------------------------------
    log("writing files")
    tables = {"account_labels.csv": account_labels, "complaints_truth.csv": truth,
              "trail_accounts.csv": trail_accounts, "trail_transfers.csv": trail_transfers,
              "complaint_overlaps.csv": overlaps, "demo_pair.csv": demo_pair}
    for fname, df in tables.items():
        write_csv(df, os.path.join(lab_dir, fname))
    fingerprints = [(f"labels/{f}", len(df), sha256_file(os.path.join(lab_dir, f))) for f, df in tables.items()]
    fingerprints.append(("labels/ring_devices.csv", len(rings), sha256_file(ring_path)))
    input_hash = {k: ("not hashed" if args.skip_input_hash else sha256_file(v)) for k, v in inp.items()}

    # ---- report -----------------------------------------------------------------------
    n_fail = sum(c["result"] == "FAIL" for c in checks)
    nm = int((account_labels["label"] == "mule").sum())
    ng = int((account_labels["label"] == "genuine").sum())
    nonvictim = trail_accounts[trail_accounts["label"] != "victim"]
    L = ["# TraceHold evaluation set (ground truth)\n"]
    L.append(f"Built {gen.fmt_ist(time.time())} by `build_eval_set.py` in {time.time() - t_start:.0f} s from IBM's "
             f"three files and the synthetic overlay. {len(checks) - n_fail} of {len(checks)} checks pass. "
             f"Never load these files into the graph: they are the answers the agents are scored against.\n")
    L.append("## Files\n")
    L.append("| File | Rows | Holds |\n| --- | --- | --- |")
    L.append(f"| `labels/account_labels.csv` | {len(account_labels):,} | Every account with a laundering-labelled transfer: "
             f"{nm:,} mules, {ng:,} genuine. The other {n_acc - len(account_labels):,} accounts are genuine |")
    L.append(f"| `labels/complaints_truth.csv` | {len(truth):,} | One row per complaint: IBM attempt, split, depth, "
             f"depth bucket, trail size |")
    L.append(f"| `labels/trail_accounts.csv` | {len(trail_accounts):,} | Per complaint, every account on the true "
             f"money trail, with its layer and label |")
    L.append(f"| `labels/trail_transfers.csv` | {len(trail_transfers):,} | Per complaint, every transfer on the true "
             f"money trail |")
    L.append(f"| `labels/ring_devices.csv` | {len(rings):,} | Written by the generator: the device and phone shared "
             f"within each laundering attempt |")
    L.append(f"| `labels/complaint_overlaps.csv` | {len(overlaps):,} | Pairs of complaints whose trails share accounts "
             f"or devices (Phase 4 overlap tests) |")
    L.append(f"| `labels/demo_pair.csv` | {len(demo_pair)} | The two complaints for the memory demo: the second "
             f"recalls the first |\n")
    L.append("## Definitions\n")
    L.append(f"- **Mule:** more than {thr:.0%} of the account's outgoing value is laundering-labelled in IBM's "
             f"`Is Laundering` column. An account with no outgoing transfers is judged on its incoming value. "
             f"Every other account is **genuine**. Victims keep their label but are flagged `is_victim` and "
             f"left out of mule metrics.")
    L.append("- **True money trail:** starts at the complaint's fraud transfers and follows laundering-labelled "
             "transfers forward in time, each leaving an account the trail had already reached (same minute "
             "allowed). The victim's own transfers count only when they are the fraud transfers.")
    L.append("- **Layer:** the fewest transfers from the victim to the account along the trail (fraud transfers "
             "reach layer 1).")
    L.append(f"- **Depth:** the longest time-ordered chain of trail transfers, capped at "
             f"{cfg['trail']['max_depth']}. `depth_trail` follows every labelled transfer; `depth_attempt` only "
             f"the attempt's own transfers in `HI-Small_Patterns.txt`.")
    L.append(f"- **Depth bucket:** by `depth_trail`: shallow 1 to {sh_max}, medium {sh_max + 1} to {md_max}, "
             f"deep {md_max + 1} or more.")
    L.append(f"- **Split:** `dev` if the IBM attempt ends within the first {cfg['split']['dev_cutoff_days']} days "
             f"(before {gen.fmt_ist(cut)}), otherwise `test`. Tune thresholds on `dev` only.")
    L.append("- **Scoring:** a lien on a `mule` on the trail is correct; a lien on a `genuine` account counts as "
             "innocent money frozen; a `mule` on the trail with no lien or review is a miss.\n")
    L.append("## What the ground truth contains\n")
    L.append("| Measure | Value |\n| --- | --- |")
    L.append(f"| Complaints | {len(truth):,} ({int((truth['split'] == 'dev').sum())} dev, "
             f"{int((truth['split'] == 'test').sum())} test) |")
    for b in ("shallow", "medium", "deep"):
        bt = truth[truth["depth_bucket"] == b]
        L.append(f"| Depth bucket: {b} | {len(bt)} complaints ({int((bt['split'] == 'dev').sum())} dev, "
                 f"{int((bt['split'] == 'test').sum())} test) |")
    L.append(f"| Accounts labelled mule | {nm:,} (at {cfg['labels']['sensitivity_thresholds'][0]:.0%}: "
             f"{sens[cfg['labels']['sensitivity_thresholds'][0]]:,}; at "
             f"{cfg['labels']['sensitivity_thresholds'][1]:.0%}: {sens[cfg['labels']['sensitivity_thresholds'][1]]:,}) |")
    L.append(f"| Trail accounts per complaint (excluding the victim) | median {int(truth['trail_accounts'].median())}, "
             f"max {int(truth['trail_accounts'].max())} |")
    L.append(f"| Trail accounts that are genuine | {int((nonvictim['label'] == 'genuine').sum()):,} of "
             f"{len(nonvictim):,} |")
    L.append(f"| Trail transfers inside the IBM attempt | {int(trail_transfers['in_attempt'].sum()):,} of "
             f"{len(trail_transfers):,} |")
    L.append(f"| Complaints whose victim banks with a demo bank | {int(truth['victim_on_demo_bank'].sum())} |\n")
    dc = cfg.get("demo", {})
    L.append("## Demo pair\n")
    L.append(f"Complaint 1 is at least {dc.get('min_depth', 4)} layers deep, passes through a registered "
             f"merchant's account and disputes at most {dc.get('max_disputed_rupees', 10_000_000):,} rupees. "
             f"Complaint 2 is reported later, disputes at most the same, and its trail uses a device that complaint "
             f"1's trail uses. Both trails reach accounts at the investigating demo bank, which is complaint 1's "
             f"victim bank when possible, so that bank's memory can recall complaint 1. Ranked by shared devices, "
             f"then complaint 1's depth.\n")
    if len(demo_pair):
        L.append("| Order | Complaint | Pattern | Split | Depth | Disputed (₹) | Shared devices | Investigating bank | "
                 "Meets every criterion |\n| --- | --- | --- | --- | --- | --- | --- | --- | --- |")
        for r in demo_pair.itertuples(index=False):
            L.append(f"| {r.order} | {r.complaint_id} | {r.pattern_type} | {r.split} | {r.depth_trail} | "
                     f"{r.disputed_amount_paise / 100:,.2f} | {r.shared_devices} | {r.investigating_bank_id} | "
                     f"{'yes' if r.meets_all_criteria else 'no'} |")
        L.append("")
    L.append(f"{len(overlaps):,} pairs of complaints share trail accounts or devices "
             f"({int((overlaps['shared_accounts'] > 0).sum()):,} share accounts, "
             f"{int((overlaps['shared_devices'] > 0).sum()):,} share devices).\n")
    L.append("## Depth by pattern\n")
    L.append("| Pattern | Complaints | Median depth (trail) | Max depth (trail) | Median depth (attempt only) |\n"
             "| --- | --- | --- | --- | --- |")
    for ptype, g in truth.groupby("pattern_type"):
        L.append(f"| {ptype} | {len(g)} | {g['depth_trail'].median():.0f} | {int(g['depth_trail'].max())} | "
                 f"{g['depth_attempt'].median():.0f} |")
    L.append("")
    L.append("## How the synthetic data maps onto IBM's data\n")
    L.append("| Synthetic file | Key | Real file and key | Matched | Note |\n| --- | --- | --- | --- | --- |")
    for r in mapping.itertuples(index=False):
        L.append(f"| `{r.synthetic}` | `{r.key}` | {r.real} | {r.matched} | {r.note} |")
    L.append("\nTransfer IDs are `T` + the zero-based data row of `HI-Small_Trans.csv`; account IDs are "
             "`<Bank ID>-<Account Number>`; complaint `CMP-0007` is the 7th attempt in `HI-Small_Patterns.txt`.\n")
    L.append("## Checks\n")
    L.append("| Check | Result | Detail |\n| --- | --- | --- |")
    for c in checks:
        L.append(f"| {c['check']} | {c['result']} | {c['detail']} |")
    L.append("\n## Limits\n")
    L.append("- IBM's labels mark laundering money in general, not which complaint it came from, so a trail can pick "
             "up labelled money from another attempt that passes through the same mule. `in_attempt` marks what "
             "belongs to the complaint's own attempt.")
    L.append("- Tainted amounts are not ground truth here: the pro-rata engine calculates them. "
             "`labelled_inflow_paise` is the laundering-labelled money each trail account received, an upper bound.")
    L.append("- The held-out pattern and deep chains from generator v2 (Phase 4) are not in this set yet.\n")
    L.append("## Inputs\n")
    L.append("| File | SHA-256 |\n| --- | --- |")
    for k, v in inp.items():
        L.append(f"| `{os.path.basename(v)}` | `{input_hash[k]}` |")
    L.append("\n## File fingerprints\n")
    L.append("| File | Rows | SHA-256 |\n| --- | --- | --- |")
    for rel, n, h in fingerprints:
        L.append(f"| `{rel}` | {n:,} | `{h}` |")
    L.append("\n## Rebuild\n\nRun `python3 ../synthetic/generate_overlay.py` first (it writes `labels/ring_devices.csv`), "
             "then `python3 build_eval_set.py` from this folder. The same inputs give identical files.")
    with open(os.path.join(out_root, "REPORT.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(L) + "\n")

    log(f"done in {time.time() - t_start:.0f} s: {len(checks) - n_fail}/{len(checks)} checks pass")
    for c in checks:
        log(f"  {c['result']}  {c['check']}  {c['detail']}")


if __name__ == "__main__":
    main()
