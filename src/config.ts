import "dotenv/config";

export type BotConfig = {
  privateKey: `0x${string}` | null;
  funderAddress: `0x${string}` | null;
  depositWalletAddress: `0x${string}` | null;
  clobApiKey: string | null;
  clobSecret: string | null;
  clobPassPhrase: string | null;
  eventSlug: string;
  targetMarketText: string;
  npmUrl: string;
  npmApiUrl: string | null;
  yesTradeUsd: number;
  noTradeUsd: number;
  yesMaxPrice: number;
  noMaxPrice: number;
  dryRun: boolean;
  pollMs: number;
  clobHost: string;
  polygonRpcUrl: string;
  chainId: number;
  logsDir: string;
  tradeLockPath: string;
  npmProfileDir: string;
  orderbookMaxAgeMs: number;
};

export function loadConfig(env = process.env): BotConfig {
  const dryRun = parseBoolean(env.DRY_RUN ?? "1");
  const logsDir = env.LOGS_DIR ?? "logs";

  const config: BotConfig = {
    privateKey: optionalHex(env.PRIVATE_KEY),
    funderAddress: optionalHex(env.FUNDER_ADDRESS),
    depositWalletAddress: optionalHex(env.DEPOSIT_WALLET_ADDRESS),
    clobApiKey: optionalString(env.CLOB_API_KEY),
    clobSecret: optionalString(env.CLOB_SECRET),
    clobPassPhrase: optionalString(env.CLOB_PASS_PHRASE),
    eventSlug: required(env.EVENT_SLUG, "EVENT_SLUG"),
    targetMarketText: required(env.TARGET_MARKET_TEXT, "TARGET_MARKET_TEXT"),
    npmUrl: required(env.NPM_URL, "NPM_URL"),
    npmApiUrl: optionalString(env.NPM_API_URL) ?? buildNpmApiUrl(env.NPM_URL),
    yesTradeUsd: positiveNumber(env.YES_TRADE_USD, "YES_TRADE_USD"),
    noTradeUsd: positiveNumber(env.NO_TRADE_USD, "NO_TRADE_USD"),
    yesMaxPrice: price(env.YES_MAX_PRICE, "YES_MAX_PRICE"),
    noMaxPrice: price(env.NO_MAX_PRICE, "NO_MAX_PRICE"),
    dryRun,
    pollMs: positiveInteger(env.POLL_MS, "POLL_MS"),
    clobHost: env.CLOB_HOST ?? "https://clob.polymarket.com",
    polygonRpcUrl: env.POLYGON_RPC_URL ?? "https://polygon-bor-rpc.publicnode.com",
    chainId: positiveInteger(env.CHAIN_ID ?? "137", "CHAIN_ID"),
    logsDir,
    tradeLockPath: env.TRADE_LOCK_PATH ?? `${logsDir}/traded.lock`,
    npmProfileDir: env.NPM_PROFILE_DIR ?? ".playwright-npm-profile",
    orderbookMaxAgeMs: positiveInteger(env.ORDERBOOK_MAX_AGE_MS ?? "1000", "ORDERBOOK_MAX_AGE_MS"),
  };

  if (!dryRun) {
    if (!config.privateKey) {
      throw new Error("PRIVATE_KEY is required when DRY_RUN=0");
    }
    if (!config.funderAddress) {
      throw new Error("FUNDER_ADDRESS is required when DRY_RUN=0");
    }
    if (!config.depositWalletAddress) {
      throw new Error("DEPOSIT_WALLET_ADDRESS is required when DRY_RUN=0");
    }
    if (!config.clobApiKey || !config.clobSecret || !config.clobPassPhrase) {
      throw new Error(
        "CLOB_API_KEY, CLOB_SECRET, and CLOB_PASS_PHRASE are required when DRY_RUN=0",
      );
    }
  }

  return config;
}

function required(value: string | undefined, name: string): string {
  if (!value?.trim()) {
    throw new Error(`${name} is required`);
  }
  return value.trim();
}

function optionalString(value: string | undefined): string | null {
  if (!value?.trim()) {
    return null;
  }
  return value.trim();
}

function buildNpmApiUrl(npmUrl: string | undefined): string | null {
  const url = optionalString(npmUrl);
  const companyId = /\/companies\/([^/]+)\/data/.exec(url ?? "")?.[1];
  if (!companyId) {
    return null;
  }
  return `https://api-npm17-data-company-pricing-review-prod.k8s-prod-1.npmdev.net/api/public/companies/${companyId}`;
}

function optionalHex(value: string | undefined): `0x${string}` | null {
  if (!value || value.includes("YOUR_")) {
    return null;
  }
  if (!/^0x[0-9a-fA-F]+$/.test(value)) {
    throw new Error(`invalid hex value: ${value}`);
  }
  return value as `0x${string}`;
}

function positiveNumber(value: string | undefined, name: string): number {
  const parsed = Number(required(value, name));
  if (!Number.isFinite(parsed) || parsed <= 0) {
    throw new Error(`${name} must be a positive number`);
  }
  return parsed;
}

function positiveInteger(value: string | undefined, name: string): number {
  const parsed = Number(required(value, name));
  if (!Number.isInteger(parsed) || parsed <= 0) {
    throw new Error(`${name} must be a positive integer`);
  }
  return parsed;
}

function price(value: string | undefined, name: string): number {
  const parsed = positiveNumber(value, name);
  if (parsed > 1) {
    throw new Error(`${name} must be <= 1`);
  }
  return parsed;
}

function parseBoolean(value: string): boolean {
  if (value === "1" || value.toLowerCase() === "true") return true;
  if (value === "0" || value.toLowerCase() === "false") return false;
  throw new Error(`invalid boolean value: ${value}`);
}
