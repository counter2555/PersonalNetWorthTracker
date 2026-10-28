# Personal Net Worth Tracker

Self-hosted Service (Docker) zum Erfassen des Vermögens von überall – mit Excel-Export im Format der bisherigen
`NetWorth.xlsx` und dem interaktiven Plotly-Dashboard.

* **Einträge** im Browser anlegen/bearbeiten (auch am Handy), vorausgefüllt mit dem letzten Stand.
  Rechnungen wie `6604,12+895,2` sind erlaubt und bleiben im Excel als Formel erhalten.
* **Dashboard** wie bisher (Kennzahlen, Vermögensentwicklung, Zuwachs, Verteilung, Depot, Hochrechnung),
  zusätzlich als eigenständige HTML-Datei herunterladbar.
* **Einkommens-Dashboard** zum erfassten Nettogehalt: letzte 12 Monate, laufendes Gehalt vs. Sonderzahlungen,
  Jahresvergleich, Jahresverlauf und – mit Vermögens-Einträgen – Aufteilung in gespart und ausgegeben.
* **Excel-Export** jederzeit: Sheet „Net Worth“ mit Formeln, Formaten und bedingter Formatierung, Sheet „Income“.
* **Excel-Import** der bestehenden Datei (Einstellungen → Excel importieren oder `networth import`).
* **Spalten konfigurierbar**: Eingabe-, Formel- und Textspalten anlegen, umbenennen, verschieben, löschen.
* **Login** (Benutzer/Passwort, Session-Cookie; HTTP Basic Auth für Skripte).
* **SQLite** als einzige Datenbank, gespeichert im gemounteten Ordner.

## Start mit Docker

```bash
cp .env.example .env
mkdir data
docker compose build
docker compose run --rm networth networth hash-password
```

Den ausgegebenen Hash (mit verdoppelten `$$`) als `NWT_PASSWORD_HASH` in `.env` eintragen, dann:

```bash
docker compose up -d
```

Der Service läuft auf <http://localhost:8000>. Datenbank (`networth.sqlite`) und Session-Schlüssel liegen im Ordner
`./data` (bzw. `NWT_HOST_DATA_DIR`), der als Volume nach `/data` gemountet wird – diesen Ordner sichern.
Der Container läuft als UID/GID `PUID`/`PGID` (Standard 1000); der Ordner muss für diesen Benutzer schreibbar sein.

Bestehendes Excel übernehmen: im Browser unter *Einstellungen → Excel importieren* oder

```bash
docker compose run --rm -v "/pfad/zu/NetWorth.xlsx:/import.xlsx:ro" networth networth import /import.xlsx
```

Excel per Skript sichern (Basic Auth):

```bash
curl -u admin -o NetWorth.xlsx http://localhost:8000/export.xlsx
```

## Deployment auf den Homeserver

Auf dem Entwicklungsrechner das Image bauen und in die Registry des Homeservers pushen:

```bash
./build.sh
```

Standardziel ist `homeserver.lan:5000/networth:latest`; anpassbar mit `IMAGE=…` und `TAG=…`
(z. B. `TAG=2026-10-28 ./build.sh`).

Auf dem Server reichen `docker-compose.yml`, `.env` und der Datenordner. In `.env` das gepushte Image eintragen:

```
NWT_IMAGE=homeserver.lan:5000/networth:latest
```

Dann starten bzw. aktualisieren:

```bash
docker compose pull && docker compose up -d
```

Hinter einem Reverse-Proxy mit HTTPS `NWT_SECURE_COOKIES=true` setzen; unter einem Unterpfad zusätzlich
`NWT_ROOT_PATH=/networth`. Alle Variablen stehen in [.env.example](.env.example).

## Spalten

Die erste Spalte ist immer `Datum`. Beim ersten Start wird das Layout der bisherigen `NetWorth.xlsx` angelegt.

| Typ     | Bedeutung |
|---------|-----------|
| Eingabe | Betrag pro Eintrag (Kontostand, …) |
| Formel  | berechnet, z. B. `{Flatex Depot}-{Depot Entwicklung}` |
| Text    | freier Text, z. B. Kommentar |

Formeln verweisen mit `{Spalte}` auf Werte desselben Eintrags, mit `{Spalte@prev}` auf den vorherigen und mit
`{Spalte@first}` auf den ersten Eintrag. `SUM({Girokonto}:{Flatex Geld})` summiert alle Spalten dazwischen
(Spaltenreihenfolge wie im Excel). Funktionen: `IF`, `SUM`, `MIN`, `MAX`, `ABS`, `ROUND`. Im Excel-Export wird
daraus eine normale Formel (`=SUM(B5:F5)`). Umbenennen einer Spalte passt alle Formeln an.

Für das Dashboard:

* **Anlagegruppe**: Konto wird im Vermögens-Chart in dieser Gruppe gestapelt (Reihenfolge unter *Einstellungen*).
* **Rolle**: `Summe` (Gesamtvermögen), `Depotwert`, `Depot-Entwicklung`, `KESt`, `Kommentar`.
* **Verrechnen mit**: z. B. Kreditkarte wird mit dem Girokonto verrechnet.

Beim Import werden Formelzellen, deren Wert von der aktuellen Formel abweicht (z. B. alte Zeilen mit einer
anderen Formel oder leere Zellen), als **Festwert** übernommen, damit das Excel unverändert bleibt. Festwerte sind
in der Eintragsliste kursiv und können im Eintrag auf die Formel zurückgesetzt werden.

## Entwicklung

```bash
poetry install
poetry run pytest
NWT_PASSWORD=dev poetry run networth serve --port 8000
```

Testdaten (fiktiv) erzeugen und importieren:

```bash
poetry run python scripts/generate_test_data.py data/example_NetWorth.xlsx
poetry run networth import data/example_NetWorth.xlsx
```

**Keine echten Daten einchecken** – `data/`, `*.xlsx`, `*.sqlite` und `.env` sind in `.gitignore`.

Struktur (`src/networth/`): `formula.py` (Formelsprache), `columns.py` (Spaltenkonfiguration, Standardlayout),
`db.py` (SQLite), `sheet.py` (Berechnung), `excel.py` (Import/Export), `dashboard.py` (Plotly-Dashboard),
`app.py` (FastAPI-Weboberfläche), `auth.py`, `cli.py`.
