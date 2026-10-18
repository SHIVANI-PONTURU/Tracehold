"""Per-bank memory graphs (implementation plan, Appendix B): one `memory_<bank>` graph per bank.

Holds that bank's investigations, handoffs, decisions, actions, liens and (Phase 3) lessons. FalkorDB cannot
join across graphs, so memory nodes point at network entities by ID through EntityRef nodes.
"""
import re

import redis

from graph import client
from graph.schema import ALREADY

MEMORY_LABELS = ["Investigation", "Handoff", "Step", "Claim", "Decision", "Lesson", "Message", "Action", "Lien",
                 "EntityRef"]
BANK_RE = re.compile(r"^[0-9]{1,6}$")
PREFIX = "memory_"          # tests use "test_memory_" so they never touch a bank's real memory
_ready = set()


def graph_name(bank_id):
    bank_id = str(bank_id)
    if not BANK_RE.match(bank_id):
        raise ValueError(f"bank_id must be IBM's numeric bank code, got {bank_id!r}")
    return f"{PREFIX}{bank_id}"


def open_memory(db, raw, bank_id, timeout_ms=10_000):
    """The bank's memory graph, with its indexes and unique constraints created on first use."""
    name = graph_name(bank_id)
    gh = client.open_graph(db, name, timeout_ms=timeout_ms)
    if name not in _ready:
        ensure_schema(gh, raw)
        _ready.add(name)
    return gh


def _ignore_existing(fn):
    try:
        fn()
    except redis.exceptions.ResponseError as e:
        if not any(s in str(e).lower() for s in ALREADY):
            raise


def ensure_schema(gh, raw):
    """An index and a unique constraint on id for every memory label. Safe to run twice."""
    for label in MEMORY_LABELS:
        _ignore_existing(lambda: gh.query(f"CREATE INDEX FOR (n:{label}) ON (n.id)"))
    for label in MEMORY_LABELS:
        _ignore_existing(lambda: raw.execute_command("GRAPH.CONSTRAINT", "CREATE", gh.name, "UNIQUE", "NODE",
                                                     label, "PROPERTIES", "1", "id"))
