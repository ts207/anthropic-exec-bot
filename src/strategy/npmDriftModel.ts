import type { NpmEvidence, ValuationLeg, ValuationCandidate, BookQuote, StrategyConfig } from "./signalTypes.ts";
import { allocateCandidate } from "./candidateAllocator.ts";

export function driftCandidate(
  leg: ValuationLeg,
  evidence: NpmEvidence,
  quote: BookQuote,
  config: StrategyConfig,
): ValuationCandidate | null {
  if (leg.threshold === undefined || quote.bestAsk === null) return null;
  const distancePct = Math.abs(evidence.latestValuation - leg.threshold) / leg.threshold;
  if (distancePct > 0.015) return null;
  const drift = recentDailyDrift(evidence);
  if (drift <= 0) return null;
  const daysLeft = Math.max(0, (Date.parse(leg.deadlineIso) - Date.parse(`${evidence.latestTapeDate}T00:00:00Z`)) / 86_400_000);
  const projected = evidence.latestValuation * Math.pow(1 + drift, Math.min(daysLeft, 14));
  const gap = (leg.threshold - evidence.latestValuation) / leg.threshold;
  const fairPrice = Math.max(0.02, Math.min(0.95, 0.45 + (drift * 25) - (gap * 12)));
  const edge = fairPrice - quote.bestAsk;
  if (projected < leg.threshold || edge < config.minimumEdge.drift) return null;
  return allocateCandidate({
    signalType: "NPM_DRIFT_MODEL_YES",
    status: "alert",
    company: leg.company,
    eventSlug: leg.eventSlug,
    marketSlug: leg.marketSlug,
    deadline: leg.deadlineIso,
    threshold: leg.threshold,
    yesTokenId: leg.yesTokenId,
    sourceValuation: evidence.latestValuation,
    sourceDate: evidence.latestTapeDate,
    maxEligibleValuation: evidence.maxEligibleValuation,
    maxEligibleDate: evidence.maxEligibleDate,
    yesAsk: quote.bestAsk,
    bestBid: quote.bestBid,
    spread: quote.spread,
    liquidity: quote.liquidity,
    fairPrice,
    edge,
    confidence: 6.5,
    reason: `near_boundary_positive_npm_drift:${(drift * 100).toFixed(3)}pct_daily`,
    ruleHash: leg.ruleHash,
  }, config);
}

export function recentDailyDrift(evidence: NpmEvidence, lookback = 5): number {
  const tape = evidence.tape.slice(-lookback - 1);
  if (tape.length < 2) return 0;
  const first = tape[0];
  const last = tape.at(-1);
  if (!first || !last || first.impliedValuation <= 0) return 0;
  const days = Math.max(1, (Date.parse(`${last.date}T00:00:00Z`) - Date.parse(`${first.date}T00:00:00Z`)) / 86_400_000);
  return Math.pow(last.impliedValuation / first.impliedValuation, 1 / days) - 1;
}
