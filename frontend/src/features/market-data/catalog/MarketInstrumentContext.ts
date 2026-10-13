import { createContext, useContext } from "react";
import type { InstrumentDetail, InstrumentResolution } from "../data";

/** D10 consumes this stable key. A source-directory key never authorizes bars. */
export interface MarketInstrumentEntry {
  instrumentKey: string;
  symbol: string;
  profile: InstrumentDetail;
  resolution: InstrumentResolution | null;
}
export const MarketInstrumentContext = createContext<MarketInstrumentEntry | null>(null);
export const useMarketInstrument = () => useContext(MarketInstrumentContext);
