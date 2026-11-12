import { useCallback, useEffect, useRef, useState } from "react";

export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(`/api${path}`, {
    ...init,
    headers: {
      ...(init.body && !(init.body instanceof FormData)
        ? { "Content-Type": "application/json" }
        : {}),
      ...init.headers,
    },
  });
  if (!response.ok) {
    let message = `请求失败 (${response.status})`;
    try {
      const result = await response.json();
      message =
        typeof result.detail === "string"
          ? result.detail
          : JSON.stringify(result.detail);
    } catch {
      /* Retain status for non-JSON errors. */
    }
    throw new Error(message);
  }
  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}
export function useData<T>(path: string, interval = 0) {
  const [data, setData] = useState<T | null>(null),
    [error, setError] = useState(""),
    [loading, setLoading] = useState(true),
    [revision, setRevision] = useState(0);
  const reload = useCallback(() => setRevision((value) => value + 1), []);
  const previousPath = useRef(path);
  useEffect(() => {
    let alive = true,
      busy = false;
    if (previousPath.current !== path) {
      previousPath.current = path;
      setLoading(true);
      setData(null);
    }
    const load = async () => {
      if (busy) return;
      busy = true;
      try {
        const value = await api<T>(path);
        if (alive) {
          setData(value);
          setError("");
        }
      } catch (cause) {
        if (alive)
          setError(cause instanceof Error ? cause.message : "无法读取数据");
      } finally {
        busy = false;
        if (alive) setLoading(false);
      }
    };
    void load();
    const timer = interval
      ? window.setInterval(() => void load(), interval)
      : undefined;
    return () => {
      alive = false;
      if (timer) window.clearInterval(timer);
    };
  }, [path, interval, revision]);
  return { data, error, loading, reload };
}
