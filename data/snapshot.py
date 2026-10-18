"""Phase 1 step 13: snapshot and restore, so the same graph comes back in minutes (after a wipe, or on EC2).

save     delete leftover test graphs, DUMP the `network` graph and gzip it, copy the cleaned table, the
         synthetic overlay and IBM's accounts file next to it, archive the evaluation set separately
         (eval_labels.zip), and write a manifest with counts, image, seed and SHA-256 hashes, all in
         work/snapshot/.
restore  check the hashes, RESTORE the graph, put the cleaned table, overlay and evaluation set back
         where they are missing (never overwriting), and check every node and edge count against the
         manifest.
restore --rebuild
         the implementation plan's fallback when RESTORE does not fit in memory: load the graph with the
         loader from the snapshot's own files (about 10 minutes), then the same count check.

  python -m data.snapshot save
  python -m data.snapshot restore            (add --force to replace an existing `network` graph)
  python -m data.snapshot restore --rebuild  (add --force to replace an existing `network` graph)

Copy the whole work/snapshot folder to the other machine's TraceHold/work/snapshot first.
"""
import argparse
import glob
import gzip
import hashlib
import json
import os
import shutil
import time
import zipfile

from data import ibm, settings, store

GRAPH = "network"
DUMP_FILE = "network.dump.gz"
MANIFEST = "manifest.json"
LABELS_ZIP = "eval_labels.zip"          # ground truth, kept apart from everything the loader reads
NODE_LABELS = ["Bank", "Customer", "Account", "Device", "Phone", "IP", "Complaint"]
EDGE_TYPES = ["HELD_AT", "OWNS", "USED_DEVICE", "LOGGED_IN_FROM", "HAS_PHONE", "TRANSFERRED"]
CLEAN_FILES = ["stats.json", "dropped.csv"]


def graph_counts(gh):
    c = {lb: int(gh.value(f"MATCH (n:{lb}) RETURN count(n)")) for lb in NODE_LABELS}
    c.update({t: int(gh.value(f"MATCH ()-[r:{t}]->() RETURN count(r)")) for t in EDGE_TYPES})
    return c


def _config_int(raw, key):
    d = raw.config_get(key)
    v = d.get(key, d.get(key.encode()))
    return int(v.decode() if isinstance(v, bytes) else v)


def _connect():
    from graph import client                     # needs the falkordb package
    if not client.reachable():
        raise SystemExit(f"FalkorDB is not reachable at {settings.FALKORDB_HOST}:{settings.FALKORDB_PORT}. "
                         f"Start it with: powershell -File deploy\\falkordb.ps1 start")
    db, raw = client.connect(), client.raw_connection()
    return client.open_graph(db, GRAPH, timeout_ms=client.LONG_QUERY_MS), raw


def _copy_overlay_and_labels(snap, files):
    """The overlay (what the loader read) and, as a separate archive, the evaluation set."""
    ov_dst = os.path.join(snap, "overlay")
    settings.ensure_dirs(ov_dst)
    srcs = sorted(glob.glob(os.path.join(settings.OVERLAY_OUTPUT_DIR, "*.csv")))
    srcs.append(os.path.join(settings.SYNTHETIC_DIR, "fx_rates.csv"))
    for src in srcs:
        dst = os.path.join(ov_dst, os.path.basename(src))
        shutil.copyfile(src, dst)
        files[f"overlay/{os.path.basename(src)}"] = ibm.sha256_file(dst)
    for stale in os.listdir(ov_dst):
        if f"overlay/{stale}" not in files:
            os.remove(os.path.join(ov_dst, stale))
    # IBM's accounts file: the loader reads its banks and owners, and the exploration notebook its owners
    ibm_dst = os.path.join(snap, "ibm")
    settings.ensure_dirs(ibm_dst)
    acc_src = settings.raw_paths()["accounts"]
    shutil.copyfile(acc_src, os.path.join(ibm_dst, os.path.basename(acc_src)))
    files[f"ibm/{os.path.basename(acc_src)}"] = ibm.sha256_file(os.path.join(ibm_dst, os.path.basename(acc_src)))
    zpath = os.path.join(snap, LABELS_ZIP)
    with zipfile.ZipFile(zpath, "w", compression=zipfile.ZIP_DEFLATED) as z:
        for src in sorted(glob.glob(os.path.join(settings.LABELS_DIR, "*.csv"))):
            info = zipfile.ZipInfo(f"labels/{os.path.basename(src)}", date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            with open(src, "rb") as f:
                z.writestr(info, f.read())
    files[LABELS_ZIP] = ibm.sha256_file(zpath)


def put_back_missing_files(snap, manifest):
    """Copy the snapshot's files to where the pipeline keeps them, only where nothing is there yet."""
    targets = {"clean/": settings.CLEAN_DIR, "overlay/": settings.OVERLAY_OUTPUT_DIR,
               "ibm/": os.path.dirname(settings.raw_paths()["accounts"])}
    for name in manifest["files_sha256"]:
        for prefix, target in targets.items():
            if not name.startswith(prefix):
                continue
            base = name[len(prefix):]
            dest_dir = settings.SYNTHETIC_DIR if base == "fx_rates.csv" else target
            dest = os.path.join(dest_dir, base)
            if not os.path.exists(dest):
                settings.ensure_dirs(dest_dir)
                shutil.copyfile(os.path.join(snap, name), dest)
                ibm.log(f"copied {name} to {os.path.relpath(dest, settings.ROOT)}")
    zpath = os.path.join(snap, LABELS_ZIP)
    if os.path.exists(zpath):
        settings.ensure_dirs(settings.LABELS_DIR)
        with zipfile.ZipFile(zpath) as z:
            for member in z.namelist():
                dest = os.path.join(settings.LABELS_DIR, os.path.basename(member))
                if not os.path.exists(dest):
                    with open(dest, "wb") as f:
                        f.write(z.read(member))
                    ibm.log(f"copied {member} from {LABELS_ZIP} to eval/labels/")


def _wait_for_memory_to_free(raw, timeout=180):
    """GRAPH.DELETE frees a large graph in the background: wait until the server's memory stops falling,
    then hand the freed pages back to the system, so the restore does not stack on top of the old graph."""
    t0, last = time.time(), None
    while time.time() - t0 < timeout:
        used = int(raw.info("memory")["used_memory"])
        if last is not None and used >= last * 0.99:
            break
        last = used
        time.sleep(3)
    try:
        raw.execute_command("MEMORY", "PURGE")
    except Exception:
        pass
    info = raw.info("memory")
    ibm.log(f"old graph freed: server now uses {int(info['used_memory']) / 1e9:.1f} GB "
            f"({int(info['used_memory_rss']) / 1e9:.1f} GB resident)")


def _delete_test_graphs(db):
    from graph import client
    gone = []
    for name in client.graph_names(db):
        if name.startswith(("test_", "scratch_")):
            client.open_graph(db, name).delete()
            gone.append(name)
    return gone


def save():
    t0 = time.time()
    gh, raw = _connect()
    if not gh.exists():
        raise SystemExit("there is no `network` graph to save: run `python -m graph.load --rebuild` first")
    gone = _delete_test_graphs(gh.db)
    if gone:
        ibm.log(f"deleted leftover test graphs: {', '.join(gone)}")
    snap = settings.SNAPSHOT_DIR
    settings.ensure_dirs(snap, os.path.join(snap, "clean"))
    counts = graph_counts(gh)
    ibm.log(f"dumping `network` ({counts['Account']:,} accounts, {counts['TRANSFERRED']:,} transfers)")
    payload = raw.dump(GRAPH)
    digest = hashlib.sha256(payload).hexdigest()
    with gzip.open(os.path.join(snap, DUMP_FILE), "wb", compresslevel=6) as f:
        f.write(payload)
    ibm.log(f"dump {len(payload) / 1e6:,.0f} MB -> {DUMP_FILE} "
            f"({os.path.getsize(os.path.join(snap, DUMP_FILE)) / 1e6:,.0f} MB compressed)")

    files = {}
    src_table = store.table_path(os.path.join(settings.CLEAN_DIR, "transfers"))
    for src in [src_table] + [os.path.join(settings.CLEAN_DIR, f) for f in CLEAN_FILES]:
        dst = os.path.join(snap, "clean", os.path.basename(src))
        shutil.copyfile(src, dst)
        files[f"clean/{os.path.basename(src)}"] = ibm.sha256_file(dst)
    for stale in os.listdir(os.path.join(snap, "clean")):
        if f"clean/{stale}" not in files:
            os.remove(os.path.join(snap, "clean", stale))
    files[DUMP_FILE] = ibm.sha256_file(os.path.join(snap, DUMP_FILE))
    _copy_overlay_and_labels(snap, files)

    from graph import client, engine_check
    module = client.module_version(raw)
    image = engine_check.image_info()
    load_report = os.path.join(settings.REPORT_DIR, "graph_load.json")
    mode = json.load(open(load_report, encoding="utf-8")).get("mode") if os.path.exists(load_report) else None
    load_args = (json.load(open(load_report, encoding="utf-8")).get("load_args")
                 if os.path.exists(load_report) else None)
    stats = json.load(open(os.path.join(settings.CLEAN_DIR, "stats.json"), encoding="utf-8"))
    manifest = {"created_ist": ibm.fmt_ist(time.time()), "graph": GRAPH, "module_version": module,
                "image": image.get("FALKORDB_IMAGE"), "image_digest": image.get("FALKORDB_DIGEST"), "mode": mode,
                "load_args": load_args,
                "seed": settings.overlay_config()["seed"], "inputs_sha256": stats.get("inputs_sha256"),
                "rate_table_sha256": files.get("overlay/fx_rates.csv"),
                "dump_bytes": len(payload), "dump_sha256": digest, "counts": counts, "files_sha256": files}
    with open(os.path.join(snap, MANIFEST), "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)
    ibm.log(f"snapshot saved in {time.time() - t0:.0f} s -> work/snapshot/ "
            f"(copy that folder to restore on another machine)")
    return manifest


def _rebuild_from_snapshot_files(snap, manifest):
    """Run the loader on the snapshot's own files (not the local ones), as the snapshot was loaded."""
    import argparse as _ap
    from graph import load as loader
    accounts = os.path.join(snap, next(n for n in manifest["files_sha256"] if n.startswith("ibm/")))
    keep = (settings.CLEAN_DIR, settings.OVERLAY_OUTPUT_DIR, settings.raw_paths)
    original_raw_paths = settings.raw_paths
    settings.CLEAN_DIR = os.path.join(snap, "clean")
    settings.OVERLAY_OUTPUT_DIR = os.path.join(snap, "overlay")
    settings.raw_paths = lambda: {**original_raw_paths(), "accounts": accounts}
    la = manifest.get("load_args") or {}
    try:
        ibm.log("loading the graph from the snapshot's files (the loader, about 10 minutes for the whole table)")
        r = loader.run(_ap.Namespace(rebuild=True, batch_size=10_000, limit=la.get("limit"), days=la.get("days"),
                                     dry_run=False, verify_only=False))
    finally:
        settings.CLEAN_DIR, settings.OVERLAY_OUTPUT_DIR, settings.raw_paths = keep
    if r["passed"] != r["total"]:
        raise SystemExit(f"the loader's checks failed ({r['total'] - r['passed']} of {r['total']}); "
                         f"see work/reports/graph_load.json")


def restore(force=False, rebuild=False):
    t0 = time.time()
    snap = settings.SNAPSHOT_DIR
    with open(os.path.join(snap, MANIFEST), encoding="utf-8") as f:
        manifest = json.load(f)
    for name, want in manifest["files_sha256"].items():
        if ibm.sha256_file(os.path.join(snap, name)) != want:
            raise SystemExit(f"{name} does not match the manifest: the snapshot is damaged or incomplete")

    gh, raw = _connect()
    if gh.exists() and not force:
        raise SystemExit("graph `network` already exists; add --force to replace it with the snapshot")
    if rebuild:
        _rebuild_from_snapshot_files(snap, manifest)
        gh, raw = _connect()
    else:
        with gzip.open(os.path.join(snap, DUMP_FILE), "rb") as f:
            payload = f.read()
        if hashlib.sha256(payload).hexdigest() != manifest["dump_sha256"]:
            raise SystemExit("the dump does not match the manifest")
        if gh.exists():
            # delete first and wait for the memory to come back: restoring over it would hold two graphs at once
            ibm.log("deleting the existing `network` graph before restoring")
            gh.delete(force=True)
            _wait_for_memory_to_free(raw)
        # a large dump is one Redis command: raise the size limits for the restore, then put them back
        need = int(len(payload) * 1.1) + (1 << 20)
        saved = {}
        for key in ("client-query-buffer-limit", "proto-max-bulk-len"):      # the buffer limit must go up first
            cur = _config_int(raw, key)
            if cur < need:
                saved[key] = cur
                raw.config_set(key, need)
        try:
            ibm.log(f"restoring {len(payload) / 1e6:,.0f} MB into `network` "
                    f"(if FalkorDB runs out of memory, use: python -m data.snapshot restore --rebuild)")
            raw.restore(GRAPH, 0, payload, replace=True)
        finally:
            del payload
            for key in ("proto-max-bulk-len", "client-query-buffer-limit"):  # and come down last
                if key in saved:
                    raw.config_set(key, saved[key])

    put_back_missing_files(snap, manifest)

    counts = graph_counts(gh)
    bad = {k: {"graph": counts[k], "manifest": v} for k, v in manifest["counts"].items() if counts.get(k) != v}
    result = {"restored_ist": ibm.fmt_ist(time.time()), "method": "loader" if rebuild else "RESTORE",
              "seconds": round(time.time() - t0, 1), "snapshot_created_ist": manifest["created_ist"],
              "counts": counts, "mismatches": bad, "result": "PASS" if not bad else "FAIL"}
    try:
        info = raw.info("memory")
        result["server_memory"] = {"used_gb": round(int(info["used_memory"]) / 1e9, 2),
                                   "peak_gb": round(int(info["used_memory_peak"]) / 1e9, 2),
                                   "resident_gb": round(int(info["used_memory_rss"]) / 1e9, 2)}
    except Exception:
        pass
    settings.ensure_dirs(settings.REPORT_DIR)
    with open(os.path.join(settings.REPORT_DIR, "snapshot_restore.json"), "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2)
    ibm.log(f"restore {'PASS' if not bad else 'FAIL'} in {result['seconds']:.0f} s: "
            + (", ".join(f"{k} {v:,}" for k, v in counts.items()) if not bad else f"mismatches {bad}"))
    return result


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("action", choices=["save", "restore"])
    ap.add_argument("--force", action="store_true", help="restore: replace an existing `network` graph")
    ap.add_argument("--rebuild", action="store_true",
                    help="restore: load the graph from the snapshot's files with the loader instead of RESTORE")
    a = ap.parse_args()
    if a.action == "save":
        save()
    else:
        raise SystemExit(0 if restore(a.force, a.rebuild)["result"] == "PASS" else 1)


if __name__ == "__main__":
    main()
