import { Camera, Map as MapIcon, MessageSquare, Send } from "lucide-react";
import { useEffect, useState } from "react";
import { horusApi } from "../api";
import type { CameraFrame, DroneState, LiveMap, TimelineEvent } from "../types";
import { TacticalMap } from "./TacticalMap";
import { Button } from "./common/Button";
import { Textarea } from "./common/Surface";

interface Props {
  token: string;
  droneState: DroneState | null;
  liveMap: LiveMap | null;
  frame: CameraFrame | null;
  events: TimelineEvent[];
  onEmergency: (action: "hold" | "land" | "rth") => Promise<void> | void;
  onSendMission: (text: string) => Promise<void> | void;
  onFlyTo: (n: number, e: number) => void;
}

export function PhoneView({
  token,
  droneState,
  liveMap,
  frame,
  events,
  onEmergency,
  onSendMission,
  onFlyTo,
}: Props) {
  const [tab, setTab] = useState<"feed" | "map" | "camera">("feed");
  const [liveCamera, setLiveCamera] = useState(false);
  const [text, setText] = useState("");
  const [aiUrl, setAiUrl] = useState("");
  const [aiBytes, setAiBytes] = useState<number | null>(null);
  const [pendingEmergency, setPendingEmergency] = useState<"hold" | "land" | "rth" | null>(null);
  const [sendingMission, setSendingMission] = useState(false);

  const aiViews = events.filter((e) => e.kind === "ai_view" && typeof e.view_id === "string");
  const latestAi = aiViews[aiViews.length - 1];

  useEffect(() => {
    if (!latestAi?.view_id || !token) return;
    let active = true;
    horusApi
      .getAiViewBlobUrl(token, String(latestAi.view_id))
      .then((url) => {
        if (active) setAiUrl(url);
        else URL.revokeObjectURL(url);
      })
      .catch(() => {});
    if (typeof latestAi.bytes === "number") setAiBytes(latestAi.bytes);
    return () => {
      active = false;
    };
  }, [latestAi?.view_id, token]);

  const mode = droneState?.vehicle?.mode || "--";
  const alt =
    typeof droneState?.vehicle?.altitude_m === "number" ? `${droneState.vehicle.altitude_m.toFixed(1)}m` : "--";
  const exec = droneState?.state || "idle";

  return (
    <div className="flex min-h-screen flex-col bg-canvas text-primary">
      <header className="sticky top-0 z-30 flex items-center justify-between border-b border-border bg-surface px-4 py-3 pt-[calc(0.75rem+env(safe-area-inset-top,0px))]">
        <strong className="text-[15px] font-semibold tracking-tight">HORUS</strong>
        <span className="font-mono text-[12.5px] text-secondary tabular-nums">
          {mode} · {exec} · {alt}
        </span>
      </header>

      <div className="grid grid-cols-3 gap-2 border-b border-border bg-canvas px-3 py-2.5">
        <Button
          variant="danger"
          loading={pendingEmergency === "hold"}
          onClick={() => {
            setPendingEmergency("hold");
            void Promise.resolve(onEmergency("hold")).finally(() => setPendingEmergency(null));
          }}
        >
          HOLD
        </Button>
        <Button
          variant="danger"
          loading={pendingEmergency === "land"}
          onClick={() => {
            setPendingEmergency("land");
            void Promise.resolve(onEmergency("land")).finally(() => setPendingEmergency(null));
          }}
        >
          LAND
        </Button>
        <Button
          variant="danger"
          loading={pendingEmergency === "rth"}
          onClick={() => {
            setPendingEmergency("rth");
            void Promise.resolve(onEmergency("rth")).finally(() => setPendingEmergency(null));
          }}
        >
          HOME
        </Button>
      </div>

      {droneState?.operator_lock && (
        <div className="border-b border-danger bg-surface px-4 py-2.5 text-[13px] font-medium text-danger">
          OPERATOR LOCK active: send next mission to resume AI
        </div>
      )}

      <main className="flex flex-1 flex-col pb-20">
        {tab === "feed" && (
          <div className="flex flex-1 flex-col">
            <div className="flex flex-col gap-2.5 border-b border-border bg-surface p-3.5">
              <Textarea
                rows={2}
                value={text}
                onChange={(e) => setText(e.target.value)}
                placeholder="Mission for the AI pilot…"
              />
              <Button
                variant="primary"
                disabled={!text.trim()}
                loading={sendingMission}
                onClick={() => {
                  const trimmed = text.trim();
                  if (trimmed) {
                    setSendingMission(true);
                    void Promise.resolve(onSendMission(trimmed)).finally(() => setSendingMission(false));
                    setText("");
                  }
                }}
              >
                {!sendingMission && <Send size={14} />} {sendingMission ? "Sending…" : "Send Mission"}
              </Button>
            </div>
            <div className="divide-y divide-border">
              {events
                .slice(-40)
                .reverse()
                .map((ev) => {
                  if (ev.kind === "tool_use" || ev.kind === "tool_result") {
                    return (
                      <details key={ev.id} className="px-4 py-3 text-[13.5px]">
                        <summary className="cursor-pointer font-medium text-secondary">
                          Tool: {ev.tool || ev.kind}
                        </summary>
                        <pre className="mt-2 overflow-x-auto font-mono text-[12px] text-muted">
                          {JSON.stringify(ev, null, 2)}
                        </pre>
                      </details>
                    );
                  }
                  return (
                    <div key={ev.id} className="px-4 py-3 text-[14px]">
                      <div
                        className={`mb-0.5 font-mono text-[11.5px] uppercase tracking-wide ${
                          ev.kind === "emergency" ? "text-danger" : "text-muted"
                        }`}
                      >
                        {ev.kind}
                      </div>
                      {String(ev.text || ev.summary || ev.error || ev.action || "")}
                    </div>
                  );
                })}
            </div>
          </div>
        )}

        {tab === "map" && (
          <TacticalMap
            token={token}
            droneState={droneState}
            liveMap={liveMap}
            onFlyTo={onFlyTo}
            onUseInMission={(sel) => {
              setText(`Inspect ${sel.label}`);
              setTab("feed");
            }}
          />
        )}

        {tab === "camera" && (
          <div className="flex flex-1 flex-col">
            <div className="flex items-center justify-between border-b border-border bg-surface px-4 py-2.5">
              <strong className="text-[14px]">{liveCamera ? "Live Camera" : "What the AI saw"}</strong>
              <Button size="sm" onClick={() => setLiveCamera((v) => !v)}>
                {liveCamera ? "Show AI View" : "Live"}
              </Button>
            </div>
            <div className="flex flex-1 items-center justify-center bg-canvas p-2">
              {liveCamera && frame?.image_base64 ? (
                <img
                  src={`data:${frame.mime_type || "image/jpeg"};base64,${frame.image_base64}`}
                  alt="Live camera"
                  className="max-h-full w-full object-contain"
                />
              ) : aiUrl ? (
                <img src={aiUrl} alt="What the AI saw" className="max-h-full w-full object-contain" />
              ) : (
                <span className="text-[14px] text-muted">No camera frame yet.</span>
              )}
            </div>
            {aiBytes !== null && !liveCamera && (
              <div className="border-t border-border bg-surface px-4 py-2 font-mono text-[12px] text-muted tabular-nums">
                Frame size: {(aiBytes / 1024).toFixed(1)} KB
              </div>
            )}
          </div>
        )}
      </main>

      <nav className="fixed inset-x-0 bottom-0 z-30 grid grid-cols-3 border-t border-border bg-surface pb-[env(safe-area-inset-bottom,0px)]">
        {[
          { id: "feed" as const, label: "Feed", icon: <MessageSquare size={17} /> },
          { id: "map" as const, label: "Map", icon: <MapIcon size={17} /> },
          { id: "camera" as const, label: "Camera", icon: <Camera size={17} /> },
        ].map((item) => (
          <button
            key={item.id}
            type="button"
            onClick={() => setTab(item.id)}
            className={`flex flex-col items-center justify-center gap-1 py-2.5 text-[12px] font-medium cursor-pointer ${
              tab === item.id ? "text-primary" : "text-muted hover:text-secondary"
            }`}
          >
            {item.icon}
            <span>{item.label}</span>
          </button>
        ))}
      </nav>
    </div>
  );
}
