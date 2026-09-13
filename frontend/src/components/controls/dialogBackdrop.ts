import type { MouseEvent } from "react";

/** Native dialog backdrop clicks target the dialog itself. Bounds distinguish
 * the backdrop from empty space inside the form and from child popovers. */
export function isDialogBackdropClick(event: MouseEvent<HTMLDialogElement>): boolean {
  if (event.target !== event.currentTarget) return false;
  const rect = event.currentTarget.getBoundingClientRect();
  return event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom;
}
