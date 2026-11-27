import { Download, Play } from "lucide-react";
import { useEffect, useState } from "react";
import { horusApi } from "../api";
import type { GrounderStatus, SerialPortInfo, StudioStatus } from "../types";
import { SceneVisual, type SceneVisualVariant } from "./SceneVisual";
import { Button, StopSessionButton } from "./common/Button";
import { CopyCommand } from "./common/CopyCommand";
import { Field, Input, PageHeader, Section, Select, StatusBadge } from "./common/Surface";
import { useToast } from "./common/Toast";

interface Props {
  token: string;
  studioStatus: StudioStatus | null;
  bootstrapping: boolean;
  stoppingSession?: boolean;
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
  stoppingSession = false,
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
  const [grounderBackend, setGrounderBackend] = useState("auto");
  const [launchingKey, setLaunchingKey] = useState<string | null>(null);
  const [grounderBusy, setGrounderBusy] = useState<"start" | "stop" | null>(null);
  const toast = useToast();

  useEffect(() => {
    horusApi
      .getSerialPorts(token)
      .then((r) => {
        setPorts(r.ports);
        if (r.ports.length && !serialDev) setSerialDev(r.ports[0].device);
      })
      .catch(() => {});
    horusApi.getGrounder(token).then(setGrounder).catch(() => {});
    horusApi
      .getStudioSettings(token)
      .then((v) => {
        if (v.settings.grounder_backend) setGrounderBackend(v.settings.grounder_backend);
      })
      .catch(() => {});
  }, [token]);

  const running = studioStatus?.lifecycle === "running";
  const busy = studioStatus?.lifecycle === "starting" || studioStatus?.lifecycle === "bootstrapping" || launchingKey !== null;

  const launch = async (key: string, payload: Record<string, unknown>) => {
    setLaunchingKey(key);
    try {
      await onStartSession(payload);
    } finally {
      setLaunchingKey(null);
    }
  };

  const handleGrounder = async (action: "start" | "stop") => {
    setGrounderBusy(action);
    try {
      const next =
        action === "start" ? await horusApi.startGrounder(token, grounderBackend) : await horusApi.stopGrounder(token);
      setGrounder(next);
      toast.success(action === "start" ? "Grounder service started" : "Grounder service stopped");
    } catch (e: unknown) {
      toast.error(
        `Grounder ${action} failed`,
        e instanceof Error ? e.message : String(e),
      );
    } finally {
      setGrounderBusy(null);
    }
  };

  return (
    <div className="mx-auto flex w-full max-w-5xl flex-col gap-9 px-5 py-7 sm:px-6 sm:py-8">
      <PageHeader
        title="Simulators & Drones"
        description="Pick a built-in 3D environment, connect an external simulator, or attach physical ArduPilot hardware."
        action={
          <div className="flex flex-wrap items-center gap-2">
            {studioStatus && !studioStatus.assets_ready && (
              <Button variant="primary" loading={bootstrapping} onClick={() => void onBootstrap()}>
                {!bootstrapping && <Download size={15} />}
                {bootstrapping ? "Downloading…" : "Download SITL & Models"}
              </Button>
            )}
            {running && <StopSessionButton size="md" loading={stoppingSession} onClick={() => void onStopSession()} />}
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
                      loading={launchingKey === `scene:${sc.name}`}
                      onClick={() => void launch(`scene:${sc.name}`, { mode: "builtin_sim", scene: sc.name, seed })}
                    >
                      {launchingKey !== `scene:${sc.name}` && <Play size={13} />}
                      {launchingKey === `scene:${sc.name}` ? "Launching…" : isCurrent ? "Restart" : "Launch"}
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

                <details className="rounded-[8px] border border-border bg-canvas px-3 py-2 text-[12.5px] text-secondary">
                  <summary className="cursor-pointer select-none font-medium text-primary">
                    Optional setup guide (Docker)
                  </summary>
                  <div className="mt-2 flex flex-col gap-2">
                    <p>1. Install Docker Engine, then build the Gazebo Harmonic image from the repo checkout:</p>
                    <CopyCommand command="./sim/run.sh build" label="Gazebo Docker build" />
                    <p className="text-muted">Or use Attach Existing to connect to a remote Koko Gazebo instance.</p>
                  </div>
                </details>
              </div>

              <div className="flex flex-wrap gap-2 border-t border-border pt-4">
                <Button
                  size="sm"
                  variant="primary"
                  disabled={busy}
                  loading={launchingKey === "gz:launch"}
                  onClick={() =>
                    void launch("gz:launch", {
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
                  {launchingKey !== "gz:launch" && <Play size={13} />}
                  {launchingKey === "gz:launch" ? "Launching…" : "Launch Gazebo"}
                </Button>
                <Button
                  size="sm"
                  disabled={busy}
                  loading={launchingKey === "gz:attach"}
                  onClick={() =>
                    void launch("gz:attach", { mode: "gazebo", action: "attach", mcp_url: remoteUrl, mcp_token: remoteToken })
                  }
                >
                  {launchingKey === "gz:attach" ? "Attaching…" : "Attach Existing"}
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

                <details className="rounded-[8px] border border-border bg-canvas px-3 py-2 text-[12.5px] text-secondary">
                  <summary className="cursor-pointer select-none font-medium text-primary">
                    Optional setup guide (Windows + GTA V)
                  </summary>
                  <div className="mt-2 flex flex-col gap-2">
                    <p>1. Install <code className="font-mono text-primary">HorusGta.dll</code> &amp; <code className="font-mono text-primary">horus_capture.asi</code> from <code className="font-mono text-primary">gta/horus_mod</code> into GTA V.</p>
                    <p>2. Ensure ArduCopter SITL is downloaded (native on Windows, or Docker on Linux):</p>
                    <CopyCommand command="horus setup" label="horus setup" />
                    <p className="text-muted">Optional full Los Santos map tiles: <code className="font-mono">python scripts/maps/build_gta_tiles.py</code></p>
                  </div>
                </details>
              </div>

              <div className="flex flex-wrap gap-2 border-t border-border pt-4">
                <Button
                  size="sm"
                  variant="primary"
                  disabled={busy}
                  loading={launchingKey === "gta:launch"}
                  onClick={() => void launch("gta:launch", { mode: "gta", action: "launch", gta_host: gtaHost })}
                >
                  {launchingKey !== "gta:launch" && <Play size={13} />}
                  {launchingKey === "gta:launch" ? "Launching…" : "Launch GTA V"}
                </Button>
                <Button
                  size="sm"
                  disabled={busy}
                  loading={launchingKey === "gta:attach"}
                  onClick={() =>
                    void launch("gta:attach", { mode: "gta", action: "attach", mcp_url: remoteUrl, mcp_token: remoteToken })
                  }
                >
                  {launchingKey === "gta:attach" ? "Attaching…" : "Attach Existing"}
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
                  loading={launchingKey === "real:direct"}
                  onClick={() =>
                    void launch("real:direct", {
                      mode: "real_drone",
                      submode: "direct",
                      mavlink_connection: serialDev,
                      baud,
                      camera_source: camSource || null,
                    })
                  }
                >
                  {launchingKey !== "real:direct" && <Play size={13} />}
                  {launchingKey === "real:direct" ? "Connecting…" : "Connect Controller"}
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
                  loading={launchingKey === "real:remote"}
                  onClick={() =>
                    void launch("real:remote", {
                      mode: "real_drone",
                      submode: "remote",
                      mcp_url: remoteUrl,
                      mcp_token: remoteToken,
                    })
                  }
                >
                  {launchingKey !== "real:remote" && <Play size={13} />}
                  {launchingKey === "real:remote" ? "Connecting…" : "Connect Companion"}
                </Button>
              </div>
            </div>
          </article>

          <article className="group flex flex-col overflow-hidden rounded-[12px] border border-border bg-surface sm:col-span-2">
            <SceneVisual variant="grounder" tag="Open-Vocab VLM" active={Boolean(grounder?.running)} />
            <div className="flex flex-col gap-3 p-5">
              <div className="flex flex-wrap items-center justify-between gap-4">
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-2.5">
                    <h3 className="text-[16px] font-semibold tracking-tight text-primary">Visual Grounder (`horus-grounder`)</h3>
                    <StatusBadge tone={grounder?.running ? "ok" : "neutral"}>
                      {grounder?.running ? `Running (PID ${grounder.pid}${grounder.backend ? ` · ${grounder.backend}` : ""})` : "Stopped"}
                    </StatusBadge>
                  </div>
                  <p className="mt-1 text-[13.5px] text-secondary">
                    Runs zero-install Cloud VLM or optional local PyTorch grounding on{" "}
                    <span className="font-mono text-primary">{grounder?.url || "http://127.0.0.1:8100"}</span> for arbitrary text targets.
                  </p>
                </div>
                <div className="flex shrink-0 flex-wrap items-center gap-2">
                  <Select
                    aria-label="Grounder backend"
                    value={grounderBackend}
                    onChange={(e) => {
                      const nextBackend = e.target.value;
                      setGrounderBackend(nextBackend);
                      void horusApi
                        .putStudioSettings(token, { grounder_backend: nextBackend })
                        .then(() => toast.info("Default grounder backend saved", nextBackend))
                        .catch(() => {});
                    }}
                    className="!h-9 !w-auto !text-[13px]"
                  >
                    <option value="auto">Auto (Cloud VLM or Local)</option>
                    <option value="cloud-vlm">Cloud VLM (Zero Install)</option>
                    <option value="grounding-dino-tiny">Grounding DINO (Local PyTorch)</option>
                    <option value="owlv2-base-patch16-ensemble">OWLv2 (Local PyTorch)</option>
                  </Select>
                  <Button
                    size="sm"
                    variant="primary"
                    loading={grounderBusy === "start"}
                    disabled={grounderBusy !== null}
                    onClick={() => void handleGrounder("start")}
                  >
                    {grounderBusy === "start" ? "Starting…" : "Start Service"}
                  </Button>
                  <Button
                    size="sm"
                    loading={grounderBusy === "stop"}
                    disabled={grounderBusy !== null}
                    onClick={() => void handleGrounder("stop")}
                  >
                    {grounderBusy === "stop" ? "Stopping…" : "Stop"}
                  </Button>
                </div>
              </div>

              <details className="rounded-[8px] border border-border bg-canvas px-3 py-2 text-[12.5px] text-secondary">
                <summary className="cursor-pointer select-none font-medium text-primary">
                  How to choose &amp; set up Grounder backends
                </summary>
                <div className="mt-2 grid gap-3 sm:grid-cols-2">
                  <div className="flex flex-col gap-1.5">
                    <span className="font-medium text-primary">Cloud VLM (Zero Install)</span>
                    <span>Uses your existing Gemini, OpenAI, or OpenRouter key from AI &amp; MCP — 0 MB local PyTorch install.</span>
                  </div>
                  <div className="flex flex-col gap-1.5">
                    <span className="font-medium text-primary">Local PyTorch (Offline)</span>
                    <span>Install optional Torch/Transformers weights only if you need offline grounding:</span>
                    <CopyCommand command='pip install -e ".[grounder]"' label="grounder extra" />
                  </div>
                </div>
              </details>
            </div>
          </article>
        </div>
      </Section>
    </div>
  );
}
