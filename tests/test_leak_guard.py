"""Phase 1 step 9: ground truth can never reach a file or column the graph loader reads."""
import glob
import os

import pandas as pd
import pytest

from data import contract, settings, store

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_forbidden_words_are_detected():
    assert contract.forbidden(["transfer_id", "is_laundering", "attempt_id", "Pattern Type", "amount"]) == \
        ["is_laundering", "attempt_id", "Pattern Type"]
    assert contract.forbidden(["account_id", "opening_balance_paise"]) == []


def test_the_loader_contract_lists_no_forbidden_column():
    for name, cols in contract.LOADER_COLUMNS.items():
        assert contract.forbidden(cols) == [], name


def test_a_label_column_in_the_cleaned_table_stops_the_loader(mini_project):
    """Proof the guard works: put a forbidden column into a fixture the loader reads."""
    from graph import load
    os.makedirs(settings.CLEAN_DIR, exist_ok=True)
    bad = pd.DataFrame({"transfer_id": ["T000000000"], "from_account_id": ["10-800000001"],
                        "to_account_id": ["20-800000003"], "amount_paise": [1], "ts": [1], "format": ["ACH"],
                        "self_loop": [False], "own_account": [False], "is_laundering": [1]})
    store.write_table(bad, os.path.join(settings.CLEAN_DIR, "transfers"))
    with pytest.raises(ValueError, match="ground-truth"):
        list(load.transfer_chunks())


def test_a_label_column_in_an_overlay_file_never_reaches_the_graph(tmp_path):
    from graph import load
    path = tmp_path / "complaints.csv"
    pd.DataFrame({"complaint_id": ["CMP-0001"], "victim_account_id": ["10-800000001"],
                  "fraud_transfer_ids": ["T000000000"], "disputed_amount_paise": [1], "reported_at": [1],
                  "attempt_id": [1], "pattern_type": ["STACK"]}).to_csv(path, index=False)
    got = load.read_input(str(path), "complaints.csv")
    assert contract.forbidden(got.columns) == []
    assert list(got.columns) == contract.LOADER_COLUMNS["complaints.csv"]


def test_graph_code_never_reads_the_evaluation_set():
    for path in glob.glob(os.path.join(ROOT, "graph", "*.py")):
        text = open(path, encoding="utf-8").read()
        for needle in ("LABELS_DIR", "eval/labels", "eval\\\\labels", "build_eval_set", "account_labels",
                       "complaints_truth", "trail_accounts", "ring_devices"):
            assert needle not in text, f"{os.path.basename(path)} mentions {needle}"


@pytest.mark.data
def test_files_the_loader_reads_have_no_ground_truth_columns():
    cols = store.read_columns(os.path.join(settings.CLEAN_DIR, "transfers"))
    assert cols and contract.forbidden(cols) == []
    for name in contract.LOADER_COLUMNS:
        if name.endswith(".csv") and name != "HI-Small_accounts.csv":
            header = list(pd.read_csv(os.path.join(settings.OVERLAY_OUTPUT_DIR, name), nrows=0).columns)
            assert contract.forbidden(header) == [], name
