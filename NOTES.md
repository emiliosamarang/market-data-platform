# Trading Bot — Bestandsaufnahme

Stand: 2026-08-04

## Zweck

Automatisierter Krypto-Spot-Trading-Bot für Binance. Scannt stündlich fünf Symbole, generiert Signale auf Basis technischer Indikatoren und Sentiment-Daten, öffnet und verwaltet Trades vollständig automatisch inklusive OCO-Orders (Take Profit / Stop Loss).

## Dateien

| Datei | Funktion |
|---|---|
| `bot.py` | Hauptlogik: Indikatoren, Strategie, Scanner, APScheduler-Loop. Lädt vor jedem Scan pro Symbol inkrementell über `BinanceSource` nach und liest danach über `RawStore.read()` — kein direkter Kline-Abruf mehr. Schlägt das Nachladen fehl, wird das Symbol für den Zyklus übersprungen statt mit veralteten Daten gehandelt |
| `config.py` | Konfiguration; alle Secrets kommen aus Umgebungsvariablen |
| `exchange.py` | Binance-Client-Wrapper für Order-Ausführung (Orders platzieren, Lot/Tick-Rounding) |
| `trader.py` | Trade-Lifecycle: öffnen, OCO platzieren, Notfall-Close, sync |
| `database.py` | SQLite-Layer (Tabellen `signals` + `trades`) |
| `notify.py` | Telegram-Benachrichtigungen |
| `sentiment.py` | Fear & Greed Index + News-Sentiment via Claude Haiku (RSS → Anthropic API) |
| `backtest.py` | Historisches Backtesting mit Equity-Kurve und Drawdown-Reporting; liest über `RawStore` aus dem Raw Layer, `--refresh` lädt fehlende Daten via Ingestion nach |
| `ingestion/` | Ingestion-Schicht: `MarketDataSource`-Abstraktion, `BinanceSource`, `RawStore` (Parquet, idempotent) |

## Strategie

- **Universum:** BTCUSDT, ETHUSDT, SOLUSDT, XRPUSDT, ADAUSDT
- **Timeframes:** 4h (Trendfilter) + 1h (Einstieg)
- **Indikatoren:** EMA 20/50, RSI 14, MACD (12/26/9), ATR 14, Volume-MA 20
- **Trendfilter (4h):** EMA20 > EMA50, Preis über EMA20, MACD > Signal → BULLISH (analog BEARISH)
- **Einstiegssignal (1h):** Trend bestätigt, Preis nahe EMA20 (±3 %), RSI im erlaubten Band, Volumen über MA
- **Trade-Plan:** SL = 1.5 × ATR, TP = 3.0 × ATR → R/R ≥ 2.0 erforderlich
- **Positionsgrösse:** 1 % Risiko pro Trade auf Accountgrösse (default 1 000 USD)
- **Sentiment-Filter:** Fear & Greed Index (BUY blockiert bei < 25, SELL bei > 75) + Claude-Newsanalyse (blockiert bei Score < −0.5 bzw. > 0.5)
- **Risiko-Controls:** Max. 3 gleichzeitige Positionen; Tages-Verlust-Limit −3 % (Circuit Breaker)

## Secrets / Umgebungsvariablen

Alle Credentials kommen aus einer lokalen `.env`-Datei (via `python-dotenv`, geladen in `config.py`) — keine hardcodierten Werte im Code. Vorlage: `.env.example` nach `.env` kopieren und ausfüllen; `.env` ist gitignored.

| Variable | Verwendung |
|---|---|
| `BINANCE_API_KEY` | Binance REST API |
| `BINANCE_API_SECRET` | Binance REST API |
| `TELEGRAM_TOKEN` | Telegram Bot |
| `TELEGRAM_CHAT_ID` | Telegram Ziel-Chat |
| `ANTHROPIC_API_KEY` | Claude Haiku (Sentiment) |

## Abhängigkeiten (venv)

Siehe `requirements.txt`. Installation: `pip install -r requirements.txt`.

- `python-binance` — Binance-Client
- `pandas`, `numpy` — Datenverarbeitung
- `pyarrow` — Parquet-I/O für den Raw Layer (`ingestion/`)
- `anthropic` — Claude API
- `apscheduler` — Cron-Scheduler
- `requests` — HTTP (Telegram, RSS, Fear & Greed)
- `python-dotenv` — lädt `.env` in `config.py`

## Datenbank

SQLite-Datei `trading_bot.db` (zur Laufzeit erstellt, nicht im Repo).

- Tabelle `signals`: alle generierten Signale mit Scores und Indikatorwerten
- Tabelle `trades`: vollständiger Trade-Lifecycle (open/closed, PnL, Order-IDs)

## Bot starten

```bash
source venv/bin/activate
python bot.py
```

## Backtest

```bash
source venv/bin/activate
python backtest.py --days 365 --symbols BTCUSDT ETHUSDT
python backtest.py --refresh   # fehlende Raw-Daten vorher via Ingestion nachladen
```

Liest aus `data/raw/` (via `RawStore.read()`), nicht mehr live von Binance. Fehlt Datenmaterial für den angeforderten Zeitraum, bricht der Lauf mit klarer Fehlermeldung ab, außer `--refresh` ist gesetzt.

## Tests

Die Test-Suite deckt alle Indikator-Funktionen und die Strategie-Logik in `bot.py` ab. Keine echten API-Calls — alles läuft gegen synthetische DataFrames.

**Dev-Abhängigkeiten installieren:**

```bash
source venv/bin/activate
pip install -r requirements-dev.txt
```

**Tests ausführen:**

```bash
python -m pytest tests/ -v
```

**Mit Coverage-Report:**

```bash
python -m pytest tests/ --cov=bot --cov-report=term-missing
```

**Was getestet wird (`tests/test_bot.py`, 59 Tests):**

| Klasse | Funktion | Abgedeckte Fälle |
|---|---|---|
| `TestCalculateRsi` | `calculate_rsi` | Nur-Gewinne → 100, Nur-Verluste → 0, alternierend ~50, zu wenig Daten → NaN, Länge, Wertebereich |
| `TestCalculateMacd` | `calculate_macd` | Struktur (3 Series), hist = macd − signal, steigende/fallende Preise, flacher Markt |
| `TestCalculateAtr` | `calculate_atr` | Bekannte Werte, period=1 = True Range, Länge, zu wenig Daten → NaN |
| `TestAddIndicators` | `add_indicators` | Alle Spalten vorhanden, Original nicht mutiert, EMA NaN-frei, EMA20 > EMA50 im Auftrend |
| `TestGetTrend4h` | `get_trend_4h` | BULLISH/BEARISH/NEUTRAL, NaN → NEUTRAL, gemischte Signale → NEUTRAL |
| `TestGenerateEntrySignal` | `generate_entry_signal` | BUY/SELL/HOLD, falsche Trendrichtung, zu wenig Daten (NaN), Volumen zu tief, RSI-Grenzen, Preis zu weit von EMA20 |
| `TestCreateTradePlan` | `create_trade_plan` | BUY/SELL-Level (SL/TP/RR), HOLD → `{}`, NaN entry/ATR → `{}` |
| `TestIsTradeWorthIt` | `is_trade_worth_it` | R/R über/unter/gleich Minimum, leerer Plan, custom `min_rr` |
| `TestCalculatePositionSize` | `calculate_position_size` | Bekannte Werte, zero risk → 0, proportional zu risk% und Accountgrösse |
| `TestCalculateScore` | `calculate_score` | Finiter Float, NaN → Sentinel −999, breiterer EMA-Spread = höherer Score, hohe Volatilität = tieferer Score |
Backtest-Ergebnisse variieren stark nach Zeitraum (90d: −6,8 %, 365d: +12,5 %). Profit Factor 1,04 bei 87 % Gebührenanteil am Bruttogewinn. Ohne fixierte Datenbasis und Walk-Forward-Validierung sind Strategieaussagen nicht belastbar → Raw Layer als Voraussetzung.

## Investigation: 452 vs. 138 Trades nach Umstellung auf Raw Layer

**Befund vorab: kein Datenfehler.** Die Ursache ist, dass `main()` in `backtest.py`
Symbole mit unvollständigem Cache über `except MissingDataError: ... continue`
komplett aus dem kombinierten Ergebnis herausfällt, ohne Trades zu verlieren
oder falsche Trades zu erzeugen.

**1. Kerzenanzahl Live-Pfad vs. Raw Layer (BTCUSDT, 365d bzw. 395d für 4h, Stand heute)**

| Interval | Live (`get_historical_klines`) | Raw Layer (`RawStore.read`) | Differenz |
|---|---|---|---|
| 1h | 8771 | 8760 | 11 |
| 4h | 2373 | 2370 | 3 |

Die Differenz kommt ausschließlich vom alten `fetch_history()`: es hat den
Start-Zeitpunkt mit `.strftime("%d %b %Y")` auf Mitternacht abgeschnitten
(`start=2025-08-05 00:00`), während der neue Pfad den exakten Zeitstempel
(`start=2025-08-05 11:00`) verwendet — daher ein paar zusätzliche Kerzen am
Anfang des Live-Fensters. Kein Effekt in der Größenordnung, die 452→138
erklären würde.

**2. Duplikate im Live-Pfad**

Keine gefunden: 8771 gezogene Kerzen, 8771 eindeutige `open_time`-Werte (0
Duplikate) bei 1h, ebenso 0 bei 4h. Die frühere Vermutung "Duplikate im alten
Pfad haben künstlich mehr Trades erzeugt" ist damit widerlegt.

**3. Abweichungen im 4h-Raster**

Keine. Alle Zeitstempel — live wie aus dem Raw Layer — liegen exakt auf dem
4h-Raster (Vielfache von 14 400 000 ms seit Epoch). Beim direkten Join auf
den Timestamp-Index stimmen 8760 von 8760 überlappenden 1h-Kerzen in
Open/High/Low/Close/Volume exakt überein; die einzige Abweichung ist die
aktuell noch offene, sich laufend ändernde letzte Kerze (erwartbar, da beide
Fetches zu leicht unterschiedlichen Zeitpunkten liefen).

**4. Tatsächliche Ursache — im Log nachvollzogen**

`trading_bot.log`, Zeilen ~7539–7548 (heutiger Lauf, `python backtest.py`
ohne `--refresh`, Symbole BTCUSDT/ETHUSDT/SOLUSDT):

```
ERROR  Raw layer is missing 4 candle(s) for BTCUSDT 4h between ... :
       2026-08-04T20:00, 2026-08-05T00:00, 2026-08-05T04:00, 2026-08-05T08:00 ...
ERROR  Raw layer is missing 4 candle(s) for ETHUSDT 4h between ... (gleiche 4 Kerzen)
ERROR  Raw layer is missing 4 candle(s) for SOLUSDT 4h between ... (gleiche 4 Kerzen)
...
WARNING  No closed trades.   (ALL SYMBOLS COMBINED)
```

Alle drei Symbole hatten in ihrem 4h-Raw-Cache exakt die letzten vier
Kerzen (die letzten ~16h bis "jetzt") noch nicht — der Cache war seit dem
letzten Ingestion-Lauf schlicht nicht mehr taufrisch. `load_history()` wirft
dafür korrekt `MissingDataError`; `main()` fängt das pro Symbol ab, loggt
einen `ERROR` und macht mit dem nächsten Symbol weiter (`continue`) — das
betroffene Symbol trägt dann mit 0 Trades zum kombinierten Ergebnis bei,
ohne dass das im Reporting selbst auffällt (nur ein `ERROR`-Log, kein
Abbruch, kein Hinweis im Report "N von M Symbolen übersprungen").

Nach `python -m ingestion.load --interval 1h` / `--interval 4h` (Cache
aufgefrischt) lieferte derselbe Backtest für **BTCUSDT + ETHUSDT + SOLUSDT**
zusammen **449 Trades** (164 + 138 + 147) — praktisch identisch mit den
berichteten 452 vor der Umstellung. Vorher (ungefrischter Cache, gleiche drei
Symbole): **0 Trades** (alle drei übersprungen).

**Fazit:** Die 452→138-Differenz ist kein Bug in `BinanceSource` oder
`RawStore` (Kerzenzahl, Werte und Raster stimmen bis auf Rundungsdifferenzen
im alten Startdatum exakt überein), sondern eine Folge davon, dass
`backtest.py` ohne `--refresh` einen zwangsläufig leicht veralteten Cache
verwendet: Sobald der 4h-Cache nicht bis zur letzten geschlossenen Kerze vor
"jetzt" reicht — was ohne Refresh direkt vor jedem Lauf der Normalfall ist,
da 4h-Kerzen nur alle vier Stunden schließen — fällt das Symbol komplett aus
dem kombinierten Ergebnis, ohne dass das im Report sichtbar wird. Bei 138
Trades waren vermutlich mehrere der fünf Symbole aus genau diesem Grund
nicht im Ergebnis enthalten.

Noch nicht geändert (Auftrag: erst untersuchen). Mögliche Folge-Fixes für
später: `backtest.py` standardmäßig mit `refresh=True` laufen lassen (ist
idempotent, kostet nur zusätzliche API-Calls), oder `main()` am Ende explizit
melden "N von M Symbolen übersprungen (fehlende Rohdaten)" statt nur
Einzel-`ERROR`s pro Symbol zu loggen.

**Update:** Beide Folge-Fixes sind inzwischen umgesetzt — `backtest.py`
markiert einen unvollständigen kombinierten Report jetzt explizit
(`— INCOMPLETE (N/M symbols)`) und beendet sich mit Exit-Code ≠ 0, sobald
Symbole übersprungen wurden. Zusätzlich lädt `bot.py`s Live-Scan vor jedem
Zyklus pro Symbol inkrementell nach (statt zu lesen und nur bei Bedarf
nachzuladen wie `backtest.py`) — die zugrunde liegende Ursache dieses
Investigations-Kapitels (veralteter Cache ohne Refresh) kann im Live-Pfad
damit strukturell nicht mehr auftreten.

## Investigation: Backtest-Rendite-Sprung nach Raw-Layer-Refresh (12,5 % → 86,1 %)

Nach dem Backfill (Raw Layer wieder taufrisch, Data-Quality-Layer grün) lief
`backtest.py --days 365` für alle 5 Symbole und meldete **+86,1 %** Rendite —
gegenüber den in der README dokumentierten **+12,5 %** (365 Tage, 3 Symbole,
Stand vorheriger Lauf) ein Sprung um Faktor 7. Ein Ergebnis, das sich nach
einem reinen Infrastruktur-Refactor (Datenzugriff auf Raw Layer umgestellt,
Strategielogik unangetastet) versiebenfacht, ist erstmal Bug-Verdacht, kein
Erfolg — Untersuchung entlang vier Punkten: Gebühren, Look-ahead-Bias,
Positionsgrößen/Kapitalgrenze, Symbol-Konzentration.

**Korrektur an der Ausgangsdiagnose, bevor der Sprung überhaupt bewertet
werden kann:** Der alte 12,5-%-Wert war selbst falsch. `log_report()` teilt
den Netto-Gewinn immer durch die feste Konstante `ACCOUNT_SIZE` (1000), auch
im kombinierten Report — unabhängig davon, wie viele Symbole tatsächlich mit
eigenem Kapital-Sleeve gehandelt haben. Beim alten 3-Symbol-Lauf waren
$125 Netto ($972 Brutto − $847 Fees) im Einsatz, geteilt durch $1000 statt
durch $3000 (drei unabhängige Sleeves à $1000, siehe `calculate_position_size`
in `bot.py` — jedes Symbol sized gegen die volle `ACCOUNT_SIZE`, es gibt kein
gemeinsames Kapital-Limit). Richtig gerechnet: **4,2 %**, nicht 12,5 %. Der
tatsächlich zu erklärende Sprung ist also **4,2 % → 17,2 %** (17,2 % ist der
korrigierte Wert für den neuen 5-Symbol-Lauf, s.u.) — immer noch Faktor ~4,
aber ein anderes Problem als ursprünglich gedacht.

**1. Gebühren — nicht die Ursache.** `run_backtest()`, `log_report()` und die
Fee-Formel (`fee = (entry + exit) * size * 0.1%`, pro Trade einmal beim Exit
verbucht) sind im Raw-Layer-Refactor (Commit `e4fce30`) byte-identisch
geblieben — nur `fetch_history()` → `load_history()` (der Datenzugriff) hat
sich geändert. Aufschlüsselung des neuen 5-Symbol-Laufs:

| Symbol | Trades | Brutto | Fees | Netto | Fee/Brutto |
|---|---|---|---|---|---|
| BTCUSDT | 159 | $389.99 | $372.06 | $17.93 | 95.4% |
| ETHUSDT | 146 | $219.94 | $272.20 | −$52.26 | 123.8% |
| SOLUSDT | 141 | $452.18 | $215.25 | $236.92 | 47.6% |
| XRPUSDT | 158 | $581.36 | $265.23 | $316.13 | 45.6% |
| ADAUSDT | 145 | $530.02 | $187.30 | $342.73 | 35.3% |
| **Total** | 749 | **$2173.50** | **$1312.05** | **$861.45** | **60.4%** |

Fees fressen weiterhin 60,4 % des Bruttogewinns — deutlich weniger als die
87 % aus dem alten 3-Symbol-Lauf, aber **nicht weil die Gebühren gesunken
sind** (die Formel und der Satz von 0,1 % pro Seite sind unverändert),
sondern weil der Bruttogewinn diesmal größer war. Die verbesserte Fee-Quote
ist eine Folge des Ergebnisses, keine unabhängige Bestätigung dafür.

**2. Look-ahead-Bias — nicht gefunden.** In `run_backtest()` wird das Signal
strikt auf Daten bis einschließlich der aktuellen, bereits geschlossenen
Kerze berechnet (`df_1h.iloc[:i+1]`, `df_4h[df_4h.index <= current_time]`);
der Einstieg erfolgt explizit am Open der **nächsten** Kerze
(`df_1h.iloc[i+1]["Open"]`, Kommentar im Code: "avoids lookahead bias on
entry price"). Diese Zeile existierte bereits vor dem Refactor unverändert.
`RawStore.load_range()`/`read()` liefern garantiert lückenlos sortierte,
deduplizierte Daten (durch den neuen Data-Quality-Layer laufend geprüft),
`iloc[i+1]` ist also immer exakt ein Intervall später — kein Indexversatz
durch `load_range(..., dedupe=...)`.

**3. Positionsgrößen/Kapitalgrenze — der eigentliche Fund.**
`calculate_position_size()` sized jeden Trade gegen die feste Konstante
`ACCOUNT_SIZE` (kein Compounding — unproblematisch). Aber `MAX_OPEN_POSITIONS`
(=3, in `trader.py` für den Live-Bot durchgesetzt) wird in `backtest.py`
**an keiner Stelle geprüft** — jedes Symbol simuliert vollständig unabhängig,
ohne gemeinsames Kapital- oder Positionslimit ("Modell A": separate Sleeves).
Der kombinierte Report hat trotzdem durch eine einzige feste `ACCOUNT_SIZE`
geteilt — mit jedem zusätzlichen Symbol im Lauf bläht sich die gemeldete
"Return on account" damit rein rechnerisch auf.

**Gegenprobe:** Backtest erneut nur mit den drei alten Symbolen (BTCUSDT,
ETHUSDT, SOLUSDT), gleiches 365-Tage-Fenster (heute, drei Wochen mehr Daten
als beim alten Lauf): **$202.60 Netto**, exakt die Summe der drei
Einzelwerte aus obiger Tabelle. Auf $3000 (drei Sleeves) gerechnet: **6,8 %**
— genau der erwartete Wert, plausibel gegenüber 4,2 % beim alten,
kürzeren Fenster. Die Gegenprobe schließt die Sache: der Rest des Deltas
(6,8 % → 17,2 %) erklärt sich vollständig durch XRPUSDT und ADAUSDT, die in
diesem Zeitraum zufällig die profitabelsten der fünf Symbole waren.

**Fix ("Modell A"):** `main()` in `backtest.py` teilt den kombinierten Report
jetzt durch `successful * ACCOUNT_SIZE` (`successful` = Anzahl tatsächlich
gelaufener, nicht übersprungener Symbole) statt durch eine feste
`ACCOUNT_SIZE`. Ändert kein einziges Trade-Ergebnis, nur den Nenner. Neuer
5-Symbol-Lauf nach dem Fix: **+17,2 %** (vorher fälschlich +86,1 %),
Max-Drawdown korrekt von 17,3 % auf 3,8 % korrigiert (dieselbe
Equity-Kurve-Berechnung startet ebenfalls bei `account`). Regressionstest in
`tests/test_backtest.py::TestCombinedAccountSize` prüft, dass der Nenner mit
der Symbolanzahl skaliert und übersprungene Symbole ausschließt — genau der
Test, der diesen Bug von Anfang an verhindert hätte.

Ein "echtes" Portfolio-Backtesting mit gemeinsamem Konto,
`MAX_OPEN_POSITIONS`-Cap und einer definierten Auswahlregel bei
Signal-Konflikten ("Modell B") ist kein Bugfix, sondern eigene Arbeit —
als Backlog-Item in `ROADMAP.md` (Phase 3) aufgenommen statt hier
reingepatcht.

**4. Symbol-Konzentration — die eigentlich belastbare Aussage aus diesem
Lauf.** Aufschlüsselung nach Netto-Beitrag:

| Symbol | Netto-PnL | Anteil |
|---|---|---|
| ADAUSDT | $342.73 | 39.8% |
| XRPUSDT | $316.13 | 36.7% |
| SOLUSDT | $236.92 | 27.5% |
| BTCUSDT | $17.93 | 2.1% |
| ETHUSDT | −$52.26 | −6.1% |

Kein einzelnes Symbol dominiert (kein Wert über 70 %), aber das gesamte
Ergebnis kommt aus drei Alts (ADA/XRP/SOL) in einem Zeitraum, in dem Alts
liefen. Auf den beiden liquidesten, "seriösesten" Paaren — BTC und ETH — ist
die Strategie nach Gebühren eine Nullnummer bis Verlustbringerin: $17.93 auf
159 BTC-Trades (95,4 % Fee-Quote), ETH sogar netto negativ (123,8 %
Fee-Quote — die Gebühren übersteigen den Bruttogewinn). Auf den liquidesten
Paaren ist die Strategie eine Gebührenmaschine ohne erkennbare Kante.

**Fazit:** Kein Bug im Raw-Layer-Refactor selbst (Simulation, Fee-Logik und
Signal-Timing sind unverändert und wurden explizit gegengeprüft). Der
Rendite-Sprung erklärt sich vollständig durch (a) einen vorbestehenden
Kapital-Buchhaltungsfehler im kombinierten Report, der mit der Symbolanzahl
skaliert — jetzt gefixt — und (b) zwei zusätzliche, in diesem Zeitraum
zufällig profitable Alt-Symbole. Der Satz "Strategie ist noch nicht
belastbar validiert" (README) bleibt damit unverändert stehen — bestätigt
durch einen zweiten, hier selbst gefundenen Reporting-Bug, nicht widerlegt
durch eine höhere Zahl.

## Phase 2: Kraken als zweite Datenquelle — 720-Kerzen-Limit und Cross-Source-Check

Vor der Implementierung stand die Annahme im Raum, `RawStore` müsse für eine
zweite Quelle erst um eine `source`-Partitionsebene erweitert werden
(inklusive Migration der bestehenden 7.340 Binance-Dateien). Das war falsch
und beruhte erkennbar auf einer Vermutung ohne Live-Zugriff auf den Code
("Heute liegt der Pfad vermutlich als symbol/interval/datum"): Der Pfad war
schon seit Phase 1 `{asset_class}/{source}/{symbol}/{interval}/{date}` —
`source` lag bereits ganz vorne, genau wo eine zweite Quelle es braucht.
Keine Migration nötig, Kraken-Daten liegen einfach zusätzlich unter
`data/raw/crypto/kraken/...` neben `.../binance/...`.

**Kraken-Historientiefe — live gegen die echte API verifiziert, nicht aus
der Doku übernommen.** Kraken dokumentiert für `/public/OHLC`: "Returns up
to 720 of the most recent entries (older data cannot be retrieved,
regardless of the value of `since`)." Das wurde vor der Implementierung
gegengecheckt statt einfach geglaubt: ein Request mit `since` = 1000 Stunden
in der Vergangenheit lieferte trotzdem nur das Fenster der letzten 720
Kerzen endend bei "jetzt" — `since` filtert innerhalb dieses festen
Fensters, ist aber kein Werkzeug, um weiter in die Vergangenheit zu
paginieren. Für 1h-Kerzen sind das ~30 Tage, für 4h ~120 Tage; ein
Zwei-Jahres-Backfill wie bei Binance ist über diesen Endpoint schlicht nicht
erreichbar. Praktische Konsequenz: Kraken taugt für Cross-Validation des
aktuellen Fensters, nicht für einen vollständigen Zweitquellen-Backfill.

**Das Limit ist strukturell, kein API-Detail — zwei Konsequenzen, die daraus
zwingend folgen, nicht aus Geschmack:**

1. **Rollenverteilung ist damit faktisch entschieden.** Binance ist die
   Quelle mit vollständiger Historie, Kraken dient ausschließlich der
   Validierung des jüngsten, rollierenden Fensters. Kraken ist keine
   gleichwertige zweite Quelle für den Curated Layer — es liefert keine
   eigenen Zeilen für Zeiträume außerhalb seines Fensters und tritt nicht
   in Konkurrenz zu Binance als Quelle der Wahrheit. Das beantwortet einen
   Teil der ursprünglich offenen Konfliktfrage (siehe unten): der Curated
   Layer wird aus Binance gespeist, Kraken bleibt Prüfinstanz. Was bei einer
   *tatsächlichen* Abweichung innerhalb des überlappenden Fensters passiert
   (welcher Wert landet im Fakt, falls sie sich widersprechen), ist damit
   noch nicht entschieden — nur, dass Kraken dafür nie die primäre Quelle
   wird.
2. **Krakens Fenster ist unwiederbringlich.** Sobald eine Kerze aus dem
   720er-Fenster herausfällt, ist sie über diesen Endpoint für immer weg —
   es gibt keinen Nachlade-Mechanismus. Läuft der Ingestion-Job vier Wochen
   nicht, ist das komplette 1h-Fenster durch, und die Lücke lässt sich nie
   mehr schließen (Binance bleibt davon unberührt, hat ja die volle
   Historie — betrifft nur Krakens Beitrag zur Cross-Validation). Das macht
   Phase 4 (Orchestrierung: "läuft ohne dich, Fehler sind sichtbar") von
   "wäre schön" zu "muss stehen, bevor Kraken-Daten laufend verloren gehen"
   — festgehalten in `ROADMAP.md`.

**Zweite Kraken-Eigenheit:** keine einheitliche Symbol-Notation. BTC heißt
intern "XBT", und legacy gelistete Paare (BTC/ETH/XRP) liefern vom
OHLC-Endpoint einen anderen, X/Z-präfixierten Response-Key zurück als den
abgefragten (Query `XBTUSD` → Response-Key `XXBTZUSD`), während neuere
Listings (SOL/ADA) auf beiden Seiten denselben String verwenden — live
gegen `/public/AssetPairs` verifiziert. Gelöst über eine explizite
Mapping-Tabelle für die Query-Seite (`_SYMBOL_MAP` in `kraken_source.py`)
plus dynamisches Lesen des tatsächlich zurückgegebenen Keys, statt
anzunehmen, er entspräche der Query.

**Cross-Source-Check gegen echte Daten (alle 5 Symbole, Binance vs.
Kraken):**

- 1h: keine Coverage-Lücken im überlappenden Fenster (26.07.–25.08.),
  Preise stimmen bei allen 5 Symbolen innerhalb der 0,5-%-Schwelle überein.
- 4h: ebenfalls keine Coverage-Lücken; bei BTCUSDT, SOLUSDT und ADAUSDT
  zunächst eine Preis-Abweichung (0,8–1,3 %) — jeweils exakt die letzte,
  zum Zeitpunkt des ersten Checks noch nicht geschlossene 4h-Kerze
  (2026-08-25T08:00). Zwei unabhängige, noch unfertige Orderbooks dürfen
  sich beim laufenden Preis leicht unterscheiden — das ist der erwartete
  Fall, kein Datenfehler.

**Aber:** ein Check, der bei jedem nächtlichen Lauf auf der jeweils
aktuellsten Kerze feuert, ist nutzlos — nach zwei Wochen liest ihn niemand
mehr, und die eine echte Warnung geht darin unter. `check_cross_source`
schließt jetzt jede noch nicht abgeschlossene Kerze (`timestamp + interval`
liegt noch in der Zukunft) explizit vom Vergleich aus, analog zur
Zwei-Intervall-Toleranz in `check_freshness`. Gegenprobe: BTCUSDT, SOLUSDT
und ADAUSDT neu geladen, nachdem die 08:00-Kerze real geschlossen hatte
(12:15 UTC, Kerzenende 12:00) — Abweichung war weg. Bestätigt beides
zugleich: die Erklärung war richtig (reines Formations-Rauschen, kein
Datenproblem), und der Ausschluss-Fix greift korrekt.

Der Vergleich läuft bewusst nur im Fenster, in dem beide Quellen tatsächlich
Daten haben (Schnittmenge der jeweiligen Zeitspannen, nicht der angefragte
Bereich) — sonst würde Krakens Tiefenlimit die gesamte ältere
Binance-Historie fälschlich als "nur in einer Quelle vorhanden" markieren,
obwohl das schlicht außerhalb von Krakens Reichweite liegt und kein
Datenproblem ist.

**Konfliktauflösung — Rollenfrage entschieden, Wertfrage noch offen:** Die
720-Kerzen-Grenze entscheidet bereits, wer im Curated Layer die Quelle der
Wahrheit ist (Binance, volle Historie) und wer nur Prüfinstanz bleibt
(Kraken, rollierendes Fenster) — siehe oben. Was aber bei einer *echten*
Abweichung innerhalb des überlappenden Fensters passiert (Binance falsch,
Kraken richtig, oder umgekehrt), ist damit nicht automatisch entschieden;
der Raw Layer speichert weiterhin beide Quellen unverändert nebeneinander,
ohne automatische Korrektur. Diese Wertfrage fällt erst beim Aufbau des
Curated Layer in Phase 3 (siehe `ROADMAP.md`), nicht implizit im Code.

## Buy-and-Hold-Benchmark, Risikokennzahlen und Marktphasen-Attribution

`backtest.py` bekam eine Buy-and-Hold-Vergleichslinie pro Symbol (Kauf zum
Open der ersten Kerze, Verkauf zum Close der letzten, volle `ACCOUNT_SIZE`
als Positionsgröße, dieselbe Round-Trip-Gebühr wie die Strategie) sowie
zwei Kennzahlen, die zur risikofokussierten Positionierung passen: Return
pro Einheit Max-Drawdown, und Rendite aufgeschlüsselt nach Marktphase
(bullish/bearish/neutral, klassifiziert über `bot.py`s eigenen
`get_trend_4h`-Filter — keine zweite, separat definierte Trend-Logik).

**Beide vollständigen Ergebnistabellen, nichts weggelassen — gerade die
Zeilen, in denen die Strategie verliert, tragen die Glaubwürdigkeit der
Zeilen, in denen sie gewinnt:**

### 365 Tage (Stand 2026-08-25)

| Symbol | Strategie | Buy & Hold | Differenz | B&H Max DD |
|---|---|---|---|---|
| BTCUSDT | +3.0% | −29.7% | +32.7pp | 53.7% |
| ETHUSDT | −4.1% | −47.0% | +42.9pp | 68.0% |
| SOLUSDT | +23.7% | −50.7% | +74.4pp | 75.8% |
| XRPUSDT | +31.6% | −50.6% | +82.2pp | 68.8% |
| ADAUSDT | +33.1% | −75.3% | +108.4pp | 85.4% |
| **Combined** | **+17.5%** | **−50.7%** | **+68.1pp** | 69.2% |

Dieses Fenster deckt einen scharfen Markteinbruch ab. Buy & Hold verliert
auf allen fünf Symbolen deutlich, die Strategie bleibt dank Trendfilter und
Stop-Loss überall positiv bis leicht negativ.

### 2 Jahre (voller verfügbarer Zeitraum)

| Symbol | Strategie | Buy & Hold | Differenz | B&H Max DD |
|---|---|---|---|---|
| BTCUSDT | −5.7% | +23.1% | **−28.8pp** | 53.7% |
| ETHUSDT | −13.8% | −10.6% | **−3.2pp** | 69.1% |
| SOLUSDT | +31.5% | −38.5% | +70.0pp | 78.7% |
| XRPUSDT | +72.9% | **+143.2%** | **−70.3pp** | 72.9% |
| ADAUSDT | +38.3% | −44.4% | +82.7pp | 89.3% |
| **Combined** | **+24.6%** | +14.5% | +10.1pp | 70.0% |

Hier ist das Bild gemischt — kein sauberes "Strategie schlägt Markt". Auf
BTC und XRP verliert die Strategie gegen simples Halten deutlich (bei XRP,
weil eine durchgehaltene Rally von +143 % besser ist als wiederholtes
Ein-/Aussteigen mit Gebühren). Auf SOL/ADA gewinnt sie klar, weil sie tiefe
Abstürze (−38 % bis −44 %) größtenteils vermeidet.

**Das eine robuste, konsistente Ergebnis über beide Zeiträume und alle
Symbole:** Max Drawdown der Strategie liegt durchgehend bei 3,6–25,1 %,
Buy & Hold bei 53,7–89,3 %. Kein Ausreißer, sondern strukturell — der
Stop-Loss tut genau das, wofür er da ist.

### Return/Max-Drawdown-Ratio — wo sich das Bild dreht

Rohe Rendite allein versteckt, dass ein Teil von Buy & Holds Vorsprung nur
mit deutlich mehr Risiko erkauft ist. `_return_to_drawdown_ratio()`
(Rendite in % geteilt durch Max-Drawdown in %; bei Drawdown = 0 und
Rendite > 0 → `inf`, sonst 0) macht das sichtbar:

| Symbol | 365T Strat | 365T B&H | 2J Strat | 2J B&H |
|---|---|---|---|---|
| BTCUSDT | 0.22 | −0.55 | −0.23 | 0.43 |
| ETHUSDT | −0.26 | −0.69 | −0.58 | −0.15 |
| SOLUSDT | 1.87 | −0.67 | 2.47 | −0.49 |
| XRPUSDT | 4.06 | −0.73 | **11.32** | **1.96** |
| ADAUSDT | 2.99 | −0.88 | 2.31 | −0.50 |
| **Combined** | **4.82** | −0.73 | **2.79** | 0.21 |

Genau der vorhergesagte Dreh bei XRP über 2 Jahre: Buy & Hold gewinnt bei
der rohen Rendite (+143,2 % vs. +72,9 %), aber die Strategie liefert pro
Einheit Risiko fast das Sechsfache (11,32 vs. 1,96) — B&H erkauft die
höhere Rendite mit 72,9 % Max-Drawdown, mehr als das Zehnfache dessen, was
die Strategie durchmacht (6,4 %). Combined bleibt die Strategie in beiden
Zeiträumen risikoadjustiert klar vorn, selbst dort, wo die rohe Rendite
knapper ausfällt (2 Jahre: +24,6 % vs. +14,5 %, aber 2.79 vs. 0.21 im
Verhältnis).

### Rendite pro Marktphase — die eigentliche Erkenntnis in Zahlen

**Methodik:** Jede 4h-Kerze wird über `bot.py`s eigenen `get_trend_4h`
(dieselbe Logik, die die Strategie selbst zum Ein-/Ausstieg nutzt) als
BULLISH/BEARISH/NEUTRAL klassifiziert. Für Buy & Hold wird der
Log-Return jeder 1h-Kerze der Phase zugeordnet, die zu diesem Zeitpunkt
galt, und pro Phase aufsummiert (Log-Returns sind additiv, also verlustfrei
zerlegbar) — für die Strategie reicht die Trade-`side` selbst als
Phasen-Label (BUY entsteht nur in einer BULLISH-, SELL nur in einer
BEARISH-Phase, per `generate_entry_signal`).

**Wichtige Einordnung, bevor die Zahlen wirken wie ein Fehler:** Die
Bullish-Phase-Werte für Buy & Hold sind über 2 Jahre teils vierstellig
(z. B. XRP: +4500,5 %). Das ist **kein literal erzielbarer Ertrag**,
sondern eine mathematische Kompositions-Zerlegung — viele einzelne,
nicht zusammenhängende Bullish-Abschnitte über 2 Jahre, deren Log-Returns
sich beim Zurückrechnen in Prozent multiplikativ statt additiv
kombinieren. Aussagekräftig ist nicht die absolute Zahl, sondern der
**Vergleich B&H vs. Strategie innerhalb derselben Phase**.

| Symbol | Bullish B&H (2J) | Bullish Strat (2J) | Bearish B&H (2J) | Bearish Strat (2J) |
|---|---|---|---|---|
| BTCUSDT | +888.8% | −0.4% | −86.8% | −5.3% |
| ETHUSDT | +2295.1% | +1.8% | −96.6% | −15.6% |
| SOLUSDT | +3068.0% | +14.2% | −98.2% | +17.3% |
| XRPUSDT | +4500.5% | +15.1% | −96.3% | +57.8% |
| ADAUSDT | +2885.2% | +2.2% | −98.5% | +36.2% |
| **Combined** | **+2727.5%** | **+6.6%** | **−95.3%** | **+18.1%** |

Das ist der eigentliche Befund, den die rohen Renditezahlen nur andeuten:
**In Bullenphasen erfasst die Strategie nur einen winzigen Bruchteil der
verfügbaren Bewegung** — sie steigt wiederholt ein und aus, zahlt jedes
Mal Gebühren, und verpasst den Großteil einer durchgehaltenen Rally. **In
Bärenphasen ist es umgekehrt:** Buy & Hold verliert nahezu die gesamte
Position (−86 % bis −98,5 % attributiert), während die Strategie durch
Stop-Loss (und bei ADA/SOL/XRP profitable SELL-Trades) flach bis deutlich
positiv bleibt. Der Netto-Vorteil der Strategie ist ein Risiko-Trade: viel
Bullenphasen-Upside gegen fast die gesamte Bärenphasen-Downside getauscht
— bei XRP über 2 Jahre war dieser Tausch in absoluten Zahlen ein
Verlustgeschäft (Bullish-Verzicht > Bearish-Ersparnis), risikoadjustiert
(Return/MaxDD 11,32 vs. 1,96) trotzdem klar vorteilhaft.

## Parameter-Sweep: waren die ursprünglichen Schwellwerte begründet?

`scripts/parameter_sweep.py` beantwortet die Frage, die seit dem
Backtest-Rendite-Sprung offenstand: wer hat eigentlich entschieden, dass
`ATR_SL_MULTIPLE=1.5` oder `EMA20_DISTANCE_THRESHOLD=0.03` die richtigen
Werte sind? Methodik, mit vier Vorgaben, damit daraus keine Zahlenschau
wird:

- **Zwei Parameter, univariat, nicht gekreuzt** — je 5 Stufen, den jeweils
  anderen auf Baseline gehalten. 20 Läufe insgesamt (10 pro Fenster), nicht
  hunderte.
- **Bewertet nach Return/MaxDD**, nicht nach roher Rendite — die Kennzahl,
  die über beide Backtest-Zeiträume konsistent war (siehe oben), nicht die,
  die am stärksten schwankte.
- **Zwei getrennte, nicht überlappende 365-Tage-Fenster**: Auswahl (letzte
  365 Tage) und Validierung (die 365 Tage davor). Eine Stufe, die nur im
  Auswahlfenster gut aussieht, ist Zufall.
- **Plateau statt Spitzenwert** — bewertet wird ein ganzer robuster
  Bereich, nicht die einzelne beste Zahl.

Jeder der 20 Läufe ist reproduzierbar in `fact_backtest_run` historisiert
(volle Parameter, Commit-Hash) — dieselbe Infrastruktur wie jeder andere
Backtest-Lauf, kein Parallelpfad.

### ATR_SL_MULTIPLE (Baseline: 1.5)

| Stufe | Auswahl: Trades | Auswahl: Return/MaxDD | Validierung: Trades | Validierung: Return/MaxDD |
|---|---|---|---|---|
| 1.00 | 923 | 1.80 | 962 | 0.33 |
| 1.25 | 861 | 2.76 | 889 | **0.84** |
| **1.50 (Baseline)** | 746 | **4.94** | 757 | 0.58 |
| 1.75 | 0 | — | 0 | — |
| 2.00 | 0 | — | 0 | — |

**Struktureller Befund, wichtiger als jede einzelne Zahl:** Ab 1.75 gibt es
in beiden Fenstern exakt null Trades — kein Rauschen, sondern Mechanik.
Stop-Loss und Take-Profit sind beide ATR-Vielfache
(`ATR_SL_MULTIPLE`/`ATR_TP_MULTIPLE=3.0`), also ist das Reward/Risk-
Verhältnis jedes Signals exakt `3.0 / ATR_SL_MULTIPLE`, unabhängig vom
Marktzustand. Sobald das unter `MIN_RR=2.0` fällt (ab `ATR_SL_MULTIPLE >
1.5`), lehnt `is_trade_worth_it` *jeden* Trade ab — nicht weniger Trades,
null. Die Baseline sitzt damit exakt an der Kante dieser Klippe, nicht in
der Mitte eines Bereichs. Das ist selbst ein Befund: `ATR_SL_MULTIPLE`
lässt sich mit fixem `ATR_TP_MULTIPLE`/`MIN_RR` nur nach unten sinnvoll
testen — eine Kopplung, die vor diesem Sweep nirgends sichtbar war.

Innerhalb des testbaren Bereichs (1.0–1.5): 1.0 ist in beiden Fenstern klar
am schlechtesten (1.80 / 0.33). 1.25 und 1.5 sind beide deutlich besser als
1.0, tauschen aber den Rang zwischen den Fenstern (Auswahl bevorzugt 1.5,
Validierung bevorzugt 1.25) — ein Zwei-Punkt-Plateau, kein einzelner
Ausreißer, aber auch kein sauberes Optimum. Die Baseline ist vertretbar,
aber nicht beweisbar optimal — und "optimal höher" lässt sich mit diesem
Sweep-Design gar nicht prüfen, ohne `ATR_TP_MULTIPLE`/`MIN_RR` mit
anzufassen.

### EMA20_DISTANCE_THRESHOLD (Baseline: 0.03)

| Stufe | Auswahl: Trades | Auswahl: Return/MaxDD | Validierung: Trades | Validierung: Return/MaxDD |
|---|---|---|---|---|
| 0.01 | 409 | 3.67 | 355 | **−0.55** |
| 0.02 | 677 | 2.58 | 650 | −0.08 |
| **0.03 (Baseline)** | 746 | **4.94** | 757 | 0.58 |
| 0.05 | 774 | 3.37 | 807 | **0.87** |
| 0.08 | 777 | 3.49 | 818 | 0.74 |

**Das ist das saubere Plateau, das der Sweep eigentlich finden sollte.**
0.01 und 0.02 sind im Auswahlfenster nicht auffällig schlecht (3.67 bzw.
2.58), kippen im Validierungsfenster aber ins Negative bzw. nahe null
(−0.55 / −0.08) — ein Wert, der nur im Fenster gut aussieht, in dem er
nie ausgewählt wurde, hätte hier genau das falsche Signal gegeben. Die
Stufen 0.03–0.08 dagegen sind in **beiden** Fenstern durchgehend positiv
und liegen nah beieinander (4.94/3.37/3.49 vs. 0.58/0.87/0.74) — ein
echter, robuster Bereich, keine Einzelspitze zwischen schlechten Nachbarn.
Die Baseline (0.03) liegt mittig in diesem Plateau, nicht an seinem Rand.

### Fazit

Realistischer Ausgang, wie erwartet: die ursprünglichen Werte erweisen
sich als brauchbar, nicht als verbesserbar. Für `EMA20_DISTANCE_THRESHOLD`
ist das eindeutig — die Baseline liegt in einem über beide Fenster
robusten Plateau, kein Zufallstreffer. Für `ATR_SL_MULTIPLE` ist die
Antwort nuancierter: die Baseline ist die vernünftigste unter den
*testbaren* Werten, sitzt aber strukturell an einer Kante, die durch die
Kopplung mit `ATR_TP_MULTIPLE`/`MIN_RR` entsteht — eine Frage, die dieser
Sweep aufgeworfen, aber nicht abschließend beantwortet hat.

Das beantwortet auch die eigentliche Frage: Die Strategie-Schwellwerte
waren nicht willkürlich unbeprüft — sie sind jetzt geprüft und liegen,
mit einer dokumentierten Einschränkung bei `ATR_SL_MULTIPLE`, in einem
robusten Bereich. Das steht jetzt so in der Doku, statt dass die Parameter
nie hinterfragt wurden.

## Strategy-Interface und Referenz-Strategien: wo steht die Strategie wirklich?

Ausgangsfrage: Buy & Hold ist der einzige Vergleichspunkt, den es bisher
gab, und strukturell mit der Strategie nicht vergleichbar — kein Trade,
kein Stop-Loss, keine Gebühr pro Round-Trip. Damit ließ sich nie sauber
beantworten, ob der Vorteil der Strategie (siehe Return/MaxDD oben) aus dem
*Signal* kommt oder einfach nur daraus, dass überhaupt ein ATR-basierter
Stop-Loss existiert.

**Dafür drei neue Bausteine:**

- `strategies/base.py` — ein `Strategy`-Interface (`decide()`/optional
  `prepare()`), nach demselben Muster wie `ingestion.base.MarketDataSource`.
  `backtest.run_backtest()` kennt jetzt nur noch das Interface, nicht mehr
  die konkrete Signal-Logik — Default-Verhalten unverändert (verifiziert
  über die volle bestehende Testsuite plus neue Wiring-Tests).
- `strategies/ema_rsi_macd.py` — `EmaRsiMacdStrategy`, ein reiner Wrapper
  um die unveränderte `bot.py`-Logik (`get_trend_4h`,
  `generate_entry_signal`, `create_trade_plan`, `is_trade_worth_it`) —
  keine Neuimplementierung, exakt dasselbe Verhalten wie vor dem Refactor.
- `strategies/random_strategy.py` — `RandomStrategy`: **derselbe**
  ATR-basierte Stop (`bot.create_trade_plan`), **dieselbe** Trade-Anzahl
  pro Symbol und Fenster wie die echte Strategie in diesem Lauf, aber
  zufälliger Einstiegszeitpunkt und zufällige Seite — bewusst **kein**
  Trend-Filter, **kein** RR-Gate. Die Nulllinie nach unten: schlägt die
  Strategie das nicht, ist da kein Signal, nur Rauschen mit Stop-Loss.
- `strategies/sma_crossover.py` — `SmaCrossoverStrategy`: die einfachste
  Regel, die *kein* Zufall ist — ein einzelner gleitender Durchschnitt
  (SMA, Periode 50), Einstieg beim Cross des Preises über/unter die
  Linie. **Derselbe** ATR-Stop wie die anderen beiden, aber eigene,
  natürliche Trade-Frequenz (kein Trade-Count-Matching) — bewusst
  ebenfalls **kein** Trend-Filter, **kein** RR-Gate. Der interessante
  Referenzpunkt: wenn eine simple Regel ähnlich gut abschneidet wie die
  dreiteilige Konstruktion aus Trend-Filter, RR-Gate und
  EMA/RSI/MACD-Konfluenz, ist die Zusatzkomplexität nicht gerechtfertigt.

**Methodik (`scripts/random_baseline.py`), dieselben zwei Fenster wie beim
Parameter-Sweep:**

- Pro Fenster läuft zuerst die echte Strategie einmal durch — ihre
  Trade-Anzahl pro Symbol wird zum Ziel für `RandomStrategy` in genau
  diesem Fenster.
- Danach 30 unabhängige Zufalls-Seeds pro Fenster (nicht ein einzelner
  Zufalls-Lauf) — aus demselben Grund, aus dem der Parameter-Sweep zwei
  Fenster brauchte: ein einzelner Zufalls-Draw ist selbst Rauschen.
  Bewertet wird die **Verteilung** (Median, Min–Max), nicht eine Zahl.
- Bewertet nach Return/MaxDD, dieselbe Kennzahl wie beim Parameter-Sweep.
- Jeder Lauf landet in `fact_backtest_run` (`strategy_name='ema_rsi_macd'`
  / `'sma_crossover'` / `'random'`) — für die 60 Zufallsläufe bewusst nur
  die Lauf-Zusammenfassung, keine `fact_backtest_trade`-Zeilen (das wären
  60 × mehrere hundert Trades, die niemand einzeln abfragt). Die beiden
  deterministischen Läufe (echte Strategie, SMA-Crossover) bekommen ihre
  Trades wie jeder normale Backtest-Lauf mitgespeichert.

### Ergebnis

| Fenster | Strategie Return/MaxDD | SMA-Crossover Return/MaxDD | Random Median | Random Min–Max (30 Seeds) |
|---|---|---|---|---|
| Auswahl (letzte 365 Tage) | **4.94** (746 Trades) | −0.95 (1086 Trades) | −0.90 | [−1.00, −0.05] |
| Validierung (365 Tage davor) | **0.58** (757 Trades) | −0.92 (1116 Trades) | −0.89 | [−1.00, −0.57] |

Trade-Ziele pro Symbol (echte Strategie, Basis für die Random-Trade-Anzahl)
— Auswahl: `{BTC: 158, ETH: 145, SOL: 141, XRP: 157, ADA: 145}`;
Validierung: `{BTC: 172, ETH: 132, SOL: 162, XRP: 153, ADA: 138}`.
`SmaCrossoverStrategy` läuft dagegen auf eigener, natürlicher Frequenz
(kein Matching) — entsprechend fast doppelt so viele Trades wie die echte
Strategie.

**Die echte Strategie schlägt in beiden Fenstern jeden einzelnen der 30
Zufallsläufe** — nicht nur den Median, auch das beste Zufallsergebnis
(Auswahl: 4.94 vs. bester Random-Wert −0.05; Validierung: 0.58 vs. bester
Random-Wert −0.57). Da Stop-Loss/Take-Profit-Mechanik zwischen echter und
Zufalls-Strategie identisch sind, lässt sich der Unterschied auf die
Entscheidungslogik zurückführen, die die Strategie zusätzlich zur
Stop-Mechanik mitbringt — nicht auf den Stop-Loss allein.

**Wichtige Einschränkung, bevor daraus mehr gelesen wird, als der Test
hergibt:** `RandomStrategy` lässt Trend-Filter und RR-Gate gleichzeitig
weg, nicht einzeln. Der Test zeigt, dass diese Kombination (Trend-Filter +
RR-Gate + die EMA/RSI/MACD-Konfluenz aus `generate_entry_signal`)
gegenüber "gar keiner Filterung" etwas beiträgt — er zeigt **nicht**,
welcher einzelne Bestandteil davon trägt. "Der Trend-Filter funktioniert"
wäre eine Aussage, die dieser Aufbau nicht stützt; dafür müsste man
Trend-Filter und RR-Gate getrennt voneinander herausnehmen, nicht beide
gleichzeitig.

**Der eigentlich interessante Befund kommt vom SMA-Crossover:** Er
schneidet in beiden Fenstern nicht nur schlechter ab als die echte
Strategie, sondern **schlechter als der Random-Median** (Auswahl: −0.95
vs. −0.90; Validierung: −0.92 vs. −0.89) — trotz desselben ATR-Stops wie
die echte Strategie und trotz fast doppelt so vieler Trades. Eine simple
Regel ("Preis kreuzt einen gleitenden Durchschnitt") ist hier *nicht*
näherungsweise so gut wie die dreiteilige Konstruktion aus Trend-Filter,
RR-Gate und EMA/RSI/MACD-Konfluenz — sie ist nicht einmal so gut wie
Zufall mit demselben Stop. Das ist ein stärkerer Beleg für die
Zusatzkomplexität als der Random-Vergleich allein: die Strategie schlägt
nicht nur "kein Signal", sie schlägt auch "ein einfacheres, aber
plausibel klingendes Signal" deutlich. Warum der SMA-Crossover so
schlecht abschneidet, ist eine eigene Frage (denkbar: verspätete
Cross-Signale in trendlosen/volatilen Phasen erzeugen viele
Fehlsignale — die höhere Trade-Zahl bei niedrigerem Return/MaxDD deutet
in diese Richtung) und nicht Teil dieser Untersuchung.

Damit gibt es jetzt drei Referenzpunkte statt einem: Buy & Hold (obere
Nulllinie — kein Trading, kein Stop-Loss, mit anderem Risikoprofil, siehe
oben), `RandomStrategy` (untere Nulllinie — derselbe Stop, kein Signal)
und `SmaCrossoverStrategy` (die einfachste nicht-zufällige Regel). Die
Strategie liegt in beiden Fenstern klar über allen dreien, nicht nur über
dem naheliegendsten Vergleich. Eine zweite echte Strategiefamilie über
die SMA-Crossover-Baseline hinaus ist damit vorerst zurückgestellt.