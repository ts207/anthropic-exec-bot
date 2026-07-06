import { curveRepairPassiveBid, farOptionalityPassiveBid, nearBoundaryPassiveBid } from "./entryQuotePlanner.ts";
import type { ForecastAuditRow } from "./npmBarrierForecast.ts";
import type { MarketAuditRow, MonotonicityAudit } from "./marketAudit.ts";
import type { BookQuote, NpmEvidence, StrategyConfig, ValuationLeg } from "./signalTypes.ts";

export type EntryMode =
  | "TAKER_SOURCE_CONFIRMED"
  | "MAKER_NEAR_BOUNDARY_BID"
  | "MAKER_FAR_OPTIONALITY_BID"
  | "MAKER_CURVE_REPAIR_BID"
  | "RANGE_SPREAD_PAPER"
  | "WATCH_ONLY"
  | "NO_ENTRY";

export type LadderDirection = "UP" | "DOWN" | "UNKNOWN";

export type LadderNeighbor = {
  marketSlug: string;
  threshold: number;
  direction: LadderDirection;
  distancePct?: number;
  yesAsk: number | null;
  yesBid: number | null;
};

export type LadderContext = {
  marketShape: {
    company: string;
    eventSlug: string;
    deadline: string;
    thresholdCount: number;
    directionCounts: Record<LadderDirection, number>;
    minThreshold?: number;
    maxThreshold?: number;
    currentValuation?: number;
    maxEligibleValuation?: number;
  };
  nearestLower?: LadderNeighbor;
  nearestUpper?: LadderNeighbor;
};

export type EntryPlan = {
  company: string;
  eventSlug: string;
  marketSlug: string;
  threshold?: number;
  deadline: string;
  direction: LadderDirection;
  sourceDate?: string;
  currentValuation?: number;
  maxEligibleValuation?: number;
  sourceConfirmed: boolean;
  distancePct?: number;
  yesAsk: number | null;
  yesBid: number | null;
  noAsk: number | null;
  noBid: number | null;
  modelFair: number;
  requiredEdge: number;
  passiveBidPrice: number | null;
  maxTakerPrice: number | null;
  entryMode: EntryMode;
  paperEligible: boolean;
  liveEligible: boolean;
  activation: {
    forecastActiveAt?: number;
    sourceConfirmedAt?: number;
    alertIfAskBelow?: number;
  };
  cancelRules: string[];
  blockers: string[];
  reason: string;
  pairedMarketSlug?: string;
  ladderContext?: LadderContext;
  range?: {
    lowerMarketSlug: string;
    higherMarketSlug: string;
    lowerThreshold: number;
    higherThreshold: number;
    deadline: string;
    modelRangeProbability: number;
    combinedCost: number;
    currentMarkPrice: number | null;
  };
};

export function buildLadderEntryPlans(input: {
  legs: ValuationLeg[];
  evidenceByCompany: Map<string, NpmEvidence>;
  quotes: Map<string, BookQuote>;
  marketRows: MarketAuditRow[];
  forecasts: ForecastAuditRow[];
  monotonicity: MonotonicityAudit[];
  config: StrategyConfig;
}): EntryPlan[] {
  const rows = new Map(input.marketRows.map((row) => [row.marketSlug, row]));
  const forecasts = new Map(input.forecasts.map((row) => [row.marketSlug, row]));
  const ladderContexts = buildLadderContexts(input.legs, input.evidenceByCompany, input.quotes);
  const curvePlans = curveRepairPlans({ ...input, ladderContexts });
  const rangePlans = rangeSpreadPlans({ ...input, ladderContexts });
  const replacementSlugs = new Set([...curvePlans, ...rangePlans].map((plan) => plan.marketSlug));
  const plans = input.legs
    .filter((leg) => leg.eventKind === "threshold")
    .map((leg) => buildLegEntryPlan({
      leg,
      evidence: input.evidenceByCompany.get(leg.company),
      quote: input.quotes.get(leg.marketSlug),
      marketRow: rows.get(leg.marketSlug),
      forecast: forecasts.get(leg.marketSlug),
      ladderContext: ladderContexts.get(leg.marketSlug),
      config: input.config,
    }))
    .filter((plan) => !replacementSlugs.has(plan.marketSlug));
  return [...plans, ...curvePlans, ...rangePlans].sort(comparePlans);
}

export function ladderDirection(leg: ValuationLeg): LadderDirection {
  const text = `${leg.question}\n${leg.ruleText}`.toLowerCase();
  const hasDownCue = /[↓↘]|down|below|less than|at or below|falls? to/.test(text);
  const hasConfirmedDownRule = /at or below|less than or equal|falls? to or below|below the listed amount/.test(text);
  if (hasDownCue && !hasConfirmedDownRule) return "UNKNOWN";
  if (hasConfirmedDownRule) return "DOWN";
  if (/reaches or exceeds|exceeds|surpass|hit/.test(text)) return "UP";
  return "UNKNOWN";
}

function buildLegEntryPlan(input: {
  leg: ValuationLeg;
  evidence?: NpmEvidence;
  quote?: BookQuote;
  marketRow?: MarketAuditRow;
  forecast?: ForecastAuditRow;
  ladderContext?: LadderContext;
  config: StrategyConfig;
}): EntryPlan {
  const { leg, evidence, quote, marketRow, forecast, config } = input;
  const direction = ladderDirection(leg);
  const currentValuation = evidence?.latestValuation;
  const maxEligibleValuation = evidence?.maxEligibleValuation ?? currentValuation;
  const distancePct = leg.threshold !== undefined && currentValuation !== undefined
    ? (leg.threshold - currentValuation) / leg.threshold
    : undefined;
  const yesAsk = quote?.bestAsk ?? null;
  const yesBid = quote?.bestBid ?? null;
  const noQuote = noSideQuote(yesAsk, yesBid);
  const sourceConfirmed = leg.threshold !== undefined
    && maxEligibleValuation !== undefined
    && maxEligibleValuation >= leg.threshold;
  const modelFair = sourceConfirmed ? 1 : forecast?.modelFairPrice ?? 0;
  const base = planBase({
    leg,
    direction,
    sourceDate: evidence?.latestTapeDate,
    currentValuation,
    maxEligibleValuation,
    sourceConfirmed,
    distancePct,
    yesAsk,
    yesBid,
    noAsk: noQuote.noAsk,
    noBid: noQuote.noBid,
    modelFair,
    requiredEdge: 0.12,
    maxTakerPrice: config.maxYesPriceBySignal.SOURCE_CONFIRMED_YES ?? 0.94,
    ladderContext: input.ladderContext,
  });
  const structuralBlockers = structuralBlockersFor({ leg, direction, evidence, quote, marketRow, config });
  const sourceTaker = sourceConfirmedTakerPlan(base, marketRow, structuralBlockers);
  if (sourceTaker) return sourceTaker;
  const near = nearBoundaryMakerPlan(base, structuralBlockers);
  if (near) return near;
  const far = farOptionalityMakerPlan(base, structuralBlockers);
  if (far) return far;
  return watchOrNoEntryPlan(base, sourceConfirmed, structuralBlockers);
}

function sourceConfirmedTakerPlan(
  base: EntryPlan,
  marketRow: MarketAuditRow | undefined,
  structuralBlockers: string[],
): EntryPlan | null {
  const blockers = [...structuralBlockers];
  if (base.direction === "UNKNOWN") blockers.push("direction_semantics_unknown");
  if (base.yesAsk === null || base.maxTakerPrice === null || base.yesAsk > base.maxTakerPrice) blockers.push("yes_ask_above_source_confirmed_taker_cap");
  if (!marketRow || marketRow.depthUnderCap <= 0) blockers.push("no_depth_under_taker_cap");
  if (marketRow?.bookAgeMs !== undefined && marketRow.bookAgeMs > 15_000) blockers.push("orderbook_stale");
  if (marketRow?.crossedQuality !== "SOURCE_CONFIRMED_AND_STALE") blockers.push("not_strict_stale_source_confirmed");
  if (marketRow?.liveBlockers.length) blockers.push(...marketRow.liveBlockers);
  if (marketRow?.state !== "NEWLY_CROSSED" && marketRow?.state !== "PREVIOUSLY_CROSSED") return null;
  return {
    ...base,
    entryMode: "TAKER_SOURCE_CONFIRMED",
    paperEligible: false,
    liveEligible: blockers.length === 0,
    blockers,
    reason: blockers.length ? "source_confirmed_but_live_blocked" : "source_confirmed_stale_yes_taker",
  };
}

function nearBoundaryMakerPlan(base: EntryPlan, structuralBlockers: string[]): EntryPlan | null {
  if (base.direction !== "UP") return null;
  if (base.distancePct === undefined || base.distancePct < 0 || base.distancePct > 0.015) return null;
  if (base.modelFair < 0.55) return null;
  const bidGap = base.yesBid === null ? Infinity : base.modelFair - base.yesBid;
  if (bidGap < 0.12) return null;
  const passiveBid = nearBoundaryPassiveBid({ modelFair: base.modelFair });
  return {
    ...base,
    passiveBidPrice: passiveBid,
    entryMode: "MAKER_NEAR_BOUNDARY_BID",
    paperEligible: passiveBid !== null && structuralBlockers.length === 0,
    liveEligible: false,
    blockers: structuralBlockers,
    reason: passiveBid === null ? "near_boundary_bid_below_minimum" : "near_boundary_passive_bid_paper_only",
  };
}

function farOptionalityMakerPlan(base: EntryPlan, structuralBlockers: string[]): EntryPlan | null {
  if (base.direction !== "UP") return null;
  if (base.distancePct === undefined || base.distancePct <= 0.05) return null;
  if (base.yesAsk === null || base.yesAsk > 0.15) return null;
  if (base.modelFair < base.yesAsk + 0.05) return null;
  const passiveBid = farOptionalityPassiveBid({ modelFair: base.modelFair, yesAsk: base.yesAsk });
  return {
    ...base,
    requiredEdge: 0.08,
    passiveBidPrice: passiveBid,
    entryMode: "MAKER_FAR_OPTIONALITY_BID",
    paperEligible: passiveBid !== null && structuralBlockers.length === 0,
    liveEligible: false,
    blockers: structuralBlockers,
    reason: passiveBid === null ? "far_optionality_bid_below_minimum" : "far_optionality_passive_bid_paper_only",
  };
}

function curveRepairPlans(input: {
  legs: ValuationLeg[];
  evidenceByCompany: Map<string, NpmEvidence>;
  quotes: Map<string, BookQuote>;
  marketRows: MarketAuditRow[];
  monotonicity: MonotonicityAudit[];
  ladderContexts: Map<string, LadderContext>;
  config: StrategyConfig;
}): EntryPlan[] {
  const legs = new Map(input.legs.map((leg) => [leg.marketSlug, leg]));
  const rows = new Map(input.marketRows.map((row) => [row.marketSlug, row]));
  return input.monotonicity
    .filter((audit) => audit.violationTier === "HARD_CROSS_MARKET_BID_VIOLATION" && audit.tradeableBuyOnly)
    .flatMap((audit) => {
      const leg = legs.get(audit.lowerMarketSlug);
      if (!leg || audit.lowerYesAsk === null || audit.bidBackedEdge === null) return [];
      const evidence = input.evidenceByCompany.get(leg.company);
      const quote = input.quotes.get(leg.marketSlug);
      const direction = ladderDirection(leg);
      const structuralBlockers = structuralBlockersFor({
        leg,
        direction,
        evidence,
        quote,
        marketRow: rows.get(leg.marketSlug),
        config: input.config,
      });
      const passiveBid = curveRepairPassiveBid({
        lowerYesAsk: audit.lowerYesAsk,
        bidBackedEdge: audit.bidBackedEdge,
      });
      const base = buildLegEntryPlan({
        leg,
        evidence,
        quote,
        marketRow: rows.get(leg.marketSlug),
        forecast: undefined,
        ladderContext: input.ladderContexts.get(leg.marketSlug),
        config: input.config,
      });
      return [{
        ...base,
        direction,
        modelFair: Math.min(0.99, (audit.higherYesBid ?? audit.lowerYesAsk) + audit.bidBackedEdge),
        requiredEdge: input.config.minimumEdge.curve,
        passiveBidPrice: passiveBid,
        entryMode: "MAKER_CURVE_REPAIR_BID" as const,
        paperEligible: passiveBid !== null && structuralBlockers.length === 0,
        liveEligible: false,
        blockers: structuralBlockers,
        reason: structuralBlockers.length
          ? "bid_backed_curve_repair_blocked_by_structural_risk"
          : "bid_backed_curve_repair_passive_bid_paper_only",
        pairedMarketSlug: audit.higherMarketSlug,
      }];
    });
}

function rangeSpreadPlans(input: {
  legs: ValuationLeg[];
  evidenceByCompany: Map<string, NpmEvidence>;
  quotes: Map<string, BookQuote>;
  marketRows: MarketAuditRow[];
  forecasts: ForecastAuditRow[];
  monotonicity: MonotonicityAudit[];
  ladderContexts: Map<string, LadderContext>;
  config: StrategyConfig;
}): EntryPlan[] {
  const forecastBySlug = new Map(input.forecasts.map((forecast) => [forecast.marketSlug, forecast]));
  const rows = new Map(input.marketRows.map((row) => [row.marketSlug, row]));
  const grouped = groupBy(input.legs.filter((leg) => leg.eventKind === "threshold" && leg.threshold !== undefined), (leg) => `${leg.company}\u0000${leg.deadlineIso}\u0000${leg.eventSlug}`);
  const plans: EntryPlan[] = [];
  for (const legs of grouped.values()) {
    const sorted = [...legs].sort((left, right) => (left.threshold ?? 0) - (right.threshold ?? 0));
    for (let index = 1; index < sorted.length; index += 1) {
      const lower = sorted[index - 1];
      const higher = sorted[index];
      if (!lower || !higher || lower.threshold === undefined || higher.threshold === undefined) continue;
      if (ladderDirection(lower) !== "UP" || ladderDirection(higher) !== "UP") continue;
      if ((lower.ruleFamilyHash ?? lower.ruleHash) !== (higher.ruleFamilyHash ?? higher.ruleHash)) continue;
      const lowerQuote = input.quotes.get(lower.marketSlug);
      const higherQuote = input.quotes.get(higher.marketSlug);
      const lowerForecast = forecastBySlug.get(lower.marketSlug);
      const higherForecast = forecastBySlug.get(higher.marketSlug);
      if (!lowerQuote || !higherQuote || !lowerForecast || !higherForecast) continue;
      const evidence = input.evidenceByCompany.get(lower.company);
      const lowerBlockers = structuralBlockersFor({
        leg: lower,
        direction: ladderDirection(lower),
        evidence,
        quote: lowerQuote,
        marketRow: rows.get(lower.marketSlug),
        config: input.config,
      });
      const higherBlockers = structuralBlockersFor({
        leg: higher,
        direction: ladderDirection(higher),
        evidence,
        quote: higherQuote,
        marketRow: rows.get(higher.marketSlug),
        config: input.config,
      }).map((blocker) => `paired_${blocker}`);
      const blockers = [...new Set([...lowerBlockers, ...higherBlockers])];
      const lowerAsk = lowerQuote.bestAsk;
      const higherNoAsk = higherQuote.bestBid === null ? null : round4(1 - higherQuote.bestBid);
      if (lowerAsk === null || higherNoAsk === null) continue;
      const modelRangeProbability = Math.max(0, lowerForecast.pTouchByDeadline - higherForecast.pTouchByDeadline);
      const combinedCost = round4(lowerAsk + higherNoAsk);
      if (combinedCost >= modelRangeProbability - 0.10) continue;
      const base = buildLegEntryPlan({
        leg: lower,
        evidence,
        quote: lowerQuote,
        marketRow: rows.get(lower.marketSlug),
        forecast: lowerForecast,
        ladderContext: input.ladderContexts.get(lower.marketSlug),
        config: input.config,
      });
      plans.push({
        ...base,
        pairedMarketSlug: higher.marketSlug,
        modelFair: round4(modelRangeProbability),
        requiredEdge: 0.10,
        passiveBidPrice: combinedCost,
        entryMode: "RANGE_SPREAD_PAPER",
        paperEligible: blockers.length === 0,
        liveEligible: false,
        blockers,
        reason: blockers.length
          ? "adjacent_threshold_range_spread_blocked_by_structural_risk"
          : "adjacent_threshold_range_spread_paper_only",
        range: {
          lowerMarketSlug: lower.marketSlug,
          higherMarketSlug: higher.marketSlug,
          lowerThreshold: lower.threshold,
          higherThreshold: higher.threshold,
          deadline: lower.deadlineIso,
          modelRangeProbability: round4(modelRangeProbability),
          combinedCost,
          currentMarkPrice: higherQuote.bestAsk === null ? null : round4((lowerQuote.bestBid ?? 0) + (1 - higherQuote.bestAsk)),
        },
      });
    }
  }
  return plans;
}

function watchOrNoEntryPlan(base: EntryPlan, sourceConfirmed: boolean, structuralBlockers: string[]): EntryPlan {
  const nearEnoughToWatch = base.distancePct !== undefined && base.distancePct >= 0 && base.distancePct <= 0.05;
  const entryMode: EntryMode = nearEnoughToWatch || sourceConfirmed ? "WATCH_ONLY" : "NO_ENTRY";
  const blockers = [...structuralBlockers];
  if (base.yesAsk !== null && base.distancePct !== undefined && base.distancePct >= 0 && base.distancePct <= 0.015) {
    blockers.push("near_boundary_but_no_edge_or_overpriced");
  }
  return {
    ...base,
    entryMode,
    paperEligible: false,
    liveEligible: false,
    blockers,
    reason: entryMode === "WATCH_ONLY" ? "watch_ladder_leg_no_entry" : "no_ladder_entry",
  };
}

function structuralBlockersFor(input: {
  leg: ValuationLeg;
  direction: LadderDirection;
  evidence?: NpmEvidence;
  quote?: BookQuote;
  marketRow?: MarketAuditRow;
  config: StrategyConfig;
}): string[] {
  const blockers: string[] = [];
  if (input.leg.parseStatus !== "ok" || input.leg.threshold === undefined) blockers.push("malformed_or_unsupported_leg");
  if (!input.leg.active || input.leg.closed || !input.leg.acceptingOrders) blockers.push("market_not_accepting_orders");
  if (!input.evidence?.identityOk) blockers.push("missing_or_unverified_npm_evidence");
  if (!input.quote) blockers.push("missing_orderbook");
  if (input.marketRow?.bookAgeMs !== undefined && input.marketRow.bookAgeMs > input.config.orderbookMaxAgeMs) blockers.push("orderbook_stale");
  if (input.direction === "UNKNOWN") blockers.push("direction_semantics_unknown");
  return [...new Set(blockers)];
}

function planBase(input: {
  leg: ValuationLeg;
  direction: LadderDirection;
  currentValuation?: number;
  maxEligibleValuation?: number;
  sourceConfirmed: boolean;
  sourceDate?: string;
  distancePct?: number;
  yesAsk: number | null;
  yesBid: number | null;
  noAsk: number | null;
  noBid: number | null;
  modelFair: number;
  requiredEdge: number;
  maxTakerPrice: number | null;
  ladderContext?: LadderContext;
}): EntryPlan {
  return {
    company: input.leg.company,
    eventSlug: input.leg.eventSlug,
    marketSlug: input.leg.marketSlug,
    threshold: input.leg.threshold,
    deadline: input.leg.deadlineIso,
    direction: input.direction,
    sourceDate: input.sourceDate,
    currentValuation: input.currentValuation,
    maxEligibleValuation: input.maxEligibleValuation,
    sourceConfirmed: input.sourceConfirmed,
    distancePct: input.distancePct,
    yesAsk: input.yesAsk,
    yesBid: input.yesBid,
    noAsk: input.noAsk,
    noBid: input.noBid,
    modelFair: round4(input.modelFair),
    requiredEdge: input.requiredEdge,
    passiveBidPrice: null,
    maxTakerPrice: input.maxTakerPrice,
    entryMode: "NO_ENTRY",
    paperEligible: false,
    liveEligible: false,
    activation: {
      forecastActiveAt: input.leg.threshold === undefined ? undefined : input.leg.threshold * 0.985,
      sourceConfirmedAt: input.leg.threshold,
      alertIfAskBelow: input.leg.threshold === undefined ? undefined : 0.94,
    },
    cancelRules: [
      "cancel_if_npm_source_moves_away_from_threshold",
      "cancel_before_next_npm_fixing_if_model_fair_is_stale",
      "cancel_if_rule_or_direction_semantics_change",
      "cancel_if_orderbook_spread_or_liquidity_fails_caps",
    ],
    blockers: [],
    reason: "not_classified",
    ladderContext: input.ladderContext,
  };
}

function buildLadderContexts(
  legs: ValuationLeg[],
  evidenceByCompany: Map<string, NpmEvidence>,
  quotes: Map<string, BookQuote>,
): Map<string, LadderContext> {
  const result = new Map<string, LadderContext>();
  const groups = groupBy(
    legs.filter((leg) => leg.eventKind === "threshold" && leg.threshold !== undefined),
    (leg) => `${leg.company}\u0000${leg.eventSlug}\u0000${leg.deadlineIso}`,
  );
  for (const group of groups.values()) {
    const sorted = [...group].sort((left, right) => (left.threshold ?? 0) - (right.threshold ?? 0));
    const first = sorted[0];
    if (!first) continue;
    const evidence = evidenceByCompany.get(first.company);
    const currentValuation = evidence?.latestValuation;
    const thresholds = sorted.flatMap((leg) => leg.threshold === undefined ? [] : [leg.threshold]);
    const directionCounts = sorted.reduce<Record<LadderDirection, number>>((counts, leg) => {
      counts[ladderDirection(leg)] += 1;
      return counts;
    }, { UP: 0, DOWN: 0, UNKNOWN: 0 });
    const marketShape = {
      company: first.company,
      eventSlug: first.eventSlug,
      deadline: first.deadlineIso,
      thresholdCount: sorted.length,
      directionCounts,
      minThreshold: thresholds[0],
      maxThreshold: thresholds[thresholds.length - 1],
      currentValuation,
      maxEligibleValuation: evidence?.maxEligibleValuation ?? currentValuation,
    };
    for (const leg of sorted) {
      if (leg.threshold === undefined) continue;
      result.set(leg.marketSlug, {
        marketShape,
        nearestLower: nearestNeighbor(sorted, quotes, currentValuation, "lower"),
        nearestUpper: nearestNeighbor(sorted, quotes, currentValuation, "upper"),
      });
    }
  }
  return result;
}

function nearestNeighbor(
  sorted: ValuationLeg[],
  quotes: Map<string, BookQuote>,
  currentValuation: number | undefined,
  side: "lower" | "upper",
): LadderNeighbor | undefined {
  if (currentValuation === undefined) return undefined;
  const candidates = sorted.filter((leg) => (
    leg.threshold !== undefined
    && (side === "lower" ? leg.threshold <= currentValuation : leg.threshold >= currentValuation)
  ));
  const leg = side === "lower" ? candidates[candidates.length - 1] : candidates[0];
  if (!leg || leg.threshold === undefined) return undefined;
  const quote = quotes.get(leg.marketSlug);
  return {
    marketSlug: leg.marketSlug,
    threshold: leg.threshold,
    direction: ladderDirection(leg),
    distancePct: (leg.threshold - currentValuation) / leg.threshold,
    yesAsk: quote?.bestAsk ?? null,
    yesBid: quote?.bestBid ?? null,
  };
}

function noSideQuote(yesAsk: number | null, yesBid: number | null): { noAsk: number | null; noBid: number | null } {
  return {
    noAsk: yesBid === null ? null : round4(1 - yesBid),
    noBid: yesAsk === null ? null : round4(1 - yesAsk),
  };
}

function comparePlans(left: EntryPlan, right: EntryPlan): number {
  const priority = (plan: EntryPlan) => {
    if (plan.liveEligible) return 100;
    if (plan.entryMode === "TAKER_SOURCE_CONFIRMED") return 90;
    if (plan.entryMode === "MAKER_NEAR_BOUNDARY_BID") return 80;
    if (plan.entryMode === "MAKER_CURVE_REPAIR_BID") return 70;
    if (plan.entryMode === "MAKER_FAR_OPTIONALITY_BID") return 60;
    if (plan.entryMode === "RANGE_SPREAD_PAPER") return 50;
    if (plan.entryMode === "WATCH_ONLY") return 20;
    return 0;
  };
  const byPriority = priority(right) - priority(left);
  if (byPriority !== 0) return byPriority;
  return (right.modelFair - (right.yesAsk ?? 1)) - (left.modelFair - (left.yesAsk ?? 1));
}

function round4(value: number): number {
  return Math.round(value * 10_000) / 10_000;
}

function groupBy<T>(items: T[], keyFn: (item: T) => string): Map<string, T[]> {
  const groups = new Map<string, T[]>();
  for (const item of items) groups.set(keyFn(item), [...(groups.get(keyFn(item)) ?? []), item]);
  return groups;
}
