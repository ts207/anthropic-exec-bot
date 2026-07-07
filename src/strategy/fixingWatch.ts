import { join } from "node:path";
import type { StrategyConfig } from "./signalTypes.ts";
import type { MarketAuditRow } from "./marketAudit.ts";

export type FixingObservation = {
  label: string;
  horizonMs: number;
  observedAt: string;
  yesAsk: number | null;
  yesBid: number | null;
  settlementEdge: number | null;
  depthUnderCap: number;
  bookAgeMs?: number;
  crossedQuality: MarketAuditRow["crossedQuality"];
  tradeBand: MarketAuditRow["tradeBand"];
  staleLiquidity: boolean;
  fakUnderCapWouldFill: boolean;
  liveBlockers: string[];
};

export type FixingCross = {
  key: string;
  firstSeenAt: string;
  company: string;
  eventSlug: string;
  marketSlug: string;
  threshold?: number;
  deadline: string;
  sourceDate?: string;
  maxEligibleValuation?: number;
  previousSnapshotMaxEligibleValuation?: number;
  previousTapeMaxEligibleValuation?: number;
  observations: FixingObservation[];
};

export type FixingWatchState = {
  version: 1;
  updatedAt: string;
  crosses: Record<string, FixingCross>;
};

export type FixingWatchSnapshot = {
  generatedAt: string;
  rows: MarketAuditRow[];
};

export type FixingWatchUpdate = {
  state: FixingWatchState;
  snapshot: FixingWatchSnapshot;
  newCrosses: FixingCross[];
  observationsRecorded: FixingObservation[];
  missedEdgeReport: Array<Record<string, unknown>>;
};

const OBSERVATION_HORIZONS = [
  { label: "first_seen", horizonMs: 0 },
  { label: "plus_5s", horizonMs: 5_000 },
  { label: "plus_30s", horizonMs: 30_000 },
  { label: "plus_2m", horizonMs: 120_000 },
  { label: "plus_10m", horizonMs: 600_000 },
];

export function fixingWatchStatePath(config: StrategyConfig): string {
  return join(config.stateDir, "fixing_watch.json");
}

export function fixingWatchSnapshotPath(config: StrategyConfig): string {
  return join(config.stateDir, "fixing_watch_snapshot.json");
}

export function parseFixingWatchState(raw: unknown): FixingWatchState {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return emptyFixingWatchState();
  const record = raw as Record<string, unknown>;
  const rawCrosses = record.crosses && typeof record.crosses === "object" && !Array.isArray(record.crosses)
    ? record.crosses as Record<string, unknown>
    : {};
  const crosses: Record<string, FixingCross> = {};
  for (const [key, value] of Object.entries(rawCrosses)) {
    const cross = parseCross(key, value);
    if (cross) crosses[key] = cross;
  }
  return {
    version: 1,
    updatedAt: typeof record.updatedAt === "string" ? record.updatedAt : new Date(0).toISOString(),
    crosses,
  };
}

export function parseFixingWatchSnapshot(raw: unknown): FixingWatchSnapshot | null {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return null;
  const record = raw as Record<string, unknown>;
  if (!Array.isArray(record.rows)) return null;
  return {
    generatedAt: typeof record.generatedAt === "string" ? record.generatedAt : new Date(0).toISOString(),
    rows: record.rows.filter((row): row is MarketAuditRow => Boolean(row && typeof row === "object" && "marketSlug" in row)),
  };
}

export function updateFixingWatch(
  rows: MarketAuditRow[],
  previousSnapshot: FixingWatchSnapshot | null,
  priorState: FixingWatchState,
  now = new Date(),
  options: { replayExisting?: boolean; minLiquidity?: number } = {},
): FixingWatchUpdate {
  const nowIso = now.toISOString();
  const state: FixingWatchState = {
    version: 1,
    updatedAt: nowIso,
    crosses: { ...priorState.crosses },
  };
  const previousRows = new Map((previousSnapshot?.rows ?? []).map((row) => [crossKey(row), row]));
  const currentRows = new Map(rows.map((row) => [crossKey(row), row]));
  const newCrosses: FixingCross[] = [];
  const observationsRecorded: FixingObservation[] = [];

  for (const row of rows) {
    const key = crossKey(row);
    if (!isSourceConfirmed(row)) continue;
    const previous = previousRows.get(key);
    const previousMax = previous?.maxEligibleValuation;
    const hasPreviousSnapshot = previousSnapshot !== null;
    const snapshotNewCross = row.threshold !== undefined
      && row.maxEligibleValuation !== undefined
      && row.maxEligibleValuation >= row.threshold
      && hasPreviousSnapshot
      && previousMax !== undefined
      && previousMax < row.threshold;
    const replayNewCross = options.replayExisting === true && row.state === "NEWLY_CROSSED";
    const shouldTrack = replayNewCross || snapshotNewCross;
    if (!shouldTrack || state.crosses[key]) continue;
    const cross: FixingCross = {
      key,
      firstSeenAt: nowIso,
      company: row.company,
      eventSlug: row.eventSlug,
      marketSlug: row.marketSlug,
      threshold: row.threshold,
      deadline: row.deadline,
      sourceDate: row.maxEligibleDate,
      maxEligibleValuation: row.maxEligibleValuation,
      previousSnapshotMaxEligibleValuation: previousMax,
      previousTapeMaxEligibleValuation: row.previousMaxEligibleValuation,
      observations: [],
    };
    state.crosses[key] = cross;
    newCrosses.push(cross);
  }

  for (const cross of Object.values(state.crosses)) {
    const row = currentRows.get(cross.key);
    if (!row) continue;
    for (const horizon of dueHorizons(cross, now)) {
      const observation = buildObservation(row, horizon.label, horizon.horizonMs, nowIso, options.minLiquidity ?? 1);
      cross.observations.push(observation);
      observationsRecorded.push(observation);
    }
  }

  return {
    state,
    snapshot: { generatedAt: nowIso, rows },
    newCrosses,
    observationsRecorded,
    missedEdgeReport: Object.values(state.crosses).map(missedEdgeRow),
  };
}

export function crossKey(row: Pick<MarketAuditRow, "eventSlug" | "marketSlug">): string {
  return `${row.eventSlug}::${row.marketSlug}`;
}

function emptyFixingWatchState(): FixingWatchState {
  return {
    version: 1,
    updatedAt: new Date(0).toISOString(),
    crosses: {},
  };
}

function parseCross(key: string, raw: unknown): FixingCross | null {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return null;
  const record = raw as Record<string, unknown>;
  if (typeof record.firstSeenAt !== "string" || typeof record.company !== "string" || typeof record.marketSlug !== "string") return null;
  const observations = Array.isArray(record.observations)
    ? record.observations.filter((item): item is FixingObservation => Boolean(item && typeof item === "object" && "label" in item))
    : [];
  return {
    key,
    firstSeenAt: record.firstSeenAt,
    company: record.company,
    eventSlug: typeof record.eventSlug === "string" ? record.eventSlug : key.split("::")[0] ?? "",
    marketSlug: record.marketSlug,
    threshold: optionalNumber(record.threshold),
    deadline: typeof record.deadline === "string" ? record.deadline : "",
    sourceDate: typeof record.sourceDate === "string" ? record.sourceDate : undefined,
    maxEligibleValuation: optionalNumber(record.maxEligibleValuation),
    previousSnapshotMaxEligibleValuation: optionalNumber(record.previousSnapshotMaxEligibleValuation),
    previousTapeMaxEligibleValuation: optionalNumber(record.previousTapeMaxEligibleValuation),
    observations,
  };
}

function dueHorizons(cross: FixingCross, now: Date): typeof OBSERVATION_HORIZONS {
  const firstSeen = Date.parse(cross.firstSeenAt);
  if (!Number.isFinite(firstSeen)) return [];
  const recorded = new Set(cross.observations.map((item) => item.label));
  return OBSERVATION_HORIZONS.filter((horizon) => (
    !recorded.has(horizon.label) && now.getTime() - firstSeen >= horizon.horizonMs
  ));
}

function buildObservation(row: MarketAuditRow, label: string, horizonMs: number, observedAt: string, minLiquidity: number): FixingObservation {
  const staleLiquidity = row.crossedQuality === "SOURCE_CONFIRMED_AND_STALE" && row.tradeBand !== "ignore";
  return {
    label,
    horizonMs,
    observedAt,
    yesAsk: row.yesAsk,
    yesBid: row.yesBid,
    settlementEdge: row.settlementEdge,
    depthUnderCap: row.depthUnderCap,
    bookAgeMs: row.bookAgeMs,
    crossedQuality: row.crossedQuality,
    tradeBand: row.tradeBand,
    staleLiquidity,
    fakUnderCapWouldFill: staleLiquidity && row.depthUnderCap >= minLiquidity,
    liveBlockers: row.liveBlockers,
  };
}

function missedEdgeRow(cross: FixingCross): Record<string, unknown> {
  const first = cross.observations.find((item) => item.label === "first_seen");
  const latest = cross.observations.at(-1);
  return {
    company: cross.company,
    eventSlug: cross.eventSlug,
    marketSlug: cross.marketSlug,
    threshold: cross.threshold,
    sourceDate: cross.sourceDate,
    maxEligibleValuation: cross.maxEligibleValuation,
    firstSeenAt: cross.firstSeenAt,
    firstSeenAsk: first?.yesAsk ?? null,
    firstSeenDepthUnderCap: first?.depthUnderCap ?? 0,
    firstSeenFakUnderCapWouldFill: first?.fakUnderCapWouldFill ?? false,
    latestAsk: latest?.yesAsk ?? null,
    latestObservation: latest?.label ?? null,
    repricedByLatest: first?.yesAsk !== undefined && latest?.yesAsk !== undefined && latest.yesAsk !== null && first.yesAsk !== null
      ? latest.yesAsk - first.yesAsk
      : null,
    observationCount: cross.observations.length,
  };
}

function isSourceConfirmed(row: MarketAuditRow): boolean {
  return row.state === "NEWLY_CROSSED" || row.state === "PREVIOUSLY_CROSSED";
}

function optionalNumber(value: unknown): number | undefined {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : undefined;
}
