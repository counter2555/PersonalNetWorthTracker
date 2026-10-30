import io
import json
import re

import openpyxl
import pytest
from fastapi.testclient import TestClient

from networth.app import create_app
from networth.auth import hash_password, verify_password


@pytest.fixture
def client(config, users, store):
    return TestClient(create_app(config, users))


def login(client):
    response = client.post("/login", data={"username": "tester", "password": "test-password", "next": "/entries"})
    assert response.status_code == 200
    return response


def test_login_required(client):
    response = client.get("/", headers={"accept": "text/html"}, follow_redirects=False)
    assert response.status_code == 303 and "/login" in response.headers["location"]
    assert client.get("/export.xlsx").status_code == 401
    assert client.get("/healthz").status_code == 200


def test_wrong_password(client, monkeypatch):
    monkeypatch.setattr("networth.app.asyncio.sleep", lambda s: _noop())
    response = client.post("/login", data={"username": "tester", "password": "nope"})
    assert response.status_code == 401


async def _noop():
    return None


def test_basic_auth_for_scripts(client):
    response = client.get("/export.xlsx", auth=("tester", "test-password"))
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/vnd.openxmlformats")


def test_open_redirect_blocked(client):
    response = client.post("/login", data={"username": "tester", "password": "test-password",
                                           "next": "//evil.example"}, follow_redirects=False)
    assert response.headers["location"] == "/"


def test_password_hash():
    encoded = hash_password("geheim", iterations=1000)
    assert verify_password("geheim", encoded) and not verify_password("falsch", encoded)


def test_entry_flow_and_dashboard(client, store):
    login(client)
    assert "Noch keine Daten" in client.get("/").text

    columns = {c.name: c.id for c in store.columns()}
    form = {"date": "2024-01-31", f"c{columns['Girokonto']}": "1.000,50", f"c{columns['Sparkonto']}": "500+250",
            f"c{columns['Kreditkarte']}": "-100", f"c{columns['Kommentar']}": "Start"}
    response = client.post("/entries/new", data=form)
    assert response.status_code == 200 and "gespeichert" in response.text
    client.post("/entries/new", data={**form, "date": "2024-02-29", f"c{columns['Girokonto']}": "2000"})

    entries = store.entries()
    assert len(entries) == 2
    assert entries[0].cells[columns["Sparkonto"]].expr == "500+250"

    page = client.get(f"/entries/{entries[0].id}").text
    assert "500+250" in page and "1650,5" in page.replace(".", "")  # Summe computed and shown

    dashboard = client.get("/")
    assert dashboard.status_code == 200 and "chart-wealth" in dashboard.text
    assert "/assets/plotly.min.js" in dashboard.text
    assert client.get("/assets/plotly.min.js").status_code == 200

    bad = client.post("/entries/new", data={**form, f"c{columns['Girokonto']}": "abc"})
    assert bad.status_code == 422 and "Keine Zahl" in bad.text

    client.post(f"/entries/{entries[1].id}/delete")
    assert len(store.entries()) == 1


def test_import_export_via_ui(client, store, example_xlsx):
    login(client)
    with example_xlsx.open("rb") as f:
        response = client.post("/import", data={"confirm": "yes"},
                               files={"file": ("x.xlsx", f, "application/octet-stream")})
    assert "150 Einträge importiert" in response.text

    exported = client.get("/export.xlsx")
    wb = openpyxl.load_workbook(io.BytesIO(exported.content))
    assert wb["Net Worth"].max_row == 151

    download = client.get("/dashboard.html")
    assert "attachment" in download.headers["content-disposition"]
    assert len(download.content) > 3_000_000  # plotly.js inlined


def test_accounts_and_metrics(client, store):
    login(client)
    page = client.get("/columns").text
    assert "Konten &amp; Kennzahlen" in page and "Summe aller Konten" in page
    assert client.get("/columns/new").status_code == 200
    assert "Welche Konten?" in client.get("/columns/new?type=metric").text

    # a new account in a new group counts towards the total right away
    response = client.post("/columns/new", data={"name": "Krypto", "spec": "asset", "group": "__new__",
                                                 "new_group": "Krypto"})
    assert "„Krypto“ gespeichert" in response.text
    by_name = {c.name: c for c in store.columns()}
    assert "{Krypto}" in by_name["Summe"].formula and store.group_order()[-1] == "Krypto"
    assert "<legend>Krypto</legend>" in client.get("/entries/new").text
    assert client.get(f"/columns/{by_name['Krypto'].id}").status_code == 200

    # debts are booked against an account and follow it
    client.post("/columns/new", data={"name": "Kredit", "spec": "liability", "net_into": "Krypto"})
    names = [c.name for c in store.columns()]
    assert names.index("Kredit") == names.index("Krypto") + 1
    assert "verwendet von: Kredit" in client.post(f"/columns/{by_name['Krypto'].id}/delete").text

    # metrics from the catalog
    response = client.post("/columns/new", data={"name": "Krypto-Anteil", "spec": "share",
                                                 "members": ["Krypto", "Kredit"]})
    assert "gespeichert" in response.text
    share = next(c for c in store.columns() if c.name == "Krypto-Anteil")
    assert share.formula == "({Krypto}+{Kredit})/{Summe}" and share.number_format == "percent"
    edit = client.get(f"/columns/{share.id}").text
    assert 'value="Krypto" checked' in edit
    response = client.post("/columns/new", data={"name": "Leer", "spec": "sum"})
    assert response.status_code == 422 and "mindestens ein Konto" in response.text
    response = client.post("/columns/new", data={"name": "Kaputt", "spec": "custom", "formula": "{Gibtsnicht}+1"})
    assert response.status_code == 422 and "unbekannte Spalte" in response.text

    # deleting an account drops it from sums and shares
    client.post(f"/columns/{next(c.id for c in store.columns() if c.name == 'Kredit')}/delete")
    assert next(c for c in store.columns() if c.name == "Krypto-Anteil").formula == "{Krypto}/{Summe}"

    # moving accounts never changes what the total adds up
    total = sorted(next(c for c in store.columns() if c.spec == "total").formula.split("+"))
    sparkonto = next(c for c in store.columns() if c.name == "Sparkonto")
    client.post(f"/columns/{sparkonto.id}/move", data={"offset": "-1"})
    names = [c.name for c in store.columns()]
    assert names.index("Sparkonto") < names.index("Girokonto")
    assert sorted(next(c for c in store.columns() if c.spec == "total").formula.split("+")) == total


def test_drag_and_drop_and_groups(client, store):
    login(client)
    ids = {c.name: c.id for c in store.columns()}
    order = [["Depot", [ids["Flatex Depot"]]], ["Konten", [ids["Sparkonto"], ids["Girokonto"]]],
             ["Vorsorge", [ids["Bausparer"], ids["Bundesschatz"]]], ["Anleihen", []]]
    client.post("/columns/order", data={"order": json.dumps(order)})
    assert store.group_order() == ["Depot", "Konten", "Vorsorge", "Anleihen"]
    names = [c.name for c in store.columns()]
    assert names[:4] == ["Flatex Depot", "Depot Entwicklung", "Sparkonto", "Girokonto"]
    assert next(c for c in store.columns() if c.name == "Bundesschatz").asset_group == "Vorsorge"

    assert "noch Konten" in client.post("/groups/delete", data={"group": "Konten"}).text
    client.post("/groups/delete", data={"group": "Anleihen"})
    client.post("/groups/rename", data={"group": "Vorsorge", "name": "Pension"})
    client.post("/groups/move", data={"group": "Pension", "offset": "-1"})
    client.post("/groups/new", data={"name": "Immobilien"})
    assert store.group_order() == ["Depot", "Pension", "Konten", "Immobilien"]
    assert next(c for c in store.columns() if c.name == "Bausparer").asset_group == "Pension"


def test_depot_is_one_account(client, store):
    login(client)
    by_name = {c.name: c for c in store.columns()}
    page = client.get(f"/columns/{by_name['Flatex Depot'].id}").text
    assert 'value="Depot Entwicklung"' in page and 'value="27,5"' in page
    assert client.get(f"/columns/{by_name['Depot Entwicklung'].id}", follow_redirects=False).status_code == 303

    client.post(f"/columns/{by_name['Flatex Depot'].id}", data={
        "name": "Depot", "spec": "depot", "group": "Depot", "gain_name": "Depot Gewinn", "tax_rate": "25"})
    by_name = {c.name: c for c in store.columns()}
    assert by_name["Kest"].formula == "IF({Depot Gewinn}>0,-{Depot Gewinn}*0.25,0)"
    assert by_name["Depot Einzahlung"].formula == "{Depot}-{Depot Gewinn}"
    response = client.post(f"/columns/{by_name['Depot'].id}", data={"name": "Depot", "spec": "asset",
                                                                    "group": "Depot"})
    assert response.status_code == 422 and "bleibt ein Depot" in response.text

    # the depot takes its gain, the tax and the depot metrics along; sums keep working
    response = client.post(f"/columns/{by_name['Depot'].id}/delete")
    assert "Depot Gewinn" in response.text and "Netto Gewinn" in response.text
    names = {c.name for c in store.columns()}
    assert not {"Depot", "Depot Gewinn", "Kest", "Depot Einzahlung", "Depot Gewinn Anteil"} & names
    assert "{Kest}" not in next(c for c in store.columns() if c.name == "Summe").formula
    assert "Depot" not in client.get("/columns/new?type=metric").text.split("Welche Konten?")[0]


def test_income_and_settings(client, store):
    login(client)
    client.post("/income", data={"date": "2024-01-31", "amount": "2.600,00"})
    assert store.income()[0].amount == 2600.0
    client.post("/settings", data={"target": "500000"})
    assert store.target() == 500000.0


def test_income_dashboard(client, store, example_xlsx):
    login(client)
    assert "Noch kein Einkommen" in client.get("/income/dashboard").text

    client.post("/income", data={"date": "2024-01-31", "amount": "2.600"})
    page = client.get("/income/dashboard")  # income without net worth entries: no savings figures
    assert page.status_code == 200 and "chart-income-monthly" in page.text
    assert "chart-income-spending" not in page.text

    with example_xlsx.open("rb") as f:
        client.post("/import", data={"confirm": "yes"}, files={"file": ("x.xlsx", f, "application/octet-stream")})
    page = client.get("/income/dashboard")
    assert page.status_code == 200
    assert all(f"chart-income-{name}" in page.text for name in ("monthly", "yearly", "cumulative", "spending"))
    assert "Sparquote" in page.text and 'class="active" aria-current="page">Einkommens-Dashboard' in page.text

    download = client.get("/income-dashboard.html")
    assert "attachment" in download.headers["content-disposition"]
    assert len(download.content) > 3_000_000  # plotly.js inlined


def test_entries_pagination(client, store, example_xlsx):
    login(client)
    with example_xlsx.open("rb") as f:
        client.post("/import", data={"confirm": "yes"}, files={"file": ("x.xlsx", f, "application/octet-stream")})
    links = lambda text: re.findall(r'href="/entries/(\d+)"', text)  # noqa: E731
    newest = [str(e.id) for e in reversed(store.entries())]

    first = client.get("/entries")  # 150 entries, 50 per page, newest first
    assert "Einträge (150)" in first.text and "1–50 von 150 · Seite 1 von 3" in first.text
    assert links(first.text) == newest[:50]
    assert 'href="/entries?per=50&amp;page=2"' in first.text

    last = client.get("/entries?page=3")
    assert links(last.text) == newest[100:] and "101–150 von 150" in last.text
    assert links(client.get("/entries?page=99").text) == newest[100:]  # out of range: last page
    assert len(links(client.get("/entries?per=25&page=2").text)) == 25
    everything = client.get("/entries?per=0")
    assert len(links(everything.text)) == 150 and "Seite 1 von" not in everything.text
    assert len(links(client.get("/entries?per=7").text)) == 50  # unsupported size: default


def test_entries_empty(client):
    login(client)
    assert "Noch keine Einträge" in client.get("/entries").text


def test_entry_form_sections_and_field_errors(client, store):
    login(client)
    page = client.get("/entries/new").text
    legends = re.findall(r"<legend>(.*?)</legend>", page)
    assert legends == ["Konten", "Depot", "Vorsorge", "Anleihen"]
    order = re.findall(r'<label>(.*?)\s*<input class="number"', page)
    assert order[:7] == ["Girokonto", "Kreditkarte", "Sparkonto", "Urlaubskonto", "Flatex Geld", "Flatex Depot",
                         "Depot Entwicklung"]  # credit card after its account, gain after the depot

    columns = {c.name: c.id for c in store.columns()}
    bad = client.post("/entries/new", data={"date": "2024-01-31", f"c{columns['Girokonto']}": "abc",
                                            f"c{columns['Sparkonto']}": "1++"})
    assert bad.status_code == 422 and "Bitte 2 Felder prüfen" in bad.text
    key = f"c{columns['Girokonto']}"
    assert f'aria-describedby="err-{key}"' in bad.text and f'id="err-{key}">Keine Zahl' in bad.text
    assert 'value="abc"' in bad.text  # typed input is kept


def test_achievements(client, store, example_xlsx):
    login(client)
    assert "Noch keine Erfolge" in client.get("/achievements").text
    with open(example_xlsx, "rb") as f:
        client.post("/import", data={"confirm": "yes"}, files={"file": ("x.xlsx", f, "application/octet-stream")})
    page = client.get("/achievements")
    assert page.status_code == 200
    assert "Erste Schritte" in page.text and "Schande" in page.text and "Freigeschaltet" in page.text
