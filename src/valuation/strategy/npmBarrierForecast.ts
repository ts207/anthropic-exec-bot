import { createHash } from "node:crypto";
import type { BookQuote, NpmEvidence, SignalType, StrategyConfig, ValuationLeg } from "./signalTypes.ts";
import type { MarketAuditRow } from "./marketAudit.ts";

export type TapeStats = {
  returnCount: number;
  meanDailyDrift: number;
  medianDailyDrift: number;
  recent3DayAvgDailyDrift: number;
  recent7DayAvgDailyDrift: number;
  meanDailyLogReturn: number;
  dailyVol: number;
  maxDailyMove: number;
};

export type SourceFreshnessState =
  | "FRESH_NEW_FIXING"
  | "FRESH_CARRIED_FORWARD"
  | "STALE_ENDPOINT"
  | "MISSED_EXPECTED_UPDATE"
  | "SOURCE_BLOCKED"
  | "UNKNOWN";

export type SourceFreshness = {
  company: string;
  fetchedAt: string;
  latestTapeDate?: string;
  latestTapeAgeHours?: number;
  expectedNextUpdateAt?: string;
  lastSuccessfulFetchAt?: string;
  endpointFresh: boolean;
  endpointChangedSinceLastFetch: boolean;
  tapeAdvancedSinceLastFetch: boolean;
  expectedUpdateMissed: boolean;
  carryForwardLikely: boolean;
  freshnessState: SourceFreshnessState;
  staleBlockReason?: string;
  rawHash?: string;
};

export type SourceFreshnessSnapshot = {
  generatedAt: string;
  companies: Record<string, SourceFreshness>;
};

export type ForecastAuditRow = {
  company: string;
  eventSlug: string;
  marketSlug: string;
  threshold?: number;
  deadline: string;
  state: MarketAuditRow["state"];
  latestValuation?: number;
  latestDate?: string;
  maxEligibleValuation?: number;
  maxEligibleDate?: string;
  distancePct?: number;
  daysRemaining: number;
  sourceDateAgeHours?: number;
  freshnessState: SourceFreshnessState;
  expectedNextUpdateAt?: string;
  lastSuccessfulFetchAt?: string;
  endpointChangedSinceLastFetch: boolean;
  tapeAdvancedSinceLastFetch: boolean;
  carryForwardLikely: boolean;
  staleBlockReason?: string;
  dailyDrift: number;
  medianDailyDrift: number;
  recent3DayAvgDailyDrift: number;
  recent7DayAvgDailyDrift: number;
  dailyVol: number;
  maxDailyMove: number;
  pCrossTomorrow: number;
  pTouchByDeadline: number;
  yesAsk: number | null;
  yesBid: number | null;
  modelFairPrice: number;
  edge: number | null;
  confidenceScore: number;
  depthUnderCap: number;
  signalType: SignalType;
  liveEligible: false;
  reason: string;
  needed: string[];
  paperTrade: {
    forecastTime: string;
    entryPrice: number | null;
    nextNpmFixingResult: null;
    thresholdTouched: null;
    marketPriceAfterFixing: null;
    finalResolution: null;
    hypotheticalPnl: null;
  };
};

export function buildNpmBarrierForecasts(input: {
  legs: ValuationLeg[];
  evidenceByCompany: Map<string, NpmEvidence>;
  quotes: Map<string, BookQuote>;
  marketRows: MarketAuditRow[];
  sourceFreshnessByCompany?: Map<string, SourceFreshness>;
  config: StrategyConfig;
  now?: Date;
  simulations?: number;
}): ForecastAuditRow[] {
  const now = input.now ?? new Date();
  const marketRows = new Map(input.marketRows.map((row) => [row.marketSlug, row]));
  return input.legs
    .filter((leg) => leg.eventKind === "threshold")
    .map((leg) => forecastLeg({
      leg,
      evidence: input.evidenceByCompany.get(leg.company),
      quote: input.quotes.get(leg.marketSlug),
      marketRow: marketRows.get(leg.marketSlug),
      sourceFreshness: input.sourceFreshnessByCompany?.get(leg.company),
      config: input.config,
      now,
      simulations: input.simulations ?? 4_000,
    }))
    .sort((left, right) => (right.edge ?? -Infinity) - (left.edge ?? -Infinity));
}

export function tapeStats(evidence: NpmEvidence | undefined): TapeStats {
  const returns = logReturns(evidence);
  const arithmetic = returns.map((value) => Math.exp(value) - 1);
  const meanLog = mean(returns);
  return {
    returnCount: returns.length,
    meanDailyDrift: Math.exp(meanLog) - 1,
    medianDailyDrift: median(arithmetic),
    recent3DayAvgDailyDrift: compoundDrift(returns.slice(-3)),
    recent7DayAvgDailyDrift: compoundDrift(returns.slice(-7)),
    meanDailyLogReturn: meanLog,
    dailyVol: sampleStddev(returns),
    maxDailyMove: arithmetic.reduce((max, value) => Math.max(max, Math.abs(value)), 0),
  };
}

export function buildSourceFreshnessSnapshot(input: {
  evidenceByCompany: Map<string, NpmEvidence>;
  previous?: SourceFreshnessSnapshot | null;
  now?: Date;
}): SourceFreshnessSnapshot {
  const now = input.now ?? new Date();
  const fetchedAt = now.toISOString();
  const companies: Record<string, SourceFreshness> = {};
  for (const [company, evidence] of input.evidenceByCompany.entries()) {
    companies[company] = sourceFreshnessForEvidence(company, evidence, input.previous?.companies[company], now);
  }
  return { generatedAt: fetchedAt, companies };
}

export function parseSourceFreshnessSnapshot(raw: unknown): SourceFreshnessSnapshot | null {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return null;
  const record = raw as Record<string, unknown>;
  const rawCompanies = record.companies && typeof record.companies === "object" && !Array.isArray(record.companies)
    ? record.companies as Record<string, unknown>
    : {};
  const companies: Record<string, SourceFreshness> = {};
  for (const [company, value] of Object.entries(rawCompanies)) {
    if (!value || typeof value !== "object" || Array.isArray(value)) continue;
    const item = value as Record<string, unknown>;
    companies[company] = {
      company,
      fetchedAt: stringOr(item.fetchedAt, new Date(0).toISOString()),
      latestTapeDate: optionalString(item.latestTapeDate),
      latestTapeAgeHours: optionalNumber(item.latestTapeAgeHours),
      expectedNextUpdateAt: optionalString(item.expectedNextUpdateAt),
      lastSuccessfulFetchAt: optionalString(item.lastSuccessfulFetchAt),
      endpointFresh: Boolean(item.endpointFresh),
      endpointChangedSinceLastFetch: Boolean(item.endpointChangedSinceLastFetch),
      tapeAdvancedSinceLastFetch: Boolean(item.tapeAdvancedSinceLastFetch),
      expectedUpdateMissed: Boolean(item.expectedUpdateMissed),
      carryForwardLikely: Boolean(item.carryForwardLikely),
      freshnessState: parseFreshnessState(item.freshnessState),
      staleBlockReason: optionalString(item.staleBlockReason),
      rawHash: optionalString(item.rawHash),
    };
  }
  return {
    generatedAt: stringOr(record.generatedAt, new Date(0).toISOString()),
    companies,
  };
}

export function sourceFreshnessMap(snapshot: SourceFreshnessSnapshot): Map<string, SourceFreshness> {
  return new Map(Object.entries(snapshot.companies));
}

export function pCrossTomorrow(latestValuation: number, threshold: number, mu: number, sigma: number): number {
  if (latestValuation >= threshold) return 1;
  if (latestValuation <= 0 || threshold <= 0) return 0;
  const gap = Math.log(threshold / latestValuation);
  if (sigma <= 1e-9) return mu >= gap ? 1 : 0;
  const z = (gap - mu) / sigma;
  return clamp01(1 - normalCdf(z));
}

export function monteCarloTouchProbability(input: {
  latestValuation: number;
  threshold: number;
  mu: number;
  sigma: number;
  days: number;
  paths: number;
  seed: string;
}): number {
  if (input.latestValuation >= input.threshold) return 1;
  if (input.days <= 0 || input.paths <= 0 || input.latestValuation <= 0 || input.threshold <= 0) return 0;
  if (input.sigma <= 1e-9) {
    const projectedMax = input.latestValuation * Math.exp(input.mu * input.days);
    return projectedMax >= input.threshold ? 1 : 0;
  }
  const random = mulberry32(hashSeed(input.seed));
  let touched = 0;
  for (let path = 0; path < input.paths; path += 1) {
    let value = input.latestValuation;
    for (let day = 0; day < input.days; day += 1) {
      value *= Math.exp(input.mu + input.sigma * normalSample(random));
      if (value >= input.threshold) {
        touched += 1;
        break;
      }
    }
  }
  return touched / input.paths;
}

function forecastLeg(input: {
  leg: ValuationLeg;
  evidence?: NpmEvidence;
  quote?: BookQuote;
  marketRow?: MarketAuditRow;
  sourceFreshness?: SourceFreshness;
  config: StrategyConfig;
  now: Date;
  simulations: number;
}): ForecastAuditRow {
  const { leg, evidence, quote, marketRow, sourceFreshness, config, now } = input;
  const stats = tapeStats(evidence);
  const latestValuation = evidence?.latestValuation;
  const threshold = leg.threshold;
  const sourceAge = evidence ? sourceDateAgeHours(evidence.latestTapeDate, now) : undefined;
  const daysRemaining = evidence ? daysRemainingFromSource(evidence.latestTapeDate, leg.deadlineIso) : 0;
  const distance = latestValuation !== undefined && threshold !== undefined
    ? (threshold - latestValuation) / threshold
    : undefined;
  const mu = stats.recent7DayAvgDailyDrift !== 0 ? Math.log(1 + stats.recent7DayAvgDailyDrift) : stats.meanDailyLogReturn;
  const sigma = stats.dailyVol;
  const tomorrow = latestValuation !== undefined && threshold !== undefined
    ? pCrossTomorrow(latestValuation, threshold, mu, sigma)
    : 0;
  const touch = latestValuation !== undefined && threshold !== undefined
    ? monteCarloTouchProbability({
      latestValuation,
      threshold,
      mu,
      sigma,
      days: Math.min(daysRemaining, 60),
      paths: input.simulations,
      seed: `${leg.company}:${leg.marketSlug}:${evidence?.latestTapeDate ?? "no-date"}`,
    })
    : 0;
  const yesAsk = quote?.bestAsk ?? null;
  const edge = yesAsk === null ? null : touch - yesAsk;
  const confidence = confidenceScore({
    distancePct: distance,
    edge,
    returnCount: stats.returnCount,
    dailyVol: stats.dailyVol,
    sourceFreshness,
  });
  const needed = neededConditions({
    state: marketRow?.state,
    distancePct: distance,
    edge,
    confidence,
    yesAsk,
    depthUnderCap: marketRow?.depthUnderCap ?? 0,
    recentDrift: stats.recent7DayAvgDailyDrift,
    sourceFreshness,
    config,
  });
  const signal = forecastSignal({
    state: marketRow?.state,
    distancePct: distance,
    daysRemaining,
    edge,
    confidence,
    yesAsk,
    depthUnderCap: marketRow?.depthUnderCap ?? 0,
    recentDrift: stats.recent7DayAvgDailyDrift,
    sourceFreshness,
    config,
  });
  return {
    company: leg.company,
    eventSlug: leg.eventSlug,
    marketSlug: leg.marketSlug,
    threshold,
    deadline: leg.deadlineIso,
    state: marketRow?.state ?? "AMBIGUOUS",
    latestValuation,
    latestDate: evidence?.latestTapeDate,
    maxEligibleValuation: evidence?.maxEligibleValuation,
    maxEligibleDate: evidence?.maxEligibleDate,
    distancePct: distance,
    daysRemaining,
    sourceDateAgeHours: sourceAge,
    freshnessState: sourceFreshness?.freshnessState ?? "UNKNOWN",
    expectedNextUpdateAt: sourceFreshness?.expectedNextUpdateAt,
    lastSuccessfulFetchAt: sourceFreshness?.lastSuccessfulFetchAt,
    endpointChangedSinceLastFetch: sourceFreshness?.endpointChangedSinceLastFetch ?? false,
    tapeAdvancedSinceLastFetch: sourceFreshness?.tapeAdvancedSinceLastFetch ?? false,
    carryForwardLikely: sourceFreshness?.carryForwardLikely ?? false,
    staleBlockReason: sourceFreshness?.staleBlockReason,
    dailyDrift: stats.meanDailyDrift,
    medianDailyDrift: stats.medianDailyDrift,
    recent3DayAvgDailyDrift: stats.recent3DayAvgDailyDrift,
    recent7DayAvgDailyDrift: stats.recent7DayAvgDailyDrift,
    dailyVol: stats.dailyVol,
    maxDailyMove: stats.maxDailyMove,
    pCrossTomorrow: tomorrow,
    pTouchByDeadline: touch,
    yesAsk,
    yesBid: quote?.bestBid ?? null,
    modelFairPrice: touch,
    edge,
    confidenceScore: confidence,
    depthUnderCap: marketRow?.depthUnderCap ?? 0,
    signalType: signal.signalType,
    liveEligible: false,
    reason: signal.reason,
    needed,
    paperTrade: {
      forecastTime: now.toISOString(),
      entryPrice: yesAsk,
      nextNpmFixingResult: null,
      thresholdTouched: null,
      marketPriceAfterFixing: null,
      finalResolution: null,
      hypotheticalPnl: null,
    },
  };
}

function forecastSignal(input: {
  state?: MarketAuditRow["state"];
  distancePct?: number;
  daysRemaining: number;
  edge: number | null;
  confidence: number;
  yesAsk: number | null;
  depthUnderCap: number;
  recentDrift: number;
  sourceFreshness?: SourceFreshness;
  config: StrategyConfig;
}): { signalType: SignalType; reason: string } {
  if (input.sourceFreshness?.freshnessState === "STALE_ENDPOINT") {
    return { signalType: "NO_FORECAST_EDGE", reason: "stale_endpoint_blocked" };
  }
  if (input.sourceFreshness?.freshnessState === "SOURCE_BLOCKED") {
    return { signalType: "NO_FORECAST_EDGE", reason: "source_blocked" };
  }
  if (!input.sourceFreshness || input.sourceFreshness.freshnessState === "UNKNOWN") {
    return { signalType: "NO_FORECAST_EDGE", reason: "source_freshness_unknown" };
  }
  if (input.state !== "NEAR_BOUNDARY") {
    return { signalType: "NO_FORECAST_EDGE", reason: `state_${input.state ?? "unknown"}_not_near_boundary` };
  }
  if (input.distancePct === undefined || input.distancePct < 0 || input.distancePct > 0.015) {
    return { signalType: "NO_FORECAST_EDGE", reason: "distance_not_in_forecast_band" };
  }
  if (input.recentDrift <= 0) return { signalType: "NO_FORECAST_EDGE", reason: "recent_npm_drift_not_positive" };
  if (input.edge === null || input.edge < 0.12) return { signalType: "NO_FORECAST_EDGE", reason: "forecast_edge_below_minimum" };
  if (input.confidence < 0.7) return { signalType: "NO_FORECAST_EDGE", reason: "forecast_confidence_below_minimum" };
  if (input.yesAsk === null || input.yesAsk > 0.75) return { signalType: "NO_FORECAST_EDGE", reason: "yes_ask_above_forecast_cap" };
  if (input.depthUnderCap < input.config.minLiquidity) return { signalType: "NO_FORECAST_EDGE", reason: "depth_under_cap_below_minimum" };
  if (input.sourceFreshness.freshnessState === "MISSED_EXPECTED_UPDATE") {
    return { signalType: "NPM_MULTI_DAY_BARRIER_FORECAST_YES", reason: "near_boundary_forecast_edge_missed_expected_update_alert_only" };
  }
  if (input.sourceFreshness.freshnessState === "FRESH_CARRIED_FORWARD") {
    return { signalType: "NPM_MULTI_DAY_BARRIER_FORECAST_YES", reason: "near_boundary_forecast_edge_carried_forward_alert_only" };
  }
  if (input.daysRemaining <= 1) {
    return { signalType: "NPM_NEAR_BOUNDARY_FORECAST_YES", reason: "near_boundary_one_fixing_forecast_edge_alert_only" };
  }
  return { signalType: "NPM_MULTI_DAY_BARRIER_FORECAST_YES", reason: "near_boundary_multi_day_barrier_forecast_edge_alert_only" };
}

function logReturns(evidence: NpmEvidence | undefined): number[] {
  if (!evidence) return [];
  const tape = [...evidence.tape].sort((left, right) => left.date.localeCompare(right.date));
  const returns: number[] = [];
  for (let i = 1; i < tape.length; i += 1) {
    const prev = tape[i - 1];
    const next = tape[i];
    if (!prev || !next || prev.impliedValuation <= 0 || next.impliedValuation <= 0) continue;
    returns.push(Math.log(next.impliedValuation / prev.impliedValuation));
  }
  return returns;
}

function confidenceScore(input: {
  distancePct?: number;
  edge: number | null;
  returnCount: number;
  dailyVol: number;
  sourceFreshness?: SourceFreshness;
}): number {
  const distanceScore = input.distancePct === undefined ? 0 : clamp01(1 - Math.max(0, input.distancePct) / 0.02);
  const edgeScore = input.edge === null ? 0 : clamp01(input.edge / 0.25);
  const sampleScore = clamp01(input.returnCount / 7);
  const volScore = input.dailyVol > 0 ? 1 : 0.25;
  const freshnessScore = freshnessConfidence(input.sourceFreshness);
  return round4((distanceScore * 0.25) + (edgeScore * 0.25) + (sampleScore * 0.2) + (volScore * 0.15) + (freshnessScore * 0.15));
}

function freshnessConfidence(freshness: SourceFreshness | undefined): number {
  if (!freshness) return 0;
  if (freshness.freshnessState === "FRESH_NEW_FIXING") return 1;
  if (freshness.freshnessState === "FRESH_CARRIED_FORWARD") return 0.75;
  if (freshness.freshnessState === "MISSED_EXPECTED_UPDATE") return 0.45;
  return 0;
}

function neededConditions(input: {
  state?: MarketAuditRow["state"];
  distancePct?: number;
  edge: number | null;
  confidence: number;
  yesAsk: number | null;
  depthUnderCap: number;
  recentDrift: number;
  sourceFreshness?: SourceFreshness;
  config: StrategyConfig;
}): string[] {
  const needed: string[] = [];
  const freshness = input.sourceFreshness?.freshnessState ?? "UNKNOWN";
  if (freshness === "STALE_ENDPOINT") needed.push("freshnessState must not be STALE_ENDPOINT");
  if (freshness === "SOURCE_BLOCKED") needed.push("source endpoint must fetch successfully");
  if (freshness === "UNKNOWN") needed.push("source freshness must be known");
  if (input.state !== "NEAR_BOUNDARY") needed.push("state must be NEAR_BOUNDARY");
  if (input.distancePct === undefined) needed.push("distancePct must be available");
  else {
    if (input.distancePct < 0) needed.push("threshold must not already be crossed");
    if (input.distancePct > 0.015) needed.push("distancePct must be <= 0.015");
  }
  if (input.recentDrift <= 0) needed.push("recent7DayAvgDailyDrift must be positive");
  if (input.edge === null) needed.push("YES ask must be available to compute edge");
  else if (input.edge < 0.12) needed.push("modelFairPrice - yesAsk must be >= 0.12");
  if (input.confidence < 0.7) needed.push("confidenceScore must be >= 0.70");
  if (input.yesAsk === null) needed.push("YES ask must be available");
  else if (input.yesAsk > 0.75) needed.push("yesAsk must be <= 0.75");
  if (input.depthUnderCap < input.config.minLiquidity) needed.push(`depthUnderCap must be >= ${input.config.minLiquidity}`);
  return needed;
}

function daysRemainingFromSource(sourceDate: string, deadlineIso: string): number {
  const start = Date.parse(`${sourceDate}T00:00:00Z`);
  const deadline = Date.parse(deadlineIso);
  if (!Number.isFinite(start) || !Number.isFinite(deadline) || deadline <= start) return 0;
  return Math.ceil((deadline - start) / 86_400_000);
}

function sourceDateAgeHours(sourceDate: string, now: Date): number | undefined {
  const ts = Date.parse(`${sourceDate}T00:00:00Z`);
  if (!Number.isFinite(ts)) return undefined;
  return Math.max(0, (now.getTime() - ts) / 3_600_000);
}

function sourceFreshnessForEvidence(
  company: string,
  evidence: NpmEvidence,
  previous: SourceFreshness | undefined,
  now: Date,
): SourceFreshness {
  const fetchedAt = now.toISOString();
  const latestTapeAgeHours = sourceDateAgeHours(evidence.latestTapeDate, now);
  const expectedNextUpdateAt = expectedNextUpdate(evidence.latestTapeDate);
  const expectedUpdateMissed = expectedNextUpdateAt ? now.getTime() > Date.parse(expectedNextUpdateAt) : false;
  const endpointChangedSinceLastFetch = previous?.rawHash === undefined ? false : previous.rawHash !== evidence.rawHash;
  const tapeAdvancedSinceLastFetch = previous?.latestTapeDate === undefined ? false : evidence.latestTapeDate > previous.latestTapeDate;
  const hoursSinceLastFetch = previous?.fetchedAt ? Math.max(0, (now.getTime() - Date.parse(previous.fetchedAt)) / 3_600_000) : undefined;
  const carryForwardLikely = !tapeAdvancedSinceLastFetch
    && (endpointChangedSinceLastFetch || previous === undefined)
    && latestTapeAgeHours !== undefined
    && latestTapeAgeHours <= 72;
  const freshnessState = classifySourceFreshness({
    latestTapeAgeHours,
    expectedUpdateMissed,
    endpointChangedSinceLastFetch,
    tapeAdvancedSinceLastFetch,
    carryForwardLikely,
    previous,
    hoursSinceLastFetch,
  });
  return {
    company,
    fetchedAt,
    latestTapeDate: evidence.latestTapeDate,
    latestTapeAgeHours,
    expectedNextUpdateAt,
    lastSuccessfulFetchAt: fetchedAt,
    endpointFresh: true,
    endpointChangedSinceLastFetch,
    tapeAdvancedSinceLastFetch,
    expectedUpdateMissed,
    carryForwardLikely,
    freshnessState,
    staleBlockReason: staleBlockReason(freshnessState),
    rawHash: evidence.rawHash,
  };
}

function classifySourceFreshness(input: {
  latestTapeAgeHours?: number;
  expectedUpdateMissed: boolean;
  endpointChangedSinceLastFetch: boolean;
  tapeAdvancedSinceLastFetch: boolean;
  carryForwardLikely: boolean;
  previous?: SourceFreshness;
  hoursSinceLastFetch?: number;
}): SourceFreshnessState {
  if (input.tapeAdvancedSinceLastFetch) return "FRESH_NEW_FIXING";
  if (input.latestTapeAgeHours !== undefined && input.latestTapeAgeHours <= 36) return "FRESH_NEW_FIXING";
  if (input.carryForwardLikely) return "FRESH_CARRIED_FORWARD";
  if (
    input.previous
    && !input.endpointChangedSinceLastFetch
    && input.latestTapeAgeHours !== undefined
    && input.latestTapeAgeHours > 72
    && input.hoursSinceLastFetch !== undefined
    && input.hoursSinceLastFetch >= 6
  ) {
    return "STALE_ENDPOINT";
  }
  if (input.expectedUpdateMissed) return "MISSED_EXPECTED_UPDATE";
  return "UNKNOWN";
}

function expectedNextUpdate(latestTapeDate: string): string | undefined {
  const latest = Date.parse(`${latestTapeDate}T00:00:00Z`);
  if (!Number.isFinite(latest)) return undefined;
  return new Date(latest + 36 * 3_600_000).toISOString();
}

function staleBlockReason(state: SourceFreshnessState): string | undefined {
  if (state === "STALE_ENDPOINT") return "endpoint_response_unchanged_after_expected_update_window";
  if (state === "SOURCE_BLOCKED") return "source_fetch_failed";
  if (state === "UNKNOWN") return "source_freshness_unknown";
  return undefined;
}

function compoundDrift(returns: number[]): number {
  if (!returns.length) return 0;
  return Math.exp(mean(returns)) - 1;
}

function mean(values: number[]): number {
  if (!values.length) return 0;
  return values.reduce((sum, value) => sum + value, 0) / values.length;
}

function median(values: number[]): number {
  if (!values.length) return 0;
  const sorted = [...values].sort((left, right) => left - right);
  const mid = Math.floor(sorted.length / 2);
  if (sorted.length % 2) return sorted[mid] ?? 0;
  return ((sorted[mid - 1] ?? 0) + (sorted[mid] ?? 0)) / 2;
}

function sampleStddev(values: number[]): number {
  if (values.length < 2) return 0;
  const avg = mean(values);
  const variance = values.reduce((sum, value) => sum + ((value - avg) ** 2), 0) / (values.length - 1);
  return Math.sqrt(Math.max(0, variance));
}

function normalCdf(value: number): number {
  return 0.5 * (1 + erf(value / Math.SQRT2));
}

function erf(value: number): number {
  const sign = value < 0 ? -1 : 1;
  const x = Math.abs(value);
  const a1 = 0.254829592;
  const a2 = -0.284496736;
  const a3 = 1.421413741;
  const a4 = -1.453152027;
  const a5 = 1.061405429;
  const p = 0.3275911;
  const t = 1 / (1 + p * x);
  const y = 1 - (((((a5 * t + a4) * t) + a3) * t + a2) * t + a1) * t * Math.exp(-x * x);
  return sign * y;
}

function normalSample(random: () => number): number {
  const u1 = Math.max(Number.EPSILON, random());
  const u2 = random();
  return Math.sqrt(-2 * Math.log(u1)) * Math.cos(2 * Math.PI * u2);
}

function mulberry32(seed: number): () => number {
  let state = seed >>> 0;
  return () => {
    state += 0x6D2B79F5;
    let t = state;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4_294_967_296;
  };
}

function hashSeed(value: string): number {
  const hash = createHash("sha256").update(value).digest();
  return hash.readUInt32LE(0);
}

function clamp01(value: number): number {
  if (!Number.isFinite(value)) return 0;
  return Math.max(0, Math.min(1, value));
}

function round4(value: number): number {
  return Math.round(value * 10_000) / 10_000;
}

function optionalString(value: unknown): string | undefined {
  return typeof value === "string" && value.trim() ? value.trim() : undefined;
}

function stringOr(value: unknown, fallback: string): string {
  return typeof value === "string" && value.trim() ? value.trim() : fallback;
}

function optionalNumber(value: unknown): number | undefined {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : undefined;
}

function parseFreshnessState(value: unknown): SourceFreshnessState {
  if (
    value === "FRESH_NEW_FIXING"
    || value === "FRESH_CARRIED_FORWARD"
    || value === "STALE_ENDPOINT"
    || value === "MISSED_EXPECTED_UPDATE"
    || value === "SOURCE_BLOCKED"
    || value === "UNKNOWN"
  ) return value;
  return "UNKNOWN";
}
