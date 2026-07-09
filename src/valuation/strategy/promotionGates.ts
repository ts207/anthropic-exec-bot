import type { SignalType } from "./signalTypes.ts";

export type PromotionGate = {
  id: string;
  appliesToSignals: SignalType[];
  liveEnabled: false;
  minimumSampleSize: number;
  requiredEvidence: string[];
  blockingReason: string;
};

export const PAPER_TO_LIVE_PROMOTION_GATES: PromotionGate[] = [
  {
    id: "forecast_model",
    appliesToSignals: [
      "NPM_DRIFT_MODEL_YES",
      "NPM_NEAR_BOUNDARY_FORECAST_YES",
      "NPM_MULTI_DAY_BARRIER_FORECAST_YES",
      "CURVE_UNDERPRICED_FORECAST_YES",
      "ORDERBOOK_CONFIRMED_FORECAST_YES",
      "NO_FORECAST_EDGE",
    ],
    liveEnabled: false,
    minimumSampleSize: 30,
    requiredEvidence: [
      "30+ paper forecast entries with resolved next-fixing outcomes",
      "positive simulated EV after spread and slippage assumptions",
      "acceptable Brier/calibration by probability bucket",
      "zero parser/source-identity errors",
      "zero stale-source false positives",
      "measured maximum drawdown before promotion",
    ],
    blockingReason: "paper_promotion_gate_forecast_model_not_satisfied",
  },
  {
    id: "passive_ladder_maker",
    appliesToSignals: [
      "NPM_NEAR_BOUNDARY_FORECAST_YES",
      "NPM_MULTI_DAY_BARRIER_FORECAST_YES",
      "CURVE_UNDERPRICED_FORECAST_YES",
    ],
    liveEnabled: false,
    minimumSampleSize: 30,
    requiredEvidence: [
      "30+ filled passive maker ladder paper orders",
      "positive hypothetical PnL after missed-fill assumptions",
      "separate proof for near-boundary and far-optionality modes",
      "zero stale-source false fills",
      "measured fill quality versus quoted entry price",
      "measured maximum drawdown before promotion",
    ],
    blockingReason: "paper_promotion_gate_passive_ladder_maker_not_satisfied",
  },
  {
    id: "relative_value_diagnostics",
    appliesToSignals: [
      "CURVE_MONOTONICITY_YES",
      "CALENDAR_DOMINANCE_YES",
      "RANKING_INCONSISTENCY_ALERT",
    ],
    liveEnabled: false,
    minimumSampleSize: 0,
    requiredEvidence: [
      "remain paper/research diagnostics until a separate paired-order live design exists",
      "range-spread and curve-repair rows are excluded from maker live proof",
      "manual promotion required before any live mode",
    ],
    blockingReason: "paper_promotion_gate_relative_value_not_live_enabled",
  },
];

export function promotionGateSummary(): Array<Record<string, unknown>> {
  return PAPER_TO_LIVE_PROMOTION_GATES.map((gate) => ({
    id: gate.id,
    appliesToSignals: gate.appliesToSignals,
    liveEnabled: gate.liveEnabled,
    minimumSampleSize: gate.minimumSampleSize,
    requiredEvidence: gate.requiredEvidence,
    blockingReason: gate.blockingReason,
  }));
}

export function paperPromotionGateBlockers(signalType: SignalType): string[] {
  return PAPER_TO_LIVE_PROMOTION_GATES
    .filter((gate) => gate.appliesToSignals.includes(signalType))
    .map((gate) => gate.blockingReason);
}
