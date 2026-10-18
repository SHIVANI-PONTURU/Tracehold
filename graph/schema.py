"""Indexes and unique constraints for the network graph (implementation plan, Appendix B).

Indexes go in before any data (the loader's MATCH-by-id needs them); unique constraints go in after
the load, so building them also proves no ID was loaded twice. Safe to run twice: an index or
constraint that already exists is left as it is.
"""
import time

import redis

NODE_LABELS = ["Bank", "Customer", "Account", "Device", "Phone", "IP", "Complaint"]
EDGE_INDEXES = [("TRANSFERRED", "id"), ("TRANSFERRED", "ts")]
ALREADY = ("already indexed", "already exists", "already been indexed")


def _ignore_existing(fn):
    try:
        fn()
        return "created"
    except redis.exceptions.ResponseError as e:
        if any(s in str(e).lower() for s in ALREADY):
            return "exists"
        raise


def create_indexes(gh):
    """gh: GraphHandle for `network`. An index on id for every label, and on TRANSFERRED.id and .ts."""
    done = []
    for label in NODE_LABELS:
        r = _ignore_existing(lambda: gh.query(f"CREATE INDEX FOR (n:{label}) ON (n.id)"))
        done.append(f"index {label}.id: {r}")
    for rel, prop in EDGE_INDEXES:
        r = _ignore_existing(lambda: gh.query(f"CREATE INDEX FOR ()-[r:{rel}]-() ON (r.{prop})"))
        done.append(f"index {rel}.{prop}: {r}")
    return done


def create_constraints(gh, raw):
    """raw: raw Redis connection. A unique constraint on id for every label (built in the background)."""
    done = []
    for label in NODE_LABELS:
        r = _ignore_existing(lambda: raw.execute_command("GRAPH.CONSTRAINT", "CREATE", gh.name, "UNIQUE", "NODE",
                                                         label, "PROPERTIES", "1", "id"))
        done.append(f"unique {label}.id: {r}")
    return done


def create_network_schema(gh, raw):
    return create_indexes(gh) + create_constraints(gh, raw)


def index_status(gh):
    rows = gh.rows("CALL db.indexes() YIELD label, properties, entitytype RETURN label, properties, entitytype")
    return [{"label": r[0], "properties": r[1], "entity": r[2]} for r in rows]


def constraint_status(gh):
    rows = gh.rows("CALL db.constraints() YIELD type, label, properties, status RETURN type, label, properties, status")
    return [{"type": r[0], "label": r[1], "properties": r[2], "status": r[3]} for r in rows]


def wait_for_constraints(gh, timeout_s=900):
    """Wait until no constraint is still being built. Returns the final status list."""
    t0 = time.time()
    while True:
        st = constraint_status(gh)
        if all(c["status"] in ("OPERATIONAL", "FAILED") for c in st) or time.time() - t0 > timeout_s:
            return st
        time.sleep(2)
