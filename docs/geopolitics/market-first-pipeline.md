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

## Profit levers

The confirmed-entry strategy's economics are dominated by latency, classifier
cost, and market selection. Four levers target them directly:

- **Small-size live tier**: thin markets are where confirmation edge persists
  longest (nobody competes for $20 of edge). When liquidity is the ONLY failed
  live gate, the market stays `LIVE_CONFIRMATION_ELIGIBLE` with
  `recommended_max_order_usd = liquidity * small_live_liquidity_fraction`;
  opportunity scans and emitted configs size orders to what the book can
  absorb instead of demoting the market to paper.
- **Screen classifier tier** (`classifier.screen_model`): every escalated
  article is first classified once by a cheap fast model; the expensive
  trade-grade model (with pass agreement) only runs when the screen sees
  anything other than NO_ACTION. Most escalated articles are noise, so this
  cuts the dominant classifier cost and answers faster on noise. A screen
  failure escalates rather than blocks, and the location bot still feeds
  screen signals to the paper forecast engine so priors keep updating.
- **Armed fast polling** (`safety.armed_poll_seconds`): live bots poll at
  seconds, not tens of seconds -- the race is lost in the gap between
  publication and the next cycle. Emitted configs default to 5s live / 30s
  dry-run.
- **Direct publisher feeds**: source plans now lead with direct RSS endpoints
  (state.gov press releases, UN news, Al Jazeera) ahead of Google News
  queries, whose indexing lag is often 5-15 minutes.

## Fleet mode: the whole-universe autopilot

`run-fleet` is one supervisor for ALL geopolitical markets. Each cycle it:

1. runs the full discovery cycle (discover → grade → plan-sources → scan);
2. emits/refreshes an executor config per `LIVE_CONFIRMATION_ELIGIBLE` market
   (liquidity-ranked, capped at `fleet.max_bots`), re-emitting only when the
   rule hash or dry-run mode changed so the ack hash doesn't churn;
3. arms each market's operator gate with `fleet.position_mode` and — with
   `--live`, `position_mode: live`, and `auto_ack: true` — writes the config
   ack, so the operator arms the fleet once instead of each market;
4. supervises one bot subprocess per market (crashed bots restart next cycle);
5. stops flat bots for demoted/closed markets — but a bot defending a HELD
   position is never stopped by a grading change.

All bots share `data/geopolitics/operator/`, so `set-fleet-mode off` is a
single master kill switch that every executor obeys mid-cycle, and the shared
portfolio ledger caps total exposure regardless of how many bots run.
`services/geopolitics-fleet.service` deploys it (KillMode=control-group takes
the supervised bots down with the fleet).

## Recurring operation

`run-discovery` runs the full cycle (discover → grade → plan-sources → scan)
on `schedule.interval_minutes`, alerting via Telegram whenever a market newly
becomes `LIVE_CONFIRMATION_ELIGIBLE` or an opportunity newly clears every
gate. `run-discovery --once` performs a single cycle for cron/systemd timers.
Cycle diffs persist in `pipeline_state.json`; failures log and wait for the
next cycle instead of killing the loop.

## Shared portfolio ledger

The allocator persists its caps into `allocations.json` (the ledger) on every
pipeline run. Emitted executor configs carry a `portfolio:` section binding
them to that ledger, and both the location and binary executors then:

- clamp every entry/rotation/flip buy by the ledger preview **in addition to**
  their own risk budgets and guardrails (any ledger problem fails closed);
- debit the ledger on order attempt (reserve-on-attempt, matching RiskState
  semantics: an unfilled order still consumed allowance);
- free the simultaneous-position slot when the position closes (exit,
  incomplete rotation/flip, or wallet reconciliation to flat).

Hand-written configs without a `portfolio:` section are unaffected.

## Probability sources for the scan

`scan-opportunities` prices an outcome only when it has a probability
estimate, in priority order: fresh paper-forecast state
(`forecast_probability.json` written by the location bot's forecast engine
under the emitted config's data dir), then operator-supplied
`opportunity.probability_estimates`. Stale forecast state (older than
`forecast_max_age_hours`) is ignored, and an outcome with no estimate is
reported as `no_probability_estimate` -- never traded.

## What stays manual (deliberately)

- Probability estimates for the opportunity scan are operator- or
  forecast-supplied; the pipeline never invents them.
- Emitted configs are reviewed before running: entry target lists should be
  trimmed, sources sanity-checked, and the rule text read by a human once.
- Live arming still requires the config-hash ack and position-mode flip.
