"""Phase 1 step 12: load the network graph in batches, indexes first.

Reads only the files and columns listed in data/contract.py (no ground truth can reach the graph):
  IBM's accounts file, work/clean/transfers, and synthetic/output/*.csv.
Graph model (implementation plan, Appendix B):
  (:Bank {id, name})                    IBM bank code and IBM's bank name
  (:Customer {id, kind, registered_merchant})   IBM owner (Entity ID); kind person or business
  (:Account {id, bank_id, opened_at, opening_balance})
  (:Device {id}) (:Phone {id}) (:IP {id})
  (:Complaint {id, victim_account_id, fraud_transfer_ids, disputed_amount, reported_at})
  (Customer)-[:OWNS]->(Account)-[:HELD_AT]->(Bank)
  (Account)-[:TRANSFERRED {id, amount, ts, format, self_loop, own_account}]->(Account)
  (Account)-[:USED_DEVICE {first_seen}]->(Device)
  (Account)-[:LOGGED_IN_FROM {first_seen}]->(IP)
  (Customer)-[:HAS_PHONE]->(Phone)
Money is in paise, times are UTC epoch seconds.

  python -m graph.load --dry-run                 build and check everything; no FalkorDB needed
  python -m graph.load --rebuild --limit 200000  quick test: first 200,000 transfers and their accounts
  python -m graph.load --rebuild                 the whole cleaned table (recommended)
  python -m graph.load --rebuild --days 5        first 5 days of transfers and their accounts
  python -m graph.load --verify-only             rerun the checks on the loaded graph

Writes work/reports/graph_load.json (or graph_load_dryrun.json) with counts, timings and checks.
"""
import argparse
import json
import os
import time

import numpy as np
import pandas as pd

from data import contract, ibm, settings, store

GRAPH = "network"
SEED = 20261015
TRANSFER_COLS = contract.LOADER_COLUMNS["transfers"]
EXPECTED_KEYS = {"id", "name", "kind", "registered_merchant", "bank_id", "opened_at", "opening_balance",
                 "amount", "ts", "format", "self_loop", "own_account", "first_seen", "victim_account_id",
                 "fraud_transfer_ids", "disputed_amount", "reported_at"}
EXPECTED_LABELS = {"Bank", "Customer", "Account", "Device", "Phone", "IP", "Complaint"}
EXPECTED_TYPES = {"OWNS", "HELD_AT", "TRANSFERRED", "USED_DEVICE", "LOGGED_IN_FROM", "HAS_PHONE"}

Q = {
    "Bank": "UNWIND $rows AS r CREATE (:Bank {id: r.id, name: r.name})",
    "Customer": "UNWIND $rows AS r CREATE (:Customer {id: r.id, kind: r.kind, registered_merchant: r.rm = 1})",
    "Account": "UNWIND $rows AS r CREATE (:Account {id: r.id, bank_id: r.bank_id, opened_at: r.opened_at, "
               "opening_balance: r.opening_balance})",
    "Device": "UNWIND $rows AS r CREATE (:Device {id: r.id})",
    "Phone": "UNWIND $rows AS r CREATE (:Phone {id: r.id})",
    "IP": "UNWIND $rows AS r CREATE (:IP {id: r.id})",
    "Complaint": "UNWIND $rows AS r CREATE (:Complaint {id: r.id, victim_account_id: r.victim, "
                 "fraud_transfer_ids: r.fraud, disputed_amount: r.disputed, reported_at: r.reported})",
    "HELD_AT": "UNWIND $rows AS r MATCH (a:Account {id: r.a}), (b:Bank {id: r.b}) CREATE (a)-[:HELD_AT]->(b)",
    "OWNS": "UNWIND $rows AS r MATCH (c:Customer {id: r.c}), (a:Account {id: r.a}) CREATE (c)-[:OWNS]->(a)",
    "USED_DEVICE": "UNWIND $rows AS r MATCH (a:Account {id: r.a}), (d:Device {id: r.d}) "
                   "CREATE (a)-[:USED_DEVICE {first_seen: r.fs}]->(d)",
    "LOGGED_IN_FROM": "UNWIND $rows AS r MATCH (a:Account {id: r.a}), (i:IP {id: r.i}) "
                      "CREATE (a)-[:LOGGED_IN_FROM {first_seen: r.fs}]->(i)",
    "HAS_PHONE": "UNWIND $rows AS r MATCH (c:Customer {id: r.c}), (p:Phone {id: r.p}) CREATE (c)-[:HAS_PHONE]->(p)",
    "TRANSFERRED": "UNWIND $rows AS r MATCH (a:Account {id: r.f}), (b:Account {id: r.t}) "
                   "CREATE (a)-[:TRANSFERRED {id: r.id, amount: r.amt, ts: r.ts, format: r.fmt, "
                   "self_loop: r.sl = 1, own_account: r.oa = 1}]->(b)",
}
NODE_ORDER = ["Bank", "Customer", "Account", "Device", "Phone", "IP", "Complaint"]
EDGE_ORDER = ["HELD_AT", "OWNS", "USED_DEVICE", "LOGGED_IN_FROM", "HAS_PHONE", "TRANSFERRED"]


class Checks:
    def __init__(self):
        self.items = []

    def add(self, name, ok, detail=""):
        self.items.append({"check": name, "result": "PASS" if ok else "FAIL", "detail": str(detail)})
        ibm.log(f"  {'PASS' if ok else 'FAIL'}  {name}  {detail}")

    @property
    def passed(self):
        return sum(c["result"] == "PASS" for c in self.items)


# ---- reading the inputs (contract columns only) ------------------------------------------
def read_input(path, name, dtype=None):
    cols = contract.LOADER_COLUMNS[name]
    header = list(pd.read_csv(path, nrows=0).columns)
    missing = [c for c in cols if c not in header]
    if missing:
        raise ValueError(f"{name} lacks the contract columns {missing}")
    if contract.forbidden(cols):
        raise ValueError(f"the contract for {name} lists ground-truth columns {contract.forbidden(cols)}")
    return pd.read_csv(path, usecols=cols, dtype=dtype)[cols]


def transfer_chunks(cutoff=None, limit=None, chunksize=500_000):
    """The cleaned transfers in time order, contract columns only, with the slice applied."""
    base = os.path.join(settings.CLEAN_DIR, "transfers")
    cols = store.read_columns(base)
    if cols is None:
        raise FileNotFoundError("work/clean/transfers is missing: run `python -m data.pipeline clean` first")
    if contract.forbidden(cols):
        raise ValueError(f"the cleaned table has ground-truth columns {contract.forbidden(cols)}")
    taken = 0
    for c in store.iter_table(base, columns=TRANSFER_COLS, chunksize=chunksize):
        c = c[TRANSFER_COLS]
        past_cutoff = cutoff is not None and int(c["ts"].max()) >= cutoff      # the table is in time order
        if cutoff is not None:
            c = c[c["ts"] < cutoff]
        if limit is not None:
            c = c.iloc[:max(limit - taken, 0)]
        if len(c):
            taken += len(c)
            yield c
        if past_cutoff or (limit is not None and taken >= limit):
            return


def to_params(c):
    """Cleaned transfer rows -> the short parameter maps the TRANSFERRED query expects."""
    return pd.DataFrame({"id": c["transfer_id"].to_numpy(), "f": c["from_account_id"].to_numpy(),
                         "t": c["to_account_id"].to_numpy(), "amt": c["amount_paise"].astype(np.int64).to_numpy(),
                         "ts": c["ts"].astype(np.int64).to_numpy(), "fmt": c["format"].to_numpy(),
                         "sl": c["self_loop"].astype(bool).astype(np.int8).to_numpy(),
                         "oa": c["own_account"].astype(bool).astype(np.int8).to_numpy()})


def prepare(limit=None, days=None):
    """Build every node and edge table except TRANSFERRED (streamed later) and check them. No database."""
    t0 = time.time()
    raw = settings.raw_paths()
    ocfg = settings.overlay_config()
    out = settings.OVERLAY_OUTPUT_DIR
    problems = []
    stats = json.load(open(os.path.join(settings.CLEAN_DIR, "stats.json"), encoding="utf-8"))
    cutoff = stats["ts_min"] + int(days) * 86400 if days else None
    subset = bool(limit or days)

    # complaints first: their fraud transfers are looked for while streaming the transfers
    comp = read_input(os.path.join(out, "complaints.csv"), "complaints.csv", dtype={"complaint_id": str,
                                                                                     "victim_account_id": str,
                                                                                     "fraud_transfer_ids": str})
    comp["fraud"] = comp["fraud_transfer_ids"].str.split(";")
    wanted = set(t for ids in comp["fraud"] for t in ids)

    ibm.log("pass over the cleaned transfers: counts, endpoints, complaint transfers")
    n_tx, touched, fraud_rows, fmt_counts = 0, set(), [], {}
    ts_lo, ts_hi = None, None
    for c in transfer_chunks(cutoff, limit):
        n_tx += len(c)
        if subset:
            touched.update(c["from_account_id"].unique())
            touched.update(c["to_account_id"].unique())
        hit = c[c["transfer_id"].isin(wanted)]
        if len(hit):
            fraud_rows.append(hit[["transfer_id", "from_account_id", "amount_paise"]])
        for k, v in c["format"].value_counts().items():
            fmt_counts[k] = fmt_counts.get(k, 0) + int(v)
        ts_lo = int(c["ts"].min()) if ts_lo is None else min(ts_lo, int(c["ts"].min()))
        ts_hi = int(c["ts"].max()) if ts_hi is None else max(ts_hi, int(c["ts"].max()))
    fraud = (pd.concat(fraud_rows) if fraud_rows else pd.DataFrame(columns=["transfer_id", "from_account_id",
                                                                             "amount_paise"]))
    fraud = fraud.set_index("transfer_id")

    # complaints whose fraud transfers are all loaded (all 370 for the whole table)
    def complaint_ok(r):
        return all(t in fraud.index for t in r.fraud)
    comp["loaded"] = [complaint_ok(r) for r in comp.itertuples(index=False)]
    if not subset and not comp["loaded"].all():
        problems.append(f"{int((~comp['loaded']).sum())} complaints reference transfers missing from the cleaned table")
    comp = comp[comp["loaded"]].copy()
    for r in comp.itertuples(index=False):
        f = fraud.loc[r.fraud]
        if (f["from_account_id"] != r.victim_account_id).any():
            problems.append(f"{r.complaint_id}: a fraud transfer does not leave the victim account")
        if int(f["amount_paise"].sum()) != int(r.disputed_amount_paise):
            problems.append(f"{r.complaint_id}: disputed amount differs from the sum of its fraud transfers")

    # accounts: all of them for the whole table, else the ones the slice touches plus complaint victims
    ov = read_input(os.path.join(out, "account_overlay.csv"), "account_overlay.csv", dtype={"account_id": str})
    if subset:
        keep = touched | set(comp["victim_account_id"])
        ov = ov[ov["account_id"].isin(keep)]
        missing = keep - set(ov["account_id"])
    else:
        missing = set()
    if missing:
        problems.append(f"{len(missing)} accounts in the transfers have no overlay row")
    if not ov["account_id"].is_unique:
        problems.append("account_overlay.csv lists an account twice")
    acc = ov.rename(columns={"account_id": "id", "opening_balance_paise": "opening_balance"}).reset_index(drop=True)
    acc["bank_id"] = acc["id"].str.split("-", n=1).str[0]
    acc_set = set(acc["id"])

    # owners and banks from IBM's accounts file
    af = read_input(raw["accounts"], "HI-Small_accounts.csv", dtype=str)
    af["bank_id"] = af["Bank ID"].astype(np.int64).astype(str)
    af["account_id"] = af["bank_id"] + "-" + af["Account Number"]
    af = af[af["account_id"].isin(acc_set)]
    owner = af.set_index("account_id")["Entity ID"]
    acc["customer_id"] = owner.reindex(acc["id"]).to_numpy()
    no_owner = acc["customer_id"].isna()
    acc.loc[no_owner, "customer_id"] = "U-" + acc.loc[no_owner, "id"]     # same rule as the overlay generator

    person = set(ocfg["customers"]["person_entity_types"])
    etype = af.drop_duplicates("Entity ID").set_index("Entity ID")["Entity Name"].str.replace(r"\s*#\d+$", "",
                                                                                              regex=True)
    merchants = set(read_input(os.path.join(out, "registered_merchants.csv"), "registered_merchants.csv",
                               dtype=str)["entity_id"])
    cust = pd.DataFrame({"id": sorted(acc["customer_id"].unique())})
    cust["kind"] = np.where(etype.reindex(cust["id"]).isin(person).to_numpy(), "person", "business")
    cust["rm"] = cust["id"].isin(merchants).astype(np.int8)

    bank_names = af.groupby("bank_id")["Bank Name"].first()
    bank = pd.DataFrame({"id": sorted(acc["bank_id"].unique(), key=int)})
    bank["name"] = bank_names.reindex(bank["id"]).fillna("Bank " + bank["id"]).to_numpy()
    if bank_names.reindex(bank["id"]).isna().any():
        problems.append("some banks have no name in the accounts file")

    dev = read_input(os.path.join(out, "used_device.csv"), "used_device.csv", dtype={"account_id": str, "device_id": str})
    ips = read_input(os.path.join(out, "logged_in_from.csv"), "logged_in_from.csv", dtype={"account_id": str, "ip_id": str})
    ph = read_input(os.path.join(out, "has_phone.csv"), "has_phone.csv", dtype=str)
    cust_set = set(cust["id"])
    if not subset:                       # whole table: every overlay row must point at a loaded account or customer
        for name, f, col, ref in (("used_device.csv", dev, "account_id", acc_set),
                                  ("logged_in_from.csv", ips, "account_id", acc_set),
                                  ("has_phone.csv", ph, "entity_id", cust_set)):
            orphans = int((~f[col].isin(ref)).sum())
            if orphans:
                problems.append(f"{name}: {orphans:,} rows point at an unknown {col}")
        if not merchants <= cust_set:
            problems.append(f"{len(merchants - cust_set)} registered merchants are not customers")
    dev = dev[dev["account_id"].isin(acc_set)]
    ips = ips[ips["account_id"].isin(acc_set)]
    ph = ph[ph["entity_id"].isin(cust_set)]
    for name, f, cols in (("used_device", dev, ["account_id", "device_id"]),
                          ("logged_in_from", ips, ["account_id", "ip_id"]),
                          ("has_phone", ph, ["entity_id", "phone_id"])):
        if f.duplicated(cols).any():
            problems.append(f"{name} lists the same pair twice")

    nodes = {
        "Bank": bank[["id", "name"]],
        "Customer": cust[["id", "kind", "rm"]],
        "Account": acc[["id", "bank_id", "opened_at", "opening_balance"]],
        "Device": pd.DataFrame({"id": sorted(dev["device_id"].unique())}),
        "Phone": pd.DataFrame({"id": sorted(ph["phone_id"].unique())}),
        "IP": pd.DataFrame({"id": sorted(ips["ip_id"].unique())}),
        "Complaint": pd.DataFrame({"id": comp["complaint_id"].to_numpy(), "victim": comp["victim_account_id"].to_numpy(),
                                   "fraud": comp["fraud"].to_numpy(),
                                   "disputed": comp["disputed_amount_paise"].astype(np.int64).to_numpy(),
                                   "reported": comp["reported_at"].astype(np.int64).to_numpy()}),
    }
    edges = {
        "HELD_AT": pd.DataFrame({"a": acc["id"], "b": acc["bank_id"]}),
        "OWNS": pd.DataFrame({"c": acc["customer_id"], "a": acc["id"]}),
        "USED_DEVICE": pd.DataFrame({"a": dev["account_id"].to_numpy(), "d": dev["device_id"].to_numpy(),
                                     "fs": dev["first_seen"].astype(np.int64).to_numpy()}),
        "LOGGED_IN_FROM": pd.DataFrame({"a": ips["account_id"].to_numpy(), "i": ips["ip_id"].to_numpy(),
                                        "fs": ips["first_seen"].astype(np.int64).to_numpy()}),
        "HAS_PHONE": pd.DataFrame({"c": ph["entity_id"].to_numpy(), "p": ph["phone_id"].to_numpy()}),
    }
    for label, f in nodes.items():
        if not f["id"].is_unique:
            problems.append(f"{label} IDs are not unique")
    if not subset and n_tx != stats["rows_out"]:
        problems.append(f"read {n_tx:,} transfers but stats.json says {stats['rows_out']:,}")
    if fmt_counts and not set(fmt_counts) <= set(stats["payment_formats"]):
        problems.append("unknown payment formats in the cleaned table")

    expected = {k: len(v) for k, v in nodes.items()}
    expected.update({k: len(v) for k, v in edges.items()})
    expected["TRANSFERRED"] = n_tx
    mode = "whole cleaned table" if not subset else ", ".join(
        x for x in [f"first {days} days" if days else "", f"first {limit:,} transfers" if limit else ""] if x)
    if stats.get("slice_days"):
        mode += f" (cleaned table already sliced to {stats['slice_days']} days)"
    ibm.log(f"prepared in {time.time() - t0:.0f} s: {mode}; " +
            ", ".join(f"{k} {v:,}" for k, v in expected.items()))
    return {"nodes": nodes, "edges": edges, "expected": expected, "problems": problems, "cutoff": cutoff,
            "limit": limit, "mode": mode, "complaints": comp, "fraud": fraud, "n_tx": n_tx,
            "ts_range": [ts_lo, ts_hi]}


# ---- loading ---------------------------------------------------------------------------
def load(prep, batch_size, rebuild):
    from graph import client, schema                      # imported here so --dry-run works without falkordb
    if not client.reachable():
        raise SystemExit(f"FalkorDB is not reachable at {settings.FALKORDB_HOST}:{settings.FALKORDB_PORT}. "
                         f"Start it with: powershell -File deploy\\falkordb.ps1 start")
    db, raw = client.connect(), client.raw_connection()
    gh = client.open_graph(db, GRAPH)
    if gh.exists():
        if not rebuild:
            raise SystemExit("graph `network` already exists; add --rebuild to delete it and load again")
        ibm.log("deleting the existing `network` graph")
        gh.delete(force=True)
    t0 = time.time()
    paused = pause_background_saves(raw)
    try:
        ibm.log("creating indexes")
        steps = {"indexes": schema.create_indexes(gh)}
        timings = {}
        for label in NODE_ORDER:
            timings[label] = gh.write_batches(Q[label], prep["nodes"][label], batch_size, label)
        for rel in EDGE_ORDER[:-1]:
            timings[rel] = gh.write_batches(Q[rel], prep["edges"][rel], batch_size, rel)
        frames = (to_params(c) for c in transfer_chunks(prep["cutoff"], prep["limit"]))
        timings["TRANSFERRED"] = gh.write_batches(Q["TRANSFERRED"], frames, batch_size, "TRANSFERRED",
                                                  total=prep["n_tx"])
        ibm.log("creating unique constraints and waiting for them to build")
        steps["constraints"] = schema.create_constraints(gh, raw)
        schema.wait_for_constraints(gh)
    except BaseException:
        restore_save_settings(raw, paused)          # the load failed: put the settings back, nothing to compact
        raise
    load_seconds = time.time() - t0
    return db, raw, gh, timings, steps, load_seconds, paused


# ---- background saves ------------------------------------------------------------------
# Every background save (an RDB snapshot or an AOF rewrite) forks the server, and every page that changes while
# the fork runs is copied. After a large load FalkorDB still holds pending matrix changes; saving settles them,
# so a fork at that moment copies most of the graph (over 4.5 GB in testing, on top of a 4.8 GB server): no
# 8 GB machine survives that. Reads alone do not settle them. So background saves stay paused through the load
# and the checks; then one synchronous SAVE (no fork) settles everything, one AOF rewrite compacts the log with
# little copying (about 0.6 GB in testing) and the loader waits for it, and the settings are put back. The SAVE
# also resets the change counter, so restoring the save rules does not start another fork.
def _config(raw, key):
    d = raw.config_get(key)
    v = d.get(key, d.get(key.encode()))
    return v.decode() if isinstance(v, bytes) else v


def _persistence(raw):
    info = raw.info("persistence")
    return {(k.decode() if isinstance(k, bytes) else k): (v.decode() if isinstance(v, bytes) else v)
            for k, v in info.items()}


def pause_background_saves(raw):
    """Stop RDB snapshots and AOF rewrites; every write still goes to the append-only log.

    Returns the settings to put back, or None if the server refused CONFIG (the load still works).
    """
    try:
        prev = {key: _config(raw, key) for key in ("save", "auto-aof-rewrite-percentage")}
        raw.config_set("save", "")
        raw.config_set("auto-aof-rewrite-percentage", 0)
        ibm.log(f"paused background saves until the checks finish (save '{prev['save']}', "
                f"AOF rewrite at {prev['auto-aof-rewrite-percentage']}%)")
        return prev
    except Exception as e:
        ibm.log(f"could not pause background saves ({e}); continuing")
        return None


def restore_save_settings(raw, prev):
    if not prev:
        return
    try:
        for key, v in prev.items():
            raw.config_set(key, v)
    except Exception as e:
        ibm.log(f"could not restore the save settings ({e}); set them by hand: {prev}")


def _wait_for_fork(raw, what, timeout):
    t0, last = time.time(), time.time()
    while True:
        p = _persistence(raw)
        busy = int(p.get("aof_rewrite_in_progress", 0)) or int(p.get("aof_rewrite_scheduled", 0)) \
            or int(p.get("rdb_bgsave_in_progress", 0))
        if not busy:
            return p
        if time.time() - t0 > timeout:
            raise TimeoutError(f"{what} still running after {timeout} s")
        if time.time() - last >= 30:
            ibm.log(f"  {what} still running ({time.time() - t0:.0f} s, "
                    f"copied so far {int(p.get('current_cow_size', 0)) / 1e6:,.0f} MB)")
            last = time.time()
        time.sleep(1)


def finish_background_saves(raw, prev, timeout=1800):
    """After the checks: save once without forking, compact the AOF once with the server idle, restore rules."""
    out = {}
    if prev is None:
        return {"note": "background saves were not paused"}
    try:
        _wait_for_fork(raw, "a background save", timeout)
        ibm.log("saving a snapshot without forking (FalkorDB pauses for about a minute at most)")
        t0 = time.time()
        raw.save()
        out["save_seconds"] = round(time.time() - t0, 1)
        ibm.log(f"saved in {out['save_seconds']:.0f} s")
        if _config(raw, "appendonly") == "yes":
            ibm.log("compacting the append-only log (one AOF rewrite; nothing else should use FalkorDB now)")
            t0 = time.time()
            raw.bgrewriteaof()
            p = _wait_for_fork(raw, "the AOF rewrite", timeout)
            out["aof_rewrite"] = {"status": p.get("aof_last_bgrewrite_status"),
                                  "seconds": round(time.time() - t0, 1),
                                  "copy_on_write_mb": round(int(p.get("aof_last_cow_size", 0)) / 1e6)}
            ibm.log(f"AOF rewrite {out['aof_rewrite']['status']} in {out['aof_rewrite']['seconds']:.0f} s, "
                    f"{out['aof_rewrite']['copy_on_write_mb']:,} MB copied during the fork")
    except Exception as e:
        out["error"] = str(e)
        ibm.log(f"could not compact or save ({e}); the append-only log still holds every write")
    finally:
        restore_save_settings(raw, prev)
        out["restored"] = prev
        ibm.log(f"restored the save settings: {prev}")
    return out


# ---- verifying -------------------------------------------------------------------------

def memory_report(raw):
    out = {}
    try:
        usage = raw.execute_command("GRAPH.MEMORY", "USAGE", GRAPH)
        if isinstance(usage, (list, tuple)) and len(usage) % 2 == 0:
            out["graph"] = {(k.decode() if isinstance(k, bytes) else str(k)): (v.decode() if isinstance(v, bytes) else v)
                            for k, v in zip(usage[::2], usage[1::2])}
        else:
            out["graph"] = str(usage)
    except Exception as e:
        out["graph"] = f"not available: {e}"
    info = raw.info("memory")
    out["used_memory"] = info.get("used_memory_human", b"").decode() if isinstance(info.get("used_memory_human"), bytes) \
        else info.get("used_memory_human")
    out["peak_memory"] = info.get("used_memory_peak_human", b"").decode() \
        if isinstance(info.get("used_memory_peak_human"), bytes) else info.get("used_memory_peak_human")
    return out


def verify(prep, gh, raw, checks):
    from graph import schema
    counts = {}
    for label in NODE_ORDER:
        counts[label] = {"graph": int(gh.value(f"MATCH (n:{label}) RETURN count(n)")), "expected": prep["expected"][label]}
    for rel in EDGE_ORDER:
        counts[rel] = {"graph": int(gh.value(f"MATCH ()-[r:{rel}]->() RETURN count(r)")), "expected": prep["expected"][rel]}
    bad = {k: v for k, v in counts.items() if v["graph"] != v["expected"]}
    checks.add("Node and edge counts match the cleaned slice", not bad, bad or "all 13 match")
    total_nodes = int(gh.value("MATCH (n) RETURN count(n)"))
    checks.add("No nodes outside the seven labels", total_nodes == sum(counts[k]["graph"] for k in NODE_ORDER),
               f"{total_nodes:,} nodes")
    out_degree = int(gh.value("MATCH (n) RETURN sum(outdegree(n))"))
    n_edges = sum(counts[k]["graph"] for k in EDGE_ORDER)
    checks.add("Total out-degree equals the edge count", out_degree == n_edges,
               f"{out_degree:,} out-degree, {n_edges:,} edges")

    # three random transfers against the cleaned table
    rng = np.random.default_rng(SEED)
    picks = set(int(x) for x in rng.choice(prep["n_tx"], size=min(3, prep["n_tx"]), replace=False))
    found, pos = [], 0
    for c in transfer_chunks(prep["cutoff"], prep["limit"]):
        for p in sorted(picks):
            if pos <= p < pos + len(c):
                found.append(c.iloc[p - pos])
        pos += len(c)
    ok_all, detail = True, []
    for r in found:
        got = gh.rows("MATCH (a:Account {id: $f})-[t:TRANSFERRED]->(b:Account) WHERE t.id = $id "
                      "RETURN b.id, t.amount, t.ts, t.format, t.self_loop, t.own_account",
                      {"f": r["from_account_id"], "id": r["transfer_id"]})
        want = [r["to_account_id"], int(r["amount_paise"]), int(r["ts"]), r["format"], bool(r["self_loop"]),
                bool(r["own_account"])]
        ok = len(got) == 1 and list(got[0]) == want
        ok_all &= ok
        detail.append(f"{r['transfer_id']} {'matches' if ok else f'differs: graph {got} vs table {want}'}")
    checks.add("Three random transfers match the cleaned table", ok_all, "; ".join(detail))

    # three random accounts against the inputs
    acc = prep["nodes"]["Account"]
    owns = prep["edges"]["OWNS"].set_index("a")["c"]
    cust = prep["nodes"]["Customer"].set_index("id")
    banks = prep["nodes"]["Bank"].set_index("id")["name"]
    ok_all, detail = True, []
    for i in rng.choice(len(acc), size=min(3, len(acc)), replace=False):
        a = acc.iloc[int(i)]
        got = gh.rows("MATCH (c:Customer)-[:OWNS]->(a:Account {id: $id})-[:HELD_AT]->(b:Bank) "
                      "RETURN c.id, c.kind, c.registered_merchant, a.bank_id, a.opened_at, a.opening_balance, b.name",
                      {"id": a["id"]})
        cid = owns[a["id"]]
        want = [cid, cust.loc[cid, "kind"], bool(cust.loc[cid, "rm"]), a["bank_id"], int(a["opened_at"]),
                int(a["opening_balance"]), banks[a["bank_id"]]]
        ok = len(got) == 1 and list(got[0]) == want
        ok_all &= ok
        detail.append(f"{a['id']} {'matches' if ok else f'differs: graph {got} vs inputs {want}'}")
    checks.add("Three random accounts match their owner, bank and overlay", ok_all, "; ".join(detail))

    n_bad = gh.value("MATCH (a:Account) OPTIONAL MATCH (a)-[h:HELD_AT]->(:Bank) WITH a, count(h) AS n "
                     "WHERE n <> 1 RETURN count(a)")
    checks.add("Every account is held at exactly one bank", n_bad == 0, f"{n_bad} accounts break it")
    n_bad = gh.value("MATCH (a:Account) OPTIONAL MATCH (c:Customer)-[o:OWNS]->(a) WITH a, count(o) AS n "
                     "WHERE n <> 1 RETURN count(a)")
    checks.add("Every account has exactly one owner", n_bad == 0, f"{n_bad} accounts break it")

    comp = prep["complaints"]
    rows = [{"c": r.complaint_id, "v": r.victim_account_id, "f": list(r.fraud), "d": int(r.disputed_amount_paise)}
            for r in comp.itertuples(index=False)]
    if rows:
        same = gh.rows("UNWIND $rows AS r MATCH (c:Complaint {id: r.c}) WHERE c.victim_account_id = r.v AND "
                       "c.fraud_transfer_ids = r.f AND c.disputed_amount = r.d RETURN count(c)", {"rows": rows})[0][0]
        # one row per fraud transfer, found through the TRANSFERRED.id index (matching the victim first and
        # filtering its edges makes FalkorDB scan every TRANSFERRED edge per complaint)
        frows = [{"c": r["c"], "t": t} for r in rows for t in r["f"]]
        found = gh.rows("UNWIND $rows AS r MATCH (a:Account)-[t:TRANSFERRED {id: r.t}]->() "
                        "RETURN r.c, r.t, a.id, t.amount", {"rows": frows})
        by_c = {}
        for cid, tid, src, amt in found:
            by_c.setdefault(cid, []).append((tid, src, amt))
        sums = 0
        for r in rows:
            got = by_c.get(r["c"], [])
            if (sorted(t for t, _, _ in got) == sorted(r["f"]) and all(s == r["v"] for _, s, _ in got)
                    and sum(a for _, _, a in got) == r["d"]):
                sums += 1
    else:
        same = sums = 0
    checks.add("Complaint nodes match complaints.csv", same == len(rows), f"{same} of {len(rows)}")
    checks.add("Each complaint's fraud transfers leave its victim and add up to the disputed amount",
               sums == len(rows), f"{sums} of {len(rows)}")

    keys = {r[0] for r in gh.rows("CALL db.propertyKeys() YIELD propertyKey RETURN propertyKey")}
    labels = {r[0] for r in gh.rows("CALL db.labels() YIELD label RETURN label")}
    types = {r[0] for r in gh.rows("CALL db.relationshipTypes() YIELD relationshipType RETURN relationshipType")}
    leaked = contract.forbidden(sorted(keys | labels | types))
    checks.add("No ground-truth words in any property, label or relationship name", not leaked, leaked or sorted(keys))
    checks.add("Exactly the planned labels, relationship types and properties",
               labels == EXPECTED_LABELS and types == EXPECTED_TYPES and keys == EXPECTED_KEYS,
               f"extra {sorted((keys | labels | types) - EXPECTED_KEYS - EXPECTED_LABELS - EXPECTED_TYPES)}, "
               f"missing {sorted((EXPECTED_KEYS | EXPECTED_LABELS | EXPECTED_TYPES) - keys - labels - types)}")

    idx = schema.index_status(gh)
    have = {(i["label"], p) for i in idx for p in i["properties"]}
    want = {(lb, "id") for lb in NODE_ORDER} | set(schema.EDGE_INDEXES)
    checks.add("Indexes on every label's id and on TRANSFERRED.id and .ts", want <= have, f"missing {sorted(want - have)}")
    cons = schema.constraint_status(gh)
    ok = len(cons) == len(NODE_ORDER) and all(c["status"] == "OPERATIONAL" for c in cons)
    checks.add("Unique constraints on every label's id are operational", ok,
               ", ".join(f"{c['label']} {c['status']}" for c in cons))
    return counts


def run(args):
    t0 = time.time()
    prep = prepare(args.limit, args.days)
    checks = Checks()
    checks.add("Inputs are consistent (endpoints, owners, complaints, duplicates)", not prep["problems"],
               "; ".join(prep["problems"]) or "no problems")
    settings.ensure_dirs(settings.REPORT_DIR)
    report = {"graph": GRAPH, "mode": prep["mode"], "load_args": {"limit": args.limit, "days": args.days},
              "batch_size": args.batch_size,
              "transfer_time_range_ist": [ibm.fmt_ist(x) for x in prep["ts_range"]] if prep["ts_range"][0] else None}
    if args.dry_run:
        first = {k: v.head(2).to_dict("records") for k, v in {**prep["nodes"], **prep["edges"]}.items()}
        first["TRANSFERRED"] = to_params(next(transfer_chunks(prep["cutoff"], 2))).to_dict("records")
        report.update({"dry_run": True, "expected": prep["expected"], "queries": Q,
                       "first_rows": json.loads(json.dumps(first, default=str)),
                       "seconds": round(time.time() - t0, 1), "passed": checks.passed, "total": len(checks.items),
                       "checks": checks.items})
        path = os.path.join(settings.REPORT_DIR, "graph_load_dryrun.json")
    else:
        if prep["problems"]:
            raise SystemExit("inputs are inconsistent, nothing loaded: " + "; ".join(prep["problems"]))
        if args.verify_only:
            from graph import client
            if not client.reachable():
                raise SystemExit(f"FalkorDB is not reachable at {settings.FALKORDB_HOST}:{settings.FALKORDB_PORT}.")
            db, raw = client.connect(), client.raw_connection()
            gh = client.open_graph(db, GRAPH)
            if not gh.exists():
                raise SystemExit("there is no `network` graph to verify: run `python -m graph.load --rebuild`")
            previous = os.path.join(settings.REPORT_DIR, "graph_load.json")
            old = json.load(open(previous, encoding="utf-8")) if os.path.exists(previous) else {}
            load_s, timings, steps = old.get("seconds"), old.get("timings"), old.get("schema")
            report["verify_only"] = True
        else:
            db, raw, gh, timings, steps, load_s, paused = load(prep, args.batch_size, args.rebuild)
        from graph import client
        gh.timeout_ms = client.LONG_QUERY_MS       # the checks scan the whole graph: not the 1-second default
        report["server_limits"] = client.server_limits(raw)
        ibm.log("verifying the graph")
        try:
            counts = verify(prep, gh, raw, checks)
        except Exception as e:                 # record it as a failed check and still write the report
            counts = None
            checks.add("The checks ran to the end", False, f"stopped by {type(e).__name__}: {e}")
        finally:
            if not args.verify_only:
                report["background_saves"] = finish_background_saves(raw, paused)
        if not args.verify_only and paused is not None:
            bs = report["background_saves"]
            rw = bs.get("aof_rewrite", {"status": "ok"})
            checks.add("FalkorDB saved the graph to disk and compacted its log",
                       "error" not in bs and rw.get("status") == "ok",
                       bs.get("error") or f"save {bs.get('save_seconds')} s; AOF rewrite {rw.get('status')}, "
                                          f"{rw.get('copy_on_write_mb', 0):,} MB copied")
        report.update({"seconds": round(load_s, 1) if load_s is not None else None,
                       "total_seconds": round(time.time() - t0, 1),
                       "memory": memory_report(raw), "counts": counts, "timings": timings, "schema": steps,
                       "passed": checks.passed, "total": len(checks.items), "checks": checks.items})
        mem = report["memory"]
        report["memory_summary"] = f"server {mem.get('used_memory')} (peak {mem.get('peak_memory')})"
        path = os.path.join(settings.REPORT_DIR, "graph_load.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, default=str)
    ibm.log(f"{'dry run' if args.dry_run else 'load'}: {checks.passed} of {len(checks.items)} checks pass "
            f"-> {os.path.relpath(path, settings.ROOT)}")
    return report


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--rebuild", action="store_true", help="delete an existing `network` graph first")
    ap.add_argument("--batch-size", type=int, default=10_000, help="rows per UNWIND batch (default 10,000)")
    ap.add_argument("--limit", type=int, default=None, help="load only the first N transfers and their accounts")
    ap.add_argument("--days", type=int, default=None, help="load only the first N days of transfers")
    ap.add_argument("--dry-run", action="store_true", help="prepare and check the inputs; do not touch FalkorDB")
    ap.add_argument("--verify-only", action="store_true", help="rerun the checks on the loaded graph; load nothing")
    r = run(ap.parse_args())
    raise SystemExit(0 if r["passed"] == r["total"] else 1)


if __name__ == "__main__":
    main()
