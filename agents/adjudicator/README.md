# agents/adjudicator

The Adjudicator's LangGraph graph (`graph.py`): load the trace handoff, gather signals, decide per account, act (propose_lien and reversible actions), submit; one retry after a check failure. Run by `python -m agents.worker adjudicator`.
