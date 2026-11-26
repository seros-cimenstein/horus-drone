import { ArrowDown, ArrowLeft, ArrowRight, ArrowUp, Eye, LocateFixed } from "lucide-react";
import { useEffect, useState } from "react";
import { horusApi } from "../api";
import type { CameraFrame, StudioStatus, TimelineEvent } from "../types";
import { Button } from "./common/Button";
import { Select, StatusBadge } from "./common/Surface";

interface Props {
  token: string;
  mode: "camera" | "3d" | "aiviews";
  studioStatus: StudioStatus | null;
  frame: CameraFrame | null;
  events: TimelineEvent[];
  controlsDisabled?: boolean;
  onManualMove: (action: string, params: Record<string, unknown>) => void;
}

export function VisualStage({
  token,
  mode,
  studioStatus,
  frame,
  events,
  controlsDisabled = false,
  onManualMove,
}: Props) {
  const [preset, setPreset] = useState<"follow" | "overhead" | "orbit">("follow");
  const [showTof, setShowTof] = useState(true);
  const [spectatorUrl, setSpectatorUrl] = useState("");
  const [aiThumbUrls, setAiThumbUrls] = useState<Record<string, string>>({});
  const [pitch, setPitch] = useState(-15);
  const [yaw, setYaw] = useState(0);

  const has3d = Boolean(studioStatus?.spectator_url);
  const aiViews = events
    .filter((e) => e.kind === "ai_view" && typeof e.view_id === "string")
    .slice(-10)
    .reverse();

  useEffect(() => {
    if (mode !== "3d" || !has3d || !token) return;
    let active = true;
    let currentUrl = "";
    const poll = async () => {
      try {
        const nextUrl = await horusApi.getSpectatorBlobUrl(token, preset, showTof);
        if (!active) {
          URL.revokeObjectURL(nextUrl);
          return;
        }
        if (currentUrl) URL.revokeObjectURL(currentUrl);
        currentUrl = nextUrl;
        setSpectatorUrl(nextUrl);
      } catch {
        // transient frame error
      }
    };
    void poll();
    const timer = window.setInterval(() => void poll(), 400);
    return () => {
      active = false;
      window.clearInterval(timer);
      if (currentUrl) URL.revokeObjectURL(currentUrl);
    };
  }, [mode, has3d, token, preset, showTof]);

  useEffect(() => {
    if (mode !== "aiviews" || !token) return;
    let active = true;
    for (const ev of aiViews) {
      const id = String(ev.view_id);
      if (aiThumbUrls[id]) continue;
      void horusApi
        .getAiViewBlobUrl(token, id)
        .then((url) => {
          if (active) setAiThumbUrls((prev) => ({ ...prev, [id]: url }));
          else URL.revokeObjectURL(url);
        })
        .catch(() => {});
    }
    return () => {
      active = false;
    };
  }, [mode, token, aiViews.length]);

  const nudgeGimbal = (dp: number, dy: number) => {
    const np = Math.max(-90, Math.min(45, pitch + dp));
    const ny = Math.max(-160, Math.min(160, yaw + dy));
    setPitch(np);
    setYaw(ny);
    onManualMove("look", { pitch_deg: np, yaw_deg: ny });
  };

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="relative flex min-h-[320px] flex-1 items-center justify-center overflow-hidden bg-canvas p-2 lg:min-h-0">
        {mode === "camera" &&
          (frame?.image_base64 ? (
            <img
              src={`data:${frame.mime_type || "image/jpeg"};base64,${frame.image_base64}`}
              alt="Drone camera"
              className="h-full w-full rounded-[6px] object-contain"
            />
          ) : (
            <div className="flex flex-col items-center gap-2.5 text-muted">
              <Eye size={22} strokeWidth={1.6} />
              <span className="text-[14px]">Waiting for drone camera stream…</span>
            </div>
          ))}

        {mode === "3d" &&
          (spectatorUrl ? (
            <img
              src={spectatorUrl}
              alt="3D World Spectator Camera"
              className="h-full w-full rounded-[6px] object-contain"
            />
          ) : (
            <span className="text-[14px] text-muted">
              {has3d ? "Loading 3D spectator view…" : "3D spectator view is only available in Built-in 3D mode."}
            </span>
          ))}

        {mode === "aiviews" &&
          (aiViews.length > 0 && aiThumbUrls[String(aiViews[0].view_id)] ? (
            <img
              src={aiThumbUrls[String(aiViews[0].view_id)]}
              alt="Latest frame sent to AI model"
              className="h-full w-full rounded-[6px] object-contain"
            />
          ) : (
            <span className="text-[14px] text-muted">No camera frame has been sent to the AI model yet.</span>
          ))}

        {mode === "3d" && has3d && (
          <div className="absolute top-4 right-4 flex items-center gap-1.5 rounded-[8px] border border-border bg-surface p-1">
            <Select
              aria-label="3D camera preset"
              value={preset}
              onChange={(e) => setPreset(e.target.value as "follow" | "overhead" | "orbit")}
              className="!h-8 !w-auto !border-0 !bg-raised !px-2.5 !py-0 !text-[13px]"
            >
              <option value="follow">Follow</option>
              <option value="overhead">Overhead</option>
              <option value="orbit">Orbit</option>
            </Select>
            <Button size="sm" variant={showTof ? "primary" : "ghost"} className="!h-8" onClick={() => setShowTof((v) => !v)}>
              ToF Beams: {showTof ? "On" : "Off"}
            </Button>
          </div>
        )}
      </div>

      {mode === "camera" && (frame?.candidates?.length ?? 0) > 0 && (
        <div className="flex shrink-0 flex-wrap items-center gap-2 border-t border-border bg-surface px-4 py-2.5">
          <span className="text-[12.5px] font-medium text-muted">Detected:</span>
          {frame!.candidates!.map((c) => (
            <StatusBadge key={c.id} tone="ok" className="tabular-nums">
              #{c.id} {c.label}
              {typeof c.distance_m === "number" ? ` · ${c.distance_m.toFixed(1)}m` : ""}
            </StatusBadge>
          ))}
        </div>
      )}

      {mode === "aiviews" && aiViews.length > 1 && (
        <div className="flex shrink-0 flex-wrap gap-2 border-t border-border bg-surface px-4 py-2.5">
          {aiViews.slice(0, 6).map((ev) => {
            const id = String(ev.view_id);
            return aiThumbUrls[id] ? (
              <img
                key={id}
                src={aiThumbUrls[id]}
                alt={String(ev.tool || "ai view")}
                className="h-14 w-[76px] rounded-[6px] border border-border object-cover"
              />
            ) : null;
          })}
        </div>
      )}

      <div className="flex shrink-0 flex-wrap items-center justify-between gap-2 border-t border-border bg-surface px-4 py-2.5">
        <span className="font-mono text-[12.5px] text-secondary tabular-nums">
          Gimbal: {pitch}° pitch · {yaw}° yaw
        </span>
        <div className="flex flex-wrap items-center gap-1.5">
          <Button
            size="sm"
            variant="secondary"
            disabled={controlsDisabled}
            onClick={() => nudgeGimbal(15, 0)}
            aria-label="Pitch gimbal up"
          >
            <ArrowUp size={14} /> Up
          </Button>
          <Button
            size="sm"
            variant="secondary"
            disabled={controlsDisabled}
            onClick={() => nudgeGimbal(-15, 0)}
            aria-label="Pitch gimbal down"
          >
            <ArrowDown size={14} /> Down
          </Button>
          <Button
            size="sm"
            variant="secondary"
            disabled={controlsDisabled}
            onClick={() => nudgeGimbal(0, -25)}
            aria-label="Pan gimbal left"
          >
            <ArrowLeft size={14} /> Left
          </Button>
          <Button
            size="sm"
            variant="secondary"
            disabled={controlsDisabled}
            onClick={() => nudgeGimbal(0, 25)}
            aria-label="Pan gimbal right"
          >
            Right <ArrowRight size={14} />
          </Button>
          <Button
            size="sm"
            variant="secondary"
            disabled={controlsDisabled}
            onClick={() => {
              setPitch(-90);
              setYaw(0);
              onManualMove("look", { pitch_deg: -90, yaw_deg: 0 });
            }}
          >
            Look Down
          </Button>
          <Button
            size="sm"
            variant="secondary"
            disabled={controlsDisabled}
            onClick={() => {
              setPitch(-15);
              setYaw(0);
              onManualMove("look", { pitch_deg: -15, yaw_deg: 0 });
            }}
          >
            <LocateFixed size={14} /> Center
          </Button>
        </div>
      </div>
    </div>
  );
}
