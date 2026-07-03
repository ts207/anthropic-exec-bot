# polybot

`polybot` is a source-update execution bot for narrow Polymarket markets. The
first target is airport-station daily-high weather markets. The default weather
source is METAR through the official AviationWeather.gov API; Wunderground is
kept as an optional reference/calibration source. The default is always dry-run;
the main purpose is to produce a complete JSONL trail that shows whether a
source-first edge exists.

This repository also contains the earlier TypeScript NPM/Anthropic bot. The
Python `polybot/` package is separate and does not overwrite that code.

## Install

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

Verified locally:

- `py-clob-client==0.34.6`
- `ClobClient(host, chain_id, key, creds, signature_type, funder)`
- `create_market_order(MarketOrderArgs, PartialCreateOrderOptions)`
- `post_order(order, OrderType.FAK)`
- `MarketOrderArgs(token_id, amount, side, price, order_type)`

Important: the public `Polymarket/py-clob-client` repository is marked archived
and no longer recommended for new integrations. The wrapper is therefore
defensive, dry-run first, and documents the installed API it was verified
against. Re-check the official Polymarket SDK/docs before live use.

## Environment

Dry-run is the default:

```bash
POLYBOT_DRY_RUN=1
```

Live trading requires all three:

1. `POLYBOT_DRY_RUN=0`
2. `python -m polybot.main run --config markets.yaml --live`
3. typing `yes` at the interactive guardrail prompt

Trading-related variables:

```bash
POLYBOT_PRIVATE_KEY=
POLYBOT_CLOB_API_KEY=
POLYBOT_CLOB_SECRET=
POLYBOT_CLOB_PASSPHRASE=
POLYBOT_SIGNATURE_TYPE=
POLYBOT_FUNDER_ADDRESS=
```

Guardrails are read from `polybot.config.Guardrails`. Env vars may only make
the bot more conservative; values above the defaults are clamped back to the
defaults.

```bash
POLYBOT_MAX_ENTRY_PRICE=0.90
POLYBOT_MAX_ENTRY_PRICE_REVISABLE=0.85
POLYBOT_PER_ORDER_NOTIONAL=25
POLYBOT_PER_MARKET_NOTIONAL=50
POLYBOT_PER_DAY_NOTIONAL=100
POLYBOT_KILL_SWITCH_FAILURES=2
```

## Weather Source

Weather markets default to `source: metar`, using the official AviationWeather
Center endpoint:

```text
https://aviationweather.gov/api/data/metar?ids=KLGA&format=json
```

No key is required. `polybot` sends the configured custom User-Agent and limits
polling to one request per minute per station. AviationWeather's database
currently exposes only the previous 15 days of data, so historical calibration
is capped there.

For US stations, the METAR parser decodes the `T` remark group, such as
`T03780211`, so tenths of a degree Celsius are preserved before converting to
Fahrenheit. This avoids the common one-degree Fahrenheit error at bucket edges.

The weather strategy also has a mandatory boundary guard: it maps the rounded
display value into a bucket, then skips if the raw temperature is less than
0.5° from the bucket edge. Boundary days are data, not trades.

Run a calibration table:

```bash
.venv/bin/python -m polybot.calibrate_metar \
  --station KLGA \
  --timezone America/New_York \
  --unit F \
  --days 15 \
  --wu-url-template 'https://www.wunderground.com/history/daily/{station}/date/{iso_date}' \
  > calibration-klga.csv
```

If you manually record Wunderground daily highs in a CSV with `date,value`, add
`--wu-values wu-klga.csv` to emit deltas and exact-match flags.

## Wunderground Setup

Wunderground history pages are JavaScript-rendered and backed by weather.com
APIs that require a key. `polybot` does not scrape or hardcode browser keys.
Configure your own authorized data access only if you explicitly set
`source: wunderground` in a market:

```bash
WU_API_KEY=...
WU_HISTORY_URL_TEMPLATE='https://your-authorized-endpoint.example/history?station={station}&date={date}&unit={unit}&apiKey={api_key}'
```

The template may use `{station}`, `{date}` (`YYYYMMDD`), `{iso_date}`,
`{unit}`, and `{api_key}`. The adapter polls day `D` and `D+1`; it only returns
`confidence=1.0` when the first `D+1` observation exists and there is no
afternoon gap over two hours. Epoch timestamps from `valid_time_gmt` are
converted into the configured station timezone before lock detection and gap
checks.

Source polling is limited to one request per minute per station and checks
`robots.txt`. If a source blocks automated access, stop and use a permitted data
route.

## Add A Market

Copy `markets.example.yaml`:

```yaml
markets:
  - type: weather
    source: metar
    slug: highest-temperature-in-seoul-on-july-3-2026
    station: RKSI
    date: "2026-07-03"
    unit: C
    timezone: Asia/Seoul
    poll_seconds: 60
```

Inspect before running:

```bash
.venv/bin/python -m polybot.main inspect highest-temperature-in-seoul-on-july-3-2026
```

Dry-run:

```bash
.venv/bin/python -m polybot.main run --config markets.yaml
```

## Log Schema

Logs are JSONL at `logs/polybot.jsonl`. Every event includes:

- `ts_utc`
- `ts_et`
- `ts_mono`
- `event`

Important event types:

- `source_poll`
- `source_locked`
- `strategy_skip`
- `book_snapshot`
- `order_skip`
- `order_submit`
- `settlement_check`
- `settlement_terminal`
- `risk_state_update`

Every skip has a machine-readable `reason`, such as `price_above_cap`,
`no_depth`, `stale_book`, `not_tradeable`, `daily_limit`,
`parse_low_confidence`, `already_traded`, or `halted`.

Live orders are at-most-once per market. The risk state reserves the market and
notional before `post_order` so a transient network exception cannot cause a
resubmit. A shared `SettlementWatcher` polls live orders until confirmed,
failed, or timed out; failures and timeouts feed the kill switch.

## Go-Live Checklist

- Run the test suite: `.venv/bin/python -m pytest -q -s`.
- Run `inspect` on the market and manually verify Gamma fields, token IDs,
  `tick_size`, `neg_risk`, and `accepting_orders`.
- Independently verify Polymarket access and legality in your jurisdiction.
- Independently verify that the market rules match the configured station,
  date, unit, and bucket labels.
- Run METAR calibration for the station and manually compare against
  Wunderground history away from bucket boundaries.
- If using `source: wunderground`, confirm that Wunderground data access is
  authorized and reliable.
- Run at least one full dry-run session that produces
  `source_poll -> source_locked -> book_snapshot -> order_submit(dry_run)`.
- Fund only an amount you can lose entirely.
- Keep guardrails at or below defaults unless the code is re-reviewed.

## Out Of Scope

No selling, no exits, no market making, no automatic position-size increases, no
macro hot path beyond a stub, no crypto/sports/social-media adapters, and no UI.
