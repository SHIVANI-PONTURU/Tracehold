"""Reading and writing the pipeline's tables.

Parquet when pyarrow (or fastparquet) is installed, otherwise gzip CSV. Readers accept either, so a
table written on one machine can be read on another.
"""
import os

import pandas as pd


def parquet_available():
    for mod in ("pyarrow", "fastparquet"):
        try:
            __import__(mod)
            return True
        except ImportError:
            continue
    return False


def table_path(base):
    """The existing file for a table base path (without extension), or None."""
    for ext in (".parquet", ".csv.gz"):
        if os.path.exists(base + ext):
            return base + ext
    return None


def write_table(df, base):
    """Write df to base.parquet (or base.csv.gz) and remove the other format so only one copy exists."""
    if parquet_available():
        path, other = base + ".parquet", base + ".csv.gz"
        df.to_parquet(path, index=False)
    else:
        path, other = base + ".csv.gz", base + ".parquet"
        df.to_csv(path, index=False, lineterminator="\n",
                  compression={"method": "gzip", "compresslevel": 1, "mtime": 0})
    if os.path.exists(other):
        os.remove(other)
    return path


def read_table(base, columns=None, dtype=None):
    path = table_path(base)
    if path is None:
        raise FileNotFoundError(f"no table at {base}.parquet or {base}.csv.gz; run the stage that writes it")
    if path.endswith(".parquet"):
        return pd.read_parquet(path, columns=columns)
    return pd.read_csv(path, usecols=columns, dtype=dtype)


def iter_table(base, columns=None, dtype=None, chunksize=500_000):
    """Yield the table in chunks of about `chunksize` rows, so a 5M-row table never sits in memory twice."""
    path = table_path(base)
    if path is None:
        raise FileNotFoundError(f"no table at {base}.parquet or {base}.csv.gz; run the stage that writes it")
    if path.endswith(".parquet"):
        import pyarrow.parquet as pq
        for batch in pq.ParquetFile(path).iter_batches(batch_size=chunksize, columns=columns):
            yield batch.to_pandas()
    else:
        yield from pd.read_csv(path, usecols=columns, dtype=dtype, chunksize=chunksize)


def read_columns(base):
    path = table_path(base)
    if path is None:
        return None
    if path.endswith(".parquet"):
        return list(pd.read_parquet(path).columns) if not _has_pyarrow() else _parquet_columns(path)
    return list(pd.read_csv(path, nrows=0).columns)


def _has_pyarrow():
    try:
        import pyarrow  # noqa: F401
        return True
    except ImportError:
        return False


def _parquet_columns(path):
    import pyarrow.parquet as pq
    return list(pq.read_schema(path).names)
