import { Download, Play } from "lucide-react";
import { useEffect, useState } from "react";
import { horusApi } from "../api";
import type { GrounderStatus, SerialPortInfo, StudioStatus } from "../types";
import { SceneVisual, type SceneVisualVariant } from "./SceneVisual";
import { Button, StopSessionButton } from "./common/Button";
import { Field, Input, PageHeader, Section, Select, StatusBadge } from "./common/Surface";

interface Props {
  token: string;
  studioStatus: StudioStatus | null;
  bootstrapping: boolean;
  onBootstrap: () => Promise<void>;
  onStartSession: (payload: Record<string, unknown>) => Promise<void>;
  onStopSession: () => Promise<void>;
}

const SCENE_TAG: Record<string, { variant: SceneVisualVariant; tag: string }> = {
  indoor_sar: { variant: "indoor_sar", tag: "Indoor" },
  forest_sar: { variant: "forest_sar", tag: "Outdoor" },
  urban_plaza: { variant: "urban_plaza", tag: "Outdoor" },
  procedural: { variant: "procedural", tag: "Seeded" },
};

export function WorldsView({
  token,
  studioStatus,
  bootstrapping,
  onBootstrap,
  onStartSession,
  onStopSession,
}: Props) {
  const [seed, setSeed] = useState(42);
  const [gzWorld, setGzWorld] = useState("horus_indoor");
  const [gzGui, setGzGui] = useState(false);
  const [gzProc, setGzProc] = useState(false);
  const [gzKind, setGzKind] = useState<"forest" | "urban">("forest");
  const [gzSeed, setGzSeed] = useState(42);
  const [gtaHost, setGtaHost] = useState("127.0.0.1");
  const [serialDev, setSerialDev] = useState("");
  const [baud, setBaud] = useState(115200);
  const [camSource, setCamSource] = useState("");
  const [remoteUrl, setRemoteUrl] = useState("http://127.0.0.1:8000/mcp");
  const [remoteToken, setRemoteToken] = useState("");
  const [ports, setPorts] = useState<SerialPortInfo[]>([]);
  const [grounder, setGrounder] = useState<GrounderStatus | null>(null);

  useEffect(() => {
    horusApi
      .getSerialPorts(token)
      .then((r) => {
        setPorts(r.ports);
        if (r.ports.length && !serialDev) setSerialDev(r.ports[0].device);
      })
      .catch(() => {});
    horusApi.getGrounder(token).then(setGrounder).catch(() => {});
  }, [token]);

  const running = studioStatus?.lifecycle === "running";
  const busy = studioStatus?.lifecycle === "starting" || studioStatus?.lifecycle === "bootstrapping";

  return (
    <div className="mx-auto flex w-full max-w-5xl flex-col gap-9 px-5 py-7 sm:px-6 sm:py-8">
      <PageHeader
        title="Simulators & Drones"
        description="Pick a built-in 3D environment, connect an external simulator, or attach physical ArduPilot hardware."
        action={
          <div className="flex flex-wrap items-center gap-2">
            {studioStatus && !studioStatus.assets_ready && (
              <Button variant="primary" disabled={bootstrapping} onClick={() => void onBootstrap()}>
                <Download size={15} /> {bootstrapping ? "Downloading…" : "Download SITL & Models"}
              </Button>
            )}
            {running && <StopSessionButton size="md" onClick={() => void onStopSession()} />}
          </div>
        }
      />

      <Section
        title="Built-in 3D Worlds"
        description="Self-contained MuJoCo physics + ArduCopter 4.7.1 SITL with 7-beam ToF avoidance, gimbal, and AprilTag pad."
      >
        <div className="grid gap-5 sm:grid-cols-2">
          {(studioStatus?.scenes || []).map((sc) => {
            const isCurrent = running && studioStatus?.scene === sc.name;
            const visual = SCENE_TAG[sc.name] || { variant: "procedural" as const, tag: "World" };
            return (
              <article
                key={sc.name}
                className="group flex flex-col overflow-hidden rounded-[12px] border border-border bg-surface transition-colors hover:border-border-strong"
              >
                <SceneVisual variant={visual.variant} tag={visual.tag} active={isCurrent} />

                <div className="flex flex-1 flex-col justify-between gap-4 p-5">
                  <div>
                    <div className="flex items-center justify-between gap-2">
                      <h3 className="text-[16px] font-semibold tracking-tight text-primary">{sc.title}</h3>
                      {isCurrent && <StatusBadge tone="ok">Running</StatusBadge>}
                    </div>
                    <p className="mt-1.5 text-[13.5px] leading-relaxed text-secondary">{sc.subtitle}</p>
                  </div>

                  <div className="flex flex-wrap items-center justify-between gap-3 border-t border-border pt-4">
                    {sc.name === "procedural" ? (
                      <label className="flex items-center gap-2 text-[13px] text-secondary">
                        Seed
                        <Input
                          type="number"
                          aria-label="Procedural seed"
                          value={seed}
                          onChange={(e) => setSeed(Number(e.target.value))}
                          className="!h-9 !w-24 tabular-nums"
                        />
                      </label>
                    ) : (
                      <span className="font-mono text-[12px] text-muted">{sc.name}</span>
                    )}

                    <Button
                      size="sm"
                      variant={isCurrent ? "secondary" : "primary"}
                      disabled={busy}
                      onClick={() => void onStartSession({ mode: "builtin_sim", scene: sc.name, seed })}
                    >
                      <Play size={13} /> {isCurrent ? "Restart" : "Launch"}
                    </Button>
                  </div>
                </div>
              </article>
            );
          })}
        </div>
      </Section>

      <Section
        title="External Simulators"
        description="Launch managed Gazebo Harmonic or GTA V bridges, or attach to an already-running MCP endpoint."
      >
        <div className="grid gap-5 sm:grid-cols-2">
          <article className="group flex flex-col overflow-hidden rounded-[12px] border border-border bg-surface">
            <SceneVisual variant="gazebo" tag="ROS 2 / Harmonic" active={running && studioStatus?.mode === "gazebo"} />
            <div className="flex flex-1 flex-col justify-between gap-4 p-5">
              <div className="flex flex-col gap-3">
                <div>
                  <h3 className="text-[16px] font-semibold tracking-tight text-primary">Gazebo Harmonic</h3>
                  <p className="mt-1 text-[13.5px] text-secondary">
                    High-fidelity ROS 2 / Gazebo world via Docker &amp; `sim/run.sh`.
                  </p>
                </div>

                <Field label="World">
                  <Select value={gzWorld} onChange={(e) => setGzWorld(e.target.value)} disabled={gzProc}>
                    <option value="horus_indoor">horus_indoor (Room)</option>
                    <option value="horus_forest">horus_forest (Trees)</option>
                    <option value="horus_urban">horus_urban (Street)</option>
                  </Select>
                </Field>

                <div className="flex items-center gap-5 text-[13.5px] text-primary">
                  <label className="inline-flex cursor-pointer items-center gap-2">
                    <input type="checkbox" checked={gzGui} onChange={() => setGzGui((v) => !v)} />
                    Show GUI
                  </label>
                  <label className="inline-flex cursor-pointer items-center gap-2">
                    <input type="checkbox" checked={gzProc} onChange={() => setGzProc((v) => !v)} />
                    Procedural
                  </label>
                </div>

                {gzProc && (
                  <div className="grid grid-cols-2 gap-3">
                    <Field label="Environment">
                      <Select value={gzKind} onChange={(e) => setGzKind(e.target.value as "forest" | "urban")}>
                        <option value="forest">Forest</option>
                        <option value="urban">Urban</option>
                      </Select>
                    </Field>
                    <Field label="Seed">
                      <Input type="number" value={gzSeed} onChange={(e) => setGzSeed(Number(e.target.value))} />
                    </Field>
                  </div>
                )}
              </div>

              <div className="flex flex-wrap gap-2 border-t border-border pt-4">
                <Button
                  size="sm"
                  variant="primary"
                  disabled={busy}
                  onClick={() =>
                    void onStartSession({
                      mode: "gazebo",
                      action: "launch",
                      world: gzWorld,
                      gui: gzGui,
                      procedural: gzProc,
                      kind: gzKind,
                      seed: gzSeed,
                    })
                  }
                >
                  <Play size={13} /> Launch Gazebo
                </Button>
                <Button
                  size="sm"
                  disabled={busy}
                  onClick={() =>
                    void onStartSession({ mode: "gazebo", action: "attach", mcp_url: remoteUrl, mcp_token: remoteToken })
                  }
                >
                  Attach Existing
                </Button>
              </div>
            </div>
          </article>

          <article className="group flex flex-col overflow-hidden rounded-[12px] border border-border bg-surface">
            <SceneVisual variant="gta" tag="HorusGta.dll" active={running && studioStatus?.mode === "gta"} />
            <div className="flex flex-1 flex-col justify-between gap-4 p-5">
              <div className="flex flex-col gap-3">
                <div>
                  <h3 className="text-[16px] font-semibold tracking-tight text-primary">GTA V Bridge</h3>
                  <p className="mt-1 text-[13.5px] leading-relaxed text-secondary">
                    Photorealistic city flight with physical collisions, 7-beam ToF, and live video via HorusGta.dll.
                  </p>
                </div>

                <Field label="GTA V Host Address" hint="UDP 9002">
                  <Input value={gtaHost} onChange={(e) => setGtaHost(e.target.value)} />
                </Field>
              </div>

              <div className="flex flex-wrap gap-2 border-t border-border pt-4">
                <Button
                  size="sm"
                  variant="primary"
                  disabled={busy}
                  onClick={() => void onStartSession({ mode: "gta", action: "launch", gta_host: gtaHost })}
                >
                  <Play size={13} /> Launch GTA V
                </Button>
                <Button
                  size="sm"
                  disabled={busy}
                  onClick={() =>
                    void onStartSession({ mode: "gta", action: "attach", mcp_url: remoteUrl, mcp_token: remoteToken })
                  }
                >
                  Attach Existing
                </Button>
              </div>
            </div>
          </article>
        </div>
      </Section>

      <Section
        title="Physical Drones & Vision Services"
        description="Attach directly over USB/telemetry radio, connect a remote companion board, or manage open-vocab grounding."
      >
        <div className="grid gap-5 sm:grid-cols-2">
          <article className="group flex flex-col overflow-hidden rounded-[12px] border border-border bg-surface">
            <SceneVisual variant="direct_usb" tag="MAVLink USB" />
            <div className="flex flex-1 flex-col justify-between gap-4 p-5">
              <div className="flex flex-col gap-3">
                <div>
                  <h3 className="text-[16px] font-semibold tracking-tight text-primary">Direct USB / Radio</h3>
                  <p className="mt-1 text-[13.5px] text-secondary">
                    Connect an ArduPilot/Pixhawk flight controller attached to this machine.
                  </p>
                </div>
                <div className="grid gap-3 sm:grid-cols-2">
                  <Field label="Serial / UDP Endpoint">
                    <Input
                      value={serialDev}
                      onChange={(e) => setSerialDev(e.target.value)}
                      placeholder={ports[0]?.device || "/dev/ttyACM0"}
                    />
                  </Field>
                  <Field label="Baud Rate">
                    <Input type="number" value={baud} onChange={(e) => setBaud(Number(e.target.value))} />
                  </Field>
                </div>
                <Field label="Camera Source" hint="optional">
                  <Input value={camSource} onChange={(e) => setCamSource(e.target.value)} placeholder="0 or rtsp://..." />
                </Field>
              </div>

              <div className="flex flex-wrap items-center justify-between gap-2 border-t border-border pt-4">
                <span className="text-[12.5px] text-muted">
                  Ports: {ports.map((p) => p.device).join(", ") || "none detected"}
                </span>
                <Button
                  size="sm"
                  variant="primary"
                  disabled={busy}
                  onClick={() =>
                    void onStartSession({
                      mode: "real_drone",
                      submode: "direct",
                      mavlink_connection: serialDev,
                      baud,
                      camera_source: camSource || null,
                    })
                  }
                >
                  <Play size={13} /> Connect Controller
                </Button>
              </div>
            </div>
          </article>

          <article className="group flex flex-col overflow-hidden rounded-[12px] border border-border bg-surface">
            <SceneVisual variant="companion_board" tag="horus mcp" />
            <div className="flex flex-1 flex-col justify-between gap-4 p-5">
              <div className="flex flex-col gap-3">
                <div>
                  <h3 className="text-[16px] font-semibold tracking-tight text-primary">Remote Companion Board</h3>
                  <p className="mt-1 text-[13.5px] text-secondary">
                    Pair with an onboard Raspberry Pi, Jetson, or remote host over Tailscale/SSH.
                  </p>
                </div>
                <Field label="Drone Board MCP URL">
                  <Input value={remoteUrl} onChange={(e) => setRemoteUrl(e.target.value)} />
                </Field>
                <Field label="MCP Bearer Token">
                  <Input type="password" value={remoteToken} onChange={(e) => setRemoteToken(e.target.value)} />
                </Field>
              </div>

              <div className="flex justify-end border-t border-border pt-4">
                <Button
                  size="sm"
                  variant="primary"
                  disabled={busy}
                  onClick={() =>
                    void onStartSession({
                      mode: "real_drone",
                      submode: "remote",
                      mcp_url: remoteUrl,
                      mcp_token: remoteToken,
                    })
                  }
                >
                  <Play size={13} /> Connect Companion
                </Button>
              </div>
            </div>
          </article>

          <article className="group flex flex-col overflow-hidden rounded-[12px] border border-border bg-surface sm:col-span-2">
            <SceneVisual variant="grounder" tag="Open-Vocab VLM" active={Boolean(grounder?.running)} />
            <div className="flex flex-wrap items-center justify-between gap-4 p-5">
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-2.5">
                  <h3 className="text-[16px] font-semibold tracking-tight text-primary">Visual Grounder (`horus-grounder`)</h3>
                  <StatusBadge tone={grounder?.running ? "ok" : "neutral"}>
                    {grounder?.running ? `Running (PID ${grounder.pid})` : "Stopped"}
                  </StatusBadge>
                </div>
                <p className="mt-1 text-[13.5px] text-secondary">
                  Runs Moondream / Qwen-VL / Gemini grounding on{" "}
                  <span className="font-mono text-primary">{grounder?.url || "http://127.0.0.1:8100"}</span> for arbitrary text targets.
                </p>
              </div>
              <div className="flex shrink-0 gap-2">
                <Button size="sm" variant="primary" onClick={() => void horusApi.startGrounder(token).then(setGrounder)}>
                  Start Service
                </Button>
                <Button size="sm" onClick={() => void horusApi.stopGrounder(token).then(setGrounder)}>
                  Stop
                </Button>
              </div>
            </div>
          </article>
        </div>
      </Section>
    </div>
  );
}
