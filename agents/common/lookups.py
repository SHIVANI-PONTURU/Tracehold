"""Read tools the model may call itself during a decision, as LangChain tools over the agent's MCP path.

bank_id and investigation_id are filled in by code: the model only names the account(s).
"""
from __future__ import annotations

import json

from pydantic import BaseModel, Field


class OneAccount(BaseModel):
    account_id: str = Field(description="an account ID such as 20-800283B60")


class Accounts(BaseModel):
    account_ids: list[str] = Field(description="account IDs, at most 50", max_length=50)


DESCRIPTIONS = {
    "shared_attribute_cluster": ("Accounts sharing a device, a phone or an IP with this account.", OneAccount),
    "account_signals": ("The account's signal record (pass-through, minutes to forward, payers, age, sharing...).",
                        OneAccount),
    "compute_taint": ("Tainted amount, balance and tainted share for these accounts (code-calculated).", Accounts),
}


def lookup_tools(tool_client, names):
    from langchain_core.tools import StructuredTool
    out = []
    for name in names:
        desc, schema = DESCRIPTIONS[name]

        def fn(_name=name, **kwargs):
            r = tool_client.call(_name, **kwargs)
            return json.dumps(r, separators=(",", ":"))[:6000]
        out.append(StructuredTool.from_function(func=fn, name=name, description=desc, args_schema=schema))
    return out
