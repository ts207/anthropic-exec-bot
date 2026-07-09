import { join } from "node:path";
import { appendJsonl } from "../logging.ts";
import { scanOnce } from "./scan.ts";
import { liveBlockers } from "../execution/liveExecution.ts";
import { candidateShell, collectValuationState, type ValuationState } from "../services/collectValuationState.ts";
import { calendarDominanceCandidates } from "../strategy/calendarArbitrage.ts";
import { buildImpliedCurves, type ImpliedCurve } from "../strategy/impliedCurve.ts";
import { buildMarketAuditRow, monotonicityAudits, type MarketAuditRow, type MonotonicityAudit } from "../strategy/marketAudit.ts";
import { withEligibleMax } from "../strategy/npmValuationSource.ts";
import { rankingAlertCandidates } from "../strategy/rankingSimulator.ts";
import { readJson, writeJson } from "../strategy/stateStore.ts";
import type { LoadedStrategyConfig } from "../strategy/valuationConfig.ts";
import type { ValuationCandidate } from "../strategy/signalTypes.ts";

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
        direction: candidate.direction,
        deadline: candidate.deadline,
      },
      market: {
        yesAsk: candidate.yesAsk,
        bestBid: candidate.bestBid,
        spread: candidate.spread,
        liquidity: candidate.liquidity,
        depthUnderCap: candidate.depthUnderCap,
        bookAgeMs: candidate.bookAgeMs,
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

export async function buildMarketAuditRows(loaded: LoadedStrategyConfig, state: ValuationState): Promise<MarketAuditRow[]> {
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
