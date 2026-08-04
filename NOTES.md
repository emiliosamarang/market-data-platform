# Trading Bot — Bestandsaufnahme

Stand: 2026-08-04

## Zweck

Automatisierter Krypto-Spot-Trading-Bot für Binance. Scannt stündlich fünf Symbole, generiert Signale auf Basis technischer Indikatoren und Sentiment-Daten, öffnet und verwaltet Trades vollständig automatisch inklusive OCO-Orders (Take Profit / Stop Loss).

## Dateien

| Datei | Funktion |
|---|---|
| `bot.py` | Hauptlogik: Indikatoren, Strategie, Scanner, APScheduler-Loop |
| `config.py` | Konfiguration; alle Secrets kommen aus Umgebungsvariablen |
| `exchange.py` | Binance-Client-Wrapper (Daten holen, Orders platzieren, Lot/Tick-Rounding) |
| `trader.py` | Trade-Lifecycle: öffnen, OCO platzieren, Notfall-Close, sync |
| `database.py` | SQLite-Layer (Tabellen `signals` + `trades`) |
| `notify.py` | Telegram-Benachrichtigungen |
| `sentiment.py` | Fear & Greed Index + News-Sentiment via Claude Haiku (RSS → Anthropic API) |
| `backtest.py` | Historisches Backtesting mit Equity-Kurve und Drawdown-Reporting |
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
```

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