import { join } from "node:path";
import { appendJsonl } from "../logging.ts";
import { collectValuationState } from "../services/collectValuationState.ts";
import { calendarDominanceCandidates } from "../strategy/calendarArbitrage.ts";
import { curveMonotonicityCandidates } from "../strategy/curveArbitrage.ts";
import { executeCandidate } from "../strategy/betaExecution.ts";
import { buildImpliedCurves } from "../strategy/impliedCurve.ts";
import { rankingAlertCandidates } from "../strategy/rankingSimulator.ts";
import { listLocks, writeJson, type CandidateLock } from "../strategy/stateStore.ts";
import type { LoadedStrategyConfig } from "../strategy/valuationConfig.ts";
import type { NpmEvidence, StrategyConfig, ValuationCandidate, ValuationLeg } from "../strategy/signalTypes.ts";

export type ScanResult = {
  evidence: NpmEvidence[];
  legs: ValuationLeg[];
  candidates: ValuationCandidate[];
};

export async function scanOnce(loaded: LoadedStrategyConfig): Promise<ScanResult> {
  const { config } = loaded;
  const runId = valuationRunId();
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
    await appendJsonl(join(config.logsDir, "decisions.jsonl"), { runId, candidate });
    if (candidate.status === "candidate") {
      const execution = await executeCandidate(candidate, config, loaded.hash);
      const executionLog = { runId, candidate, execution };
      await appendJsonl(
        join(config.logsDir, execution.posted ? "orders.jsonl" : "execution_skips.jsonl"),
        executionLog,
      );
    } else if (candidate.status === "alert") {
      await appendJsonl(join(config.logsDir, "alerts.jsonl"), { runId, candidate });
    }
  }
  await writeJson(join(config.stateDir, "last_candidates.json"), ranked);
  return { evidence: [...state.evidenceByCompany.values()], legs: state.allLegs, candidates: ranked };
}

export function scanSummary(result: ScanResult): Record<string, unknown> {
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

function valuationRunId(): string {
  return `valuation-${new Date().toISOString()}-${process.pid}`;
}

function compactCandidate(candidate: ValuationCandidate): Record<string, unknown> {
  return {
    signalType: candidate.signalType,
    status: candidate.status,
    company: candidate.company,
    marketSlug: candidate.marketSlug,
    deadline: candidate.deadline,
    threshold: candidate.threshold,
    direction: candidate.direction,
    yesAsk: candidate.yesAsk,
    depthUnderCap: candidate.depthUnderCap,
    bookAgeMs: candidate.bookAgeMs,
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
