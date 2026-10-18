"""Phase 1 step 3: the pinned FalkorDB engine does everything later phases rely on."""
import pytest


@pytest.mark.integration
def test_engine_check_passes_every_check():
    from graph import engine_check
    report = engine_check.run()
    failed = [c for c in report["checks"] if c["result"] != "PASS"]
    assert not failed, failed
    assert report["module_version"].startswith("600"), report["module_version"]   # FalkorDB 6.0.x
