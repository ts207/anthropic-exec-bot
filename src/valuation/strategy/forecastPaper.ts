import { join } from "node:path";
import type { StrategyConfig } from "./signalTypes.ts";
import type { ForecastAuditRow } from "./npmBarrierForecast.ts";

export type ForecastPaperTradeStatus = "open" | "resolved";

export type ForecastPaperTrade = {
  id: string;
  company: string;
  eventSlug: string;
  marketSlug: string;
  threshold?: number;
  deadline: string;
  forecastTime: string;
  latestValuation?: number;
  sourceDate?: string;
  distancePct?: number;
  modelFairPrice: number;
  pTouchByDeadline: number;
  yesAsk: number;
  edge: number;
  confidenceScore: number;
  signalType: string;
  paperTrigger: "forecast_candidate" | "paper_watchlist";
  opened: true;
  entryPrice: number;
  sizeUsd: number;
  nextFixingValuation: number | null;
  nextFixingDate: string | null;
  distanceAfterFixing: number | null;
  distanceNarrowed: boolean | null;
  thresholdTouched: boolean | null;
  markPriceAfterFixing: number | null;
  currentMarkPrice: number | null;
  finalResolution: boolean | null;
  hypotheticalPnl: number | null;
  brierScore: number | null;
  calibrationBucket: string;
  status: ForecastPaperTradeStatus;
  reason: string;
  freshnessState: string;
};

export type ForecastPaperState = {
  version: 1;
  updatedAt: string;
  trades: ForecastPaperTrade[];
};

export type ForecastPaperUpdate = {
  state: ForecastPaperState;
  opened: ForecastPaperTrade[];
  updated: ForecastPaperTrade[];
  metrics: ForecastPaperMetrics;
};

export type ForecastPaperMetrics = {
  totalTrades: number;
  openTrades: number;
  resolvedTrades: number;
  openedThisRun: number;
  updatedThisRun: number;
  totalHypotheticalPnl: number;
  averageBrierScore: number | null;
  calibration: Array<{
    bucket: string;
    count: number;
    resolved: number;
    wins: number;
    averagePredicted: number;
    averageBrierScore: number | null;
    hypotheticalPnl: number;
  }>;
  proofBeforeLive: {
    minimumEntries: 30;
    currentEntries: number;
    readyForLive: false;
    requirements: string[];
  };
};

export function forecastPaperPath(config: StrategyConfig): string {
  return join(config.stateDir, "forecast_paper_trades.json");
}

export function parseForecastPaperState(raw: unknown): ForecastPaperState {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return emptyState();
  const record = raw as Record<string, unknown>;
  const trades = Array.isArray(record.trades)
    ? record.trades.filter((item): item is ForecastPaperTrade => Boolean(item && typeof item === "object" && "id" in item))
    : [];
  return {
    version: 1,
    updatedAt: typeof record.updatedAt === "string" ? record.updatedAt : new Date(0).toISOString(),
    trades,
  };
}

export function updateForecastPaperTrades(input: {
  previous: ForecastPaperState;
  forecasts: ForecastAuditRow[];
  now?: Date;
  sizeUsd?: number;
}): ForecastPaperUpdate {
  const now = input.now ?? new Date();
  const sizeUsd = input.sizeUsd ?? 1;
  const forecastByMarket = new Map(input.forecasts.map((row) => [row.marketSlug, row]));
  const trades = input.previous.trades.map((trade) => ({ ...trade }));
  const updated: ForecastPaperTrade[] = [];

  for (let index = 0; index < trades.length; index += 1) {
    const trade = trades[index];
    if (!trade || trade.status !== "open") continue;
    const forecast = forecastByMarket.get(trade.marketSlug);
    if (!forecast) continue;
    const next = updateOpenTrade(trade, forecast);
    trades[index] = next;
    if (next !== trade) updated.push(next);
  }

  const openKeys = new Set(trades.map((trade) => openingKey(trade.marketSlug, trade.sourceDate)));
  const opened: ForecastPaperTrade[] = [];
  for (const forecast of input.forecasts) {
    if (!isPaperOpenTrigger(forecast)) continue;
    const key = openingKey(forecast.marketSlug, forecast.latestDate);
    if (openKeys.has(key)) continue;
    const trade = openTrade(forecast, now, sizeUsd);
    trades.push(trade);
    opened.push(trade);
    openKeys.add(key);
  }

  const state: ForecastPaperState = {
    version: 1,
    updatedAt: now.toISOString(),
    trades,
  };
  return {
    state,
    opened,
    updated,
    metrics: paperMetrics(trades, opened.length, updated.length),
  };
}

export function isPaperOpenTrigger(row: ForecastAuditRow): boolean {
  if (row.threshold === undefined || row.latestValuation === undefined || row.yesAsk === null || row.edge === null) return false;
  if (row.freshnessState === "STALE_ENDPOINT" || row.freshnessState === "SOURCE_BLOCKED" || row.freshnessState === "UNKNOWN") return false;
  if (row.signalType !== "NO_FORECAST_EDGE") return true;
  return row.distancePct !== undefined
    && row.distancePct >= 0
    && row.distancePct <= 0.025
    && row.edge >= 0.05
    && row.confidenceScore >= 0.5
    && row.yesAsk <= 0.85;
}

function openTrade(row: ForecastAuditRow, now: Date, sizeUsd: number): ForecastPaperTrade {
  const sourceDate = row.latestDate;
  const id = [
    row.company,
    row.threshold ?? "unknown",
    row.deadline,
    row.marketSlug,
    sourceDate ?? now.toISOString(),
  ].join(":");
  return {
    id,
    company: row.company,
    eventSlug: row.eventSlug,
    marketSlug: row.marketSlug,
    threshold: row.threshold,
    deadline: row.deadline,
    forecastTime: now.toISOString(),
    latestValuation: row.latestValuation,
    sourceDate,
    distancePct: row.distancePct,
    modelFairPrice: row.modelFairPrice,
    pTouchByDeadline: row.pTouchByDeadline,
    yesAsk: row.yesAsk ?? 0,
    edge: row.edge ?? 0,
    confidenceScore: row.confidenceScore,
    signalType: row.signalType,
    paperTrigger: row.signalType === "NO_FORECAST_EDGE" ? "paper_watchlist" : "forecast_candidate",
    opened: true,
    entryPrice: row.yesAsk ?? 0,
    sizeUsd,
    nextFixingValuation: null,
    nextFixingDate: null,
    distanceAfterFixing: null,
    distanceNarrowed: null,
    thresholdTouched: null,
    markPriceAfterFixing: null,
    currentMarkPrice: row.yesAsk,
    finalResolution: null,
    hypotheticalPnl: null,
    brierScore: null,
    calibrationBucket: calibrationBucket(row.pTouchByDeadline),
    status: "open",
    reason: row.reason,
    freshnessState: row.freshnessState,
  };
}

function updateOpenTrade(trade: ForecastPaperTrade, row: ForecastAuditRow): ForecastPaperTrade {
  const currentMarkPrice = row.yesAsk;
  const sourceAdvanced = Boolean(row.latestDate && trade.sourceDate && row.latestDate > trade.sourceDate);
  const deadlinePassed = Date.parse(row.deadline) <= Date.now();
  const thresholdTouched = row.threshold !== undefined && (row.maxEligibleValuation ?? row.latestValuation ?? -Infinity) >= row.threshold;
  const next: ForecastPaperTrade = {
    ...trade,
    currentMarkPrice,
    freshnessState: row.freshnessState,
  };
  if (sourceAdvanced && next.nextFixingValuation === null) {
    next.nextFixingValuation = row.latestValuation ?? null;
    next.nextFixingDate = row.latestDate ?? null;
    next.distanceAfterFixing = row.distancePct ?? null;
    next.distanceNarrowed = trade.distancePct !== undefined && row.distancePct !== undefined
      ? Math.max(0, row.distancePct) < Math.max(0, trade.distancePct)
      : null;
    next.thresholdTouched = thresholdTouched;
    next.markPriceAfterFixing = currentMarkPrice;
  }
  if (thresholdTouched || deadlinePassed) {
    next.finalResolution = thresholdTouched;
    next.status = "resolved";
    next.hypotheticalPnl = pnlUsd(trade.entryPrice, thresholdTouched ? 1 : 0, trade.sizeUsd);
    next.brierScore = (trade.pTouchByDeadline - (thresholdTouched ? 1 : 0)) ** 2;
    return next;
  }
  if (currentMarkPrice !== null) {
    next.hypotheticalPnl = pnlUsd(trade.entryPrice, currentMarkPrice, trade.sizeUsd);
  }
  return JSON.stringify(next) === JSON.stringify(trade) ? trade : next;
}

function paperMetrics(trades: ForecastPaperTrade[], openedThisRun: number, updatedThisRun: number): ForecastPaperMetrics {
  const resolved = trades.filter((trade) => trade.status === "resolved");
  const briers = resolved.flatMap((trade) => trade.brierScore === null ? [] : [trade.brierScore]);
  const buckets = ["0.50-0.60", "0.60-0.70", "0.70-0.80", "0.80-0.90", "other"];
  return {
    totalTrades: trades.length,
    openTrades: trades.filter((trade) => trade.status === "open").length,
    resolvedTrades: resolved.length,
    openedThisRun,
    updatedThisRun,
    totalHypotheticalPnl: round4(trades.reduce((sum, trade) => sum + (trade.hypotheticalPnl ?? 0), 0)),
    averageBrierScore: briers.length ? round4(mean(briers)) : null,
    calibration: buckets.map((bucket) => calibrationRow(bucket, trades.filter((trade) => trade.calibrationBucket === bucket))),
    proofBeforeLive: {
      minimumEntries: 30,
      currentEntries: trades.length,
      readyForLive: false,
      requirements: [
        "30+ paper forecast entries",
        "positive simulated EV after spread/slippage",
        "reasonable Brier/calibration by bucket",
        "no parser/source errors",
        "no hidden stale-source false positives",
      ],
    },
  };
}

function calibrationRow(bucket: string, trades: ForecastPaperTrade[]): ForecastPaperMetrics["calibration"][number] {
  const resolved = trades.filter((trade) => trade.status === "resolved");
  const briers = resolved.flatMap((trade) => trade.brierScore === null ? [] : [trade.brierScore]);
  return {
    bucket,
    count: trades.length,
    resolved: resolved.length,
    wins: resolved.filter((trade) => trade.finalResolution === true).length,
    averagePredicted: trades.length ? round4(mean(trades.map((trade) => trade.pTouchByDeadline))) : 0,
    averageBrierScore: briers.length ? round4(mean(briers)) : null,
    hypotheticalPnl: round4(trades.reduce((sum, trade) => sum + (trade.hypotheticalPnl ?? 0), 0)),
  };
}

function openingKey(marketSlug: string, sourceDate: string | undefined): string {
  return `${marketSlug}:${sourceDate ?? "unknown"}`;
}

function calibrationBucket(probability: number): string {
  if (probability >= 0.5 && probability < 0.6) return "0.50-0.60";
  if (probability >= 0.6 && probability < 0.7) return "0.60-0.70";
  if (probability >= 0.7 && probability < 0.8) return "0.70-0.80";
  if (probability >= 0.8 && probability < 0.9) return "0.80-0.90";
  return "other";
}

function pnlUsd(entryPrice: number, markPrice: number, sizeUsd: number): number {
  if (entryPrice <= 0) return 0;
  return round4(((markPrice - entryPrice) * sizeUsd) / entryPrice);
}

function emptyState(): ForecastPaperState {
  return {
    version: 1,
    updatedAt: new Date(0).toISOString(),
    trades: [],
  };
}

function mean(values: number[]): number {
  if (!values.length) return 0;
  return values.reduce((sum, value) => sum + value, 0) / values.length;
}

function round4(value: number): number {
  return Math.round(value * 10_000) / 10_000;
}
