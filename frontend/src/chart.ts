import type { Point } from "./types";
export function chartPoints(
  points: Point[],
  step: number,
  mode: "rpm" | "share",
) {
  const codes = [...new Set(points.flatMap((p) => Object.keys(p.codes)))];
  return points.map((p) => ({
    time: p.time,
    total: p.total,
    ...Object.fromEntries(
      codes.map((code) => [
        code,
        mode === "rpm"
          ? ((p.codes[code] || 0) * 60) / step
          : p.total
            ? ((p.codes[code] || 0) / p.total) * 100
            : 0,
      ]),
    ),
  }));
}
export function codeColor(code: string) {
  const palettes: Record<string, string[]> = {
    "1": ["#b9a6e8", "#9486b6"],
    "2": ["#5dd6a0", "#8ce8bd", "#38af84", "#b1f2d1"],
    "3": ["#79aaff", "#a3c5ff", "#5385d5", "#bfdaff"],
    "4": ["#f3bd62", "#ffe1a0", "#d89537", "#f4d18c"],
    "5": ["#fa8585", "#ffb0a3", "#dd596d", "#f8a6c6"],
  };
  const p = palettes[code[0]] || palettes["1"];
  return p[Number(code.slice(1)) % p.length];
}
