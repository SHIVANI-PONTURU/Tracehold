"""Phase 4 step 10: 20 complaints in parallel against bank 11 (scripted models, no tokens spent): every run finishes,
no duplicate nodes, nothing leaks to another bank's memory, and the liens on every account stay within its balance.
Writes the load-test table to work/reports/evaluation/load_test.json."""
import os

import pytest

pytest.importorskip("langgraph")
pytest.importorskip("fastapi")

from data import settings                                   # noqa: E402
from eval import load_test                                  # noqa: E402
from tests.test_tool_server import servers                  # noqa: E402,F401  (fixture: tool server + mock bank)

pytestmark = [pytest.mark.integration, pytest.mark.data]


def test_twenty_parallel_complaints_leave_no_duplicates_leaks_or_overdrawn_accounts(servers, tool_ctx, tmp_path):   # noqa: F811
    base, have_bank = servers
    if not have_bank:
        pytest.skip("the mock bank API needs fastapi")
    out = os.path.join(settings.WORK_DIR, "reports", "evaluation", "load_test.json")
    res = load_test.run(base, tool_ctx, bank="11", n=20, threads=20, tmp_dir=tmp_path, out_path=out)
    assert not res["errors"], res["errors"][:3]
    assert sum(res["statuses"].values()) == 20 and set(res["statuses"]) <= {"executed", "closed"}
    assert res["duplicates"]["total"] == 0, res["duplicates"]
    assert res["leaks"]["total"] == 0, res["leaks"]
    assert res["liens_over_balance"] == []
    assert res["latency_ms"]["read"]["n"] > 0 and res["latency_ms"]["write"]["n"] > 0
