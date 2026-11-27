import type {
  CameraFrame,
  CapabilityItem,
  DroneState,
  GrounderStatus,
  LiveMap,
  MapIndex,
  MapTileData,
  McpConfigsResponse,
  PhoneStatus,
  PreflightItem,
  RunLogEntry,
  SerialPortInfo,
  StudioSettingsView,
  StudioStatus,
  TimelineEvent,
} from "./types";

const TOKEN_STORAGE_KEY = "horus.control.token";

export function initToken(): string {
  const hash = window.location.hash || "";
  if (hash.startsWith("#token=")) {
    const raw = decodeURIComponent(hash.slice(7));
    if (raw) {
      try {
        sessionStorage.setItem(TOKEN_STORAGE_KEY, raw);
        localStorage.setItem(TOKEN_STORAGE_KEY, raw);
      } catch {
        // ignore storage errors
      }
      history.replaceState(null, "", window.location.pathname + window.location.search);
      return raw;
    }
  }
  try {
    return sessionStorage.getItem(TOKEN_STORAGE_KEY) || localStorage.getItem(TOKEN_STORAGE_KEY) || "";
  } catch {
    return "";
  }
}

export function saveToken(token: string): void {
  try {
    sessionStorage.setItem(TOKEN_STORAGE_KEY, token);
    localStorage.setItem(TOKEN_STORAGE_KEY, token);
  } catch {
    // ignore
  }
}

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

async function apiFetch(path: string, token: string, init: RequestInit = {}): Promise<Response> {
  const headers = new Headers(init.headers);
  if (token) {
    headers.set("Authorization", `Bearer ${token}`);
  }
  if (init.body && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  const resp = await fetch(path, { ...init, headers, cache: "no-store" });
  if (!resp.ok) {
    let msg = `HTTP ${resp.status}`;
    try {
      const data = await resp.json();
      if (data && typeof data.error === "string") msg = data.error;
    } catch {
      // keep status text
    }
    throw new ApiError(resp.status, msg);
  }
  return resp;
}

export async function apiJson<T>(path: string, token: string, init: RequestInit = {}): Promise<T> {
  const resp = await apiFetch(path, token, init);
  return (await resp.json()) as T;
}

export async function apiBlobUrl(path: string, token: string): Promise<string> {
  const resp = await apiFetch(path, token);
  const blob = await resp.blob();
  return URL.createObjectURL(blob);
}

export const horusApi = {
  getState: (token: string) => apiJson<DroneState>("/api/state", token),
  getFrame: (token: string) => apiJson<CameraFrame>("/api/frame", token),
  getLiveMap: (token: string) => apiJson<LiveMap>("/api/map/live", token),
  getMapIndex: (token: string) => apiJson<MapIndex>("/api/map/index", token),
  getMapTile: (token: string, level: number, x: number, y: number) =>
    apiJson<MapTileData>(`/api/map/tile?level=${level}&x=${x}&y=${y}`, token),
  sendTask: (token: string, text: string) =>
    apiJson<{ status: string; task_id: string }>("/task", token, {
      method: "POST",
      body: JSON.stringify({ text }),
    }),
  sendEmergency: (token: string, action: "hold" | "land" | "rth") =>
    apiJson<{ status: string; action: string }>("/emergency", token, {
      method: "POST",
      body: JSON.stringify({ action }),
    }),
  resetMission: (token: string) =>
    apiJson<{ status: string }>("/reset", token, { method: "POST" }),
  getStudioStatus: (token: string) => apiJson<StudioStatus>("/api/studio/status", token),
  bootstrapAssets: (token: string) =>
    apiJson<{ ready: boolean }>("/api/studio/bootstrap", token, { method: "POST", body: "{}" }),
  startSession: (token: string, payload: Record<string, unknown>) =>
    apiJson<StudioStatus>("/api/studio/session/start", token, {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  stopSession: (token: string) =>
    apiJson<StudioStatus>("/api/studio/session/stop", token, { method: "POST" }),
  restartSession: (token: string) =>
    apiJson<StudioStatus>("/api/studio/session/restart", token, { method: "POST" }),
  getStudioSettings: (token: string) => apiJson<StudioSettingsView>("/api/studio/settings", token),
  putStudioSettings: (token: string, payload: Record<string, unknown>) =>
    apiJson<StudioSettingsView>("/api/studio/settings", token, {
      method: "PUT",
      body: JSON.stringify(payload),
    }),
  testAi: (token: string) =>
    apiJson<{ ok: boolean; provider: string; detail: string }>("/api/studio/ai/test", token, {
      method: "POST",
    }),
  setLinkShaper: (token: string, payload: { delay_ms?: number; jitter_ms?: number; link_cut?: boolean }) =>
    apiJson<{ delay_ms: number; jitter_ms: number; link_cut: boolean }>("/api/studio/link-shaper", token, {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  manualMove: (token: string, action: string, params: Record<string, unknown> = {}) =>
    apiJson<{ action: string; result: unknown }>("/api/studio/manual-move", token, {
      method: "POST",
      body: JSON.stringify({ action, params }),
    }),
  getPreflight: (token: string) => apiJson<{ items: PreflightItem[] }>("/api/studio/preflight", token),
  getSerialPorts: (token: string) => apiJson<{ ports: SerialPortInfo[] }>("/api/studio/hardware/ports", token),
  getRuns: (token: string) => apiJson<{ runs: RunLogEntry[] }>("/api/studio/runs", token),
  getCapabilities: (token: string, probe = true) =>
    apiJson<{ capabilities: CapabilityItem[] }>(`/api/studio/capabilities?probe=${probe ? 1 : 0}`, token),
  getMcpConfigs: (token: string, includeToken = false) =>
    apiJson<McpConfigsResponse>(`/api/studio/mcp-configs?include_token=${includeToken ? 1 : 0}`, token),
  getGrounder: (token: string) => apiJson<GrounderStatus>("/api/studio/grounder", token),
  startGrounder: (token: string, backend = "auto", port = 8100) =>
    apiJson<GrounderStatus>("/api/studio/grounder/start", token, {
      method: "POST",
      body: JSON.stringify({ backend, port }),
    }),
  stopGrounder: (token: string) =>
    apiJson<GrounderStatus>("/api/studio/grounder/stop", token, { method: "POST" }),
  getPhoneStatus: (token: string) => apiJson<PhoneStatus>("/api/studio/phone", token),
  phoneServe: (token: string, action: "start" | "stop" | "check") =>
    apiJson<PhoneStatus>("/api/studio/phone/serve", token, {
      method: "POST",
      body: JSON.stringify({ action }),
    }),
  getSpectatorBlobUrl: (token: string, preset: string, showTof: boolean) =>
    apiBlobUrl(`/api/studio/spectator-frame?preset=${encodeURIComponent(preset)}&tof=${showTof ? 1 : 0}`, token),
  getAiViewBlobUrl: (token: string, id: string) => apiBlobUrl(`/api/ai-view/${encodeURIComponent(id)}`, token),
};

export function subscribeEvents(
  token: string,
  onEvent: (event: TimelineEvent) => void,
  onStatus: (state: "live" | "reconnecting" | "unauthorized") => void,
): () => void {
  let stopped = false;
  let lastId = 0;
  let runId = "";
  let controller: AbortController | null = null;

  const connect = async () => {
    while (!stopped) {
      controller = new AbortController();
      try {
        const resp = await fetch(`/events?after=${lastId}`, {
          headers: { Authorization: `Bearer ${token}` },
          signal: controller.signal,
          cache: "no-store",
        });
        if (resp.status === 401) {
          onStatus("unauthorized");
          return;
        }
        if (!resp.ok || !resp.body) {
          onStatus("reconnecting");
          await new Promise((r) => setTimeout(r, 1500));
          continue;
        }
        const serverRun = resp.headers.get("x-horus-run") || "";
        if (runId && serverRun && serverRun !== runId) {
          lastId = 0;
        }
        runId = serverRun;
        onStatus("live");

        const reader = resp.body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";
        while (!stopped) {
          const { value, done } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });
          const frames = buffer.split("\n\n");
          buffer = frames.pop() || "";
          for (const frame of frames) {
            let dataLine = "";
            for (const line of frame.split("\n")) {
              if (line.startsWith("id:")) {
                const parsed = Number.parseInt(line.slice(3).trim(), 10);
                if (!Number.isNaN(parsed)) lastId = Math.max(lastId, parsed);
              } else if (line.startsWith("data:")) {
                dataLine += line.slice(5).trim();
              }
            }
            if (dataLine) {
              try {
                const ev = JSON.parse(dataLine) as TimelineEvent;
                if (typeof ev.id === "number") lastId = Math.max(lastId, ev.id);
                onEvent(ev);
              } catch {
                // ignore malformed frame
              }
            }
          }
        }
      } catch {
        if (stopped) return;
        onStatus("reconnecting");
      }
      if (!stopped) {
        await new Promise((r) => setTimeout(r, 1200));
      }
    }
  };

  void connect();
  return () => {
    stopped = true;
    controller?.abort();
  };
}

// Lightweight IndexedDB offline map tile cache
const DB_NAME = "horus-offline-tiles";
const STORE_NAME = "tiles";

function openTileDb(): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open(DB_NAME, 1);
    req.onupgradeneeded = () => {
      if (!req.result.objectStoreNames.contains(STORE_NAME)) {
        req.result.createObjectStore(STORE_NAME);
      }
    };
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}

export async function getCachedTile(key: string): Promise<MapTileData | null> {
  try {
    const db = await openTileDb();
    return await new Promise((resolve) => {
      const tx = db.transaction(STORE_NAME, "readonly");
      const req = tx.objectStore(STORE_NAME).get(key);
      req.onsuccess = () => resolve((req.result as MapTileData) || null);
      req.onerror = () => resolve(null);
    });
  } catch {
    return null;
  }
}

export async function putCachedTile(key: string, tile: MapTileData): Promise<void> {
  try {
    const db = await openTileDb();
    await new Promise<void>((resolve) => {
      const tx = db.transaction(STORE_NAME, "readwrite");
      tx.objectStore(STORE_NAME).put(tile, key);
      tx.oncomplete = () => resolve();
      tx.onerror = () => resolve();
    });
  } catch {
    // ignore quota/private mode errors
  }
}

export async function clearTileCache(): Promise<void> {
  try {
    const db = await openTileDb();
    await new Promise<void>((resolve) => {
      const tx = db.transaction(STORE_NAME, "readwrite");
      tx.objectStore(STORE_NAME).clear();
      tx.oncomplete = () => resolve();
      tx.onerror = () => resolve();
    });
  } catch {
    // ignore
  }
}
