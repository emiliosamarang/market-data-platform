# Market Data Platform — Zielbild & Roadmap

Stand: 25.08.2026

Dieses Dokument beschreibt (1) wie das fertige Produkt aussieht und sich anfühlt,
(2) wie die Architektur dahinter aufgebaut ist und (3) in welchen Schritten wir
dorthin kommen. Es ergänzt `NOTES.md` (laufender Arbeitsstand) und `README.md`
(Kurzbeschreibung fürs Repo).

---

## 1. Was das Ding am Ende ist

**In einem Satz:** Eine Datenplattform, die Kursdaten mehrerer Kryptobörsen
automatisiert einsammelt, auf Qualität prüft, zu analysefähigen Tabellen
aufbereitet und in Power-BI-Dashboards auswertbar macht — inklusive
Backtesting von Handelsstrategien als analytischem Anwendungsfall.

**Was es ausdrücklich nicht ist:** Kein Trading-Bot. Es wird nichts gekauft,
nichts verkauft, kein Echtgeld angefasst. Die Strategie-Logik bleibt drin, aber
als *Analyseobjekt* — die Frage ist nicht mehr "verdient das Geld?", sondern
"kann ich Signale, Indikatoren und Strategie-Performance sauber modellieren und
visualisieren?".

**Wozu es gut ist:** Portfolio-Stück für den Einstieg Richtung
Data Engineering / BI. Es zeigt exakt die Kette, die in einem Konzern-Data-Team
gebraucht wird: Quellanbindung → Raw Layer → Qualitätssicherung → Modellierung →
Semantic Layer → Dashboard. Dass die Daten aus Krypto kommen statt aus Kassen-
oder Warenwirtschaftssystemen, ist technisch nebensächlich — die Muster sind
dieselben (Zeitreihen, Stammdaten, Nachladen, Idempotenz, Data Quality Gates).

---

## 2. Wie sich das in der Anwendung anfühlt

### Der tägliche Lauf (automatisiert, ohne dich)

Nachts um 02:00 startet ein Job. Er fragt pro Symbol und Intervall die Börsen-APIs
nach allem, was seit dem letzten Lauf dazugekommen ist, und schreibt es in den
Raw Layer. Weil der Layer idempotent ist, ist es egal, ob der Job einmal, dreimal
oder nach zwei Wochen Pause läuft — es entstehen keine Duplikate und keine Lücken.

Direkt danach läuft der Quality Check: Sind alle erwarteten Zeitstempel da? Gibt
es Sprünge im Raster? Sind Preise plausibel (kein `high < low`, keine Nullen, keine
Ausreißer jenseits definierter Schwellen)? Weichen die beiden Datenquellen für
dasselbe Symbol stärker voneinander ab als erlaubt? Das Ergebnis landet als
Report-Tabelle in der Datenbank — nicht nur als Logzeile, sondern als Fakt, den
man später auswerten kann.

Fällt der Check durch, bricht die Pipeline ab, die nachgelagerten Schritte laufen
nicht, und du bekommst eine Nachricht. Fällt er durch, läuft die Transformation:
Aus dem Raw Layer werden Indikatoren berechnet (EMA, RSI, MACD, ATR), Signale
abgeleitet und alles in ein sternförmiges Modell geschrieben. Am Ende steht ein
Datenbestand, den Power BI direkt anzapfen kann.

### Was du morgens siehst

Du machst Power BI auf und hast vier Bereiche:

**Market Overview** — Kursverlauf pro Symbol über wählbare Zeiträume,
Volatilität, Volumen, Korrelationen zwischen den fünf Symbolen. Der Teil, den
jeder versteht.

**Indicator Explorer** — Für ein gewähltes Symbol und Intervall: Kerzen mit
überlagerten Indikatoren, aktueller Zustand der Trendfilter, wie oft ein Indikator
in der Vergangenheit ein Signal ausgelöst hat. Hier siehst du die Strategie
*arbeiten*, ohne dass sie handelt.

**Signal & Strategy Performance** — Alle historisch erzeugten Signale als
Tabelle und Zeitstrahl. Pro Backtest-Lauf: Trades, Trefferquote, Profit Factor,
Drawdown, Gebührenanteil. Und — das ist der eigentlich interessante Teil — der
Vergleich mehrerer Läufe gegeneinander: Was passiert mit dem Profit Factor, wenn
ich den RSI-Schwellwert oder das ATR-Multiple verändere? Jeder Backtest-Lauf ist
ein Datensatz mit seinen Parametern, also lässt sich das als Parametervergleich
visualisieren statt als Zettelwirtschaft.

**Data Quality & Pipeline Health** — Wann lief die Pipeline zuletzt, wie
vollständig ist der Bestand pro Symbol/Intervall, wie viele Zeilen kamen dazu,
welche Checks sind wann durchgefallen. Das ist die Seite, die einen Recruiter im
Data-Bereich am meisten beeindruckt, weil sie zeigt, dass du an Betrieb denkst
und nicht nur an den Happy Path.

### Was du machst, wenn du etwas ausprobieren willst

Du änderst Parameter in einer Config, startest den Backtest, er schreibt einen
neuen Lauf in die Datenbank, du lädst das Dashboard neu und vergleichst gegen die
bisherigen Läufe. Kein Copy-Paste von Terminalausgaben, keine Excel-Files.

---

## 3. Architektur

```
Binance API ─┐
             ├─→ Ingestion (Python)  →  RAW Layer      (Parquet / ADLS Gen2)
Quelle #2  ──┘        │                    │
                      │                    ▼
                      │                Quality Checks  →  Report-Tabelle
                      │                    │
                      │                    ▼
                      └──────────→   CURATED Layer     (Azure SQL, Star Schema)
                                           │
                                           ▼
                                     Power BI Semantic Model
                                           │
                                           ▼
                                       Dashboards
```

**Raw Layer** — unveränderte Quelldaten, partitioniert nach
Symbol / Intervall / Datum, append-only, idempotent. Steht bereits.

**Curated Layer** — Star Schema:
`dim_symbol`, `dim_date`, `dim_interval`, `dim_source`,
`fact_ohlcv`, `fact_indicator`, `fact_signal`, `fact_backtest_run`,
`fact_backtest_trade`, `fact_quality_check`.

**Semantic Layer** — Power-BI-Modell mit DAX-Measures (Rendite, Drawdown,
Profit Factor, Trefferquote, Datenvollständigkeit in Prozent).

---

## 4. Roadmap

### Phase 1 — Data Quality Layer
*Ziel: Die Pipeline erkennt selbst, wenn Daten kaputt oder unvollständig sind.*

- `ingestion/quality.py`: Lückenprüfung im Zeitraster, Duplikate,
  OHLC-Plausibilität, Aktualität des letzten Candles, Ausreißererkennung
- CLI-Einstieg mit Report-Ausgabe und Exit-Code ≠ 0 bei harten Verstößen
  (konsistent zur bestehenden INCOMPLETE-Logik im Backtest)
- Tests für jeden Check mit bewusst kaputten Fixture-Daten
- Report wird als Datei geschrieben, damit er später in die DB wandern kann

**Warum zuerst:** Das ist der Check, der die 452-vs-138-Trades-Sache gefunden
hätte, bevor sie zwei Stunden Debugging gekostet hat.

### Phase 2 — Zweite Datenquelle ✅
*Ziel: Multi-Source-Fähigkeit beweisen und Daten gegeneinander validieren.*

- Zweiten `MarketDataSource` implementiert: Kraken, öffentliche OHLC-API ohne
  API-Key. Wichtige Einschränkung, erst gegen die Live-API verifiziert statt
  aus der Doku übernommen: der Endpoint liefert nie mehr als die letzten
  ~720 Kerzen (~30 Tage bei 1h, ~120 Tage bei 4h), unabhängig vom
  `since`-Parameter — kein Pagination-Problem, sondern eine harte Grenze des
  Endpoints. Kraken taugt damit für Cross-Validation des aktuellen Fensters,
  nicht für einen vollen Zweitquellen-Backfill über die gesamte Binance-Historie.
- Quelle war bereits Partitionsdimension im Raw Layer
  (`{asset_class}/{source}/{symbol}/{interval}/{date}`, seit Phase 1) — keine
  Migration nötig, Kraken-Daten liegen einfach zusätzlich unter `.../kraken/...`.
- `ingestion/load.py` und `ingestion/quality.py` haben beide ein `--source`
  Flag (Registry `binance`/`kraken`) bekommen, damit derselbe CLI-Einstieg für
  jede Quelle funktioniert.
- Cross-Source-Check in `quality.py` (`--compare-source`): Abweichung auf
  Close-Basis, 0,5 % Schwelle, WARNING (nie blockierend — verschiedene Börsen
  sind verschiedene Märkte mit eigener Liquidität). Fehlende Kerze in einer
  Quelle ist ein eigener Befund (`cross_source_gaps`), kein Preisvergleich —
  und nur innerhalb des Fensters geprüft, in dem beide Quellen tatsächlich
  Daten haben (sonst würde Krakens Tiefenlimit die gesamte ältere
  Binance-Historie fälschlich als "Lücke" markieren).
- Ergebnis gegen echte Daten: alle 5 Symbole, keine Coverage-Lücken im
  überlappenden Fenster, Preise stimmen bei 1h durchgehend überein; bei 4h
  weicht ausschließlich die aktuell noch offene, sich füllende Kerze leicht ab
  (0,8–1,3 %) — plausibel für eine unabgeschlossene Kerze zwischen zwei
  unabhängigen Orderbooks, kein Datenfehler. Details in `NOTES.md`.

**Offene Frage für später, bewusst hier notiert statt implizit im Code
entschieden:** Wenn sich Binance und Kraken widersprechen — welche Quelle
gilt? Antwort für jetzt: **keine**. Der Raw Layer speichert beide Quellen
unverändert nebeneinander; eine Entscheidung (z. B. primäre Quelle,
Mittelwert, zuletzt-aktualisiert) fällt erst beim Aufbau des Curated Layer
in Phase 3, nicht vorher.

### Phase 3 — Curated Layer lokal
*Ziel: Aus Rohdaten wird ein Modell, mit dem man analysieren kann.*

- Transformationsschicht `transform/`: Raw → Indikatoren → Signale
- Star Schema in lokaler Postgres oder DuckDB aufbauen
- Backtest schreibt seine Läufe und Trades als Fakten in die DB statt in die Konsole
- Historisierung: Läufe werden nie überschrieben, jeder bekommt eine ID und
  seine Parameter mitgespeichert
- **Portfolio-Backtesting ("Modell B")** — bisher simuliert `backtest.py`
  jedes Symbol mit einem eigenen, unabhängigen ACCOUNT_SIZE-Sleeve
  ("Modell A", seit dem Account-Size-Fix korrekt ausgewiesen). Modell B wäre
  ein einzelnes gemeinsames Konto über alle Symbole, mit `MAX_OPEN_POSITIONS`
  durchgesetzt und Sizing gegen tatsächlich freies Kapital — das simuliert,
  was der Live-Bot wirklich täte. Kein Bugfix, sondern eigene Arbeit: sobald
  mehr Symbole gleichzeitig ein Signal liefern als Slots frei sind, braucht
  es eine explizite, getestete Auswahlregel (Signalstärke? Reihenfolge im
  Array? Zufall?) — die verändert nachweislich, welche Trades überhaupt
  stattfinden, nicht nur wie sie verbucht werden.

### Phase 4 — Orchestrierung
*Ziel: Es läuft ohne dich, und ein Fehler ist sichtbar.*

- Pipeline als DAG: Ingest → Quality → Transform → Load, mit Abbruch bei Fehler
- Lokal mit Prefect oder schlicht Makefile + cron
- Runs werden protokolliert (Startzeit, Dauer, verarbeitete Zeilen, Status)
- Retry-Logik für API-Timeouts

### Phase 5 — Azure-Migration
*Ziel: Cloud-Stack, den man im Lebenslauf nennen kann.*

- ADLS Gen2 als Raw Layer (dieselbe Partitionsstruktur wie lokal)
- Azure SQL als Curated Layer
- Data Factory für die Orchestrierung
- Key Vault für Secrets statt `.env`
- Alles im Free Tier / Student-Guthaben planen und Kosten dokumentieren

### Phase 6 — Power BI
*Ziel: Das sichtbare Ergebnis.*

- Semantic Model auf Azure SQL, Beziehungen und Hierarchien sauber setzen
- DAX-Measures für die Kennzahlen
- Die vier Report-Seiten aus Abschnitt 2 bauen
- Inkrementelles Refresh konfigurieren (zeigt, dass du an Datenvolumen denkst)

### Phase 7 — Portfolio-Politur
*Ziel: Dass jemand in fünf Minuten versteht, was du gebaut hast.*

- README mit Architekturdiagramm, Screenshots, ehrlichem Findings-Abschnitt
- Kurze Case Study: Problem, Ansatz, Entscheidungen, was du gelernt hast
- Der Abschnitt über den 452-vs-138-Bug gehört ausdrücklich rein — eine
  dokumentierte Fehlersuche wirkt stärker als ein Projekt, das angeblich nie
  Probleme hatte

---

## 5. Reihenfolge-Logik

Die Phasen bauen bewusst aufeinander auf: Ohne Quality Checks ist der Curated
Layer nur schnellerer Müll. Ohne Curated Layer gibt es für Power BI nichts
Sinnvolles zu modellieren. Und die Azure-Migration kommt erst, wenn lokal alles
funktioniert — sonst debuggst du Cloud-Konfiguration und Fachlogik gleichzeitig.

Phasen 1–3 sind der inhaltliche Kern. Phasen 5–6 sind das, was im Lebenslauf
steht. Phase 7 entscheidet, ob es jemand versteht.
