# Location Protection Bot Specification

## Purpose

Protect a held YES position on one venue leg of a grouped categorical Polymarket event. The primary live position is Qatar YES for “Where will the next US-Iran peace talks be?” by the configured deadline.

The bot must sell the held venue YES only when a source-backed, decision-relevant event makes the held venue unlikely to resolve YES. It may buy a replacement venue YES only for configured rotation targets.

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
- live position query confirms held YES exposure;
- operator position mode is live and config hash is acknowledged;
- execution `dry_run` is false and command uses `--live`;
- Telegram alerting is configured;
- classifier provider is available;
- source freshness policy blocks stale and unknown-age auto-trades unless explicitly overridden.
