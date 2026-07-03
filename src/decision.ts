export const THRESHOLD = 1_100_000_000_000;

export type TradeSide = "YES" | "NO";

export type TradeDecision = {
  side: TradeSide;
  reason: string;
};

export function decide(valuation: number): TradeDecision {
  if (!Number.isFinite(valuation) || valuation < 0) {
    throw new Error(`invalid valuation: ${valuation}`);
  }

  if (valuation >= THRESHOLD) {
    return {
      side: "YES",
      reason: `valuation ${valuation} >= threshold ${THRESHOLD}`,
    };
  }

  return {
    side: "NO",
    reason: `valuation ${valuation} < threshold ${THRESHOLD}`,
  };
}
