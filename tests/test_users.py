import pytest
from fastapi.testclient import TestClient

from networth.app import create_app
from networth.auth import ensure_configured
from networth.db import Store
from networth.users import UserError, Users


@pytest.fixture
def app(config, users):
    return create_app(config, users)


def client_for(app, username=None, password=None):
    client = TestClient(app)
    if username:
        response = client.post("/login", data={"username": username, "password": password}, follow_redirects=False)
        assert response.status_code == 303, response.text
    return client


def register(app, username, password="secret-pass", password2=None):
    client = TestClient(app)
    response = client.post("/register", data={"username": username, "password": password,
                                              "password2": password2 or password})
    return client, response


def test_first_admin_from_config(users):
    tester = users.by_name("TESTER")  # names are case-insensitive
    assert tester is not None and tester.is_admin and users.count() == 1


def test_legacy_database_becomes_admin_data(config):
    config.data_dir.mkdir(parents=True)
    Store(config.data_dir / "networth.sqlite").set_settings({"target": "123"})
    users = Users(config.data_dir)
    ensure_configured(config, users)
    assert not (config.data_dir / "networth.sqlite").exists()
    assert users.store(users.by_name("tester").id).target() == 123.0


def test_register_and_separate_data(app, users, store):
    client, response = register(app, "alice")
    assert response.status_code == 200 and "Willkommen, alice" in response.text
    alice = users.by_name("alice")
    assert alice is not None and not alice.is_admin

    columns = {c.name: c.id for c in users.store(alice.id).columns()}
    client.post("/entries/new", data={"date": "2024-01-31", f"c{columns['Girokonto']}": "1000"})
    client.post("/income", data={"date": "2024-01-31", "amount": "2000"})
    assert len(users.store(alice.id).entries()) == 1 and len(users.store(alice.id).income()) == 1
    assert store.entries() == [] and store.income() == []  # the admin does not see it

    admin = client_for(app, "tester", "test-password")
    assert "Noch keine Daten" in admin.get("/").text
    assert "chart-wealth" in client.get("/").text
    entry_id = users.store(alice.id).entries()[0].id
    assert admin.get(f"/entries/{entry_id}").status_code == 404


def test_register_validation(app, users):
    assert register(app, "x")[1].status_code == 422
    assert register(app, "bob", "short")[1].status_code == 422
    assert "stimmen nicht überein" in register(app, "bob", "secret-pass", "other-pass")[1].text
    assert "bereits vergeben" in register(app, "Tester")[1].text
    assert users.count() == 1


def test_registration_closed(app, users):
    admin = client_for(app, "tester", "test-password")
    admin.post("/admin/registration", data={})
    assert not users.registration_open()
    _, response = register(app, "carol")
    assert response.status_code == 403 and users.by_name("carol") is None
    assert "Registrieren" not in TestClient(app).get("/login").text


def test_admin_pages_forbidden_for_users(app):
    client, _ = register(app, "dave")
    assert client.get("/admin/users").status_code == 403
    assert client.post("/admin/users", data={"username": "eve", "password": "secret-pass"}).status_code == 403
    assert "Benutzer</a>" not in client.get("/entries").text


def test_admin_manages_users(app, users):
    admin = client_for(app, "tester", "test-password")
    page = admin.get("/admin/users")
    assert page.status_code == 200 and "Benutzer anlegen" in page.text

    admin.post("/admin/users", data={"username": "frank", "password": "frank-pass"})
    frank = users.by_name("frank")
    users.store(frank.id).set_settings({"target": "1"})
    frank_client = client_for(app, "frank", "frank-pass")

    admin.post(f"/admin/users/{frank.id}/password", data={"password": "new-password"})
    assert users.authenticate("frank", "new-password") is not None

    admin.post(f"/admin/users/{frank.id}/admin", data={"is_admin": "1"})
    assert users.get(frank.id).is_admin
    admin.post(f"/admin/users/{frank.id}/admin", data={})
    assert not users.get(frank.id).is_admin

    response = admin.post(f"/admin/users/{frank.id}/delete")
    assert "gelöscht" in response.text and users.by_name("frank") is None
    assert not users.store_path(frank.id).exists()
    # the session of a deleted user is no longer valid
    assert frank_client.get("/entries", headers={"accept": "text/html"}, follow_redirects=False).status_code == 303

    tester = users.by_name("tester")
    assert "eigene Konto" in admin.post(f"/admin/users/{tester.id}/delete").text
    assert "letzte Administrator" in admin.post(f"/admin/users/{tester.id}/admin", data={}).text
    assert users.get(tester.id).is_admin


def test_change_own_password(app, users):
    client, _ = register(app, "gina")
    assert "falsch" in client.post("/account/password", data={"current": "nope", "password": "x" * 8,
                                                               "password2": "x" * 8}).text
    response = client.post("/account/password", data={"current": "secret-pass", "password": "another-pass",
                                                       "password2": "another-pass"})
    assert "Passwort geändert" in response.text
    assert users.authenticate("gina", "another-pass") is not None


def test_basic_auth_per_user(app, users):
    register(app, "hank")
    assert TestClient(app).get("/export.xlsx", auth=("hank", "secret-pass")).status_code == 200
    assert TestClient(app).get("/export.xlsx", auth=("hank", "wrong-pass")).status_code == 401


def test_last_admin_protected(users):
    with pytest.raises(UserError):
        users.delete(users.by_name("tester").id)
    other = users.create("ida", "secret-pass", is_admin=True)
    users.delete(users.by_name("tester").id)
    assert users.all() == [other]
    assert users.create("ivan", "secret-pass").id > other.id  # ids (= data files) are never reused


def test_columns_per_user(app, users, store):
    client, _ = register(app, "jane")
    jane = users.store(users.by_name("jane").id)
    admin_columns = [c.name for c in store.columns()]

    client.post("/columns/new", data={"name": "Krypto", "kind": "input", "number_format": "currency", "width": "12"})
    giro = next(c for c in jane.columns() if c.name == "Girokonto")
    client.post(f"/columns/{giro.id}", data={"name": "Hauptkonto", "number_format": "currency", "width": "12",
                                             "asset_group": giro.asset_group, "role": giro.role})
    kommentar = next(c for c in jane.columns() if c.name == "Kommentar")
    client.post(f"/columns/{kommentar.id}/delete")

    names = [c.name for c in jane.columns()]
    assert "Krypto" in names and "Hauptkonto" in names and "Girokonto" not in names and "Kommentar" not in names
    assert [c.name for c in store.columns()] == admin_columns  # the admin's columns are untouched
    admin = client_for(app, "tester", "test-password")
    page = admin.get("/columns").text
    assert "Krypto" not in page and "Hauptkonto" not in page and "Girokonto" in page
    assert "Hauptkonto" in client.get("/columns").text
