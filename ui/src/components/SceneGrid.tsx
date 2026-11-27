import { ChevronDown, ChevronUp, Play } from "lucide-react";
import { useState } from "react";
import type { SceneInfo } from "../types";
import { SceneVisual, type SceneVisualVariant } from "./SceneVisual";
import { Button } from "./common/Button";
import { Input, StatusBadge } from "./common/Surface";

const SCENE_TAG: Record<string, { variant: SceneVisualVariant; tag: string }> = {
  indoor_sar: { variant: "indoor_sar", tag: "Indoor" },
  forest_sar: { variant: "forest_sar", tag: "Outdoor" },
  urban_plaza: { variant: "urban_plaza", tag: "Outdoor" },
  procedural: { variant: "procedural", tag: "Seeded" },
};

interface SceneCardProps {
  scene: SceneInfo;
  isCurrent: boolean;
  busy: boolean;
  launchingThis: boolean;
  seed: number;
  onSeedChange: (seed: number) => void;
  onLaunch: (scene: SceneInfo, seed: number) => void;
}

export function SceneCard({
  scene,
  isCurrent,
  busy,
  launchingThis,
  seed,
  onSeedChange,
  onLaunch,
}: SceneCardProps) {
  const visual = SCENE_TAG[scene.name] || { variant: "procedural" as const, tag: "World" };

  return (
    <article className="group flex flex-col overflow-hidden rounded-[12px] border border-border bg-surface transition-colors hover:border-border-strong">
      <SceneVisual variant={visual.variant} tag={visual.tag} active={isCurrent} />

      <div className="flex flex-1 flex-col justify-between gap-4 p-5">
        <div>
          <div className="flex items-center justify-between gap-2">
            <h3 className="text-[16px] font-semibold tracking-tight text-primary">{scene.title}</h3>
            {isCurrent && <StatusBadge tone="ok">Running</StatusBadge>}
          </div>
          <p className="mt-1.5 text-[13.5px] leading-relaxed text-secondary">{scene.subtitle}</p>
        </div>

        <div className="flex flex-wrap items-center justify-between gap-3 border-t border-border pt-4">
          {scene.name === "procedural" ? (
            <label className="flex items-center gap-2 text-[13px] text-secondary">
              Seed
              <Input
                type="number"
                aria-label="Procedural seed"
                value={seed}
                onChange={(e) => onSeedChange(Number(e.target.value))}
                className="!h-9 !w-24 tabular-nums"
              />
            </label>
          ) : (
            <span className="font-mono text-[12px] text-muted">{scene.name}</span>
          )}

          <Button
            size="sm"
            variant={isCurrent ? "secondary" : "primary"}
            disabled={busy}
            loading={launchingThis}
            onClick={() => onLaunch(scene, seed)}
          >
            {!launchingThis && <Play size={13} />}
            {launchingThis ? "Launching…" : isCurrent ? "Restart" : "Launch"}
          </Button>
        </div>
      </div>
    </article>
  );
}

interface BuiltinScenesGridProps {
  scenes: SceneInfo[];
  activeSceneName?: string | null;
  running?: boolean;
  busy: boolean;
  launchingSceneName: string | null;
  initialLimit?: number;
  onLaunchScene: (scene: SceneInfo, seed: number) => void;
}

export function BuiltinScenesGrid({
  scenes,
  activeSceneName,
  running = false,
  busy,
  launchingSceneName,
  initialLimit,
  onLaunchScene,
}: BuiltinScenesGridProps) {
  const [seed, setSeed] = useState(42);
  const [expanded, setExpanded] = useState(false);

  const hasMore = typeof initialLimit === "number" && scenes.length > initialLimit;
  const visibleScenes = hasMore && !expanded ? scenes.slice(0, initialLimit) : scenes;
  const hiddenCount = hasMore ? scenes.length - initialLimit : 0;

  return (
    <div className="flex flex-col gap-4">
      <div className="grid gap-5 sm:grid-cols-2">
        {visibleScenes.map((sc) => (
          <SceneCard
            key={sc.name}
            scene={sc}
            isCurrent={Boolean(running && activeSceneName === sc.name)}
            busy={busy}
            launchingThis={launchingSceneName === sc.name}
            seed={seed}
            onSeedChange={setSeed}
            onLaunch={onLaunchScene}
          />
        ))}
      </div>

      {hasMore && (
        <div className="flex justify-center">
          <Button size="sm" variant="ghost" onClick={() => setExpanded((v) => !v)}>
            {expanded ? (
              <>
                <ChevronUp size={14} /> Show fewer scenes
              </>
            ) : (
              <>
                <ChevronDown size={14} /> Show more scenes (+{hiddenCount})
              </>
            )}
          </Button>
        </div>
      )}
    </div>
  );
}
