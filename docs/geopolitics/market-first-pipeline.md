# Market-First Discovery Pipeline

The protection/entry bots are execution kernels: they assume an operator
already chose a market and hand-filled its outcomes, token IDs, and rules.
`polybot/discovery/` inverts that: markets come first, and the executors are
the final component.

```
discover geopolitical markets
  -> read context and resolution rules
  -> score tradeability and ambiguity
  -> plan sources and estimate probability vs price
  -> allocate, then hand off to the entry/defense/exit executors
```

## Stages and commands

All commands take `--config configs/discovery/<name>.yaml`; all state lives
under the config's `data_dir` as atomic JSON records.

| Stage | Command | What it does |
| --- | --- | --- |
| 1. Universe | `discover-markets` | Enumerates active Gamma events (liquidity-ordered, paginated), keeps geopolitical candidates by tag/keyword filter, drops markets below liquidity+volume floors, and builds a durable `MarketContext` per market: question, outcomes, condition/token IDs, neg-risk structure, deadline, verbatim rule text pinned by SHA-256. Re-runs refresh live fields; a changed rule hash bumps `rule_version`, drops the analysis, and demotes the market. |
| 2. Context | `grade-markets` (analysis step) | The rule analyzer (Anthropic, or a deterministic fixture offline) reads the VERBATIM rules and produces structure: what explicitly counts / does not count, cancellation/postponement behavior, ambiguous terms, discretion, deciding parties/mediators, decisive-event vocabulary, and who would credibly report the decisive event. The downstream classifier never infers rules from the question alone. |
| 3. Grading | `grade-markets` (scoring step) | Scores rule clarity, evidence observability, liquidity, spread, time horizon, resolution risk, correlation, and automation suitability, then assigns a state (below). |
| 4. Sources | `plan-sources` | Derives a per-market source plan FROM the context: official domains of deciding parties/mediators, wires, the market's named resolution source, and Google News discovery queries built from parties + decisive vocabulary. No analysis, no plan. |
| 5. Opportunity | `scan-opportunities` | For each eligible outcome: `edge = estimated_probability − executable ask − slippage − resolution-risk buffer − model-uncertainty buffer`, must clear `min_edge`. Probability estimates come from operator config (`opportunity.probability_estimates`) or, later, promoted forecast state; an outcome without an estimate is reported, never traded. Every result carries its blockers for the funnel. |
| 6. Allocation | (inside scan) | The `PortfolioAllocator` previews per-order, per-market, per-event, per-correlation-group, per-deadline-week, daily, and total caps plus a simultaneous-position limit, persisted in `allocations.json`. Discovering more markets must not multiply correlated risk. |
| 7. Handoff | `emit-bot-config` | Renders a ready-to-review executor config (binary bot for single markets, location bot for grouped neg-risk events) with the rule hash pinned, sources from the plan, dry-run true, and budgets from the allocator. The operator gate / ack sequence still applies — emission arms nothing. |
| 8. Measurement | `funnel-report` | all → understandable → observable → eligible → mispriced → executable, plus state counts, top blockers, and current portfolio exposure. Profitability is measured on the funnel, not on one hand-picked market. |

## Market states

- `DISCOVERED` — matched the geopolitical filter; no context analysis yet
- `RULES_REVIEW_REQUIRED` — missing/short rule text, unverified token mapping, no analysis yet, or the rule hash changed
- `PAPER_ELIGIBLE` — understandable and observable, but a live gate failed (liquidity, spread, horizon, resolution risk, correlation limit)
- `LIVE_CONFIRMATION_ELIGIBLE` — every live gate passed; may be emitted for the confirmed-entry executor path
- `MONITOR_ONLY` — discretionary/ambiguous rules or unobservable evidence
- `REJECTED` — not geopolitical / excluded vocabulary
- `CLOSED` — resolved, closed, past deadline, or not accepting orders

## Hard safety rules

- Missing full resolution text → `RULES_REVIEW_REQUIRED`, never tradeable.
- Discretionary or unclear rules → `MONITOR_ONLY`.
- Changed rule hash → analysis dropped, market demoted, emitted configs fail
  closed on their pinned SHA-256.
- Unverified token mapping → `RULES_REVIEW_REQUIRED`.
- No source plan → `emit-bot-config` refuses; a plan built for an older
  rule-text version is also refused.
- Excessive spread, price above cap, unknown quotes, or edge below minimum →
  blocked at scan time with the reason recorded.
- Allocator caps exceeded → blocked; commits are atomic and fail closed on a
  corrupt ledger.
- Emitted configs are always dry-run with the operator gate at its default
  (`alert_only`) — the standard inspect/preflight/ack/soak sequence from
  `autonomous-entry-hardening.md` still governs live arming.

## What stays manual (deliberately)

- Probability estimates for the opportunity scan are operator- or
  forecast-supplied; the pipeline never invents them.
- Emitted configs are reviewed before running: entry target lists should be
  trimmed, sources sanity-checked, and the rule text read by a human once.
- Live arming still requires the config-hash ack and position-mode flip.
