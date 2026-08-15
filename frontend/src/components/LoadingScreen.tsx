import { LoaderCircle } from "lucide-react";

export function LoadingScreen() {
  return (
    <main className="loading-screen" aria-label="正在验证登录状态">
      <LoaderCircle className="spin" aria-hidden="true" />
    </main>
  );
}
