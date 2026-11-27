import { AlertCircle, AlertTriangle, Check, Info, X } from "lucide-react";
import { createContext, type ReactNode, useCallback, useContext, useEffect, useMemo, useRef, useState } from "react";

export type ToastTone = "success" | "info" | "warning" | "error";

interface ToastItem {
  id: number;
  tone: ToastTone;
  title: string;
  detail?: string;
  exiting: boolean;
}

interface TimerMeta {
  remaining: number;
  startedAt: number;
  timer?: number;
  removeTimer?: number;
}

interface ToastApi {
  show: (tone: ToastTone, title: string, detail?: string) => void;
  success: (title: string, detail?: string) => void;
  info: (title: string, detail?: string) => void;
  warning: (title: string, detail?: string) => void;
  error: (title: string, detail?: string) => void;
}

const ToastContext = createContext<ToastApi | null>(null);

function ToastGlyph({ tone }: { tone: ToastTone }) {
  if (tone === "success") {
    return (
      <span
        aria-hidden="true"
        className="mt-0.5 inline-flex h-5 w-5 shrink-0 items-center justify-center rounded-full bg-success/15 text-success"
      >
        <Check size={12} strokeWidth={2.5} />
      </span>
    );
  }
  if (tone === "warning") {
    return (
      <span
        aria-hidden="true"
        className="mt-0.5 inline-flex h-5 w-5 shrink-0 items-center justify-center rounded-full bg-warning/15 text-warning"
      >
        <AlertTriangle size={12} strokeWidth={2.2} />
      </span>
    );
  }
  if (tone === "error") {
    return (
      <span
        aria-hidden="true"
        className="mt-0.5 inline-flex h-5 w-5 shrink-0 items-center justify-center rounded-full bg-danger/15 text-danger"
      >
        <AlertCircle size={12} strokeWidth={2.2} />
      </span>
    );
  }
  return (
    <span
      aria-hidden="true"
      className="mt-0.5 inline-flex h-5 w-5 shrink-0 items-center justify-center rounded-full bg-hover text-secondary"
    >
      <Info size={12} strokeWidth={2.2} />
    </span>
  );
}

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<ToastItem[]>([]);
  const nextIdRef = useRef(1);
  const metaRef = useRef<Map<number, TimerMeta>>(new Map());

  useEffect(() => {
    const meta = metaRef.current;
    return () => {
      for (const entry of meta.values()) {
        if (entry.timer !== undefined) window.clearTimeout(entry.timer);
        if (entry.removeTimer !== undefined) window.clearTimeout(entry.removeTimer);
      }
      meta.clear();
    };
  }, []);

  const dismiss = useCallback((id: number) => {
    const entry = metaRef.current.get(id);
    if (entry?.timer !== undefined) {
      window.clearTimeout(entry.timer);
      entry.timer = undefined;
    }
    if (entry?.removeTimer !== undefined) return;

    setToasts((prev) => prev.map((t) => (t.id === id ? { ...t, exiting: true } : t)));
    const removeTimer = window.setTimeout(() => {
      metaRef.current.delete(id);
      setToasts((prev) => prev.filter((t) => t.id !== id));
    }, 190);

    if (entry) {
      entry.removeTimer = removeTimer;
    } else {
      metaRef.current.set(id, { remaining: 0, startedAt: Date.now(), removeTimer });
    }
  }, []);

  const scheduleTimer = useCallback(
    (id: number, ms: number) => {
      const existing = metaRef.current.get(id);
      if (existing?.timer !== undefined) window.clearTimeout(existing.timer);
      if (existing?.removeTimer !== undefined) return;
      const timer = window.setTimeout(() => dismiss(id), ms);
      metaRef.current.set(id, { remaining: ms, startedAt: Date.now(), timer });
    },
    [dismiss],
  );

  const pauseTimer = useCallback((id: number) => {
    const entry = metaRef.current.get(id);
    if (!entry || entry.timer === undefined) return;
    window.clearTimeout(entry.timer);
    entry.timer = undefined;
    const elapsed = Date.now() - entry.startedAt;
    entry.remaining = Math.max(500, entry.remaining - elapsed);
  }, []);

  const resumeTimer = useCallback(
    (id: number) => {
      const entry = metaRef.current.get(id);
      if (!entry || entry.removeTimer !== undefined || entry.timer !== undefined) return;
      scheduleTimer(id, entry.remaining);
    },
    [scheduleTimer],
  );

  const show = useCallback(
    (tone: ToastTone, title: string, detail?: string) => {
      const id = nextIdRef.current++;
      const durationMs = tone === "error" || tone === "warning" ? 5500 : 3200;
      setToasts((prev) => [...prev.slice(-3), { id, tone, title, detail, exiting: false }]);
      scheduleTimer(id, durationMs);
    },
    [scheduleTimer],
  );

  const api = useMemo<ToastApi>(
    () => ({
      show,
      success: (title, detail) => show("success", title, detail),
      info: (title, detail) => show("info", title, detail),
      warning: (title, detail) => show("warning", title, detail),
      error: (title, detail) => show("error", title, detail),
    }),
    [show],
  );

  return (
    <ToastContext.Provider value={api}>
      {children}
      {toasts.length > 0 && (
        <div
          aria-live="polite"
          aria-label="Notifications"
          className="pointer-events-none fixed right-4 bottom-20 z-50 flex w-[calc(100%-2rem)] max-w-[360px] flex-col gap-2 md:right-5 md:bottom-5"
        >
          {toasts.map((t) => (
            <div
              key={t.id}
              role={t.tone === "error" || t.tone === "warning" ? "alert" : "status"}
              onMouseEnter={() => pauseTimer(t.id)}
              onMouseLeave={() => resumeTimer(t.id)}
              className={`pointer-events-auto flex items-start gap-2.5 rounded-[12px] border border-border/90 bg-surface/95 px-3.5 py-2.5 shadow-[0_10px_28px_rgb(0_0_0/0.16)] backdrop-blur-md ${
                t.exiting ? "animate-toast-out pointer-events-none" : "animate-toast-in"
              }`}
            >
              <ToastGlyph tone={t.tone} />
              <div className="min-w-0 flex-1">
                <div className="text-[13px] font-medium leading-snug text-primary">{t.title}</div>
                {t.detail && (
                  <div className="mt-0.5 break-words text-[12px] leading-snug text-secondary">{t.detail}</div>
                )}
              </div>
              <button
                type="button"
                aria-label="Dismiss notification"
                onClick={() => dismiss(t.id)}
                className="inline-flex h-5 w-5 shrink-0 items-center justify-center rounded-[6px] text-muted opacity-60 transition-opacity hover:bg-hover hover:text-primary hover:opacity-100 cursor-pointer"
              >
                <X size={13} />
              </button>
            </div>
          ))}
        </div>
      )}
    </ToastContext.Provider>
  );
}

const FALLBACK_TOAST: ToastApi = {
  show: () => {},
  success: () => {},
  info: () => {},
  warning: () => {},
  error: () => {},
};

export function useToast(): ToastApi {
  return useContext(ToastContext) ?? FALLBACK_TOAST;
}
