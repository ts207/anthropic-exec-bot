import "dotenv/config";

import { fileURLToPath } from "node:url";
import { join } from "node:path";
import { appendJsonl } from "./logging.ts";
import { loadStrategyConfig, type LoadedStrategyConfig } from "./strategy/valuationConfig.ts";
import { fetchNpmEvidence, withEligibleMax } from "./strategy/npmValuationSource.ts";
import { fetchGammaEvent, parseValuationLegs } from "./strategy/marketParser.ts";
import { fetchBookQuote } from "./strategy/orderbookSource.ts";
import { decideThresholdLeg } from "./strategy/valuationDecision.ts";
import { curveMonotonicityCandidates } from "./strategy/curveArbitrage.ts";
import { calendarDominanceCandidates } from "./strategy/calendarArbitrage.ts";
import { rankingAlertCandidates } from "./strategy/rankingSimulator.ts";
import { buildImpliedCurves } from "./strategy/impliedCurve.ts";
import { executeCandidate, postedProbe } from "./strategy/betaExecution.ts";
import { hasLiveAck, isCandidateLocked, listLocks, liveAckPath, probePath, readJson, writeJson, writeLiveAck, type CandidateLock } from "./strategy/stateStore.ts";
import { validatePostedProbeForCandidate } from "./strategy/probeValidation.ts";
import { buildMarketAuditRow, monotonicityAudits, type MarketAuditRow, type MonotonicityAudit } from "./strategy/marketAudit.ts";
import { fixingWatchSnapshotPath, fixingWatchStatePath, parseFixingWatchSnapshot, parseFixingWatchState, updateFixingWatch } from "./strategy/fixingWatch.ts";
import { buildNpmBarrierForecasts, buildSourceFreshnessSnapshot, parseSourceFreshnessSnapshot, sourceFreshnessMap, type ForecastAuditRow, type SourceFreshnessSnapshot } from "./strategy/npmBarrierForecast.ts";
import { forecastPaperPath, parseForecastPaperState, updateForecastPaperTrades } from "./strategy/forecastPaper.ts";
import { parseAutomationPhase, type AutomationTask } from "./strategy/automationSchedule.ts";
import { runAutomationCycle } from "./strategy/valuationAutomation.ts";
import { buildDailyReport } from "./strategy/dailyReport.ts";
import { acquireAutomationLock, writeAutomationHeartbeat } from "./strategy/automationRuntime.ts";
import { buildLadderEntryPlans, type EntryPlan } from "./strategy/valuationLadderEntries.ts";
import { discoverValuationUniverse } from "./strategy/valuationUniverseDiscovery.ts";
import { ladderPaperPath, parseLadderPaperState, updateLadderPaperOrders } from "./strategy/ladderPaper.ts";
import type { ImpliedCurve } from "./strategy/impliedCurve.ts";
import type { BookQuote, CurvePoint, EventConfig, NpmEvidence, StrategyConfig, ValuationCandidate, ValuationLeg } from "./strategy/signalTypes.ts";

type Command = "scan" | "run" | "preflight" | "probe" | "ack" | "audit" | "curve-audit" | "market-audit" | "fixing-watch" | "forecast-audit" | "forecast-paper" | "daily-report" | "auto" | "discover" | "entry-audit" | "ladder-paper";

type ScanResult = {
  evidence: NpmEvidence[];
  legs: ValuationLeg[];
  candidates: ValuationCandidate[];
};

type ValuationState = {
  evidenceByCompany: Map<string, NpmEvidence>;
  allLegs: ValuationLeg[];
  quotes: Map<string, BookQuote>;
  curvePoints: CurvePoint[];
  thresholdCandidates: ValuationCandidate[];
};

async function main(): Promise<void> {
  const { command, args } = parseCli(process.argv.slice(2));
  const configPath = args.get("config") ?? "configs/private-valuations-july31.json";
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

export async function scanOnce(loaded: LoadedStrategyConfig): Promise<ScanResult> {
  const { config } = loaded;
  const state = await collectValuationState(config);

  const rankingLegs = state.allLegs.filter((leg) => leg.eventKind === "ranking");
  const rawCandidates = rankCandidates([
    ...state.thresholdCandidates,
    ...curveMonotonicityCandidates(state.curvePoints, state.quotes, config),
    ...calendarDominanceCandidates(state.curvePoints, state.quotes, config),
    ...rankingAlertCandidates(rankingLegs, state.evidenceByCompany, state.quotes, config, buildImpliedCurves(state.curvePoints)),
  ]);
  const candidates = rankCandidates(applyCaps(config, rawCandidates, await listLocks(config)));

  const ranked = rankCandidates(candidates);
  for (const candidate of ranked) {
    await appendJsonl(join(config.logsDir, "decisions.jsonl"), candidate);
    if (candidate.status === "candidate" || candidate.status === "alert") {
      const execution = await executeCandidate(candidate, config, loaded.hash);
      await appendJsonl(join(config.logsDir, "orders.jsonl"), {
        candidate,
        execution,
      });
    }
  }
  await writeJson(join(config.stateDir, "last_candidates.json"), ranked);
  return { evidence: [...state.evidenceByCompany.values()], legs: state.allLegs, candidates: ranked };
}

async function collectValuationState(config: StrategyConfig): Promise<ValuationState> {
  const evidenceByCompany = await loadEvidence(config);
  const allLegs: ValuationLeg[] = [];
  const quotes = new Map<string, BookQuote>();
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
      if (leg.threshold !== undefined && quote?.bestAsk !== null && quote?.bestAsk !== undefined) {
        curvePoints.push({ leg, yesAsk: quote.bestAsk });
      }
      if (leg.eventKind === "threshold") {
        const rawEvidence = evidenceByCompany.get(leg.company);
        const evidence = rawEvidence ? withEligibleMax(rawEvidence, leg.marketWindowStartIso, leg.deadlineIso) : undefined;
        const locked = await isCandidateLocked(config, candidateShell(leg));
        const decision = decideThresholdLeg(leg, evidence, quote, config, locked);
        thresholdCandidates.push(decision);
      }
    }
  }

  return { evidenceByCompany, allLegs, quotes, curvePoints, thresholdCandidates };
}

function scanSummary(result: ScanResult): Record<string, unknown> {
  const candidates = result.candidates.filter((candidate) => candidate.status === "candidate");
  const alerts = result.candidates.filter((candidate) => candidate.status === "alert");
  return {
    evidenceCount: result.evidence.length,
    legCount: result.legs.length,
    candidateCount: candidates.length,
    alertCount: alerts.length,
    topCandidates: candidates.slice(0, 10).map(compactCandidate),
    topAlerts: alerts.slice(0, 10).map(compactCandidate),
  };
}

function compactCandidate(candidate: ValuationCandidate): Record<string, unknown> {
  return {
    signalType: candidate.signalType,
    status: candidate.status,
    company: candidate.company,
    marketSlug: candidate.marketSlug,
    deadline: candidate.deadline,
    threshold: candidate.threshold,
    yesAsk: candidate.yesAsk,
    distancePct: candidate.distancePct,
    fairPrice: candidate.fairPrice,
    edge: candidate.edge,
    edgeScore: candidate.edgeScore,
    confidenceScore: candidate.confidenceScore,
    orderUsd: candidate.orderUsd,
    orderTemplate: candidate.orderTemplate,
    liveAllowed: candidate.liveAllowed,
    reason: candidate.reason,
  };
}

export async function curveAudit(loaded: LoadedStrategyConfig, args: Map<string, string> = new Map()): Promise<Record<string, unknown>> {
  const config = loaded.config;
  const strict = args.get("strict") === "true" || args.get("strict") === "1";
  const state = await collectValuationState(config);
  const curves = buildImpliedCurves(state.curvePoints);
  const rankingLegs = state.allLegs.filter((leg) => leg.eventKind === "ranking");
  const monotonicity = monotonicityAudits(state.curvePoints, state.quotes, config);
  const strictMonotonicity = monotonicity.filter((violation) => violation.violationTier === "HARD_CROSS_MARKET_BID_VIOLATION");
  const calendar = calendarDominanceCandidates(state.curvePoints, state.quotes, config);
  const ranking = rankingAlertCandidates(rankingLegs, state.evidenceByCompany, state.quotes, config, curves);
  const crossed = state.thresholdCandidates.filter((candidate) => (
    candidate.signalType === "SOURCE_CONFIRMED_YES" && candidate.status === "candidate"
  ));
  const marketRows = await buildMarketAuditRows(loaded, state);
  const strictCrossed = marketRows.filter((row) => row.crossedQuality === "SOURCE_CONFIRMED_AND_STALE" && row.tradeBand !== "ignore");
  const strictCrossedSlugs = new Set(strictCrossed.map((row) => row.marketSlug));
  const nestedMonotonicity = strict ? strictMonotonicity : monotonicity;
  const nestedCrossed = strict ? crossed.filter((candidate) => strictCrossedSlugs.has(candidate.marketSlug)) : crossed;
  const report = {
    ok: true,
    generatedAt: new Date().toISOString(),
    mode: config.mode,
    strict,
    liveEligible: false,
    livePolicy: "relative_value_audit_is_alert_only",
    summary: {
      curveCount: curves.length,
      curvePointCount: state.curvePoints.length,
      monotonicityViolationCount: monotonicity.length,
      hardMonotonicityCount: strictMonotonicity.length,
      softMonotonicityCount: monotonicity.filter((violation) => violation.violationTier === "SOFT_MID_VIOLATION" || violation.violationTier === "SOFT_ASK_ONLY_VIOLATION").length,
      staleViolationCount: monotonicity.filter((violation) => violation.violationTier === "STALE_BOOK_VIOLATION").length,
      calendarViolationCount: calendar.length,
      crossedLegOpportunityCount: crossed.length,
      strictCrossedLegCount: strictCrossed.length,
      rankingContradictionCount: ranking.length,
      tradeableCandidateCount: strictMonotonicity.length + strictCrossed.length,
    },
    curves: curves.map((curve) => curveAuditRow(curve, nestedMonotonicity, [...calendar, ...nestedCrossed])),
    monotonicityViolations: (strict ? strictMonotonicity : monotonicity).map(monotonicityRow),
    calendarViolations: calendar.map(relativeCandidate),
    crossedLegOpportunities: strict
      ? strictCrossed.map(marketRowSummary)
      : crossed.map(relativeCandidate),
    rankingContradictions: ranking.map(relativeCandidate),
  };
  await appendJsonl(join(config.logsDir, "curve_audit.jsonl"), report);
  await writeJson(join(config.stateDir, "last_curve_audit.json"), report);
  return report;
}

function curveAuditRow(curve: ImpliedCurve, monotonicity: MonotonicityAudit[], candidates: ValuationCandidate[]): Record<string, unknown> {
  const curveCandidates = candidates
    .filter((candidate) => candidate.company === curve.company && candidate.deadline === curve.deadlineIso)
    .sort((left, right) => right.edge - left.edge);
  const curveViolations = monotonicity.filter((violation) => violation.company === curve.company && violation.deadline === curve.deadlineIso);
  return {
    company: curve.company,
    deadline: curve.deadlineIso,
    medianValuation: curve.medianValuation,
    expectedValuation: curve.expectedValuation,
    curvePoints: curve.points.map((point) => ({
      threshold: point.leg.threshold,
      yesAsk: point.yesAsk,
      marketSlug: point.leg.marketSlug,
      label: point.leg.label,
    })),
    monotonicityViolations: curveViolations.map(monotonicityRow),
    calendarViolations: curveCandidates.filter((candidate) => candidate.signalType === "CALENDAR_DOMINANCE_YES").map(relativeCandidate),
    crossedLegs: curveCandidates.filter((candidate) => candidate.signalType === "SOURCE_CONFIRMED_YES").map(relativeCandidate),
    bestUnderpricedYesLeg: curveCandidates[0] ? relativeCandidate(curveCandidates[0]) : null,
    liveEligible: false,
  };
}

function monotonicityRow(violation: MonotonicityAudit): Record<string, unknown> {
  return {
    company: violation.company,
    deadline: violation.deadline,
    lowerMarketSlug: violation.lowerMarketSlug,
    higherMarketSlug: violation.higherMarketSlug,
    lowerThreshold: violation.lowerThreshold,
    higherThreshold: violation.higherThreshold,
    lowerYesAsk: violation.lowerYesAsk,
    lowerYesBid: violation.lowerYesBid,
    higherYesAsk: violation.higherYesAsk,
    higherYesBid: violation.higherYesBid,
    bidBackedEdge: violation.bidBackedEdge,
    midEdge: violation.midEdge,
    askOnlyEdge: violation.askOnlyEdge,
    bookAgeMs: violation.bookAgeMs,
    sameRuleHashFamily: violation.sameRuleHashFamily,
    sameDirectionSemantics: violation.sameDirectionSemantics,
    violationTier: violation.violationTier,
    tradeableBuyOnly: violation.tradeableBuyOnly,
    reason: violation.reason,
    liveEligible: false,
  };
}

function relativeCandidate(candidate: ValuationCandidate): Record<string, unknown> {
  return {
    signalType: candidate.signalType,
    company: candidate.company,
    eventSlug: candidate.eventSlug,
    marketSlug: candidate.marketSlug,
    deadline: candidate.deadline,
    threshold: candidate.threshold,
    yesAsk: candidate.yesAsk,
    fairPrice: candidate.fairPrice,
    edgeEstimate: candidate.edge,
    confidence: candidate.confidenceScore,
    reason: candidate.reason,
    pairedMarketSlug: candidate.pairedMarketSlug,
    pairedYesAsk: candidate.pairedYesAsk,
    orderTemplate: candidate.orderTemplate ?? null,
    liveEligible: false,
  };
}

export async function marketAudit(loaded: LoadedStrategyConfig, args: Map<string, string> = new Map()): Promise<Record<string, unknown>> {
  const state = await collectValuationState(loaded.config);
  const rows = await buildMarketAuditRows(loaded, state);
  const strict = args.get("strict") === "true" || args.get("strict") === "1";
  const filteredRows = strict
    ? rows.filter((row) => row.crossedQuality === "SOURCE_CONFIRMED_AND_STALE" && row.tradeBand !== "ignore")
    : rows;
  const companies = [...new Set(filteredRows.map((row) => row.company))].map((company) => {
    const companyRows = filteredRows
      .filter((row) => row.company === company)
      .sort((left, right) => (left.threshold ?? 0) - (right.threshold ?? 0));
    const evidence = state.evidenceByCompany.get(company);
    return {
      company,
      latestNpmValuation: evidence?.latestValuation,
      latestNpmDate: evidence?.latestTapeDate,
      maxEligibleValuation: evidence?.maxEligibleValuation ?? evidence?.latestValuation,
      maxEligibleDate: evidence?.maxEligibleDate ?? evidence?.latestTapeDate,
      thresholdCurve: companyRows.map(marketRowSummary),
      bestStaleCrossedLeg: companyRows
        .filter((row) => row.crossedQuality === "SOURCE_CONFIRMED_AND_STALE")
        .sort((left, right) => right.tradeScore - left.tradeScore)[0] ?? null,
      nearBoundaryWatchlist: companyRows.filter((row) => row.state === "NEAR_BOUNDARY").map(marketRowSummary),
    };
  });
  const report = {
    ok: true,
    generatedAt: new Date().toISOString(),
    mode: loaded.config.mode,
    strict,
    summary: {
      legCount: rows.length,
      outputLegCount: filteredRows.length,
      newlyCrossedCount: rows.filter((row) => row.state === "NEWLY_CROSSED").length,
      previouslyCrossedCount: rows.filter((row) => row.state === "PREVIOUSLY_CROSSED").length,
      strictCrossedLegCount: rows.filter((row) => row.crossedQuality === "SOURCE_CONFIRMED_AND_STALE" && row.tradeBand !== "ignore").length,
      nearBoundaryCount: rows.filter((row) => row.state === "NEAR_BOUNDARY").length,
      ambiguousCount: rows.filter((row) => row.state === "AMBIGUOUS").length,
      tradeableCandidateCount: rows.filter((row) => row.tradeBand === "tradeable" && row.crossedQuality === "SOURCE_CONFIRMED_AND_STALE").length,
    },
    companies,
    rows: filteredRows.map(marketRowSummary),
  };
  await appendJsonl(join(loaded.config.logsDir, "market_audit.jsonl"), report);
  await writeJson(join(loaded.config.stateDir, "last_market_audit.json"), report);
  return report;
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
  const update = updateFixingWatch(rows, previousSnapshot, priorWatchState, new Date(), { replayExisting });
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
  const sizeUsd = Math.max(0.01, Number(args.get("size-usd") ?? 1));
  const { plans, sourceFreshness } = await entryPlanContext(loaded);
  const previous = parseLadderPaperState(await readJson(ladderPaperPath(loaded.config)));
  const update = updateLadderPaperOrders({
    previous,
    plans,
    sizeUsd,
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
    summary: update.metrics,
    opened: update.opened,
    filled: update.filled,
    updated: update.updated,
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

async function buildMarketAuditRows(loaded: LoadedStrategyConfig, state: ValuationState): Promise<MarketAuditRow[]> {
  const candidates = new Map(state.thresholdCandidates.map((candidate) => [candidate.marketSlug, candidate]));
  const rows: MarketAuditRow[] = [];
  for (const leg of state.allLegs.filter((item) => item.eventKind === "threshold")) {
    const rawEvidence = state.evidenceByCompany.get(leg.company);
    const evidence = rawEvidence ? withEligibleMax(rawEvidence, leg.marketWindowStartIso, leg.deadlineIso) : undefined;
    const candidate = candidates.get(leg.marketSlug) ?? candidateShell(leg);
    rows.push(buildMarketAuditRow({
      leg,
      evidence,
      quote: state.quotes.get(leg.marketSlug),
      config: loaded.config,
      liveBlockers: await liveBlockers(candidate, loaded.config, loaded.hash),
    }));
  }
  return rows.sort((left, right) => {
    if (left.company !== right.company) return left.company.localeCompare(right.company);
    return (left.threshold ?? 0) - (right.threshold ?? 0);
  });
}

function marketRowSummary(row: MarketAuditRow): Record<string, unknown> {
  return {
    company: row.company,
    eventSlug: row.eventSlug,
    marketSlug: row.marketSlug,
    threshold: row.threshold,
    deadline: row.deadline,
    label: row.label,
    state: row.state,
    crossedQuality: row.crossedQuality,
    latestValuation: row.latestValuation,
    latestDate: row.latestDate,
    maxEligibleValuation: row.maxEligibleValuation,
    maxEligibleDate: row.maxEligibleDate,
    previousMaxEligibleValuation: row.previousMaxEligibleValuation,
    sourceDateAgeHours: row.sourceDateAgeHours,
    yesAsk: row.yesAsk,
    yesBid: row.yesBid,
    settlementEdge: row.settlementEdge,
    distancePct: row.distancePct,
    depthUnderCap: row.depthUnderCap,
    bookAgeMs: row.bookAgeMs,
    ruleConfidence: row.ruleConfidence,
    tradeScore: row.tradeScore,
    tradeBand: row.tradeBand,
    liveBlockers: row.liveBlockers,
    reason: row.reason,
  };
}

function entryAuditSummary(plans: EntryPlan[]): Record<string, unknown> {
  const counts = plans.reduce<Record<string, number>>((result, plan) => {
    result[plan.entryMode] = (result[plan.entryMode] ?? 0) + 1;
    return result;
  }, {});
  const strictSourceConfirmed = plans.filter((plan) => (
    plan.entryMode === "TAKER_SOURCE_CONFIRMED"
    && !plan.blockers.includes("not_strict_stale_source_confirmed")
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
    sourceDate: plan.sourceDate,
    currentValuation: plan.currentValuation,
    maxEligibleValuation: plan.maxEligibleValuation,
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

export async function auditCandidates(
  loaded: LoadedStrategyConfig,
  args: Map<string, string> = new Map(),
): Promise<Record<string, unknown>> {
  const refresh = args.get("refresh") === "true" || args.get("refresh") === "1";
  const top = Math.max(1, Number(args.get("top") ?? 20));
  const lastCandidatesPath = join(loaded.config.stateDir, "last_candidates.json");
  let source = "last_candidates";
  let candidates: ValuationCandidate[] | null = null;
  if (!refresh) {
    const raw = await readJson(lastCandidatesPath);
    candidates = parseCandidateArray(raw);
  }
  if (!candidates) {
    source = "fresh_scan";
    candidates = (await scanOnce(loaded)).candidates;
  }
  const audited = [];
  for (const candidate of candidates.slice(0, top)) {
    const blockers = await liveBlockers(candidate, loaded.config, loaded.hash);
    audited.push({
      candidate: candidateLabel(candidate),
      signalType: candidate.signalType,
      status: candidate.status,
      company: candidate.company,
      eventSlug: candidate.eventSlug,
      marketSlug: candidate.marketSlug,
      sourceEvidence: {
        valuation: candidate.sourceValuation,
        sourceDate: candidate.sourceDate,
        maxEligibleValuation: candidate.maxEligibleValuation,
        maxEligibleDate: candidate.maxEligibleDate,
      },
      ruleEvidence: {
        ruleHash: candidate.ruleHash,
        threshold: candidate.threshold,
        deadline: candidate.deadline,
      },
      market: {
        yesAsk: candidate.yesAsk,
        bestBid: candidate.bestBid,
        spread: candidate.spread,
        liquidity: candidate.liquidity,
        cap: candidate.maxPrice,
      },
      scores: {
        distancePct: candidate.distancePct,
        confidenceScore: candidate.confidenceScore,
        edgeScore: candidate.edgeScore,
        fairPrice: candidate.fairPrice,
        edge: candidate.edge,
      },
      orderTemplate: candidate.orderTemplate ?? null,
      live: blockers.length === 0 ? "ALLOWED" : "BLOCKED",
      liveBlockers: blockers,
      reason: candidate.reason,
    });
  }
  return {
    ok: true,
    generatedAt: new Date().toISOString(),
    source,
    configHash: loaded.hash,
    mode: loaded.config.mode,
    top,
    candidateCount: candidates.length,
    candidates: audited,
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

async function preflight(loaded: LoadedStrategyConfig): Promise<Record<string, unknown>> {
  const config = loaded.config;
  const liveAck = liveAckPath(config, loaded.hash);
  return {
    ok: true,
    configHash: loaded.hash,
    mode: config.mode,
    liveAckPath: liveAck,
    liveAckPresent: await filePresent(liveAck),
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

export async function liveBlockers(
  candidate: ValuationCandidate,
  config: StrategyConfig,
  configHash: string,
): Promise<string[]> {
  const blockers: string[] = [];
  if (candidate.status !== "candidate") blockers.push(`candidate_status_${candidate.status}`);
  if (config.mode !== "live") blockers.push(`operator_mode_${config.mode}`);
  if (candidate.signalType === "NPM_DRIFT_MODEL_YES") blockers.push("drift_model_alert_only");
  if (candidate.signalType === "RANKING_INCONSISTENCY_ALERT") blockers.push("ranking_market_alert_only");
  if (!["SOURCE_CONFIRMED_YES", "CURVE_MONOTONICITY_YES", "CALENDAR_DOMINANCE_YES"].includes(candidate.signalType)) {
    blockers.push("signal_not_live_enabled");
  }
  if (!candidate.yesTokenId) blockers.push("missing_yes_token");
  if (!candidate.orderTemplate) blockers.push("missing_order_template");
  if (candidate.yesAsk === null || candidate.yesAsk === undefined) blockers.push("missing_yes_ask");
  if (candidate.status === "candidate" && candidate.yesAsk !== null && candidate.yesAsk !== undefined && candidate.yesAsk > candidate.maxPrice) {
    blockers.push("best_ask_above_cap");
  }
  if (candidate.orderUsd <= 0) blockers.push("zero_order_usd");
  if (!await hasLiveAck(config, configHash)) blockers.push("missing_live_config_ack");
  if (await isCandidateLocked(config, candidate)) blockers.push("duplicate_lock");
  const probe = await validatePostedProbeForCandidate(config, candidate);
  if (!probe.ok) blockers.push(...probe.blockers);
  if (process.env.POLYBOT_TS_BRIDGE_ALLOW_POST !== "1") blockers.push("posting_env_not_armed");
  return [...new Set(blockers)];
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

export function applyCaps(
  config: StrategyConfig,
  candidates: ValuationCandidate[],
  locks: CandidateLock[],
): ValuationCandidate[] {
  let globalSpent = locks.reduce((sum, lock) => sum + lock.orderUsd, 0);
  const eventSpent = new Map<string, number>();
  const companySpent = new Map<string, number>();
  const deadlineSpent = new Map<string, number>();
  for (const lock of locks) {
    eventSpent.set(lock.eventSlug, (eventSpent.get(lock.eventSlug) ?? 0) + lock.orderUsd);
    if (lock.company) companySpent.set(lock.company, (companySpent.get(lock.company) ?? 0) + lock.orderUsd);
    if (lock.deadline) deadlineSpent.set(lock.deadline, (deadlineSpent.get(lock.deadline) ?? 0) + lock.orderUsd);
  }
  return candidates.map((candidate) => {
    if (candidate.status !== "candidate" || candidate.orderUsd <= 0) return candidate;
    const spentForEvent = eventSpent.get(candidate.eventSlug) ?? 0;
    const spentForCompany = companySpent.get(candidate.company) ?? 0;
    const spentForDeadline = deadlineSpent.get(candidate.deadline) ?? 0;
    if (globalSpent + candidate.orderUsd > config.globalUsdCap) return capBlocked(candidate, "global_notional_cap_exceeded");
    if (spentForEvent + candidate.orderUsd > config.perEventUsdCap) return capBlocked(candidate, "event_notional_cap_exceeded");
    if (spentForCompany + candidate.orderUsd > config.perCompanyUsdCap) return capBlocked(candidate, "company_notional_cap_exceeded");
    if (spentForDeadline + candidate.orderUsd > config.perDeadlineUsdCap) return capBlocked(candidate, "deadline_notional_cap_exceeded");
    globalSpent += candidate.orderUsd;
    eventSpent.set(candidate.eventSlug, spentForEvent + candidate.orderUsd);
    companySpent.set(candidate.company, spentForCompany + candidate.orderUsd);
    deadlineSpent.set(candidate.deadline, spentForDeadline + candidate.orderUsd);
    return candidate;
  });
}

function capBlocked(candidate: ValuationCandidate, reason: string): ValuationCandidate {
  return { ...candidate, status: "skip", liveAllowed: false, orderUsd: 0, reason };
}

function rankCandidates(candidates: ValuationCandidate[]): ValuationCandidate[] {
  return [...candidates].sort((left, right) => {
    const statusScore = scoreStatus(right.status) - scoreStatus(left.status);
    if (statusScore !== 0) return statusScore;
    return right.edge - left.edge;
  });
}

function scoreStatus(status: ValuationCandidate["status"]): number {
  if (status === "candidate") return 3;
  if (status === "alert") return 2;
  if (status === "no_action") return 1;
  return 0;
}

async function safeQuote(tokenId: string): Promise<BookQuote | undefined> {
  try {
    return await fetchBookQuote(tokenId);
  } catch {
    return undefined;
  }
}

function candidateShell(leg: ValuationLeg): ValuationCandidate {
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
