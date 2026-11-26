import { ArrowDown, ArrowLeft, ArrowRight, ArrowUp, RefreshCw, RotateCcw, RotateCw, Unplug } from "lucide-react";
import { useEffect, useState } from "react";
import { horusApi } from "../api";
import type { PreflightItem, StudioStatus } from "../types";
import { Button } from "./common/Button";
import { Input, PageHeader, Section, StatusBadge } from "./common/Surface";

interface Props {
  token: string;
  studioStatus: StudioStatus | null;
  executorState: string;
  onManualMove: (action: string, params: Record<string, unknown>) => void;
  onRefreshStatus: () => void;
}

export function FaultLabView({ token, studioStatus, executorState, onManualMove, onRefreshStatus }: Props) {
  const [delayMs, setDelayMs] = useState(studioStatus?.link_shaper.delay_ms ?? 0);
  const [linkCut, setLinkCut] = useState(studioStatus?.link_shaper.link_cut ?? false);
  const [vocab, setVocab] = useState("red backpack, injured person");
  const [preflight, setPreflight] = useState<PreflightItem[]>([]);
  const [statusMsg, setStatusMsg] = useState("");

  const loadPreflight = () => {
    horusApi
      .getPreflight(token)
      .then((r) => setPreflight(r.items))
      .catch(() => {});
  };

  useEffect(() => {
    loadPreflight();
  }, [token]);

  const applyShaper = async (nextDelay: number, nextCut: boolean) => {
    setDelayMs(nextDelay);
    setLinkCut(nextCut);
    try {
      await horusApi.setLinkShaper(token, { delay_ms: nextDelay, jitter_ms: 0, link_cut: nextCut });
      onRefreshStatus();
      setStatusMsg(nextCut ? "AI link cut active — watch HOLDING -> auto_rth" : `AI delay ${nextDelay} ms active`);
    } catch (e: unknown) {
      setStatusMsg(e instanceof Error ? e.message : String(e));
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
                <Button size="md" onClick={() => onManualMove("move", { forward_m: 1.5 })}>
                  <ArrowUp size={15} /> Fwd
                </Button>
                <div />
                <Button size="md" onClick={() => onManualMove("move", { right_m: -1.5 })}>
                  <ArrowLeft size={15} /> Left
                </Button>
                <Button size="md" variant="primary" onClick={() => onManualMove("hold", {})}>
                  Hold
                </Button>
                <Button size="md" onClick={() => onManualMove("move", { right_m: 1.5 })}>
                  Right <ArrowRight size={15} />
                </Button>
                <div />
                <Button size="md" onClick={() => onManualMove("move", { forward_m: -1.5 })}>
                  <ArrowDown size={15} /> Back
                </Button>
                <div />
              </div>
            </div>

            {/* Vertical & Heading Controls */}
            <div className="flex flex-col justify-between gap-4 rounded-[10px] border border-border bg-surface p-5">
              <div className="flex flex-col gap-2">
                <span className="text-[13px] font-medium text-secondary">Takeoff &amp; Altitude</span>
                <div className="flex flex-wrap gap-2">
                  <Button variant="primary" onClick={() => onManualMove("take_off", { altitude_m: 2.0 })}>
                    Take Off (2m)
                  </Button>
                  <Button onClick={() => onManualMove("move", { up_m: 1.0 })}>Climb +1m</Button>
                  <Button onClick={() => onManualMove("move", { up_m: -0.8 })}>Descend -0.8m</Button>
                </div>
              </div>

              <div className="flex flex-col gap-2">
                <span className="text-[13px] font-medium text-secondary">Heading</span>
                <div className="flex flex-wrap gap-2">
                  <Button onClick={() => onManualMove("turn", { yaw_deg: -45 })}>
                    <RotateCcw size={14} /> Turn Left 45°
                  </Button>
                  <Button onClick={() => onManualMove("turn", { yaw_deg: 45 })}>
                    <RotateCw size={14} /> Turn Right 45°
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
              onClick={() =>
                onManualMove("set_vocabulary", {
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
                onClick={() => void applyShaper(ms, false)}
              >
                {ms === 0 ? "0 ms (Normal)" : `${ms} ms Latency`}
              </Button>
            ))}
            <Button variant={linkCut ? "danger" : "secondary"} onClick={() => void applyShaper(delayMs, !linkCut)}>
              <Unplug size={14} /> {linkCut ? "Restore AI Link" : "Cut AI Link"}
            </Button>
          </div>
          {statusMsg && <p className="font-mono text-[13px] text-secondary">{statusMsg}</p>}
        </div>
      </Section>

      <Section
        title="Pre-Flight Checklist"
        description="Checks live autopilot heartbeat, proximity sensors, battery level, and camera stream freshness."
        action={
          <Button size="sm" onClick={loadPreflight}>
            <RefreshCw size={13} /> Refresh
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
