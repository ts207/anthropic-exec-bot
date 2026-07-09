import { mkdirSync, writeFileSync } from "node:fs";
import { dirname } from "node:path";
import {
  AssetType,
  ClobClient,
  OrderType,
  Side,
  SignatureTypeV2,
  type MarketDetails,
  type SignedOrder,
  type TickSize,
} from "@polymarket/clob-client-v2";
import { createWalletClient, http } from "viem";
import { privateKeyToAccount } from "viem/accounts";
import type { BotConfig } from "./config.ts";

export type GammaMarket = {
  id?: string;
  question?: string;
  title?: string;
  slug?: string;
  conditionId?: string;
  groupItemTitle?: string;
  groupItemThreshold?: string;
  clobTokenIds?: string[] | string;
  orderPriceMinTickSize?: number | string;
  tickSize?: number | string;
  negRisk?: boolean;
  acceptingOrders?: boolean;
  active?: boolean;
  closed?: boolean;
  [key: string]: unknown;
};

export type GammaEvent = {
  slug?: string;
  title?: string;
  markets?: GammaMarket[];
  [key: string]: unknown;
};

export type SelectedMarket = {
  eventSlug: string;
  selectedMarket: string;
  question: string;
  conditionId: string;
  yesTokenId: string;
  noTokenId: string;
  tickSize: TickSize;
  negRisk: boolean;
};

export type OrderPlan = {
  dryRun: boolean;
  side: "YES" | "NO";
  tokenID: string;
  amount: number;
  price: number;
  orderType: "FAK";
  market: SelectedMarket;
};

export type PreparedFakOrders =
  | {
      dryRun: true;
      yes: OrderPlan;
      no: OrderPlan;
    }
  | {
      dryRun: false;
      yes: SignedOrder;
      no: SignedOrder;
      yesPlan: OrderPlan;
      noPlan: OrderPlan;
    };

export type PrepareOrdersInput = {
  config: BotConfig;
  market: SelectedMarket;
  client: ClobClient | null;
};

export type SubmitPreparedOrderInput = {
  config: BotConfig;
  market: SelectedMarket;
  side: "YES" | "NO";
  client: ClobClient | null;
  preparedOrders: PreparedFakOrders;
  bestAsk: number | null;
};

export type SubmitPreparedOrderResult =
  | {
      submitted: false;
      accepted: false;
      skipped: "PRICE_ABOVE_CAP";
      side: "YES" | "NO";
      bestAsk: number;
      maxPrice: number;
    }
  | {
      submitted: false;
      accepted: false;
      skipped: "DRY_RUN=1";
      bestAsk: number | null;
    } & OrderPlan
  | {
      submitted: true;
      accepted: boolean;
      side: "YES" | "NO";
      latencyMs: number;
      response: unknown;
    };

export type ClobMarketDelayInfo = {
  itode?: boolean;
  ao?: boolean;
  mos?: number;
  mts?: number;
};

export async function fetchEventBySlug(slug: string): Promise<GammaEvent> {
  const url = `https://gamma-api.polymarket.com/events/slug/${encodeURIComponent(slug)}`;
  const response = await fetch(url);
  if (!response.ok) {
    throw new Error(`Gamma event fetch failed: ${response.status} ${response.statusText}`);
  }
  return (await response.json()) as GammaEvent;
}

export async function initializeClobClient(config: BotConfig): Promise<ClobClient | null> {
  if (config.dryRun) {
    return null;
  }

  if (
    !config.privateKey ||
    !config.funderAddress ||
    !config.depositWalletAddress ||
    !config.clobApiKey ||
    !config.clobSecret ||
    !config.clobPassPhrase
  ) {
    throw new Error("missing live signing config");
  }

  const account = privateKeyToAccount(config.privateKey);
  const signer = createWalletClient({
    account,
    transport: http(config.polygonRpcUrl),
  });

  const client = new ClobClient({
    host: config.clobHost,
    chain: config.chainId,
    signer,
    creds: {
      key: config.clobApiKey,
      secret: config.clobSecret,
      passphrase: config.clobPassPhrase,
    },
    signatureType: SignatureTypeV2.POLY_1271,
    funderAddress: config.depositWalletAddress,
  });

  await client.updateBalanceAllowance({ asset_type: AssetType.COLLATERAL });

  return client;
}

export async function getClobMarketDelayInfo(
  client: ClobClient | null,
  conditionId: string,
  clobHost?: string,
): Promise<ClobMarketDelayInfo | null> {
  if (!client) {
    if (!clobHost) {
      return null;
    }

    const response = await fetch(
      `${clobHost.replace(/\/$/, "")}/clob-markets/${conditionId}`,
    );
    if (!response.ok) {
      return null;
    }
    const info = (await response.json()) as Partial<MarketDetails>;
    return {
      itode: Boolean(info.itode),
      ao: info.ao,
      mos: info.mos,
      mts: info.mts,
    };
  }

  const info = await client.getClobMarketInfo(conditionId);
  return {
    itode: Boolean(info.itode),
    ao: info.ao,
    mos: info.mos,
    mts: info.mts,
  };
}

export function selectThresholdMarket(
  event: GammaEvent,
  targetMarketText: string,
): SelectedMarket {
  const markets = event.markets ?? [];
  const matches = markets.filter((market) =>
    marketMatchesThreshold(market, targetMarketText),
  );

  if (matches.length === 0) {
    throw new Error(`no market matched threshold text ${targetMarketText}`);
  }
  if (matches.length > 1) {
    const questions = matches.map((market) => market.question ?? market.slug ?? market.id);
    throw new Error(`ambiguous market match for ${targetMarketText}: ${questions.join(" | ")}`);
  }

  const market = matches[0];
  if (!market) {
    throw new Error("market selection failed");
  }

  const tokenIds = parseTokenIds(market.clobTokenIds);
  const conditionId = requiredString(market.conditionId, "conditionId");
  const tickSize = parseTickSize(market);

  return {
    eventSlug: requiredString(event.slug, "event.slug"),
    selectedMarket:
      market.groupItemTitle ??
      market.groupItemThreshold ??
      market.question ??
      targetMarketText,
    question: market.question ?? "",
    conditionId,
    yesTokenId: tokenIds[0],
    noTokenId: tokenIds[1],
    tickSize,
    negRisk: Boolean(market.negRisk),
  };
}

export async function prepareFakBuyOrders(
  input: PrepareOrdersInput,
): Promise<PreparedFakOrders> {
  const { config, market, client } = input;
  const yesPlan = buildOrderPlan(config, market, "YES");
  const noPlan = buildOrderPlan(config, market, "NO");

  if (config.dryRun) {
    return {
      dryRun: true,
      yes: yesPlan,
      no: noPlan,
    };
  }

  if (!client) {
    throw new Error("live CLOB client was not initialized");
  }

  const orderOptions = {
    tickSize: market.tickSize,
    negRisk: market.negRisk,
  };

  const [signedYesOrder, signedNoOrder] = await Promise.all([
    client.createMarketOrder(
      {
        tokenID: yesPlan.tokenID,
        side: Side.BUY,
        amount: yesPlan.amount,
        price: yesPlan.price,
        orderType: OrderType.FAK,
      },
      orderOptions,
    ),
    client.createMarketOrder(
      {
        tokenID: noPlan.tokenID,
        side: Side.BUY,
        amount: noPlan.amount,
        price: noPlan.price,
        orderType: OrderType.FAK,
      },
      orderOptions,
    ),
  ]);

  return {
    dryRun: false,
    yes: signedYesOrder,
    no: signedNoOrder,
    yesPlan,
    noPlan,
  };
}

export async function submitPreparedFakBuyOrder(
  input: SubmitPreparedOrderInput,
): Promise<SubmitPreparedOrderResult> {
  const { config, side, client, preparedOrders, bestAsk } = input;
  const plan = preparedOrders.dryRun
    ? side === "YES"
      ? preparedOrders.yes
      : preparedOrders.no
    : side === "YES"
      ? preparedOrders.yesPlan
      : preparedOrders.noPlan;
  const maxPrice = plan.price;

  if (bestAsk !== null && bestAsk > maxPrice) {
    return {
      submitted: false,
      accepted: false,
      skipped: "PRICE_ABOVE_CAP",
      side,
      bestAsk,
      maxPrice,
    };
  }

  if (preparedOrders.dryRun) {
    return {
      ...plan,
      submitted: false,
      accepted: false,
      bestAsk,
      skipped: "DRY_RUN=1",
    };
  }

  if (!client) {
    throw new Error("live CLOB client was not initialized");
  }

  const signedOrder = side === "YES" ? preparedOrders.yes : preparedOrders.no;
  const t0 = performance.now();
  const response = await client.postOrder(signedOrder, OrderType.FAK);
  const t1 = performance.now();
  const accepted = isSuccessfulOrderResponse(response);

  if (accepted) {
    acquireTradeLock(config.tradeLockPath, {
      side,
      bestAsk,
      maxPrice,
      tokenID: plan.tokenID,
      response,
    });
  }

  return {
    submitted: true,
    accepted,
    side,
    latencyMs: Math.round(t1 - t0),
    response,
  };
}

export function isSuccessfulOrderResponse(response: unknown): boolean {
  if (!response || typeof response !== "object") {
    return false;
  }
  const maybe = response as {
    success?: unknown;
    error?: unknown;
    errorMsg?: unknown;
  };
  if (typeof maybe.error === "string" && maybe.error.trim() !== "") {
    return false;
  }
  if (typeof maybe.errorMsg === "string" && maybe.errorMsg.trim() !== "") {
    return false;
  }
  return maybe.success === true;
}

export function startWarmConnectionLoop(
  client: ClobClient | null,
  market: SelectedMarket,
): () => void {
  if (!client) {
    return () => undefined;
  }

  let stopped = false;
  let timer: NodeJS.Timeout | null = null;

  const warm = async (): Promise<void> => {
    try {
      await Promise.allSettled([
        client.getServerTime(),
        client.getOrderBook(market.yesTokenId),
        client.getOrderBook(market.noTokenId),
      ]);
    } finally {
      if (!stopped) {
        timer = setTimeout(warm, 5_000);
      }
    }
  };

  void warm();

  return () => {
    stopped = true;
    if (timer) {
      clearTimeout(timer);
    }
  };
}

function buildOrderPlan(
  config: BotConfig,
  market: SelectedMarket,
  side: "YES" | "NO",
): OrderPlan {
  return {
    dryRun: config.dryRun,
    side,
    tokenID: side === "YES" ? market.yesTokenId : market.noTokenId,
    amount: side === "YES" ? config.yesTradeUsd : config.noTradeUsd,
    price: side === "YES" ? config.yesMaxPrice : config.noMaxPrice,
    orderType: "FAK",
    market,
  };
}

function marketMatchesThreshold(
  market: GammaMarket,
  targetMarketText: string,
): boolean {
  const haystack = [
    market.question,
    market.title,
    market.slug,
    market.groupItemTitle,
    market.groupItemThreshold,
    stringifyUnknown(market.outcomes),
    stringifyUnknown(market.shortOutcomes),
  ]
    .filter(Boolean)
    .join(" ")
    .toLowerCase();

  const target = targetMarketText.toLowerCase();
  const numericTarget = escapeRegExp(target.replace(/\s+/g, ""));
  const thresholdPattern = new RegExp(
    `(^|[^0-9.])\\$?${numericTarget}\\s*t?(?=$|[^0-9.])`,
    "i",
  );

  return thresholdPattern.test(haystack);
}

function parseTokenIds(value: GammaMarket["clobTokenIds"]): [string, string] {
  const parsed = typeof value === "string" ? JSON.parse(value) : value;
  if (!Array.isArray(parsed) || parsed.length < 2) {
    throw new Error("missing clobTokenIds");
  }
  const yes = parsed[0];
  const no = parsed[1];
  if (typeof yes !== "string" || typeof no !== "string" || !yes || !no) {
    throw new Error("invalid clobTokenIds");
  }
  return [yes, no];
}

function parseTickSize(market: GammaMarket): TickSize {
  const raw = market.tickSize ?? market.orderPriceMinTickSize;
  if (raw === undefined || raw === null || raw === "") {
    throw new Error("missing tickSize/orderPriceMinTickSize");
  }
  const tickSize = String(raw);
  if (
    tickSize !== "0.1" &&
    tickSize !== "0.01" &&
    tickSize !== "0.001" &&
    tickSize !== "0.0001"
  ) {
    throw new Error(`unsupported tickSize: ${tickSize}`);
  }
  return tickSize;
}

function requiredString(value: unknown, name: string): string {
  if (typeof value !== "string" || value.trim() === "") {
    throw new Error(`missing ${name}`);
  }
  return value;
}

function stringifyUnknown(value: unknown): string {
  if (value === undefined || value === null) return "";
  if (typeof value === "string") return value;
  return JSON.stringify(value);
}

function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function acquireTradeLock(path: string, payload: unknown): void {
  mkdirSync(dirname(path), { recursive: true });
  writeFileSync(path, JSON.stringify({ ts: new Date().toISOString(), payload }, null, 2), {
    flag: "wx",
  });
}
