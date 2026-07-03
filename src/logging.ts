import { appendFile, mkdir } from "node:fs/promises";
import { dirname } from "node:path";

export async function appendJsonl(path: string, value: unknown): Promise<void> {
  await mkdir(dirname(path), { recursive: true });
  await appendFile(path, `${JSON.stringify(withTimestamp(value))}\n`);
}

function withTimestamp(value: unknown): unknown {
  if (value && typeof value === "object" && !Array.isArray(value)) {
    return {
      ts: new Date().toISOString(),
      ...value,
    };
  }
  return {
    ts: new Date().toISOString(),
    value,
  };
}
