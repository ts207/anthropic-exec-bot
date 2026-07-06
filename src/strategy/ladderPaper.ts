import { join } from "node:path";
import type { StrategyConfig } from "./signalTypes.ts";
import type { EntryMode, EntryPlan } from "./valuationLadderEntries.ts";

export type LadderPaperStatus = "working" | "filled" | "resolved" | "cancelled";

export type LadderPaperOrder = {
  id: string;
  company: string;
  eventSlug: string;
  marketSlug: string;
  pairedMarketSlug?: string;
  threshold?: number;
  pairedThreshold?: number;
  deadline?: string;
  entryMode: EntryMode;
  openedAt: string;
  sourceDate?: string;
  currentValuation?: number;
  maxEligibleValuation?: number;
  distancePct?: number;
  passiveBidPrice: number;
  modelFair: number;
  requiredEdge: number;
  sizeUsd: number;
  status: LadderPaperStatus;
  filledAt: string | null;
  fillPrice: number | null;
  currentMarkPrice: number | null;
  finalResolution: boolean | null;
  hypotheticalPnl: number | null;
  cancelReason: string | null;
  reason: string;
};

export type LadderPaperState = {
  version: 1;
  updatedAt: string;
  orders: LadderPaperOrder[];
};

export type LadderPaperUpdate = {
  state: LadderPaperState;
  opened: LadderPaperOrder[];
  filled: LadderPaperOrder[];
  updated: LadderPaperOrder[];
  metrics: LadderPaperMetrics;
};

export type LadderPaperMetrics = {
  totalOrders: number;
  workingOrders: number;
  filledOrders: number;
  resolvedOrders: number;
  cancelledOrders: number;
  openedThisRun: number;
  filledThisRun: number;
  updatedThisRun: number;
  totalHypotheticalPnl: number;
  byMode: Record<string, number>;
  byModeProof: Array<{
    entryMode: string;
    totalOrders: number;
    filledOrResolvedOrders: number;
    resolvedOrders: number;
    cancelledOrders: number;
    totalHypotheticalPnl: number;
    averageHypotheticalPnl: number | null;
    staleSourceErrorCount: number;
    readyForManualReview: boolean;
  }>;
  proofBeforeLive: {
    minimumFilledOrders: 30;
    currentFilledOrders: number;
    currentResolvedOrders: number;
    totalHypotheticalPnl: number;
    positivePnlRequired: true;
    staleSourceErrorCount: number;
    readyForManualReview: boolean;
    readyForLive: false;
    requirements: string[];
  };
};

export function ladderPaperPath(config: StrategyConfig): string {
  return join(config.stateDir, "ladder_paper_orders.json");
}

export function parseLadderPaperState(raw: unknown): LadderPaperState {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return emptyState();
  const record = raw as Record<string, unknown>;
  const orders = Array.isArray(record.orders)
    ? record.orders.filter((item): item is LadderPaperOrder => Boolean(item && typeof item === "object" && "id" in item))
    : [];
  return {
    version: 1,
    updatedAt: typeof record.updatedAt === "string" ? record.updatedAt : new Date(0).toISOString(),
    orders,
  };
}

export function updateLadderPaperOrders(input: {
  previous: LadderPaperState;
  plans: EntryPlan[];
  now?: Date;
  sizeUsd?: number;
  nextFixingAt?: Date;
  cancelBeforeFixingMs?: number;
}): LadderPaperUpdate {
  const now = input.now ?? new Date();
  const sizeUsd = input.sizeUsd ?? 1;
  const plansByKey = new Map(input.plans.map((plan) => [planKey(plan), plan]));
  const plansByStableKey = new Map(input.plans.map((plan) => [stablePlanKey(plan), plan]));
  const orders = dedupeOrders(input.previous.orders).map((order) => ({ ...order }));
  const updated: LadderPaperOrder[] = [];
  const filled: LadderPaperOrder[] = [];

  for (let index = 0; index < orders.length; index += 1) {
    const order = orders[index];
    if (!order || order.status === "resolved" || order.status === "cancelled") continue;
    const plan = plansByKey.get(order.id);
    const replacementPlan = plansByStableKey.get(orderStableKey(order));
    const next = updateOpenOrder(order, plan, {
      now,
      nextFixingAt: input.nextFixingAt,
      cancelBeforeFixingMs: input.cancelBeforeFixingMs,
      replacementPlan,
    });
    orders[index] = next;
    if (next !== order) {
      updated.push(next);
      if (order.status === "working" && next.status === "filled") filled.push(next);
    }
  }

  const knownIds = new Set(orders.map((order) => order.id));
  const opened: LadderPaperOrder[] = [];
  for (const plan of input.plans) {
    if (!isLadderPaperOpenTrigger(plan)) continue;
    const id = planKey(plan);
    if (knownIds.has(id)) continue;
    const order = openOrder(plan, now, sizeUsd);
    orders.push(order);
    opened.push(order);
    if (order.status === "filled") filled.push(order);
    knownIds.add(id);
  }

  const state = {
    version: 1 as const,
    updatedAt: now.toISOString(),
    orders,
  };
  return {
    state,
    opened,
    filled,
    updated,
    metrics: ladderPaperMetrics(orders, opened.length, filled.length, updated.length),
  };
}

export function isLadderPaperOpenTrigger(plan: EntryPlan): boolean {
  if (!plan.paperEligible || plan.passiveBidPrice === null) return false;
  if (plan.entryMode === "TAKER_SOURCE_CONFIRMED") return false;
  if (plan.blockers.length > 0) return false;
  return (
    plan.entryMode === "MAKER_NEAR_BOUNDARY_BID"
    || plan.entryMode === "MAKER_FAR_OPTIONALITY_BID"
    || plan.entryMode === "MAKER_CURVE_REPAIR_BID"
    || plan.entryMode === "RANGE_SPREAD_PAPER"
  );
}

function openOrder(plan: EntryPlan, now: Date, sizeUsd: number): LadderPaperOrder {
  return {
    id: planKey(plan),
    company: plan.company,
    eventSlug: plan.eventSlug,
    marketSlug: plan.marketSlug,
    pairedMarketSlug: plan.pairedMarketSlug,
    threshold: plan.threshold,
    pairedThreshold: plan.range?.higherThreshold,
    deadline: plan.range?.deadline,
    entryMode: plan.entryMode,
    openedAt: now.toISOString(),
    sourceDate: plan.sourceDate,
    currentValuation: plan.currentValuation,
    maxEligibleValuation: plan.maxEligibleValuation,
    distancePct: plan.distancePct,
    passiveBidPrice: plan.passiveBidPrice ?? 0,
    modelFair: plan.modelFair,
    requiredEdge: plan.requiredEdge,
    sizeUsd,
    status: plan.entryMode === "RANGE_SPREAD_PAPER" ? "filled" : "working",
    filledAt: plan.entryMode === "RANGE_SPREAD_PAPER" ? now.toISOString() : null,
    fillPrice: plan.entryMode === "RANGE_SPREAD_PAPER" ? plan.passiveBidPrice : null,
    currentMarkPrice: plan.yesBid,
    finalResolution: null,
    hypotheticalPnl: null,
    cancelReason: null,
    reason: plan.reason,
  };
}

function updateOpenOrder(
  order: LadderPaperOrder,
  plan: EntryPlan | undefined,
  timing: { now: Date; nextFixingAt?: Date; cancelBeforeFixingMs?: number; replacementPlan?: EntryPlan },
): LadderPaperOrder {
  const { now } = timing;
  if (!plan) {
    if (timing.replacementPlan && timing.replacementPlan.passiveBidPrice !== order.passiveBidPrice) {
      return cancelOrder(order, "model_fair_repriced_passive_bid");
    }
    return cancelOrder(order, "entry_plan_no_longer_available");
  }
  if (plan.blockers.some((blocker) => blocker === "direction_semantics_unknown" || blocker === "market_not_accepting_orders" || blocker === "malformed_or_unsupported_leg")) {
    return cancelOrder(order, `cancelled_by_blocker:${plan.blockers.join(",")}`);
  }
  if (order.status === "working" && shouldCancelBeforeFixing(order, plan, timing)) {
    return cancelOrder(order, "cancel_before_npm_fixing_model_fair_stale");
  }
  if (plan.currentValuation !== undefined && order.currentValuation !== undefined && plan.threshold !== undefined) {
    const movedAway = Math.max(0, plan.distancePct ?? 0) > Math.max(0, order.distancePct ?? 0) + 0.025;
    if (movedAway) return cancelOrder(order, "source_moved_away_from_threshold");
  }
  const next: LadderPaperOrder = {
    ...order,
    currentValuation: plan.currentValuation,
    maxEligibleValuation: plan.maxEligibleValuation,
    distancePct: plan.distancePct,
    currentMarkPrice: markPrice(plan),
  };
  if (next.status === "working" && wouldPassiveBidFill(plan, next.passiveBidPrice)) {
    next.status = "filled";
    next.filledAt = now.toISOString();
    next.fillPrice = next.passiveBidPrice;
  }
  if (next.status === "filled") {
    const resolved = finalResolution(plan);
    if (resolved !== null || deadlinePassed(plan, now)) {
      next.status = "resolved";
      next.finalResolution = resolved ?? false;
      next.hypotheticalPnl = pnlUsd(next.fillPrice ?? next.passiveBidPrice, next.finalResolution ? 1 : 0, next.sizeUsd);
    } else if (next.currentMarkPrice !== null) {
      next.hypotheticalPnl = pnlUsd(next.fillPrice ?? next.passiveBidPrice, next.currentMarkPrice, next.sizeUsd);
    }
  }
  return JSON.stringify(next) === JSON.stringify(order) ? order : next;
}

function shouldCancelBeforeFixing(
  order: LadderPaperOrder,
  plan: EntryPlan,
  timing: { now: Date; nextFixingAt?: Date; cancelBeforeFixingMs?: number },
): boolean {
  if (!timing.nextFixingAt || !timing.cancelBeforeFixingMs || timing.cancelBeforeFixingMs <= 0) return false;
  const msUntilFixing = timing.nextFixingAt.getTime() - timing.now.getTime();
  if (msUntilFixing < 0 || msUntilFixing > timing.cancelBeforeFixingMs) return false;
  if (order.entryMode === "RANGE_SPREAD_PAPER") return false;
  return order.sourceDate === undefined || order.sourceDate === plan.sourceDate;
}

function wouldPassiveBidFill(plan: EntryPlan, bidPrice: number): boolean {
  if (plan.entryMode === "RANGE_SPREAD_PAPER") return false;
  return plan.yesAsk !== null && plan.yesAsk <= bidPrice;
}

function finalResolution(plan: EntryPlan): boolean | null {
  if (plan.threshold === undefined || plan.maxEligibleValuation === undefined) return null;
  if (plan.entryMode === "RANGE_SPREAD_PAPER" && plan.range) {
    const lowerTouched = plan.maxEligibleValuation >= plan.range.lowerThreshold;
    const higherTouched = plan.maxEligibleValuation >= plan.range.higherThreshold;
    return lowerTouched && !higherTouched;
  }
  if (plan.maxEligibleValuation >= plan.threshold) return true;
  return null;
}

function deadlinePassed(plan: EntryPlan, now: Date): boolean {
  const deadline = plan.range?.deadline ?? plan.deadline;
  return deadline !== undefined && Date.parse(deadline) <= now.getTime();
}

function markPrice(plan: EntryPlan): number | null {
  if (plan.entryMode === "RANGE_SPREAD_PAPER" && plan.range) return plan.range.currentMarkPrice ?? null;
  return plan.yesBid;
}

function cancelOrder(order: LadderPaperOrder, reason: string): LadderPaperOrder {
  return {
    ...order,
    status: "cancelled",
    cancelReason: reason,
  };
}

function ladderPaperMetrics(
  orders: LadderPaperOrder[],
  openedThisRun: number,
  filledThisRun: number,
  updatedThisRun: number,
): LadderPaperMetrics {
  const byMode = orders.reduce<Record<string, number>>((counts, order) => {
    counts[order.entryMode] = (counts[order.entryMode] ?? 0) + 1;
    return counts;
  }, {});
  const filledOrResolved = orders.filter((order) => order.status === "filled" || order.status === "resolved");
  const resolved = orders.filter((order) => order.status === "resolved");
  const totalHypotheticalPnl = round4(orders.reduce((sum, order) => sum + (order.hypotheticalPnl ?? 0), 0));
  const staleSourceErrorCount = orders.filter(hasStaleSourceError).length;
  const readyForManualReview = filledOrResolved.length >= 30 && totalHypotheticalPnl > 0 && staleSourceErrorCount === 0;
  return {
    totalOrders: orders.length,
    workingOrders: orders.filter((order) => order.status === "working").length,
    filledOrders: orders.filter((order) => order.status === "filled").length,
    resolvedOrders: resolved.length,
    cancelledOrders: orders.filter((order) => order.status === "cancelled").length,
    openedThisRun,
    filledThisRun,
    updatedThisRun,
    totalHypotheticalPnl,
    byMode,
    byModeProof: modeProofRows(orders),
    proofBeforeLive: {
      minimumFilledOrders: 30,
      currentFilledOrders: filledOrResolved.length,
      currentResolvedOrders: resolved.length,
      totalHypotheticalPnl,
      positivePnlRequired: true,
      staleSourceErrorCount,
      readyForManualReview,
      readyForLive: false,
      requirements: [
        "30+ filled passive ladder paper orders",
        "positive hypothetical PnL after spread and missed-fill assumptions",
        "separate calibration for near-boundary, far-optionality, and curve-repair orders",
        "no stale-source false fills",
        "manual promotion before any maker live mode",
      ],
    },
  };
}

function dedupeOrders(orders: LadderPaperOrder[]): LadderPaperOrder[] {
  const byId = new Map<string, LadderPaperOrder>();
  for (const order of orders) {
    const existing = byId.get(order.id);
    if (!existing || orderRank(order) >= orderRank(existing)) byId.set(order.id, order);
  }
  return [...byId.values()];
}

function orderRank(order: LadderPaperOrder): number {
  if (order.status === "resolved") return 4;
  if (order.status === "filled") return 3;
  if (order.status === "working") return 2;
  return 1;
}

function planKey(plan: EntryPlan): string {
  return [
    plan.entryMode,
    plan.company,
    plan.marketSlug,
    plan.pairedMarketSlug ?? "single",
    plan.sourceDate ?? "unknown-source-date",
    plan.passiveBidPrice ?? "no-bid",
  ].join(":");
}

function stablePlanKey(plan: EntryPlan): string {
  return [
    plan.entryMode,
    plan.company,
    plan.marketSlug,
    plan.pairedMarketSlug ?? "single",
    plan.sourceDate ?? "unknown-source-date",
  ].join(":");
}

function orderStableKey(order: LadderPaperOrder): string {
  return [
    order.entryMode,
    order.company,
    order.marketSlug,
    order.pairedMarketSlug ?? "single",
    order.sourceDate ?? "unknown-source-date",
  ].join(":");
}

function pnlUsd(entryPrice: number, markPrice: number, sizeUsd: number): number {
  if (entryPrice <= 0) return 0;
  return round4(((markPrice - entryPrice) * sizeUsd) / entryPrice);
}

function modeProofRows(orders: LadderPaperOrder[]): LadderPaperMetrics["byModeProof"] {
  const modes = [...new Set(orders.map((order) => order.entryMode))].sort();
  return modes.map((entryMode) => {
    const rows = orders.filter((order) => order.entryMode === entryMode);
    const filledOrResolved = rows.filter((order) => order.status === "filled" || order.status === "resolved");
    const resolved = rows.filter((order) => order.status === "resolved");
    const totalHypotheticalPnl = round4(rows.reduce((sum, order) => sum + (order.hypotheticalPnl ?? 0), 0));
    const staleSourceErrorCount = rows.filter(hasStaleSourceError).length;
    return {
      entryMode,
      totalOrders: rows.length,
      filledOrResolvedOrders: filledOrResolved.length,
      resolvedOrders: resolved.length,
      cancelledOrders: rows.filter((order) => order.status === "cancelled").length,
      totalHypotheticalPnl,
      averageHypotheticalPnl: resolved.length ? round4(totalHypotheticalPnl / resolved.length) : null,
      staleSourceErrorCount,
      readyForManualReview: filledOrResolved.length >= 30 && totalHypotheticalPnl > 0 && staleSourceErrorCount === 0,
    };
  });
}

function hasStaleSourceError(order: LadderPaperOrder): boolean {
  const reason = `${order.cancelReason ?? ""} ${order.reason}`.toLowerCase();
  return reason.includes("stale-source") || reason.includes("stale_source");
}

function emptyState(): LadderPaperState {
  return {
    version: 1,
    updatedAt: new Date(0).toISOString(),
    orders: [],
  };
}

function round4(value: number): number {
  return Math.round(value * 10_000) / 10_000;
}
