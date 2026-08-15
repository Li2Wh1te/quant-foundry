export class InvalidApiTokenError extends Error {
  constructor() {
    super("API Token 无效，请检查后重试。");
    this.name = "InvalidApiTokenError";
  }
}

export interface SystemVersion {
  version: string;
}

export async function verifyApiToken(
  token: string,
  signal?: AbortSignal
): Promise<void> {
  let response: Response;
  try {
    response = await fetch("/api/auth/verify", {
      method: "GET",
      headers: { Authorization: `Bearer ${token}` },
      signal
    });
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") {
      throw error;
    }
    throw new Error("无法连接到服务，请检查网络或稍后重试。");
  }

  if (response.status === 401) {
    throw new InvalidApiTokenError();
  }
  if (!response.ok) {
    throw new Error(`服务暂时不可用（HTTP ${response.status}）。`);
  }
}

export async function fetchSystemVersion(
  token: string,
  signal?: AbortSignal
): Promise<SystemVersion> {
  let response: Response;
  try {
    response = await fetch("/api/system/version", {
      method: "GET",
      headers: { Authorization: `Bearer ${token}` },
      signal
    });
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") {
      throw error;
    }
    throw new Error("无法读取服务版本，请检查网络或稍后重试。");
  }

  if (response.status === 401) {
    throw new InvalidApiTokenError();
  }
  if (!response.ok) {
    throw new Error(`无法读取服务版本（HTTP ${response.status}）。`);
  }

  const body = await response.json() as Partial<SystemVersion>;
  if (typeof body.version !== "string") {
    throw new Error("服务返回了无效的版本信息。");
  }
  return { version: body.version };
}
