"""Phase 4: the ground truth the evaluation scores against, read from eval/labels (never loaded into the graph).

- complaints(): one row per complaint (split, pattern, depth bucket, victim, disputed amount, report time).
- account_labels(): IBM-derived label per account; an account missing from the file is genuine (eval/REPORT.md).
- trail_mules(): per complaint, the mules on its true money trail (the accounts a method should catch).
"""
from __future__ import annotations

import csv
import os

from data import settings

INTS = ("disputed_paise", "disputed_amount_paise", "reported_at", "depth_trail", "depth_attempt", "trail_accounts",
        "trail_mules", "trail_genuine", "n_fraud_transfers")


def _rows(name, labels_dir=None):
    with open(os.path.join(labels_dir or settings.LABELS_DIR, name), newline="", encoding="utf-8") as f:
        yield from csv.DictReader(f)


def complaints(labels_dir=None):
    out = {}
    for r in _rows("complaints_truth.csv", labels_dir):
        for k in INTS:
            if r.get(k) not in (None, ""):
                r[k] = int(r[k])
        out[r["complaint_id"]] = r
    return out


def account_labels(labels_dir=None):
    """{account_id: "mule" | "genuine"} for the accounts in the file (victims keep their own label)."""
    return {r["account_id"]: r["label"] for r in _rows("account_labels.csv", labels_dir)}


def label_of(labels, account_id):
    return labels.get(account_id, "genuine")


def trail_mules(labels_dir=None):
    """{complaint_id: set of mule account IDs on its true money trail} (the victim, layer 0, left out)."""
    out = {}
    for r in _rows("trail_accounts.csv", labels_dir):
        out.setdefault(r["complaint_id"], set())
        if r["label"] == "mule" and r["layer"] not in ("", "0"):
            out[r["complaint_id"]].add(r["account_id"])
    return out
