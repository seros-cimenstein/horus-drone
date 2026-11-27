import {
  ArrowRight,
  Bot,
  Box,
  Compass,
  FlaskConical,
  Loader2,
  Monitor,
  Moon,
  PanelLeft,
  RotateCcw,
  Send,
  Settings,
  Sliders,
  Smartphone,
  Square,
  Sun,
} from "lucide-react";
import { type ReactNode, useEffect, useRef, useState } from "react";
import { horusApi, initToken, saveToken, subscribeEvents } from "./api";
import { AiMcpView } from "./components/AiMcpView";
import { FaultLabView } from "./components/FaultLabView";
import { PhoneView } from "./components/PhoneView";
import { SystemPhoneView } from "./components/SystemPhoneView";
import { TacticalMap } from "./components/TacticalMap";
import { VisualStage } from "./components/VisualStage";
import { WorldsView } from "./components/WorldsView";
import { Button, StopSessionButton } from "./components/common/Button";
import { StatusBanner } from "./components/common/StatusBanner";
import { StatusDot } from "./components/common/StatusDot";
import { Input, Textarea } from "./components/common/Surface";
import { useToast } from "./components/common/Toast";
import { BottomTabBar } from "./components/layout/BottomTabBar";
import type {
  CameraFrame,
  CapabilityItem,
  DroneState,
  LiveMap,
  MapSelection,
  StudioStatus,
  TimelineEvent,
} from "./types";

type NavId = "flight" | "lab" | "worlds" | "ai" | "phone" | "tuning" | "system";
type StageTab = "camera" | "3d" | "map" | "aiviews";
type ThemeMode = "system" | "dark" | "light";

const NAV_GROUPS: Array<{
  heading: string;
  items: Array<{ id: NavId; label: string; icon: ReactNode }>;
}> = [
  {
    heading: "Flight",
    items: [
      { id: "flight", label: "Flight Deck", icon: <Compass size={16} strokeWidth={1.7} /> },
      { id: "lab", label: "Manual & Safety", icon: <FlaskConical size={16} strokeWidth={1.7} /> },
    ],
  },
  {
    heading: "Connect",
    items: [
      { id: "worlds", label: "Simulators & Drones", icon: <Box size={16} strokeWidth={1.7} /> },
      { id: "ai", label: "AI & MCP", icon: <Bot size={16} strokeWidth={1.7} /> },
      { id: "phone", label: "Phone App", icon: <Smartphone size={16} strokeWidth={1.7} /> },
    ],
  },
  {
    heading: "System",
    items: [
      { id: "tuning", label: "Flight Settings", icon: <Sliders size={16} strokeWidth={1.7} /> },
      { id: "system", label: "System & Logs", icon: <Settings size={16} strokeWidth={1.7} /> },
    ],
  },
];

export function App() {
  const [token, setToken] = useState<string>(() => initToken());
  const [tokenDraft, setTokenDraft] = useState("");
  const [sseState, setSseState] = useState<"live" | "reconnecting" | "unauthorized">("reconnecting");
  const [studioStatus, setStudioStatus] = useState<StudioStatus | null>(null);
  const [caps, setCaps] = useState<CapabilityItem[]>([]);
  const [droneState, setDroneState] = useState<DroneState | null>(null);
  const [frame, setFrame] = useState<CameraFrame | null>(null);
  const [liveMap, setLiveMap] = useState<LiveMap | null>(null);
  const [events, setEvents] = useState<TimelineEvent[]>([]);
  const [missionText, setMissionText] = useState("");
  const [nav, setNav] = useState<NavId>("flight");
  const [stageTab, setStageTab] = useState<StageTab>("camera");
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
  const [theme, setTheme] = useState<ThemeMode>(() => {
    try {
      const saved = localStorage.getItem("horus:theme");
      if (saved === "light" || saved === "dark" || saved === "system") return saved;
    } catch {
      // ignore storage failure
    }
    return "dark";
  });
  const [bootstrapping, setBootstrapping] = useState(false);
  const [pendingEmergency, setPendingEmergency] = useState<"hold" | "land" | "rth" | null>(null);
  const [stoppingSession, setStoppingSession] = useState(false);
  const [launchingScene, setLaunchingScene] = useState<string | null>(null);
  const [sendingMission, setSendingMission] = useState(false);
  const [resettingMission, setResettingMission] = useState(false);
  const [releasingLock, setReleasingLock] = useState(false);
  const [notice, setNotice] = useState("");
  const [dismissedError, setDismissedError] = useState(false);
  const frameBackoffUntilRef = useRef(0);
  const toast = useToast();

  const isPhone = new URLSearchParams(window.location.search).get("view") === "phone";

  useEffect(() => {
    try {
      localStorage.setItem("horus:theme", theme);
    } catch {
      // ignore
    }
    const prefersDark = window.matchMedia?.("(prefers-color-scheme: dark)").matches;
    const isDark = theme === "dark" || (theme === "system" && prefersDark);
    document.documentElement.classList.toggle("dark", isDark);
  }, [theme]);

  const cycleTheme = () => {
    setTheme((prev) => (prev === "dark" ? "light" : prev === "light" ? "system" : "dark"));
  };

  const refreshStudio = () => {
    if (!token) return;
    horusApi.getStudioStatus(token).then(setStudioStatus).catch(() => {});
    horusApi.getCapabilities(token, false).then((r) => setCaps(r.capabilities)).catch(() => {});
  };

  const refreshCaps = async (force: boolean): Promise<CapabilityItem[]> => {
    if (!token) return [];
    const r = await horusApi.getCapabilities(token, force);
    setCaps(r.capabilities);
    return r.capabilities;
  };

  useEffect(() => {
    if (!token) return;
    refreshStudio();
    const unsub = subscribeEvents(
      token,
      (ev) => {
        setEvents((prev) => [...prev.slice(-240), ev]);
        if (ev.kind === "studio_status") refreshStudio();
        if (ev.kind === "bootstrap_progress") {
          setNotice(`Downloading ${String(ev.asset)}: ${String(ev.state)} (${String(ev.bytes_done || 0)} B)`);
        }
      },
      setSseState,
    );
    return unsub;
  }, [token]);

  useEffect(() => {
    setDismissedError(false);
  }, [studioStatus?.error]);

  useEffect(() => {
    frameBackoffUntilRef.current = 0;
    if (!token || studioStatus?.lifecycle !== "running") {
      setDroneState(null);
      setLiveMap(null);
      setFrame(null);
      return;
    }
    const pollFast = async () => {
      try {
        const [st, lm] = await Promise.all([horusApi.getState(token), horusApi.getLiveMap(token)]);
        setDroneState(st);
        setLiveMap(lm);
      } catch {
        // transient disconnect while running
      }
      if ((stageTab === "camera" || isPhone) && Date.now() >= frameBackoffUntilRef.current) {
        try {
          setFrame(await horusApi.getFrame(token));
        } catch {
          // Back off briefly if perception camera is still warming up or disabled.
          frameBackoffUntilRef.current = Date.now() + 4000;
        }
      }
    };
    void pollFast();
    const timer = window.setInterval(() => void pollFast(), 450);
    return () => window.clearInterval(timer);
  }, [token, studioStatus?.lifecycle, stageTab, isPhone]);

  if (!token || sseState === "unauthorized") {
    return (
      <div className="flex min-h-screen items-center justify-center bg-canvas p-6 text-primary">
        <div className="w-full max-w-md rounded-[10px] border border-border bg-surface p-7">
          <h1 className="text-[20px] font-semibold tracking-tight text-primary">HORUS Studio</h1>
          <p className="mt-1.5 text-[14px] text-secondary">Enter the bearer token printed by `horus` in your terminal.</p>
          <div className="mt-5 flex gap-2.5">
            <Input
              type="password"
              value={tokenDraft}
              onChange={(e) => setTokenDraft(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && tokenDraft.trim()) {
                  saveToken(tokenDraft.trim());
                  setToken(tokenDraft.trim());
                  setSseState("reconnecting");
                }
              }}
              placeholder="Paste bearer token"
            />
            <Button
              variant="primary"
              onClick={() => {
                saveToken(tokenDraft.trim());
                setToken(tokenDraft.trim());
                setSseState("reconnecting");
              }}
            >
              Connect
            </Button>
          </div>
        </div>
      </div>
    );
  }

  const sendEmergency = async (action: "hold" | "land" | "rth") => {
    const label = action === "rth" ? "HOME" : action.toUpperCase();
    setPendingEmergency(action);
    try {
      await horusApi.sendEmergency(token, action);
      setNotice(`Emergency ${label} sent`);
      toast.warning(`Emergency ${label} sent`, "AI flight tools are locked until your next mission or manual release.");
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : String(e);
      setNotice(`Emergency failed: ${msg}`);
      toast.error(`Emergency ${label} failed`, msg);
    } finally {
      setPendingEmergency(null);
    }
  };

  const sendMission = async (text: string) => {
    if (!studioStatus?.ai_status.configured) {
      setNav("ai");
      setNotice("Choose an AI provider first, or fly manually under Manual & Safety.");
      toast.warning("AI provider not configured", "Choose an AI provider or fly manually under Manual & Safety.");
      return;
    }
    setSendingMission(true);
    try {
      await horusApi.sendTask(token, text);
      setMissionText("");
      toast.success("Mission sent", text.length > 72 ? `${text.slice(0, 72)}…` : text);
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : String(e);
      setNotice(`Mission error: ${msg}`);
      toast.error("Mission failed", msg);
    } finally {
      setSendingMission(false);
    }
  };

  const handleResetMission = async () => {
    setResettingMission(true);
    try {
      await horusApi.resetMission(token);
      toast.info("Mission reset", "Started a fresh mission thread.");
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : String(e);
      setNotice(`Reset failed: ${msg}`);
      toast.error("Mission reset failed", msg);
    } finally {
      setResettingMission(false);
    }
  };

  const manualMove = async (action: string, params: Record<string, unknown>): Promise<boolean> => {
    if (studioStatus?.lifecycle !== "running") {
      setNotice("Start a session before sending flight or gimbal commands.");
      toast.warning("No active session", "Start a session before sending flight or gimbal commands.");
      return false;
    }
    try {
      await horusApi.manualMove(token, action, params);
      toast.info(`Command sent: ${action}`);
      return true;
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : String(e);
      setNotice(`Manual command failed: ${msg}`);
      toast.error(`Command "${action}" failed`, msg);
      return false;
    }
  };

  const handleBootstrap = async () => {
    setBootstrapping(true);
    setNotice("Downloading SITL binary & ONNX models…");
    toast.info("Downloading assets", "Fetching ArduCopter SITL and perception models…");
    try {
      const res = await horusApi.bootstrapAssets(token);
      refreshStudio();
      setNotice("");
      if (res.ready) {
        toast.success("SITL & models ready", "Required standalone assets are verified.");
      } else {
        toast.warning("Download finished with warnings", "Check System & Logs for missing assets.");
      }
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : String(e);
      setNotice(`Asset download failed: ${msg}`);
      toast.error("Asset download failed", msg);
    } finally {
      setBootstrapping(false);
    }
  };

  const handleStartSession = async (payload: Record<string, unknown>) => {
    const label = String(payload.scene || payload.mode || "session");
    setDismissedError(false);
    setLaunchingScene(label);
    setNotice(`Starting ${label}…`);
    try {
      const next = await horusApi.startSession(token, payload);
      setStudioStatus(next);
      setNotice("");
      setNav("flight");
      toast.success("Session started", next.target_label || label);
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : String(e);
      setNotice(msg);
      toast.error(`Failed to start ${label}`, msg);
      refreshStudio();
    } finally {
      setLaunchingScene(null);
    }
  };

  const handleStopSession = async () => {
    setStoppingSession(true);
    try {
      setStudioStatus(await horusApi.stopSession(token));
      toast.info("Session stopped");
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : String(e);
      setNotice(`Failed to stop session: ${msg}`);
      toast.error("Failed to stop session", msg);
    } finally {
      setStoppingSession(false);
    }
  };

  if (isPhone) {
    return (
      <PhoneView
        token={token}
        droneState={droneState}
        liveMap={liveMap}
        frame={frame}
        events={events}
        onEmergency={sendEmergency}
        onSendMission={sendMission}
        onFlyTo={(n, e) => void manualMove("goto", { pose: { north_m: n, east_m: e, up_m: 2.0 } })}
      />
    );
  }

  const veh = droneState?.vehicle;
  const activeScene = studioStatus?.scenes.find((s) => s.name === studioStatus?.scene);
  const running = studioStatus?.lifecycle === "running";
  const isIdle = studioStatus !== null && studioStatus.lifecycle === "idle";
  const lockDesktopViewport = nav === "flight" && !isIdle;
  const aiViewsCount = events.filter((e) => e.kind === "ai_view" && typeof e.view_id === "string").length;
  const coreCaps = caps.filter((c) => c.tier !== "optional");
  const readyCoreCapsCount = coreCaps.filter((c) => c.ready).length;
  const ThemeIcon = theme === "light" ? Sun : theme === "dark" ? Moon : Monitor;

  const rawNotice = notice || (!dismissedError ? studioStatus?.error || "" : "");
  const isWarningNotice =
    Boolean(!notice && studioStatus?.error && !dismissedError) ||
    /error|fail|requires|cannot|unable/i.test(rawNotice);
  const newlineIdx = rawNotice.indexOf("\n");
  const noticeTitle = newlineIdx === -1 ? rawNotice : rawNotice.slice(0, newlineIdx);
  const noticeDetail = newlineIdx === -1 ? undefined : rawNotice.slice(newlineIdx + 1).trim();

  const statusDotKind =
    studioStatus?.lifecycle === "running"
      ? "ok"
      : studioStatus?.lifecycle === "error"
        ? "danger"
        : studioStatus?.lifecycle === "starting" || studioStatus?.lifecycle === "bootstrapping"
          ? "warn"
          : "idle";

  return (
    <div
      className={
        lockDesktopViewport
          ? "flex min-h-screen bg-canvas text-primary lg:h-screen lg:overflow-hidden"
          : "flex min-h-screen bg-canvas text-primary"
      }
    >
      {/* Grouped Left Sidebar (all 7 sections directly visible, zero nested tabs) */}
      <aside
        aria-label="HORUS Studio navigation"
        className={`sticky top-0 hidden h-screen shrink-0 flex-col border-r border-border bg-surface transition-[width] duration-150 md:flex ${
          sidebarCollapsed ? "w-[64px]" : "w-[248px]"
        }`}
      >
        <div
          className={`flex h-14 shrink-0 items-center border-b border-border ${
            sidebarCollapsed ? "justify-center px-2" : "justify-between px-4"
          }`}
        >
          {!sidebarCollapsed && (
            <button
              type="button"
              onClick={() => setNav("flight")}
              className="flex items-center gap-2.5 text-left cursor-pointer"
            >
              <img
                src="/icon.svg"
                alt=""
                className="h-7 w-7 shrink-0 rounded-[7px]"
              />
              <span className="flex items-baseline gap-1.5">
                <span className="text-[15.5px] font-semibold tracking-tight text-primary">HORUS</span>
                <span className="text-[12.5px] text-muted">Studio</span>
              </span>
            </button>
          )}
          <button
            type="button"
            onClick={() => setSidebarCollapsed((c) => !c)}
            aria-label={sidebarCollapsed ? "Expand sidebar" : "Collapse sidebar"}
            className="inline-flex h-8 w-8 items-center justify-center rounded-[7px] text-secondary hover:bg-hover hover:text-primary cursor-pointer"
          >
            <PanelLeft size={16} strokeWidth={1.7} />
          </button>
        </div>

        <nav className="flex flex-1 flex-col gap-5 overflow-y-auto p-3">
          {NAV_GROUPS.map((group) => (
            <div key={group.heading} className="flex flex-col gap-1">
              {!sidebarCollapsed && (
                <div className="px-3 pb-1 text-[11.5px] font-medium uppercase tracking-wider text-muted">
                  {group.heading}
                </div>
              )}
              {group.items.map((item) => {
                const active = nav === item.id;
                const badgeNode =
                  !sidebarCollapsed && item.id === "worlds" && running ? (
                    <StatusDot status="ok" />
                  ) : !sidebarCollapsed && item.id === "ai" ? (
                    <StatusDot status={studioStatus?.ai_status.configured ? "ok" : "warn"} />
                  ) : !sidebarCollapsed && item.id === "system" && coreCaps.length > 0 ? (
                    <span className="ml-auto font-mono text-[11.5px] text-muted tabular-nums">
                      {readyCoreCapsCount}/{coreCaps.length}
                    </span>
                  ) : null;
                return (
                  <button
                    key={item.id}
                    type="button"
                    onClick={() => setNav(item.id)}
                    title={sidebarCollapsed ? item.label : undefined}
                    className={`flex items-center justify-between gap-2.5 rounded-[8px] px-3 py-2 text-[14px] font-medium transition-colors cursor-pointer ${
                      sidebarCollapsed ? "justify-center px-0" : ""
                    } ${active ? "bg-raised text-primary" : "text-secondary hover:bg-hover hover:text-primary"}`}
                  >
                    <span className="flex min-w-0 items-center gap-3">
                      {item.icon}
                      {!sidebarCollapsed && <span className="truncate">{item.label}</span>}
                    </span>
                    {badgeNode}
                  </button>
                );
              })}
            </div>
          ))}
        </nav>

        <div className="flex flex-col gap-3 border-t border-border p-3.5">
          {!sidebarCollapsed && (
            <div className="flex items-center justify-between gap-2">
              <button
                type="button"
                onClick={() => setNav("worlds")}
                className="min-w-0 text-left hover:underline cursor-pointer"
              >
                <StatusDot
                  status={statusDotKind}
                  label={studioStatus?.target_label || (sseState === "live" ? "Connected" : sseState)}
                />
              </button>
              {running && (
                <button
                  type="button"
                  title="Stop active session"
                  aria-label="Stop active session"
                  onClick={() => void handleStopSession()}
                  className="inline-flex h-7 w-7 shrink-0 items-center justify-center rounded-[6px] text-secondary hover:bg-hover hover:text-danger cursor-pointer"
                >
                  <Square size={12} />
                </button>
              )}
            </div>
          )}

          <div className={`flex items-center ${sidebarCollapsed ? "justify-center" : "justify-between"}`}>
            {!sidebarCollapsed && (
              <button type="button" onClick={() => setNav("ai")} className="hover:underline cursor-pointer">
                <StatusDot
                  status={studioStatus?.ai_status.configured ? "ok" : "warn"}
                  label={studioStatus?.ai_status.configured ? `AI: ${studioStatus.ai_status.provider}` : "AI not configured"}
                />
              </button>
            )}
            <button
              type="button"
              onClick={cycleTheme}
              title={`Theme: ${theme}`}
              aria-label={`Switch theme (current: ${theme})`}
              className="inline-flex h-8 w-8 items-center justify-center rounded-[7px] text-secondary hover:bg-hover hover:text-primary cursor-pointer"
            >
              <ThemeIcon size={15} />
            </button>
          </div>
        </div>
      </aside>

      {/* Main Column */}
      <div
        className={
          lockDesktopViewport
            ? "flex min-w-0 flex-1 flex-col lg:min-h-0 lg:overflow-hidden"
            : "flex min-w-0 flex-1 flex-col"
        }
      >
        <header className="sticky top-0 z-30 flex h-14 shrink-0 items-center justify-between gap-3 border-b border-border bg-canvas px-3 md:px-5">
          <div className="flex min-w-0 items-center gap-4">
            <span className="text-[15.5px] font-semibold tracking-tight md:hidden">HORUS</span>
            <dl
              aria-label="Live telemetry"
              className="hidden flex-wrap items-center gap-x-5 gap-y-1 font-mono text-[13px] text-secondary tabular-nums md:flex"
            >
              <div className="flex items-center gap-1.5">
                <dt className="text-muted">ALT</dt>
                <dd className="font-semibold text-primary">
                  {typeof veh?.altitude_m === "number" ? `${veh.altitude_m.toFixed(2)}m` : "--"}
                </dd>
              </div>
              <div className="flex items-center gap-1.5">
                <dt className="text-muted">GND</dt>
                <dd className="font-semibold text-primary">
                  {typeof veh?.ground_range_m === "number" ? `${veh.ground_range_m.toFixed(2)}m` : "--"}
                </dd>
              </div>
              <div className="flex items-center gap-1.5">
                <dt className="text-muted">SPD</dt>
                <dd className="font-semibold text-primary">
                  {typeof veh?.speed_m_s === "number" ? `${veh.speed_m_s.toFixed(1)}m/s` : "--"}
                </dd>
              </div>
              <div className="flex items-center gap-1.5">
                <dt className="text-muted">MODE</dt>
                <dd className="font-semibold text-primary">{veh?.mode || "--"}</dd>
              </div>
              <div className="flex items-center gap-1.5">
                <dt className="text-muted">BAT</dt>
                <dd className="font-semibold text-primary">
                  {typeof veh?.battery_pct === "number" ? `${veh.battery_pct.toFixed(0)}%` : "--"}
                </dd>
              </div>
            </dl>
          </div>

          <div className="flex shrink-0 items-center gap-1.5 md:gap-2" role="group" aria-label="Emergency controls">
            <Button
              size="sm"
              variant="danger"
              disabled={!running}
              loading={pendingEmergency === "hold"}
              title={running ? "Immediately hover in place and lock AI flight tools" : "Start a session to enable emergency controls"}
              onClick={() => void sendEmergency("hold")}
            >
              HOLD
            </Button>
            <Button
              size="sm"
              variant="danger"
              disabled={!running}
              loading={pendingEmergency === "land"}
              title={running ? "Immediately descend and land in place" : "Start a session to enable emergency controls"}
              onClick={() => void sendEmergency("land")}
            >
              LAND
            </Button>
            <Button
              size="sm"
              variant="danger"
              disabled={!running}
              loading={pendingEmergency === "rth"}
              title={running ? "Immediately return home and land" : "Start a session to enable emergency controls"}
              onClick={() => void sendEmergency("rth")}
            >
              HOME
            </Button>
          </div>
        </header>

        {/* Mobile live telemetry bar */}
        {running && (
          <dl
            aria-label="Mobile live telemetry"
            className="flex shrink-0 items-center justify-between gap-2 border-b border-border bg-surface px-3 py-1.5 font-mono text-[11.5px] text-secondary tabular-nums md:hidden"
          >
            <span>
              <span className="text-muted">ALT </span>
              <strong className="text-primary">
                {typeof veh?.altitude_m === "number" ? `${veh.altitude_m.toFixed(1)}m` : "--"}
              </strong>
            </span>
            <span>
              <span className="text-muted">GND </span>
              <strong className="text-primary">
                {typeof veh?.ground_range_m === "number" ? `${veh.ground_range_m.toFixed(1)}m` : "--"}
              </strong>
            </span>
            <span>
              <span className="text-muted">SPD </span>
              <strong className="text-primary">
                {typeof veh?.speed_m_s === "number" ? `${veh.speed_m_s.toFixed(1)}` : "--"}
              </strong>
            </span>
            <span>
              <strong className="text-primary">{veh?.mode || "--"}</strong>
            </span>
            <span>
              <strong className="text-primary">
                {typeof veh?.battery_pct === "number" ? `${veh.battery_pct.toFixed(0)}%` : "--"}
              </strong>
            </span>
          </dl>
        )}

        {((nav === "flight" && droneState?.operator_lock) || rawNotice) && (
          <div className="sticky top-14 z-20 shrink-0">
            {nav === "flight" && droneState?.operator_lock && (
              <StatusBanner
                tone="danger"
                title="Operator lock active"
                detail="AI flight tools are paused until the next mission or manual release."
                action={{
                  label: releasingLock ? "Releasing…" : "Release Lock",
                  onClick: () => {
                    setReleasingLock(true);
                    void manualMove("take_off", { altitude_m: 2.0 }).finally(() => setReleasingLock(false));
                  },
                }}
              />
            )}
            {rawNotice && (
              <StatusBanner
                tone={isWarningNotice ? "warning" : "info"}
                title={noticeTitle}
                detail={noticeDetail}
                onDismiss={() => {
                  setNotice("");
                  setDismissedError(true);
                }}
              />
            )}
          </div>
        )}

        <main
          className={
            lockDesktopViewport
              ? "flex min-h-0 flex-1 flex-col pb-[calc(4rem+env(safe-area-inset-bottom,0px))] lg:overflow-hidden lg:pb-0"
              : "flex flex-1 flex-col pb-[calc(4rem+env(safe-area-inset-bottom,0px))] md:pb-0"
          }
        >
          {nav === "flight" &&
            (isIdle ? (
              <div className="mx-auto flex w-full max-w-2xl flex-1 flex-col justify-center gap-7 px-6 py-12">
                <div>
                  <h1 className="text-[24px] font-semibold tracking-tight text-primary">Ready to fly</h1>
                  <p className="mt-1.5 text-[14.5px] text-secondary">
                    Start a built-in 3D environment to open the camera, map, and mission control, or choose any page on the left.
                  </p>
                </div>

                <div className="divide-y divide-border overflow-hidden rounded-[10px] border border-border bg-surface">
                  {(studioStatus?.scenes || []).map((sc) => {
                    const isLaunching = launchingScene === sc.name;
                    return (
                      <button
                        key={sc.name}
                        type="button"
                        disabled={Boolean(launchingScene)}
                        onClick={() => void handleStartSession({ mode: "builtin_sim", scene: sc.name, seed: 42 })}
                        className="flex w-full select-none items-center justify-between gap-4 px-5 py-4 text-left transition-colors duration-75 hover:bg-hover active:bg-border/40 disabled:pointer-events-none disabled:opacity-60 cursor-pointer"
                      >
                        <div className="min-w-0">
                          <div className="text-[15px] font-medium text-primary">{sc.title}</div>
                          <div className="mt-0.5 text-[13.5px] text-secondary">{sc.subtitle}</div>
                        </div>
                        <span className="inline-flex items-center gap-1.5 text-[13.5px] font-medium text-primary">
                          {isLaunching ? (
                            <>
                              <Loader2 size={15} className="animate-spin" /> Starting…
                            </>
                          ) : (
                            <>
                              Launch <ArrowRight size={15} />
                            </>
                          )}
                        </span>
                      </button>
                    );
                  })}
                </div>

                <div className="flex flex-wrap items-center gap-2.5">
                  <Button onClick={() => setNav("worlds")}>Simulators &amp; Drones</Button>
                  <Button variant="ghost" onClick={() => setNav("ai")}>
                    AI &amp; MCP
                  </Button>
                  <Button variant="ghost" onClick={() => setNav("system")}>
                    System &amp; Logs ({readyCoreCapsCount}/{coreCaps.length || 3})
                  </Button>
                </div>
              </div>
            ) : (
              <div className="grid min-h-0 flex-1 lg:grid-cols-[minmax(0,1.55fr)_minmax(360px,1fr)] lg:overflow-hidden">
                {/* Left Unified Stage */}
                <div className="flex min-h-0 min-w-0 flex-col overflow-hidden border-b border-border lg:border-r lg:border-b-0">
                  <div className="flex shrink-0 items-center justify-between gap-3 border-b border-border bg-surface px-5">
                    <div className="flex gap-6" role="tablist" aria-label="Flight stage view">
                      {(
                        [
                          { id: "camera", label: "Camera" },
                          { id: "3d", label: "3D View" },
                          { id: "map", label: "Map" },
                          { id: "aiviews", label: `AI Views (${aiViewsCount})` },
                        ] as const
                      ).map((t) => (
                        <button
                          key={t.id}
                          type="button"
                          role="tab"
                          aria-selected={stageTab === t.id}
                          onClick={() => setStageTab(t.id)}
                          className={`border-b-2 py-3 text-[14px] font-medium transition-colors cursor-pointer ${
                            stageTab === t.id
                              ? "border-primary text-primary"
                              : "border-transparent text-secondary hover:text-primary"
                          }`}
                        >
                          {t.label}
                        </button>
                      ))}
                    </div>
                    {running && (
                      <StopSessionButton
                        className="shrink-0"
                        loading={stoppingSession}
                        onClick={() => void handleStopSession()}
                      />
                    )}
                  </div>

                  {stageTab === "map" ? (
                    <TacticalMap
                      token={token}
                      droneState={droneState}
                      liveMap={liveMap}
                      onFlyTo={(n, e) => void manualMove("goto", { pose: { north_m: n, east_m: e, up_m: 2.0 } })}
                      onUseInMission={(sel: MapSelection) => {
                        const desc =
                          sel.kind === "point"
                            ? `Fly to (${sel.point[0]}m north, ${sel.point[1]}m east) and inspect.`
                            : `Search area ${sel.label} (${sel.points.map(([n, e]) => `[${n},${e}]`).join(", ")}).`;
                        setMissionText(desc);
                      }}
                    />
                  ) : (
                    <VisualStage
                      token={token}
                      mode={stageTab}
                      studioStatus={studioStatus}
                      frame={frame}
                      events={events}
                      controlsDisabled={!running}
                      onManualMove={manualMove}
                    />
                  )}
                </div>

                {/* Right Mission Timeline & Composer */}
                <div className="flex min-h-[440px] flex-col bg-surface lg:min-h-0 lg:overflow-hidden">
                  <div className="flex h-12 shrink-0 items-center justify-between border-b border-border px-4">
                    <span className="text-[14px] font-semibold text-primary">Mission</span>
                    <div className="flex items-center gap-2.5">
                      <span className="font-mono text-[12px] text-muted">{events.length} events</span>
                      <Button
                        size="sm"
                        variant="ghost"
                        loading={resettingMission}
                        onClick={() => void handleResetMission()}
                      >
                        {!resettingMission && <RotateCcw size={13} />} {resettingMission ? "Resetting…" : "Reset"}
                      </Button>
                    </div>
                  </div>

                  <div className="min-h-0 flex-1 divide-y divide-border overflow-y-auto">
                    {events
                      .slice(-60)
                      .reverse()
                      .map((ev) => (
                        <div key={ev.id} className="px-4 py-3 text-[14px]">
                          <div
                            className={`mb-1 font-mono text-[11.5px] uppercase tracking-wide ${
                              ev.kind === "emergency" ? "text-danger" : "text-muted"
                            }`}
                          >
                            #{ev.id} · {ev.kind}
                            {ev.tool ? ` · ${ev.tool}` : ""}
                          </div>
                          {ev.kind === "tool_use" || ev.kind === "tool_result" ? (
                            <details>
                              <summary className="cursor-pointer font-medium text-secondary">
                                {String(ev.summary || ev.tool || ev.kind)}
                              </summary>
                              <pre className="mt-2 overflow-x-auto font-mono text-[12px] text-muted">
                                {JSON.stringify(ev, null, 2)}
                              </pre>
                            </details>
                          ) : (
                            <span className="leading-relaxed text-primary">
                              {String(ev.text || ev.summary || ev.error || ev.action || ev.target_label || "")}
                            </span>
                          )}
                        </div>
                      ))}
                    {events.length === 0 && (
                      <div className="px-5 py-10 text-center text-[14px] text-muted">
                        Send a natural-language mission below or pick a quick instruction.
                      </div>
                    )}
                  </div>

                  <div className="flex shrink-0 flex-col gap-3 border-t border-border bg-canvas p-4">
                    <div className="flex flex-wrap gap-1.5">
                      {(
                        activeScene?.prompts || [
                          "Take off to 2 m, look around, and report what you see.",
                          "Return home and land on the AprilTag pad.",
                        ]
                      ).map((prompt) => (
                        <button
                          key={prompt}
                          type="button"
                          onClick={() => void sendMission(prompt)}
                          className="select-none rounded-[6px] border border-border bg-surface px-2.5 py-1.5 text-left text-[12.5px] text-secondary transition-colors duration-75 hover:border-border-strong hover:text-primary active:bg-hover cursor-pointer"
                        >
                          {prompt}
                        </button>
                      ))}
                    </div>
                    <Textarea
                      rows={2}
                      value={missionText}
                      onChange={(e) => setMissionText(e.target.value)}
                      onKeyDown={(e) => {
                        if ((e.ctrlKey || e.metaKey) && e.key === "Enter" && missionText.trim()) {
                          void sendMission(missionText.trim());
                        }
                      }}
                      placeholder="Give the AI pilot a mission (Ctrl/Cmd + Enter to send)…"
                    />
                    <div className="flex items-center justify-between gap-2">
                      <button
                        type="button"
                        onClick={() => setNav("ai")}
                        className="text-[12.5px] text-secondary hover:text-primary hover:underline cursor-pointer"
                      >
                        {studioStatus?.ai_status.configured
                          ? `${studioStatus.ai_status.provider} (${studioStatus.ai_status.model})`
                          : "AI unconfigured — click to configure →"}
                      </button>
                      <Button
                        variant="primary"
                        disabled={!missionText.trim() || !running}
                        loading={sendingMission}
                        onClick={() => void sendMission(missionText.trim())}
                      >
                        {!sendingMission && <Send size={14} />} {sendingMission ? "Sending…" : "Send Mission"}
                      </Button>
                    </div>
                  </div>
                </div>
              </div>
            ))}

          {nav === "lab" && (
            <FaultLabView
              token={token}
              studioStatus={studioStatus}
              executorState={droneState?.state || "idle"}
              onManualMove={manualMove}
              onRefreshStatus={refreshStudio}
            />
          )}

          {nav === "worlds" && (
            <WorldsView
              token={token}
              studioStatus={studioStatus}
              bootstrapping={bootstrapping}
              stoppingSession={stoppingSession}
              onBootstrap={handleBootstrap}
              onStartSession={handleStartSession}
              onStopSession={handleStopSession}
            />
          )}

          {nav === "ai" && <AiMcpView token={token} studioStatus={studioStatus} onUpdated={refreshStudio} />}

          {(nav === "phone" || nav === "tuning" || nav === "system") && (
            <SystemPhoneView
              section={nav}
              token={token}
              studioStatus={studioStatus}
              caps={caps}
              bootstrapping={bootstrapping}
              onBootstrap={handleBootstrap}
              onRefreshCaps={refreshCaps}
              onSaved={refreshStudio}
            />
          )}
        </main>
      </div>

      <BottomTabBar
        nav={nav}
        onSelectNav={setNav}
        running={running}
        stoppingSession={stoppingSession}
        targetLabel={studioStatus?.target_label || (sseState === "live" ? "Connected" : sseState)}
        statusDotKind={statusDotKind}
        aiConfigured={Boolean(studioStatus?.ai_status.configured)}
        readyCapsCount={readyCoreCapsCount}
        totalCapsCount={coreCaps.length}
        theme={theme}
        onCycleTheme={cycleTheme}
        onStopSession={() => void handleStopSession()}
      />
    </div>
  );
}
