import type { BookQuote, CurvePoint, StrategyConfig, ValuationCandidate } from "./signalTypes.ts";
import { allocateCandidate } from "./candidateAllocator.ts";

export function calendarDominanceCandidates(
  points: CurvePoint[],
  quotes: Map<string, BookQuote>,
  config: StrategyConfig,
): ValuationCandidate[] {
  const grouped = groupBy(points, (point) => `${point.leg.company}\u0000${point.leg.threshold ?? "na"}`);
  const candidates: ValuationCandidate[] = [];
  for (const group of grouped.values()) {
    const sorted = [...group].sort((left, right) => Date.parse(left.leg.deadlineIso) - Date.parse(right.leg.deadlineIso));
    for (let i = 1; i < sorted.length; i += 1) {
      const earlier = sorted[i - 1];
      const later = sorted[i];
      if (!earlier || !later || earlier.leg.threshold === undefined || later.leg.threshold === undefined) continue;
      const violation = earlier.yesAsk - later.yesAsk;
      if (violation < config.minimumEdge.calendar) continue;
      const quote = quotes.get(later.leg.marketSlug);
      if (!quote) continue;
      candidates.push(allocateCandidate({
        signalType: "CALENDAR_DOMINANCE_YES",
        status: "candidate",
        company: later.leg.company,
        eventSlug: later.leg.eventSlug,
        marketSlug: later.leg.marketSlug,
        deadline: later.leg.deadlineIso,
        threshold: later.leg.threshold,
        yesTokenId: later.leg.yesTokenId,
        yesAsk: quote.bestAsk,
        bestBid: quote.bestBid,
        spread: quote.spread,
        liquidity: quote.liquidity,
        fairPrice: Math.min(0.99, earlier.yesAsk),
        edge: violation,
        confidence: 9,
        reason: `deadline_dominance_violation:earlier_${earlier.leg.deadlineIso}_ask_${earlier.yesAsk}_gt_later_${later.yesAsk}`,
        ruleHash: later.leg.ruleHash,
        pairedMarketSlug: earlier.leg.marketSlug,
        pairedYesAsk: earlier.yesAsk,
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
