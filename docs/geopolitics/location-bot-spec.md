# Location Protection Bot Specification

## Purpose

Protect a held YES position on one venue leg of a grouped categorical Polymarket event. The primary live position is Qatar YES for “Where will the next US-Iran peace talks be?” by the configured deadline.

The bot must sell the held venue YES only when a source-backed, decision-relevant event makes the held venue unlikely to resolve YES. It may buy a replacement venue YES only for configured rotation targets.

Optionally, the bot can also OPEN the position itself: with `entry.enabled` and an empty `event.held_location`, it starts flat, watches the same sources, and buys a configured entry target's YES when a trusted tier-one source confirms a qualifying senior round there. After a verified entry fill it defends the entered leg with the same protection machinery described below.

## Position lifecycle

The wallet is authoritative. `data_dir/holdings.json` is an atomic local cache
(dry-run isolated under `data_dir/dry_run/`): `event.held_location` is only the
initial default. Live preflight and every live cycle query all configured YES
and NO balances. Exactly one meaningful YES balance is adopted; multiple YES
balances, any unexpected NO exposure, or resting orders fail closed for manual
reconciliation.

```
FLAT --(ENTER_YES, verified fill)--> HOLDING <--(ROTATE_YES, verified fill)--+
  ^                                    |  \___________________________________/
  |                                    |
  +----(EXIT_YES_ONLY / rotation-incomplete sale)
```

- Flat bot: articles are routed through the entry decision table; time decay, trims, and exits do not apply.
- Holding bot: articles are routed through the protection decision table against the LIVE holding (which may be the entered or rotated-into leg, not the configured one).
- `ROTATED` is non-terminal: the new leg remains protected and can later be
  rotated again or exited. After an exit, `safety.one_shot` and
  `entry.max_entries` control automatic re-entry.

## Entry decision table (flat only)

| Evidence from article | Required strength/source | Bot action | Reason |
| --- | --- | --- | --- |
| Confirmed venue is a configured entry target | tier-one source, senior round, confirmed started/scheduled, final decision announced | `ENTER_YES` | `confirmed_location:<target>` |
| Confirmed venue configured but not an entry target | any | `ALERT_ONLY` | `entry_target_not_allowed:<venue>` |
| Confirmed venue not configured / other_specific | any | `ALERT_ONLY` | `confirmed_location_not_configured:<venue>` |
| Confirmed venue but entry disabled | any | `ALERT_ONLY` | `entry_disabled_confirmed_location:<venue>` |
| Venue reported but weak / non-tier-one | any | `ALERT_ONLY` | `entry_signal_not_yet_confirmed:<venue>` |
| Venue confirmed but final decision not announced | any | `ALERT_ONLY` | `entry_venue_not_final:<venue>` |
| No-meeting report (leg not an entry target) | any | `ALERT_ONLY` | `no_meeting_reported_while_flat` |
| No-meeting confirmed and leg IS an entry target | tier-one, confirmed/denied evidence | `ENTER_YES` | `confirmed_location:no_meeting` |
| Technical/preparatory/staff-level only | any | `NO_ACTION` | `technical_or_non_qualifying` |
| Untrusted source | any | `ALERT_ONLY` | `source_not_trusted` |

## Entry invariants

- Entry uses the same evidence bar as a rotation buy, plus a stricter finality requirement (`final_decision_announced`).
- The no-meeting collapse fast path (first-pass execution without multi-pass agreement) never applies while flat: there is no loss to race, so an entry always requires full pass agreement.
- Entry spend is capped by `entry.usd_budget`, `entry.max_price`, and the global `POLYBOT_MAX_ENTRY_PRICE` guardrail (which can only lower the cap).
- Entry requires positive execution-adjusted edge: `confirmed_probability - ask - slippage_buffer - resolution_risk_buffer >= min_edge`.
- Entries are counted against `entry.max_entries` (lifetime, persisted in `entry_count.json`), separately from `safety.max_executions`, so an entry can never consume the execution budget needed to defend the position it opened.
- All trade-action policies apply to `ENTER_YES`: operator gate + live ack,
  quote verification, source domain policy, freshness, auto-execute level, and
  market verification. Feed and promoted-feed summaries cannot open risk;
  entry requires fetched publisher text or a first-party full-text feed.
- An unfilled, above-cap, or no-edge entry remains flat. Any positive fill is
  adopted and defended. A sub-threshold fill becomes `PARTIALLY_ENTERED`.
- Every mutation has an execution journal. Startup wallet reconciliation repairs
  local holdings after a crash between exchange acceptance and persistence.
- A process lock prevents two instances from trading the same strategy directory.

## Qualifying meeting standard

A qualifying event is a genuine, formal, senior-level round of US-Iran diplomacy that occurs in person or indirect-in-person via authorized mediators and matches the market resolution rules.

The following do not qualify on their own:

- technical talks
- staff-level talks
- working-group meetings
- implementation or monitoring meetings
- preparatory meetings
- deconfliction contacts
- greetings, chance encounters, or photo opportunities
- vague mediator diplomacy without a confirmed qualifying venue

## Anticipatory forecast research

`forecast.enabled` activates a deterministic, paper-only probability engine.
It is structurally separate from confirmed live entry and has no order-posting
method.

- Priors must cover every categorical outcome and sum to one.
- Classifier passes must agree on the explicit forecast target and direction,
  evidence strength, source tier, finality, and qualification fields before an
  update is accepted.
- The supporting quote must occur verbatim in article text. Supportive evidence
  uses a likelihood ratio above one; contradictory evidence uses the reciprocal
  ratio below one; neutral evidence cannot update probabilities.
- Exact articles and repeated supporting claims are deduplicated.
- Source-tier and evidence-strength likelihood ratios update the target with a
  bounded Bayesian odds update; every outcome remains normalized to one.
- Future expected formal venues can update probabilities. Technical venues
  alone do not become forecast targets.
- Dry-run paper execution reads public live CLOB books through an adapter with
  no mutation methods. Paper entry requires a fresh, sufficiently tight quote,
  buffered edge, and a maximum simulated fill price.
- Paper positions exit when fair probability no longer exceeds the executable
  bid by `exit_remaining_edge` after resolution-risk allowance. This check runs
  every poll, even when no new article arrives.
- Simulated entry/exit prices include configured fees and adverse slippage.
- Probability and paper states carry a model version plus configuration
  fingerprint; incompatible prior state is archived rather than silently used.
- Final confirmed reports update probabilities but are excluded from forecast
  entries because they belong to the separately gated confirmation strategy.

State is written atomically to `forecast_probability.json` and
`forecast_paper.json`; observations and simulated actions are appended to
`location_forecast_paper.jsonl`.

## Decision table

| Evidence from article | Required strength/source | Bot action | Reason |
| --- | --- | --- | --- |
| Confirmed venue is held location | trusted source, senior round, confirmed started/scheduled | `NO_ACTION` | `held_location_reinforced` |
| Confirmed venue is a configured rotation target | tier-one source, senior round, confirmed started/scheduled | `ROTATE_YES` | `confirmed_location:<target>` |
| Confirmed venue is configured but not a rotation target | tier-one source, senior round, confirmed started/scheduled | `EXIT_YES_ONLY` | `confirmed_non_rotated_location:<target>` |
| Confirmed venue is real but not configured | tier-one source, senior round, confirmed started/scheduled | `EXIT_YES_ONLY` | `confirmed_other_specific_location` |
| No qualifying meeting will occur by deadline | tier-one source, confirmed/denied/no-meeting evidence | `EXIT_YES_ONLY` | `no_meeting_confirmed` |
| No-meeting/collapse report is weak or non-tier-one | any weak/non-tier-one signal | `ALERT_ONLY` | `no_meeting_reported_unconfirmed` |
| Technical/preparatory/staff-level only | any source | `NO_ACTION` | `technical_or_non_qualifying` |
| Location report is speculative/indirect | any source | `ALERT_ONLY` | `location_not_yet_confirmed` |
| Source is untrusted | any location | `ALERT_ONLY` | `source_not_trusted` |
| Article quote does not match article text | trade action | `ALERT_ONLY` | `quote_verification_failed` |
| Article is too old | trade action | `ALERT_ONLY` | `article_stale_for_auto_trade` |
| Article age is unknown | trade action | `ALERT_ONLY` by default | `article_age_unknown_for_auto_trade` |
| Feed item auto-trading disabled | trade action | `ALERT_ONLY` | `feed_item_auto_trade_disabled` |
| Operator gate not live/allowed | trade action | `ALERT_ONLY` | `operator_block:<reason>` |

## Classifier invariants

- `confirmed_location` must use one of the configured outcome keys when the article names a configured outcome.
- The held location is always a valid `confirmed_location`, regardless of whether it is configured as an automatic rotation target.
- `other_specific` is reserved for a named, real venue that is not one of the configured outcome keys.
- `no_meeting` is reserved for credible reporting that no qualifying round will occur by the deadline.
- Analyst context is prior context only; the classifier must classify from the article text and market rules.

## Execution invariants

- Sell of held YES is the primary protection action.
- Rotation buy is optional and only runs after a verified held-YES sale.
- Rotation buy can only target configured rotation targets.
- Rotation buy budget is capped by configured budget, configured max rotation spend, and confirmed sale proceeds.
- Every entry and rotation buy is additionally capped by the persistent global
  per-order, per-market, and daily notional state; a halted risk state blocks
  new buys.
- Entry and rotation buys enforce configured maximum spreads.
- Protection counts use unique execution IDs, so repeated rotations consume
  the allowance instead of overwriting a single marker.
- Calendar/time-decay sales are blocked below configured bid floors.
- News-triggered exits are not blocked by calendar/time-decay price floors.

## Live arming standard

Do not run live unless all of the following hold:

- event rule text hash has been inspected and pinned;
- every configured outcome verifies against live Gamma condition/token IDs;
- wallet reconciliation across every configured outcome confirms exactly one
  held YES leg or a genuinely flat wallet;
- operator position mode is live and config hash is acknowledged;
- execution `dry_run` is false and command uses `--live`;
- Telegram alerting is configured;
- classifier provider is available;
- source freshness policy blocks stale and unknown-age auto-trades unless explicitly overridden.
- no incomplete or ambiguous wallet reconciliation remains;
- the single-instance process lock can be acquired.
