# Iran July 17 YES Protection Bot

This repo contains a scoped position-protection workflow for the US-Iran
peace-talks July 17 YES thesis under `polybot/iran/`.

## Commands

Inspect and manually verify the July 17 market:

```bash
.venv/bin/python -m polybot.geopolitics inspect-iran --config configs/geopolitics/iran-july17-yes-protection.yaml
```

Run the polling loop:

```bash
.venv/bin/python -m polybot.geopolitics run-iran --config configs/geopolitics/iran-july17-yes-protection.yaml
```

Run live only after config, credential, source, and position-size review:

```bash
.venv/bin/python -m polybot.geopolitics run-iran --config configs/geopolitics/iran-july17-yes-protection.yaml --live
```

## Operator Gate

Live execution is blocked unless the current config hash is acknowledged and
the position mode is `live`. The bot reads these files every cycle, so a running
process can be moved out of live mode without redeploying.

Preflight the current config:

```bash
.venv/bin/python -m polybot.geopolitics preflight-iran --config configs/geopolitics/iran-july17-yes-protection.yaml --live
```

Set the position mode:

```bash
.venv/bin/python -m polybot.geopolitics set-iran-mode --config configs/geopolitics/iran-july17-yes-protection.yaml --mode live
```

Acknowledge the exact config hash:

```bash
.venv/bin/python -m polybot.geopolitics ack-iran-live --config configs/geopolitics/iran-july17-yes-protection.yaml --note "reviewed July 17 live config"
```

Probe the current deposit-wallet `clob-client-v2` execution path without
posting an order:

```bash
.venv/bin/python -m polybot.geopolitics probe-iran-clob-v2 --config configs/geopolitics/iran-july17-yes-protection.yaml --amount 5
```

This initializes the TypeScript v2 client with `POLY_1271`, reads the held
conditional-token balance, checks open orders and the book, signs a local FAK
SELL order for the held side, prints a sanitized summary, and exits without
calling `postOrder`.

There is also an explicit posted-probe mode for a deliberately non-crossing
minimum-size FAK SELL:

```bash
.venv/bin/python -m polybot.geopolitics probe-iran-clob-v2 --config configs/geopolitics/iran-july17-yes-protection.yaml --amount 5 --post
```

Posted probe mode forces the probe price to `0.99` and blocks if that price
could cross the current ask. It is still a real order submission.

Smoke the configured classifier without executing anything:

```bash
.venv/bin/python -m polybot.geopolitics smoke-iran-classifier --config configs/geopolitics/iran-july17-yes-protection.yaml --text "Reuters reports senior US and Iranian representatives scheduled a formal round of talks for July 14."
```

Live execution still defaults to the Python `py-clob-client` adapter. The
legacy TypeScript `clob-client-v2` backend remains available for diagnostics,
but the posted probe exposed the current Polymarket deposit-wallet breakage:
the exchange rejects posted orders because the order signer is the deposit
wallet while the API key is bound to the EOA.

```bash
POLYBOT_EXECUTION_BACKEND=clob_v2
```

The official beta SDK backend is wired separately:

```bash
POLYBOT_EXECUTION_BACKEND=polymarket_beta
```

That backend requires Node 24. The bot has been migrated to the beta-derived
deposit wallet `0xf9021f4aa0cec3059a6b1da1083a68c9dc5fa267`, which is where
on-chain reconciliation shows the July 17 YES shares. The beta bridge refuses
balance queries and orders if `DEPOSIT_WALLET_ADDRESS` differs from the beta
SDK's derived wallet, so it cannot silently trade the wrong account.

Both TypeScript bridges refuse mutating actions unless called through the
Python adapter, which sets an internal `POLYBOT_TS_BRIDGE_ALLOW_POST=1` flag
after the operator gate has allowed execution.

Modes are `off`, `alert_only`, `dry_run`, and `live`. In the scoped Iran runner,
only `live` permits real orders; the other modes block execution. The default
position mode is `alert_only`, and the default global mode is `live`. A global
mode file at `data/operator/global_mode.json` can force all positions to a safer
mode, including `off`.

For live preflight, missing Telegram credentials block execution when
`safety.degraded_mode_alert: true`. Missing `ANTHROPIC_API_KEY`/`LLM_API_KEY`
also blocks live execution when `classifier.provider: anthropic`.

## Read-Only Portfolio View

`configs/geopolitics/positions.example.yaml` is the first step toward the broader position manager.
It is read-only and can inspect configured markets without authorizing trades:

```bash
.venv/bin/python -m polybot.main positions --config configs/geopolitics/positions.example.yaml
.venv/bin/python -m polybot.main inspect-position iran-july17-yes --config configs/geopolitics/positions.example.yaml
```

The snapshot includes market metadata, token mapping, live YES/NO balances,
open orders when authenticated, best bid/ask where available, configured
limits, and the operator gate status for that position.

## Current Live Boundary

`run-iran --live` is live-capable when `execution.dry_run: false`, the operator
passes `--live`, the operator gate passes preflight, and the market verifies as
active/open/accepting orders. The live adapter queries conditional balances with
`get_balance_allowance`, cancels open market orders before sizing when
`safety.cancel_open_orders_first: true`, and posts FAK market orders through
`py-clob-client`.

Live setup must use an SDK-supported signature type:

- `POLYBOT_SIGNATURE_TYPE=0` for an EOA
- `POLYBOT_SIGNATURE_TYPE=1` for a Polymarket proxy/Magic wallet
- `POLYBOT_SIGNATURE_TYPE=2` for a browser-wallet Gnosis Safe proxy
- `POLYBOT_SIGNATURE_TYPE=3` for a Polymarket deposit wallet / `POLY_1271`

If `DEPOSIT_WALLET_ADDRESS` is set and no explicit signature type is provided,
the bot defaults to `3`. If only `FUNDER_ADDRESS`/`POLYBOT_FUNDER_ADDRESS` is
set, it defaults to `1`. Unsupported signature types fail at settings load time.

## July 17 YES Guardrails

The calendar decay path is intentionally news-aware. If a trusted article
produces a YES-side `senior_round_scheduled_hold_not_resolved` decision, the
bot writes nonterminal `YES_SCHEDULED_HOLD_SIGNAL`. Time-decay trim/exit checks
that signal and skips the calendar sale while the configured suspension window
is active.

The live July 17 config also has explicit time-decay sell floors:

- `time_decay.min_trim_price: 0.05`
- `time_decay.min_exit_price: 0.10`

When a time-decay sale would occur below the configured YES best-bid floor, the
bot writes nonterminal `TIME_DECAY_PRICE_FLOOR`, alerts, and keeps polling
instead of dumping the position at the FAK minimum.

Feed items are discovery-grade by default. Reuters/AP discovery uses Google
News RSS; State Department discovery uses official State RSS feeds and safely
no-ops if State returns an HTML error page instead of RSS. Feed-only items do
not auto-trade while `allow_feed_auto_trade: false`. If a feed item resolves to
a publisher URL but the publisher article cannot be fetched, the bot keeps the
feed title/summary as `promoted_feed_summary`; that can alert or record a
scheduled hold, but it cannot auto-trade.

IRNA is alert-only by default. It can still surface useful information, but a
single Iranian state-media item is not allowed to auto-trade.

## Covered Behavior

- strict single-source classification path with two classifier passes
- deterministic pass-agreement checks
- verbatim quote verification
- deterministic source-domain allowlist checks before execution
- YES-protection classifier fields for event type, seniority, deadline timing,
  and source tier
- time-decay trim/exit decisions for silence risk
- explicit states: trigger, cancel, sell, partial, sold, buying, exited,
  incomplete, stopped, execution error, unconfirmed position, price-floor skip
- `safety.cancel_open_orders_first: true` cancels open market orders before the
  position balance check
- July 17 YES protection sells YES first and optionally buys capped NO only on
  high-confidence break signals
- one-shot terminal-state prevention
- feed-based scheduled hold signals do not overwrite terminal states
