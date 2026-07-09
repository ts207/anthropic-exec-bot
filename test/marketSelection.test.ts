import assert from "node:assert/strict";
import test from "node:test";
import { selectThresholdMarket, type GammaEvent } from "../src/valuation/legacy/polymarket.ts";

test("selects the unique 1.1T market and extracts CLOB fields", () => {
  const event: GammaEvent = {
    slug: "will-anthropics-valuation-hit-by-june-30",
    markets: [
      {
        question: "Will Anthropic's valuation reach $1.0T by June 30?",
        conditionId: "0xaaa",
        clobTokenIds: JSON.stringify(["yes-1t", "no-1t"]),
        orderPriceMinTickSize: 0.01,
        negRisk: false,
      },
      {
        question: "Will Anthropic's valuation reach $1.1T by June 30?",
        conditionId: "0xbbb",
        clobTokenIds: JSON.stringify(["yes-11t", "no-11t"]),
        orderPriceMinTickSize: 0.01,
        negRisk: false,
      },
    ],
  };

  assert.deepEqual(selectThresholdMarket(event, "1.1"), {
    eventSlug: "will-anthropics-valuation-hit-by-june-30",
    selectedMarket: "Will Anthropic's valuation reach $1.1T by June 30?",
    question: "Will Anthropic's valuation reach $1.1T by June 30?",
    conditionId: "0xbbb",
    yesTokenId: "yes-11t",
    noTokenId: "no-11t",
    tickSize: "0.01",
    negRisk: false,
  });
});

test("rejects ambiguous threshold markets", () => {
  const event: GammaEvent = {
    slug: "will-anthropics-valuation-hit-by-june-30",
    markets: [
      {
        question: "Will Anthropic hit $1.1T?",
        conditionId: "0x1",
        clobTokenIds: ["yes1", "no1"],
        orderPriceMinTickSize: "0.01",
      },
      {
        question: "Anthropic valuation at least $1.1T?",
        conditionId: "0x2",
        clobTokenIds: ["yes2", "no2"],
        orderPriceMinTickSize: "0.01",
      },
    ],
  };

  assert.throws(() => selectThresholdMarket(event, "1.1"), /ambiguous market/);
});

test("does not match a larger threshold that only shares the prefix", () => {
  const event: GammaEvent = {
    slug: "will-anthropics-valuation-hit-by-june-30",
    markets: [
      {
        question: "Will Anthropic's valuation reach $1.15T by June 30?",
        conditionId: "0xaaa",
        clobTokenIds: ["yes-115t", "no-115t"],
        orderPriceMinTickSize: "0.01",
      },
    ],
  };

  assert.throws(() => selectThresholdMarket(event, "1.1"), /no market matched/);
});
