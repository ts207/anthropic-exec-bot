# US–Iran Announcement Ladders — Analysis & Plan

_Written 2026-07-20. Supersedes nothing in PLAN.md; this is the focused theater bet._

## Thesis in one line

Concentrate the fleet on **US–Iran announcement markets structured as date ladders**, where a
single tier-one wire confirmation resolves several legs at once, the near-dated legs carry the
largest payoff multiples, and IMF PortWatch's 8-day publication lag is irrelevant because
nothing resolves on data.

## Why this theater, why now

- **Regime shift is live.** Hormuz transits collapsed from 26–40/day (late June) to 9–15/day
  (Jul 8–12). An active US naval blockade, a July 13 Trump reinstatement announcement, and
  US-led clearance operations are all in play. Announcements are pending, not hypothetical.
- **20 Iran-family markets** already discovered and graded, 8 of them announcement-resolved.
- **Wire monitoring is our fastest capability** and announcements are exactly what it is for.
  Data lag doesn't bind; classifier precision does.

## The structural opportunity: date ladders

Both key markets are cumulative "by DATE" ladders. **One announcement resolves every leg whose
deadline is on/after the announcement date, simultaneously.**

### "US announces halt in Iran offensive operations by…?" (deadline Aug 31)

| Leg | Bid | Ask | Implied P | Payoff if it fires |
|---|---|---|---|---|
| Jul 21 | 0.06 | 0.08 | ~7% | **12.5×** |
| Jul 24 | 0.13 | 0.14 | ~13.5% | 7.1× |
| Jul 31 | 0.24 | 0.27 | ~25.5% | 3.7× |
| Aug 15 | 0.32 | 0.44 | ~38% | 2.3× |
| Aug 31 | 0.54 | 0.56 | ~55% | 1.8× |

### "US announces end of Iranian blockade by…?" (deadline Aug 31)

| Leg | Bid | Ask | Implied P | Payoff if it fires |
|---|---|---|---|---|
| Jul 24 | 0.04 | 0.045 | ~4.2% | **22×** |
| Jul 31 | 0.11 | 0.14 | ~12.5% | 7.1× |
| Aug 15 | 0.19 | 0.21 | ~20% | 4.8× |
| Aug 31 | 0.37 | 0.39 | ~38% | 2.6× |

Both ladders are monotone and internally coherent — **no arbitrage**. The market prices
halt-in-offensive ~1.5–2× more likely than blockade-end at every horizon, which is sensible
(halting operations is a lesser step than lifting a blockade). We are not smarter than this
curve; the edge is purely **reaction speed to the announcement**, not superior forecasting.

### Why near-dated legs are the play
If an announcement lands on, say, July 25, the Jul 31 blockade leg bought at 0.14 pays 1.00
(7.1×) while the Aug 31 leg pays 2.6×. The nearest leg that still contains the announcement
date is always the highest-return expression of the same single event.

## Rule quality (the bot's own grades)

| Market | Clarity | Observability | Res. risk | Automation | State |
|---|---|---|---|---|---|
| Who will sign US×Iran deal | 0.83 | 0.90 | 0.30 | 0.72 | **LIVE_CONFIRMATION_ELIGIBLE** |
| US announces end of blockade | 0.78 | 0.90 | 0.30 | 0.62 | MONITOR_ONLY (discretionary) |
| US announces halt in offensive | 0.72 | 0.90 | 0.40 | 0.55 | MONITOR_ONLY (discretionary) |
| US obtains Iranian uranium | 0.74 | 0.82 | 0.42 | 0.55 | MONITOR_ONLY |
| US invades Iran before 2027 | 0.62 | 0.93 | 0.52 | 0.42 | MONITOR_ONLY |
| Regime falls (Sep 30 / 2027) | 0.55–0.60 | 0.80–0.85 | 0.45–0.50 | 0.35 | MONITOR_ONLY |
| Iran leadership change | 0.45 | 0.60 | **0.72** | 0.35 | MONITOR_ONLY |
| US charges Hormuz fees | 0.72 | **0.45** | 0.55 | 0.35 | MONITOR_ONLY |

**Trade the top block. Never the bottom block** — "leadership change" (risk 0.72) and "Hormuz
fees" (observability 0.45) are exactly the markets where a confident classifier loses money.

## THE BLOCKER (highest-priority finding)

The two best announcement ladders are `MONITOR_ONLY` for one reason: `discretionary=True`.
That flag is a **hard gate before the paper thresholds are even evaluated** (scorer.py:80).

The flag is *correctly* set — the blockade rule qualifies an announcement that "**generally**"
ceases blocking vessel traffic, which is a judgment word. But the consequence is perverse:

- These markets pass every paper threshold (clarity 0.78 vs 0.55 required; observability 0.90
  vs 0.50 required).
- Excluding them from **paper** means we generate **zero evidence** about whether the
  classifier can read them — which is the entire purpose of the soak.
- We therefore can never learn our way to trading the best-observability markets in the theater.

### Proposed change (safety-preserving)
Allow discretionary markets into **PAPER_ELIGIBLE only**, behind a new
`scoring.allow_discretionary_paper` flag (default false, enabled in this config). The
discretionary → **never live** rule stays absolute, exactly like the existing
`allow_fixture_analysis_live` pattern at scorer.py:107 which already demotes to paper rather
than excluding. Paper cannot lose money; the misfire corpus is the deliverable.

## Execution reality check

- **Latency**: publish→fetch p50 is ~38 min, dominated by RSS aggregation. For a *scheduled*
  data print that's fatal; for an unscheduled announcement it is survivable — the market also
  takes minutes to reprice a surprise — but it is the single biggest threat to this thesis.
  Priority: direct wire feeds (Reuters/AP/AFP), plus `armed_poll_seconds` tightening on these
  specific bots.
- **Fee drag**: verify per-market `feesEnabled` before sizing. Some Polymarket families charge
  ~4% taker; at 7× payoffs that is noise, at 1.8× (Aug 31 legs) it materially bites.
- **Liquidity**: ladder legs hold roughly $30–60k liquidity each — far beyond our $50/order
  caps. Depth is not a constraint here; our own risk limits are.
- **Spread**: Aug 15 halt leg is 0.32/0.44 — a 12¢ spread. Never cross it; that leg is
  maker-only or skip.

## Risks, honestly

1. **Classifier misfire on judgment language.** "Suspension" vs "pause" vs "temporary halt" —
   the rule requires a *general* cessation. A partial/conditional announcement that the
   classifier reads as qualifying is the main loss mode. Mitigation: two-pass agreement +
   verbatim-quote requirement stay ON for this family; paper first, always.
2. **The market front-runs us.** These are watched markets ($1.1M volume on the Hormuz event).
   Our 38-min lag may mean we buy after repricing. The book snapshots at gate-escalation will
   measure exactly how much edge remained — that is the go/no-go evidence.
3. **Correlated cluster.** All 8 markets fire on related news. `max_markets_per_correlation_group`
   (currently 2) must stay enforced or one wrong classification hits multiple positions.
4. **Deadline decay is real.** Near-dated legs are cheap *because* they are probably wrong.
   Buying the 22× Jul 24 blockade leg is a lottery ticket unless an announcement is actually
   in the news; only confirmation entries, never speculative ones.

## What is NOT the play

- **No arbitrage.** Both ladders are coherently priced; I checked. The Hormuz transit-count
  ladder is also correctly priced — the apparent "already resolved" 6–13× edge there was a
  false positive from reading event-level dates against per-leg windows (each threshold leg is
  relisted with its own start date). Fourth phantom edge caught this weekend.
- **No forecasting alpha.** We do not know better than the crowd whether a blockade ends. We
  might know *sooner*. That is the whole bet.
- **Not the low-quality markets.** Regime-fall and leadership-change are drama markets with
  poor rule clarity; they are where confident classifiers go to die.

## Plan

### Step 1 — Unblock paper evidence (do first)
Add `scoring.allow_discretionary_paper`, enable it for this config. Discretionary markets
become PAPER_ELIGIBLE, never live. Expected result: blockade-end and halt-in-offensive ladders
get fleet bots and start generating classifier decisions on real news.

### Step 2 — Prioritize the theater
Move the Iran family to the front of the estimator queue and ensure per-market bots exist for
the announcement ladders. Confirm each ladder's source plan carries tier-one auto-trade domains
(reuters.com, apnews.com, afp.com, whitehouse.gov, defense.gov, state.gov) and the right
escalate terms ("blockade", "lift", "suspend", "halt", "cease", "ceasefire", "agreement").

### Step 3 — Attack latency for this family only
Direct wire feeds ahead of aggregators; tighten `armed_poll_seconds` on the Iran bots. Measure
with `make latency` — target publish→fetch p50 under 5 minutes for these specific sources.

### Step 4 — Measure, do not trade
Two weeks of paper on this family produces the three numbers that decide everything:
gate escalations that reached confirm-grade, **edge remaining at signal time** (from
gate-escalation book snapshots), and misfire count. Nothing goes live before those exist.

### Step 5 — Go/no-go (~Aug 2, and again at the Aug 31 ladder expiries)
Arm live only if: escalations actually occurred, book snapshots show edge surviving our
latency, and zero classifier misfires on the judgment-language cases. Otherwise the failure
points at a specific fix (feeds, prompts, or universe), not at abandoning the thesis.

## Standing rules for this theater
- Confirmation entries only. Never buy a cheap far-leg on speculation.
- Two-pass agreement + verbatim quote stay ON for discretionary-language markets.
- Discretionary ⇒ paper only, forever, regardless of how good the numbers look.
- Any "already resolved" signal must be validated against the **per-leg** creation date.
