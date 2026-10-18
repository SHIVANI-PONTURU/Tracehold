"""Phase 1 step 12: the loaded `network` graph matches the cleaned data and holds no ground truth.

Read-only: these tests never change the graph. Skipped until `python -m graph.load --rebuild` has run.
"""
import json
import os
import time

import pytest

from data import contract, settings

pytestmark = [pytest.mark.integration, pytest.mark.data]


@pytest.fixture(scope="module")
def network():
    from graph import client
    g = client.open_graph(client.connect(), "network", timeout_ms=client.LONG_QUERY_MS)
    if not g.exists():
        pytest.skip("no `network` graph yet: run `python -m graph.load --rebuild`")
    return g


@pytest.fixture(scope="module")
def load_report():
    path = os.path.join(settings.REPORT_DIR, "graph_load.json")
    if not os.path.exists(path):
        pytest.skip("no work/reports/graph_load.json yet")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def test_no_ground_truth_in_property_keys_labels_or_types(network):
    keys = [r[0] for r in network.rows("CALL db.propertyKeys() YIELD propertyKey RETURN propertyKey")]
    labels = [r[0] for r in network.rows("CALL db.labels() YIELD label RETURN label")]
    types = [r[0] for r in network.rows("CALL db.relationshipTypes() YIELD relationshipType RETURN relationshipType")]
    assert contract.forbidden(keys + labels + types) == []


def test_counts_match_the_load_report(network, load_report):
    from graph.load import NODE_ORDER
    for name, c in load_report["counts"].items():
        q = (f"MATCH (n:{name}) RETURN count(n)" if name in NODE_ORDER
             else f"MATCH ()-[r:{name}]->() RETURN count(r)")
        assert network.value(q) == c["expected"], name


def test_load_report_passed_every_check(load_report):
    failed = [c for c in load_report["checks"] if c["result"] != "PASS"]
    assert not failed, failed


def test_cleaned_table_and_graph_agree_on_transfer_count(network):
    with open(os.path.join(settings.CLEAN_DIR, "stats.json"), encoding="utf-8") as f:
        stats = json.load(f)
    assert network.value("MATCH ()-[t:TRANSFERRED]->() RETURN count(t)") == stats["rows_out"]


# The pattern trace_funds will use in Phase 2. Two planner rules (QUERIES.md): look the fraud transfers up by
# their own ID (TRANSFERRED.id index), and compare times only after a WITH, so the next hop is read from the
# account's own transfers. With `MATCH ... MATCH ... WHERE n.ts >= f.ts` FalkorDB 6.0.2 range-scans the
# TRANSFERRED.ts index or every edge instead: 6.5 to 77 seconds for one complaint, against 2 milliseconds.
HOP = ("UNWIND $ids AS tid MATCH (v:Account)-[f:TRANSFERRED {id: tid}]->(m:Account) WITH f, m "
       "MATCH (m)-[n:TRANSFERRED]->(x:Account) WITH f, n WHERE n.ts >= f.ts RETURN count(n)")


def test_time_respecting_hop_query_works(network):
    """A later transfer out of an account the money reached, with the plan the query is written for."""
    victim, ids = network.rows("MATCH (c:Complaint) RETURN c.victim_account_id, c.fraud_transfer_ids "
                               "ORDER BY c.id LIMIT 1")[0]
    plan = str(network.g.explain(HOP, {"ids": ids}))
    assert "Edge By Index Scan | (v)-[f:TRANSFERRED]->(m" in plan, plan
    assert "Conditional Traverse | (m)-[n:TRANSFERRED]->(x" in plan, plan
    t0 = time.time()
    hops = network.value(HOP, {"ids": ids})
    assert time.time() - t0 < 5, "the hop query should take milliseconds"
    first = network.rows("UNWIND $ids AS tid MATCH (v:Account)-[f:TRANSFERRED {id: tid}]->() RETURN DISTINCT v.id",
                         {"ids": ids})
    assert [r[0] for r in first] == [victim]
    assert hops >= 0
