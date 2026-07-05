import { readFile } from "node:fs/promises";
import { createHash } from "node:crypto";
import type { SignalType, StrategyConfig } from "./signalTypes.ts";

const SIGNALS: SignalType[] = [
  "SOURCE_CONFIRMED_YES",
  "CURVE_MONOTONICITY_YES",
  "CALENDAR_DOMINANCE_YES",
  "RANKING_INCONSISTENCY_ALERT",
  "NPM_DRIFT_MODEL_YES",
  "STALE_SOURCE_ALERT",
  "NO_ACTION",
];

export type LoadedStrategyConfig = {
  path: string;
  rawText: string;
  hash: string;
  config: StrategyConfig;
};

export async function loadStrategyConfig(path: string): Promise<LoadedStrategyConfig> {
  const rawText = await readFile(path, "utf8");
  const parsed = JSON.parse(rawText) as unknown;
  const config = normalizeConfig(parsed);
  return {
    path,
    rawText,
    hash: sha256(rawText),
    config,
  };
}

export function normalizeConfig(value: unknown): StrategyConfig {
  const record = asRecord(value);
  const minimumEdge = asRecord(record.minimumEdge);
  const signalMultipliersInput = asRecord(record.signalMultipliers);
  const signalMultipliers = Object.fromEntries(
    SIGNALS.map((signal) => [signal, numberOr(signalMultipliersInput[signal], defaultSignalMultiplier(signal))]),
  ) as Record<SignalType, number>;

  const config: StrategyConfig = {
    mode: parseMode(record.mode, "alert_only"),
    pollMs: numberOr(record.pollMs, 30_000),
    logsDir: stringOr(record.logsDir, "logs/valuation"),
    stateDir: stringOr(record.stateDir, "data/valuation"),
    orderbookMaxAgeMs: numberOr(record.orderbookMaxAgeMs, 15_000),
    maxSpread: numberOr(record.maxSpread, 0.2),
    minLiquidity: numberOr(record.minLiquidity, 100),
    globalUsdCap: numberOr(record.globalUsdCap, 100),
    perEventUsdCap: numberOr(record.perEventUsdCap, 50),
    baseOrderUsd: numberOr(record.baseOrderUsd, 10),
    defaultMaxYesPrice: numberOr(record.defaultMaxYesPrice, 0.95),
    minimumEdge: {
      sourceConfirmed: numberOr(minimumEdge.sourceConfirmed, 0.03),
      curve: numberOr(minimumEdge.curve, 0.06),
      calendar: numberOr(minimumEdge.calendar, 0.06),
      drift: numberOr(minimumEdge.drift, 0.15),
    },
    signalMultipliers,
    maxYesPriceBySignal: normalizeSignalPriceMap(record.maxYesPriceBySignal),
    events: normalizeEvents(record.events),
    companies: normalizeCompanies(record.companies),
  };

  if (config.pollMs < 5_000) throw new Error("pollMs must be at least 5000");
  if (config.defaultMaxYesPrice <= 0 || config.defaultMaxYesPrice > 1) {
    throw new Error("defaultMaxYesPrice must be in (0, 1]");
  }
  return config;
}

export function strategyConfigHash(config: StrategyConfig): string {
  return sha256(JSON.stringify(config));
}

function normalizeEvents(value: unknown): StrategyConfig["events"] {
  if (!Array.isArray(value) || value.length === 0) throw new Error("events must be a non-empty array");
  return value.map((item) => {
    const record = asRecord(item);
    const kind: "ranking" | "threshold" = record.kind === "ranking" ? "ranking" : "threshold";
    const event = {
      slug: requiredString(record.slug, "event.slug"),
      kind,
      companyName: optionalString(record.companyName),
      deadlineIso: requiredString(record.deadlineIso, "event.deadlineIso"),
      marketWindowStartIso: optionalString(record.marketWindowStartIso),
      ranking: parseRanking(record.ranking),
      mode: record.mode === undefined ? undefined : parseMode(record.mode, "alert_only"),
    };
    if (kind === "threshold" && !event.companyName) throw new Error(`threshold event ${event.slug} needs companyName`);
    if (kind === "ranking" && !event.ranking) throw new Error(`ranking event ${event.slug} needs ranking`);
    return event;
  });
}

function normalizeCompanies(value: unknown): StrategyConfig["companies"] {
  if (!Array.isArray(value) || value.length === 0) throw new Error("companies must be a non-empty array");
  return value.map((item) => {
    const record = asRecord(item);
    const aliases = Array.isArray(record.aliases) ? record.aliases.map(String).filter(Boolean) : undefined;
    return {
      name: requiredString(record.name, "company.name"),
      npmCompanyId: optionalString(record.npmCompanyId),
      aliases,
    };
  });
}

function normalizeSignalPriceMap(value: unknown): StrategyConfig["maxYesPriceBySignal"] {
  const record = asRecord(value);
  const result: StrategyConfig["maxYesPriceBySignal"] = {};
  for (const signal of SIGNALS) {
    if (record[signal] !== undefined) result[signal] = numberOr(record[signal], 0);
  }
  return result;
}

function parseMode(value: unknown, fallback: StrategyConfig["mode"]): StrategyConfig["mode"] {
  if (value === undefined || value === null || value === "") return fallback;
  if (value === "off" || value === "alert_only" || value === "dry_run" || value === "live") return value;
  throw new Error(`invalid operator mode: ${String(value)}`);
}

function parseRanking(value: unknown): 1 | 2 | 3 | undefined {
  if (value === undefined) return undefined;
  if (value === 1 || value === 2 || value === 3) return value;
  throw new Error("ranking must be 1, 2, or 3");
}

function defaultSignalMultiplier(signal: SignalType): number {
  if (signal === "SOURCE_CONFIRMED_YES") return 1;
  if (signal === "CALENDAR_DOMINANCE_YES") return 0.5;
  if (signal === "CURVE_MONOTONICITY_YES") return 0.4;
  if (signal === "NPM_DRIFT_MODEL_YES") return 0.15;
  return 0;
}

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
}

function numberOr(value: unknown, fallback: number): number {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
}

function stringOr(value: unknown, fallback: string): string {
  return typeof value === "string" && value.trim() ? value.trim() : fallback;
}

function optionalString(value: unknown): string | undefined {
  return typeof value === "string" && value.trim() ? value.trim() : undefined;
}

function requiredString(value: unknown, name: string): string {
  if (typeof value !== "string" || !value.trim()) throw new Error(`${name} is required`);
  return value.trim();
}

function sha256(value: string): string {
  return createHash("sha256").update(value).digest("hex");
}
