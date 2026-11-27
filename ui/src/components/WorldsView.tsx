import { Download, Play } from "lucide-react";
import { useEffect, useState } from "react";
import { horusApi } from "../api";
import type { CapabilityItem, SerialPortInfo, StudioStatus } from "../types";
import { BuiltinScenesGrid } from "./SceneGrid";
import { SceneVisual } from "./SceneVisual";
import { Button, StopSessionButton } from "./common/Button";
import { CapabilityRequirements } from "./common/CapabilityRequirements";
import { CopyCommand } from "./common/CopyCommand";
import { Field, Input, PageHeader, Section, Select } from "./common/Surface";

interface Props {
  token: string;
  studioStatus: StudioStatus | null;
  caps: CapabilityItem[];
  bootstrapping: boolean;
  stoppingSession?: boolean;
  onBootstrap: () => Promise<void>;
  onStartSession: (payload: Record<string, unknown>) => Promise<void>;
  onStopSession: () => Promise<void>;
}

export function WorldsView({
  token,
  studioStatus,
  caps,
  bootstrapping,
  stoppingSession = false,
  onBootstrap,
  onStartSession,
  onStopSession,
}: Props) {
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
  const [launchingKey, setLaunchingKey] = useState<string | null>(null);

  useEffect(() => {
    horusApi
      .getSerialPorts(token)
      .then((r) => {
        setPorts(r.ports);
        if (r.ports.length && !serialDev) setSerialDev(r.ports[0].device);
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

  const builtinCap = caps.find((c) => c.id === "builtin_3d");
  const gazeboCap = caps.find((c) => c.id === "gazebo");
  const gtaCap = caps.find((c) => c.id === "gta");
  const realCap = caps.find((c) => c.id === "real_drone");

  return (
    <div className="mx-auto flex w-full max-w-5xl flex-col gap-9 px-5 py-7 sm:px-6 sm:py-8">
      <PageHeader
        title="Simulators & Drones"
        description="Choose where HORUS flies: a built-in 3D simulator, an external Gazebo or GTA V bridge, or physical ArduPilot hardware."
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
        description="Zero-setup MuJoCo physics + ArduCopter 4.7.1 SITL with 7-beam ToF obstacle avoidance, camera gimbal, and AprilTag landing pad."
      >
        <div className="flex flex-col gap-5">
          {builtinCap && (
            <CapabilityRequirements
              cap={builtinCap}
              mode="inline"
              onBootstrap={onBootstrap}
              bootstrapping={bootstrapping}
            />
          )}

          <BuiltinScenesGrid
            scenes={studioStatus?.scenes || []}
            activeSceneName={studioStatus?.scene}
            running={running}
            busy={busy}
            launchingSceneName={launchingKey?.startsWith("scene:") ? launchingKey.slice(6) : null}
            onLaunchScene={(sc, seed) => void launch(`scene:${sc.name}`, { mode: "builtin_sim", scene: sc.name, seed })}
          />
        </div>
      </Section>

      <Section
        title="External Simulators"
        description="Connect HORUS to a Gazebo Harmonic Docker world or a GTA V city instance. Click Launch to start locally, or Attach Existing to connect to one already running (e.g. on Koko)."
      >
        <div className="grid gap-5 sm:grid-cols-2">
          <article className="group flex flex-col overflow-hidden rounded-[12px] border border-border bg-surface">
            <SceneVisual variant="gazebo" tag="ROS 2 / Harmonic" active={running && studioStatus?.mode === "gazebo"} />
            <div className="flex flex-1 flex-col justify-between gap-4 p-5">
              <div className="flex flex-col gap-3">
                <div>
                  <h3 className="text-[16px] font-semibold tracking-tight text-primary">Gazebo Harmonic</h3>
                  <p className="mt-1 text-[13.5px] text-secondary">
                    High-fidelity ROS 2 / Gazebo world via Docker &amp; <code className="font-mono text-primary">sim/run.sh</code>.
                  </p>
                </div>

                {gazeboCap && <CapabilityRequirements cap={gazeboCap} mode="inline" />}

                <Field label="World" hint="preset Gazebo arena">
                  <Select value={gzWorld} onChange={(e) => setGzWorld(e.target.value)} disabled={gzProc}>
                    <option value="horus_indoor">horus_indoor (Room)</option>
                    <option value="horus_forest">horus_forest (Trees)</option>
                    <option value="horus_urban">horus_urban (Street)</option>
                  </Select>
                </Field>

                <div className="flex flex-wrap items-center gap-5 text-[13.5px] text-primary">
                  <label className="inline-flex cursor-pointer items-center gap-2">
                    <input type="checkbox" checked={gzGui} onChange={() => setGzGui((v) => !v)} />
                    Show 3D window
                  </label>
                  <label className="inline-flex cursor-pointer items-center gap-2">
                    <input type="checkbox" checked={gzProc} onChange={() => setGzProc((v) => !v)} />
                    Procedural layout
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
                    <Field label="Seed" hint="layout number">
                      <Input type="number" value={gzSeed} onChange={(e) => setGzSeed(Number(e.target.value))} />
                    </Field>
                  </div>
                )}

                <details className="rounded-[8px] border border-border bg-canvas px-3 py-2 text-[12.5px] text-secondary">
                  <summary className="cursor-pointer select-none font-medium text-primary">
                    Setup guide (Docker)
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

                {gtaCap && <CapabilityRequirements cap={gtaCap} mode="inline" />}

                <Field label="GTA V Host Address" hint="PC running HorusGta.dll (UDP 9002)">
                  <Input value={gtaHost} onChange={(e) => setGtaHost(e.target.value)} />
                </Field>

                <details className="rounded-[8px] border border-border bg-canvas px-3 py-2 text-[12.5px] text-secondary">
                  <summary className="cursor-pointer select-none font-medium text-primary">
                    Setup guide (Windows + GTA V)
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
        title="Physical Drones"
        description="Fly real ArduPilot hardware attached over USB/telemetry radio, or pair with an onboard companion computer running HORUS."
      >
        <div className="flex flex-col gap-5">
          {realCap && <CapabilityRequirements cap={realCap} mode="inline" />}

          <div className="grid gap-5 sm:grid-cols-2">
            <article className="group flex flex-col overflow-hidden rounded-[12px] border border-border bg-surface">
              <SceneVisual variant="direct_usb" tag="MAVLink USB" />
              <div className="flex flex-1 flex-col justify-between gap-4 p-5">
                <div className="flex flex-col gap-3">
                  <div>
                    <h3 className="text-[16px] font-semibold tracking-tight text-primary">Direct USB / Radio</h3>
                    <p className="mt-1 text-[13.5px] text-secondary">
                      Connect a Pixhawk or Cube flight controller plugged directly into this computer.
                    </p>
                  </div>
                  <div className="grid gap-3 sm:grid-cols-2">
                    <Field label="Serial / UDP Endpoint" hint="port or udp:...">
                      <Input
                        value={serialDev}
                        onChange={(e) => setSerialDev(e.target.value)}
                        placeholder={ports[0]?.device || "/dev/ttyACM0"}
                      />
                    </Field>
                    <Field label="Baud Rate" hint="57600 radio · 115200 USB">
                      <Input type="number" value={baud} onChange={(e) => setBaud(Number(e.target.value))} />
                    </Field>
                  </div>
                  <Field label="Camera Source" hint="optional, webcam index or RTSP URL">
                    <Input value={camSource} onChange={(e) => setCamSource(e.target.value)} placeholder="0 or rtsp://..." />
                  </Field>
                </div>

                <div className="flex flex-wrap items-center justify-between gap-2 border-t border-border pt-4">
                  <span className="text-[12.5px] text-muted">
                    Detected ports: {ports.map((p) => p.device).join(", ") || "none"}
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
                      Pair with an onboard Raspberry Pi, Jetson, or remote host over Tailscale or SSH.
                    </p>
                  </div>
                  <Field label="Drone Board MCP URL" hint="http://<host>:8000/mcp">
                    <Input value={remoteUrl} onChange={(e) => setRemoteUrl(e.target.value)} />
                  </Field>
                  <Field label="MCP Bearer Token" hint="printed by horus on the board">
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
          </div>
        </div>
      </Section>
    </div>
  );
}
