import "dotenv/config";

import { RelayClient, type DepositWalletCall } from "@polymarket/builder-relayer-client";
import { BuilderConfig, type BuilderApiKeyCreds } from "@polymarket/builder-signing-sdk";
import { createSecureClient } from "@polymarket/client";
import { privateKey } from "@polymarket/client/viem";
import { getContractConfig } from "@polymarket/clob-client-v2";
import {
  createPublicClient,
  createWalletClient,
  encodeFunctionData,
  formatUnits,
  http,
  parseAbi,
  type Hex,
} from "viem";
import { privateKeyToAccount } from "viem/accounts";
import { polygon } from "viem/chains";

const IRAN_JULY17_YES_TOKEN_ID =
  "61185693373091340617494150277851514641770797636186465474956889484881632951438";
const CONFIRMATION = "TRANSFER_IRAN_JULY17_YES_TO_BETA_WALLET";

const erc1155Abi = parseAbi([
  "function balanceOf(address account, uint256 id) view returns (uint256)",
  "function safeTransferFrom(address from, address to, uint256 id, uint256 amount, bytes data)",
]);

type Args = {
  execute: boolean;
  tokenId: string;
  amountRaw: bigint | "max";
};

async function main(): Promise<void> {
  const args = parseArgs(process.argv.slice(2));
  const chainId = Number(process.env.CHAIN_ID ?? "137");
  if (chainId !== 137) throw new Error(`only Polygon mainnet is configured, got CHAIN_ID=${chainId}`);
  const privateKeyValue = requiredHex("PRIVATE_KEY");
  const rpcUrl = process.env.POLYGON_RPC_URL ?? process.env.RPC_URL;
  if (!rpcUrl) throw new Error("POLYGON_RPC_URL or RPC_URL is required");

  const account = privateKeyToAccount(privateKeyValue);
  const walletClient = createWalletClient({ account, chain: polygon, transport: http(rpcUrl) });
  const publicClient = createPublicClient({ chain: polygon, transport: http(rpcUrl) });
  const relayer = new RelayClient(required("RELAYER_URL"), chainId, walletClient, buildBuilderConfig());

  const sourceWallet = (process.env.DEPOSIT_WALLET_ADDRESS as `0x${string}` | undefined)
    ?? ((await relayer.deriveDepositWalletAddress()) as `0x${string}`);
  const betaClient = await createSecureClient({
    signer: privateKey(privateKeyValue, { chain: polygon, transport: http(rpcUrl) }),
    credentials: {
      key: required("CLOB_API_KEY"),
      secret: required("CLOB_SECRET"),
      passphrase: required("CLOB_PASS_PHRASE"),
    } as never,
  });
  const targetWallet = betaClient.account.wallet as `0x${string}`;
  const contracts = getContractConfig(chainId);
  const conditionalTokens = contracts.conditionalTokens as `0x${string}`;
  const sourceDeployed = await relayer.getDeployed(sourceWallet, "WALLET");
  const [targetCode, sourceRawBalance, targetRawBalance] = await Promise.all([
    publicClient.getCode({ address: targetWallet }),
    publicClient.readContract({
      address: conditionalTokens,
      abi: erc1155Abi,
      functionName: "balanceOf",
      args: [sourceWallet, BigInt(args.tokenId)],
    }),
    publicClient.readContract({
      address: conditionalTokens,
      abi: erc1155Abi,
      functionName: "balanceOf",
      args: [targetWallet, BigInt(args.tokenId)],
    }),
  ]);
  const amountRaw = args.amountRaw === "max" ? sourceRawBalance : args.amountRaw;

  if (sourceWallet.toLowerCase() === targetWallet.toLowerCase()) {
    throw new Error("source and beta target wallets are identical; nothing to migrate");
  }
  if (!sourceDeployed) {
    throw new Error(`source deposit wallet is not deployed: ${sourceWallet}`);
  }
  if (amountRaw <= 0n) {
    throw new Error(`nothing to migrate; source raw balance is ${sourceRawBalance.toString()}`);
  }
  if (amountRaw > sourceRawBalance) {
    throw new Error(`requested raw amount ${amountRaw.toString()} exceeds source balance ${sourceRawBalance.toString()}`);
  }

  const call: DepositWalletCall = {
    target: conditionalTokens,
    value: "0",
    data: encodeFunctionData({
      abi: erc1155Abi,
      functionName: "safeTransferFrom",
      args: [sourceWallet, targetWallet, BigInt(args.tokenId), amountRaw, "0x"],
    }),
  };

  const summary = {
    mode: args.execute ? "execute" : "dry_run",
    owner: account.address,
    source_wallet: sourceWallet,
    target_beta_wallet: targetWallet,
    target_beta_wallet_deployed: Boolean(targetCode && targetCode !== "0x"),
    token_id: args.tokenId,
    conditional_tokens_contract: conditionalTokens,
    source_balance_raw_before: sourceRawBalance.toString(),
    source_balance_shares_before: formatUnits(sourceRawBalance, 6),
    target_balance_raw_before: targetRawBalance.toString(),
    target_balance_shares_before: formatUnits(targetRawBalance, 6),
    transfer_raw_amount: amountRaw.toString(),
    transfer_shares: formatUnits(amountRaw, 6),
    confirmation_required: CONFIRMATION,
  };

  if (!args.execute) {
    console.log(JSON.stringify({ ...summary, would_send_call: call }, null, 2));
    return;
  }

  if (process.env.POLYBOT_MIGRATE_POSITION_CONFIRM !== CONFIRMATION) {
    throw new Error(`set POLYBOT_MIGRATE_POSITION_CONFIRM=${CONFIRMATION} to execute`);
  }

  const deadline = Math.floor(Date.now() / 1000 + 600).toString();
  const response = await relayer.executeDepositWalletBatch([call], sourceWallet, deadline);
  const confirmed = await response.wait();
  const [sourceRawAfter, targetRawAfter] = await Promise.all([
    publicClient.readContract({
      address: conditionalTokens,
      abi: erc1155Abi,
      functionName: "balanceOf",
      args: [sourceWallet, BigInt(args.tokenId)],
    }),
    publicClient.readContract({
      address: conditionalTokens,
      abi: erc1155Abi,
      functionName: "balanceOf",
      args: [targetWallet, BigInt(args.tokenId)],
    }),
  ]);
  console.log(
    JSON.stringify(
      {
        ...summary,
        confirmed,
        source_balance_raw_after: sourceRawAfter.toString(),
        source_balance_shares_after: formatUnits(sourceRawAfter, 6),
        target_balance_raw_after: targetRawAfter.toString(),
        target_balance_shares_after: formatUnits(targetRawAfter, 6),
      },
      null,
      2,
    ),
  );
}

function parseArgs(argv: string[]): Args {
  let execute = false;
  let tokenId = IRAN_JULY17_YES_TOKEN_ID;
  let amountRaw: bigint | "max" = "max";
  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    if (arg === "--execute") {
      execute = true;
      continue;
    }
    if (arg === "--token-id") {
      tokenId = requiredValue(argv, i, arg);
      i += 1;
      continue;
    }
    if (arg === "--amount-raw") {
      const value = requiredValue(argv, i, arg);
      amountRaw = value === "max" ? "max" : BigInt(value);
      i += 1;
      continue;
    }
    throw new Error(`unexpected argument: ${arg}`);
  }
  if (!/^[0-9]+$/.test(tokenId)) throw new Error("--token-id must be a uint256 decimal string");
  if (amountRaw !== "max" && amountRaw <= 0n) throw new Error("--amount-raw must be positive or max");
  return { execute, tokenId, amountRaw };
}

function requiredValue(argv: string[], index: number, flag: string): string {
  const value = argv[index + 1];
  if (!value || value.startsWith("--")) throw new Error(`${flag} requires a value`);
  return value;
}

function buildBuilderConfig(): BuilderConfig {
  const creds: BuilderApiKeyCreds = {
    key: required("BUILDER_API_KEY"),
    secret: required("BUILDER_SECRET"),
    passphrase: required("BUILDER_PASS_PHRASE"),
  };
  return new BuilderConfig({ localBuilderCreds: creds });
}

function required(name: string): string {
  const value = process.env[name];
  if (!value?.trim()) throw new Error(`${name} is required`);
  return value.trim();
}

function requiredHex(name: string): Hex {
  const value = required(name);
  if (!/^0x[0-9a-fA-F]+$/.test(value)) throw new Error(`${name} must be a hex string`);
  return value as Hex;
}

main().catch((error: unknown) => {
  console.error(error instanceof Error ? error.stack : error);
  process.exitCode = 1;
});
