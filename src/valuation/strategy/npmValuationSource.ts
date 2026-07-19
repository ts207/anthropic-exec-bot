import { createHash } from "node:crypto";
import type { CompanyConfig, NpmEvidence, NpmTapePoint } from "./signalTypes.ts";

const NPM_BASE_URL = "https://api-npm17-data-company-pricing-review-prod.k8s-prod-1.npmdev.net/api/public/companies";

export async function fetchNpmEvidence(company: CompanyConfig): Promise<NpmEvidence | null> {
  if (!company.npmCompanyId) return null;
  const sourceUrl = `${NPM_BASE_URL}/${encodeURIComponent(company.npmCompanyId)}`;
  const response = await fetch(sourceUrl, { headers: { accept: "application/json" } });
  if (!response.ok) throw new Error(`NPM fetch failed for ${company.name}: ${response.status} ${response.statusText}`);
  const raw = await response.json() as unknown;
  return parseNpmEvidence(raw, company, sourceUrl);
}

export function parseNpmEvidence(raw: unknown, company: CompanyConfig, sourceUrl = "fixture://npm"): NpmEvidence {
  const record = asRecord(raw);
  // The NPM public API renamed its payload while the bot was stopped:
  // latest_tape_d -> latest_npm_price (same {date, price, implied_valuation}
  // shape) and tape_d_prices -> valuations (+ npm_prices for per-date
  // prices). Accept both shapes so a rollback upstream cannot break us.
  const latest = asRecord(
    "latest_tape_d" in record ? record.latest_tape_d : record.latest_npm_price
  );
  const latestTapeDate = stringField(latest.date, "latest_tape_d.date");
  const latestValuation = numberField(latest.implied_valuation, "latest_tape_d.implied_valuation");
  const latestPrice = optionalNumber(latest.price ?? asRecord(record.latest_npm_price).price ?? record.latest_npm_price);
  const tapeRows = "tape_d_prices" in record ? record.tape_d_prices : mergeValuationTape(record.valuations, record.npm_prices);
  const tape = parseTape(tapeRows, latestTapeDate, latestValuation, latestPrice);
  const identityOk = companyIdentityMatches(record.company, company);
  return {
    company: company.name,
    npmCompanyId: company.npmCompanyId ?? "",
    sourceUrl,
    latestTapeDate,
    latestValuation,
    latestPrice,
    tape,
    identityOk,
    rawHash: sha256(JSON.stringify(raw)),
  };
}

export function withEligibleMax(evidence: NpmEvidence, startIso: string | undefined, deadlineIso: string): NpmEvidence {
  const start = startIso ? Date.parse(startIso) : Number.NEGATIVE_INFINITY;
  const deadline = Date.parse(deadlineIso);
  const eligible = evidence.tape.filter((point) => {
    const ts = Date.parse(`${point.date}T00:00:00Z`);
    return Number.isFinite(ts) && ts >= start && ts <= deadline;
  });
  const max = eligible.reduce<NpmTapePoint | null>((best, point) => (
    !best || point.impliedValuation > best.impliedValuation ? point : best
  ), null);
  return {
    ...evidence,
    maxEligibleValuation: max?.impliedValuation,
    maxEligibleDate: max?.date,
  };
}

function parseTape(value: unknown, latestDate: string, latestValuation: number, latestPrice?: number): NpmTapePoint[] {
  const rows = Array.isArray(value) ? value : [];
  const parsed = rows.flatMap((item) => {
    const record = asRecord(item);
    const date = typeof record.date === "string" ? record.date : undefined;
    const impliedValuation = optionalNumber(record.implied_valuation);
    if (!date || impliedValuation === undefined) return [];
    return [{ date, impliedValuation, price: optionalNumber(record.price) }];
  });
  if (!parsed.some((point) => point.date === latestDate)) {
    parsed.push({ date: latestDate, impliedValuation: latestValuation, price: latestPrice });
  }
  return parsed.sort((left, right) => left.date.localeCompare(right.date));
}

function mergeValuationTape(valuations: unknown, prices: unknown): unknown {
  // New API shape: valuations = [{date, implied_valuation}], npm_prices =
  // [{date, price}]. Join them by date into the old tape_d_prices shape.
  const priceByDate = new Map<string, number>();
  for (const item of Array.isArray(prices) ? prices : []) {
    const record = asRecord(item);
    const price = optionalNumber(record.price);
    if (typeof record.date === "string" && price !== undefined) priceByDate.set(record.date, price);
  }
  return (Array.isArray(valuations) ? valuations : []).map((item) => {
    const record = asRecord(item);
    const date = typeof record.date === "string" ? record.date : undefined;
    return { ...record, price: record.price ?? (date ? priceByDate.get(date) : undefined) };
  });
}

function companyIdentityMatches(rawCompany: unknown, company: CompanyConfig): boolean {
  const record = asRecord(rawCompany);
  const rawName = String(record.name ?? record.company_name ?? record.dba_name ?? "").toLowerCase();
  if (!rawName) return true;
  const accepted = [company.name, ...(company.aliases ?? [])].map((name) => name.toLowerCase());
  return accepted.some((name) => rawName.includes(name) || name.includes(rawName));
}

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
}

function stringField(value: unknown, name: string): string {
  if (typeof value !== "string" || !value.trim()) throw new Error(`${name} is missing`);
  return value.trim();
}

function numberField(value: unknown, name: string): number {
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) throw new Error(`${name} is missing`);
  return parsed;
}

function optionalNumber(value: unknown): number | undefined {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : undefined;
}

function sha256(value: string): string {
  return createHash("sha256").update(value).digest("hex");
}
