# polybot

`polybot` is now focused on Polymarket position protection for the US-Iran
peace-talks July 17 YES thesis. The active Python bot lives under
`polybot/iran/` and is configured by `iran-july17-yes-protection.yaml`.

The older weather-market automation path has been removed.

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
- `get_balance_allowance(BalanceAllowanceParams(...))`

Important: rechecked against current Polymarket trading docs on 2026-07-06.
The live Python path still uses `py-clob-client==0.34.6`; its public GitHub
repo is archived, so keep treating it as legacy. Its FAK market-order usage
matches current docs: FOK/FAK execute immediately against resting liquidity,
BUY amounts are dollars, and SELL amounts are shares. Prefer the maintained
`@polymarket/clob-client-v2` path for new execution work once the
deposit-wallet signer/API-key mismatch is resolved.

## Environment

Dry-run is the default:

```bash
POLYBOT_DRY_RUN=1
```

Live Iran execution requires all of:

1. `execution.dry_run: false` in the Iran YAML config
2. `python -m polybot.geopolitics run-iran --config <config.yaml> --live`
3. valid Polymarket credentials

The Iran classifier now calls the Anthropic API (`classifier.provider: anthropic`,
model `claude-opus-4-8`, two passes with required agreement and structured JSON
output). It needs a key in the bot's process environment — note the Python bot
does not read `.env`, so export it in the shell or run wrapper:

```bash
ANTHROPIC_API_KEY=
```

If the key is missing or the API is down, every escalated article degrades to
`ALERT_ONLY` and no trade is placed (`classifier.if_api_down`).

Trading-related variables:

```bash
POLYBOT_PRIVATE_KEY=
POLYBOT_CLOB_API_KEY=
POLYBOT_CLOB_SECRET=
POLYBOT_CLOB_PASSPHRASE=
POLYBOT_SIGNATURE_TYPE=
POLYBOT_FUNDER_ADDRESS=
```

Shared aliases are also accepted for compatibility:

```bash
PRIVATE_KEY=
CLOB_API_KEY=
CLOB_SECRET=
CLOB_PASS_PHRASE=
DEPOSIT_WALLET_ADDRESS=
FUNDER_ADDRESS=
CHAIN_ID=
CLOB_HOST=
```

Supported signature types are:

- `0` for EOA
- `1` for Polymarket proxy/Magic wallet
- `2` for browser-wallet Gnosis Safe proxy
- `3` for Polymarket deposit wallet / `POLY_1271`

If `DEPOSIT_WALLET_ADDRESS` is set and no explicit signature type is provided,
the bot defaults to `3`. If only `FUNDER_ADDRESS`/`POLYBOT_FUNDER_ADDRESS` is
set, it defaults to `1`.

## Commands

Inspect a Polymarket event:

```bash
.venv/bin/python -m polybot.main inspect <event-slug>
```

Inspect and verify an Iran config:

```bash
.venv/bin/python -m polybot.geopolitics inspect-iran --config iran-july17-yes-protection.yaml
```

Preflight live readiness, including operator mode, config hash ack, credentials,
token mapping, and live balances:

```bash
.venv/bin/python -m polybot.geopolitics preflight-iran --config iran-july17-yes-protection.yaml --live
```

Set the current position mode and acknowledge the exact config hash before live:

```bash
.venv/bin/python -m polybot.geopolitics set-iran-mode --config iran-july17-yes-protection.yaml --mode live
.venv/bin/python -m polybot.geopolitics ack-iran-live --config iran-july17-yes-protection.yaml --note "reviewed live config"
```

Read a portfolio-style position snapshot:

```bash
.venv/bin/python -m polybot.main positions --config positions.example.yaml
.venv/bin/python -m polybot.main inspect-position iran-july17-yes --config positions.example.yaml
```

Probe the TypeScript `clob-client-v2` deposit-wallet path without posting:

```bash
.venv/bin/python -m polybot.geopolitics probe-iran-clob-v2 --config iran-july17-yes-protection.yaml --amount 5
```

Smoke the configured classifier without executing:

```bash
.venv/bin/python -m polybot.geopolitics smoke-iran-classifier --config iran-july17-yes-protection.yaml --text "Reuters reports senior US and Iranian representatives scheduled a formal round of talks for July 14."
```

Live runs default to the Python CLOB adapter. The legacy TypeScript
`clob-client-v2` backend is still available for diagnostics, but the posted
probe exposed a Polymarket-side deposit-wallet signer/API-key mismatch:

```bash
POLYBOT_EXECUTION_BACKEND=clob_v2
```

The official beta SDK backend is also wired:

```bash
POLYBOT_EXECUTION_BACKEND=polymarket_beta
```

It requires Node 24. The bot has been migrated to the beta-derived deposit
wallet `0xf9021f4aa0cec3059a6b1da1083a68c9dc5fa267`, which is where on-chain
reconciliation shows the July 17 YES shares. The bridge refuses balance queries
and orders if `DEPOSIT_WALLET_ADDRESS` differs from the beta SDK's derived
wallet, so it cannot silently trade the wrong account.

Run the Iran bot:

```bash
.venv/bin/python -m polybot.geopolitics run-iran --config iran-july17-yes-protection.yaml
```

Run live only after config and credential review:

```bash
.venv/bin/python -m polybot.geopolitics run-iran --config iran-july17-yes-protection.yaml --live
```

## Iran Configs

- `iran-july17-yes-protection.yaml`: protects a YES position on the July 17
  peace-talks leg.
- `positions.example.yaml`: read-only portfolio snapshot config. It does not
  authorize live trading; operator mode files and config-hash acks still gate
  execution.

`sources.poll_urls` is for fixed, execution-grade article URLs. `sources.feed_urls`
is for discovery-grade RSS/Atom feeds. Feed items are processed through the same
keyword gate and classifier, but `allow_feed_auto_trade: false` keeps them from
placing trades by default. A trusted feed item can still record a scheduled-round
hold signal and pause a blind July 17 YES time-decay sale. If a feed item can be
resolved to a publisher URL but the full article fetch fails, it remains
`promoted_feed_summary`: useful for alerts/hold signals, never auto-trade.

## Safety Model

The Iran bot is a state machine, not a market-making system. It writes explicit
states below the configured `data_dir`, with dry-run state isolated under
`data_dir/dry_run`.

Important states include:

- `TRIGGER_DETECTED`
- `CANCELING_ORDERS`
- `SELLING_NO`
- `SELLING_YES`
- `NO_SOLD`
- `BUYING_YES`
- `BUYING_NO`
- `FLIPPED`
- `EXITED`
- `FLIP_INCOMPLETE`
- `NO_POSITION_UNCONFIRMED`
- `YES_POSITION_UNCONFIRMED`
- `TIME_DECAY_PRICE_FLOOR`
- `EXECUTION_ERROR`

Live execution cancels open market orders before sizing when
`safety.cancel_open_orders_first: true`. Transient zero-balance reads are
nonterminal, and unexpected execution exceptions write `EXECUTION_ERROR` and
keep the polling process alive.

The operator gate blocks live execution unless the position mode is `live` and
the current config hash has been acknowledged. The bot re-reads the mode files
every polling cycle; write `off` or `alert_only` to
`data/operator/positions/<config-stem>.mode` to stop live execution mid-run.
Live preflight also blocks missing Telegram credentials when degraded alerts are
required, and missing `ANTHROPIC_API_KEY`/`LLM_API_KEY` when the configured
classifier provider is `anthropic`.

The July 17 YES config includes calendar-decay brakes:

- trusted scheduled-round hold signals suspend time-decay selling temporarily
- `time_decay.min_trim_price` prevents dumping trims below a floor
- `time_decay.min_exit_price` prevents dumping full exits below a floor
- Reuters/AP discovery uses Google News RSS because stable public RSS is not
  available for those sources
- State Department discovery uses official State RSS feeds and safely no-ops in
  environments where State returns an HTML error page instead of RSS

IRNA is alert-only by default. It can surface information, but a single IRNA
item should not auto-trade.

## Logs

Primary logs are JSONL under `logs/`. Iran decisions are written to
`logs/decisions.jsonl`, article hashes to `logs/articles.jsonl`, and shared
runtime events to `logs/polybot.jsonl`.

Every log event is intended to explain why the bot acted or skipped.

## Go-Live Checklist

- Run the tests: `.venv/bin/python -m pytest -q -s`.
- Run `inspect-iran` and manually verify question, rule text, token IDs,
  `condition_id`, `tick_size`, `neg_risk`, and `accepting_orders`.
- Pin and review the rule-text SHA256.
- Add real `sources.poll_urls` if relying on news protection.
- Verify Polymarket access and legality in your jurisdiction.
- Confirm position caps match the actual position size you are willing to sell.
- Run at least one complete dry-run session and inspect state/log output.
- Fund only an amount you can lose entirely.

## Out Of Scope

No weather markets, no market making, no automatic position-size increases, no
generic sports/crypto/social-media adapters, and no UI.
