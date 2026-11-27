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

export const DEFAULT_BUILTIN_SCENES: SceneInfo[] = [
  {
    name: "indoor_sar",
    title: "Indoor SAR Room",
    subtitle: "Operations facility with terrazzo floor, sunlit windows, corridor, furniture, AprilTag pad, and SAR subjects",
    indoor: true,
    prompts: [
      "Take off and find the person in the red shirt, then hover 2 m away.",
      "Fly down the north corridor and report everyone you see.",
      "Test wall avoidance: try flying 8 m north toward the wall.",
      "Return home and land on the AprilTag pad.",
    ],
  },
  {
    name: "forest_sar",
    title: "Outdoor Forest",
    subtitle: "Alpine woodland clearing with conifers, deciduous trees, boulders, campsite, trail, and hikers",
    indoor: false,
    prompts: [
      "Take off to 4 m and search along the trail for hikers.",
      "Find the hiker near the clearing and report their position.",
      "Return home and land on the AprilTag pad.",
    ],
  },
  {
    name: "urban_plaza",
    title: "Urban Plaza",
    subtitle: "City square with glass & brick high-rises, water fountain, vehicles, streetlights, and pedestrians",
    indoor: false,
    prompts: [
      "Find Central Plaza on the map and fly there at 6 m altitude.",
      "Search the plaza for pedestrians and mark any person you spot.",
      "Return home and land on the AprilTag pad.",
    ],
  },
  {
    name: "procedural",
    title: "Procedural World",
    subtitle: "Seeded countryside valley with ranger shelter, campsite, rock outcrops, mixed forest, and hikers",
    indoor: false,
    prompts: [
      "Take off to 5 m, inspect nearby landmarks, and look for hikers.",
      "Return home and land on the AprilTag pad.",
    ],
  },
];

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

  const effectiveScenes = scenes.length > 0 ? scenes : DEFAULT_BUILTIN_SCENES;
  const hasMore = typeof initialLimit === "number" && effectiveScenes.length > initialLimit;
  const visibleScenes = hasMore && !expanded ? effectiveScenes.slice(0, initialLimit) : effectiveScenes;
  const hiddenCount = hasMore ? effectiveScenes.length - initialLimit : 0;

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
