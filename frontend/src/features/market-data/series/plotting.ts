import { exactMarketValue, type BarPage, type MarketBar } from "../data";
import type { Bar } from "../../../pages/etf/etfData";

export class PlotLimitError extends Error {}
/** Exact binary64 rational; conversion is allowed ONLY at this display boundary. */
function ratio(value: number): [bigint, bigint] {
  const bytes = new DataView(new ArrayBuffer(8)); bytes.setFloat64(0, value);
  const bits = bytes.getBigUint64(0), exponent = Number((bits >> 52n) & 2047n);
  const fraction = bits & ((1n << 52n) - 1n);
  const significand = exponent ? (1n << 52n) + fraction : fraction;
  const power = (exponent || 1) - 1023 - 52;
  return power >= 0 ? [significand << BigInt(power), 1n] : [significand, 1n << BigInt(-power)];
}
export function checkedPlotNumber(text: string | null, precision: number): number | undefined {
  if (text === null) return undefined;
  if (!/^(0|[1-9][0-9]*)(\.[0-9]+)?$/.test(text) || text.length > 256) throw new PlotLimitError("数值不符合绘图契约。");
  const number = Number(text);
  if (!Number.isFinite(number)) throw new PlotLimitError("数值超出图表显示范围，精确原值仍可查看。");
  const [whole, fraction = ""] = text.split("."), denominator = 10n ** BigInt(fraction.length);
  const numerator = BigInt(whole + fraction), [binary, divisor] = ratio(number);
  const delta = numerator * divisor - binary * denominator;
  // Refuse error >= half a displayed step; exact tooltips never use this number.
  if ((delta < 0 ? -delta : delta) * 2n * 10n ** BigInt(precision) >= denominator * divisor)
    throw new PlotLimitError("绘图误差达到显示最小步长，请通过精确值表查看。");
  if (numerator > 0n && number === 0) throw new PlotLimitError("数值过小，图表无法显示。");
  return number;
}
export function plotBars(page: BarPage): { bars: Bar[]; error: string; volumeMissing: boolean; gaps: boolean } {
  const bars: Bar[] = [], gaps = Boolean(page.meta.coverage.missing_dates?.length || page.meta.limitations.some(item => item.code === "MISSING_DATES"));
  let volumeMissing = false;
  try {
    for (const exact of page.items) {
      const prices = [exact.open, exact.high, exact.low, exact.close].map(value => checkedPlotNumber(value, 3));
      if (prices.some(value => value === undefined || value <= 0)) throw new PlotLimitError("价格缺失或无效，已停止绘图，未删除缺失日期。");
      const [open, high, low, close] = prices as number[];
      if (high < Math.max(open, close, low) || low > Math.min(open, close)) throw new PlotLimitError("价格范围无效，已停止绘图。");
      let volume: number | undefined, turnover: number | undefined;
      // Quantity approximation failure does not close independently correct OHLC.
      try { volume = checkedPlotNumber(exact.volume, 2); } catch { volumeMissing = true; }
      try { turnover = checkedPlotNumber(exact.amount, 2); } catch { /* exact table retains it */ }
      volumeMissing ||= volume === undefined;
      bars.push({ timestamp: Date.parse(`${exact.last_trade_date}T00:00:00Z`), date: exact.last_trade_date,
        open, high, low, close, volume, turnover, exact });
    }
    return { bars, error: "", volumeMissing, gaps };
  } catch (problem) {
    return { bars: [], error: problem instanceof Error ? problem.message : "图表显示受限。", volumeMissing, gaps };
  }
}
export function exactBarValue(bar: Bar | undefined, field: keyof Pick<MarketBar, "open" | "high" | "low" | "close" | "volume" | "amount">): string {
  return exactMarketValue(bar?.exact?.[field] ?? null);
}
