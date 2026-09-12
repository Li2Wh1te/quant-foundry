import { useLayoutEffect, useRef, type ReactNode } from "react";

export interface PopoverAnchor {
  element: HTMLElement;
  key: string;
}
export function EtfPopover({
  anchor,
  title,
  close,
  children,
  boundary,
}: {
  anchor: PopoverAnchor;
  title: string;
  close: () => void;
  children: ReactNode;
  boundary?: HTMLElement | null;
}) {
  const ref = useRef<HTMLElement>(null);
  const closeRef = useRef(close);
  closeRef.current = close;
  useLayoutEffect(() => {
    const panel = ref.current!;
    const position = () => {
      const rect = anchor.element.getBoundingClientRect();
      // Sidebar menus must fit their own surface, not just the viewport.
      const bounds = boundary?.getBoundingClientRect();
      const left = Math.max(8, bounds ? bounds.left + 8 : 8);
      const right = Math.min(
        innerWidth - 8,
        bounds ? bounds.left + boundary!.clientWidth - 8 : innerWidth - 8,
      );
      panel.style.width = `${Math.max(0, Math.min(320, right - left))}px`;
      panel.style.left = `${Math.max(left, Math.min(rect.left, right - panel.offsetWidth))}px`;
      panel.style.top = `${Math.max(8, Math.min(rect.bottom + 8, innerHeight - panel.offsetHeight - 8))}px`;
    };
    position();
    const observer = new ResizeObserver(position);
    observer.observe(panel);
    if (boundary) observer.observe(boundary);
    const outside = (e: PointerEvent) => {
      if (
        !panel.contains(e.target as Node) &&
        !anchor.element.contains(e.target as Node)
      )
        closeRef.current();
    };
    const key = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.stopPropagation();
        closeRef.current();
      }
    };
    document.addEventListener("pointerdown", outside);
    document.addEventListener("keydown", key, true);
    window.addEventListener("resize", position);
    panel
      .querySelector<HTMLElement>('input,button,select,[tabindex="0"]')
      ?.focus();
    return () => {
      observer.disconnect();
      document.removeEventListener("pointerdown", outside);
      document.removeEventListener("keydown", key, true);
      window.removeEventListener("resize", position);
      if (anchor.element.isConnected) anchor.element.focus();
    };
  }, [anchor.element, anchor.key, boundary]);
  return (
    <section ref={ref} className="qfe-popover" role="dialog" aria-label={title}>
      <h2>{title}</h2>
      {children}
    </section>
  );
}
