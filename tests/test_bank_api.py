"""Phase 2 step 3: the mock bank API (FastAPI and SQLite): liens, lien requests, reversals and information
requests; liens are idempotent on decision_id."""
import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient            # noqa: E402

from bank_api.app import create_app                   # noqa: E402

LIEN = {"decision_id": "DEC-0011-abc123def456-a1-1", "investigation_id": "INV-0011-abc123def456",
        "from_bank_id": "11", "bank_id": "11", "account_id": "11-8001D5B50", "amount_paise": 106_498}


@pytest.fixture
def bank(tmp_path):
    return TestClient(create_app(str(tmp_path / "bank.sqlite")))


def test_a_lien_is_placed_once_per_decision(bank):
    first = bank.post("/liens", json=LIEN).json()
    assert first["status"] == "placed" and first["id"].startswith("LIEN-") and not first["idempotent_replay"]
    again = bank.post("/liens", json=LIEN).json()
    assert again["id"] == first["id"] and again["idempotent_replay"]
    assert bank.post("/liens", json={**LIEN, "amount_paise": 5}).status_code == 409
    assert len(bank.get("/liens", params={"investigation_id": LIEN["investigation_id"]}).json()) == 1


def test_the_bank_caps_a_lien_at_what_other_investigations_leave_of_the_balance(bank):
    """Phase 4 step 3, earlier report first, checked under the bank's lock: with balance_paise given, a lien takes at
    most the balance less other investigations' active liens; nothing left is refused; a reversal frees the room."""
    first = bank.post("/liens", json={**LIEN, "balance_paise": 150_000}).json()
    assert first["amount_paise"] == 106_498
    later = {**LIEN, "decision_id": "DEC-0012-abc123def456-a1-1", "investigation_id": "INV-0012-abc123def456",
             "amount_paise": 90_000, "balance_paise": 150_000}
    capped = bank.post("/liens", json=later).json()
    assert capped["amount_paise"] == 150_000 - 106_498                     # 43,502 left of the balance
    assert bank.post("/liens", json=later).json()["idempotent_replay"]     # the replay asks 90,000 and gets the lien
    third = {**later, "decision_id": "DEC-0013-abc123def456-a1-1", "investigation_id": "INV-0013-abc123def456"}
    assert bank.post("/liens", json=third).status_code == 409             # nothing left
    bank.post("/reversals", json={"lien_id": first["id"], "reason": "the first complaint was withdrawn"})
    assert bank.post("/liens", json=third).json()["amount_paise"] == 90_000      # 1,06,498 free again
    by_acc = bank.get("/liens", params={"account_id": LIEN["account_id"]}).json()
    assert len(by_acc) == 3 and all(l["account_id"] == LIEN["account_id"] for l in by_acc)
    assert bank.get("/liens", params={"account_id": "20-800283B60"}).json() == []


def test_other_banks_get_a_lien_request_not_a_lien(bank):
    other = {**LIEN, "decision_id": "DEC-0011-abc123def456-a1-2", "bank_id": "20", "account_id": "20-800283B60"}
    assert bank.post("/liens", json=other).status_code == 400
    r = bank.post("/lien-requests", json=other).json()
    assert r["status"] == "requested" and r["id"].startswith("LREQ-")
    assert bank.post("/lien-requests", json=LIEN).status_code == 400


def test_reversal_and_info_request(bank):
    lien = bank.post("/liens", json=LIEN).json()
    r = bank.post("/reversals", json={"lien_id": lien["id"], "reason": "the customer proved the money was theirs"})
    assert r.json()["status"] == "reversed"
    assert bank.post("/reversals", json={"lien_id": "LIEN-nope", "reason": "x" * 5}).status_code == 404
    info = bank.post("/info-requests", json={"investigation_id": LIEN["investigation_id"], "from_bank_id": "11",
                                             "to_bank_id": "20", "account_id": "20-800283B60",
                                             "what": "KYC and device history"}).json()
    assert info["status"] == "sent" and info["request_id"].startswith("INFO-")


def test_bad_input_is_refused(bank):
    assert bank.post("/liens", json={**LIEN, "amount_paise": 0}).status_code == 422
    assert bank.post("/liens", json={**LIEN, "note": "extra"}).status_code == 422
    assert bank.post("/liens", json={**LIEN, "account_id": "not-an-account"}).status_code == 422


INFO = {"investigation_id": LIEN["investigation_id"], "from_bank_id": "20", "to_bank_id": "23",
        "account_id": "23-8001B8810", "what": "KYC and device history", "request_key": "ACT-0123456789ab"}


def test_an_information_request_is_sent_once_per_key_and_can_be_withdrawn(bank):
    first = bank.post("/info-requests", json=INFO).json()
    again = bank.post("/info-requests", json=INFO).json()
    assert again["request_id"] == first["request_id"] and again["idempotent_replay"] and not first["idempotent_replay"]
    assert len(bank.get("/info-requests", params={"investigation_id": INFO["investigation_id"]}).json()) == 1
    w = bank.post(f"/info-requests/{first['request_id']}/withdraw", json={"reason": "undone by the analyst"}).json()
    assert w["status"] == "withdrawn" and w["withdraw_reason"] == "undone by the analyst"
    assert bank.post("/info-requests/INFO-nope/withdraw", json={"reason": "x" * 5}).status_code == 404


def test_a_phase_2_database_gains_the_new_columns(tmp_path):
    import sqlite3
    path = str(tmp_path / "old.sqlite")
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE info_requests (id TEXT PRIMARY KEY, investigation_id TEXT NOT NULL, from_bank_id TEXT "
                "NOT NULL, to_bank_id TEXT NOT NULL, account_id TEXT NOT NULL, what TEXT NOT NULL, status TEXT NOT NULL, "
                "created_at INTEGER NOT NULL)")
    con.execute("INSERT INTO info_requests VALUES ('INFO-old', 'INV-0011-x', '11', '20', '20-800283B60', 'KYC', 'sent', 1)")
    con.commit()
    con.close()
    old = TestClient(create_app(path))
    assert old.post("/info-requests", json=INFO).json()["status"] == "sent"
    assert {r["id"] for r in old.get("/info-requests").json()} >= {"INFO-old"}
