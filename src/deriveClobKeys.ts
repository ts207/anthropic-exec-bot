import "dotenv/config";

import { ClobClient } from "@polymarket/clob-client-v2";
import { createWalletClient, http, type Hex } from "viem";
import { privateKeyToAccount } from "viem/accounts";

async function main(): Promise<void> {
  const privateKey = requiredHex("PRIVATE_KEY");
  const account = privateKeyToAccount(privateKey);
  const signer = createWalletClient({
    account,
    transport: http(process.env.POLYGON_RPC_URL),
  });

  const client = new ClobClient({
    host: process.env.CLOB_HOST ?? "https://clob.polymarket.com",
    chain: Number(process.env.CHAIN_ID ?? "137"),
    signer,
  });

  const creds = await client.createOrDeriveApiKey();
  console.log(`CLOB_API_KEY=${creds.key}`);
  console.log(`CLOB_SECRET=${creds.secret}`);
  console.log(`CLOB_PASS_PHRASE=${creds.passphrase}`);
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
