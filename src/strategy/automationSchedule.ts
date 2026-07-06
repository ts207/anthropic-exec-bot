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

export type NpmUpdateSchedule = {
  timeZone: string;
  hour: number;
  minute: number;
};

export function phaseForNow(now: Date, expectedUpdateAt: Date): AutomationPhase {
  const min = (now.getTime() - expectedUpdateAt.getTime()) / 60_000;
  if (min >= -60 && min < -10) return "PRE_FIXING_PREP";
  if (min >= -10 && min <= 10) return "FIXING_WINDOW";
  if (min > 10 && min <= 60) return "POST_FIXING_REVIEW";
  return "LOW_FREQUENCY_MONITOR";
}

export function expectedNpmUpdateAt(
  now: Date,
  schedule: NpmUpdateSchedule = { timeZone: "America/New_York", hour: 13, minute: 0 },
): Date {
  const local = zonedParts(now, schedule.timeZone);
  const today = zonedTimeToUtc({
    year: local.year,
    month: local.month,
    day: local.day,
    hour: schedule.hour,
    minute: schedule.minute,
  }, schedule.timeZone);
  const postWindowEnd = today.getTime() + 60 * 60_000;
  if (now.getTime() <= postWindowEnd) return today;
  const tomorrow = new Date(Date.UTC(local.year, local.month - 1, local.day + 1, schedule.hour, schedule.minute, 0, 0));
  const nextLocal = zonedParts(tomorrow, "UTC");
  return zonedTimeToUtc({
    year: nextLocal.year,
    month: nextLocal.month,
    day: nextLocal.day,
    hour: schedule.hour,
    minute: schedule.minute,
  }, schedule.timeZone);
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

function zonedTimeToUtc(
  parts: { year: number; month: number; day: number; hour: number; minute: number },
  timeZone: string,
): Date {
  let utc = Date.UTC(parts.year, parts.month - 1, parts.day, parts.hour, parts.minute, 0, 0);
  for (let i = 0; i < 3; i += 1) {
    const actual = zonedParts(new Date(utc), timeZone);
    const desiredAsUtc = Date.UTC(parts.year, parts.month - 1, parts.day, parts.hour, parts.minute, 0, 0);
    const actualAsUtc = Date.UTC(actual.year, actual.month - 1, actual.day, actual.hour, actual.minute, 0, 0);
    const delta = desiredAsUtc - actualAsUtc;
    if (delta === 0) break;
    utc += delta;
  }
  return new Date(utc);
}

function zonedParts(date: Date, timeZone: string): { year: number; month: number; day: number; hour: number; minute: number } {
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).formatToParts(date);
  const get = (type: string) => Number(parts.find((part) => part.type === type)?.value ?? 0);
  const rawHour = get("hour");
  return {
    year: get("year"),
    month: get("month"),
    day: get("day"),
    hour: rawHour === 24 ? 0 : rawHour,
    minute: get("minute"),
  };
}
