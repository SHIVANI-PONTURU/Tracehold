"""The pandera schema for the cleaned transfers table (Phase 1 steps 6-7).

clean.check_clean always runs the same rules in plain pandas; when pandera is installed (it is in
requirements.txt) this schema runs too, and the cleaned table must pass both with zero violations.
"""
KNOWN_FORMATS = ["ACH", "Bitcoin", "Cash", "Cheque", "Credit Card", "Reinvestment", "Wire"]
ACCOUNT_RE = r"^\d+-[0-9A-F]+$"
TRANSFER_RE = r"^T\d{9}$"


def _pandera():
    try:
        import pandera.pandas as pa          # pandera 0.24 and later
    except ImportError:
        try:
            import pandera as pa
        except ImportError:
            return None
    return pa


def available():
    return _pandera() is not None


def schema(ts_min, ts_max):
    pa = _pandera()
    return pa.DataFrameSchema(
        {
            "transfer_id": pa.Column(str, pa.Check.str_matches(TRANSFER_RE), unique=True),
            "from_account_id": pa.Column(str, pa.Check.str_matches(ACCOUNT_RE)),
            "to_account_id": pa.Column(str, pa.Check.str_matches(ACCOUNT_RE)),
            "amount_paise": pa.Column("int64", pa.Check.ge(1)),
            "ts": pa.Column("int64", pa.Check.in_range(ts_min, ts_max)),
            "format": pa.Column(str, pa.Check.isin(KNOWN_FORMATS)),
            "self_loop": pa.Column(bool),
            "own_account": pa.Column(bool),
            "dup_group": pa.Column("int64", pa.Check.ge(-1)),
            "fx_mismatch": pa.Column(bool),
        },
        strict=True, ordered=True, coerce=False,
    )


def problems(table, ts_min, ts_max):
    """Violations as readable strings; None when pandera is not installed."""
    pa = _pandera()
    if pa is None:
        return None
    try:
        schema(ts_min, ts_max).validate(table, lazy=True)
        return []
    except pa.errors.SchemaErrors as e:
        fc = e.failure_cases
        summary = fc.groupby(["column", "check"], dropna=False).size()
        return [f"pandera: {col} failed {chk} ({n} rows)" for (col, chk), n in summary.items()]


def version():
    pa = _pandera()
    if pa is None:
        return None
    import pandera
    return pandera.__version__
