import { ArrowDown, ArrowLeft, ArrowRight, ArrowUp, RotateCcw, RotateCw } from "lucide-react";
import { useEffect, useState } from "react";
import { horusApi } from "../api";
import type { GrounderStatus, SettingsTab } from "../types";
import { Button } from "./common/Button";
import { Input, StatusBadge } from "./common/Surface";
import { useToast } from "./common/Toast";

interface Props {
  token: string;
  running: boolean;
  onManualMove: (action: string, params: Record<string, unknown>) => Promise<boolean> | void;
  onOpenSettings: (tab?: SettingsTab) => void;
}

export function ManualFlightPanel({ token, running, onManualMove, onOpenSettings }: Props) {
  const [vocab, setVocab] = useState("red backpack, injured person");
  const [pendingMove, setPendingMove] = useState<string | null>(null);
  const [grounder, setGrounder] = useState<GrounderStatus | null>(null);
  const [grounderBusy, setGrounderBusy] = useState<"start" | "stop" | null>(null);
  const toast = useToast();

  useEffect(() => {
    horusApi.getGrounder(token).then(setGrounder).catch(() => {});
  }, [token]);

  const runMove = async (key: string, action: string, params: Record<string, unknown>) => {
    setPendingMove(key);
    try {
      await onManualMove(action, params);
    } finally {
      setPendingMove(null);
    }
  };

  const toggleGrounder = async (action: "start" | "stop") => {
    setGrounderBusy(action);
    try {
      const next =
        action === "start" ? await horusApi.startGrounder(token, "auto") : await horusApi.stopGrounder(token);
      setGrounder(next);
      toast.success(action === "start" ? "Visual Grounder started" : "Visual Grounder stopped");
    } catch (e: unknown) {
      toast.error(`Grounder ${action} failed`, e instanceof Error ? e.message : String(e));
    } finally {
      setGrounderBusy(null);
    }
  };

  return (
    <div className="flex flex-1 flex-col gap-4 overflow-y-auto p-4">
      <p className="text-[13px] text-secondary">
        Fly directly without AI keys. Sending any manual move clears operator lock automatically.
      </p>

      {/* Takeoff & Altitude */}
      <div className="flex flex-col gap-2 rounded-[10px] border border-border bg-canvas p-3.5">
        <span className="text-[12.5px] font-medium text-secondary">Takeoff &amp; Altitude</span>
        <div className="flex flex-wrap gap-2">
          <Button
            size="sm"
            variant="primary"
            disabled={!running}
            loading={pendingMove === "takeoff"}
            onClick={() => void runMove("takeoff", "take_off", { altitude_m: 2.0 })}
          >
            Take Off (2m)
          </Button>
          <Button
            size="sm"
            disabled={!running}
            loading={pendingMove === "up"}
            onClick={() => void runMove("up", "move", { up_m: 1.0 })}
          >
            Climb +1m
          </Button>
          <Button
            size="sm"
            disabled={!running}
            loading={pendingMove === "down"}
            onClick={() => void runMove("down", "move", { up_m: -0.8 })}
          >
            Descend -0.8m
          </Button>
        </div>
      </div>

      {/* Horizontal 3x3 D-pad */}
      <div className="flex flex-col items-center gap-2 rounded-[10px] border border-border bg-canvas p-4">
        <span className="text-[12.5px] font-medium text-secondary">Horizontal Move (1.5 m step)</span>
        <div className="grid grid-cols-3 gap-2">
          <div />
          <Button
            size="sm"
            disabled={!running}
            loading={pendingMove === "fwd"}
            onClick={() => void runMove("fwd", "move", { forward_m: 1.5 })}
          >
            {pendingMove !== "fwd" && <ArrowUp size={14} />} Fwd
          </Button>
          <div />
          <Button
            size="sm"
            disabled={!running}
            loading={pendingMove === "left"}
            onClick={() => void runMove("left", "move", { right_m: -1.5 })}
          >
            {pendingMove !== "left" && <ArrowLeft size={14} />} Left
          </Button>
          <Button
            size="sm"
            variant="primary"
            disabled={!running}
            loading={pendingMove === "hold"}
            onClick={() => void runMove("hold", "hold", {})}
          >
            Hold
          </Button>
          <Button
            size="sm"
            disabled={!running}
            loading={pendingMove === "right"}
            onClick={() => void runMove("right", "move", { right_m: 1.5 })}
          >
            Right {pendingMove !== "right" && <ArrowRight size={14} />}
          </Button>
          <div />
          <Button
            size="sm"
            disabled={!running}
            loading={pendingMove === "back"}
            onClick={() => void runMove("back", "move", { forward_m: -1.5 })}
          >
            {pendingMove !== "back" && <ArrowDown size={14} />} Back
          </Button>
          <div />
        </div>
      </div>

      {/* Heading */}
      <div className="flex flex-col gap-2 rounded-[10px] border border-border bg-canvas p-3.5">
        <span className="text-[12.5px] font-medium text-secondary">Heading</span>
        <div className="flex flex-wrap gap-2">
          <Button
            size="sm"
            disabled={!running}
            loading={pendingMove === "turn_l"}
            onClick={() => void runMove("turn_l", "turn", { yaw_deg: -45 })}
          >
            {pendingMove !== "turn_l" && <RotateCcw size={13} />} Turn Left 45°
          </Button>
          <Button
            size="sm"
            disabled={!running}
            loading={pendingMove === "turn_r"}
            onClick={() => void runMove("turn_r", "turn", { yaw_deg: 45 })}
          >
            {pendingMove !== "turn_r" && <RotateCw size={13} />} Turn Right 45°
          </Button>
        </div>
      </div>

      {/* Tracked Labels + Visual Grounder quick bar */}
      <div className="flex flex-col gap-3 rounded-[10px] border border-border bg-canvas p-3.5">
        <div className="flex flex-col gap-1.5">
          <span className="text-[12.5px] font-medium text-secondary">Tracked Target Labels (Open-Vocab)</span>
          <div className="flex gap-2">
            <Input
              className="!h-9 flex-1 text-[13px]"
              value={vocab}
              onChange={(e) => setVocab(e.target.value)}
              placeholder="e.g. red backpack, injured person"
            />
            <Button
              size="sm"
              variant="primary"
              disabled={!running}
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
              Update
            </Button>
          </div>
        </div>

        <div className="flex flex-wrap items-center justify-between gap-2 border-t border-border pt-2.5 text-[12.5px]">
          <div className="flex items-center gap-2">
            <span className="font-medium text-primary">Visual Grounder</span>
            <StatusBadge tone={grounder?.running ? "ok" : "neutral"} className="!py-0.5 !text-[11.5px]">
              {grounder?.running ? `Running${grounder.backend ? ` · ${grounder.backend}` : ""}` : "Stopped"}
            </StatusBadge>
          </div>
          <div className="flex items-center gap-1.5">
            {grounder?.running ? (
              <Button
                size="sm"
                className="!h-7 !px-2.5 !text-[12px]"
                loading={grounderBusy === "stop"}
                disabled={grounderBusy !== null}
                onClick={() => void toggleGrounder("stop")}
              >
                Stop
              </Button>
            ) : (
              <Button
                size="sm"
                variant="secondary"
                className="!h-7 !px-2.5 !text-[12px]"
                loading={grounderBusy === "start"}
                disabled={grounderBusy !== null}
                onClick={() => void toggleGrounder("start")}
              >
                Start
              </Button>
            )}
            <Button
              size="sm"
              variant="ghost"
              className="!h-7 !px-2 !text-[12px]"
              onClick={() => onOpenSettings("ai")}
            >
              Configure →
            </Button>
          </div>
        </div>
      </div>
    </div>
  );
}
