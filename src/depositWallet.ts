import "dotenv/config";

import {
  RelayClient,
  type DepositWalletCall,
} from "@polymarket/builder-relayer-client";
import {
  BuilderConfig,
  type BuilderApiKeyCreds,
} from "@polymarket/builder-signing-sdk";
import {
  AssetType,
  ClobClient,
  SignatureTypeV2,
  getContractConfig,
} from "@polymarket/clob-client-v2";
import {
  encodeFunctionData,
  erc20Abi,
  formatUnits,
  http,
  maxUint256,
  parseAbi,
  type Hex,
} from "viem";
import { createPublicClient, createWalletClient } from "viem";
import { polygon } from "viem/chains";
import { privateKeyToAccount } from "viem/accounts";

type SetupMode = "derive" | "deploy" | "approve" | "sync";

const pUsdAbi = parseAbi([
  "function balanceOf(address) view returns (uint256)",
  "function allowance(address,address) view returns (uint256)",
]);

async function main(): Promise<void> {
  const mode = parseMode(process.argv[2] ?? "derive");
  const chainId = Number(process.env.CHAIN_ID ?? "137");
  if (chainId !== 137) {
    throw new Error(`only Polygon mainnet is configured, got CHAIN_ID=${chainId}`);
  }

  const privateKey = requiredHex("PRIVATE_KEY");
  const relayerUrl = required("RELAYER_URL");
  const rpcUrl = process.env.POLYGON_RPC_URL ?? process.env.RPC_URL;
  if (!rpcUrl) {
    throw new Error("POLYGON_RPC_URL or RPC_URL is required");
  }

  const account = privateKeyToAccount(privateKey);
  const walletClient = createWalletClient({
    account,
    chain: polygon,
    transport: http(rpcUrl),
  });
  const publicClient = createPublicClient({
    chain: polygon,
    transport: http(rpcUrl),
  });
  const relayer = new RelayClient(
    relayerUrl,
    chainId,
    walletClient,
    buildBuilderConfig(),
  );
  const depositWalletAddress =
    (process.env.DEPOSIT_WALLET_ADDRESS as `0x${string}` | undefined) ??
    ((await relayer.deriveDepositWalletAddress()) as `0x${string}`);
  const deployed = await relayer.getDeployed(depositWalletAddress, "WALLET");
  const contracts = getContractConfig(chainId);

  if (mode === "deploy") {
    if (!deployed) {
      const response = await relayer.deployDepositWallet();
      const confirmed = await response.wait();
      console.log(JSON.stringify({ mode, depositWalletAddress, confirmed }, null, 2));
    } else {
      console.log(JSON.stringify({ mode, depositWalletAddress, deployed }, null, 2));
    }
    return;
  }

  if (mode === "approve") {
    if (!deployed) {
      throw new Error(`deposit wallet is not deployed: ${depositWalletAddress}`);
    }

    const calls = [
      approveCall(contracts.collateral, contracts.exchangeV2),
      approveCall(contracts.collateral, contracts.negRiskExchangeV2),
    ];
    const deadline = Math.floor(Date.now() / 1000 + 600).toString();
    const response = await relayer.executeDepositWalletBatch(
      calls,
      depositWalletAddress,
      deadline,
    );
    const confirmed = await response.wait();
    console.log(JSON.stringify({ mode, depositWalletAddress, confirmed }, null, 2));
    return;
  }

  const onchain = await readOnchainBuyingPower({
    publicClient,
    collateral: contracts.collateral as `0x${string}`,
    wallet: depositWalletAddress,
    exchangeV2: contracts.exchangeV2 as `0x${string}`,
    negRiskExchangeV2: contracts.negRiskExchangeV2 as `0x${string}`,
  });

  if (mode === "sync") {
    const clob = buildDepositClobClient({ chainId, walletClient, depositWalletAddress });
    await clob.updateBalanceAllowance({ asset_type: AssetType.COLLATERAL });
    const clobBalanceAllowance = await clob.getBalanceAllowance({
      asset_type: AssetType.COLLATERAL,
    });
    console.log(
      JSON.stringify(
        {
          mode,
          owner: account.address,
          depositWalletAddress,
          deployed,
          onchain,
          clobBalanceAllowance,
        },
        null,
        2,
      ),
    );
    return;
  }

  console.log(
    JSON.stringify(
      {
        mode,
        owner: account.address,
        depositWalletAddress,
        deployed,
        onchain,
        next:
          "If deployed=false, run `npm run deposit-wallet -- deploy`. Then transfer pUSD to depositWalletAddress, run `npm run deposit-wallet -- approve`, then `npm run deposit-wallet -- sync`.",
      },
      null,
      2,
    ),
  );
}

function buildBuilderConfig(): BuilderConfig {
  const creds: BuilderApiKeyCreds = {
    key: required("BUILDER_API_KEY"),
    secret: required("BUILDER_SECRET"),
    passphrase: required("BUILDER_PASS_PHRASE"),
  };
  return new BuilderConfig({ localBuilderCreds: creds });
}

function buildDepositClobClient(input: {
  chainId: number;
  walletClient: ReturnType<typeof createWalletClient>;
  depositWalletAddress: `0x${string}`;
}): ClobClient {
  return new ClobClient({
    host: process.env.CLOB_HOST ?? "https://clob.polymarket.com",
    chain: input.chainId,
    signer: input.walletClient,
    creds: {
      key: required("CLOB_API_KEY"),
      secret: required("CLOB_SECRET"),
      passphrase: required("CLOB_PASS_PHRASE"),
    },
    signatureType: SignatureTypeV2.POLY_1271,
    funderAddress: input.depositWalletAddress,
  });
}

function approveCall(token: string, spender: string): DepositWalletCall {
  return {
    target: token,
    value: "0",
    data: encodeFunctionData({
      abi: erc20Abi,
      functionName: "approve",
      args: [spender as `0x${string}`, maxUint256],
    }),
  };
}

async function readOnchainBuyingPower(input: {
  publicClient: ReturnType<typeof createPublicClient>;
  collateral: `0x${string}`;
  wallet: `0x${string}`;
  exchangeV2: `0x${string}`;
  negRiskExchangeV2: `0x${string}`;
}): Promise<Record<string, string>> {
  const [balance, exchangeV2Allowance, negRiskExchangeV2Allowance] = await Promise.all([
    input.publicClient.readContract({
      address: input.collateral,
      abi: pUsdAbi,
      functionName: "balanceOf",
      args: [input.wallet],
    }),
    input.publicClient.readContract({
      address: input.collateral,
      abi: pUsdAbi,
      functionName: "allowance",
      args: [input.wallet, input.exchangeV2],
    }),
    input.publicClient.readContract({
      address: input.collateral,
      abi: pUsdAbi,
      functionName: "allowance",
      args: [input.wallet, input.negRiskExchangeV2],
    }),
  ]);

  return {
    pUsd: formatUnits(balance, 6),
    exchangeV2Allowance: formatUnits(exchangeV2Allowance, 6),
    negRiskExchangeV2Allowance: formatUnits(negRiskExchangeV2Allowance, 6),
  };
}

function parseMode(value: string): SetupMode {
  if (value === "derive" || value === "deploy" || value === "approve" || value === "sync") {
    return value;
  }
  throw new Error("mode must be one of: derive, deploy, approve, sync");
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
