# Autonomous Geopolitics Entry/Exit Hardening

This branch implements the first live-capable scope: autonomous entry only
after a qualifying geopolitical outcome is confirmed, followed by continuous
protection, repeated rotation when allowed, and final exit.

Anticipatory geopolitical forecasting is included as a technically paper-only
research layer. The LLM extracts structured facts; deterministic policy applies
market rules, source and evidence likelihoods, normalized probability updates,
edge requirements, and simulated entry/exit state. Only the separate confirmed
entry path can reach a trading adapter mutation.

## Entry formula

An entry must satisfy:

```text
confirmed_probability
  - executable_yes_ask
  - slippage_buffer
  - resolution_risk_buffer
  >= min_edge
```

The submitted USD amount is the lower of `entry.usd_budget` and the global
`POLYBOT_PER_ORDER_NOTIONAL` guardrail. `POLYBOT_MAX_ENTRY_PRICE` can only lower
the configured price cap. Rotation buys now pass through the same persistent
per-order, per-market, and daily budget state; confirmed sale proceeds remain
an additional upper bound.

## Runtime safety

- The live wallet is authoritative. Every configured outcome balance and open
  order is reconciled at preflight and every cycle.
- Exactly zero or one meaningful YES balance is accepted. Multiple balances or
  any unexpected NO balance or resting order halt mutations.
- Atomic `holdings.json` records cache the reconciled active outcome.
- Every mutation has a unique file under `execution_journal/`.
- Protection attempts are counted by unique execution ID.
- A per-data-directory process lock rejects duplicate bot instances.
- Any positive entry fill is adopted. Small fills become
  `PARTIALLY_ENTERED` and remain protected.
- `ROTATED` is not terminal; the new outcome continues through the same defense
  lifecycle.
- Feed summaries can alert but cannot autonomously open a position.
- Entry and rotation buys reject unavailable or over-limit spreads.
- Consecutive execution exceptions feed the shared halt state; successful
  reconciled mutations reset the consecutive-failure count.

## Safe activation sequence

1. Copy `configs/geopolitics/location-entry.example.yaml` to an event-specific
   config and fill every outcome from the live grouped market.
2. Configure full-text sources and discovery feeds.
3. Keep `execution.dry_run: true` and run tests plus classifier smokes.
4. Run `inspect-location`, review all outcomes and pin the rule-text SHA-256.
5. Run the live preflight and resolve every wallet, open-order, credential,
   market, source, and notification blocker.
6. Change `execution.dry_run` to false, acknowledge that exact config hash, and
   leave the operator position mode at `alert_only` for a monitoring soak.
7. Set the position mode to `live` only after reviewing the soak logs.

The existing `qatar-sept30-yes-protection.yaml` is intentionally not converted
to flat autonomous entry because it represents a currently held position. Use
a separate data directory for every autonomous strategy config.

## Forecast promotion evidence

Do not add a live forecast execution path until the paper ledger demonstrates:

- enough independent observations across more than one event;
- positive realized and mark-to-market EV after buffers;
- calibration by probability band;
- no dependence on duplicated/syndicated reports;
- stable results under stricter source likelihoods and wider cost buffers;
- acceptable drawdown and concentration.

Forecast state is versioned by model and configuration fingerprint. A changed
model/config archives incompatible paper state and starts from the declared
priors, preventing silent reuse of probabilities from a different experiment.
Use `evaluate-location-forecast` after resolution for Brier and log-loss
reporting; one event's sequential observations remain correlated, so promotion
requires aggregation across held-out events.
