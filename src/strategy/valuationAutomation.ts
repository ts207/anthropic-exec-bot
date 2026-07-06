import {
  AUTOMATION_INTERVALS_MS,
  AUTOMATION_PHASE_TASKS,
  expectedNpmUpdateAt,
  phaseForNow,
  type AutomationPhase,
  type AutomationTask,
  type NpmUpdateSchedule,
} from "./automationSchedule.ts";

export type AutomationTaskResult = {
  task: AutomationTask;
  ok: boolean;
  dryRun?: boolean;
  result?: unknown;
  error?: string;
  timedOut?: boolean;
};

export type AutomationCycle = {
  ok: boolean;
  generatedAt: string;
  phase: AutomationPhase;
  expectedUpdateAt: string;
  nextRunInMs: number;
  dryRun: boolean;
  tasks: AutomationTask[];
  results: AutomationTaskResult[];
  alerts: Array<Record<string, unknown>>;
};

export async function runAutomationCycle(input: {
  now?: Date;
  phaseOverride?: AutomationPhase;
  dryRun?: boolean;
  npmUpdate?: NpmUpdateSchedule;
  taskTimeoutMs?: number;
  runTask: (task: AutomationTask) => Promise<unknown>;
}): Promise<AutomationCycle> {
  const now = input.now ?? new Date();
  const expectedUpdate = expectedNpmUpdateAt(now, input.npmUpdate);
  const phase = input.phaseOverride ?? phaseForNow(now, expectedUpdate);
  const tasks = AUTOMATION_PHASE_TASKS[phase];
  const results: AutomationTaskResult[] = [];
  for (const task of tasks) {
    if (input.dryRun) {
      results.push({ task, ok: true, dryRun: true });
      continue;
    }
    try {
      results.push({ task, ok: true, result: await withTimeout(input.runTask(task), input.taskTimeoutMs ?? 120_000, task) });
    } catch (error) {
      results.push({
        task,
        ok: false,
        error: error instanceof Error ? error.message : String(error),
        timedOut: error instanceof Error && error.message.includes("timed out"),
      });
    }
  }
  return {
    ok: results.every((result) => result.ok),
    generatedAt: now.toISOString(),
    phase,
    expectedUpdateAt: expectedUpdate.toISOString(),
    nextRunInMs: AUTOMATION_INTERVALS_MS[phase],
    dryRun: input.dryRun === true,
    tasks,
    results,
    alerts: meaningfulAlerts(results),
  };
}

export function meaningfulAlerts(results: AutomationTaskResult[]): Array<Record<string, unknown>> {
  const alerts: Array<Record<string, unknown>> = [];
  for (const item of results) {
    if (!item.ok) {
      alerts.push({ type: "TASK_FAILED", task: item.task, error: item.error });
      continue;
    }
    const result = asRecord(item.result);
    if (item.task === "forecast-paper") {
      const opened = Number(asRecord(result.summary).openedThisRun ?? 0);
      if (opened > 0) alerts.push({ type: "FORECAST_PAPER_OPENED", count: opened });
    }
    if (item.task === "discover") {
      const accessIssues = Array.isArray(result.accessIssues) ? result.accessIssues.length : 0;
      if (accessIssues > 0) alerts.push({ type: "DISCOVERY_ACCESS_ISSUE", count: accessIssues });
    }
    if (item.task === "entry-audit") {
      const summary = asRecord(result.summary);
      const sourceTakers = Number(summary.strictSourceConfirmedTakerCount ?? 0);
      const nearBoundary = Number(summary.nearBoundaryPassiveBidCount ?? 0);
      const rangeSpreads = Number(summary.rangeSpreadPaperCount ?? 0);
      if (sourceTakers > 0) alerts.push({ type: "SOURCE_CONFIRMED_STALE_YES_PLAN", count: sourceTakers });
      if (nearBoundary > 0) alerts.push({ type: "NEAR_BOUNDARY_PASSIVE_BID_PLAN", count: nearBoundary });
      if (rangeSpreads > 0) alerts.push({ type: "RANGE_SPREAD_PAPER_PLAN", count: rangeSpreads });
    }
    if (item.task === "ladder-paper") {
      const summary = asRecord(result.summary);
      const opened = Number(summary.openedThisRun ?? 0);
      const filled = Number(summary.filledThisRun ?? 0);
      if (opened > 0) alerts.push({ type: "LADDER_PAPER_OPENED", count: opened });
      if (filled > 0) alerts.push({ type: "LADDER_PAPER_FILLED", count: filled });
    }
    if (item.task === "fixing-watch") {
      const summary = asRecord(result.summary);
      const newCross = Number(summary.newCrossCount ?? 0);
      if (newCross > 0) alerts.push({ type: "NEWLY_CROSSED_BARRIER", count: newCross });
    }
    if (item.task === "market-audit-strict") {
      const strictCrossed = Number(asRecord(result.summary).strictCrossedLegCount ?? 0);
      if (strictCrossed > 0) alerts.push({ type: "STRICT_STALE_CROSSED_LEG", count: strictCrossed });
    }
    if (item.task === "curve-audit-strict") {
      const hard = Number(asRecord(result.summary).hardMonotonicityCount ?? 0);
      if (hard > 0) alerts.push({ type: "HARD_CURVE_VIOLATION", count: hard });
    }
    if (item.task === "forecast-audit") {
      const freshness = asRecord(asRecord(result.summary).freshnessStates);
      const stale = Number(freshness.STALE_ENDPOINT ?? 0);
      const blocked = Number(freshness.SOURCE_BLOCKED ?? 0);
      if (stale > 0 || blocked > 0) alerts.push({ type: "SOURCE_FRESHNESS_PROBLEM", stale, blocked });
    }
    if (item.task === "daily-report") {
      alerts.push({ type: "DAILY_REPORT", summary: result.summary ?? null });
    }
  }
  return alerts;
}

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
}

function withTimeout<T>(promise: Promise<T>, timeoutMs: number, task: AutomationTask): Promise<T> {
  let timeout: NodeJS.Timeout | undefined;
  const timeoutPromise = new Promise<never>((_, reject) => {
    timeout = setTimeout(() => reject(new Error(`automation task timed out: ${task}`)), timeoutMs);
  });
  return Promise.race([promise, timeoutPromise]).finally(() => {
    if (timeout) clearTimeout(timeout);
  });
}
