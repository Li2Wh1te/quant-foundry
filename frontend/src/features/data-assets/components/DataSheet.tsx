import type { ReactNode } from "react";

interface DataSheetProps {
  title?: string;
  children: ReactNode;
  className?: string;
  labelledBy?: string;
}

/** Continuous local surface, without its own vertical scrolling container. */
export function DataSheet({ title, children, className = "", labelledBy }: DataSheetProps) {
  return <section className={`qf-assets-sheet ${className}`.trim()} aria-labelledby={labelledBy}>
    {title && <h2>{title}</h2>}
    {children}
  </section>;
}
