export const TARGET_AS_OF = "Jun 30, 2026";

export type ParsedNpmValuation = {
  asOf: string;
  valuation: number;
  raw: string;
};

export type NpmParseResult =
  | {
      ok: true;
      data: ParsedNpmValuation;
    }
  | {
      ok: false;
      reason: string;
    };

export type NpmApiResponse = {
  latest_tape_d?: {
    date?: unknown;
    implied_valuation?: unknown;
    price?: unknown;
  };
};

const AS_OF_RE = /As\s+of\s+([A-Z][a-z]{2}\s+\d{1,2},\s+\d{4})/g;
const RAW_MONEY_RE = /\$([0-9]+(?:\.[0-9]+)?)\s*([TtBbMm])/;

export function parseNpmValuationText(
  text: string,
  targetAsOf = TARGET_AS_OF,
): NpmParseResult {
  const normalized = normalizeText(text);
  const lines = normalized.split("\n").map((line) => line.trim()).filter(Boolean);
  const asOfMatches = Array.from(normalized.matchAll(AS_OF_RE));

  if (!asOfMatches.some((match) => match[1] === targetAsOf)) {
    return { ok: false, reason: "not Jun 30" };
  }

  const targetLineIndex = lines.findIndex((line) => line === `As of ${targetAsOf}`);
  if (targetLineIndex === -1) {
    return { ok: false, reason: "parse error" };
  }

  const sectionLines: string[] = [];
  for (let index = targetLineIndex + 1; index < lines.length; index += 1) {
    const line = lines[index];
    if (!line) continue;
    if (AS_OF_RE.test(line)) break;
    sectionLines.push(line);
  }
  AS_OF_RE.lastIndex = 0;

  const valuationLineIndex = sectionLines.findIndex((line) => line === "Valuation");
  if (valuationLineIndex === -1) {
    return { ok: false, reason: "no raw valuation" };
  }

  const rawCandidates = sectionLines.slice(valuationLineIndex + 1, valuationLineIndex + 4);
  const rawMatch = RAW_MONEY_RE.exec(rawCandidates.join("\n"));
  if (!rawMatch) {
    return { ok: false, reason: "no raw valuation" };
  }

  const amount = rawMatch[1];
  const suffix = rawMatch[2];
  if (!amount || !suffix) {
    return { ok: false, reason: "parse error" };
  }

  const valuation = parseMoneyToDollars(amount, suffix);
  if (!Number.isSafeInteger(valuation)) {
    return { ok: false, reason: "parse error" };
  }

  return {
    ok: true,
    data: {
      asOf: targetAsOf,
      valuation,
      raw: `$${amount}${suffix.toUpperCase()}`,
    },
  };
}

export function parseNpmApiResponse(
  value: unknown,
  targetIsoDate = "2026-06-30",
): NpmParseResult {
  if (!value || typeof value !== "object") {
    return { ok: false, reason: "parse error" };
  }

  const response = value as NpmApiResponse;
  const latest = response.latest_tape_d;
  if (!latest || typeof latest !== "object") {
    return { ok: false, reason: "no raw valuation" };
  }

  if (latest.date !== targetIsoDate) {
    return { ok: false, reason: "not Jun 30" };
  }

  const valuation = Number(latest.implied_valuation);
  if (!Number.isFinite(valuation) || valuation < 0) {
    return { ok: false, reason: "no raw valuation" };
  }

  return {
    ok: true,
    data: {
      asOf: TARGET_AS_OF,
      valuation,
      raw: String(latest.implied_valuation),
    },
  };
}

export function parseMoneyToDollars(amount: string, suffix: string): number {
  const multiplier = suffixMultiplier(suffix);
  const [wholeRaw, fractionalRaw = ""] = amount.split(".");
  const whole = Number(wholeRaw);
  const fractional = fractionalRaw.replace(/[^0-9]/g, "");

  if (!Number.isSafeInteger(whole) || fractional.length > 12) {
    throw new Error(`invalid money amount: ${amount}${suffix}`);
  }

  const scale = 10 ** fractional.length;
  const fractionalValue = fractional.length === 0 ? 0 : Number(fractional);
  const value = whole * multiplier + (fractionalValue * multiplier) / scale;

  if (!Number.isSafeInteger(value)) {
    throw new Error(`unsafe money amount: ${amount}${suffix}`);
  }

  return value;
}

function suffixMultiplier(suffix: string): number {
  switch (suffix.toUpperCase()) {
    case "T":
      return 1_000_000_000_000;
    case "B":
      return 1_000_000_000;
    case "M":
      return 1_000_000;
    default:
      throw new Error(`unsupported valuation suffix: ${suffix}`);
  }
}

function normalizeText(text: string): string {
  return text
    .replace(/\u00a0/g, " ")
    .replace(/\r\n/g, "\n")
    .split("\n")
    .map((line) => line.replace(/[ \t]+/g, " ").trim())
    .join("\n")
    .trim();
}
