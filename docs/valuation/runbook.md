# Valuation Runbook

The valuation bot is TypeScript-only. Active strategy code lives under
`src/valuation/`, with config in `configs/valuation/private-valuations-july31.json`
and runtime state/logs under `data/valuation/` and `logs/valuation/`.

## Main Commands

Run read-only discovery and audits first:

```bash
TMPDIR=/tmp ./node_modules/.bin/tsx src/valuation/cli.ts preflight --config configs/valuation/private-valuations-july31.json
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

`npm start` is intentionally mapped to valuation preflight, not the legacy
watcher. If `npm` resolves to a Windows install from WSL, run the direct `tsx`
command above with `TMPDIR=/tmp`; preflight reports this as a runtime warning.

## Live Promotion Policy

Only source-confirmed stale YES taker candidates are live-eligible today.
Forecast, ladder-maker, curve, calendar, and ranking signals remain paper or
research diagnostics until their promotion gates are explicitly changed in
`src/valuation/strategy/promotionGates.ts`.

Before adding any new live mode, require proof metrics for sample size, fill
quality, realized/simulated edge, stale-source error rate, and maximum drawdown.
Preflight reports the active paper-to-live gates and live-blocker audits include
the relevant promotion-gate blocking reason.

## Layout

- `src/valuation/cli.ts`: command dispatcher for scan, audits, reports, paper
  workflows, automation, probes, and live acks.
- `src/valuation/commands/audit.ts`: curve, market, and candidate audit command
  implementations plus shared market-audit row construction.
- `src/valuation/commands/scan.ts`: scan command implementation, candidate
  ranking, cap application, and execution/alert logging.
- `src/valuation/execution/liveExecution.ts`: live-blocker audit policy shared by
  preflight, market audits, and candidate audits.
- `src/valuation/services/collectValuationState.ts`: evidence, event, leg, quote,
  and threshold-candidate collection service shared by valuation commands.
- `src/valuation/strategy/`: reusable valuation strategy modules.
- `src/valuation/legacy/`: older single-market NPM watcher path retained for
  compatibility and focused parser/orderbook tests; it is not the default
  runtime path.
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
