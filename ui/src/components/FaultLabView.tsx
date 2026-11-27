import { ArrowDown, ArrowLeft, ArrowRight, ArrowUp, RefreshCw, RotateCcw, RotateCw, Unplug } from "lucide-react";
import { useEffect, useState } from "react";
import { horusApi } from "../api";
import type { PreflightItem, StudioStatus } from "../types";
import { Button } from "./common/Button";
import { Input, PageHeader, Section, StatusBadge } from "./common/Surface";
import { useToast } from "./common/Toast";

interface Props {
  token: string;
  studioStatus: StudioStatus | null;
  executorState: string;
  onManualMove: (action: string, params: Record<string, unknown>) => Promise<boolean> | void;
  onRefreshStatus: () => void;
}

export function FaultLabView({ token, studioStatus, executorState, onManualMove, onRefreshStatus }: Props) {
  const [delayMs, setDelayMs] = useState(studioStatus?.link_shaper.delay_ms ?? 0);
  const [linkCut, setLinkCut] = useState(studioStatus?.link_shaper.link_cut ?? false);
  const [vocab, setVocab] = useState("red backpack, injured person");
  const [preflight, setPreflight] = useState<PreflightItem[]>([]);
  const [statusMsg, setStatusMsg] = useState("");
  const [pendingMove, setPendingMove] = useState<string | null>(null);
  const [applyingShaper, setApplyingShaper] = useState<string | null>(null);
  const [loadingPreflight, setLoadingPreflight] = useState(false);
  const toast = useToast();

  const loadPreflight = async (notify = false) => {
    setLoadingPreflight(true);
    try {
      const r = await horusApi.getPreflight(token);
      setPreflight(r.items);
      if (notify) toast.success("Pre-flight checks refreshed");
    } catch (e: unknown) {
      if (notify) toast.error("Pre-flight check failed", e instanceof Error ? e.message : String(e));
    } finally {
      setLoadingPreflight(false);
    }
  };

  useEffect(() => {
    void loadPreflight(false);
  }, [token]);

  const runMove = async (key: string, action: string, params: Record<string, unknown>) => {
    setPendingMove(key);
    try {
      await onManualMove(action, params);
    } finally {
      setPendingMove(null);
    }
  };

  const applyShaper = async (key: string, nextDelay: number, nextCut: boolean) => {
    setApplyingShaper(key);
    setDelayMs(nextDelay);
    setLinkCut(nextCut);
    try {
      await horusApi.setLinkShaper(token, { delay_ms: nextDelay, jitter_ms: 0, link_cut: nextCut });
      onRefreshStatus();
      const msg = nextCut ? "AI link cut active — watch HOLDING -> auto_rth" : `AI delay ${nextDelay} ms active`;
      setStatusMsg(msg);
      if (nextCut) {
        toast.warning("AI link cut active", "Watch HOLDING -> auto_rth while telemetry stays live.");
      } else {
        toast.info(nextDelay === 0 ? "AI link latency restored (0 ms)" : `AI link latency set to ${nextDelay} ms`);
      }
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : String(e);
      setStatusMsg(msg);
      toast.error("Link shaper failed", msg);
    } finally {
      setApplyingShaper(null);
    }
  };

  return (
    <div className="mx-auto flex w-full max-w-4xl flex-col gap-8 px-6 py-8">
      <PageHeader
        title="Manual & Safety"
        description="Fly the drone directly without AI, test AI link-loss failsafes, and inspect live pre-flight checks."
        action={
          <StatusBadge tone={linkCut ? "danger" : delayMs > 0 ? "warn" : "neutral"} className="font-mono">
            Executor: {executorState}
          </StatusBadge>
        }
      />

      <Section
        title="Manual Flight Pad"
        description="Sends one verified Flight move directly via MCP and clears operator lock automatically (works without AI keys)."
      >
        <div className="flex flex-col gap-5">
          <div className="grid gap-6 sm:grid-cols-2">
            {/* Visual 3x3 D-pad for Horizontal Moves */}
            <div className="flex flex-col items-center justify-center gap-2 rounded-[10px] border border-border bg-surface p-5">
              <span className="mb-1 text-[13px] font-medium text-secondary">Horizontal Move (1.5 m)</span>
              <div className="grid grid-cols-3 gap-2">
                <div />
                <Button size="md" loading={pendingMove === "fwd"} onClick={() => void runMove("fwd", "move", { forward_m: 1.5 })}>
                  {pendingMove !== "fwd" && <ArrowUp size={15} />} Fwd
                </Button>
                <div />
                <Button size="md" loading={pendingMove === "left"} onClick={() => void runMove("left", "move", { right_m: -1.5 })}>
                  {pendingMove !== "left" && <ArrowLeft size={15} />} Left
                </Button>
                <Button size="md" variant="primary" loading={pendingMove === "hold"} onClick={() => void runMove("hold", "hold", {})}>
                  Hold
                </Button>
                <Button size="md" loading={pendingMove === "right"} onClick={() => void runMove("right", "move", { right_m: 1.5 })}>
                  Right {pendingMove !== "right" && <ArrowRight size={15} />}
                </Button>
                <div />
                <Button size="md" loading={pendingMove === "back"} onClick={() => void runMove("back", "move", { forward_m: -1.5 })}>
                  {pendingMove !== "back" && <ArrowDown size={15} />} Back
                </Button>
                <div />
              </div>
            </div>

            {/* Vertical & Heading Controls */}
            <div className="flex flex-col justify-between gap-4 rounded-[10px] border border-border bg-surface p-5">
              <div className="flex flex-col gap-2">
                <span className="text-[13px] font-medium text-secondary">Takeoff &amp; Altitude</span>
                <div className="flex flex-wrap gap-2">
                  <Button variant="primary" loading={pendingMove === "takeoff"} onClick={() => void runMove("takeoff", "take_off", { altitude_m: 2.0 })}>
                    Take Off (2m)
                  </Button>
                  <Button loading={pendingMove === "up"} onClick={() => void runMove("up", "move", { up_m: 1.0 })}>
                    Climb +1m
                  </Button>
                  <Button loading={pendingMove === "down"} onClick={() => void runMove("down", "move", { up_m: -0.8 })}>
                    Descend -0.8m
                  </Button>
                </div>
              </div>

              <div className="flex flex-col gap-2">
                <span className="text-[13px] font-medium text-secondary">Heading</span>
                <div className="flex flex-wrap gap-2">
                  <Button loading={pendingMove === "turn_l"} onClick={() => void runMove("turn_l", "turn", { yaw_deg: -45 })}>
                    {pendingMove !== "turn_l" && <RotateCcw size={14} />} Turn Left 45°
                  </Button>
                  <Button loading={pendingMove === "turn_r"} onClick={() => void runMove("turn_r", "turn", { yaw_deg: 45 })}>
                    {pendingMove !== "turn_r" && <RotateCw size={14} />} Turn Right 45°
                  </Button>
                </div>
              </div>
            </div>
          </div>

          <div className="flex flex-wrap items-center gap-2.5">
            <Input
              className="min-w-[260px] flex-1"
              value={vocab}
              onChange={(e) => setVocab(e.target.value)}
              placeholder="Open-vocabulary target labels (e.g. red backpack, injured person)"
            />
            <Button
              variant="primary"
              loading={pendingMove === "vocab"}
              onClick={() =>
                void runMove("vocab", "set_vocabulary", {
                  terms: vocab
                    .split(",")
                    .map((s) => s.trim())
                    .filter(Boolean),
                })
              }
            >
              Update Tracked Labels
            </Button>
          </div>
        </div>
      </Section>

      <Section
        title="AI Link-Loss & Latency Simulation"
        description="Injects delay or drops ONLY the AI pilot MCP link. Passive UI telemetry and emergency HOLD / LAND / HOME stay live."
      >
        <div className="flex flex-col gap-4 rounded-[10px] border border-border bg-surface p-5">
          <div className="flex flex-wrap items-center gap-2.5">
            {[0, 500, 1500, 3000].map((ms) => (
              <Button
                key={ms}
                variant={delayMs === ms && !linkCut ? "primary" : "secondary"}
                loading={applyingShaper === `delay:${ms}`}
                onClick={() => void applyShaper(`delay:${ms}`, ms, false)}
              >
                {ms === 0 ? "0 ms (Normal)" : `${ms} ms Latency`}
              </Button>
            ))}
            <Button
              variant={linkCut ? "danger" : "secondary"}
              loading={applyingShaper === "cut"}
              onClick={() => void applyShaper("cut", delayMs, !linkCut)}
            >
              {applyingShaper !== "cut" && <Unplug size={14} />} {linkCut ? "Restore AI Link" : "Cut AI Link"}
            </Button>
          </div>
          {statusMsg && <p className="font-mono text-[13px] text-secondary">{statusMsg}</p>}
        </div>
      </Section>

      <Section
        title="Pre-Flight Checklist"
        description="Checks live autopilot heartbeat, proximity sensors, battery level, and camera stream freshness."
        action={
          <Button size="sm" loading={loadingPreflight} onClick={() => void loadPreflight(true)}>
            {!loadingPreflight && <RefreshCw size={13} />} {loadingPreflight ? "Checking…" : "Refresh"}
          </Button>
        }
      >
        <div className="divide-y divide-border overflow-hidden rounded-[10px] border border-border bg-surface">
          {preflight.map((item) => (
            <div key={item.id} className="flex items-center justify-between gap-4 px-5 py-3.5">
              <div>
                <div className="text-[14px] font-medium text-primary">{item.label}</div>
                <div className="mt-0.5 text-[13px] text-secondary">{item.detail}</div>
              </div>
              <StatusBadge tone={item.status === "pass" ? "ok" : item.status === "warn" ? "warn" : "danger"}>
                {item.status}
              </StatusBadge>
            </div>
          ))}
          {preflight.length === 0 && (
            <div className="px-5 py-4 text-[14px] text-muted">Start a session to view live pre-flight checks.</div>
          )}
        </div>
      </Section>
    </div>
  );
}
