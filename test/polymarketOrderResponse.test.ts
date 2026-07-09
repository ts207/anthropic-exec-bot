import assert from "node:assert/strict";
import test from "node:test";
import { isSuccessfulOrderResponse } from "../src/valuation/legacy/polymarket.ts";

test("rejected CLOB response is not accepted", () => {
  assert.equal(
    isSuccessfulOrderResponse({
      error: "maker address not allowed, please use the deposit wallet flow",
      status: 400,
    }),
    false,
  );
});

test("successful CLOB response is accepted", () => {
  assert.equal(
    isSuccessfulOrderResponse({
      success: true,
      errorMsg: "",
      orderID: "0xorder",
      status: "matched",
      takingAmount: "10",
      makingAmount: "6.4",
    }),
    true,
  );
});

test("status without success is not accepted", () => {
  assert.equal(
    isSuccessfulOrderResponse({
      status: "matched",
      orderID: "0xorder",
    }),
    false,
  );
});
