# Location Protection Bot Specification

## Purpose

Protect a held YES position on one venue leg of a grouped categorical Polymarket event. The primary live position is Qatar YES for “Where will the next US-Iran peace talks be?” by the configured deadline.

The bot must sell the held venue YES only when a source-backed, decision-relevant event makes the held venue unlikely to resolve YES. It may buy a replacement venue YES only for configured rotation targets.

Optionally, the bot can also OPEN the position itself: with `entry.enabled` and an empty `event.held_location`, it starts flat, watches the same sources, and buys a configured entry target's YES when a trusted tier-one source confirms a qualifying senior round there. After a verified entry fill it defends the entered leg with the same protection machinery described below.

## Position lifecycle

The live holding is persisted in `data_dir/holdings.json` (dry-run isolated under `data_dir/dry_run/`), not in the YAML: `event.held_location` is only the initial default, and an explicit holdings record always wins — including an explicit flat record after an exit.

```
FLAT --(ENTER_YES, verified fill)--> HOLDING <--(ROTATE_YES, verified fill)--+
  ^                                    |  \___________________________________/
  |                                    |
  +----(EXIT_YES_ONLY / rotation-incomplete sale)
```

- Flat bot: articles are routed through the entry decision table; time decay, trims, and exits do not apply.
- Holding bot: articles are routed through the protection decision table against the LIVE holding (which may be the entered or rotated-into leg, not the configured one).
- After an exit the bot is flat again, but `safety.one_shot` (terminal state) and `entry.max_entries` block automatic re-entry.

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
- Entries are counted against `entry.max_entries` (lifetime, persisted in `entry_count.json`), separately from `safety.max_executions`, so an entry can never consume the execution budget needed to defend the position it opened.
- All trade-action policies apply to `ENTER_YES` unchanged: operator gate + live ack, quote verification, source domain policy, article freshness, feed auto-trade restrictions, auto-execute level, and market re-verification blocks.
- An unfilled or above-cap entry leaves the bot flat (`ENTRY_UNFILLED` / `ENTRY_PRICE_ABOVE_CAP` are non-terminal); a filled entry writes `ENTERED` and updates holdings before anything else can trade.

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
- Calendar/time-decay sales are blocked below configured bid floors.
- News-triggered exits are not blocked by calendar/time-decay price floors.

## Live arming standard

Do not run live unless all of the following hold:

- event rule text hash has been inspected and pinned;
- every configured outcome verifies against live Gamma condition/token IDs;
- live position query confirms held YES exposure (or, for a flat entry-enabled
  config, every entry target verifies tradeable);
- operator position mode is live and config hash is acknowledged;
- execution `dry_run` is false and command uses `--live`;
- Telegram alerting is configured;
- classifier provider is available;
- source freshness policy blocks stale and unknown-age auto-trades unless explicitly overridden.
