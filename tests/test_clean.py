"""Phase 1 steps 6-7: validation and cleaning on a 10-transfer fixture with known answers."""
import json
import os

import numpy as np
import pandas as pd
import pytest

from data import clean, clean_schema, settings, store, validate


@pytest.fixture
def cleaned(mini_project):
    stats = clean.run()
    table = store.read_table(os.path.join(settings.CLEAN_DIR, "transfers"))
    return stats, table.set_index("transfer_id")


def test_validation_counts_every_violation(mini_project):
    path = os.path.join(mini_project, "HI-Small_Trans.csv")
    with open(path, "a", encoding="utf-8") as f:
        f.write("2022/13/40 00:00,0X1,8000000ZZ,020,800000003,-5,US Dollar,abc,Peso,Barter,7\n")
    summary = validate.run()
    v = pd.read_csv(os.path.join(settings.REPORT_DIR, "validation_raw.csv"))
    rules = set(v["rule"])
    for rule in ("V03 timestamp is not a real date", "V04 bank code not numeric", "V05 account number not hexadecimal",
                 "V06 amount not a number", "V07 amount not positive", "V08 unknown currency",
                 "V09 unknown payment format", "V10 label not 0 or 1"):
        assert rule in rules, rule
    assert summary["row_counts_match"] and summary["transfers"] == 11


def test_clean_keeps_every_row_and_sorts_by_time(cleaned):
    stats, t = cleaned
    assert stats["rows_in"] == stats["rows_out"] == 10 and stats["rows_dropped"] == 0
    assert list(t.index[:2]) == ["T000000001", "T000000000"]          # 00:01 comes before 00:05
    assert t["ts"].is_monotonic_increasing


def test_reason_codes_flag_the_right_rows(cleaned):
    stats, t = cleaned
    assert list(t.index[t["self_loop"]]) == ["T000000001"]             # C07
    assert list(t.index[t["own_account"]]) == ["T000000002"]           # same owner E1, two accounts
    assert set(t.index[t["dup_group"] >= 0]) == {"T000000006", "T000000007"}   # C06
    assert set(t.loc[["T000000006", "T000000007"], "dup_group"]) == {6}
    assert list(t.index[t["fx_mismatch"]]) == ["T000000008"]           # C04: 20 EUR received for 10 USD
    codes = {r["code"]: r["rows"] for r in stats["rules"]}
    assert codes["C04"] == 1 and codes["C06"] == 2 and codes["C07"] == 1 and codes["C05"] == codes["C08"] == 0


def test_ids_and_amounts(cleaned):
    stats, t = cleaned
    assert t.loc["T000000000", "from_account_id"] == "10-800000001"    # bank code 010 -> 10
    assert t.loc["T000000000", "to_account_id"] == "20-800000003"
    assert t.loc["T000000004", "amount_paise"] == 7344                   # ₹73.44 is 7,344 paise
    assert t.loc["T000000000", "ts"] == 1661970600 + 5 * 60
    assert t["amount_paise"].dtype == np.int64 and (t["amount_paise"] >= 1).all()


def test_clean_table_has_no_ground_truth_and_passes_both_schemas(cleaned):
    stats, t = cleaned
    table = t.reset_index()
    assert "is_laundering" not in [c.lower() for c in table.columns]
    assert clean.check_clean(table, stats["ts_min"], stats["ts_max"]) == []
    assert clean_schema.problems(table, stats["ts_min"], stats["ts_max"]) == []


def test_clean_schema_rejects_a_label_column(cleaned):
    stats, t = cleaned
    bad = t.reset_index()
    bad["is_laundering"] = 0
    assert any("ground-truth" in p for p in clean.check_clean(bad, stats["ts_min"], stats["ts_max"]))
    assert clean_schema.problems(bad, stats["ts_min"], stats["ts_max"])      # pandera: unexpected column


def test_a_time_slice_reconciles_with_C09(mini_project):
    # the fixture spans 70 minutes, so a 1-day slice keeps every row; the totals must still reconcile
    stats = clean.run(days=1)
    codes = {r["code"]: r["rows"] for r in stats["rules"]}
    assert stats["rows_in"] - codes["C05"] - codes["C08"] - codes["C09"] == stats["rows_out"]


def test_stats_record_patterns_and_labels(cleaned):
    stats, _ = cleaned
    assert stats["attempts"] == 1 and stats["pattern_rows_matched"] == 2
    assert stats["laundering_transfers"] == 2
    with open(os.path.join(settings.CLEAN_DIR, "stats.json"), encoding="utf-8") as f:
        assert json.load(f)["rows_out"] == 10
