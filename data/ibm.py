"""Readers for IBM's HI-Small files, shared by the pipeline, the overlay generator and the
evaluation builder.

IDs (implementation plan, Phase 1 decisions):
  account ID  = "<bank code without leading zeros>-<account number>"
  transfer ID = "T" + zero-based data row of HI-Small_Trans.csv, zero-padded to 9 digits
Times are read as IST wall-clock and returned as UTC epoch seconds.
"""
import hashlib
import time

import numpy as np
import pandas as pd

TX_COLS = ["Timestamp", "From Bank", "From Account", "To Bank", "To Account",
           "Amount Received", "Receiving Currency", "Amount Paid",
           "Payment Currency", "Payment Format", "Is Laundering"]
MATCH_KEY = ["ts", "src", "dst", "paid", "pcur", "recv", "rcur", "fmt"]


def transfer_id(row):
    return f"T{int(row):09d}"


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 22), b""):
            h.update(block)
    return h.hexdigest()


def ibm_time_to_epoch(values, offset_minutes):
    """IBM 'YYYY/MM/DD HH:MM' read as wall-clock time at the given UTC offset -> UTC epoch seconds."""
    dt = pd.to_datetime(pd.Index(list(values), dtype=object), format="%Y/%m/%d %H:%M")
    return dt.values.astype("datetime64[s]").astype(np.int64) - offset_minutes * 60


def fmt_ist(epoch):
    return pd.Timestamp(int(epoch) + 19800, unit="s").strftime("%Y-%m-%d %H:%M IST")


class AccountRegistry:
    """Maps '<bank code without leading zeros>-<account number>' to a dense integer index."""

    def __init__(self):
        self.index, self.ids, self.bank = {}, [], []

    def get(self, bank_int, acct):
        aid = f"{bank_int}-{acct}"
        j = self.index.get(aid)
        if j is None:
            j = len(self.ids)
            self.index[aid] = j
            self.ids.append(aid)
            self.bank.append(bank_int)
        return j

    def lookup(self, bank_int, acct):
        return self.index.get(f"{bank_int}-{acct}", -1)


def _codes(series):
    codes = series.cat.codes.to_numpy()
    if (codes < 0).any():
        raise ValueError(f"empty values in column '{series.name}'")
    return codes


def chunk_accounts(bank_s, acct_s, reg):
    bank_codes = _codes(bank_s).astype(np.int64)
    acct_codes = _codes(acct_s).astype(np.int64)
    bank_lut = [int(c) for c in bank_s.cat.categories]
    acct_cats = acct_s.cat.categories
    keys = (bank_codes << 32) | acct_codes
    uniq, inv = np.unique(keys, return_inverse=True)
    idx = np.fromiter((reg.get(bank_lut[k >> 32], acct_cats[k & 0xFFFFFFFF]) for k in uniq.tolist()),
                      dtype=np.int64, count=len(uniq))
    return idx[inv.reshape(-1)].astype(np.int32)


def chunk_names(series, names):
    lut = np.array([names.setdefault(c, len(names)) for c in series.cat.categories], dtype=np.int16)
    return lut[_codes(series)]


def read_transactions(path, limit_rows, offset):
    cats = ["Timestamp", "From Bank", "From Account", "To Bank", "To Account",
            "Receiving Currency", "Payment Currency", "Payment Format"]
    dtypes = {c: "category" for c in cats}
    dtypes.update({"Amount Received": "float64", "Amount Paid": "float64", "Is Laundering": "int8"})
    reg, cur_names, fmt_names = AccountRegistry(), {}, {}
    parts = {k: [] for k in ("ts", "src", "dst", "paid", "recv", "pcur", "rcur", "fmt", "lab")}
    n = 0
    reader = pd.read_csv(path, names=TX_COLS, header=0, dtype=dtypes, chunksize=1_000_000,
                         nrows=limit_rows, float_precision="round_trip")
    for chunk in reader:
        ts_lut = ibm_time_to_epoch(chunk["Timestamp"].cat.categories, offset)
        parts["ts"].append(ts_lut[_codes(chunk["Timestamp"])])
        parts["src"].append(chunk_accounts(chunk["From Bank"], chunk["From Account"], reg))
        parts["dst"].append(chunk_accounts(chunk["To Bank"], chunk["To Account"], reg))
        parts["paid"].append(chunk["Amount Paid"].to_numpy())
        parts["recv"].append(chunk["Amount Received"].to_numpy())
        parts["pcur"].append(chunk_names(chunk["Payment Currency"], cur_names))
        parts["rcur"].append(chunk_names(chunk["Receiving Currency"], cur_names))
        parts["fmt"].append(chunk_names(chunk["Payment Format"], fmt_names))
        parts["lab"].append(chunk["Is Laundering"].to_numpy())
        n += len(chunk)
        log(f"read {n:,} transfers")
    tx = {k: np.concatenate(v) for k, v in parts.items()}
    if np.isnan(tx["paid"]).any() or np.isnan(tx["recv"]).any():
        raise ValueError("empty amounts in the transactions file")
    tx["row"] = np.arange(n, dtype=np.int64)
    return tx, reg, list(cur_names), list(fmt_names)


def read_patterns(path):
    attempts, rows, current = [], [], None
    with open(path, encoding="utf-8") as f:
        for line_no, raw in enumerate(f, 1):
            line = raw.strip()
            if not line:
                continue
            if line.startswith("BEGIN LAUNDERING ATTEMPT"):
                head = line.split(" - ", 1)[1] if " - " in line else ""
                ptype, _, detail = head.partition(":")
                current = len(attempts) + 1
                attempts.append({"attempt_id": current, "pattern_type": ptype.strip(),
                                 "detail": detail.strip()})
            elif line.startswith("END LAUNDERING ATTEMPT"):
                current = None
            else:
                fields = line.split(",")
                if current is None or len(fields) != len(TX_COLS):
                    raise ValueError(f"unexpected line {line_no} in the patterns file: {line[:80]}")
                rows.append([current, line_no] + fields)
    return pd.DataFrame(attempts), pd.DataFrame(rows, columns=["attempt_id", "line_no"] + TX_COLS)


def match_patterns(prows, tx, reg, cur_names, fmt_names, offset):
    """Match each pattern row to its transfer on every field; identical rows are assigned in order."""
    cur = {c: i for i, c in enumerate(cur_names)}
    fmt = {c: i for i, c in enumerate(fmt_names)}
    p = pd.DataFrame({
        "attempt_id": prows["attempt_id"].to_numpy(),
        "line_no": prows["line_no"].to_numpy(),
        "ts": ibm_time_to_epoch(prows["Timestamp"], offset),
        "src": [reg.lookup(int(b), a) for b, a in zip(prows["From Bank"], prows["From Account"])],
        "dst": [reg.lookup(int(b), a) for b, a in zip(prows["To Bank"], prows["To Account"])],
        "paid": prows["Amount Paid"].astype(float).to_numpy(),
        "pcur": prows["Payment Currency"].map(lambda c: cur.get(c, -1)).to_numpy(),
        "recv": prows["Amount Received"].astype(float).to_numpy(),
        "rcur": prows["Receiving Currency"].map(lambda c: cur.get(c, -1)).to_numpy(),
        "fmt": prows["Payment Format"].map(lambda c: fmt.get(c, -1)).to_numpy(),
    })
    cmask = np.isin(tx["ts"], np.unique(p["ts"].to_numpy()))
    cand = pd.DataFrame({k: tx[k][cmask] for k in MATCH_KEY + ["row"]})
    for k in ("src", "dst", "pcur", "rcur", "fmt"):
        cand[k] = cand[k].astype(np.int64)
        p[k] = p[k].astype(np.int64)
    cand = cand.sort_values("row", kind="stable")
    cand["occ"] = cand.groupby(MATCH_KEY, sort=False).cumcount()
    n_tx = cand.groupby(MATCH_KEY, sort=False).size().rename("n_tx").reset_index()
    p = p.sort_values("line_no", kind="stable")
    p["p_occ"] = p.groupby(MATCH_KEY, sort=False).cumcount()
    p = p.merge(n_tx, on=MATCH_KEY, how="left")
    p["n_tx"] = p["n_tx"].fillna(0).astype(np.int64)
    p["occ"] = np.where(p["n_tx"] > 0, p["p_occ"] % p["n_tx"].clip(lower=1), -1)
    p = p.merge(cand[MATCH_KEY + ["occ", "row"]], on=MATCH_KEY + ["occ"], how="left")
    p["status"] = np.select([p["row"].isna(), p["p_occ"] >= p["n_tx"], p["n_tx"] > 1],
                            ["unmatched", "matched_shared", "matched_duplicate"], default="matched")
    p["row"] = p["row"].fillna(-1).astype(np.int64)
    return p.sort_values("line_no", kind="stable").reset_index(drop=True)


def derive_rates(tx, cur_names, usd_name, rupee_name):
    """Rate table from IBM's own cross-currency transfers: median implied rate per currency, via US dollars."""
    names = list(cur_names)
    if usd_name not in names or rupee_name not in names:
        raise ValueError(f"'{usd_name}' and '{rupee_name}' must both appear in the transfers")
    usd = names.index(usd_name)
    pc, rc = tx["pcur"].astype(np.int64), tx["rcur"].astype(np.int64)
    cross = (pc != rc) & (tx["paid"] > 0) & (tx["recv"] > 0)
    pc, rc = pc[cross], rc[cross]
    ratio = tx["recv"][cross] / tx["paid"][cross]          # receiving units per paying unit
    usd_per, samples = {usd: 1.0}, {usd: np.array([1.0])}
    for c in range(len(names)):
        if c == usd:
            continue
        v = np.concatenate([ratio[(pc == c) & (rc == usd)], 1.0 / ratio[(pc == usd) & (rc == c)]])
        if len(v):
            usd_per[c], samples[c] = float(np.median(v)), v
    changed = True
    while changed:                                          # chain through currencies already priced
        changed = False
        for c in range(len(names)):
            if c in usd_per:
                continue
            vs = []
            for k, kv in list(usd_per.items()):
                vs.append(ratio[(pc == c) & (rc == k)] * kv)
                vs.append(kv / ratio[(pc == k) & (rc == c)])
            v = np.concatenate(vs)
            if len(v):
                usd_per[c], samples[c], changed = float(np.median(v)), v, True
    missing = [names[c] for c in range(len(names)) if c not in usd_per]
    if missing:
        raise ValueError(f"no exchange rate could be derived for {missing}")
    inr_per_usd = 1.0 / usd_per[names.index(rupee_name)]
    rows = []
    for c, name in enumerate(names):
        rel = samples[c] / usd_per[c]
        rows.append({"currency": name, "usd_per_unit": usd_per[c],
                     "paise_per_unit": usd_per[c] * inr_per_usd * 100.0,
                     "quotes": 0 if c == usd else int(len(samples[c])),
                     "spread_p1": float(np.percentile(rel, 1)), "spread_p99": float(np.percentile(rel, 99))})
    return pd.DataFrame(rows), inr_per_usd
