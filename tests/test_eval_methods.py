"""Phase 4 steps 3 and 5: the baselines, the scoring, the rules-only calibration, the sample and the overlap rule
(eval/methods.py), and the pieces of the runners that need no database."""
import os

from eval import methods as M
from eval import truth as TR

ACCS = [  # a three-account trail: a mule holding stolen money, a genuine merchant holding a little, a drained mule
    {"account_id": "1-AAAAAAAAA", "layer": 1, "tainted_paise": 80_000, "balance_paise": 100_000,
     "lien_amount_paise": 80_000, "tainted_in_paise": 90_000},
    {"account_id": "2-BBBBBBBBB", "layer": 2, "tainted_paise": 10_000, "balance_paise": 5_000_000,
     "lien_amount_paise": 10_000, "tainted_in_paise": 10_000},
    {"account_id": "3-CCCCCCCCC", "layer": 1, "tainted_paise": 0, "balance_paise": 2_000,
     "lien_amount_paise": 0, "tainted_in_paise": 50_000},
]
LABELS = {"1-AAAAAAAAA": "mule", "3-CCCCCCCCC": "mule"}          # 2-BBBBBBBBB is missing: genuine
MULE_SIG = {"pass_through_ratio": 0.99, "distinct_payers": 2, "account_age_days": 40, "registered_merchant": False}
SHOP_SIG = {"pass_through_ratio": 0.4, "distinct_payers": 300, "account_age_days": 2000, "registered_merchant": True,
            "tainted_share": 0.002}


def test_full_freeze_and_greedy_take_whole_balances_and_the_disputed_amount_per_hop():
    ff = M.full_freeze(ACCS)
    assert {a: d["amount"] for a, d in ff.items()} == {"1-AAAAAAAAA": 100_000, "2-BBBBBBBBB": 5_000_000,
                                                       "3-CCCCCCCCC": 2_000}
    gr = M.greedy(ACCS, disputed=150_000)
    assert {a: d["amount"] for a, d in gr.items()} == {"1-AAAAAAAAA": 100_000, "2-BBBBBBBBB": 150_000,
                                                       "3-CCCCCCCCC": 2_000}


def test_rules_labels_follow_the_rubric_signals():
    assert M.rules_label(MULE_SIG) == "mule"
    assert M.rules_label(SHOP_SIG) == "likely_genuine"
    assert M.rules_label({**MULE_SIG, "registered_merchant": True}) == "uncertain"      # strong on both sides
    assert M.rules_label({}) == "likely_genuine"                                          # no signal: rubric default


def test_rules_only_uses_tracehold_amounts_and_the_floor():
    d = M.rules_only(ACCS, {"1-AAAAAAAAA": MULE_SIG, "2-BBBBBBBBB": SHOP_SIG}, floor=50_000)
    assert d["1-AAAAAAAAA"] == {"action": "lien", "amount": 80_000, "label": "mule"}
    assert d["2-BBBBBBBBB"]["action"] == "review"                 # likely genuine, below the floor
    assert "3-CCCCCCCCC" not in d                                 # holds no tainted money: not judged


def test_tracehold_takes_only_the_liens_the_verifier_approved():
    proposal = {"accounts": [
        {"account_id": "1-AAAAAAAAA", "label": "mule", "proposed_action": "lien", "lien_proposal_id": "DEC-1",
         "lien_amount_paise": 80_000},
        {"account_id": "2-BBBBBBBBB", "label": "likely_genuine", "proposed_action": "lien", "lien_proposal_id": "DEC-2",
         "lien_amount_paise": 10_000},
        {"account_id": "3-CCCCCCCCC", "label": "uncertain", "proposed_action": "review_and_request_info"}]}
    verdict = {"verdicts": [{"decision_id": "DEC-1", "verdict": "approve"}, {"decision_id": "DEC-2", "verdict": "reject"}]}
    d = M.tracehold(proposal, verdict)
    assert d["1-AAAAAAAAA"]["action"] == "lien" and d["1-AAAAAAAAA"]["amount"] == 80_000
    assert d["2-BBBBBBBBB"]["action"] == "none" and d["3-CCCCCCCCC"]["action"] == "review"   # rejected: no action
    assert all(x["action"] != "lien" for x in M.tracehold(proposal, {**verdict, "checks_failed": True}).values())


def test_scoring_counts_innocent_money_money_beyond_the_taint_and_mules_caught():
    s = M.score_case(M.full_freeze(ACCS), ACCS, LABELS, {"1-AAAAAAAAA", "3-CCCCCCCCC", "9-ZZZZZZZZZ"})
    assert s["frozen"] == 5_102_000 and s["innocent"] == 5_000_000 and s["innocent_accounts"] == 1
    assert s["beyond_taint"] == 20_000 + 4_990_000 + 2_000 and s["stolen_held"] == 90_000
    assert (s["liens"], s["liened_mules"], s["caught"], s["trail_mules"]) == (3, 2, 2, 3)
    t = M.score_case(M.rules_only(ACCS, {"1-AAAAAAAAA": MULE_SIG, "2-BBBBBBBBB": SHOP_SIG}, 50_000), ACCS, LABELS,
                     {"1-AAAAAAAAA", "3-CCCCCCCCC"})
    assert t["innocent"] == 0 and t["beyond_taint"] == 0 and t["reviews"] == 1 and t["caught"] == 1
    agg = M.aggregate([{**s, "disputed": 1}, {**t, "disputed": 1, "usd": 0.3, "seconds": 40}])
    assert agg["complaints"] == 2 and agg["precision"] == round(3 / 4, 4) and agg["recall"] == round(3 / 5, 4)
    assert agg["median_usd"] == 0.3 and agg["innocent_per_case"] == 2_500_000


def test_calibration_picks_thresholds_on_dev_and_defaults_win_ties():
    dev = [{"complaint_id": "CMP-1", "accounts": ACCS, "signals": {"1-AAAAAAAAA": MULE_SIG, "2-BBBBBBBBB": SHOP_SIG}}]
    th, f1 = M.calibrate(dev, LABELS, {"CMP-1": {"1-AAAAAAAAA"}}, floor=50_000)
    assert f1 == 1.0 and th == M.RULES                           # the defaults already separate them


def test_the_sample_is_fixed_by_its_seed_and_reports_the_held_out_pattern_alone():
    truth = TR.complaints()
    s1, s2 = M.sample(truth), M.sample(truth)
    assert s1 == s2 and len(s1) == 46
    counts = {g: sum(1 for v in s1.values() if v == g) for g in M.GROUPS}
    assert counts == {"shallow": 12, "medium": 12, "deep": 12, "held_out": 10}
    assert all(truth[c]["split"] == "test" for c in s1)
    assert all(truth[c]["pattern_type"] == "CYCLE" for c, g in s1.items() if g == "held_out")
    assert not any(truth[c]["pattern_type"] == "CYCLE" for c, g in s1.items() if g != "held_out")
    assert M.sample(truth, seed=1) != s1


def test_the_second_sample_shares_no_complaint_with_the_first():
    truth = TR.complaints()
    a, b = M.sample_set(truth, "a"), M.sample_set(truth, "b")
    assert a == M.sample(truth) and not set(a) & set(b) and len(b) == 41               # medium has only 7 left
    assert {g: sum(1 for v in b.values() if v == g) for g in M.GROUPS} == {"shallow": 12, "medium": 7, "deep": 12,
                                                                        "held_out": 10}


def test_rules_only_liens_an_account_holding_a_large_part_of_the_loss():
    shop = {**ACCS[1], "tainted_paise": 60_000, "lien_amount_paise": 60_000}       # likely genuine, share 1.2%
    d = M.rules_only([shop], {"2-BBBBBBBBB": SHOP_SIG}, floor=100_000, disputed=150_000)
    assert d["2-BBBBBBBBB"]["action"] == "review"                                   # below the floor: still a review
    d = M.rules_only([shop], {"2-BBBBBBBBB": SHOP_SIG}, floor=50_000, disputed=150_000)
    assert d["2-BBBBBBBBB"]["action"] == "lien"                                     # 40% of the loss: liened
    d = M.rules_only([shop], {"2-BBBBBBBBB": {**MULE_SIG, "registered_merchant": True}}, floor=50_000,
                     disputed=150_000, hold_part=None)
    assert d["2-BBBBBBBBB"]["action"] == "review" and d["2-BBBBBBBBB"]["label"] == "uncertain"   # rule off


def test_truth_files_give_labels_and_trail_mules():
    labels, mules = TR.account_labels(), TR.trail_mules()
    assert TR.label_of(labels, "no-such-account") == "genuine"
    assert sum(len(v) for v in mules.values()) > 0 and all(isinstance(v, set) for v in mules.values())
    assert TR.complaints()["CMP-0011"]["depth_bucket"] == "deep"


def test_earlier_report_first_never_exceeds_the_balance_or_the_disputed_amount():
    assert M.allocate([(200, "B", 70), (100, "A", 60)], balance=100) == {"A": 60, "B": 40}
    shared = {"account_id": "5-SHARED000", "layer": 1, "tainted_paise": 60, "balance_paise": 100,
              "lien_amount_paise": 60}
    cases = {"A": {"reported_at": 100, "disputed_paise": 60, "accounts": [shared]},
             "B": {"reported_at": 200, "disputed_paise": 500,
                   "accounts": [{**shared, "lien_amount_paise": 70, "greedy_paise": 100}]},
             "C": {"reported_at": 50, "disputed_paise": 10, "accounts": [{**shared, "account_id": "6-ALONE0000",
                                                                        "lien_amount_paise": 10}]}}
    r = M.overlap_check(cases)
    assert r["overlapping_sets"] == 1 and r["complaints_in_sets"] == 2 and r["shared_accounts"] == 1
    assert r["claims_over_balance_before_rule"] == 1 and r["paise_cut_by_rule"] == 30
    assert r["account_limit_holds"] and r["complaint_limit_holds"]


def test_follow_every_branch_stands_in_for_the_tracers_model():
    from agents.tracer.graph import BranchCalls
    from eval.run_eval import FollowEvery
    rows = "20-800283B60 | layer 1 | received Rs 1\n10-8001A6140 | layer 2 | received Rs 2"
    calls = FollowEvery().decide([("system", "x"), ("human", "Open branches:\n" + rows)], BranchCalls)
    assert [(c.account_id, c.decision) for c in calls.calls] == [("20-800283B60", "follow"), ("10-8001A6140", "follow")]


def test_run_time_spans_the_agent_steps_only():
    from eval.run_tracehold import banks, seconds_of
    steps = [{"agent": "tracer", "started_ms": 1_000, "latency_ms": 500}, {"agent": "verifier", "started_ms": 9_000,
                                                                          "latency_ms": 1_000},
             {"agent": "gate", "started_ms": 99_000, "latency_ms": 10}]
    assert seconds_of(steps) == 9.0 and seconds_of([]) is None
    b = banks()
    assert b[:3] == ["11", "20", "12"] and len(b) == len(set(b))


def test_score_turns_saved_trails_and_runs_into_one_results_file(tmp_path, monkeypatch):
    """run_eval.score end to end without a database: saved code-only trails for a dev, a sample and a held-out
    complaint, one saved TraceHold run, and the results file the Evaluation page reads."""
    import json
    from eval import run_eval as RE
    raw, th = tmp_path / "raw", tmp_path / "raw_tracehold"
    raw.mkdir(), th.mkdir()
    truth = TR.complaints()
    for cid in ("CMP-0001", "CMP-0061", "CMP-0129"):                # dev; test shallow in the sample; held-out
        rec = {"complaint_id": cid, "bank_id": "1", "investigation_id": f"INV-{cid[4:]}-abc123def456",
               "disputed_paise": 150_000, "reported_at": truth[cid]["reported_at"], "accounts": ACCS,
               "signals": {"1-AAAAAAAAA": MULE_SIG, "2-BBBBBBBBB": SHOP_SIG}, "error": None}
        (raw / f"{cid}.json").write_text(json.dumps(rec))
    (raw / "CMP-0002.json").write_text(json.dumps({"complaint_id": "CMP-0002", "error": "RuntimeError: too_large"}))
    (th / "CMP-0061.json").write_text(json.dumps({
        "complaint_id": "CMP-0061", "status": "at_gate", "accounts": ACCS[:1], "usd": 0.31, "seconds": 52.0,
        "proposal": {"accounts": [{"account_id": "1-AAAAAAAAA", "label": "mule", "proposed_action": "lien",
                                   "lien_proposal_id": "DEC-1", "lien_amount_paise": 80_000}]},
        "verdict": {"verdicts": [{"decision_id": "DEC-1", "verdict": "approve"}]}}))
    monkeypatch.setattr(RE, "RAW_DIR", str(raw))
    monkeypatch.setattr(RE, "TH_DIR", str(th))
    path, res = RE.score(out_dir=str(tmp_path))
    assert os.path.basename(path).startswith("evaluation_") and json.load(open(path))["meta"]["trails"] == 3
    assert res["meta"]["failed_trails"] == ["CMP-0002"] and res["meta"]["tracehold_runs"] == 1
    sample = {(r["method"], r["group"]): r for r in res["by_group"] if r["scope"] == "sample"}
    assert set(m for m, _ in sample) == set(M.METHODS) and sample[("tracehold", "shallow")]["complaints"] == 1
    # the real IBM labels know none of these made-up accounts, so all count as genuine
    assert sample[("tracehold", "shallow")]["innocent"] == 80_000 and sample[("full_freeze", "shallow")]["innocent"] == 5_102_000
    assert res["headline"]["innocent_less_than_full_freeze"] == round(1 - 80_000 / 5_102_000, 4)
    assert res["headline"]["tracehold_precision"] == 0.0
    groups = {r["group"] for r in res["by_group"] if r["scope"] == "all_test"}
    assert {"shallow", "held_out", "all_buckets"} <= groups                         # CMP-0001 is dev: not scored
    assert res["overlap"]["shared_accounts"] == 3 and res["overlap"]["account_limit_holds"]
    assert res["overlap_greedy"]["claims_over_balance_before_rule"] >= 1          # greedy asks for more than exists


def test_label_table_compares_a_methods_labels_with_ibm():
    from eval.run_eval import label_table
    t = label_table([{"1-AAAAAAAAA": {"label": "mule"}, "2-BBBBBBBBB": {"label": "mule"}}], LABELS)
    assert t == {"mule": {"mule": 1, "genuine": 1}}
