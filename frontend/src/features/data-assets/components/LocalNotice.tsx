import { CircleAlert, Info } from "lucide-react";
import type { ReactNode } from "react";

interface LocalNoticeProps {
  children: ReactNode;
  tone?: "error" | "warning" | "info";
}

/** Present an explicit caller-provided message, never infer availability. */
export function LocalNotice({ children, tone = "info" }: LocalNoticeProps) {
  const Icon = tone === "info" ? Info : CircleAlert;
  return <div className={`qf-assets-notice qf-assets-${tone === "info" ? "banner" : tone}`}
    role={tone === "error" ? "alert" : "status"}>
    <Icon size={18} aria-hidden="true" />
    <div>{children}</div>
  </div>;
}
