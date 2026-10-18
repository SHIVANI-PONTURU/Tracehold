"""Phase 1 data contract: what the graph loader may read, and the words that must never reach it.

Kept in code so the loader, the generator and the leak tests share one definition (see CONTRACT.md).
"""

# Column names containing any of these words are ground truth and must never reach the graph.
FORBIDDEN_COLUMN_WORDS = ("label", "laundering", "attempt", "pattern", "depth", "role", "ring")

# Columns the graph loader reads, per input file. Anything else in those files is ignored.
LOADER_COLUMNS = {
    "HI-Small_accounts.csv": ["Bank Name", "Bank ID", "Account Number", "Entity ID", "Entity Name"],
    "transfers": ["transfer_id", "from_account_id", "to_account_id", "amount_paise", "ts", "format",
                  "self_loop", "own_account"],
    "account_overlay.csv": ["account_id", "opened_at", "opening_balance_paise"],
    "registered_merchants.csv": ["entity_id"],
    "used_device.csv": ["account_id", "device_id", "first_seen"],
    "has_phone.csv": ["entity_id", "phone_id"],
    "logged_in_from.csv": ["account_id", "ip_id", "first_seen"],
    "complaints.csv": ["complaint_id", "victim_account_id", "fraud_transfer_ids", "disputed_amount_paise",
                       "reported_at"],
}


def forbidden(columns):
    """Return the columns whose names contain a forbidden word."""
    return [c for c in columns if any(w in c.lower() for w in FORBIDDEN_COLUMN_WORDS)]
