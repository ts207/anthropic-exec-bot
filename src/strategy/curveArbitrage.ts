import type { BookQuote, CurvePoint, StrategyConfig, ValuationCandidate } from "./signalTypes.ts";
import { allocateCandidate } from "./candidateAllocator.ts";

export function curveMonotonicityCandidates(
  points: CurvePoint[],
  quotes: Map<string, BookQuote>,
  config: StrategyConfig,
): ValuationCandidate[] {
  const grouped = groupBy(points, (point) => `${point.leg.company}\u0000${point.leg.deadlineIso}`);
  const candidates: ValuationCandidate[] = [];
  for (const group of grouped.values()) {
    const sorted = [...group]
      .filter((point) => point.leg.threshold !== undefined)
      .sort((left, right) => (left.leg.threshold ?? 0) - (right.leg.threshold ?? 0));
    for (let i = 1; i < sorted.length; i += 1) {
      const lower = sorted[i - 1];
      const higher = sorted[i];
      if (!lower || !higher || lower.leg.threshold === undefined || higher.leg.threshold === undefined) continue;
      const violation = higher.yesAsk - lower.yesAsk;
      if (violation < config.minimumEdge.curve) continue;
      const quote = quotes.get(lower.leg.marketSlug);
      if (!quote) continue;
      candidates.push(allocateCandidate({
        signalType: "CURVE_MONOTONICITY_YES",
        status: "candidate",
        company: lower.leg.company,
        eventSlug: lower.leg.eventSlug,
        marketSlug: lower.leg.marketSlug,
        deadline: lower.leg.deadlineIso,
        threshold: lower.leg.threshold,
        yesTokenId: lower.leg.yesTokenId,
        yesAsk: quote.bestAsk,
        bestBid: quote.bestBid,
        spread: quote.spread,
        liquidity: quote.liquidity,
        fairPrice: Math.min(0.99, higher.yesAsk),
        edge: violation,
        confidence: 9,
        reason: `hard_monotonicity_violation:higher_threshold_${higher.leg.threshold}_ask_${higher.yesAsk}_gt_lower_${lower.yesAsk}`,
        ruleHash: lower.leg.ruleHash,
        pairedMarketSlug: higher.leg.marketSlug,
        pairedYesAsk: higher.yesAsk,
      }, config));
    }
  }
  return candidates;
}

function groupBy<T>(items: T[], keyFn: (item: T) => string): Map<string, T[]> {
  const groups = new Map<string, T[]>();
  for (const item of items) groups.set(keyFn(item), [...(groups.get(keyFn(item)) ?? []), item]);
  return groups;
}
