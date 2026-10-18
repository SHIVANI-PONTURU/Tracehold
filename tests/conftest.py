"""Shared test setup.

Unit tests (every test without another marker; `pytest -m unit`) need nothing but Python.
Integration tests (marked `integration`) need FalkorDB; they are skipped when it is not reachable.
Data tests (marked `data`) need IBM's files and `python -m data.pipeline all`; skipped when missing.
"""
import os

import pytest

from data import settings

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURES = os.path.join(HERE, "fixtures")


def _falkordb_up():
    try:
        from graph import client
        return client.reachable()
    except Exception:
        return False


def pytest_collection_modifyitems(config, items):
    up = None
    have_data = os.path.exists(os.path.join(settings.CLEAN_DIR, "stats.json"))
    for item in items:
        if "integration" not in item.keywords and "data" not in item.keywords:
            item.add_marker(pytest.mark.unit)              # `pytest -m unit` runs these (plan, step 1)
        if "integration" in item.keywords:
            if up is None:
                up = _falkordb_up()
            if not up:
                item.add_marker(pytest.mark.skip(
                    reason=f"FalkorDB not reachable at {settings.FALKORDB_HOST}:{settings.FALKORDB_PORT} "
                           f"(start it: powershell -ExecutionPolicy Bypass -File deploy\\falkordb.ps1 start)"))
        if "data" in item.keywords and not have_data:
            item.add_marker(pytest.mark.skip(reason="no pipeline outputs yet: run `python -m data.pipeline all`"))


@pytest.fixture(autouse=True)
def no_langfuse(monkeypatch):
    """Tests never send traces or scores to Langfuse (.env may hold the keys); TRACEHOLD_TEST_LANGFUSE=1 allows it."""
    if os.environ.get("TRACEHOLD_TEST_LANGFUSE") != "1":
        for k in ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY"):
            monkeypatch.delenv(k, raising=False)


@pytest.fixture
def mini_project(tmp_path, monkeypatch):
    """A tiny copy of the project's inputs (10 transfers, 4 accounts, 1 attempt) with settings pointed at it."""
    import shutil
    root = tmp_path / "proj"
    syn = root / "synthetic"
    syn.mkdir(parents=True)
    for name in ("HI-Small_Trans.csv", "HI-Small_accounts.csv", "HI-Small_Patterns.txt"):
        shutil.copyfile(os.path.join(FIXTURES, name), root / name)
    shutil.copyfile(os.path.join(settings.SYNTHETIC_DIR, "overlay_config.yaml"), syn / "overlay_config.yaml")
    work = root / "work"
    monkeypatch.setattr(settings, "ROOT", str(root))
    monkeypatch.setattr(settings, "SYNTHETIC_DIR", str(syn))
    monkeypatch.setattr(settings, "OVERLAY_CONFIG_PATH", str(syn / "overlay_config.yaml"))
    monkeypatch.setattr(settings, "OVERLAY_OUTPUT_DIR", str(syn / "output"))
    monkeypatch.setattr(settings, "WORK_DIR", str(work))
    monkeypatch.setattr(settings, "CLEAN_DIR", str(work / "clean"))
    monkeypatch.setattr(settings, "REPORT_DIR", str(work / "reports"))
    monkeypatch.setattr(settings, "SNAPSHOT_DIR", str(work / "snapshot"))
    return root


@pytest.fixture(scope="session")
def tool_ctx():
    """Tool context on the loaded network graph, with memory graphs named test_memory_<bank> (deleted after)."""
    from graph import client, memory_graph
    from mcp_server import tools
    db = client.connect()
    if "network" not in client.graph_names(db):
        pytest.skip("no `network` graph yet: run `python -m data.pipeline graph`")
    old = memory_graph.PREFIX
    memory_graph.PREFIX = "test_memory_"
    memory_graph._ready.clear()
    ctx = tools.Ctx()
    yield ctx
    for name in client.graph_names(db):
        if name.startswith("test_memory_"):
            client.open_graph(db, name).delete()
    memory_graph.PREFIX = old
    memory_graph._ready.clear()
