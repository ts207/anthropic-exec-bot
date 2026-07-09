import { join } from "node:path";
import { appendJsonl } from "../logging.ts";
import { fetchGammaEvent, parseValuationLegs } from "../strategy/marketParser.ts";
import { fetchNpmEvidence, withEligibleMax } from "../strategy/npmValuationSource.ts";
import { fetchBookQuote } from "../strategy/orderbookSource.ts";
import { isCandidateLocked } from "../strategy/stateStore.ts";
import { decideThresholdLeg } from "../strategy/valuationDecision.ts";
import type { BookQuote, CurvePoint, NpmEvidence, StrategyConfig, ValuationCandidate, ValuationLeg } from "../strategy/signalTypes.ts";

export type ValuationState = {
  evidenceByCompany: Map<string, NpmEvidence>;
  allLegs: ValuationLeg[];
  quotes: Map<string, BookQuote>;
  noQuotes: Map<string, BookQuote>;
  curvePoints: CurvePoint[];
  thresholdCandidates: ValuationCandidate[];
};

export async function collectValuationState(config: StrategyConfig): Promise<ValuationState> {
  const evidenceByCompany = await loadEvidence(config);
  const allLegs: ValuationLeg[] = [];
  const quotes = new Map<string, BookQuote>();
  const noQuotes = new Map<string, BookQuote>();
  const curvePoints: CurvePoint[] = [];
  const thresholdCandidates: ValuationCandidate[] = [];

  for (const eventConfig of config.events) {
    const event = await fetchGammaEvent(eventConfig.slug);
    await appendJsonl(join(config.logsDir, "events.jsonl"), {
      eventSlug: event.slug,
      title: event.title,
      rawHash: event.rawHash,
      kind: eventConfig.kind,
    });
    const legs = parseValuationLegs(event, eventConfig);
    allLegs.push(...legs);
    for (const leg of legs) {
      await appendJsonl(join(config.logsDir, "legs.jsonl"), leg);
      const quote = leg.yesTokenId ? await safeQuote(leg.yesTokenId) : undefined;
      if (quote) quotes.set(leg.marketSlug, quote);
      const noQuote = leg.noTokenId ? await safeQuote(leg.noTokenId) : undefined;
      if (noQuote) noQuotes.set(leg.marketSlug, noQuote);
      if (leg.threshold !== undefined && quote?.bestAsk !== null && quote?.bestAsk !== undefined) {
        curvePoints.push({ leg, yesAsk: quote.bestAsk });
      }
      if (leg.eventKind === "threshold") {
        const rawEvidence = evidenceByCompany.get(leg.company);
        const evidence = rawEvidence ? withEligibleMax(rawEvidence, leg.marketWindowStartIso, leg.deadlineIso) : undefined;
        const locked = await isCandidateLocked(config, candidateShell(leg));
        thresholdCandidates.push(decideThresholdLeg(leg, evidence, quote, config, locked));
      }
    }
  }

  return { evidenceByCompany, allLegs, quotes, noQuotes, curvePoints, thresholdCandidates };
}

export function candidateShell(leg: ValuationLeg): ValuationCandidate {
  return {
    signalType: "NO_ACTION",
    status: "skip",
    company: leg.company,
    eventSlug: leg.eventSlug,
    marketSlug: leg.marketSlug,
    deadline: leg.deadlineIso,
    threshold: leg.threshold,
    yesTokenId: leg.yesTokenId,
    yesAsk: null,
    bestBid: null,
    spread: null,
    liquidity: 0,
    fairPrice: 0,
    edge: 0,
    confidence: 0,
    confidenceScore: 0,
    edgeScore: 0,
    maxPrice: 0,
    orderUsd: 0,
    liveAllowed: false,
    reason: "lock_probe",
    ruleHash: leg.ruleHash,
  };
}

async function loadEvidence(config: StrategyConfig): Promise<Map<string, NpmEvidence>> {
  const evidenceByCompany = new Map<string, NpmEvidence>();
  for (const company of config.companies) {
    if (!company.npmCompanyId) continue;
    const evidence = await fetchNpmEvidence(company);
    if (!evidence) continue;
    evidenceByCompany.set(company.name, evidence);
    await appendJsonl(join(config.logsDir, "evidence.jsonl"), evidence);
  }
  return evidenceByCompany;
}

async function safeQuote(tokenId: string): Promise<BookQuote | undefined> {
  try {
    return await fetchBookQuote(tokenId);
  } catch {
    return undefined;
  }
}
