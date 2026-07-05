import "dotenv/config";

import { fileURLToPath } from "node:url";
import { join } from "node:path";
import { appendJsonl } from "./logging.ts";
import { loadStrategyConfig, type LoadedStrategyConfig } from "./strategy/valuationConfig.ts";
import { fetchNpmEvidence, withEligibleMax } from "./strategy/npmValuationSource.ts";
import { fetchGammaEvent, parseValuationLegs } from "./strategy/marketParser.ts";
import { fetchBookQuote } from "./strategy/orderbookSource.ts";
import { decideThresholdLeg } from "./strategy/valuationDecision.ts";
import { curveMonotonicityCandidates } from "./strategy/curveArbitrage.ts";
import { calendarDominanceCandidates } from "./strategy/calendarArbitrage.ts";
import { rankingAlertCandidates } from "./strategy/rankingSimulator.ts";
import { buildImpliedCurves } from "./strategy/impliedCurve.ts";
import { executeCandidate, postedProbe } from "./strategy/betaExecution.ts";
import { candidateLockPath, isCandidateLocked, listLocks, liveAckPath, probePath, writeJson, writeLiveAck } from "./strategy/stateStore.ts";
import type { BookQuote, CurvePoint, EventConfig, NpmEvidence, StrategyConfig, ValuationCandidate, ValuationLeg } from "./strategy/signalTypes.ts";

type Command = "scan" | "run" | "preflight" | "probe" | "ack";

type ScanResult = {
  evidence: NpmEvidence[];
  legs: ValuationLeg[];
  candidates: ValuationCandidate[];
};

async function main(): Promise<void> {
  const { command, args } = parseCli(process.argv.slice(2));
  const configPath = args.get("config") ?? "configs/private-valuations-july31.json";
  const loaded = await loadStrategyConfig(configPath);
  if (command === "ack") {
    const path = await writeLiveAck(loaded.config, loaded.hash);
    return print({ ok: true, configHash: loaded.hash, liveAckPath: path });
  }
  if (command === "preflight") {
    return print(await preflight(loaded));
  }
  if (command === "probe") {
    return print(await runProbe(loaded, args));
  }
  if (command === "run") {
    for (;;) {
      await scanOnce(loaded);
      await sleep(loaded.config.pollMs);
    }
  }
  return print(scanSummary(await scanOnce(loaded)));
}

export async function scanOnce(loaded: LoadedStrategyConfig): Promise<ScanResult> {
  const { config } = loaded;
  const evidenceByCompany = await loadEvidence(config);
  const allLegs: ValuationLeg[] = [];
  const quotes = new Map<string, BookQuote>();
  const curvePoints: CurvePoint[] = [];
  const thresholdCandidates: ValuationCandidate[] = [];

  for (const eventConfig of config.events) {
    const event = await fetchGammaEvent(eventConfig.slug);
    await appendJsonl(join(config.logsDir, "events.jsonl"), {
      eventSlug: event.slug,
      title: event.title,
      rawHash: event.rawHash,
      kind: eventConfig.kind,
    });
    const legs = parseValuationLegs(event, eventConfig);
    allLegs.push(...legs);
    for (const leg of legs) {
      await appendJsonl(join(config.logsDir, "legs.jsonl"), leg);
      const quote = leg.yesTokenId ? await safeQuote(leg.yesTokenId) : undefined;
      if (quote) quotes.set(leg.marketSlug, quote);
      if (leg.threshold !== undefined && quote?.bestAsk !== null && quote?.bestAsk !== undefined) {
        curvePoints.push({ leg, yesAsk: quote.bestAsk });
      }
      if (leg.eventKind === "threshold") {
        const rawEvidence = evidenceByCompany.get(leg.company);
        const evidence = rawEvidence ? withEligibleMax(rawEvidence, leg.marketWindowStartIso, leg.deadlineIso) : undefined;
        const locked = await isCandidateLocked(config, candidateShell(leg));
        const decision = decideThresholdLeg(leg, evidence, quote, config, locked);
        thresholdCandidates.push(decision);
      }
    }
  }

  const rankingLegs = allLegs.filter((leg) => leg.eventKind === "ranking");
  const rawCandidates = rankCandidates([
    ...thresholdCandidates,
    ...curveMonotonicityCandidates(curvePoints, quotes, config),
    ...calendarDominanceCandidates(curvePoints, quotes, config),
    ...rankingAlertCandidates(rankingLegs, evidenceByCompany, quotes, config, buildImpliedCurves(curvePoints)),
  ]);
  const candidates = rankCandidates(applyCaps(config, rawCandidates, await listLocks(config)));

  const ranked = rankCandidates(candidates);
  for (const candidate of ranked) {
    await appendJsonl(join(config.logsDir, "decisions.jsonl"), candidate);
    if (candidate.status === "candidate" || candidate.status === "alert") {
      const execution = await executeCandidate(candidate, config, loaded.hash);
      await appendJsonl(join(config.logsDir, "orders.jsonl"), {
        candidate,
        execution,
      });
    }
  }
  await writeJson(join(config.stateDir, "last_candidates.json"), ranked);
  return { evidence: [...evidenceByCompany.values()], legs: allLegs, candidates: ranked };
}

function scanSummary(result: ScanResult): Record<string, unknown> {
  const candidates = result.candidates.filter((candidate) => candidate.status === "candidate");
  const alerts = result.candidates.filter((candidate) => candidate.status === "alert");
  return {
    evidenceCount: result.evidence.length,
    legCount: result.legs.length,
    candidateCount: candidates.length,
    alertCount: alerts.length,
    topCandidates: candidates.slice(0, 10).map(compactCandidate),
    topAlerts: alerts.slice(0, 10).map(compactCandidate),
  };
}

function compactCandidate(candidate: ValuationCandidate): Record<string, unknown> {
  return {
    signalType: candidate.signalType,
    status: candidate.status,
    company: candidate.company,
    marketSlug: candidate.marketSlug,
    deadline: candidate.deadline,
    threshold: candidate.threshold,
    yesAsk: candidate.yesAsk,
    distancePct: candidate.distancePct,
    fairPrice: candidate.fairPrice,
    edge: candidate.edge,
    edgeScore: candidate.edgeScore,
    confidenceScore: candidate.confidenceScore,
    orderUsd: candidate.orderUsd,
    orderTemplate: candidate.orderTemplate,
    liveAllowed: candidate.liveAllowed,
    reason: candidate.reason,
  };
}

async function loadEvidence(config: StrategyConfig): Promise<Map<string, NpmEvidence>> {
  const evidenceByCompany = new Map<string, NpmEvidence>();
  for (const company of config.companies) {
    if (!company.npmCompanyId) continue;
    const evidence = await fetchNpmEvidence(company);
    if (!evidence) continue;
    evidenceByCompany.set(company.name, evidence);
    await appendJsonl(join(config.logsDir, "evidence.jsonl"), evidence);
  }
  return evidenceByCompany;
}

async function preflight(loaded: LoadedStrategyConfig): Promise<Record<string, unknown>> {
  const config = loaded.config;
  const liveAck = liveAckPath(config, loaded.hash);
  return {
    ok: true,
    configHash: loaded.hash,
    mode: config.mode,
    liveAckPath: liveAck,
    liveAckPresent: await filePresent(liveAck),
    betaSdkEnv: {
      privateKey: Boolean(process.env.PRIVATE_KEY),
      clobApiKey: Boolean(process.env.CLOB_API_KEY),
      clobSecret: Boolean(process.env.CLOB_SECRET),
      clobPassPhrase: Boolean(process.env.CLOB_PASS_PHRASE),
      postingArmed: process.env.POLYBOT_TS_BRIDGE_ALLOW_POST === "1",
    },
    companies: config.companies.map((company) => ({
      name: company.name,
      npmCompanyId: company.npmCompanyId ?? null,
      hasNpmSource: Boolean(company.npmCompanyId),
    })),
    events: config.events.map((event) => ({
      slug: event.slug,
      kind: event.kind,
      mode: event.mode ?? config.mode,
      deadlineIso: event.deadlineIso,
    })),
  };
}

async function runProbe(loaded: LoadedStrategyConfig, args: Map<string, string>): Promise<Record<string, unknown>> {
  const marketSlug = requiredArg(args, "market-slug");
  const tokenId = args.get("token-id");
  let selectedToken = tokenId;
  if (!selectedToken) {
    const eventSlug = requiredArg(args, "event-slug");
    const eventConfig = loaded.config.events.find((event) => event.slug === eventSlug);
    if (!eventConfig) throw new Error(`unknown event slug in config: ${eventSlug}`);
    const event = await fetchGammaEvent(eventSlug);
    const leg = parseValuationLegs(event, eventConfig).find((item) => item.marketSlug === marketSlug);
    if (!leg?.yesTokenId) throw new Error(`could not find YES token for market slug ${marketSlug}`);
    selectedToken = leg.yesTokenId;
  }
  const quote = await fetchBookQuote(selectedToken);
  const result = await postedProbe(
    selectedToken,
    quote,
    marketSlug,
    loaded.config,
    Number(args.get("price") ?? 0.001),
    Number(args.get("amount-usd") ?? 1),
  );
  return {
    ok: true,
    marketSlug,
    tokenId: selectedToken,
    probePath: probePath(loaded.config, marketSlug),
    result,
  };
}

function applyCaps(
  config: StrategyConfig,
  candidates: ValuationCandidate[],
  locks: Array<{ eventSlug: string; orderUsd: number }>,
): ValuationCandidate[] {
  let globalSpent = locks.reduce((sum, lock) => sum + lock.orderUsd, 0);
  const eventSpent = new Map<string, number>();
  for (const lock of locks) eventSpent.set(lock.eventSlug, (eventSpent.get(lock.eventSlug) ?? 0) + lock.orderUsd);
  return candidates.map((candidate) => {
    if (candidate.status !== "candidate" || candidate.orderUsd <= 0) return candidate;
    const spentForEvent = eventSpent.get(candidate.eventSlug) ?? 0;
    if (globalSpent + candidate.orderUsd > config.globalUsdCap) return capBlocked(candidate, "global_notional_cap_exceeded");
    if (spentForEvent + candidate.orderUsd > config.perEventUsdCap) return capBlocked(candidate, "event_notional_cap_exceeded");
    globalSpent += candidate.orderUsd;
    eventSpent.set(candidate.eventSlug, spentForEvent + candidate.orderUsd);
    return candidate;
  });
}

function capBlocked(candidate: ValuationCandidate, reason: string): ValuationCandidate {
  return { ...candidate, status: "skip", liveAllowed: false, orderUsd: 0, reason };
}

function rankCandidates(candidates: ValuationCandidate[]): ValuationCandidate[] {
  return [...candidates].sort((left, right) => {
    const statusScore = scoreStatus(right.status) - scoreStatus(left.status);
    if (statusScore !== 0) return statusScore;
    return right.edge - left.edge;
  });
}

function scoreStatus(status: ValuationCandidate["status"]): number {
  if (status === "candidate") return 3;
  if (status === "alert") return 2;
  if (status === "no_action") return 1;
  return 0;
}

async function safeQuote(tokenId: string): Promise<BookQuote | undefined> {
  try {
    return await fetchBookQuote(tokenId);
  } catch {
    return undefined;
  }
}

function candidateShell(leg: ValuationLeg): ValuationCandidate {
  return {
    signalType: "NO_ACTION",
    status: "skip",
    company: leg.company,
    eventSlug: leg.eventSlug,
    marketSlug: leg.marketSlug,
    deadline: leg.deadlineIso,
    threshold: leg.threshold,
    yesTokenId: leg.yesTokenId,
    yesAsk: null,
    bestBid: null,
    spread: null,
    liquidity: 0,
    fairPrice: 0,
    edge: 0,
    confidence: 0,
    confidenceScore: 0,
    edgeScore: 0,
    maxPrice: 0,
    orderUsd: 0,
    liveAllowed: false,
    reason: "lock_probe",
    ruleHash: leg.ruleHash,
  };
}

function parseCli(argv: string[]): { command: Command; args: Map<string, string> } {
  const first = argv[0];
  const command = first && !first.startsWith("--") ? parseCommand(first) : "scan";
  const rest = first && !first.startsWith("--") ? argv.slice(1) : argv;
  const args = new Map<string, string>();
  for (let i = 0; i < rest.length; i += 1) {
    const item = rest[i];
    if (!item?.startsWith("--")) throw new Error(`unexpected argument: ${item}`);
    const key = item.slice(2);
    const value = rest[i + 1];
    if (!value || value.startsWith("--")) throw new Error(`missing value for --${key}`);
    args.set(key, value);
    i += 1;
  }
  return { command, args };
}

function parseCommand(value: string): Command {
  if (value === "scan" || value === "run" || value === "preflight" || value === "probe" || value === "ack") return value;
  throw new Error(`unknown valuationStrategy command: ${value}`);
}

function requiredArg(args: Map<string, string>, name: string): string {
  const value = args.get(name);
  if (!value?.trim()) throw new Error(`--${name} is required`);
  return value.trim();
}

async function filePresent(path: string): Promise<boolean> {
  return (await import("node:fs/promises")).access(path).then(() => true, () => false);
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function print(value: unknown): void {
  console.log(JSON.stringify(value, null, 2));
}

if (process.argv[1] && fileURLToPath(import.meta.url) === process.argv[1]) {
  main().catch((error: unknown) => {
    console.error(error instanceof Error ? error.stack : error);
    process.exitCode = 1;
  });
}
