export type SceneVisualVariant =
  | "indoor_sar"
  | "forest_sar"
  | "urban_plaza"
  | "procedural"
  | "gazebo"
  | "gta"
  | "direct_usb"
  | "companion_board"
  | "grounder";

const VARIANT_ALT: Record<SceneVisualVariant, string> = {
  indoor_sar: "Indoor search-and-rescue room environment",
  forest_sar: "Aerial forest clearing and trail environment",
  urban_plaza: "Aerial modern city plaza environment",
  procedural: "Aerial rolling hills and procedural terrain",
  gazebo: "Gazebo 3D robotics simulation test arena",
  gta: "Golden-hour aerial city skyline for GTA V simulation",
  direct_usb: "Flight controller connected via USB cable",
  companion_board: "Carbon-fiber autonomous drone with onboard companion computer",
  grounder: "Daytime aerial view for visual object grounding",
};

interface Props {
  variant: SceneVisualVariant;
  tag?: string;
  active?: boolean;
}

export function SceneVisual({ variant, tag, active }: Props) {
  return (
    <div className="relative h-44 w-full overflow-hidden border-b border-border bg-canvas select-none">
      <img
        src={`/scenes/${variant}.jpg`}
        alt={VARIANT_ALT[variant]}
        loading="lazy"
        className="h-full w-full object-cover transition-transform duration-300 group-hover:scale-[1.03]"
      />

      {(tag || active) && (
        <div className="pointer-events-none absolute top-3 left-3 flex items-center gap-1.5">
          {tag && (
            <span className="rounded-[6px] border border-border bg-surface px-2 py-0.5 font-mono text-[11px] font-medium text-primary shadow-sm">
              {tag}
            </span>
          )}
          {active && (
            <span className="inline-flex items-center gap-1.5 rounded-[6px] border border-border bg-surface px-2 py-0.5 font-mono text-[11px] font-medium text-success shadow-sm">
              <span className="h-1.5 w-1.5 rounded-full bg-success" />
              Active
            </span>
          )}
        </div>
      )}
    </div>
  );
}
