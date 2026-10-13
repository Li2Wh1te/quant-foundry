import type { ComponentType } from "react";
import type { Adjustment, BarPage, SeriesOption } from "../data";

/** D11 integration seam: components may read factors independently; no shell rewrite. */
export interface BasisControlsProps {
  instrumentKey: string; series: SeriesOption | null; adjustment: Adjustment;
  asOf: string; disabled: boolean; onChange: (adjustment: Adjustment) => void;
}
export interface FactorTableProps {
  instrumentKey: string; seriesKey: string; start: string | null; end: string; asOf: string;
  adjustment: Adjustment; page: BarPage | null;
  onError: (error: unknown) => void;
}
export interface DetailBasisSlots {
  BasisControls?: ComponentType<BasisControlsProps>;
  FactorTable?: ComponentType<FactorTableProps>;
}
