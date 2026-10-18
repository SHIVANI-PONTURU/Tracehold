"""Reading and writing the pipeline's tables."""
import os

import pandas as pd

from data import store


def test_round_trip_keeps_types_and_only_one_copy(tmp_path):
    base = str(tmp_path / "t")
    df = pd.DataFrame({"a": ["x", "y"], "n": [1, 2], "ok": [True, False]})
    path = store.write_table(df, base)
    assert store.table_path(base) == path
    got = store.read_table(base)
    assert got["n"].tolist() == [1, 2] and got["ok"].tolist() == [True, False]
    assert store.read_columns(base) == ["a", "n", "ok"]
    other = base + (".csv.gz" if path.endswith(".parquet") else ".parquet")
    assert not os.path.exists(other)


def test_iter_table_yields_every_row(tmp_path):
    base = str(tmp_path / "t")
    store.write_table(pd.DataFrame({"n": range(25)}), base)
    chunks = list(store.iter_table(base, chunksize=10))
    assert sum(len(c) for c in chunks) == 25
