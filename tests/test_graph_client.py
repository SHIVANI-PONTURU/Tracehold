"""Phase 1 step 4: the graph helper (name whitelist, delete protection, read-only queries)."""
import pytest

from graph import client


@pytest.mark.parametrize("name", ["network", "memory_12", "memory_bank_20", "scratch_inv42", "test_engine"])
def test_allowed_graph_names(name):
    assert client.check_name(name) == name


@pytest.mark.parametrize("name", ["", "Network", "netw0rk", "memory_", "memory 12", "prod", "test_", "../x", None])
def test_other_graph_names_are_refused(name):
    with pytest.raises(client.GraphNameError):
        client.check_name(name)


class FakeDB:
    def __init__(self, graphs):
        self.graphs, self.deleted = list(graphs), []

    def list_graphs(self):
        return self.graphs

    def select_graph(self, name):
        db = self

        class G:
            def delete(self):
                db.deleted.append(name)
        return G()


def test_network_cannot_be_deleted_without_force():
    db = FakeDB(["network"])
    with pytest.raises(PermissionError):
        client.open_graph(db, "network").delete()
    assert db.deleted == []
    client.open_graph(db, "network").delete(force=True)
    assert db.deleted == ["network"]


def test_test_graphs_delete_without_force():
    db = FakeDB(["test_x"])
    client.open_graph(db, "test_x").delete()
    assert db.deleted == ["test_x"]


@pytest.mark.integration
def test_create_query_read_only_and_delete_a_test_graph():
    import redis
    db = client.connect()
    g = client.open_graph(db, "test_pytest_client")
    g.delete()
    g.query("UNWIND $rows AS r CREATE (:Account {id: r.id})", {"rows": [{"id": "a"}, {"id": "b"}]})
    assert g.value("MATCH (n:Account) RETURN count(n)") == 2
    with pytest.raises(redis.exceptions.ResponseError):
        g.ro_query("CREATE (:Account {id: 'c'})")
    g.delete()
    assert not g.exists()


@pytest.mark.integration
def test_schema_creation_is_safe_to_run_twice():
    from graph import schema
    db, raw = client.connect(), client.raw_connection()
    g = client.open_graph(db, "test_pytest_schema")
    g.delete()
    g.query("CREATE (:Account {id: 'a'})")
    first = schema.create_indexes(g)
    second = schema.create_indexes(g)
    assert all(s.endswith("created") for s in first) and all(s.endswith("exists") for s in second)
    g.delete()
