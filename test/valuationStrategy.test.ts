import assert from "node:assert/strict";
import { mkdtemp } from "node:fs/promises";
import { join } from "node:path";
import { tmpdir } from "node:os";
import test from "node:test";
import { normalizeConfig } from "../src/strategy/valuationConfig.ts";
import { parseNpmEvidence, withEligibleMax } from "../src/strategy/npmValuationSource.ts";
import { parseGammaEvent, parseThreshold, parseValuationLegs } from "../src/strategy/marketParser.ts";
import { decideThresholdLeg } from "../src/strategy/valuationDecision.ts";
import { curveMonotonicityCandidates } from "../src/strategy/curveArbitrage.ts";
import { calendarDominanceCandidates } from "../src/strategy/calendarArbitrage.ts";
import { rankingAlertCandidates } from "../src/strategy/rankingSimulator.ts";
import type { BookQuote, CurvePoint, EventConfig, StrategyConfig, ValuationLeg } from "../src/strategy/signalTypes.ts";
import { liveBlockers } from "../src/valuationStrategy.ts";
import { betaProbeMetadata, validatePostedProbeForCandidate } from "../src/strategy/probeValidation.ts";
import { probePath, writeJson } from "../src/strategy/stateStore.ts";

test("config loader applies safe low-risk defaults", () => {
  const config = testConfig();
  assert.equal(config.mode, "alert_only");
  assert.equal(config.pollMs, 30_000);
  assert.equal(config.signalMultipliers.SOURCE_CONFIRMED_YES, 1);
  assert.equal(config.signalMultipliers.RANKING_INCONSISTENCY_ALERT, 0);
});

test("threshold parser accepts B/T/M suffixes and rejects malformed dollar amount", () => {
  assert.equal(parseThreshold("Will OpenAI hit $900B by July 31?")?.value, 900_000_000_000);
  assert.equal(parseThreshold("Will Anthropic hit HIGH $1.15T by July 31?")?.value, 1_150_000_000_000);
  assert.equal(parseThreshold("Will Perplexity hit $20B by July 31?")?.value, 20_000_000_000);
  assert.equal(parseThreshold("Will Anthropic hit LOW $1.0T by July 31?")?.value, 1_000_000_000_000);
  assert.equal(parseThreshold("Will Anthropic hit LOW $800 by July 31?"), null);
});

test("Gamma fixture parser keeps LOW label but relies on rule threshold language", () => {
  const eventConfig = thresholdEvent("Anthropic");
  const event = parseGammaEvent({
    slug: "anthropic-event",
    title: "Will Anthropic's valuation hit by July 31?",
    description: "This market resolves Yes if NPM Price reports Anthropic reaches or exceeds the listed amount.",
    markets: [
      marketFixture("Will Anthropic's valuation hit LOW $900B by July 31?", "low-900b"),
      marketFixture("Will Anthropic's valuation hit LOW $800 by July 31?", "low-800"),
    ],
  });
  const legs = parseValuationLegs(event, eventConfig);
  assert.equal(legs[0]?.label, "LOW");
  assert.equal(legs[0]?.parseStatus, "ok");
  assert.equal(legs[0]?.threshold, 900_000_000_000);
  assert.equal(legs[1]?.parseStatus, "malformed_threshold");
});

test("NPM parser stores latest tape and eligible-window max valuation", () => {
  const evidence = parseNpmEvidence({
    company: { name: "Anthropic" },
    latest_tape_d: { date: "2026-07-03", implied_valuation: 1_080_000_000_000, price: 42 },
    tape_d_prices: [
      { date: "2026-06-28", implied_valuation: 1_200_000_000_000 },
      { date: "2026-07-01", implied_valuation: 1_105_000_000_000 },
      { date: "2026-07-03", implied_valuation: 1_080_000_000_000 },
    ],
  }, { name: "Anthropic", npmCompanyId: "company-a" });
  const windowed = withEligibleMax(evidence, "2026-06-29T00:00:00Z", "2026-08-01T03:59:59Z");
  assert.equal(windowed.latestValuation, 1_080_000_000_000);
  assert.equal(windowed.maxEligibleValuation, 1_105_000_000_000);
  assert.equal(windowed.maxEligibleDate, "2026-07-01");
});

test("source-confirmed crossed threshold creates BUY YES candidate", () => {
  const config = normalizeConfig({
    mode: "live",
    events: [thresholdEvent("Anthropic")],
    companies: [{ name: "Anthropic", npmCompanyId: "company-a" }],
  });
  const leg = legFixture({ threshold: 1_100_000_000_000 });
  const evidence = parseNpmEvidence({
    latest_tape_d: { date: "2026-07-01", implied_valuation: 1_101_000_000_000 },
  }, { name: "Anthropic", npmCompanyId: "company-a" });
  const candidate = decideThresholdLeg(leg, evidence, quoteFixture(0.81), config);
  assert.equal(candidate.signalType, "SOURCE_CONFIRMED_YES");
  assert.equal(candidate.status, "candidate");
  assert.equal(candidate.liveAllowed, true);
  assert.equal(candidate.orderUsd > 0, true);
  assert.equal(candidate.distancePct !== undefined && candidate.distancePct < 0.001, true);
  assert.equal(candidate.confidenceScore, 10);
  assert.equal(candidate.edgeScore > 0, true);
  assert.deepEqual(candidate.orderTemplate, {
    tokenId: "yes-token",
    side: "BUY",
    outcome: "YES",
    orderType: "FAK",
    amountUsd: candidate.orderUsd,
    maxPrice: 0.95,
    posted: false,
  });
});

test("non-crossed threshold no-actions unless drift edge exists", () => {
  const config = testConfig();
  const leg = legFixture({ threshold: 1_100_000_000_000 });
  const evidence = parseNpmEvidence({
    latest_tape_d: { date: "2026-07-01", implied_valuation: 1_050_000_000_000 },
  }, { name: "Anthropic", npmCompanyId: "company-a" });
  const candidate = decideThresholdLeg(leg, evidence, quoteFixture(0.5), config);
  assert.equal(candidate.signalType, "NO_ACTION");
  assert.equal(candidate.liveAllowed, false);
});

test("closed and malformed legs do not trade", () => {
  const config = testConfig();
  const closed = decideThresholdLeg(
    { ...legFixture({ threshold: 1_100_000_000_000 }), closed: true },
    undefined,
    quoteFixture(0.5),
    config,
  );
  const malformed = decideThresholdLeg(
    { ...legFixture({ threshold: undefined }), parseStatus: "malformed_threshold" },
    undefined,
    quoteFixture(0.5),
    config,
  );
  assert.equal(closed.status, "skip");
  assert.equal(malformed.signalType, "STALE_SOURCE_ALERT");
  assert.equal(malformed.liveAllowed, false);
});

test("hard monotonicity violation buys underpriced lower threshold YES", () => {
  const config = testConfig();
  const lower = legFixture({ threshold: 900_000_000_000, marketSlug: "lower" });
  const higher = legFixture({ threshold: 950_000_000_000, marketSlug: "higher" });
  const quotes = new Map([
    ["lower", quoteFixture(0.52)],
    ["higher", quoteFixture(0.64)],
  ]);
  const candidates = curveMonotonicityCandidates([
    { leg: lower, yesAsk: 0.52 },
    { leg: higher, yesAsk: 0.64 },
  ], quotes, config);
  assert.equal(candidates.length, 1);
  assert.equal(candidates[0]?.signalType, "CURVE_MONOTONICITY_YES");
  assert.equal(candidates[0]?.marketSlug, "lower");
});

test("calendar dominance violation buys later deadline YES", () => {
  const config = testConfig();
  const july = legFixture({ threshold: 900_000_000_000, marketSlug: "july", deadlineIso: "2026-08-01T03:59:59Z" });
  const dec = legFixture({ threshold: 900_000_000_000, marketSlug: "dec", deadlineIso: "2027-01-01T04:59:59Z" });
  const quotes = new Map([
    ["july", quoteFixture(0.7)],
    ["dec", quoteFixture(0.58)],
  ]);
  const candidates = calendarDominanceCandidates([
    { leg: july, yesAsk: 0.7 },
    { leg: dec, yesAsk: 0.58 },
  ], quotes, config);
  assert.equal(candidates.length, 1);
  assert.equal(candidates[0]?.signalType, "CALENDAR_DOMINANCE_YES");
  assert.equal(candidates[0]?.marketSlug, "dec");
});

test("ranking market inconsistency is alert-only", () => {
  const config = testConfig();
  const leg = rankingLegFixture("Anthropic", 1);
  const evidence = parseNpmEvidence({
    latest_tape_d: { date: "2026-07-01", implied_valuation: 1_200_000_000_000 },
  }, { name: "Anthropic", npmCompanyId: "company-a" });
  const candidates = rankingAlertCandidates([leg], new Map([["Anthropic", evidence]]), new Map([[leg.marketSlug, quoteFixture(0.4)]]), config);
  assert.equal(candidates.length, 1);
  assert.equal(candidates[0]?.signalType, "RANKING_INCONSISTENCY_ALERT");
  assert.equal(candidates[0]?.liveAllowed, false);
});

test("live blocker audit explains why candidate is not live-eligible", async () => {
  const config = testConfig();
  const leg = legFixture({ threshold: 1_100_000_000_000 });
  const evidence = parseNpmEvidence({
    latest_tape_d: { date: "2026-07-01", implied_valuation: 1_101_000_000_000 },
  }, { name: "Anthropic", npmCompanyId: "company-a" });
  const candidate = decideThresholdLeg(leg, evidence, quoteFixture(0.81), config);
  const blockers = await liveBlockers(candidate, config, "test-config-hash");
  assert.equal(blockers.includes("operator_mode_alert_only"), true);
  assert.equal(blockers.includes("missing_live_config_ack"), true);
  assert.equal(blockers.includes("missing_posted_probe_success"), true);
  assert.equal(blockers.includes("posting_env_not_armed"), true);
});

test("posted probe validation rejects malformed probe files", async () => {
  const stateDir = await mkdtemp(join(tmpdir(), "valuation-probe-test-"));
  const config = normalizeConfig({
    mode: "live",
    stateDir,
    events: [thresholdEvent("Anthropic")],
    companies: [{ name: "Anthropic", npmCompanyId: "company-a" }],
  });
  const candidate = decideThresholdLeg(
    legFixture({ threshold: 1_100_000_000_000 }),
    parseNpmEvidence({ latest_tape_d: { date: "2026-07-01", implied_valuation: 1_101_000_000_000 } }, { name: "Anthropic", npmCompanyId: "company-a" }),
    quoteFixture(0.81),
    config,
  );
  await writeJson(probePath(config, candidate.marketSlug), {
    ok: true,
    marketSlug: candidate.marketSlug,
    tokenId: "wrong-token",
    timestamp: new Date().toISOString(),
  });
  const validation = await validatePostedProbeForCandidate(config, candidate);
  assert.equal(validation.ok, false);
  assert.equal(validation.blockers.includes("probe_token_mismatch"), true);
  assert.equal(validation.blockers.includes("probe_side_mismatch"), true);
  assert.equal(validation.blockers.includes("probe_order_type_mismatch"), true);
  assert.equal(validation.blockers.includes("probe_sdk_mismatch"), true);
});

test("posted probe validation accepts current beta BUY FAK probe metadata", async () => {
  const stateDir = await mkdtemp(join(tmpdir(), "valuation-probe-test-"));
  const config = normalizeConfig({
    mode: "live",
    stateDir,
    events: [thresholdEvent("Anthropic")],
    companies: [{ name: "Anthropic", npmCompanyId: "company-a" }],
  });
  const candidate = decideThresholdLeg(
    legFixture({ threshold: 1_100_000_000_000 }),
    parseNpmEvidence({ latest_tape_d: { date: "2026-07-01", implied_valuation: 1_101_000_000_000 } }, { name: "Anthropic", npmCompanyId: "company-a" }),
    quoteFixture(0.81),
    config,
  );
  await writeJson(probePath(config, candidate.marketSlug), {
    ok: true,
    marketSlug: candidate.marketSlug,
    tokenId: candidate.yesTokenId,
    timestamp: new Date().toISOString(),
    ...betaProbeMetadata(),
  });
  const validation = await validatePostedProbeForCandidate(config, candidate);
  assert.equal(validation.ok, true);
  assert.deepEqual(validation.blockers, []);
});

function testConfig(): StrategyConfig {
  return normalizeConfig({
    events: [thresholdEvent("Anthropic")],
    companies: [{ name: "Anthropic", npmCompanyId: "company-a" }],
  });
}

function thresholdEvent(companyName: string): EventConfig {
  return {
    slug: "test-event",
    kind: "threshold",
    companyName,
    deadlineIso: "2026-08-01T03:59:59Z",
    marketWindowStartIso: "2026-06-29T00:00:00Z",
  };
}

function marketFixture(question: string, slug: string): Record<string, unknown> {
  return {
    question,
    slug,
    outcomes: "[\"Yes\",\"No\"]",
    clobTokenIds: "[\"yes-token\",\"no-token\"]",
    active: true,
    closed: false,
    acceptingOrders: true,
    liquidityNum: 500,
  };
}

function legFixture(overrides: Partial<ValuationLeg>): ValuationLeg {
  return {
    eventSlug: "event",
    marketSlug: overrides.marketSlug ?? "market",
    question: "Will Anthropic's valuation hit $1.1T by July 31?",
    eventKind: "threshold",
    company: "Anthropic",
    deadlineIso: overrides.deadlineIso ?? "2026-08-01T03:59:59Z",
    marketWindowStartIso: "2026-06-29T00:00:00Z",
    threshold: overrides.threshold,
    thresholdText: "$1.1T",
    yesTokenId: "yes-token",
    noTokenId: "no-token",
    active: true,
    closed: false,
    acceptingOrders: true,
    liquidity: 500,
    ruleText: "reaches or exceeds the listed amount",
    ruleHash: "rule-hash",
    parseStatus: "ok",
    ...overrides,
  };
}

function rankingLegFixture(company: string, ranking: 1 | 2 | 3): ValuationLeg {
  return {
    ...legFixture({ threshold: undefined, marketSlug: `${company}-ranking` }),
    eventKind: "ranking",
    company,
    label: "RANKING",
    ranking,
    parseStatus: "ok",
  };
}

function quoteFixture(bestAsk: number): BookQuote {
  return {
    tokenId: "yes-token",
    bestBid: Math.max(0, bestAsk - 0.03),
    bestAsk,
    spread: 0.03,
    liquidity: 500,
    fetchedAt: "2026-07-05T00:00:00Z",
  };
}
