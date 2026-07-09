import type { SignalType, StrategyConfig, ValuationCandidate } from "./signalTypes.ts";

export function allocateCandidate(
  base: Omit<ValuationCandidate, "confidenceScore" | "edgeScore" | "maxPrice" | "orderUsd" | "orderTemplate" | "liveAllowed">,
  config: StrategyConfig,
): ValuationCandidate {
  const maxPrice = Math.min(
    config.defaultMaxYesPrice,
    config.maxYesPriceBySignal[base.signalType] ?? config.defaultMaxYesPrice,
    Math.max(0, base.fairPrice - minimumEdgeFor(base.signalType, config)),
  );
  const multiplier = config.signalMultipliers[base.signalType] ?? 0;
  const edgeMultiplier = Math.max(0.25, Math.min(2, base.edge / Math.max(0.01, minimumEdgeFor(base.signalType, config))));
  const confidenceMultiplier = Math.max(0, Math.min(1, base.confidence / 10));
  const liquidityMultiplier = base.yesAsk && base.liquidity > 0
    ? Math.max(0.1, Math.min(1, (base.liquidity * 0.25) / config.baseOrderUsd))
    : 0;
  const orderUsd = roundUsd(config.baseOrderUsd * multiplier * edgeMultiplier * confidenceMultiplier * liquidityMultiplier);
  const edgeScore = Math.max(0, Math.min(10, (base.edge / Math.max(0.01, minimumEdgeFor(base.signalType, config))) * 5));
  const confidenceScore = Math.max(0, Math.min(10, base.confidence));
  return {
    ...base,
    confidenceScore,
    edgeScore,
    maxPrice,
    orderUsd,
    orderTemplate: orderUsd > 0 && base.yesTokenId
      ? {
        tokenId: base.yesTokenId,
        side: "BUY",
        outcome: "YES",
        orderType: "FAK",
        amountUsd: orderUsd,
        maxPrice,
        posted: false,
      }
      : undefined,
    liveAllowed: config.mode === "live" && base.status === "candidate" && orderUsd > 0 && base.confidence >= 9,
  };
}

export function minimumEdgeFor(signal: SignalType, config: StrategyConfig): number {
  if (signal === "SOURCE_CONFIRMED_YES") return config.minimumEdge.sourceConfirmed;
  if (signal === "CURVE_MONOTONICITY_YES") return config.minimumEdge.curve;
  if (signal === "CALENDAR_DOMINANCE_YES") return config.minimumEdge.calendar;
  if (signal === "NPM_DRIFT_MODEL_YES") return config.minimumEdge.drift;
  return 1;
}

function roundUsd(value: number): number {
  return Math.round(value * 100) / 100;
}
