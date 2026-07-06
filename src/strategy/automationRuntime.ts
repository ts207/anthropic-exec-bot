import { mkdir, open, readFile, rm, writeFile } from "node:fs/promises";
import { join } from "node:path";
import type { StrategyConfig } from "./signalTypes.ts";

export type AutomationLock = {
  path: string;
  release: () => Promise<void>;
};

export function automationLockPath(config: StrategyConfig): string {
  return join(config.stateDir, "automation.lock");
}

export function automationHeartbeatPath(config: StrategyConfig): string {
  return join(config.stateDir, "automation_heartbeat.json");
}

export async function acquireAutomationLock(config: StrategyConfig): Promise<AutomationLock> {
  const path = automationLockPath(config);
  await mkdir(dirname(path), { recursive: true });
  const payload = {
    pid: process.pid,
    acquiredAt: new Date().toISOString(),
  };
  try {
    const handle = await open(path, "wx");
    await handle.writeFile(`${JSON.stringify(payload, null, 2)}\n`);
    await handle.close();
  } catch (error) {
    if (!isFileExists(error)) throw error;
    const existing = await readLock(path);
    if (!lockIsStale(existing, config.automation.lockTtlMs)) {
      throw new Error(`valuation automation already running: ${path}`);
    }
    await rm(path, { force: true });
    const handle = await open(path, "wx");
    await handle.writeFile(`${JSON.stringify({ ...payload, replacedStaleLock: existing }, null, 2)}\n`);
    await handle.close();
  }
  return {
    path,
    release: async () => {
      await rm(path, { force: true });
    },
  };
}

export async function writeAutomationHeartbeat(config: StrategyConfig, value: unknown): Promise<void> {
  const path = automationHeartbeatPath(config);
  await mkdir(dirname(path), { recursive: true });
  await writeFile(path, `${JSON.stringify({
    updatedAt: new Date().toISOString(),
    pid: process.pid,
    ...asRecord(value),
  }, null, 2)}\n`);
}

async function readLock(path: string): Promise<Record<string, unknown>> {
  try {
    const parsed = JSON.parse(await readFile(path, "utf8")) as unknown;
    return asRecord(parsed);
  } catch {
    return {};
  }
}

function lockIsStale(value: Record<string, unknown>, ttlMs: number): boolean {
  const acquiredAt = typeof value.acquiredAt === "string" ? Date.parse(value.acquiredAt) : Number.NaN;
  return !Number.isFinite(acquiredAt) || Date.now() - acquiredAt > ttlMs;
}

function isFileExists(error: unknown): boolean {
  return Boolean(error && typeof error === "object" && "code" in error && error.code === "EEXIST");
}

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
}

function dirname(path: string): string {
  const index = path.lastIndexOf("/");
  return index <= 0 ? "." : path.slice(0, index);
}
