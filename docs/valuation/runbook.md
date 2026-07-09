# Valuation Runbook

The valuation bot is TypeScript-only. Active strategy code lives under
`src/valuation/`, with config in `configs/valuation/private-valuations-july31.json`
and runtime state/logs under `data/valuation/` and `logs/valuation/`.

## Main Commands

Run read-only discovery and audits first:

```bash
npm run valuation:discover
npm run valuation:market-audit
npm run valuation:forecast-audit
npm run valuation:entry-audit
```

Run paper workflows before enabling live execution:

```bash
npm run valuation:forecast-paper
npm run valuation:ladder-paper
npm run valuation:daily-report
```

Run one automation cycle:

```bash
npm run valuation:auto
```

The default config keeps the strategy in alert/paper mode unless the config,
live ack, probe metadata, orderbook liquidity, source freshness, and candidate
locks all pass the live gates.

## Layout

- `src/valuation/cli.ts`: command dispatcher for scan, audits, reports, paper
  workflows, automation, probes, and live acks.
- `src/valuation/strategy/`: reusable valuation strategy modules.
- `src/valuation/legacy/`: older single-market NPM watcher path retained for
  compatibility and focused parser/orderbook tests.
- `src/valuation/logging.ts`: JSONL logging helper shared by valuation entry
  points.
- `tools/polymarket-ts/`: shared Polymarket bridge scripts used outside the
  valuation namespace too.

## Verification

Before changing live-relevant behavior, run:

```bash
./node_modules/.bin/tsc --noEmit
TMPDIR=/tmp ./node_modules/.bin/tsx --test test/*.test.ts
```

For full repo confidence after shared path changes, also run:

```bash
.venv/bin/python -m compileall -q polybot
.venv/bin/python -m pytest -q -s
```
