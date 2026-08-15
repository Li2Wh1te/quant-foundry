const API_TOKEN_STORAGE_KEY = "quant-foundry.api-token";

export function readApiToken(): string | null {
  try {
    return window.sessionStorage.getItem(API_TOKEN_STORAGE_KEY);
  } catch {
    return null;
  }
}

export function writeApiToken(token: string): void {
  try {
    window.sessionStorage.setItem(API_TOKEN_STORAGE_KEY, token);
  } catch {
    throw new Error("浏览器无法保存登录状态，请检查隐私设置。");
  }
}

export function removeApiToken(): void {
  try {
    window.sessionStorage.removeItem(API_TOKEN_STORAGE_KEY);
  } catch {
    // An unavailable storage backend already behaves like an empty session.
  }
}
