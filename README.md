# Personal Net Worth Tracker

Self-hosted Service (Docker) zum Erfassen des Vermögens von überall – mit Excel-Export im Format der bisherigen
`NetWorth.xlsx` und dem interaktiven Plotly-Dashboard.

* **Einträge** im Browser anlegen/bearbeiten (auch am Handy), vorausgefüllt mit dem letzten Stand. Beim Tippen
  zeigt eine Leiste die neue Summe und die Änderung zum letzten Eintrag, jedes Feld seinen Vorwert; doppelte Daten
  werden erkannt, gelöschte Einträge lassen sich rückgängig machen. Am Handy gibt es eine Tab-Leiste unten.
  Rechnungen wie `6604,12+895,2` sind erlaubt und bleiben im Excel als Formel erhalten.
* **Dashboard** wie bisher (Kennzahlen, Vermögensentwicklung, Zuwachs, Verteilung, Depot, Hochrechnung),
  zusätzlich als eigenständige HTML-Datei herunterladbar.
* **Einkommens-Dashboard** zum erfassten Nettogehalt: letzte 12 Monate, Gehalt pro Monat und Jahr,
  Jahresvergleich, Jahresverlauf und – mit Vermögens-Einträgen – Aufteilung in gespart und ausgegeben.
* **Erfolge**: Achievements im Stil von Spiele-Plattformen zu Vermögen, Börse, Disziplin, Einkommen und Sparquote –
  mit Stufen (Bronze bis Platin), Punkten, Fortschrittsbalken, geheimen und negativen „Schande“-Achievements.
* **Excel-Export** jederzeit: Sheet „Net Worth“ mit Formeln, Formaten und bedingter Formatierung, Sheet „Income“.
* **Excel-Import** der bestehenden Datei (Einstellungen → Excel importieren oder `networth import`) mit Vorschau
  vor dem Ersetzen und Download einer Sicherung des aktuellen Stands.
* **Konten & Kennzahlen** statt Spalten-Konfiguration: Konten in Gruppen anlegen und per Drag & Drop sortieren,
  die Summe rechnet automatisch, Kennzahlen aus einem Katalog oder als eigene Formel.
* **Mehrbenutzerfähig**: Registrierung mit Benutzername und Passwort, jedes Konto hat eigene, getrennte Daten
  (Einträge, Konten, Einkommen, Einstellungen). Login per Session-Cookie, HTTP Basic Auth für Skripte.
* **Administratoren** verwalten unter *Benutzer* die Konten: anlegen, löschen (inkl. aller Daten), Passwort
  zurücksetzen, Admin-Rechte vergeben/entziehen und die Selbst-Registrierung öffnen oder schließen.
* **SQLite** als einzige Datenbank, gespeichert im gemounteten Ordner.

## Start mit Docker

```bash
cp .env.example .env
mkdir data
docker compose build
docker compose run --rm networth networth hash-password
```

Den ausgegebenen Hash (mit verdoppelten `$$`) als `NWT_PASSWORD_HASH` in `.env` eintragen. Damit wird beim ersten
Start der erste **Administrator** `NWT_USERNAME` angelegt; danach sind die Konten in der Datenbank maßgeblich
(Passwortänderungen unter *Konto* bzw. *Benutzer*, Änderungen an `.env` wirken sich nicht mehr aus). Dann:

```bash
docker compose up -d
```

Der Service läuft auf <http://localhost:8000>. Datenbanken (`users.sqlite` mit den Konten, `users/<id>.sqlite` mit den
Daten je Benutzer) und Session-Schlüssel liegen im Ordner
`./data` (bzw. `NWT_HOST_DATA_DIR`), der als Volume nach `/data` gemountet wird – diesen Ordner sichern.
Der Container läuft als UID/GID `PUID`/`PGID` (Standard 1000); der Ordner muss für diesen Benutzer schreibbar sein.

Bestehendes Excel übernehmen: im Browser unter *Einstellungen → Excel importieren* oder

```bash
docker compose run --rm -v "/pfad/zu/NetWorth.xlsx:/import.xlsx:ro" networth networth import --user admin /import.xlsx
```

**Upgrade von der Einzelbenutzer-Version:** Beim ersten Start wird die bisherige `networth.sqlite` automatisch zu den
Daten des Administrators `NWT_USERNAME` (nach `users/1.sqlite` verschoben). Vorher den Datenordner sichern.

Benutzer auf der Kommandozeile verwalten (z. B. Admin-Passwort vergessen):

```bash
docker compose run --rm networth networth users
docker compose run --rm networth networth create-user bob
docker compose run --rm networth networth set-password admin --admin
```

Excel per Skript sichern (Basic Auth):

```bash
curl -u admin -o NetWorth.xlsx http://localhost:8000/export.xlsx
```

Jeder Benutzer exportiert so seine eigenen Daten.

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

## Konten & Kennzahlen

Jeder Benutzer richtet unter *Konten & Kennzahlen* ein, was er erfasst – unabhängig von anderen Konten. Der erste
Administrator startet mit dem Layout der bisherigen `NetWorth.xlsx`, neue Benutzer mit einem schlanken Layout
(Girokonto, Sparkonto, Kreditkarte, Depot). Unter *Zurücksetzen* lässt sich jederzeit eines der beiden Layouts
übernehmen; Felder mit gleichem Namen behalten ihre Werte.

**Konten** liegen in Gruppen (z. B. Konten, Depot, Vorsorge). Die Reihenfolge der Gruppen und Konten gilt überall:
im Eintragsformular, im Excel und im Vermögens-Chart (erste Gruppe unten). Konten lassen sich mit der Maus in eine
andere Position oder Gruppe ziehen, alternativ mit ↑/↓.

| Art            | Bedeutung |
|----------------|-----------|
| Guthaben       | Kontostand, zählt zum Vermögen |
| Schulden       | z. B. Kreditkarte, gehört zu einem Konto und wird damit verrechnet (Betrag negativ eintragen) |
| Depot          | Wert und Gewinn (Depot-Entwicklung) werden eingetragen, die Kapitalertragsteuer wird berechnet |
| Sonstiger Wert | wird erfasst, zählt aber nicht zum Vermögen |

**Automatisch berechnet**: die *Summe* ist immer die Summe aller Konten (abzüglich der Kapitalertragsteuer), neue
Konten zählen sofort mit. Die *Kapitalertragsteuer* ist der eingestellte Prozentsatz (Standard 27,5 %) vom
positiven Depot-Gewinn.

**Kennzahlen** kommen aus einem Katalog: Veränderung zum vorigen bzw. ersten Eintrag (% oder €), Summe oder Anteil
ausgewählter Konten, Depot-Kennzahlen (eingezahltes Kapital, Gewinn nach Steuer, Gewinnanteil). Für alles andere
gibt es *Eigene Formel*: `{Spalte}` verweist auf einen Wert desselben Eintrags, `{Spalte@prev}` auf den vorherigen,
`{Spalte@first}` auf den ersten Eintrag. Funktionen: `IF`, `SUM`, `MIN`, `MAX`, `ABS`, `ROUND`. Im Excel-Export wird
daraus eine normale Formel (`=B5+C5`). Umbenennen passt alle Formeln an, gelöschte Konten fallen aus Summen und
Anteilen heraus.

**Textfelder**: der Kommentar jedes Eintrags (Anmerkungen im Chart) und beliebige weitere Notizfelder.

Spaltenbreite und bedingte Formatierung („Hervorhebung“) fürs Excel stehen bei jedem Feld unter *Erweitert*.

Beim Import werden unbekannte Zahlenspalten als *sonstiger Wert* angelegt (die Summe bleibt unverändert) und
können danach zu einem Konto gemacht werden. Formelzellen, deren Wert von der aktuellen Formel abweicht (z. B. alte
Zeilen mit einer anderen Formel oder leere Zellen), werden als **Festwert** übernommen, damit das Excel unverändert
bleibt. Festwerte sind in der Eintragsliste kursiv und können im Eintrag auf die Formel zurückgesetzt werden.

**Umstellung bestehender Daten:** Spalten aus früheren Versionen werden beim ersten Start automatisch zu Konten,
Kennzahlen und Textfeldern. Bereiche wie `SUM({A}:{C})` werden dabei in einzelne Verweise aufgelöst. Weicht die
bisherige Summe oder Steuerformel von der automatischen Berechnung ab, bleibt sie als eigene Formel „… (alt)“
erhalten und die Seite *Konten & Kennzahlen* zeigt einen Hinweis.

## Entwicklung

```bash
poetry install
poetry run pytest
NWT_PASSWORD=dev poetry run networth serve --port 8000
```

Testdaten (fiktiv) erzeugen und importieren:

```bash
poetry run python scripts/generate_test_data.py data/example_NetWorth.xlsx
poetry run networth import --user admin data/example_NetWorth.xlsx
```

**Keine echten Daten einchecken** – `data/`, `*.xlsx`, `*.sqlite` und `.env` sind in `.gitignore`.

Struktur (`src/networth/`): `formula.py` (Formelsprache), `columns.py` (Spalten, Abhängigkeiten),
`structure.py` (Konten, Kennzahlen, Textfelder → Spalten; Standardlayouts, Umstellung),
`db.py` (SQLite), `sheet.py` (Berechnung), `excel.py` (Import/Export), `dashboard.py` (Plotly-Dashboard),
`app.py` (FastAPI-Weboberfläche), `auth.py`, `users.py` (Konten, Datenbank je Benutzer), `cli.py`.
