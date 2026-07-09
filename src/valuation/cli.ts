import "dotenv/config";

import { fileURLToPath } from "node:url";
import { join } from "node:path";
import { appendJsonl } from "./logging.ts";
import { auditCandidates, buildMarketAuditRows, curveAudit, marketAudit } from "./commands/audit.ts";
import { scanOnce, scanSummary } from "./commands/scan.ts";
import { liveBlockers } from "./execution/liveExecution.ts";
import { collectValuationState } from "./services/collectValuationState.ts";
import { loadStrategyConfig, type LoadedStrategyConfig } from "./strategy/valuationConfig.ts";
import { fetchGammaEvent, parseValuationLegs } from "./strategy/marketParser.ts";
import { fetchBookQuote } from "./strategy/orderbookSource.ts";
import { postedProbe } from "./strategy/betaExecution.ts";
import { liveAckPath, probePath, readJson, writeJson, writeLiveAck } from "./strategy/stateStore.ts";
import { fixingWatchSnapshotPath, fixingWatchStatePath, parseFixingWatchSnapshot, parseFixingWatchState, updateFixingWatch } from "./strategy/fixingWatch.ts";
import { monotonicityAudits } from "./strategy/marketAudit.ts";
import { buildNpmBarrierForecasts, buildSourceFreshnessSnapshot, parseSourceFreshnessSnapshot, sourceFreshnessMap, type ForecastAuditRow, type SourceFreshnessSnapshot } from "./strategy/npmBarrierForecast.ts";
import { forecastPaperPath, parseForecastPaperState, updateForecastPaperTrades } from "./strategy/forecastPaper.ts";
import { expectedNpmUpdateAt, parseAutomationPhase, type AutomationTask } from "./strategy/automationSchedule.ts";
import { runAutomationCycle } from "./strategy/valuationAutomation.ts";
import { buildDailyReport } from "./strategy/dailyReport.ts";
import { acquireAutomationLock, writeAutomationHeartbeat } from "./strategy/automationRuntime.ts";
import { promotionGateSummary } from "./strategy/promotionGates.ts";
import { buildLadderEntryPlans, type EntryPlan } from "./strategy/valuationLadderEntries.ts";
import { discoverValuationUniverse } from "./strategy/valuationUniverseDiscovery.ts";
import { STRATEGY_LADDER_PAPER_SIZE_MULTIPLIERS, ladderPaperPath, parseLadderPaperState, updateLadderPaperOrders } from "./strategy/ladderPaper.ts";
import type { StrategyConfig, ValuationCandidate } from "./strategy/signalTypes.ts";

type Command = "scan" | "run" | "preflight" | "probe" | "ack" | "audit" | "curve-audit" | "market-audit" | "fixing-watch" | "forecast-audit" | "forecast-paper" | "daily-report" | "auto" | "discover" | "entry-audit" | "ladder-paper";

export { applyCaps, scanOnce } from "./commands/scan.ts";
export { liveBlockers } from "./execution/liveExecution.ts";

async function main(): Promise<void> {
  const { command, args } = parseCli(process.argv.slice(2));
  const configPath = args.get("config") ?? "configs/valuation/private-valuations-july31.json";
  const loaded = await loadStrategyConfig(configPath);
  if (command === "ack") {
    const path = await writeLiveAck(loaded.config, loaded.hash);
    return print({ ok: true, configHash: loaded.hash, liveAckPath: path });
  }
  if (command === "preflight") {
    return print(await preflight(loaded));
  }
  if (command === "probe") {
    return print(await runProbe(loaded, args));
  }
  if (command === "audit") {
    return print(await auditCandidates(loaded, args));
  }
  if (command === "curve-audit") {
    return print(await curveAudit(loaded, args));
  }
  if (command === "market-audit") {
    return print(await marketAudit(loaded, args));
  }
  if (command === "fixing-watch") {
    return print(await fixingWatch(loaded, args));
  }
  if (command === "forecast-audit") {
    return print(await forecastAudit(loaded, args));
  }
  if (command === "forecast-paper") {
    return print(await forecastPaper(loaded, args));
  }
  if (command === "discover") {
    return print(await discoverUniverse(loaded, args));
  }
  if (command === "entry-audit") {
    return print(await entryAudit(loaded, args));
  }
  if (command === "ladder-paper") {
    return print(await ladderPaper(loaded, args));
  }
  if (command === "daily-report") {
    return print(await dailyReport(loaded));
  }
  if (command === "auto") {
    return print(await valuationAuto(loaded, args));
  }
  if (command === "run") {
    for (;;) {
      await scanOnce(loaded);
      await sleep(loaded.config.pollMs);
    }
  }
  return print(scanSummary(await scanOnce(loaded)));
}

export async function fixingWatch(loaded: LoadedStrategyConfig, args: Map<string, string> = new Map()): Promise<Record<string, unknown>> {
  const durationMs = Math.max(0, Number(args.get("duration-ms") ?? 0));
  const intervalMs = Math.max(1_000, Number(args.get("interval-ms") ?? loaded.config.pollMs));
  const replayExisting = args.get("replay-existing") === "true" || args.get("replay-existing") === "1";
  const startedAt = Date.now();
  let latest: Record<string, unknown> | undefined;
  do {
    latest = await fixingWatchCycleWithOptions(loaded, replayExisting);
    if (Date.now() - startedAt >= durationMs) break;
    await sleep(intervalMs);
  } while (true);
  return {
    ...latest,
    durationMs,
    intervalMs,
  };
}

async function fixingWatchCycleWithOptions(loaded: LoadedStrategyConfig, replayExisting: boolean): Promise<Record<string, unknown>> {
  const state = await collectValuationState(loaded.config);
  const rows = await buildMarketAuditRows(loaded, state);
  const previousSnapshot = parseFixingWatchSnapshot(await readJson(fixingWatchSnapshotPath(loaded.config)));
  const priorWatchState = parseFixingWatchState(await readJson(fixingWatchStatePath(loaded.config)));
  const update = updateFixingWatch(rows, previousSnapshot, priorWatchState, new Date(), {
    replayExisting,
    minLiquidity: loaded.config.minLiquidity,
  });
  await writeJson(fixingWatchStatePath(loaded.config), update.state);
  await writeJson(fixingWatchSnapshotPath(loaded.config), update.snapshot);
  const report = {
    ok: true,
    generatedAt: update.snapshot.generatedAt,
    mode: loaded.config.mode,
    liveEligible: false,
    livePolicy: "fixing_watch_is_research_only",
    replayExisting,
    seededBaseline: previousSnapshot === null,
    summary: {
      legCount: rows.length,
      sourceConfirmedLegCount: rows.filter((row) => row.state === "NEWLY_CROSSED" || row.state === "PREVIOUSLY_CROSSED").length,
      newCrossCount: update.newCrosses.length,
      trackedCrossCount: Object.keys(update.state.crosses).length,
      observationsRecordedCount: update.observationsRecorded.length,
      staleLiquidityAtDetectionCount: update.newCrosses.filter((cross) => cross.observations.some((obs) => obs.label === "first_seen" && obs.staleLiquidity)).length,
      fakUnderCapWouldFillCount: update.newCrosses.filter((cross) => cross.observations.some((obs) => obs.label === "first_seen" && obs.fakUnderCapWouldFill)).length,
    },
    newCrosses: update.newCrosses.map((cross) => ({
      company: cross.company,
      eventSlug: cross.eventSlug,
      marketSlug: cross.marketSlug,
      threshold: cross.threshold,
      sourceDate: cross.sourceDate,
      maxEligibleValuation: cross.maxEligibleValuation,
      previousSnapshotMaxEligibleValuation: cross.previousSnapshotMaxEligibleValuation,
      previousTapeMaxEligibleValuation: cross.previousTapeMaxEligibleValuation,
      firstSeenAt: cross.firstSeenAt,
      observations: cross.observations,
    })),
    observationsRecorded: update.observationsRecorded,
    missedEdgeReport: update.missedEdgeReport,
  };
  await appendJsonl(join(loaded.config.logsDir, "fixing_watch.jsonl"), report);
  await writeJson(join(loaded.config.stateDir, "last_fixing_watch.json"), report);
  return report;
}

export async function forecastAudit(loaded: LoadedStrategyConfig, args: Map<string, string> = new Map()): Promise<Record<string, unknown>> {
  const top = Math.max(1, Number(args.get("top") ?? 20));
  const { forecasts, sourceFreshness } = await forecastContext(loaded);
  const candidates = forecasts.filter((row) => row.signalType !== "NO_FORECAST_EDGE");
  const report = {
    ok: true,
    generatedAt: new Date().toISOString(),
    mode: loaded.config.mode,
    liveEligible: false,
    livePolicy: "forecast_audit_is_alert_only_until_paper_validated",
    summary: {
      legCount: forecasts.length,
      candidateCount: candidates.length,
      nearBoundaryCount: forecasts.filter((row) => row.state === "NEAR_BOUNDARY").length,
      staleSourceBlockedCount: forecasts.filter((row) => row.reason === "stale_endpoint_blocked" || row.reason === "source_blocked" || row.reason === "source_freshness_unknown").length,
      freshnessStates: Object.values(sourceFreshness.companies).reduce<Record<string, number>>((counts, item) => {
        counts[item.freshnessState] = (counts[item.freshnessState] ?? 0) + 1;
        return counts;
      }, {}),
      maxEdge: forecasts.reduce((max, row) => Math.max(max, row.edge ?? -Infinity), -Infinity),
      minimumProofBeforeLive: {
        forecastCandidates: 30,
        requiresPositiveCalibration: true,
        requiresPositiveSimulatedEvAfterCosts: true,
        requiresNoParserRuleErrors: true,
        requiresOutOfSampleEdge: true,
      },
    },
    sourceFreshness,
    candidates,
    watchlist: forecasts
      .filter((row) => row.state === "NEAR_BOUNDARY" || (row.distancePct !== undefined && row.distancePct >= 0 && row.distancePct <= 0.03))
      .slice(0, top),
    rows: forecasts,
  };
  await appendJsonl(join(loaded.config.logsDir, "forecast_audit.jsonl"), report);
  await writeJson(join(loaded.config.stateDir, "last_forecast_audit.json"), report);
  await writeJson(sourceFreshnessPath(loaded.config), sourceFreshness);
  return report;
}

export async function forecastPaper(loaded: LoadedStrategyConfig, args: Map<string, string> = new Map()): Promise<Record<string, unknown>> {
  const sizeUsd = Math.max(0.01, Number(args.get("size-usd") ?? 1));
  const { forecasts, sourceFreshness } = await forecastContext(loaded);
  const previous = parseForecastPaperState(await readJson(forecastPaperPath(loaded.config)));
  const update = updateForecastPaperTrades({
    previous,
    forecasts,
    sizeUsd,
  });
  await writeJson(forecastPaperPath(loaded.config), update.state);
  await writeJson(sourceFreshnessPath(loaded.config), sourceFreshness);
  const report = {
    ok: true,
    generatedAt: update.state.updatedAt,
    mode: loaded.config.mode,
    liveEligible: false,
    livePolicy: "forecast_paper_is_research_only",
    sizeUsd,
    summary: update.metrics,
    opened: update.opened,
    updated: update.updated,
    openTrades: update.state.trades.filter((trade) => trade.status === "open"),
    resolvedTrades: update.state.trades.filter((trade) => trade.status === "resolved"),
  };
  await appendJsonl(join(loaded.config.logsDir, "forecast_paper.jsonl"), report);
  await writeJson(join(loaded.config.stateDir, "last_forecast_paper.json"), report);
  return report;
}

export async function discoverUniverse(loaded: LoadedStrategyConfig, args: Map<string, string> = new Map()): Promise<Record<string, unknown>> {
  const crawlGamma = args.get("crawl") !== "false" && args.get("crawl") !== "0";
  const maxPages = Number(args.get("max-pages") ?? 50);
  const fetchQuotes = args.get("quotes") !== "false" && args.get("quotes") !== "0";
  const report = await discoverValuationUniverse({
    config: loaded.config,
    crawlGamma,
    maxPages,
    fetchQuotes,
  });
  await appendJsonl(join(loaded.config.logsDir, "discovery.jsonl"), report);
  await writeJson(join(loaded.config.stateDir, "last_discovery.json"), report);
  return report;
}

export async function entryAudit(loaded: LoadedStrategyConfig, args: Map<string, string> = new Map()): Promise<Record<string, unknown>> {
  const top = Math.max(1, Number(args.get("top") ?? 30));
  const { plans, sourceFreshness } = await entryPlanContext(loaded);
  const actionable = plans.filter((plan) => plan.entryMode !== "NO_ENTRY" && plan.entryMode !== "WATCH_ONLY");
  const report = {
    ok: true,
    generatedAt: new Date().toISOString(),
    mode: loaded.config.mode,
    liveEligible: false,
    livePolicy: "entry_audit_is_planning_only_maker_entries_are_paper_until_promoted",
    summary: entryAuditSummary(plans),
    topPlans: plans.slice(0, top).map(entryPlanSummary),
    actionablePlans: actionable.slice(0, top).map(entryPlanSummary),
    plans: plans.map(entryPlanSummary),
  };
  await appendJsonl(join(loaded.config.logsDir, "entry_audit.jsonl"), report);
  await writeJson(join(loaded.config.stateDir, "last_entry_audit.json"), report);
  await writeJson(sourceFreshnessPath(loaded.config), sourceFreshness);
  return report;
}

export async function ladderPaper(loaded: LoadedStrategyConfig, args: Map<string, string> = new Map()): Promise<Record<string, unknown>> {
  const sizeUsd = Math.max(0.01, Number(args.get("size-usd") ?? loaded.config.baseOrderUsd));
  const now = new Date();
  const { plans, sourceFreshness } = await entryPlanContext(loaded);
  const previous = parseLadderPaperState(await readJson(ladderPaperPath(loaded.config)));
  const update = updateLadderPaperOrders({
    previous,
    plans,
    now,
    sizeUsd,
    sizeMultipliers: STRATEGY_LADDER_PAPER_SIZE_MULTIPLIERS,
    nextFixingAt: expectedNpmUpdateAt(now, loaded.config.npmUpdate),
    cancelBeforeFixingMs: 10 * 60_000,
    caps: loaded.config,
  });
  await writeJson(ladderPaperPath(loaded.config), update.state);
  await writeJson(sourceFreshnessPath(loaded.config), sourceFreshness);
  const report = {
    ok: true,
    generatedAt: update.state.updatedAt,
    mode: loaded.config.mode,
    liveEligible: false,
    livePolicy: "ladder_paper_is_research_only_until_passive_fills_prove_ev",
    sizeUsd,
    baseSizeUsd: sizeUsd,
    sizeMultipliers: STRATEGY_LADDER_PAPER_SIZE_MULTIPLIERS,
    summary: update.metrics,
    opened: update.opened,
    filled: update.filled,
    updated: update.updated,
    blocked: update.blocked,
    workingOrders: update.state.orders.filter((order) => order.status === "working"),
    filledOrders: update.state.orders.filter((order) => order.status === "filled"),
    resolvedOrders: update.state.orders.filter((order) => order.status === "resolved"),
  };
  await appendJsonl(join(loaded.config.logsDir, "ladder_paper.jsonl"), report);
  await writeJson(join(loaded.config.stateDir, "last_ladder_paper.json"), report);
  return report;
}

export async function dailyReport(loaded: LoadedStrategyConfig): Promise<Record<string, unknown>> {
  const report = buildDailyReport({
    generatedAt: new Date().toISOString(),
    sourceFreshness: await readJson(sourceFreshnessPath(loaded.config)),
    forecastAudit: await readJson(join(loaded.config.stateDir, "last_forecast_audit.json")),
    forecastPaper: await readJson(join(loaded.config.stateDir, "last_forecast_paper.json")),
    fixingWatch: await readJson(join(loaded.config.stateDir, "last_fixing_watch.json")),
    marketAudit: await readJson(join(loaded.config.stateDir, "last_market_audit.json")),
    curveAudit: await readJson(join(loaded.config.stateDir, "last_curve_audit.json")),
    entryAudit: await readJson(join(loaded.config.stateDir, "last_entry_audit.json")),
    ladderPaper: await readJson(join(loaded.config.stateDir, "last_ladder_paper.json")),
    discovery: await readJson(join(loaded.config.stateDir, "last_discovery.json")),
  });
  await appendJsonl(join(loaded.config.logsDir, "daily_report.jsonl"), report);
  await writeJson(join(loaded.config.stateDir, "last_daily_report.json"), report);
  return report;
}

async function entryPlanContext(loaded: LoadedStrategyConfig): Promise<{
  plans: EntryPlan[];
  sourceFreshness: SourceFreshnessSnapshot;
}> {
  const state = await collectValuationState(loaded.config);
  const marketRows = await buildMarketAuditRows(loaded, state);
  const previousFreshness = parseSourceFreshnessSnapshot(await readJson(sourceFreshnessPath(loaded.config)));
  const sourceFreshness = buildSourceFreshnessSnapshot({
    evidenceByCompany: state.evidenceByCompany,
    previous: previousFreshness,
  });
  const forecasts = buildNpmBarrierForecasts({
    legs: state.allLegs,
    evidenceByCompany: state.evidenceByCompany,
    quotes: state.quotes,
    marketRows,
    sourceFreshnessByCompany: sourceFreshnessMap(sourceFreshness),
    config: loaded.config,
  });
  const monotonicity = monotonicityAudits(state.curvePoints, state.quotes, loaded.config);
  return {
    plans: buildLadderEntryPlans({
      legs: state.allLegs,
      evidenceByCompany: state.evidenceByCompany,
      quotes: state.quotes,
      noQuotes: state.noQuotes,
      marketRows,
      forecasts,
      monotonicity,
      config: loaded.config,
    }),
    sourceFreshness,
  };
}

export async function valuationAuto(loaded: LoadedStrategyConfig, args: Map<string, string> = new Map()): Promise<Record<string, unknown>> {
  const once = args.get("once") === "true" || args.get("once") === "1";
  const dryRun = args.get("dry-run") === "true" || args.get("dry-run") === "1";
  const phaseOverride = parseAutomationPhase(args.get("phase"));
  const lock = dryRun ? null : await acquireAutomationLock(loaded.config);
  let stopping = false;
  const stop = () => {
    stopping = true;
  };
  process.once("SIGINT", stop);
  process.once("SIGTERM", stop);
  let consecutiveFailures = 0;
  try {
    if (once || dryRun) {
      const cycle = await runAutomationCycle({
        phaseOverride,
        dryRun,
        npmUpdate: loaded.config.npmUpdate,
        taskTimeoutMs: loaded.config.automation.taskTimeoutMs,
        runTask: (task) => runAutomationTask(loaded, task),
      });
      await persistAutomationCycle(loaded.config, cycle);
      return cycle;
    }
    while (!stopping) {
      const cycle = await runAutomationCycle({
        phaseOverride,
        dryRun,
        npmUpdate: loaded.config.npmUpdate,
        taskTimeoutMs: loaded.config.automation.taskTimeoutMs,
        runTask: (task) => runAutomationTask(loaded, task),
      });
      consecutiveFailures = cycle.ok ? 0 : consecutiveFailures + 1;
      const sleepMs = cycle.ok
        ? cycle.nextRunInMs
        : Math.min(cycle.nextRunInMs * (2 ** consecutiveFailures), loaded.config.automation.maxBackoffMs);
      const persistedCycle = { ...cycle, nextRunInMs: sleepMs, consecutiveFailures };
      await persistAutomationCycle(loaded.config, persistedCycle);
      console.log(JSON.stringify(persistedCycle));
      await sleepInterruptible(sleepMs, () => stopping);
    }
    return { ok: true, stopped: true };
  } finally {
    process.off("SIGINT", stop);
    process.off("SIGTERM", stop);
    await lock?.release();
  }
}

async function persistAutomationCycle(config: StrategyConfig, cycle: Record<string, unknown> & { alerts?: Array<Record<string, unknown>> }): Promise<void> {
  await appendJsonl(join(config.logsDir, "automation.jsonl"), cycle);
  await writeJson(join(config.stateDir, "last_automation.json"), cycle);
  await writeAutomationHeartbeat(config, {
    phase: cycle.phase,
    ok: cycle.ok,
    nextRunInMs: cycle.nextRunInMs,
    alertCount: cycle.alerts?.length ?? 0,
  });
  if (cycle.alerts?.length) {
    if (config.automation.alertSink === "file" || config.automation.alertSink === "both") {
      for (const alert of cycle.alerts) await appendJsonl(join(config.logsDir, "automation_alerts.jsonl"), alert);
    }
    if (config.automation.alertSink === "console" || config.automation.alertSink === "both") {
      for (const alert of cycle.alerts) console.log(JSON.stringify({ alert }));
    }
  }
}

async function runAutomationTask(loaded: LoadedStrategyConfig, task: AutomationTask): Promise<unknown> {
  if (task === "discover") return discoverUniverse(loaded);
  if (task === "entry-audit") return entryAudit(loaded);
  if (task === "ladder-paper") return ladderPaper(loaded);
  if (task === "forecast-audit") return forecastAudit(loaded);
  if (task === "forecast-paper") return forecastPaper(loaded);
  if (task === "preflight") return preflight(loaded);
  if (task === "market-audit-strict") return marketAudit(loaded, new Map([["strict", "true"]]));
  if (task === "fixing-watch") return fixingWatch(loaded);
  if (task === "daily-report") return dailyReport(loaded);
  if (task === "curve-audit-strict") return curveAudit(loaded, new Map([["strict", "true"]]));
  throw new Error(`unknown automation task: ${task}`);
}

async function forecastContext(loaded: LoadedStrategyConfig): Promise<{
  forecasts: ForecastAuditRow[];
  sourceFreshness: SourceFreshnessSnapshot;
}> {
  const state = await collectValuationState(loaded.config);
  const marketRows = await buildMarketAuditRows(loaded, state);
  const previousFreshness = parseSourceFreshnessSnapshot(await readJson(sourceFreshnessPath(loaded.config)));
  const sourceFreshness = buildSourceFreshnessSnapshot({
    evidenceByCompany: state.evidenceByCompany,
    previous: previousFreshness,
  });
  const forecasts = buildNpmBarrierForecasts({
    legs: state.allLegs,
    evidenceByCompany: state.evidenceByCompany,
    quotes: state.quotes,
    marketRows,
    sourceFreshnessByCompany: sourceFreshnessMap(sourceFreshness),
    config: loaded.config,
  });
  return { forecasts, sourceFreshness };
}

function sourceFreshnessPath(config: StrategyConfig): string {
  return join(config.stateDir, "source_freshness.json");
}

function entryAuditSummary(plans: EntryPlan[]): Record<string, unknown> {
  const counts = plans.reduce<Record<string, number>>((result, plan) => {
    result[plan.entryMode] = (result[plan.entryMode] ?? 0) + 1;
    return result;
  }, {});
  const strictSourceConfirmed = plans.filter((plan) => (
    plan.entryMode === "TAKER_SOURCE_CONFIRMED"
    && plan.liveEligible
  ));
  return {
    planCount: plans.length,
    sourceConfirmedTakerCount: counts.TAKER_SOURCE_CONFIRMED ?? 0,
    strictSourceConfirmedTakerCount: strictSourceConfirmed.length,
    nearBoundaryPassiveBidCount: counts.MAKER_NEAR_BOUNDARY_BID ?? 0,
    farOptionalityBidCount: counts.MAKER_FAR_OPTIONALITY_BID ?? 0,
    curveRepairBidCount: counts.MAKER_CURVE_REPAIR_BID ?? 0,
    rangeSpreadPaperCount: counts.RANGE_SPREAD_PAPER ?? 0,
    watchOnlyCount: counts.WATCH_ONLY ?? 0,
    noEntryCount: counts.NO_ENTRY ?? 0,
    paperEligibleCount: plans.filter((plan) => plan.paperEligible).length,
    liveEligibleCount: plans.filter((plan) => plan.liveEligible).length,
    modeCounts: counts,
  };
}

function entryPlanSummary(plan: EntryPlan): Record<string, unknown> {
  return {
    company: plan.company,
    eventSlug: plan.eventSlug,
    marketSlug: plan.marketSlug,
    threshold: plan.threshold,
    deadline: plan.deadline,
    direction: plan.direction,
    yesTokenId: plan.yesTokenId,
    noTokenId: plan.noTokenId,
    sourceDate: plan.sourceDate,
    currentValuation: plan.currentValuation,
    maxEligibleValuation: plan.maxEligibleValuation,
    sourceConfirmed: plan.sourceConfirmed,
    distancePct: plan.distancePct,
    yesAsk: plan.yesAsk,
    yesBid: plan.yesBid,
    noAsk: plan.noAsk,
    noBid: plan.noBid,
    modelFair: plan.modelFair,
    requiredEdge: plan.requiredEdge,
    passiveBidPrice: plan.passiveBidPrice,
    maxTakerPrice: plan.maxTakerPrice,
    entryMode: plan.entryMode,
    paperEligible: plan.paperEligible,
    liveEligible: plan.liveEligible,
    activation: plan.activation,
    cancelRules: plan.cancelRules,
    blockers: plan.blockers,
    reason: plan.reason,
    pairedMarketSlug: plan.pairedMarketSlug,
    ladderContext: plan.ladderContext,
    range: plan.range,
  };
}

async function preflight(loaded: LoadedStrategyConfig): Promise<Record<string, unknown>> {
  const config = loaded.config;
  const liveAck = liveAckPath(config, loaded.hash);
  const liveAckPresent = await filePresent(liveAck);
  const missingNpmSources = config.companies
    .filter((company) => !company.npmCompanyId)
    .map((company) => company.name);
  const lastCandidates = parseCandidateArray(await readJson(join(config.stateDir, "last_candidates.json"))) ?? [];
  const sourceFreshness = parseSourceFreshnessSnapshot(await readJson(sourceFreshnessPath(config)));
  const runtime = preflightRuntime();
  const warnings = preflightWarnings({
    config,
    liveAckPresent,
    missingNpmSources,
    candidateCount: lastCandidates.length,
    runtime,
  });
  return {
    ok: true,
    configHash: loaded.hash,
    mode: config.mode,
    liveAckPath: liveAck,
    liveAckPresent,
    warnings,
    runtime,
    riskCaps: {
      globalUsdCap: config.globalUsdCap,
      perEventUsdCap: config.perEventUsdCap,
      perCompanyUsdCap: config.perCompanyUsdCap,
      perDeadlineUsdCap: config.perDeadlineUsdCap,
    },
    betaSdkEnv: {
      privateKey: Boolean(process.env.PRIVATE_KEY),
      clobApiKey: Boolean(process.env.CLOB_API_KEY),
      clobSecret: Boolean(process.env.CLOB_SECRET),
      clobPassPhrase: Boolean(process.env.CLOB_PASS_PHRASE),
      postingArmed: process.env.POLYBOT_TS_BRIDGE_ALLOW_POST === "1",
    },
    activeUniverse: {
      companyCount: config.companies.length,
      npmSourceCompanyCount: config.companies.length - missingNpmSources.length,
      missingNpmSourceCompanyCount: missingNpmSources.length,
      missingNpmSourceCompanies: missingNpmSources,
      eventCount: config.events.length,
      thresholdEventCount: config.events.filter((event) => event.kind === "threshold").length,
      rankingEventCount: config.events.filter((event) => event.kind === "ranking").length,
      eventModes: countBy(config.events.map((event) => event.mode ?? config.mode)),
    },
    sourceFreshness: sourceFreshness
      ? {
        generatedAt: sourceFreshness.generatedAt,
        companyCount: Object.keys(sourceFreshness.companies).length,
        states: countBy(Object.values(sourceFreshness.companies).map((item) => item.freshnessState)),
        staleCompanies: Object.values(sourceFreshness.companies)
          .filter((item) => item.freshnessState === "STALE_ENDPOINT" || item.freshnessState === "SOURCE_BLOCKED" || item.freshnessState === "MISSED_EXPECTED_UPDATE")
          .map((item) => ({
            company: item.company,
            freshnessState: item.freshnessState,
            staleBlockReason: item.staleBlockReason ?? null,
          })),
      }
      : null,
    paperToLivePromotionGates: promotionGateSummary(),
    lastCandidates: {
      present: lastCandidates.length > 0,
      count: lastCandidates.length,
      topLiveBlockers: await preflightTopLiveBlockers(lastCandidates, config, loaded.hash),
    },
    companies: config.companies.map((company) => ({
      name: company.name,
      npmCompanyId: company.npmCompanyId ?? null,
      hasNpmSource: Boolean(company.npmCompanyId),
    })),
    events: config.events.map((event) => ({
      slug: event.slug,
      kind: event.kind,
      mode: event.mode ?? config.mode,
      deadlineIso: event.deadlineIso,
    })),
  };
}

function preflightRuntime(): Record<string, unknown> {
  return {
    node: process.version,
    platform: process.platform,
    cwd: process.cwd(),
    tmpdir: process.env.TMPDIR ?? process.env.TEMP ?? process.env.TMP ?? null,
    npmExecPath: process.env.npm_execpath ?? null,
    npmLifecycleEvent: process.env.npm_lifecycle_event ?? null,
    localRunHint: "TMPDIR=/tmp ./node_modules/.bin/tsx src/valuation/cli.ts preflight --config configs/valuation/private-valuations-july31.json",
  };
}

function preflightWarnings(input: {
  config: StrategyConfig;
  liveAckPresent: boolean;
  missingNpmSources: string[];
  candidateCount: number;
  runtime: Record<string, unknown>;
}): string[] {
  const warnings: string[] = [];
  const npmExecPath = String(input.runtime.npmExecPath ?? "");
  const tmpdir = String(input.runtime.tmpdir ?? "");
  if (npmExecPath.includes("/mnt/c/") || npmExecPath.includes("\\") || npmExecPath.includes("Program Files")) {
    warnings.push("windows_npm_detected_from_wsl_use_local_node_or_direct_tsx_with_TMPDIR");
  }
  if (!tmpdir || tmpdir.includes("/mnt/c/") || tmpdir.includes("AppData")) {
    warnings.push("tsx_tmpdir_may_not_support_ipc_use_TMPDIR_/tmp");
  }
  if (input.config.mode === "live" && !input.liveAckPresent) warnings.push("live_mode_missing_config_ack");
  if (input.config.mode !== "live" && process.env.POLYBOT_TS_BRIDGE_ALLOW_POST === "1") {
    warnings.push("posting_env_armed_while_operator_mode_not_live");
  }
  if (input.missingNpmSources.length > 0) warnings.push("companies_missing_npm_sources_limit_ranking_and_relative_value_reliability");
  if (input.candidateCount === 0) warnings.push("no_last_candidates_state_run_scan_or_audit_for_live_blocker_context");
  return warnings;
}

async function preflightTopLiveBlockers(
  candidates: ValuationCandidate[],
  config: StrategyConfig,
  configHash: string,
): Promise<Array<Record<string, unknown>>> {
  const rows: Array<Record<string, unknown>> = [];
  for (const candidate of candidates
    .filter((item) => item.status === "candidate" || item.status === "alert")
    .slice(0, 5)) {
    rows.push({
      candidate: candidateLabel(candidate),
      signalType: candidate.signalType,
      status: candidate.status,
      marketSlug: candidate.marketSlug,
      liveBlockers: await liveBlockers(candidate, config, configHash),
    });
  }
  return rows;
}

function countBy(values: string[]): Record<string, number> {
  const counts: Record<string, number> = {};
  for (const value of values) counts[value] = (counts[value] ?? 0) + 1;
  return counts;
}

async function runProbe(loaded: LoadedStrategyConfig, args: Map<string, string>): Promise<Record<string, unknown>> {
  const marketSlug = requiredArg(args, "market-slug");
  const tokenId = args.get("token-id");
  let selectedToken = tokenId;
  if (!selectedToken) {
    const eventSlug = requiredArg(args, "event-slug");
    const eventConfig = loaded.config.events.find((event) => event.slug === eventSlug);
    if (!eventConfig) throw new Error(`unknown event slug in config: ${eventSlug}`);
    const event = await fetchGammaEvent(eventSlug);
    const leg = parseValuationLegs(event, eventConfig).find((item) => item.marketSlug === marketSlug);
    if (!leg?.yesTokenId) throw new Error(`could not find YES token for market slug ${marketSlug}`);
    selectedToken = leg.yesTokenId;
  }
  const quote = await fetchBookQuote(selectedToken);
  const result = await postedProbe(
    selectedToken,
    quote,
    marketSlug,
    loaded.config,
    Number(args.get("price") ?? 0.001),
    Number(args.get("amount-usd") ?? 1),
  );
  return {
    ok: true,
    marketSlug,
    tokenId: selectedToken,
    probePath: probePath(loaded.config, marketSlug),
    result,
  };
}

function parseCli(argv: string[]): { command: Command; args: Map<string, string> } {
  const first = argv[0];
  const command = first && !first.startsWith("--") ? parseCommand(first) : "scan";
  const rest = first && !first.startsWith("--") ? argv.slice(1) : argv;
  const args = new Map<string, string>();
  for (let i = 0; i < rest.length; i += 1) {
    const item = rest[i];
    if (!item?.startsWith("--")) throw new Error(`unexpected argument: ${item}`);
    const key = item.slice(2);
    const value = rest[i + 1];
    if (!value || value.startsWith("--")) {
      args.set(key, "true");
    } else {
      args.set(key, value);
      i += 1;
    }
  }
  return { command, args };
}

function parseCommand(value: string): Command {
  if (value === "scan" || value === "run" || value === "preflight" || value === "probe" || value === "ack" || value === "audit" || value === "curve-audit" || value === "market-audit" || value === "fixing-watch" || value === "forecast-audit" || value === "forecast-paper" || value === "daily-report" || value === "auto" || value === "discover" || value === "entry-audit" || value === "ladder-paper") return value;
  throw new Error(`unknown valuationStrategy command: ${value}`);
}

function parseCandidateArray(value: unknown): ValuationCandidate[] | null {
  if (!Array.isArray(value)) return null;
  return value.filter((item): item is ValuationCandidate => Boolean(item && typeof item === "object" && "marketSlug" in item));
}

function candidateLabel(candidate: ValuationCandidate): string {
  const threshold = candidate.threshold === undefined ? "" : ` ${formatUsd(candidate.threshold)}`;
  return `${candidate.company}${threshold} ${candidate.deadline.slice(0, 10)}`.trim();
}

function formatUsd(value: number): string {
  if (value >= 1_000_000_000_000) return `$${trimNumber(value / 1_000_000_000_000)}T`;
  if (value >= 1_000_000_000) return `$${trimNumber(value / 1_000_000_000)}B`;
  if (value >= 1_000_000) return `$${trimNumber(value / 1_000_000)}M`;
  return `$${value}`;
}

function trimNumber(value: number): string {
  return value.toFixed(3).replace(/\.?0+$/, "");
}

function requiredArg(args: Map<string, string>, name: string): string {
  const value = args.get(name);
  if (!value?.trim()) throw new Error(`--${name} is required`);
  return value.trim();
}

async function filePresent(path: string): Promise<boolean> {
  return (await import("node:fs/promises")).access(path).then(() => true, () => false);
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function sleepInterruptible(ms: number, shouldStop: () => boolean): Promise<void> {
  return new Promise((resolve) => {
    const started = Date.now();
    const tick = () => {
      if (shouldStop() || Date.now() - started >= ms) {
        resolve();
        return;
      }
      setTimeout(tick, Math.min(1_000, ms));
    };
    tick();
  });
}

function print(value: unknown): void {
  console.log(JSON.stringify(value, null, 2));
}

if (process.argv[1] && fileURLToPath(import.meta.url) === process.argv[1]) {
  main().catch((error: unknown) => {
    console.error(error instanceof Error ? error.stack : error);
    process.exitCode = 1;
  });
}
