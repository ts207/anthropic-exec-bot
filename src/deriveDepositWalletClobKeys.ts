import "dotenv/config";

import { webcrypto } from "node:crypto";
import { createWalletClient, http, type Hex } from "viem";
import { privateKeyToAccount } from "viem/accounts";
import { polygon } from "viem/chains";
import { createL1Headers } from "@polymarket/clob-client-v2";

// The clob-client-v2 createOrDeriveApiKey() binds the key to the EOA signer
// address. POLY_1271 orders carry the deposit wallet as order.signer, and the
// exchange requires the API key address to match it — so the key must be
// derived with POLY_ADDRESS = deposit wallet. createL1Headers supports that
// via its optional address argument; the client just never passes it.

if (!globalThis.crypto?.subtle) {
  Object.defineProperty(globalThis, "crypto", { value: webcrypto });
}

async function main(): Promise<void> {
  const privateKey = requiredHex("PRIVATE_KEY");
  const depositWalletAddress = requiredHex("DEPOSIT_WALLET_ADDRESS");
  const host = process.env.CLOB_HOST ?? "https://clob.polymarket.com";
  const chainId = Number(process.env.CHAIN_ID ?? "137");

  const account = privateKeyToAccount(privateKey);
  const signer = createWalletClient({
    account,
    chain: polygon,
    transport: http(process.env.POLYGON_RPC_URL ?? process.env.RPC_URL),
  });

  const headers = stringifyHeaders(await createL1Headers(signer, chainId, 0, undefined, depositWalletAddress));

  let raw = await request(`${host}/auth/derive-api-key`, "GET", headers);
  if (!raw?.apiKey) {
    raw = await request(`${host}/auth/api-key`, "POST", headers);
  }
  if (!raw?.apiKey) {
    throw new Error(`derive and create both failed: ${JSON.stringify(raw)}`);
  }
  console.log(
    JSON.stringify(
      {
        bound_address: depositWalletAddress,
        CLOB_API_KEY: raw.apiKey,
        CLOB_SECRET: raw.secret,
        CLOB_PASS_PHRASE: raw.passphrase,
      },
      null,
      2,
    ),
  );
}

async function request(url: string, method: string, headers: Record<string, string>): Promise<Record<string, string> | null> {
  const response = await fetch(url, { method, headers });
  const text = await response.text();
  try {
    return JSON.parse(text) as Record<string, string>;
  } catch {
    if (!response.ok) {
      console.error(`${method} ${url} -> ${response.status}: ${text.slice(0, 300)}`);
    }
    return null;
  }
}

function stringifyHeaders(headers: Record<string, string | number | boolean>): Record<string, string> {
  return Object.fromEntries(Object.entries(headers).map(([key, value]) => [key, String(value)]));
}

function requiredHex(name: string): Hex {
  const value = process.env[name];
  if (!value?.trim()) {
    throw new Error(`${name} is required`);
  }
  if (!/^0x[0-9a-fA-F]+$/.test(value)) {
    throw new Error(`${name} must be a hex string`);
  }
  return value as Hex;
}

main().catch((error: unknown) => {
  console.error(error instanceof Error ? error.stack : error);
  process.exitCode = 1;
});
