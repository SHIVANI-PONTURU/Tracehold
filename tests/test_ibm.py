"""IDs, time conversion and pattern parsing (implementation plan, Phase 1 decisions)."""
import os

import numpy as np

from data import ibm
from tests.conftest import FIXTURES


def test_transfer_id_is_T_plus_nine_digit_row():
    assert ibm.transfer_id(0) == "T000000000"
    assert ibm.transfer_id(123) == "T000000123"
    assert ibm.transfer_id(5_078_344) == "T005078344"


def test_ist_wall_clock_becomes_utc_epoch_seconds():
    # 2022-09-01 00:00 IST is 2022-08-31 18:30 UTC
    got = ibm.ibm_time_to_epoch(["2022/09/01 00:00", "2022/09/01 00:05"], 330)
    assert list(got) == [1661970600, 1661970900]


def test_account_id_drops_leading_zeros_of_the_bank_code():
    reg = ibm.AccountRegistry()
    a = reg.get(int("010"), "8000EBD30")
    b = reg.get(int("10"), "8000EBD30")
    assert a == b and reg.ids[a] == "10-8000EBD30"
    assert reg.lookup(int("0010"), "8000EBD30") == a
    assert reg.lookup(10, "FFFFFFFFF") == -1


def test_patterns_file_parses_attempts_and_rows():
    attempts, rows = ibm.read_patterns(os.path.join(FIXTURES, "HI-Small_Patterns.txt"))
    assert list(attempts["pattern_type"]) == ["STACK"]
    assert attempts["detail"].iloc[0] == "Max 2-degree Stack"
    assert len(rows) == 2 and set(rows["attempt_id"]) == {1}


def test_pattern_rows_match_their_transfers():
    tx, reg, cur, fmt = ibm.read_transactions(os.path.join(FIXTURES, "HI-Small_Trans.csv"), None, 330)
    attempts, prows = ibm.read_patterns(os.path.join(FIXTURES, "HI-Small_Patterns.txt"))
    pm = ibm.match_patterns(prows, tx, reg, cur, fmt, 330)
    assert list(pm["row"]) == [3, 4]
    assert set(pm["status"]) == {"matched"}


def test_identical_transfers_are_matched_in_order():
    tx, reg, cur, fmt = ibm.read_transactions(os.path.join(FIXTURES, "HI-Small_Trans.csv"), None, 330)
    attempts, prows = ibm.read_patterns(os.path.join(FIXTURES, "HI-Small_Patterns.txt"))
    dup = prows.iloc[[0]].copy()
    line = "2022/09/01 00:50,020,800000003,010,800000002,5.00,US Dollar,5.00,US Dollar,Cash,0".split(",")
    dup.iloc[0, 2:] = line
    two = dup.loc[dup.index.repeat(2)].reset_index(drop=True)
    two["line_no"] = [10, 11]
    pm = ibm.match_patterns(two, tx, reg, cur, fmt, 330)
    assert list(pm["row"]) == [6, 7]
    assert set(pm["status"]) == {"matched_duplicate"}


def test_rate_table_from_cross_currency_transfers():
    tx, reg, cur, fmt = ibm.read_transactions(os.path.join(FIXTURES, "HI-Small_Trans.csv"), None, 330)
    fx, inr_per_usd = ibm.derive_rates(tx, cur, "US Dollar", "Rupee")
    ppu = fx.set_index("currency")["paise_per_unit"]
    assert np.isclose(ppu["Rupee"], 100.0)                 # a rupee is 100 paise, by construction
    assert np.isclose(inr_per_usd, np.median([7344.40 / 100.00, 1 / (1.00 / 73.44)]))
    assert np.isclose(ppu["US Dollar"], inr_per_usd * 100)
