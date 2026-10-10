import type { ReactNode } from "react";

interface WorkspaceHeaderProps {
  title: string;
  description?: ReactNode;
  eyebrow?: string;
  back?: ReactNode;
  status?: ReactNode;
  actions?: ReactNode;
}

/** The caller supplies all wording and state; this header has no data policy. */
export function WorkspaceHeader({ title, description, eyebrow, back, status, actions }: WorkspaceHeaderProps) {
  return <header className="qf-assets-heading">
    <div>
      {back}
      {eyebrow && <p className="qf-assets-eyebrow">{eyebrow}</p>}
      <h1>{title}</h1>
      {description && <p>{description}</p>}
      {status && <div className="qf-assets-heading-status">{status}</div>}
    </div>
    {actions && <div className="qf-assets-heading-actions">{actions}</div>}
  </header>;
}
