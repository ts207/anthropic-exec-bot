import { probePath, readJson } from "./stateStore.ts";
import type { StrategyConfig, ValuationCandidate } from "./signalTypes.ts";

const EXPECTED_SDK = "@polymarket/client@beta";
const EXPECTED_SIDE = "BUY";
const EXPECTED_ORDER_TYPE = "FAK";
const PROBE_MAX_AGE_MS = 24 * 60 * 60 * 1000;

export type ProbeValidation = {
  ok: boolean;
  blockers: string[];
  probe?: Record<string, unknown>;
};

export async function validatePostedProbeForCandidate(
  config: StrategyConfig,
  candidate: ValuationCandidate,
  now = new Date(),
): Promise<ProbeValidation> {
  const raw = await readJson(probePath(config, candidate.marketSlug));
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) {
    return { ok: false, blockers: ["missing_posted_probe_success"] };
  }
  const probe = raw as Record<string, unknown>;
  const blockers: string[] = [];
  if (probe.ok !== true) blockers.push("probe_not_ok");
  if (probe.marketSlug !== candidate.marketSlug) blockers.push("probe_market_slug_mismatch");
  if (probe.tokenId !== candidate.yesTokenId) blockers.push("probe_token_mismatch");
  if (probe.side !== EXPECTED_SIDE) blockers.push("probe_side_mismatch");
  if (probe.orderType !== EXPECTED_ORDER_TYPE) blockers.push("probe_order_type_mismatch");
  if (probe.sdk !== EXPECTED_SDK) blockers.push("probe_sdk_mismatch");
  const timestamp = typeof probe.timestamp === "string" ? probe.timestamp : probe.probedAt;
  const probedAt = typeof timestamp === "string" ? Date.parse(timestamp) : NaN;
  if (!Number.isFinite(probedAt)) {
    blockers.push("probe_timestamp_missing");
  } else if (now.getTime() - probedAt > PROBE_MAX_AGE_MS) {
    blockers.push("probe_stale");
  } else if (probedAt > now.getTime() + 60_000) {
    blockers.push("probe_timestamp_in_future");
  }
  return {
    ok: blockers.length === 0,
    blockers,
    probe,
  };
}

export function betaProbeMetadata(): Record<string, string> {
  return {
    sdk: EXPECTED_SDK,
    side: EXPECTED_SIDE,
    orderType: EXPECTED_ORDER_TYPE,
  };
}
