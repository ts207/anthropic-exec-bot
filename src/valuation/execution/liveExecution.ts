import { sourceConfirmedLivePolicyBlockers } from "../strategy/betaExecution.ts";
import { validatePostedProbeForCandidate } from "../strategy/probeValidation.ts";
import { paperPromotionGateBlockers } from "../strategy/promotionGates.ts";
import { hasLiveAck, isCandidateLocked } from "../strategy/stateStore.ts";
import type { StrategyConfig, ValuationCandidate } from "../strategy/signalTypes.ts";

export async function liveBlockers(
  candidate: ValuationCandidate,
  config: StrategyConfig,
  configHash: string,
): Promise<string[]> {
  const blockers: string[] = [];
  if (candidate.status !== "candidate") blockers.push(`candidate_status_${candidate.status}`);
  if (config.mode !== "live") blockers.push(`operator_mode_${config.mode}`);
  if (candidate.signalType === "NPM_DRIFT_MODEL_YES") blockers.push("drift_model_alert_only");
  if (candidate.signalType === "RANKING_INCONSISTENCY_ALERT") blockers.push("ranking_market_alert_only");
  blockers.push(...paperPromotionGateBlockers(candidate.signalType));
  blockers.push(...sourceConfirmedLivePolicyBlockers(candidate, config));
  if (!candidate.yesTokenId) blockers.push("missing_yes_token");
  if (!candidate.orderTemplate) blockers.push("missing_order_template");
  if (candidate.orderUsd <= 0) blockers.push("zero_order_usd");
  if (!await hasLiveAck(config, configHash)) blockers.push("missing_live_config_ack");
  if (await isCandidateLocked(config, candidate)) blockers.push("duplicate_lock");
  const probe = await validatePostedProbeForCandidate(config, candidate);
  if (!probe.ok) blockers.push(...probe.blockers);
  if (process.env.POLYBOT_TS_BRIDGE_ALLOW_POST !== "1") blockers.push("posting_env_not_armed");
  return [...new Set(blockers)];
}
