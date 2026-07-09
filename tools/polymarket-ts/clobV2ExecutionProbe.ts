import "dotenv/config";

import { webcrypto } from "node:crypto";
import {
  AssetType,
  ClobClient,
  OrderType,
  Side,
  SignatureTypeV2,
  type OpenOrder,
} from "@polymarket/clob-client-v2";
import { createWalletClient, http, type Hex } from "viem";
import { privateKeyToAccount } from "viem/accounts";
import { polygon } from "viem/chains";

type ProbeArgs = {
  tokenId: string;
  conditionId: string | null;
  side: Side;
  amount: number;
  price: number;
  tickSize: "0.1" | "0.01" | "0.001" | "0.0001";
  negRisk: boolean;
  post: boolean;
};

const CONDITIONAL_DECIMALS = 1_000_000;

if (!globalThis.crypto?.subtle) {
  Object.defineProperty(globalThis, "crypto", { value: webcrypto });
}

async function main(): Promise<void> {
  const args = parseArgs(process.argv.slice(2));
  const privateKey = requiredHex("PRIVATE_KEY");
  const depositWalletAddress = requiredHex("DEPOSIT_WALLET_ADDRESS");
  const chainId = Number(process.env.CHAIN_ID ?? "137");
  if (chainId !== 137) {
    throw new Error(`only Polygon mainnet is configured, got CHAIN_ID=${chainId}`);
  }

  const account = privateKeyToAccount(privateKey);
  const signer = createWalletClient({
    account,
    chain: polygon,
    transport: http(process.env.POLYGON_RPC_URL ?? process.env.RPC_URL),
  });
  const client = new ClobClient({
    host: process.env.CLOB_HOST ?? "https://clob.polymarket.com",
    chain: chainId,
    signer,
    creds: {
      key: required("CLOB_API_KEY"),
      secret: required("CLOB_SECRET"),
      passphrase: required("CLOB_PASS_PHRASE"),
    },
    signatureType: SignatureTypeV2.POLY_1271,
    funderAddress: depositWalletAddress,
  });

  const [balanceAllowance, orderBook, openOrders] = await Promise.all([
    client.getBalanceAllowance({
      asset_type: AssetType.CONDITIONAL,
      token_id: args.tokenId,
    }),
    client.getOrderBook(args.tokenId),
    args.conditionId
      ? client.getOpenOrders({ market: args.conditionId }, true)
      : Promise.resolve([] as OpenOrder[]),
  ]);

  const signedOrder = await client.createMarketOrder(
    {
      tokenID: args.tokenId,
      amount: args.amount,
      side: args.side,
      price: args.price,
      orderType: OrderType.FAK,
    },
    {
      tickSize: args.tickSize,
      negRisk: args.negRisk,
    },
  );
  const warnings = probeWarnings(args, orderBook.min_order_size);
  const postGuard = postGuardResult(args, orderBook);
  let postResponse: unknown = null;
  if (args.post) {
    if (!postGuard.allowed) {
      throw new Error(`posted probe blocked: ${postGuard.reason}`);
    }
    postResponse = await client.postOrder(signedOrder, OrderType.FAK);
  }

  console.log(
    JSON.stringify(
      {
        probe: "clob-client-v2-deposit-wallet-fak",
        posted: args.post,
        note: args.post
          ? "Order was posted as a deliberately non-crossing FAK SELL probe."
          : "Order was signed locally for validation only and was not posted.",
        account: {
          owner: account.address,
          depositWalletAddress,
          chainId,
          signatureType: SignatureTypeV2.POLY_1271,
        },
        orderRequest: {
          tokenId: args.tokenId,
          conditionId: args.conditionId,
          side: args.side,
          amount: args.amount,
          price: args.price,
          orderType: OrderType.FAK,
          tickSize: args.tickSize,
          negRisk: args.negRisk,
        },
        balance: summarizeBalance(balanceAllowance.balance),
        allowances: summarizeAllowances(balanceAllowance.allowances),
        orderBook: {
          bestBid: bestBid(orderBook.bids),
          bestAsk: bestAsk(orderBook.asks),
          tickSize: orderBook.tick_size,
          minOrderSize: orderBook.min_order_size,
          negRisk: orderBook.neg_risk,
          hash: orderBook.hash,
        },
        warnings,
        postGuard,
        postResponse,
        openOrders: {
          count: openOrders.length,
          orders: openOrders.map(summarizeOpenOrder),
        },
        signedOrder: summarizeSignedOrder(signedOrder),
      },
      null,
      2,
    ),
  );
}

function parseArgs(argv: string[]): ProbeArgs {
  const values = new Map<string, string>();
  for (let i = 0; i < argv.length; i += 1) {
    const item = argv[i];
    if (!item?.startsWith("--")) {
      throw new Error(`unexpected argument: ${item}`);
    }
    const key = item.slice(2);
    const value = argv[i + 1];
    if (!value || value.startsWith("--")) {
      throw new Error(`missing value for --${key}`);
    }
    values.set(key, value);
    i += 1;
  }
  const tokenId = values.get("token-id");
  if (!tokenId) {
    throw new Error("--token-id is required");
  }
  return {
    tokenId,
    conditionId: values.get("condition-id") ?? null,
    side: parseSide(values.get("side") ?? "SELL"),
    amount: positiveNumber(values.get("amount") ?? "5", "amount"),
    price: price(values.get("price") ?? "0.03", "price"),
    tickSize: parseTickSize(values.get("tick-size") ?? "0.01"),
    negRisk: parseBoolean(values.get("neg-risk") ?? "false"),
    post: parseBoolean(values.get("post") ?? "false"),
  };
}

function parseSide(value: string): Side {
  const normalized = value.toUpperCase();
  if (normalized === Side.BUY) return Side.BUY;
  if (normalized === Side.SELL) return Side.SELL;
  throw new Error("--side must be BUY or SELL");
}

function parseTickSize(value: string): ProbeArgs["tickSize"] {
  if (value === "0.1" || value === "0.01" || value === "0.001" || value === "0.0001") {
    return value;
  }
  throw new Error("--tick-size must be one of 0.1, 0.01, 0.001, 0.0001");
}

function parseBoolean(value: string): boolean {
  if (value === "1" || value.toLowerCase() === "true") return true;
  if (value === "0" || value.toLowerCase() === "false") return false;
  throw new Error("boolean value must be true/false or 1/0");
}

function price(value: string, name: string): number {
  const parsed = positiveNumber(value, name);
  if (parsed > 1) {
    throw new Error(`${name} must be <= 1`);
  }
  return parsed;
}

function positiveNumber(value: string, name: string): number {
  const parsed = Number(value);
  if (!Number.isFinite(parsed) || parsed <= 0) {
    throw new Error(`${name} must be a positive number`);
  }
  return parsed;
}

function summarizeBalance(rawBalance: string): Record<string, string | number> {
  return {
    raw: rawBalance,
    shares: Number(rawBalance) / CONDITIONAL_DECIMALS,
  };
}

function probeWarnings(args: ProbeArgs, minOrderSize: string): string[] {
  const warnings: string[] = [];
  const parsedMinOrderSize = Number(minOrderSize);
  if (Number.isFinite(parsedMinOrderSize) && args.amount < parsedMinOrderSize) {
    warnings.push(`amount_below_min_order_size:${args.amount}<${parsedMinOrderSize}`);
  }
  return warnings;
}

function postGuardResult(args: ProbeArgs, orderBook: { asks: Array<{ price: string }>; min_order_size: string }): { allowed: boolean; reason: string } {
  if (!args.post) {
    return { allowed: true, reason: "sign_only" };
  }
  if (args.side !== Side.SELL) {
    return { allowed: false, reason: "posted_probe_requires_sell" };
  }
  const parsedMinOrderSize = Number(orderBook.min_order_size);
  if (Number.isFinite(parsedMinOrderSize) && args.amount < parsedMinOrderSize) {
    return { allowed: false, reason: `amount_below_min_order_size:${args.amount}<${parsedMinOrderSize}` };
  }
  const ask = bestAsk(orderBook.asks);
  if (ask === null) {
    return { allowed: false, reason: "missing_best_ask" };
  }
  if (args.price <= ask) {
    return { allowed: false, reason: `price_may_cross:${args.price}<=${ask}` };
  }
  if (args.price < 0.95) {
    return { allowed: false, reason: `posted_probe_price_too_low:${args.price}<0.95` };
  }
  return { allowed: true, reason: `non_crossing_sell_probe:${args.price}>${ask}` };
}

function summarizeAllowances(allowances: Record<string, string>): Record<string, string | number | string[]> {
  const entries = Object.entries(allowances);
  return {
    count: entries.length,
    spenderAddresses: entries.map(([address]) => address),
    maxAllowanceCount: entries.filter(([, value]) => /^115792089237316195423570985008687907853269984665640564/.test(value)).length,
  };
}

function bestBid(levels: Array<{ price: string }>): number | null {
  const prices = levels.map((level) => Number(level.price)).filter(Number.isFinite);
  return prices.length ? Math.max(...prices) : null;
}

function bestAsk(levels: Array<{ price: string }>): number | null {
  const prices = levels.map((level) => Number(level.price)).filter(Number.isFinite);
  return prices.length ? Math.min(...prices) : null;
}

function summarizeOpenOrder(order: OpenOrder): Record<string, string | number> {
  return {
    id: order.id,
    status: order.status,
    market: order.market,
    assetId: order.asset_id,
    side: order.side,
    originalSize: order.original_size,
    sizeMatched: order.size_matched,
    price: order.price,
    orderType: order.order_type,
  };
}

function summarizeSignedOrder(order: unknown): Record<string, unknown> {
  if (!order || typeof order !== "object") {
    return { created: false, type: typeof order };
  }
  const record = order as Record<string, unknown>;
  return {
    created: true,
    maker: record.maker,
    signer: record.signer,
    tokenId: record.tokenId,
    side: record.side,
    signatureType: record.signatureType,
    makerAmount: record.makerAmount,
    takerAmount: record.takerAmount,
    expiration: record.expiration,
    timestamp: record.timestamp,
    hasSignature: typeof record.signature === "string" && record.signature.length > 0,
  };
}

function required(name: string): string {
  const value = process.env[name];
  if (!value?.trim()) {
    throw new Error(`${name} is required`);
  }
  return value.trim();
}

function requiredHex(name: string): Hex {
  const value = required(name);
  if (!/^0x[0-9a-fA-F]+$/.test(value)) {
    throw new Error(`${name} must be a hex string`);
  }
  return value as Hex;
}

main().catch((error: unknown) => {
  console.error(error instanceof Error ? error.stack : error);
  process.exitCode = 1;
});
