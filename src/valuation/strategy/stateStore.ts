import { mkdir, open, readFile, readdir, rename, rm, writeFile } from "node:fs/promises";
import { join } from "node:path";
import type { StrategyConfig, ValuationCandidate } from "./signalTypes.ts";

export type CandidateLock = {
  eventSlug: string;
  marketSlug: string;
  company?: string;
  deadline?: string;
  orderUsd: number;
};

export async function writeJson(path: string, value: unknown): Promise<void> {
  await mkdir(dirname(path), { recursive: true });
  const tmpPath = `${path}.${process.pid}.${Date.now()}.tmp`;
  try {
    await writeFile(tmpPath, `${JSON.stringify(value, null, 2)}\n`);
    await rename(tmpPath, path);
  } catch (error) {
    await rm(tmpPath, { force: true });
    throw error;
  }
}

export async function readJson(path: string): Promise<unknown | null> {
  try {
    return JSON.parse(await readFile(path, "utf8")) as unknown;
  } catch (error) {
    if (error && typeof error === "object" && "code" in error && error.code === "ENOENT") return null;
    throw error;
  }
}

export function candidateLockPath(config: StrategyConfig, candidate: ValuationCandidate): string {
  return join(
    config.stateDir,
    "locks",
    safe(candidate.eventSlug),
    `${safe(candidate.marketSlug)}-BUY_YES.json`,
  );
}

export async function isCandidateLocked(config: StrategyConfig, candidate: ValuationCandidate): Promise<boolean> {
  return (await readJson(candidateLockPath(config, candidate))) !== null;
}

export async function claimCandidateLock(config: StrategyConfig, candidate: ValuationCandidate): Promise<boolean> {
  const path = candidateLockPath(config, candidate);
  await mkdir(dirname(path), { recursive: true });
  let handle: Awaited<ReturnType<typeof open>> | undefined;
  try {
    handle = await open(path, "wx");
    await handle.writeFile(`${JSON.stringify(lockRecord(candidate, {
      status: "pending",
      pid: process.pid,
    }), null, 2)}\n`);
    return true;
  } catch (error) {
    if (error && typeof error === "object" && "code" in error && error.code === "EEXIST") return false;
    throw error;
  } finally {
    await handle?.close();
  }
}

export async function lockCandidate(config: StrategyConfig, candidate: ValuationCandidate, order: unknown): Promise<void> {
  await writeJson(candidateLockPath(config, candidate), lockRecord(candidate, {
    status: "posted",
    order,
  }));
}

export async function releaseCandidateLock(config: StrategyConfig, candidate: ValuationCandidate): Promise<void> {
  await rm(candidateLockPath(config, candidate), { force: true });
}

export function liveAckPath(config: StrategyConfig, configHash: string): string {
  return join(config.stateDir, "live_ack", `${configHash}.json`);
}

export function probePath(config: StrategyConfig, marketSlug: string): string {
  return join(config.stateDir, "probe", `${safe(marketSlug)}.json`);
}

export async function hasLiveAck(config: StrategyConfig, configHash: string): Promise<boolean> {
  return (await readJson(liveAckPath(config, configHash))) !== null;
}

export async function writeLiveAck(config: StrategyConfig, configHash: string): Promise<string> {
  const path = liveAckPath(config, configHash);
  await writeJson(path, { acknowledgedAt: new Date().toISOString(), configHash });
  return path;
}

export async function listLocks(config: StrategyConfig): Promise<CandidateLock[]> {
  const root = join(config.stateDir, "locks");
  const entries: CandidateLock[] = [];
  let events: string[] = [];
  try {
    events = await readdir(root);
  } catch (error) {
    if (error && typeof error === "object" && "code" in error && error.code === "ENOENT") return entries;
    throw error;
  }
  for (const eventSlug of events) {
    const files = await readdir(join(root, eventSlug));
    for (const file of files) {
      const raw = await readJson(join(root, eventSlug, file));
      const record = raw && typeof raw === "object" ? raw as Record<string, unknown> : {};
      const recordEventSlug = typeof record.eventSlug === "string" ? record.eventSlug : eventSlug;
      entries.push({
        eventSlug: recordEventSlug,
        marketSlug: String(record.marketSlug ?? file.replace(/-BUY_YES\.json$/, "")),
        company: typeof record.company === "string" ? record.company : config.events.find((event) => event.slug === recordEventSlug)?.companyName,
        deadline: typeof record.deadline === "string" ? record.deadline : config.events.find((event) => event.slug === recordEventSlug)?.deadlineIso,
        orderUsd: Number(record.orderUsd ?? 0),
      });
    }
  }
  return entries;
}

function safe(value: string): string {
  return value.replace(/[^a-zA-Z0-9._-]+/g, "_");
}

function lockRecord(candidate: ValuationCandidate, extra: Record<string, unknown>): Record<string, unknown> {
  return {
    lockedAt: new Date().toISOString(),
    eventSlug: candidate.eventSlug,
    marketSlug: candidate.marketSlug,
    company: candidate.company,
    deadline: candidate.deadline,
    signalType: candidate.signalType,
    orderUsd: candidate.orderUsd,
    maxPrice: candidate.maxPrice,
    ...extra,
  };
}

function dirname(path: string): string {
  const index = path.lastIndexOf("/");
  return index <= 0 ? "." : path.slice(0, index);
}
