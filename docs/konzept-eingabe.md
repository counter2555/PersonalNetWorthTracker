# Konzept: Einfachere Eingabe neuer Einträge

Ziel: Ein neuer Eintrag soll in wenigen Sekunden erledigt sein – vor allem am Handy. Alle Beträge bleiben
**Textfelder** (keine Slider, Stepper oder `type=number`-Felder), damit Komma, Minus und Rechnungen wie
`6604,12+895,2` weiterhin direkt eingetippt werden können.

**Stand:** Umgesetzt sind die Bausteine 1 (Gruppen), 3 (Antippen markiert), 7 (Enter springt weiter) und 8 (Fehler am Feld). Die Bausteine 2, 4, 5 und 6 sind bewusst nicht umgesetzt.

## Was die Eingabe heute schwer macht

Formular `entries/new` (`templates/entry_form.html`):

1. **Eine flache Liste** aller Eingabespalten (Standard: 11 Felder) in Spaltenreihenfolge. Am Handy ist das eine
   lange Spalte ohne Orientierung.
2. **Kein Bezug zum letzten Stand.** Die Felder sind zwar vorausgefüllt, aber nach dem Überschreiben sieht man
   nicht mehr, was vorher drinstand und wie groß die Änderung ist. Tippfehler (eine Null zu viel) fallen erst im
   Dashboard auf.
3. **Überschreiben ist umständlich.** Zum Ersetzen des vorausgefüllten Werts muss man den Inhalt erst markieren
   bzw. löschen.
4. **Ergebnis erst nach dem Speichern.** Summe und Formelwerte („Berechnete Werte“) gibt es nur beim Bearbeiten
   eines gespeicherten Eintrags.
5. **Vorzeichen.** Die Kreditkarte muss negativ eingetragen werden; das Minus vergisst man leicht.
6. **Enter speichert sofort** statt ins nächste Feld zu springen.
7. **Fehler stehen gesammelt oben**, nicht beim betroffenen Feld.

## Lösungsidee im Überblick

```
┌─ Neuer Eintrag ─────────────────────────────── Datum [29.10.2026] ┐
│                                                                   │
│ Konten                                                            │
│  Girokonto      [   2.143,50 ]  zuletzt 1.980,10 €   +163,40 €    │
│  Kreditkarte −  [     432,10 ]  zuletzt −210,00 €    −222,10 €    │
│  Sparkonto      [   5.640,00 ]  zuletzt 5.600,00 €    +40,00 €    │
│  Urlaubskonto   [     780,00 ]  unverändert                       │
│  Flatex Geld    [     310,00 ]  unverändert                       │
│                                                                   │
│ Depot                                                             │
│  Flatex Depot   [  12.410,77 ]  zuletzt 12.012,40 €  +398,37 €    │
│  Depot Entw.    [   1.020,77 ]  zuletzt 872,40 €     +148,37 €    │
│                                                                   │
│ ▸ Unverändert seit 4 Einträgen (Bausparer, Bundespensionskasse,   │
│   Allianz Vorsorgekasse, Bundesschatz)                            │
│                                                                   │
│  Kommentar      [                                    ]            │
├───────────────────────────────────────────────────────────────────┤
│ Summe 38.912,40 €  (+1.021,07 € seit 22.10.)        [ Speichern ] │  ← bleibt unten sichtbar
└───────────────────────────────────────────────────────────────────┘
```

Alle Felder bleiben normale `<input>`-Textfelder; neu ist nur, was um sie herum angezeigt wird.

## Bausteine

### 1. Felder nach Anlagegruppe gliedern

Die Gruppen existieren schon in der Spaltenkonfiguration (`asset_group`). Das Formular zeigt je Gruppe eine
Zwischenüberschrift, Reihenfolge nach der ersten Spalte der Gruppe. Zuordnung für Spalten ohne Gruppe:

* `net_into` gesetzt (Kreditkarte → Girokonto): Gruppe des Zielkontos, direkt danach.
* Rolle `depot_gain` (Depot Entwicklung): Gruppe der Spalte mit Rolle `depot_value`, direkt danach.
* sonst: Abschnitt „Sonstiges“ am Ende.

Kein neues Konfigurationsfeld nötig.

### 2. Letzter Wert und Änderung neben jedem Feld

Unter bzw. neben jedem Feld steht klein „zuletzt 1.980,10 €“ und die Differenz zum aktuellen Feldinhalt
(„+163,40 €“, „unverändert“). Die Differenz aktualisiert sich beim Tippen. Große Sprünge (z. B. > 50 % oder
> 5.000 €) werden farblich hervorgehoben – das fängt Tippfehler, ohne die Eingabe zu blockieren.

Beim Bearbeiten eines alten Eintrags ist „zuletzt“ der Eintrag davor.

Umsetzung: Der Server gibt den Vorwert als `data-prev` mit; ein kleines Skript wertet den Feldinhalt aus
(gleiche Regeln wie `normalize_number_input`: Komma/Punkt, `€`, Leerzeichen, `+ - * / ( )`). Kann das Skript
einen Ausdruck nicht auswerten, zeigt es nichts an – maßgeblich bleibt die Prüfung auf dem Server.

### 3. Antippen markiert den ganzen Inhalt

Beim Fokussieren wird der Feldinhalt komplett markiert (`select()`), der erste Tastendruck ersetzt also den alten
Wert. Wer stattdessen ergänzen möchte, tippt ein zweites Mal ins Feld und hängt z. B. `+250` an
(`5600+250`) – das funktioniert mit der bestehenden Rechen-Eingabe bereits.

### 4. Unveränderte Konten einklappen

Konten, deren Wert in den letzten 3 Einträgen gleich geblieben ist (typisch: Vorsorge, Bundesschatz), landen in
einem eingeklappten Abschnitt (`<details>`) „Unverändert seit N Einträgen“. Die Felder sind trotzdem im
Formular und werden mit dem Vorwert gespeichert; aufklappen genügt zum Ändern. Ein Feld mit Fehler klappt den
Abschnitt automatisch auf.

Damit sieht man im Normalfall nur noch die 5–7 Felder, die sich wirklich bewegen.

### 5. Vorzeichen pro Spalte festlegen

Neue Spalteneigenschaft **„Als negativen Betrag speichern“** (z. B. für Kreditkarte). Im Formular trägt man den
Betrag positiv ein (`432,10`), gespeichert wird `−432,10`. Das Feld zeigt ein „−“ vor dem Eingabefeld, damit klar
ist, was passiert. Wird trotzdem ein Minus eingegeben, bleibt der Wert negativ (kein doppeltes Umdrehen).

Export, Import, Formeln und Dashboard sehen weiterhin den negativen Wert – die Umkehr passiert nur im Formular.
Dafür braucht die Spaltentabelle ein neues Feld (kleine DB-Migration) und die Spaltenmaske eine Checkbox.

### 6. Live-Summe in einer festen Leiste unten

Am unteren Bildschirmrand bleibt eine Leiste mit **Summe** (Rolle `total`) und der **Veränderung seit dem letzten
Eintrag**, daneben der Speichern-Button. Optional aufklappbar: alle Formelwerte wie heute unter „Berechnete
Werte“, aber schon vor dem Speichern.

Umsetzung: neuer Endpunkt `POST /entries/preview`, der die Formulardaten wie beim Speichern parst, den Eintrag
nicht speichert, sondern mit `sheet.compute` durchrechnet und die Werte als JSON zurückgibt. Das Formular ruft
ihn nach einer kurzen Tipp-Pause (≈ 300 ms) auf. So rechnet nur der Server Formeln, es gibt keine zweite
Formel-Implementierung in JavaScript. Ohne JavaScript funktioniert das Formular wie bisher.

### 7. Tastatur: Enter springt weiter

* `enterkeyhint="next"` an allen Betragsfeldern, Enter springt ins nächste sichtbare Feld; im letzten Feld
  speichert Enter.
* `inputmode="text"` bleibt bewusst: Nur die volle Tastatur hat auf allen Handys Minus, Komma und `+`.
* `autocomplete="off"` bleibt, damit der Browser keine alten Werte vorschlägt.

### 8. Fehler direkt am Feld

Statt einer Sammelmeldung oben bekommt jedes fehlerhafte Feld einen roten Rahmen und den Text darunter
(„Keine Zahl: '12,,5'“). Oben bleibt nur ein kurzer Hinweis „2 Felder prüfen“, der Fokus springt ins erste
fehlerhafte Feld. Der Server liefert die Fehler dafür pro Spalte statt als zusammengefügten String.

## Bewusst nicht vorgesehen

* **Slider, Stepper, `type=number`**: schwer zu treffen, kein Komma/Minus auf manchen Handys, keine Rechnungen.
* **Leeres Feld = „unverändert“**: Leer bedeutet heute „kein Wert“ (z. B. Depot vor der Eröffnung) und wird von
  Formeln so behandelt. Die Vorbelegung mit dem letzten Wert bleibt deshalb.
* **Delta-Eingabe mit führendem `+`** (`+250` = „250 dazu“): `+250` ist heute ein gültiger Betrag; die
  Umdeutung wäre eine stille Falle. Ergänzen per `5600+250` reicht.

## Umsetzung in Etappen

| Etappe | Inhalt | Aufwand | Änderungen |
|---|---|---|---|
| 1 | Gruppen (1), Vorwert + Differenz (2), Markieren beim Antippen (3), Enter springt weiter (7), Fehler am Feld (8) | klein | Template, CSS, ~60 Zeilen JS, Fehler pro Spalte in `save_entry_from_form` |
| 2 | Live-Summe mit `POST /entries/preview` (6) | mittel | neuer Endpunkt, Leiste im Template, Tests |
| 3 | Unverändertes einklappen (4), Vorzeichen pro Spalte (5) | mittel | Spalteneigenschaft + Migration, Spaltenmaske, Tests |

Etappe 1 bringt den größten Teil des Nutzens und ändert nichts an Datenbank oder Export.

## Offene Fragen

1. Ab welcher Änderung soll ein Wert als „auffällig“ markiert werden – Prozent, fester Betrag oder beides?
2. Reichen „3 Einträge unverändert“ zum Einklappen, oder soll man Spalten fest als „selten“ markieren können?
3. Soll das Nettogehalt optional im selben Formular erfasst werden können (Feld „Gehalt seit letztem Eintrag“),
   statt nur auf der Einkommens-Seite?
