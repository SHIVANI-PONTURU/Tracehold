"""Phase 1 step 6: validate IBM's raw files and report every violation with a count.

Reads the three files as text (nothing is converted yet) and checks each column against the rules below.
Writes work/reports/validation_raw.csv; violations do not stop the pipeline (the clean stage drops or
flags them with a reason code), but the clean stage's own stricter check must pass with zero violations.

  python -m data.validate
"""
import argparse
import json
import os
import time
from collections import defaultdict

import numpy as np
import pandas as pd

from data import ibm, settings

KNOWN_CURRENCIES = {"Australian Dollar", "Bitcoin", "Brazil Real", "Canadian Dollar", "Euro", "Mexican Peso",
                    "Ruble", "Rupee", "Saudi Riyal", "Shekel", "Swiss Franc", "UK Pound", "US Dollar", "Yen",
                    "Yuan"}
KNOWN_FORMATS = {"ACH", "Bitcoin", "Cash", "Cheque", "Credit Card", "Reinvestment", "Wire"}
TS_RE = r"^\d{4}/\d{2}/\d{2} \d{2}:\d{2}$"
DIGITS_RE = r"^\d+$"
HEX_RE = r"^[0-9A-F]+$"
MAX_EXAMPLES = 5


class Tally:
    def __init__(self):
        self.counts = defaultdict(int)
        self.examples = defaultdict(list)

    def flag(self, file, rule, column, mask, rows):
        mask = np.asarray(mask, dtype=bool)
        k = int(mask.sum())
        if not k:
            return
        key = (file, rule, column)
        self.counts[key] += k
        room = MAX_EXAMPLES - len(self.examples[key])
        if room > 0:
            self.examples[key].extend(int(x) for x in rows[mask][:room])

    def table(self):
        rows = [{"file": f, "rule": r, "column": c, "count": n,
                 "example_rows": " ".join(str(x) for x in self.examples[(f, r, c)])}
                for (f, r, c), n in sorted(self.counts.items())]
        return pd.DataFrame(rows, columns=["file", "rule", "column", "count", "example_rows"])


def validate_transactions(path, tally, chunksize=1_000_000):
    name = os.path.basename(path)
    n = 0
    for c in pd.read_csv(path, names=ibm.TX_COLS, header=0, dtype=str, chunksize=chunksize, keep_default_na=False):
        rows = np.arange(n, n + len(c))
        for col in ibm.TX_COLS:
            tally.flag(name, "V01 empty value", col, c[col].str.strip() == "", rows)
        ts_ok = c["Timestamp"].str.match(TS_RE)
        tally.flag(name, "V02 timestamp not YYYY/MM/DD HH:MM", "Timestamp", ~ts_ok, rows)
        parsed = pd.to_datetime(c["Timestamp"].where(ts_ok), format="%Y/%m/%d %H:%M", errors="coerce")
        tally.flag(name, "V03 timestamp is not a real date", "Timestamp", ts_ok & parsed.isna(), rows)
        for col in ("From Bank", "To Bank"):
            tally.flag(name, "V04 bank code not numeric", col, ~c[col].str.match(DIGITS_RE), rows)
        for col in ("From Account", "To Account"):
            tally.flag(name, "V05 account number not hexadecimal", col, ~c[col].str.match(HEX_RE), rows)
        for col in ("Amount Paid", "Amount Received"):
            v = pd.to_numeric(c[col], errors="coerce")
            tally.flag(name, "V06 amount not a number", col, v.isna(), rows)
            tally.flag(name, "V07 amount not positive", col, v.notna() & (v <= 0), rows)
        for col in ("Payment Currency", "Receiving Currency"):
            tally.flag(name, "V08 unknown currency", col, ~c[col].isin(KNOWN_CURRENCIES), rows)
        tally.flag(name, "V09 unknown payment format", "Payment Format", ~c["Payment Format"].isin(KNOWN_FORMATS), rows)
        tally.flag(name, "V10 label not 0 or 1", "Is Laundering", ~c["Is Laundering"].isin(["0", "1"]), rows)
        n += len(c)
        ibm.log(f"validated {n:,} transfers")
    return n


def validate_accounts(path, tally):
    name = os.path.basename(path)
    a = pd.read_csv(path, dtype=str, keep_default_na=False)
    rows = np.arange(len(a))
    for col in a.columns:
        tally.flag(name, "V01 empty value", col, a[col].str.strip() == "", rows)
    tally.flag(name, "V04 bank code not numeric", "Bank ID", ~a["Bank ID"].str.match(DIGITS_RE), rows)
    tally.flag(name, "V05 account number not hexadecimal", "Account Number", ~a["Account Number"].str.match(HEX_RE), rows)
    tally.flag(name, "V11 owner ID not hexadecimal", "Entity ID", ~a["Entity ID"].str.match(HEX_RE), rows)
    key = a["Bank ID"].str.lstrip("0") + "-" + a["Account Number"]
    tally.flag(name, "V12 account listed twice", "Bank ID + Account Number", key.duplicated(keep=False), rows)
    return len(a)


def validate_patterns(path, tally):
    name = os.path.basename(path)
    try:
        attempts, prows = ibm.read_patterns(path)
    except ValueError as e:                     # a malformed line stops the parser; report it as one violation
        tally.flag(name, "V13 malformed line", "line", np.array([True]), np.array([0]))
        ibm.log(f"patterns file: {e}")
        return 0, 0
    rows = prows["line_no"].to_numpy()
    tally.flag(name, "V09 unknown payment format", "Payment Format", ~prows["Payment Format"].isin(KNOWN_FORMATS), rows)
    tally.flag(name, "V10 label not 0 or 1", "Is Laundering", ~prows["Is Laundering"].isin(["0", "1"]), rows)
    return len(attempts), len(prows)


def data_lines(path):
    """Non-empty lines after the header, counted from the bytes (the row-count check for raw ingest)."""
    n = 0
    with open(path, "rb") as f:
        next(f, None)
        for line in f:
            if line.strip():
                n += 1
    return n


def run():
    t0 = time.time()
    settings.ensure_dirs(settings.REPORT_DIR)
    raw = settings.raw_paths()
    tally = Tally()
    n_tx = validate_transactions(raw["transactions"], tally)
    n_acc = validate_accounts(raw["accounts"], tally)
    n_att, n_prow = validate_patterns(raw["patterns"], tally)
    table = tally.table()
    out = os.path.join(settings.REPORT_DIR, "validation_raw.csv")
    table.to_csv(out, index=False, lineterminator="\n")
    row_counts = {os.path.basename(raw[k]): {"lines_in_file": data_lines(raw[k]), "rows_read": n}
                  for k, n in (("transactions", n_tx), ("accounts", n_acc))}
    summary = {"transfers": n_tx, "accounts": n_acc, "attempts": n_att, "pattern_rows": n_prow,
               "row_counts": row_counts,
               "row_counts_match": all(v["lines_in_file"] == v["rows_read"] for v in row_counts.values()),
               "violations": int(table["count"].sum()) if len(table) else 0, "rules_broken": len(table)}
    with open(os.path.join(settings.REPORT_DIR, "validation_summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)
    if not summary["row_counts_match"]:
        raise RuntimeError(f"rows read differ from lines in the file: {row_counts}")
    ibm.log(f"validation done in {time.time() - t0:.0f} s: {summary['violations']:,} violations "
            f"across {summary['rules_broken']} rules -> {os.path.relpath(out, settings.ROOT)}")
    for r in table.itertuples(index=False):
        ibm.log(f"  {r.file}  {r.rule}  [{r.column}]  {r.count:,}")
    return summary


def main():
    argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter).parse_args()
    run()


if __name__ == "__main__":
    main()
