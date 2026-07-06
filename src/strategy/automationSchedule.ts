export type AutomationPhase =
  | "LOW_FREQUENCY_MONITOR"
  | "PRE_FIXING_PREP"
  | "FIXING_WINDOW"
  | "POST_FIXING_REVIEW";

export type AutomationTask =
  | "forecast-audit"
  | "forecast-paper"
  | "preflight"
  | "market-audit-strict"
  | "fixing-watch"
  | "daily-report"
  | "curve-audit-strict";

export const AUTOMATION_INTERVALS_MS: Record<AutomationPhase, number> = {
  LOW_FREQUENCY_MONITOR: 15 * 60_000,
  PRE_FIXING_PREP: 2 * 60_000,
  FIXING_WINDOW: 15_000,
  POST_FIXING_REVIEW: 2 * 60_000,
};

export const AUTOMATION_PHASE_TASKS: Record<AutomationPhase, AutomationTask[]> = {
  LOW_FREQUENCY_MONITOR: [
    "forecast-audit",
    "forecast-paper",
  ],
  PRE_FIXING_PREP: [
    "preflight",
    "market-audit-strict",
    "forecast-audit",
    "forecast-paper",
  ],
  FIXING_WINDOW: [
    "fixing-watch",
    "market-audit-strict",
    "forecast-paper",
  ],
  POST_FIXING_REVIEW: [
    "fixing-watch",
    "market-audit-strict",
    "forecast-audit",
    "forecast-paper",
    "daily-report",
  ],
};

export function phaseForNow(now: Date, expectedUpdateAt: Date): AutomationPhase {
  const min = (now.getTime() - expectedUpdateAt.getTime()) / 60_000;
  if (min >= -60 && min < -10) return "PRE_FIXING_PREP";
  if (min >= -10 && min <= 10) return "FIXING_WINDOW";
  if (min > 10 && min <= 60) return "POST_FIXING_REVIEW";
  return "LOW_FREQUENCY_MONITOR";
}

export function expectedNpmUpdateAt(now: Date): Date {
  const today = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate(), 17, 0, 0, 0));
  const postWindowEnd = today.getTime() + 60 * 60_000;
  if (now.getTime() <= postWindowEnd) return today;
  return new Date(today.getTime() + 24 * 60 * 60_000);
}

export function parseAutomationPhase(value: string | undefined): AutomationPhase | undefined {
  if (!value) return undefined;
  if (
    value === "LOW_FREQUENCY_MONITOR"
    || value === "PRE_FIXING_PREP"
    || value === "FIXING_WINDOW"
    || value === "POST_FIXING_REVIEW"
  ) return value;
  throw new Error(`invalid automation phase: ${value}`);
}
