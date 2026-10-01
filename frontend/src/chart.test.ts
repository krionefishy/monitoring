import { expect, it } from "vitest";
import { chartPoints } from "./chart";
it("shares retain the denominator of all responses", () => {
  expect(
    chartPoints(
      [{ time: 0, total: 100, codes: { 200: 80, 404: 15, 409: 5 } }],
      30,
      "share",
    )[0],
  ).toEqual({ time: 0, total: 100, 200: 80, 404: 15, 409: 5 });
});
it("normalizes RPM and fills missing codes with zero", () => {
  expect(
    chartPoints(
      [
        { time: 0, total: 10, codes: { 200: 10 } },
        { time: 30, total: 0, codes: {} },
      ],
      30,
      "rpm",
    ),
  ).toEqual([
    { time: 0, total: 10, 200: 20 },
    { time: 30, total: 0, 200: 0 },
  ]);
});
