import type { BookQuote, NpmEvidence, StrategyConfig, ValuationCandidate, ValuationLeg } from "./signalTypes.ts";
import type { ImpliedCurve } from "./impliedCurve.ts";
import { allocateCandidate } from "./candidateAllocator.ts";

export function rankingAlertCandidates(
  rankingLegs: ValuationLeg[],
  evidenceByCompany: Map<string, NpmEvidence>,
  quotes: Map<string, BookQuote>,
  config: StrategyConfig,
  curves: ImpliedCurve[] = [],
): ValuationCandidate[] {
  const valuationByCompany = new Map<string, number>();
  for (const evidence of evidenceByCompany.values()) {
    valuationByCompany.set(evidence.company, evidence.maxEligibleValuation ?? evidence.latestValuation);
  }
  for (const curve of curves) {
    if (curve.expectedValuation !== undefined) valuationByCompany.set(curve.company, curve.expectedValuation);
  }
  const ranked = [...valuationByCompany.entries()].sort((left, right) => right[1] - left[1]);
  const rankByCompany = new Map(ranked.map(([company], index) => [company, index + 1]));
  const candidates: ValuationCandidate[] = [];
  for (const leg of rankingLegs) {
    const quote = quotes.get(leg.marketSlug);
    const rank = rankByCompany.get(leg.company);
    if (!quote || quote.bestAsk === null || rank === undefined || !leg.ranking) continue;
    const expectedWinner = rank === leg.ranking;
    const fairPrice = expectedWinner ? 0.85 : 0.05;
    const contradiction = expectedWinner ? fairPrice - quote.bestAsk : quote.bestAsk - fairPrice;
    if (contradiction < 0.25) continue;
    candidates.push(allocateCandidate({
      signalType: "RANKING_INCONSISTENCY_ALERT",
      status: "alert",
      company: leg.company,
      eventSlug: leg.eventSlug,
      marketSlug: leg.marketSlug,
      deadline: leg.deadlineIso,
      yesTokenId: leg.yesTokenId,
      sourceValuation: evidenceByCompany.get(leg.company)?.latestValuation,
      sourceDate: evidenceByCompany.get(leg.company)?.latestTapeDate,
      maxEligibleValuation: evidenceByCompany.get(leg.company)?.maxEligibleValuation,
      maxEligibleDate: evidenceByCompany.get(leg.company)?.maxEligibleDate,
      yesAsk: quote.bestAsk,
      bestBid: quote.bestBid,
      spread: quote.spread,
      liquidity: quote.liquidity,
      fairPrice,
      edge: expectedWinner ? fairPrice - quote.bestAsk : 0,
      confidence: 6,
      reason: `ranking_market_inconsistent_with_curve_or_npm_rank:known_rank_${rank}_target_rank_${leg.ranking}`,
      ruleHash: leg.ruleHash,
    }, config));
  }
  return candidates;
}
