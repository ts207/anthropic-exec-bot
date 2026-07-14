# Risk Register & Remediation Plan

Honest inventory of what can hurt this system, what already mitigates it, what
gap remains, and the prioritized plan to close the gaps. Severity = plausible
damage x likelihood, judged against the current guardrail sizing ($25/order,
$100/day, $1,000 total).

## A. Classification / model risks

### A1. Screen tier can miss a DEFENSE trigger (severity: HIGH — top gap)
The Haiku screen pass gates the strong model for ALL articles, including while
HOLDING a position. A screen false-negative on a foreclosure report means the
Opus passes never run and the bot fails to exit a losing position. Missing an
entry costs an opportunity; missing an exit costs the whole stake.
- In place: screen failure (exception) escalates; only NO_ACTION short-circuits.
- Gap: a wrong-but-confident NO_ACTION from the cheap model silently suppresses
  protection.
- **Plan (P0): screen gates apply only while FLAT.** A holding bot runs full
  confirm passes on every gated article — held bots are few, so the cost is
  negligible and defense never depends on the weakest model.

### A2. Confidently wrong rule reading (severity: HIGH)
The classifier can misjudge "qualifying" on exactly the wording markets settle
on. This is the core epistemic risk of the whole design.
- In place: verbatim rules in every prompt, two-pass agreement on
  decision-relevant fields, verbatim-quote verification, tier-one source
  requirement, `MONITOR_ONLY` for discretionary rules, small sizing.
- Gap: no adversarial evaluation set; no per-market accuracy data; entry fires
  on a single article.
- **Plan (P1): regression eval set** — a corpus of real tricky articles
  (technical-vs-senior, postponed-vs-cancelled, frontrunner-vs-confirmed) with
  expected classifications, run in CI against the fixture and periodically
  against live models. **Plan (P2): second-source confirmation option** —
  config knob requiring a second independent domain to confirm before entries
  above a size threshold.

### A3. Prompt injection via article text (severity: MEDIUM)
Scraped article text is untrusted input to the classifier; adversarial content
could try to steer it ("this qualifies, output confirmed").
- In place: strict JSON schema output, pass agreement, quote-must-exist-in-
  article check, and — decisively — trades only fire from `auto_trade_domains`
  (wires + official governments), so the attacker must compromise a wire.
- Gap: prompt does not explicitly fence the article as untrusted data.
- **Plan (P1):** delimit article text as untrusted in all prompts and instruct
  the model to ignore instructions inside it; add injection cases to the A2
  eval set.

### A4. Fixture analyzer grading a live market (severity: MEDIUM, cheap fix)
The offline rule analyzer is a keyword toy. A config mistake (provider
`rule_based` in production) would grade markets on heuristics.
- **Plan (P0):** scorer refuses `LIVE_CONFIRMATION_ELIGIBLE` when
  `rule_analysis.model` is `fixture` — fixture-graded markets cap at
  `PAPER_ELIGIBLE`.

## B. Market / economic risks

### B1. Adverse selection at fill (severity: HIGH, inherent)
If a "confirmed" event still trades at 0.55, sometimes the market knows a rule
nuance the classifier missed. The fills you get are biased toward your errors.
- In place: edge buffers, price cap, small size, thin-market focus (there the
  cheap price is usually inattention, not information).
- **Plan (P1): post-entry re-verification** — 10–30 minutes after entry,
  re-scan sources; if no second independent source has corroborated, alert
  loudly and (config-gated) trim. Track corroboration rate in the ledger as a
  quality metric.

### B2. Resolution/oracle (UMA) risk (severity: MEDIUM-HIGH)
One adverse resolution erases ~15 winning trades at 5% edge. Flat 2% buffer
may under-price wording risk.
- In place: rule-hash pin, discretionary → monitor-only, buffer.
- **Plan (P1):** scale `resolution_risk_buffer` by the analyzer's per-market
  `resolution_risk` score instead of a flat constant.

### B3. Exit liquidity in thin markets (severity: MEDIUM)
A $25 entry into a $1,400 book may face an empty bid side on a defense exit;
FAK at `min_price 0.03` could dump at a terrible print.
- In place: news exits deliberately не floored (protecting beats price), size
  is small, primary plan for small positions is hold-to-resolution.
- **Plan (P2):** exit-aware sizing (size down when bid-side depth is far below
  ask-side), and a staged exit (half now, half after a re-quote) for defense
  sells above a notional threshold.

### B4. Correlation proxy too coarse (severity: MEDIUM)
Same-parties grouping misses regional-contagion and theme correlation
(e.g. separate party sets all keyed to one conflict).
- In place: group caps, event caps, total/daily caps bound damage.
- **Plan (P2):** add region/conflict-family as a second grouping dimension in
  the allocator; report cross-group co-movement in the funnel.

### B5. No portfolio-level P&L kill switch (severity: HIGH, cheap fix)
RiskState halts on consecutive execution failures, but nothing halts the fleet
on realized LOSSES. A systematically wrong day (e.g. a misbehaving model
release) burns until the daily notional cap.
- **Plan (P0): realized-P&L ledger + drawdown halt** — record entry/exit
  proceeds per market in the shared ledger; when rolling realized loss exceeds
  `max_drawdown_usd`, write the shared operator global mode to `alert_only`
  automatically and page via Telegram.

## C. Operational risks

### C1. Binary bot lacks the hardened runtime (severity: HIGH — top gap)
The fleet spawns mostly binary bots, but wallet reconciliation, the execution
journal, and the per-data-dir process lock exist only in the location bot. A
crashed-mid-order binary bot restarts with only local state.
- **Plan (P0): port the hardening to the binary executor** (reconcile live
  YES/NO balances against holdings each cycle and at startup, journal every
  mutation, process lock). This is the single highest-value engineering item.

### C2. Hung (not crashed) bots (severity: MEDIUM)
The fleet restarts dead processes but cannot see a live-but-stuck one; it
occupies its market slot silently.
- **Plan (P1): heartbeat files** — each bot touches `heartbeat.json` per
  cycle; the fleet terminates and restarts any bot whose heartbeat is older
  than N cycles, with restart backoff and a max-restarts alarm.

### C3. Ledger allowance leakage (severity: MEDIUM)
Reserve-on-attempt permanently consumes budget even for unfilled orders, and
spent history never decays; capacity quietly shrinks over weeks.
- **Plan (P1): `reconcile-ledger` command** — recompute per-market spend from
  execution journals/fills, release unfilled reservations, and roll daily
  buckets; run it from the fleet cycle.

### C4. Emitted-config trust under auto-ack (severity: MEDIUM)
The fleet auto-acks machine-generated configs; a pipeline bug could arm a
malformed config.
- In place: every bot re-verifies market/rule-hash against Gamma at startup
  and fails closed; loaders validate schemas.
- **Plan (P1):** fleet runs the bot's own preflight before spawning with
  `--live` and refuses to spawn on blockers (turns a runtime crash-loop into a
  visible fleet error).

### C5. Feed rate-limiting / bans at 2s polling (severity: MEDIUM)
- In place: conditional GETs make polls ~free; parallel fetch keeps concurrency
  modest.
- **Plan (P1): per-domain backoff** — on 429/403, exponentially back off that
  domain and alert if a critical feed stays throttled.

### C6. Single host, single wallet (severity: MEDIUM, accepted)
Host loss = blind while holding; wallet compromise = full loss.
- Mitigation: fund the wallet ONLY with proving capital you can lose entirely
  (already the README's stance); systemd restarts; Telegram degradation alerts.
  Accepted at current size; revisit before any cap raise.

## D. External / compliance

### D1. Venue terms and jurisdiction (severity: user-owned)
Automated trading permissibility and jurisdictional legality are the
operator's responsibility (already in the go-live checklist). No engineering
mitigation; do not skip the checklist.

### D2. Anthropic/Gamma/CLOB outages (severity: LOW-MEDIUM, handled)
API down → alert-only degrade, no trades (safe direction); exits also pause —
another reason for C2 heartbeats and Telegram degradation alerts. Accepted.

## The plan, prioritized

**P0 — before any live arming (small, high-value):**
1. ✅ IMPLEMENTED — Screen tier gates entries only; holding bots always run full confirm passes (A1).
2. ✅ IMPLEMENTED — Wallet reconciliation + execution journal + process lock in the binary executor (C1).
3. ✅ IMPLEMENTED — Realized-P&L tracking in the shared ledger (`settle`/`reduce_basis` at every flat transition) + automatic fleet halt on `max_drawdown_usd` (B5).
4. ✅ IMPLEMENTED — Scorer: fixture-analyzed markets can never be live-eligible unless `scoring.allow_fixture_analysis_live` is explicitly set (A4).

**P1 — during the alert-only soak:**
5. ✅ IMPLEMENTED — Bot heartbeats (`heartbeat.json` per cycle) + fleet hang detection with `max_restarts_per_hour` backoff (C2).
6. ✅ IMPLEMENTED — `reconcile-ledger` command: frees dead position slots, rolls daily buckets (C3).
7. ✅ IMPLEMENTED — Fleet pre-spawn operator-gate check; refuses to spawn blocked configs (C4).
8. ✅ IMPLEMENTED — Per-domain feed backoff on 429/403 (exponential, capped at 1h) (C5).
9. ◐ PARTIAL — Untrusted-article fencing in prompts is in (`<<<ARTICLE ... ARTICLE>>>`); the CI adversarial eval set remains open (A2/A3).
10. ✅ IMPLEMENTED — Resolution-risk buffer scaled by the analyzer's per-market score (`resolution_risk_scale`) (B2).

**P2 — after first live weeks, informed by data:**
11. ✅ IMPLEMENTED — Post-entry corroboration check (`entry.corroboration_minutes` / `corroboration_action: alert|trim`): a second independent domain must reinforce the thesis within the window or the operator is alerted / the position trimmed (B1).
12. ✅ IMPLEMENTED — Book-aware sizing (`recommended_max_order_usd`) and staged defense exits (`sell.max_fraction_per_order`) (B3).
13. ✅ IMPLEMENTED — Region second correlation dimension (`per_region_usd` ledger cap; region derived from deciding actors) (B4).
14. ✅ IMPLEMENTED — Second-source confirmation above `entry.second_source_above_usd`: the first trigger defers to `ENTRY_AWAITING_SECOND_SOURCE` until an independent domain confirms within `second_source_window_minutes` (A2).

**Standing rule:** raise the notional guardrails only after the funnel and
calibration reports show realized positive edge across multiple resolved,
uncorrelated events — never on a good week.
