interface BrandProps {
  compact?: boolean;
}

export function Brand({ compact = false }: BrandProps) {
  return (
    <div className={compact ? "brand brand--compact" : "brand"}>
      <span className="brand__mark" aria-hidden="true">
        QF
      </span>
      <span className="brand__name">Quant Foundry</span>
    </div>
  );
}
