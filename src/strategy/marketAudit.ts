import type { BookQuote, CurvePoint, NpmEvidence, StrategyConfig, ValuationLeg } from "./signalTypes.ts";

export type MarketState =
  | "NEWLY_CROSSED"
  | "PREVIOUSLY_CROSSED"
  | "NEAR_BOUNDARY"
  | "UNCROSSED"
  | "FAR_ABOVE"
  | "AMBIGUOUS";

export type CrossedLegQuality =
  | "SOURCE_CONFIRMED_AND_STALE"
  | "SOURCE_CONFIRMED_BUT_ALREADY_PRICED"
  | "SOURCE_CONFIRMED_BUT_LOW_LIQUIDITY"
  | "SOURCE_CONFIRMED_BUT_RULE_AMBIGUOUS"
  | "SOURCE_CONFIRMED_BUT_SOURCE_DATE_RISK"
  | "NOT_SOURCE_CONFIRMED";

export type ViolationTier =
  | "HARD_CROSS_MARKET_BID_VIOLATION"
  | "SOFT_MID_VIOLATION"
  | "SOFT_ASK_ONLY_VIOLATION"
  | "STALE_BOOK_VIOLATION"
  | "RULE_MISMATCH_REJECTED"
  | "LOW_CONFIDENCE_LABEL_MISMATCH";

export type MarketAuditRow = {
  company: string;
  eventSlug: string;
  marketSlug: string;
  threshold?: number;
  deadline: string;
  label?: string;
  state: MarketState;
  crossedQuality: CrossedLegQuality;
  latestValuation?: number;
  latestDate?: string;
  maxEligibleValuation?: number;
  maxEligibleDate?: string;
  previousMaxEligibleValuation?: number;
  sourceDateAgeHours?: number;
  yesAsk: number | null;
  yesBid: number | null;
  settlementEdge: number | null;
  distancePct?: number;
  depthUnderCap: number;
  bookAgeMs?: number;
  ruleConfidence: number;
  tradeScore: number;
  tradeBand: "tradeable" | "maybe" | "alert" | "ignore";
  liveBlockers: string[];
  reason: string;
};

export type MonotonicityAudit = {
  company: string;
  deadline: string;
  lowerMarketSlug: string;
  higherMarketSlug: string;
  lowerThreshold: number;
  higherThreshold: number;
  lowerYesAsk: number | null;
  lowerYesBid: number | null;
  higherYesAsk: number | null;
  higherYesBid: number | null;
  bidBackedEdge: number | null;
  midEdge: number | null;
  askOnlyEdge: number | null;
  bookAgeMs: number | null;
  sameRuleHashFamily: boolean;
  sameDirectionSemantics: boolean;
  violationTier: ViolationTier;
  tradeableBuyOnly: boolean;
  reason: string;
};

const NEAR_BOUNDARY_PCT = 0.01;
const FAR_ABOVE_PCT = 0.03;
const DEFAULT_MIN_DEPTH_UNDER_CAP = 5;
const MAX_SOURCE_DATE_AGE_HOURS = 72;

export function depthUnderCap(quote: BookQuote | undefined, cap: number): number {
  if (!quote) return 0;
  return quote.asks
    .filter((level) => level.price <= cap)
    .reduce((sum, level) => sum + level.price * level.size, 0);
}

export function bookAgeMs(quote: BookQuote | undefined, now = new Date()): number | undefined {
  if (!quote) return undefined;
  const ts = Date.parse(quote.fetchedAt);
  return Number.isFinite(ts) ? Math.max(0, now.getTime() - ts) : undefined;
}

export function previousEligibleMax(evidence: NpmEvidence | undefined, latestDate: string | undefined): number | undefined {
  if (!evidence || !latestDate) return undefined;
  const previous = evidence.tape.filter((point) => point.date < latestDate);
  if (!previous.length) return undefined;
  return Math.max(...previous.map((point) => point.impliedValuation));
}

export function previousEligibleMaxForLeg(
  evidence: NpmEvidence | undefined,
  leg: ValuationLeg,
  latestDate: string | undefined,
): number | undefined {
  if (!evidence || !latestDate) return undefined;
  const deadline = Date.parse(leg.deadlineIso);
  const start = leg.marketWindowStartIso ? Date.parse(leg.marketWindowStartIso) : Number.NEGATIVE_INFINITY;
  const previous = evidence.tape.filter((point) => {
    const ts = Date.parse(`${point.date}T00:00:00Z`);
    return Number.isFinite(ts)
      && ts >= start
      && ts <= deadline
      && point.date < latestDate;
  });
  if (!previous.length) return undefined;
  return Math.max(...previous.map((point) => point.impliedValuation));
}

export function classifyMarketState(
  leg: ValuationLeg,
  evidence: NpmEvidence | undefined,
): MarketState {
  if (leg.parseStatus !== "ok" || leg.threshold === undefined || !evidence?.identityOk) return "AMBIGUOUS";
  const maxEligible = evidence.maxEligibleValuation ?? evidence.latestValuation;
  const previousMax = previousEligibleMaxForLeg(evidence, leg, evidence.maxEligibleDate ?? evidence.latestTapeDate);
  if (maxEligible >= leg.threshold) {
    if (previousMax !== undefined && previousMax < leg.threshold) return "NEWLY_CROSSED";
    return "PREVIOUSLY_CROSSED";
  }
  const distance = (leg.threshold - evidence.latestValuation) / leg.threshold;
  if (distance >= FAR_ABOVE_PCT) return "FAR_ABOVE";
  if (distance >= 0 && distance <= NEAR_BOUNDARY_PCT) return "NEAR_BOUNDARY";
  return "UNCROSSED";
}

export function ruleConfidence(leg: ValuationLeg): number {
  if (leg.parseStatus !== "ok" || leg.threshold === undefined) return 2;
  if (!leg.acceptingOrders || leg.closed || !leg.active) return 4;
  if (!leg.ruleText.toLowerCase().includes("reaches or exceeds")) return 6;
  return 10;
}

export function buildMarketAuditRow(input: {
  leg: ValuationLeg;
  evidence?: NpmEvidence;
  quote?: BookQuote;
  config: StrategyConfig;
  liveBlockers?: string[];
  now?: Date;
}): MarketAuditRow {
  const now = input.now ?? new Date();
  const { leg, evidence, quote, config } = input;
  const state = classifyMarketState(leg, evidence);
  const cap = config.maxYesPriceBySignal.SOURCE_CONFIRMED_YES ?? config.defaultMaxYesPrice;
  const depth = depthUnderCap(quote, cap);
  const age = bookAgeMs(quote, now);
  const sourceDate = evidence?.maxEligibleDate ?? evidence?.latestTapeDate;
  const sourceAgeHours = sourceDate ? Math.max(0, (now.getTime() - Date.parse(`${sourceDate}T00:00:00Z`)) / 3_600_000) : undefined;
  const yesAsk = quote?.bestAsk ?? null;
  const settlementEdge = yesAsk === null ? null : 1 - yesAsk;
  const confidence = ruleConfidence(leg);
  const crossedQuality = classifyCrossedQuality({
    state,
    yesAsk,
    depthUnderCap: depth,
    ruleConfidence: confidence,
    sourceDateAgeHours: sourceAgeHours,
    cap,
  });
  const liquidityScore = Math.min(20, depth * 2);
  const ruleScore = confidence * 2;
  const staleBookPenalty = age !== undefined && age > config.orderbookMaxAgeMs ? 30 : 0;
  const ambiguityPenalty = confidence < 8 ? 25 : 0;
  const newlyCrossedBonus = state === "NEWLY_CROSSED" ? 25 : 0;
  const tradeScore = Math.max(0, Math.round(((settlementEdge ?? 0) * 100) + newlyCrossedBonus + liquidityScore + ruleScore - staleBookPenalty - ambiguityPenalty));
  return {
    company: leg.company,
    eventSlug: leg.eventSlug,
    marketSlug: leg.marketSlug,
    threshold: leg.threshold,
    deadline: leg.deadlineIso,
    label: leg.label,
    state,
    crossedQuality,
    latestValuation: evidence?.latestValuation,
    latestDate: evidence?.latestTapeDate,
    maxEligibleValuation: evidence?.maxEligibleValuation ?? evidence?.latestValuation,
    maxEligibleDate: evidence?.maxEligibleDate ?? evidence?.latestTapeDate,
    previousMaxEligibleValuation: previousEligibleMaxForLeg(evidence, leg, evidence?.maxEligibleDate ?? evidence?.latestTapeDate),
    sourceDateAgeHours: sourceAgeHours,
    yesAsk,
    yesBid: quote?.bestBid ?? null,
    settlementEdge,
    distancePct: leg.threshold !== undefined && evidence ? Math.abs((evidence.maxEligibleValuation ?? evidence.latestValuation) - leg.threshold) / leg.threshold : undefined,
    depthUnderCap: depth,
    bookAgeMs: age,
    ruleConfidence: confidence,
    tradeScore,
    tradeBand: tradeBand(tradeScore),
    liveBlockers: input.liveBlockers ?? [],
    reason: rowReason(state, crossedQuality),
  };
}

export function classifyCrossedQuality(input: {
  state: MarketState;
  yesAsk: number | null;
  depthUnderCap: number;
  ruleConfidence: number;
  sourceDateAgeHours?: number;
  cap: number;
}): CrossedLegQuality {
  if (input.state !== "NEWLY_CROSSED" && input.state !== "PREVIOUSLY_CROSSED") return "NOT_SOURCE_CONFIRMED";
  if (input.ruleConfidence < 8) return "SOURCE_CONFIRMED_BUT_RULE_AMBIGUOUS";
  if (input.sourceDateAgeHours !== undefined && input.sourceDateAgeHours > MAX_SOURCE_DATE_AGE_HOURS) {
    return "SOURCE_CONFIRMED_BUT_SOURCE_DATE_RISK";
  }
  if (input.yesAsk === null || input.depthUnderCap < DEFAULT_MIN_DEPTH_UNDER_CAP) return "SOURCE_CONFIRMED_BUT_LOW_LIQUIDITY";
  if (input.yesAsk > input.cap) return "SOURCE_CONFIRMED_BUT_ALREADY_PRICED";
  return "SOURCE_CONFIRMED_AND_STALE";
}

export function monotonicityAudits(
  points: CurvePoint[],
  quotes: Map<string, BookQuote>,
  config: StrategyConfig,
  now = new Date(),
): MonotonicityAudit[] {
  const groups = groupBy(points, (point) => `${point.leg.company}\u0000${point.leg.deadlineIso}`);
  const audits: MonotonicityAudit[] = [];
  for (const group of groups.values()) {
    const sorted = [...group]
      .filter((point) => point.leg.threshold !== undefined)
      .sort((left, right) => (left.leg.threshold ?? 0) - (right.leg.threshold ?? 0));
    for (let i = 1; i < sorted.length; i += 1) {
      const lower = sorted[i - 1];
      const higher = sorted[i];
      if (!lower || !higher || lower.leg.threshold === undefined || higher.leg.threshold === undefined) continue;
      const lowerQuote = quotes.get(lower.leg.marketSlug);
      const higherQuote = quotes.get(higher.leg.marketSlug);
      const lowerAsk = lowerQuote?.bestAsk ?? null;
      const lowerBid = lowerQuote?.bestBid ?? null;
      const higherAsk = higherQuote?.bestAsk ?? null;
      const higherBid = higherQuote?.bestBid ?? null;
      const bidBackedEdge = higherBid !== null && lowerAsk !== null ? higherBid - lowerAsk : null;
      const higherMid = higherAsk !== null && higherBid !== null ? (higherAsk + higherBid) / 2 : null;
      const midEdge = higherMid !== null && lowerAsk !== null ? higherMid - lowerAsk : null;
      const askOnlyEdge = higherAsk !== null && lowerAsk !== null ? higherAsk - lowerAsk : null;
      if ((bidBackedEdge ?? -Infinity) < config.minimumEdge.curve && (midEdge ?? -Infinity) < config.minimumEdge.curve && (askOnlyEdge ?? -Infinity) < config.minimumEdge.curve) {
        continue;
      }
      const ages = [bookAgeMs(lowerQuote, now), bookAgeMs(higherQuote, now)].filter((age): age is number => age !== undefined);
      const maxAge = ages.length ? Math.max(...ages) : null;
      const sameRuleHashFamily = (lower.leg.ruleFamilyHash ?? lower.leg.ruleHash) === (higher.leg.ruleFamilyHash ?? higher.leg.ruleHash);
      const sameDirectionSemantics = lower.leg.eventSlug === higher.leg.eventSlug && lower.leg.company === higher.leg.company && lower.leg.deadlineIso === higher.leg.deadlineIso;
      const tier = classifyViolationTier({
        bidBackedEdge,
        midEdge,
        askOnlyEdge,
        bookAgeMs: maxAge,
        maxBookAgeMs: config.orderbookMaxAgeMs,
        sameRuleHashFamily,
        sameDirectionSemantics,
        lowerLabel: lower.leg.label,
        higherLabel: higher.leg.label,
        minEdge: config.minimumEdge.curve,
      });
      audits.push({
        company: lower.leg.company,
        deadline: lower.leg.deadlineIso,
        lowerMarketSlug: lower.leg.marketSlug,
        higherMarketSlug: higher.leg.marketSlug,
        lowerThreshold: lower.leg.threshold,
        higherThreshold: higher.leg.threshold,
        lowerYesAsk: lowerAsk,
        lowerYesBid: lowerBid,
        higherYesAsk: higherAsk,
        higherYesBid: higherBid,
        bidBackedEdge,
        midEdge,
        askOnlyEdge,
        bookAgeMs: maxAge,
        sameRuleHashFamily,
        sameDirectionSemantics,
        violationTier: tier,
        tradeableBuyOnly: tier === "HARD_CROSS_MARKET_BID_VIOLATION"
          && lower.leg.acceptingOrders
          && lowerAsk !== null
          && lowerAsk <= (config.maxYesPriceBySignal.CURVE_MONOTONICITY_YES ?? config.defaultMaxYesPrice),
        reason: violationReason(tier),
      });
    }
  }
  return audits;
}

function classifyViolationTier(input: {
  bidBackedEdge: number | null;
  midEdge: number | null;
  askOnlyEdge: number | null;
  bookAgeMs: number | null;
  maxBookAgeMs: number;
  sameRuleHashFamily: boolean;
  sameDirectionSemantics: boolean;
  lowerLabel?: string;
  higherLabel?: string;
  minEdge: number;
}): ViolationTier {
  if (!input.sameRuleHashFamily || !input.sameDirectionSemantics) return "RULE_MISMATCH_REJECTED";
  if (input.lowerLabel !== input.higherLabel && input.lowerLabel !== undefined && input.higherLabel !== undefined) {
    return "LOW_CONFIDENCE_LABEL_MISMATCH";
  }
  if (input.bookAgeMs === null || input.bookAgeMs > input.maxBookAgeMs) return "STALE_BOOK_VIOLATION";
  if ((input.bidBackedEdge ?? -Infinity) >= input.minEdge) return "HARD_CROSS_MARKET_BID_VIOLATION";
  if ((input.midEdge ?? -Infinity) >= input.minEdge) return "SOFT_MID_VIOLATION";
  return "SOFT_ASK_ONLY_VIOLATION";
}

function tradeBand(score: number): MarketAuditRow["tradeBand"] {
  if (score >= 80) return "tradeable";
  if (score >= 60) return "maybe";
  if (score >= 40) return "alert";
  return "ignore";
}

function rowReason(state: MarketState, quality: CrossedLegQuality): string {
  if (quality !== "NOT_SOURCE_CONFIRMED") return quality.toLowerCase();
  return state.toLowerCase();
}

function violationReason(tier: ViolationTier): string {
  if (tier === "HARD_CROSS_MARKET_BID_VIOLATION") return "higher_threshold_bid_exceeds_lower_threshold_ask_by_min_edge";
  if (tier === "SOFT_MID_VIOLATION") return "higher_threshold_mid_exceeds_lower_threshold_ask_by_min_edge";
  if (tier === "SOFT_ASK_ONLY_VIOLATION") return "higher_threshold_ask_exceeds_lower_threshold_ask_only";
  if (tier === "STALE_BOOK_VIOLATION") return "one_or_both_books_missing_or_stale";
  if (tier === "RULE_MISMATCH_REJECTED") return "rule_hash_or_semantics_mismatch";
  return "label_semantics_mismatch";
}

function groupBy<T>(items: T[], keyFn: (item: T) => string): Map<string, T[]> {
  const groups = new Map<string, T[]>();
  for (const item of items) groups.set(keyFn(item), [...(groups.get(keyFn(item)) ?? []), item]);
  return groups;
}
