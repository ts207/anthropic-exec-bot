import assert from "node:assert/strict";
import test from "node:test";
import { decide } from "../src/valuation/legacy/decision.ts";
import {
  parseNpmApiResponse,
  parseNpmValuationText,
} from "../src/valuation/legacy/npmParser.ts";

test("parser below threshold decides NO", () => {
  const parsed = parseNpmValuationText(`
    As of Jun 30, 2026
    Valuation
    $1.097T
  `);

  assert.equal(parsed.ok, true);
  if (!parsed.ok) return;
  assert.equal(parsed.data.valuation, 1_097_000_000_000);
  assert.equal(decide(parsed.data.valuation).side, "NO");
});

test("parser exactly threshold decides YES", () => {
  const parsed = parseNpmValuationText(`
    As of Jun 30, 2026
    Valuation
    $1.100T
  `);

  assert.equal(parsed.ok, true);
  if (!parsed.ok) return;
  assert.deepEqual(parsed.data, {
    asOf: "Jun 30, 2026",
    valuation: 1_100_000_000_000,
    raw: "$1.100T",
  });
  assert.equal(decide(parsed.data.valuation).side, "YES");
});

test("parser above threshold decides YES", () => {
  const parsed = parseNpmValuationText(`
    As of Jun 30, 2026
    Valuation
    $1.101T
  `);

  assert.equal(parsed.ok, true);
  if (!parsed.ok) return;
  assert.equal(parsed.data.valuation, 1_101_000_000_000);
  assert.equal(decide(parsed.data.valuation).side, "YES");
});

test("wrong date does not trade", () => {
  const parsed = parseNpmValuationText(`
    As of Jun 29, 2026
    Valuation
    $1.100T
  `);

  assert.deepEqual(parsed, {
    ok: false,
    reason: "not Jun 30",
  });
});

test("rounded current valuation text is ignored", () => {
  const parsed = parseNpmValuationText(`
    As of Jun 30, 2026
    Current Valuation: $1.1T
  `);

  assert.equal(parsed.ok, false);
});

test("api parser rejects pre-Jun 30 latest_tape_d", () => {
  const parsed = parseNpmApiResponse({
    latest_tape_d: {
      date: "2026-06-29",
      implied_valuation: 1_097_455_908_439.9583,
    },
  });

  assert.deepEqual(parsed, {
    ok: false,
    reason: "not Jun 30",
  });
});

test("api parser uses exact implied valuation at threshold", () => {
  const parsed = parseNpmApiResponse({
    latest_tape_d: {
      date: "2026-06-30",
      implied_valuation: 1_100_000_000_000,
    },
  });

  assert.equal(parsed.ok, true);
  if (!parsed.ok) return;
  assert.deepEqual(parsed.data, {
    asOf: "Jun 30, 2026",
    valuation: 1_100_000_000_000,
    raw: "1100000000000",
  });
  assert.equal(decide(parsed.data.valuation).side, "YES");
});
