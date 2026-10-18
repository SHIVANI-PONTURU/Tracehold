"""Phase 2 step 2: each read tool matches a manual query on complaint 1 (CMP-0011, bank 11), the planted ring
comes out as one community, and the act tools write only through code. Needs the loaded network graph."""
import pytest

from handoff import contracts as C
from mcp_server import network
from mcp_server import tools as T
from memory import store
from taint import engine
from tests.test_tool_server import servers  # noqa: F401  (fixture: tool server + a fresh mock bank)

pytestmark = [pytest.mark.integration, pytest.mark.data]
BANK, COMPLAINT = "11", "CMP-0011"
TRAIL = ["20-800283B60", "10-8001A6140", "22-800202780", "10-800119810", "12-80010D740", "20-8001B0FA0",
         "23-8002A0E00", "220-800115860"]
RING = sorted(TRAIL)                      # complaint 1's planted ring: the 8 trail accounts


@pytest.fixture(scope="module")
def inv(tool_ctx):
    mem = tool_ctx.memory(BANK)
    if not network.complaint(tool_ctx.net, COMPLAINT):
        pytest.skip("complaint CMP-0011 is not in the graph")
    return store.create_investigation(mem, COMPLAINT)


def call(ctx, fn, inv, *args):
    r = T.run_tool(fn, ctx, BANK, inv, *args)
    assert "error" not in r, r
    return r


def test_trace_funds_matches_a_manual_query_and_respects_time(tool_ctx, inv):
    comp = network.complaint(tool_ctx.net, COMPLAINT)
    fraud = network.transfers_by_id(tool_ctx.net, comp["fraud_transfer_ids"])[0]
    r = call(tool_ctx, T.trace_funds, inv, [fraud.dst], fraud.ts, 2)
    hop1 = r["hops"][0]["accounts"][0]
    manual = tool_ctx.net.rows("MATCH (a:Account {id: $id})-[t:TRANSFERRED]->(b:Account) WITH t "
                               "WHERE t.ts >= $ts AND t.self_loop = false RETURN count(t), sum(t.amount)",
                               {"id": fraud.dst, "ts": fraud.ts})[0]
    assert (hop1["later_outflows"], hop1["later_outflow_total_paise"]) == (manual[0], manual[1])
    listed = {t["transfer_id"]: t for t in hop1["listed"]}
    assert "T000418370" in listed                              # the true trail's next transfer
    for h in r["hops"][1:]:
        for a in h["accounts"]:
            assert all(t["ts"] >= a["after_ts"] for t in a["listed"])


def test_compute_taint_matches_the_engine_on_a_manual_read(tool_ctx, inv):
    r = call(tool_ctx, T.compute_taint, inv, TRAIL)
    assert r["conservation_ok"] and r["disputed_paise"] == 86_719_601
    comp = network.complaint(tool_ctx.net, COMPLAINT)
    fraud = network.transfers_by_id(tool_ctx.net, comp["fraud_transfer_ids"])
    opening, transfers = {}, []
    for a in TRAIL:                                           # a plain per-account read, no paging helper
        opening[a] = tool_ctx.net.value("MATCH (x:Account {id: $id}) RETURN x.opening_balance", {"id": a})
        for q in ("MATCH (x:Account {id: $id})-[t:TRANSFERRED]->(y) RETURN t.id, x.id, y.id, t.amount, t.ts, t.format, t.self_loop",
                  "MATCH (y)-[t:TRANSFERRED]->(x:Account {id: $id}) RETURN t.id, y.id, x.id, t.amount, t.ts, t.format, t.self_loop"):
            transfers += [engine.Transfer(*row[:5], row[5], row[6]) for row in tool_ctx.net.rows(q, {"id": a})]
    manual = engine.compute(fraud, opening, transfers)
    for acc in r["accounts"]:
        m = manual.accounts[acc["account_id"]]
        assert (acc["tainted_paise"], acc["balance_paise"]) == (m.tainted, m.balance)
    held = sum(a["tainted_paise"] for a in r["accounts"])
    assert held + r["not_followed_total_paise"] + r["unrecoverable_paise"] == r["disputed_paise"]


def test_account_signals_match_manual_counts(tool_ctx, inv):
    s = call(tool_ctx, T.account_signals, inv, "20-800283B60")
    payers = tool_ctx.net.value("MATCH (p:Account)-[t:TRANSFERRED]->(a:Account {id: $id}) WITH p, t "
                                "WHERE t.self_loop = false RETURN count(DISTINCT p)", {"id": "20-800283B60"})
    assert s["distinct_payers"] == payers
    assert set(T.SIGNALS) >= {k for k in s if k not in ("account_id", "bank_id")}


def test_shared_attribute_cluster_matches_a_manual_query(tool_ctx, inv):
    c = call(tool_ctx, T.shared_attribute_cluster, inv, "10-800119810")
    devices = {d["device_id"]: set(d["accounts"]) for d in c["device"]}
    assert devices["DV-0218094"] == {"12-80010D740", "20-8001B0FA0", "220-800115860"}   # the planted ring device


def test_the_planted_ring_comes_out_as_one_community(tool_ctx, inv):
    r = call(tool_ctx, T.mule_community, inv, TRAIL + ["2591-80035FF20"])
    comm = {a["account_id"]: a["community_id"] for a in r["accounts"]}
    assert len({comm[a] for a in RING}) == 1
    assert comm["2591-80035FF20"] != comm[RING[0]]
    assert not [g for g in T.client.graph_names(tool_ctx.db) if g.startswith("scratch_")]   # scratch deleted


def test_key_accounts_are_repeatable_and_rank_the_bridge_first(tool_ctx, inv):
    mem = tool_ctx.memory(BANK)
    res = call(tool_ctx, T.compute_taint, inv, TRAIL)
    trace = C.TraceHandoff(investigation_id=inv, complaint_id=COMPLAINT, disputed_paise=res["disputed_paise"],
                           trail=[C.TraceLayer(layer=1, account_ids=[TRAIL[0]], transfer_ids=["T000418368"])],
                           accounts=[C.TracedAccount(account_id=a["account_id"], layer=a["layer"] or 1,
                                                     tainted_paise=a["tainted_paise"], balance_paise=a["balance_paise"])
                                     for a in res["accounts"]],
                           unrecoverable_paise=res["unrecoverable_paise"],
                           not_followed_paise=res["not_followed_total_paise"], trace_id=inv)
    store.add_handoff(mem, inv, "trace", "tracer", "adjudicator", trace.model_dump(mode="json"))
    first = call(tool_ctx, T.key_accounts, inv, "C-10-800119810")
    second = call(tool_ctx, T.key_accounts, inv, "C-10-800119810")
    assert first == second and first["ranking"][0]["account_id"] == "10-800119810"


def test_compute_taint_after_the_trace_follows_the_whole_trail(tool_ctx, inv):
    """The Verifier checks one deep account at a time (17 Oct: it asked about two layer-2+ accounts alone, got zero
    taint and rejected both liens). Once the trace handoff exists the engine runs over the whole trail."""
    full = {a["account_id"]: a["tainted_paise"] for a in call(tool_ctx, T.compute_taint, inv, TRAIL)["accounts"]}
    deep = [a for a in TRAIL[1:] if full[a] > 0][0]
    one = call(tool_ctx, T.compute_taint, inv, [deep])
    assert [a["account_id"] for a in one["accounts"]] == [deep]
    assert one["accounts"][0]["tainted_paise"] == full[deep] > 0
    assert one["computed_over"].startswith("the traced trail")
    assert {a["account_id"] for a in call(tool_ctx, T.compute_taint, inv, [])["accounts"]} == set(full)


def test_act_tools_write_through_code_only(servers, tool_ctx, inv):             # noqa: F811
    mem = tool_ctx.memory(BANK)
    assert "review_case_id" in call(tool_ctx, T.open_review, inv, "20-800283B60", "Why the large cheques?")
    assert call(tool_ctx, T.flag_entity, inv, "DV-0218094", "shared by the traced accounts")["entity_kind"] == "device"
    p = call(tool_ctx, T.propose_lien, inv, "20-800283B60", f"DEC-{inv[4:]}-1", "mule", "Fast pass-through.")
    expected = call(tool_ctx, T.compute_taint, inv, TRAIL)["accounts"][0]["lien_amount_paise"]
    assert p["lien_amount_paise"] == expected
    d = store.get_decision(mem, p["decision_id"])
    assert d.get("approved") is None and d["lien_amount"] == expected
    assert T.run_tool(T.propose_lien, tool_ctx, BANK, inv, "11-800178CC0", f"DEC-{inv[4:]}-2", "mule", "x")["error"]["code"] == "forbidden"
    assert T.run_tool(T.execute_lien, tool_ctx, BANK, inv, p["decision_id"])["error"]["code"] == "refused_not_approved"


def test_a_later_complaint_liens_only_what_earlier_liens_leave(servers, tool_ctx, inv):    # noqa: F811
    """Phase 4 step 3, earlier report first: an earlier complaint's lien request on the account leaves 1,000 paise of
    its balance free, so this complaint's lien is capped at 1,000 paise; once that lien is reversed the cap goes."""
    import httpx2
    acc = TRAIL[0]
    t = call(tool_ctx, T.compute_taint, inv, TRAIL)["accounts"][0]
    assert t["account_id"] == acc and t["lien_amount_paise"] > 1_000
    earlier = httpx2.post(f"{tool_ctx.bank_api_url}/lien-requests", json={
        "decision_id": "DEC-9999-earlier-a1", "investigation_id": "INV-9999-earlier", "from_bank_id": "11",
        "bank_id": acc.split("-")[0], "account_id": acc, "amount_paise": t["balance_paise"] - 1_000}).json()
    capped = call(tool_ctx, T.propose_lien, inv, acc, f"DEC-{inv[4:]}-3", "mule", "Fast pass-through.")
    assert capped["lien_amount_paise"] == 1_000
    httpx2.post(f"{tool_ctx.bank_api_url}/reversals", json={"lien_id": earlier["id"], "reason": "complaint withdrawn"})
    free = call(tool_ctx, T.propose_lien, inv, acc, f"DEC-{inv[4:]}-4", "mule", "Fast pass-through.")
    assert free["lien_amount_paise"] == t["lien_amount_paise"]
