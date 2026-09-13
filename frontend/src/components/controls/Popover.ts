import { useLayoutEffect, useRef, useState } from "react";
/** Keep overlays inside their owning dialog while positioning against the viewport.
 * Native dialog top-layer/inert behavior is preserved; no portal escapes it.
 */
export function useControlPopover(open: boolean, close: () => void, width?: number) {
  const root = useRef<HTMLSpanElement>(null);
  const [style, setStyle] = useState<{ top: number; left: number; width: number; maxHeight: number }>({ top: 0, left: 0, width: width || 280, maxHeight: 320 });
  useLayoutEffect(() => {
    if (!open) return;
    const place = () => {
      const box = root.current?.getBoundingClientRect(); if (!box) return;
      const wanted = Math.min(width || box.width, window.innerWidth-24);
      const below = window.innerHeight-box.bottom-12, above=box.top-12;
      const up = below < 260 && above > below;
      const height = Math.min(width ? 370 : 320, up ? above-6 : below-6);
      setStyle({ left: Math.max(12,Math.min(box.left,window.innerWidth-wanted-12)), top: up ? box.top-height-6 : box.bottom+6, width:wanted, maxHeight:Math.max(100,height) });
    };
    const outside = (event: PointerEvent) => { if (!root.current?.contains(event.target as Node)) close(); };
    place(); window.addEventListener("resize",place); window.addEventListener("scroll",place,true); document.addEventListener("pointerdown",outside,true);
    return () => { window.removeEventListener("resize",place); window.removeEventListener("scroll",place,true); document.removeEventListener("pointerdown",outside,true); };
  }, [open, width]);
  return { root, style };
}
