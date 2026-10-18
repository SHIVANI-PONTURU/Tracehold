# agents/verifier

The Verifier's LangGraph graph (`graph.py`): review each proposed lien as a senior analyst, submit verdicts, wait at the HITL gate (SQLite checkpointer, survives restarts), then record the gate's decisions and execute approved liens. Run by `python -m agents.worker verifier`.
