"""The named signals in an account's signal record (the account_signals tool).

The Adjudicator cites these by name; deterministic check 6 accepts only names present (not null) in that
account's record. Kept apart from mcp_server/tools.py so the agents can import the names without importing the
graph code: an agent's sandbox has no database client and cannot reach FalkorDB.
"""
SIGNALS = ("pass_through_ratio", "median_minutes_to_forward", "distinct_payers", "distinct_payees",
           "account_age_days", "registered_merchant", "owner_kind", "inflow_count", "outflow_count",
           "tainted_share", "accounts_sharing_device", "accounts_sharing_phone", "accounts_sharing_ip",
           "devices_shared_with_known_mules", "phones_shared_with_known_mules", "related_cases", "prior_decisions",
           "mule_community", "key_account")
