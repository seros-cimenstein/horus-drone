import { Crosshair, Database, MapPin, Minus, Pentagon, Plus, Square, Trash2 } from "lucide-react";
import { type MouseEvent, useEffect, useRef, useState } from "react";
import { clearTileCache, getCachedTile, horusApi, putCachedTile } from "../api";
import type { DroneState, LiveMap, MapIndex, MapSelection, MapTileData } from "../types";
import { Button } from "./common/Button";

interface Props {
  token: string;
  droneState: DroneState | null;
  liveMap: LiveMap | null;
  onFlyTo: (north: number, east: number) => void;
  onUseInMission: (selection: MapSelection) => void;
}

export function TacticalMap({ token, droneState, liveMap, onFlyTo, onUseInMission }: Props) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const [mapIndex, setMapIndex] = useState<MapIndex | null>(null);
  const [tiles, setTiles] = useState<Map<string, MapTileData>>(new Map());
  const [follow, setFollow] = useState(true);
  const [center, setCenter] = useState<[number, number]>([0, 0]);
  const [pxPerM, setPxPerM] = useState(14);
  const [drawMode, setDrawMode] = useState<"none" | "point" | "polygon" | "rect">("none");
  const [draftPoints, setDraftPoints] = useState<Array<[number, number]>>([]);
  const [selection, setSelection] = useState<MapSelection | null>(null);
  const [offlineStatus, setOfflineStatus] = useState("");
  const dragRef = useRef<{ x: number; y: number; cn: number; ce: number } | null>(null);

  const pose = liveMap?.pose || droneState?.vehicle?.pose;

  useEffect(() => {
    if (follow && pose) {
      setCenter([pose.north_m, pose.east_m]);
    }
  }, [follow, pose?.north_m, pose?.east_m]);

  useEffect(() => {
    if (!token) return;
    let active = true;
    horusApi
      .getMapIndex(token)
      .then(async (idx) => {
        if (!active) return;
        setMapIndex(idx);
        const level0 = idx.tiles["0"] || [];
        const loaded = new Map<string, MapTileData>();
        for (const [tx, ty] of level0.slice(0, 16)) {
          const cacheKey = `${idx.version}:0:${tx}:${ty}`;
          let tile = await getCachedTile(cacheKey);
          if (!tile) {
            tile = await horusApi.getMapTile(token, 0, tx, ty);
            await putCachedTile(cacheKey, tile);
          }
          loaded.set(`0:${tx}:${ty}`, tile);
        }
        if (active) setTiles(loaded);
      })
      .catch(() => {
        if (active) setMapIndex(null);
      });
    return () => {
      active = false;
    };
  }, [token]);

  const toScreen = (n: number, e: number, w: number, h: number): [number, number] => [
    w * 0.5 + (e - center[1]) * pxPerM,
    h * 0.5 - (n - center[0]) * pxPerM,
  ];

  const toWorld = (sx: number, sy: number, w: number, h: number): [number, number] => [
    center[0] + (h * 0.5 - sy) / pxPerM,
    center[1] + (sx - w * 0.5) / pxPerM,
  ];

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const rect = canvas.getBoundingClientRect();
    const w = Math.max(300, Math.floor(rect.width));
    const h = Math.max(280, Math.floor(rect.height));
    if (canvas.width !== w || canvas.height !== h) {
      canvas.width = w;
      canvas.height = h;
    }
    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    ctx.fillStyle = "#09090b";
    ctx.fillRect(0, 0, w, h);

    const gridStepM = pxPerM >= 20 ? 1 : pxPerM >= 7 ? 5 : 20;
    ctx.strokeStyle = "rgba(161, 161, 170, 0.14)";
    ctx.lineWidth = 1;
    const minE = center[1] - w / (2 * pxPerM);
    const maxE = center[1] + w / (2 * pxPerM);
    const minN = center[0] - h / (2 * pxPerM);
    const maxN = center[0] + h / (2 * pxPerM);
    for (let e = Math.floor(minE / gridStepM) * gridStepM; e <= maxE; e += gridStepM) {
      const [sx] = toScreen(0, e, w, h);
      ctx.beginPath();
      ctx.moveTo(sx, 0);
      ctx.lineTo(sx, h);
      ctx.stroke();
    }
    for (let n = Math.floor(minN / gridStepM) * gridStepM; n <= maxN; n += gridStepM) {
      const [, sy] = toScreen(n, 0, w, h);
      ctx.beginPath();
      ctx.moveTo(0, sy);
      ctx.lineTo(w, sy);
      ctx.stroke();
    }

    for (const tile of tiles.values()) {
      for (const area of tile.walkable || []) {
        if (area.outline.length < 3) continue;
        ctx.fillStyle = "rgba(34, 197, 94, 0.08)";
        ctx.beginPath();
        area.outline.forEach(([n, e], i) => {
          const [sx, sy] = toScreen(n, e, w, h);
          if (i === 0) ctx.moveTo(sx, sy);
          else ctx.lineTo(sx, sy);
        });
        ctx.closePath();
        ctx.fill();
      }
      for (const obs of tile.obstacles || []) {
        if (obs.outline.length < 3) continue;
        ctx.fillStyle = "rgba(161, 161, 170, 0.22)";
        ctx.strokeStyle = "rgba(161, 161, 170, 0.55)";
        ctx.beginPath();
        obs.outline.forEach(([n, e], i) => {
          const [sx, sy] = toScreen(n, e, w, h);
          if (i === 0) ctx.moveTo(sx, sy);
          else ctx.lineTo(sx, sy);
        });
        ctx.closePath();
        ctx.fill();
        ctx.stroke();
      }
      for (const way of tile.ways || []) {
        if (way.points.length < 2) continue;
        ctx.strokeStyle = "rgba(212, 212, 216, 0.45)";
        ctx.lineWidth = 2;
        ctx.beginPath();
        way.points.forEach(([n, e], i) => {
          const [sx, sy] = toScreen(n, e, w, h);
          if (i === 0) ctx.moveTo(sx, sy);
          else ctx.lineTo(sx, sy);
        });
        ctx.stroke();
      }
      for (const place of tile.places || []) {
        const [sx, sy] = toScreen(place.north_m, place.east_m, w, h);
        ctx.fillStyle = "#d4d4d8";
        ctx.beginPath();
        ctx.arc(sx, sy, 3.5, 0, Math.PI * 2);
        ctx.fill();
        ctx.font = "12px Inter, system-ui, sans-serif";
        ctx.fillText(place.name, sx + 6, sy + 4);
      }
    }

    const [hx, hy] = toScreen(0, 0, w, h);
    ctx.strokeStyle = "#f59e0b";
    ctx.lineWidth = 2;
    ctx.strokeRect(hx - 6, hy - 6, 12, 12);
    ctx.fillStyle = "#f59e0b";
    ctx.font = "12px ui-monospace, monospace";
    ctx.fillText("HOME", hx + 10, hy + 4);

    for (const mark of liveMap?.marks || []) {
      const [mx, my] = toScreen(mark.north_m, mark.east_m, w, h);
      ctx.fillStyle = "#e4e4e7";
      ctx.beginPath();
      ctx.arc(mx, my, 4.5, 0, Math.PI * 2);
      ctx.fill();
      ctx.fillText(mark.name, mx + 7, my + 4);
    }
    for (const cand of liveMap?.candidates || []) {
      const [cx, cy] = toScreen(cand.north_m, cand.east_m, w, h);
      ctx.fillStyle = "#22c55e";
      ctx.beginPath();
      ctx.arc(cx, cy, 4.5, 0, Math.PI * 2);
      ctx.fill();
      ctx.fillText(`#${cand.id} ${cand.label}`, cx + 7, cy + 4);
    }

    const drawPoly = (pts: Array<[number, number]>, label: string) => {
      if (!pts.length) return;
      ctx.strokeStyle = "#fafafa";
      ctx.fillStyle = "rgba(250, 250, 250, 0.14)";
      ctx.lineWidth = 2;
      ctx.beginPath();
      pts.forEach(([n, e], i) => {
        const [sx, sy] = toScreen(n, e, w, h);
        if (i === 0) ctx.moveTo(sx, sy);
        else ctx.lineTo(sx, sy);
      });
      if (pts.length >= 3) {
        ctx.closePath();
        ctx.fill();
      }
      ctx.stroke();
      const [lx, ly] = toScreen(pts[0][0], pts[0][1], w, h);
      ctx.fillStyle = "#fafafa";
      ctx.fillText(label, lx + 6, ly - 6);
    };

    if (draftPoints.length) drawPoly(draftPoints, "Drawing…");
    if (selection?.kind === "point") {
      const [sx, sy] = toScreen(selection.point[0], selection.point[1], w, h);
      ctx.strokeStyle = "#fafafa";
      ctx.lineWidth = 2;
      ctx.beginPath();
      ctx.arc(sx, sy, 7, 0, Math.PI * 2);
      ctx.stroke();
      ctx.fillStyle = "#fafafa";
      ctx.fillText(selection.label, sx + 9, sy - 6);
    } else if (selection?.kind === "area") {
      drawPoly(selection.points, selection.label);
    }

    if (pose) {
      const [dx, dy] = toScreen(pose.north_m, pose.east_m, w, h);
      const headingRad = ((liveMap?.heading_deg ?? pose.yaw_deg ?? 0) * Math.PI) / 180;
      const prox = droneState?.vehicle?.proximity;
      if (prox) {
        const keys = [
          "front_m", "front_right_m", "right_m", "back_right_m",
          "back_m", "back_left_m", "left_m", "front_left_m",
        ];
        keys.forEach((k, idx) => {
          const dist = prox[k];
          if (typeof dist !== "number") return;
          const ang = headingRad + (idx * Math.PI) / 4 - Math.PI / 2;
          const r = Math.min(4.0, Math.max(0.2, dist)) * pxPerM;
          ctx.strokeStyle = dist < 1.2 ? "rgba(220, 38, 38, 0.85)" : "rgba(34, 197, 94, 0.5)";
          ctx.lineWidth = 2;
          ctx.beginPath();
          ctx.arc(dx, dy, r, ang - 0.34, ang + 0.34);
          ctx.stroke();
        });
      }
      ctx.save();
      ctx.translate(dx, dy);
      ctx.rotate(headingRad);
      ctx.fillStyle = "#fafafa";
      ctx.strokeStyle = "#09090b";
      ctx.lineWidth = 1.5;
      ctx.beginPath();
      ctx.moveTo(0, -10);
      ctx.lineTo(7, 8);
      ctx.lineTo(0, 4);
      ctx.lineTo(-7, 8);
      ctx.closePath();
      ctx.fill();
      ctx.stroke();
      ctx.restore();
    }
  }, [center, pxPerM, tiles, liveMap, droneState, draftPoints, selection]);

  const handleCanvasClick = (ev: MouseEvent<HTMLCanvasElement>) => {
    const canvas = canvasRef.current;
    if (!canvas || drawMode === "none") return;
    const rect = canvas.getBoundingClientRect();
    const [n, e] = toWorld(ev.clientX - rect.left, ev.clientY - rect.top, canvas.width, canvas.height);
    const pt: [number, number] = [Number(n.toFixed(1)), Number(e.toFixed(1))];
    if (drawMode === "point") {
      setSelection({ kind: "point", label: `P1 (${pt[0]}m, ${pt[1]}m)`, point: pt });
      setDrawMode("none");
      setDraftPoints([]);
    } else if (drawMode === "rect") {
      if (draftPoints.length === 0) {
        setDraftPoints([pt]);
      } else {
        const a = draftPoints[0];
        setSelection({
          kind: "area",
          label: "A1 Rectangle",
          points: [a, [a[0], pt[1]], pt, [pt[0], a[1]]],
        });
        setDraftPoints([]);
        setDrawMode("none");
      }
    } else if (drawMode === "polygon") {
      setDraftPoints((prev) => [...prev, pt]);
    }
  };

  const downloadOffline = async () => {
    if (!mapIndex) return;
    const all: Array<[number, number, number]> = [];
    for (const [lvl, list] of Object.entries(mapIndex.tiles)) {
      for (const [tx, ty] of list) all.push([Number(lvl), tx, ty]);
    }
    setOfflineStatus(`Downloading 0 / ${all.length} tiles…`);
    let done = 0;
    for (const [lvl, tx, ty] of all) {
      const tile = await horusApi.getMapTile(token, lvl, tx, ty);
      await putCachedTile(`${mapIndex.version}:${lvl}:${tx}:${ty}`, tile);
      done += 1;
      setOfflineStatus(`Cached ${done} / ${all.length} tiles`);
    }
  };

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="relative min-h-[320px] flex-1 overflow-hidden bg-canvas lg:min-h-0">
        <canvas
          ref={canvasRef}
          className="block h-full min-h-[320px] w-full cursor-crosshair lg:min-h-0"
          onClick={handleCanvasClick}
          onMouseDown={(ev) => {
            if (drawMode !== "none") return;
            dragRef.current = { x: ev.clientX, y: ev.clientY, cn: center[0], ce: center[1] };
          }}
          onMouseMove={(ev) => {
            if (!dragRef.current) return;
            setFollow(false);
            const dx = ev.clientX - dragRef.current.x;
            const dy = ev.clientY - dragRef.current.y;
            setCenter([dragRef.current.cn + dy / pxPerM, dragRef.current.ce - dx / pxPerM]);
          }}
          onMouseUp={() => {
            dragRef.current = null;
          }}
          onMouseLeave={() => {
            dragRef.current = null;
          }}
          onWheel={(ev) => {
            ev.preventDefault();
            setPxPerM((p) => Math.max(2, Math.min(80, p * (ev.deltaY < 0 ? 1.15 : 0.87))));
          }}
        />
        <div className="absolute top-3 right-3 flex items-center gap-1 rounded-[8px] border border-border bg-surface p-1">
          <Button size="sm" variant="ghost" aria-label="Zoom in" onClick={() => setPxPerM((p) => Math.min(80, p * 1.25))}>
            <Plus size={14} />
          </Button>
          <Button size="sm" variant="ghost" aria-label="Zoom out" onClick={() => setPxPerM((p) => Math.max(2, p * 0.8))}>
            <Minus size={14} />
          </Button>
          <Button size="sm" variant={follow ? "primary" : "ghost"} onClick={() => setFollow((v) => !v)}>
            <Crosshair size={13} /> {follow ? "Follow Drone" : "Free Pan"}
          </Button>
        </div>
      </div>

      {drawMode === "polygon" && (
        <div className="flex shrink-0 items-center justify-between gap-2 border-t border-border bg-raised px-4 py-2.5 text-[13.5px]">
          <span className="text-secondary">Click vertices on map ({draftPoints.length} placed)</span>
          <div className="flex gap-2">
            <Button
              size="sm"
              variant="primary"
              disabled={draftPoints.length < 3}
              onClick={() => {
                setSelection({ kind: "area", label: "A1 Area", points: draftPoints });
                setDraftPoints([]);
                setDrawMode("none");
              }}
            >
              Done
            </Button>
            <Button
              size="sm"
              onClick={() => {
                setDraftPoints([]);
                setDrawMode("none");
              }}
            >
              Cancel
            </Button>
          </div>
        </div>
      )}

      {selection && (
        <div className="flex shrink-0 flex-wrap items-center justify-between gap-2 border-t border-border bg-raised px-4 py-2.5 text-[13.5px]">
          <span className="font-medium text-primary">{selection.label}</span>
          <div className="flex flex-wrap gap-2">
            {selection.kind === "point" && (
              <Button size="sm" variant="primary" onClick={() => onFlyTo(selection.point[0], selection.point[1])}>
                Fly here
              </Button>
            )}
            <Button size="sm" onClick={() => onUseInMission(selection)}>
              {selection.kind === "area" ? "Search area" : "Use in mission"}
            </Button>
            <Button size="sm" variant="ghost" onClick={() => setSelection(null)}>
              Clear
            </Button>
          </div>
        </div>
      )}

      <div className="flex shrink-0 flex-wrap items-center justify-between gap-2 border-t border-border bg-surface px-4 py-2.5">
        <span className="text-[12.5px] text-secondary">
          {mapIndex ? `${mapIndex.provider}` : "Own-map telemetry"}
          {offlineStatus ? ` · ${offlineStatus}` : ""}
        </span>
        <div className="flex flex-wrap items-center gap-1.5">
          <Button
            size="sm"
            variant={drawMode === "point" ? "primary" : "secondary"}
            onClick={() => {
              setDrawMode("point");
              setDraftPoints([]);
            }}
          >
            <MapPin size={13} /> Point
          </Button>
          <Button
            size="sm"
            variant={drawMode === "polygon" ? "primary" : "secondary"}
            onClick={() => {
              setDrawMode("polygon");
              setDraftPoints([]);
            }}
          >
            <Pentagon size={13} /> Area
          </Button>
          <Button
            size="sm"
            variant={drawMode === "rect" ? "primary" : "secondary"}
            onClick={() => {
              setDrawMode("rect");
              setDraftPoints([]);
            }}
          >
            <Square size={13} /> Box
          </Button>
          <Button size="sm" variant="secondary" onClick={() => void downloadOffline()} disabled={!mapIndex}>
            <Database size={13} /> Cache Offline
          </Button>
          <Button
            size="sm"
            variant="ghost"
            aria-label="Clear tile cache"
            onClick={() => {
              void clearTileCache();
              setOfflineStatus("Cache cleared");
            }}
          >
            <Trash2 size={13} />
          </Button>
        </div>
      </div>
    </div>
  );
}
