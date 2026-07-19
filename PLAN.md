# Polybot — Operating Plan

_Last updated: 2026-07-19_

## Where we are

Two paper-trading strategies run continuously as systemd user services on WSL, both
paper/alert-only, both survive reboots:

- **`polybot-fleet`** — geopolitics. Discovers markets, grades resolution rules, watches
  news feeds, and now forms its **own** P(YES) per market (autonomous estimator). Trades on
  two independent paths: news-confirmation entries and its own calibrated estimates.
- **`polybot-valuation`** — the "NPM" strategy. Trades Polymarket company-valuation ladders
  against Nasdaq Private Market's daily data print.

Neither can move real money: `dry_run: true`, operator gate `alert_only`, and (for the
estimator) a calibration gate that bars the bot's own opinions from live sizing until they
are proven.

## The one thesis both strategies share

We are small, slow (minutes not seconds), automated, and cheap to run. That profile only wins
in **thin, mechanical markets that a scheduled/verifiable signal resolves** and that faster
players ignore. Every profit engine below is a variation on: _public signal → independent
probability → edge vs. the book → paper journal → calibration → earned live sizing._

Nothing goes live on an edge the paper record has not demonstrated. Anything that looks like
free money is treated as a bug until proven otherwise — that rule caught three phantom-edge
bugs this weekend (negRisk arb, and the (LOW)-direction trap in two places), each of which
would have been a confident loss.

## The gate to profit (mechanical, self-running)

1. Estimator writes P(YES) per market each cycle (no market price in the prompt → independent).
2. Every scan prices those estimates against the live book; divergences recorded with the
   tradable price.
3. As markets resolve, the calibration log scores the bot's estimates (Brier) vs. the market.
4. **When the bot's Brier beats the market over 20 resolved outcomes**, `forecast_calibrated`
   flips true. Raising `opportunity.model_weight` (today 0.35, which mathematically shuts the
   edge bar) is then the evidence-backed switch that lets the bot trade its own view — paper
   first, then a small live arming decision.

This loop needs no more code. It needs resolutions and compute.

---

## Plan

### Track A — Let it run (now → the resolution dates)
- Both services soak untouched. Weekly 5-min review: `make status`, `make funnel`,
  `make calibration`, `make latency`.
- Watch two numbers: `no_probability_estimate` (should fall as the estimator covers the
  universe) and the calibration report's resolved-outcome count (climbing toward 20).
- Human judgment item: **Stripe** NPM tape stopped 2026-06-30 — the market's cessation clause
  may already decide those legs. Eyeball before July 31.

### Track B — Unblock estimator throughput (compute is now the binding limit)
The estimator, the fleet's classifiers, and interactive chats all draw the **same Claude
subscription quota**; 429 session limits starved this cycle (3 written, 2 errored).
- Decide a throughput policy: cheaper/separate tier for estimates, off-peak staggering, or
  accept slow coverage. This is the single biggest lever on how fast the bot earns calibration.
- Small code option: back off the estimator automatically when the subscription is rate-limited
  so it stops burning cycles on 429s.

### Track C — Sharpen the signal (make each estimate better)
- **Polling ingestion** for foreign-election markets (Brazil TSE, Sweden, UK, France, Germany
  already in the universe; Polymarket's US-centric crowd misprices them). Feeds the estimator
  real priors instead of pure base rates.
- **PortWatch parser** — nearest clone of the NPM strategy (official IMF Hormuz data, ~7 markets
  already graded). Data-anchored, schedulable, buildable in a day.

### Track D — Improve execution economics (multiplier on every strategy)
- **Maker orders** for data-anchored families: finance markets charge ~4% taker fees but pay
  maker rebates (0.25 in the fee schedule). Posting bids instead of lifting asks flips 3–6¢
  edges from marginal to real. Pure engineering.

### Housekeeping (low effort, do at next natural window)
- Cap Telegram article-spam (alerts chunk full articles into up to 16 messages → rate-limits
  the channel, can delay a real trigger).
- Filter closed outcomes out of the valuation sweep upstream (cycle time ~60min → ~20min).
- Optional: scheduled morning digest so the weekly review comes to you.

---

## Decision points (calendar)

- **~July 31** — valuation ladders resolve. First real calibration data; fastest path to a
  strategy earning live status. Confirms whether the (LOW) legs resolve as falls-to (validating
  this weekend's direction fix).
- **~Aug 2** — fleet news-confirmation go/no-go, judged on three numbers: gate escalations that
  reached confirm, edge remaining at signal time (book snapshots), classifier misfire count.
- **When any strategy's Brier beats the market over 20 resolutions** — raise `model_weight`,
  arm that strategy live at minimum size. Not before.

## Standing rules
- Paper record must demonstrate an edge before it goes live.
- "Obvious" market mispricing is a bug in our parsing until proven otherwise.
- Compute quota is shared and finite — treat it as a budget.
- Size to book depth, never to ambition: total deployable across both strategies is realistically
  a few thousand dollars, so this is a hundreds-per-month system unless the universe widens.

## Recent hardening (2026-07-19 commits)
`b1f1a75` estimator · `8d3799f` sports/esports excluded · `99bf35b`+`de49134`+`b1abc18` (LOW)
direction + NPM payload · `d922fc5` dead-book cache · `a17d82f` Bing feed redundancy ·
`09acc2f` negRisk arb filter.
