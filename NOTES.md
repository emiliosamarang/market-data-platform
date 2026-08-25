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