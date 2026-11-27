import { ArrowRight, Check, Copy, Download, Globe, QrCode, RefreshCw, Save, Smartphone, Wifi } from "lucide-react";
import { useEffect, useState } from "react";
import { horusApi } from "../api";
import { buildQrMatrix } from "../qr";
import type { CapabilityItem, PhoneStatus, RunLogEntry, StudioStatus } from "../types";
import { Button } from "./common/Button";
import { CopyCommand } from "./common/CopyCommand";
import { Field, Input, PageHeader, Section, Select, SliderField, StatusBadge } from "./common/Surface";
import { useToast } from "./common/Toast";

type NavId = "flight" | "lab" | "worlds" | "ai" | "phone" | "tuning" | "system";

interface Props {
  section: "tuning" | "phone" | "system";
  token: string;
  studioStatus: StudioStatus | null;
  caps: CapabilityItem[];
  bootstrapping?: boolean;
  onBootstrap?: () => Promise<void>;
  onRefreshCaps: (force: boolean) => Promise<CapabilityItem[]>;
  onSaved: () => void;
  onNavigate?: (id: NavId) => void;
}

export function SystemPhoneView({
  section,
  token,
  studioStatus,
  caps,
  bootstrapping = false,
  onBootstrap,
  onRefreshCaps,
  onSaved,
  onNavigate,
}: Props) {
  const [flightSpeed, setFlightSpeed] = useState(1.2);
  const [rthDelay, setRthDelay] = useState(30.0);
  const [gimbalPitch, setGimbalPitch] = useState(-15.0);
  const [maxFps, setMaxFps] = useState(5.0);
  const [openVocabHz, setOpenVocabHz] = useState(0.5);
  const [grounderUrl, setGrounderUrl] = useState("http://127.0.0.1:8100");
  const [grounderBackend, setGrounderBackend] = useState("auto");
  const [saveNotice, setSaveNotice] = useState("");
  const [savingTuning, setSavingTuning] = useState(false);
  const [checkingCaps, setCheckingCaps] = useState(false);
  const [phoneBusy, setPhoneBusy] = useState<"start" | "stop" | null>(null);
  const [phoneMode, setPhoneMode] = useState<"lan" | "tailscale">("lan");
  const [phone, setPhone] = useState<PhoneStatus | null>(null);
  const [runs, setRuns] = useState<RunLogEntry[]>([]);
  const [copiedUrl, setCopiedUrl] = useState(false);
  const toast = useToast();

  useEffect(() => {
    horusApi
      .getStudioSettings(token)
      .then((v) => {
        setFlightSpeed(v.settings.flight_speed_m_s ?? 1.2);
        setRthDelay(v.settings.link_loss_rth_delay_s ?? 30.0);
        setGimbalPitch(v.settings.default_gimbal_pitch_deg ?? -15.0);
        setMaxFps(v.settings.camera_max_fps ?? 5.0);
        setOpenVocabHz(v.settings.open_vocab_hz ?? 0.5);
        setGrounderUrl(v.settings.grounder_url ?? "http://127.0.0.1:8100");
        setGrounderBackend(v.settings.grounder_backend ?? "auto");
      })
      .catch(() => {});
    if (section === "phone") {
      horusApi
        .getPhoneStatus(token)
        .then((p) => {
          setPhone(p);
          if (p.serve_active) setPhoneMode("tailscale");
        })
        .catch(() => {});
    }
    if (section === "system") {
      horusApi.getRuns(token).then((r) => setRuns(r.runs)).catch(() => {});
    }
  }, [section, token]);

  const saveTuning = async () => {
    setSavingTuning(true);
    try {
      await horusApi.putStudioSettings(token, {
        flight_speed_m_s: flightSpeed,
        link_loss_rth_delay_s: rthDelay,
        default_gimbal_pitch_deg: gimbalPitch,
        camera_max_fps: maxFps,
        open_vocab_hz: openVocabHz,
        grounder_url: grounderUrl,
        grounder_backend: grounderBackend,
      });
      onSaved();
      setSaveNotice("Saved. Restart an active session to apply.");
      toast.success("Flight settings saved", "Restart an active session to apply changes.");
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : String(e);
      setSaveNotice(`Save failed: ${msg}`);
      toast.error("Failed to save flight settings", msg);
    } finally {
      setSavingTuning(false);
    }
  };

  const handleRecheckCaps = async () => {
    setCheckingCaps(true);
    try {
      const latest = await onRefreshCaps(true);
      const core = latest.filter((c) => c.tier !== "optional");
      const readyCount = core.filter((c) => c.ready).length;
      toast.success("System checks updated", `Core: ${readyCount}/${core.length} ready`);
    } catch (e: unknown) {
      toast.error("System check failed", e instanceof Error ? e.message : String(e));
    } finally {
      setCheckingCaps(false);
    }
  };

  const handlePhoneServe = async (action: "start" | "stop") => {
    setPhoneBusy(action);
    try {
      const next = await horusApi.phoneServe(token, action);
      setPhone(next);
      setSaveNotice("");
      toast.success(
        action === "start" ? "Tailscale Serve started" : "Tailscale Serve stopped",
        next.tailnet_url || undefined,
      );
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : String(e);
      setSaveNotice(msg);
      toast.error(`Tailscale ${action} failed`, msg);
    } finally {
      setPhoneBusy(null);
    }
  };

  const phoneUrl = phone?.tailnet_url || phone?.lan_url || phone?.local_url || "";
  const qrRows = phoneUrl ? buildQrMatrix(phoneUrl) : [];

  if (section === "tuning") {
    return (
      <div className="mx-auto flex w-full max-w-4xl flex-col gap-8 px-6 py-8">
        <PageHeader
          title="Flight Settings"
          description="Tune cruise speed, safety failsafes, gimbal defaults, and camera rates (applied automatically on session start)."
        />

        <div className="flex flex-col gap-5">
          <div className="grid gap-4 sm:grid-cols-2">
            <SliderField
              label="Cruise Speed"
              value={flightSpeed}
              valueLabel={`${flightSpeed.toFixed(1)} m/s`}
              min={0.3}
              max={5.0}
              step={0.1}
              hint="Indoor scenes cap automatically to 2.0 m/s for safety."
              onChange={setFlightSpeed}
            />
            <SliderField
              label="AI Link-Loss Auto-RTH Delay"
              value={rthDelay}
              valueLabel={`${Math.round(rthDelay)} s`}
              min={5}
              max={120}
              step={5}
              hint="Time before Flight moves holds and returns home if AI goes silent."
              onChange={setRthDelay}
            />
            <SliderField
              label="Default Gimbal Pitch"
              value={gimbalPitch}
              valueLabel={`${Math.round(gimbalPitch)}°`}
              min={-90}
              max={15}
              step={5}
              hint="-90° points straight down (nadir); -15° looks slightly ahead."
              onChange={setGimbalPitch}
            />
            <SliderField
              label="Perception Max FPS"
              value={maxFps}
              valueLabel={`${Math.round(maxFps)} fps`}
              min={1}
              max={15}
              step={1}
              hint="Upper frame rate for YOLO + ReID detection on the camera stream."
              onChange={setMaxFps}
            />
            <SliderField
              label="Open-Vocab Grounding Rate"
              value={openVocabHz}
              valueLabel={`${openVocabHz.toFixed(1)} Hz`}
              min={0.1}
              max={2.0}
              step={0.1}
              hint="How frequently custom text labels are checked via horus-grounder."
              onChange={setOpenVocabHz}
            />
            <div className="flex flex-col gap-3 rounded-[8px] border border-border bg-surface p-3.5 sm:col-span-2">
              <div className="grid gap-3 sm:grid-cols-2">
                <Field label="Default Grounder Backend" hint="for open-vocab targets">
                  <Select value={grounderBackend} onChange={(e) => setGrounderBackend(e.target.value)}>
                    <option value="auto">Auto (Cloud VLM or Local PyTorch)</option>
                    <option value="cloud-vlm">Cloud VLM (Zero Install — uses API key)</option>
                    <option value="grounding-dino-tiny">Grounding DINO (Local PyTorch)</option>
                    <option value="owlv2-base-patch16-ensemble">OWLv2 (Local PyTorch)</option>
                  </Select>
                </Field>
                <Field label="Grounder Service URL">
                  <Input value={grounderUrl} onChange={(e) => setGrounderUrl(e.target.value)} />
                </Field>
              </div>
            </div>
          </div>

          <div className="flex flex-wrap items-center justify-between gap-3 border-t border-border pt-4">
            <span className="text-[13.5px] text-secondary">{saveNotice}</span>
            <Button variant="primary" loading={savingTuning} onClick={() => void saveTuning()}>
              {!savingTuning && <Save size={14} />} {savingTuning ? "Saving…" : "Save Settings"}
            </Button>
          </div>
        </div>
      </div>
    );
  }

  if (section === "phone") {
    return (
      <div className="mx-auto flex w-full max-w-4xl flex-col gap-8 px-6 py-8">
        <PageHeader
          title="Phone App"
          description="Open HORUS on your phone over local Wi-Fi (`horus studio --phone --lan`) or optional Tailscale HTTPS (`/?view=phone`)."
          action={
            <StatusBadge tone={phone?.serve_active || phone?.tls_enabled ? "ok" : "neutral"}>
              {phone?.serve_active ? "Tailnet HTTPS" : phone?.tls_enabled ? "LAN HTTPS" : "Local only"}
            </StatusBadge>
          }
        />

        <div className="flex flex-col items-start gap-5 rounded-[10px] border border-border bg-surface p-5 sm:flex-row">
          {qrRows.length > 0 ? (
            <svg
              className="h-36 w-36 shrink-0 rounded-[8px] border border-border bg-white p-2"
              viewBox={`0 0 ${qrRows.length + 4} ${qrRows.length + 4}`}
              aria-label="Phone companion QR code"
            >
              <rect width={qrRows.length + 4} height={qrRows.length + 4} fill="#ffffff" />
              {qrRows.map((row, r) =>
                row.map((dark, c) =>
                  dark ? <rect key={`${r}-${c}`} x={c + 2} y={r + 2} width="1" height="1" fill="#09090b" /> : null,
                ),
              )}
            </svg>
          ) : (
            <div className="flex h-36 w-36 shrink-0 items-center justify-center rounded-[8px] border border-border bg-raised text-muted">
              <QrCode size={28} />
            </div>
          )}
          <div className="flex min-w-0 flex-1 flex-col gap-3">
            <div className="text-[13.5px] text-secondary">
              {phone?.serve_active
                ? "Active link: encrypted Tailscale HTTPS (works anywhere over cellular/internet)."
                : phone?.tls_enabled
                  ? `Active link: Local LAN HTTPS (${phone.lan_ip || "0.0.0.0"}) — no Tailscale required.`
                  : "Local loopback only right now. Choose Wi-Fi LAN mode or Tailscale below to open on your phone."}
            </div>
            <pre className="overflow-x-auto rounded-[8px] border border-border bg-canvas p-3 font-mono text-[12.5px] text-primary">
              {phoneUrl || "Start Studio with `horus studio --phone --lan` for a dedicated phone link."}
            </pre>
            <div className="flex flex-wrap gap-2">
              <Button
                disabled={!phoneUrl}
                onClick={() => {
                  navigator.clipboard?.writeText(phoneUrl).then(() => {
                    setCopiedUrl(true);
                    toast.info("Phone URL copied");
                    window.setTimeout(() => setCopiedUrl(false), 1500);
                  });
                }}
              >
                {copiedUrl ? <Check size={14} /> : <Copy size={14} />} {copiedUrl ? "Copied" : "Copy Active URL"}
              </Button>
            </div>
          </div>
        </div>

        <Section
          title="Connection Mode"
          description="Use Local Wi-Fi / Hotspot with zero external accounts, or Tailscale for remote cellular access."
        >
          <div className="flex flex-col gap-4 rounded-[10px] border border-border bg-surface p-5">
            <div className="flex flex-wrap gap-2" role="tablist" aria-label="Phone connection mode">
              <Button
                size="sm"
                variant={phoneMode === "lan" ? "primary" : "secondary"}
                onClick={() => setPhoneMode("lan")}
              >
                <Wifi size={14} /> Local Wi-Fi / Hotspot (No Tailscale)
              </Button>
              <Button
                size="sm"
                variant={phoneMode === "tailscale" ? "primary" : "secondary"}
                onClick={() => setPhoneMode("tailscale")}
              >
                <Globe size={14} /> Remote Access (Tailscale HTTPS)
              </Button>
            </div>

            {phoneMode === "lan" ? (
              <div className="flex flex-col gap-3 border-t border-border pt-4 text-[13.5px] text-secondary">
                <p className="text-primary">
                  Best for field hotspots or home Wi-Fi — uses a built-in self-signed HTTPS certificate and works completely offline without Tailscale.
                </p>
                <ol className="list-decimal space-y-2 pl-5">
                  <li>
                    Connect your laptop and phone to the same Wi-Fi network or mobile hotspot.
                  </li>
                  <li>
                    Restart HORUS Studio with LAN HTTPS enabled:
                    <div className="mt-1.5">
                      <CopyCommand command="horus studio --phone --lan" label="LAN Phone command" />
                    </div>
                  </li>
                  <li>
                    Scan the QR code above on your phone, accept the one-time browser security prompt for the local self-signed cert, and tap <strong className="text-primary">Add to Home Screen</strong>.
                  </li>
                </ol>
                <div className="text-[12.5px] text-muted">
                  Current status:{" "}
                  <strong className="text-primary">
                    {phone?.tls_enabled ? `LAN HTTPS active on ${phone.lan_ip || "0.0.0.0"}` : "Not started with --lan"}
                  </strong>
                </div>
              </div>
            ) : (
              <div className="flex flex-col gap-3 border-t border-border pt-4 text-[13.5px] text-secondary">
                <p className="text-primary">
                  Best when your phone is on cellular or a different network — routes strictly over your private Tailscale tailnet with trusted HTTPS certs.
                </p>
                <ol className="list-decimal space-y-2 pl-5">
                  <li>
                    Install Tailscale on both your laptop and phone and sign into the same tailnet.
                  </li>
                  <li>
                    Enable <strong className="text-primary">MagicDNS</strong> and <strong className="text-primary">HTTPS Certificates</strong> in your Tailscale admin console.
                  </li>
                  <li>
                    Start Studio with <code className="font-mono text-primary">horus studio --phone</code> and click <strong className="text-primary">Start Tailscale Serve</strong> below.
                  </li>
                </ol>
                <div className="text-[12.5px] text-muted">
                  Tailscale CLI:{" "}
                  <strong className="text-primary">{phone?.tailscale_installed ? "installed" : "not installed"}</strong>
                  {" · "}HTTPS certs:{" "}
                  <strong className="text-primary">{phone?.tailscale_certs ? "enabled" : "disabled"}</strong>
                  {phone?.tailscale_dns ? ` · DNS: ${phone.tailscale_dns}` : ""}
                </div>
                <div className="flex flex-wrap gap-2">
                  <Button
                    variant="primary"
                    loading={phoneBusy === "start"}
                    disabled={phoneBusy !== null}
                    onClick={() => void handlePhoneServe("start")}
                  >
                    {phoneBusy !== "start" && <Smartphone size={14} />}
                    {phoneBusy === "start" ? "Starting…" : "Start Tailscale Serve"}
                  </Button>
                  <Button
                    loading={phoneBusy === "stop"}
                    disabled={phoneBusy !== null}
                    onClick={() => void handlePhoneServe("stop")}
                  >
                    {phoneBusy === "stop" ? "Stopping…" : "Stop Serve"}
                  </Button>
                </div>
                {saveNotice && <p className="text-[13px] text-warning">{saveNotice}</p>}
              </div>
            )}
          </div>
        </Section>
      </div>
    );
  }

  const coreCaps = caps.filter((c) => c.tier !== "optional");
  const optionalCaps = caps.filter((c) => c.tier === "optional");
  const optionalActiveCount = optionalCaps.filter((c) => c.ready).length;

  const targetForCap = (id: string): NavId | null => {
    if (id === "agents") return "ai";
    if (id === "phone_pwa") return "phone";
    if (id === "builtin_3d" || id === "gazebo" || id === "gta" || id === "real_drone" || id === "grounder") {
      return "worlds";
    }
    return null;
  };

  const renderCapabilityCard = (cap: CapabilityItem, isOptionalTier: boolean) => {
    const targetNav = targetForCap(cap.id);
    return (
      <div key={cap.id}>
        <div className="flex items-center justify-between gap-4 px-5 py-3.5">
          <div className="min-w-0">
            <div className="text-[14.5px] font-medium text-primary">{cap.title}</div>
            <div className="mt-0.5 text-[13px] text-secondary">{cap.summary}</div>
          </div>
          <div className="flex shrink-0 items-center gap-2">
            {onNavigate && targetNav && (
              <Button size="sm" variant="ghost" onClick={() => onNavigate(targetNav)}>
                Configure <ArrowRight size={12} />
              </Button>
            )}
            <StatusBadge tone={cap.ready ? "ok" : isOptionalTier ? "neutral" : "warn"}>
              {cap.ready ? "Ready" : isOptionalTier ? "Optional" : "Setup needed"}
            </StatusBadge>
          </div>
        </div>
        <div className="divide-y divide-border border-t border-border bg-canvas px-5">
          {cap.checks.map((c) => {
            const checkOptional = Boolean(c.optional || isOptionalTier);
            const cmdMatch = !c.ok && c.fix_hint ? c.fix_hint.match(/`([^`]+)`/) : null;
            return (
              <div key={c.name} className="flex items-start justify-between gap-4 py-3 text-[13px]">
                <div className="min-w-0 flex-1">
                  <div className="font-medium text-primary">{c.name}</div>
                  <div className="text-secondary">{c.detail}</div>
                  {!c.ok && c.fix_hint && (
                    <div className={`mt-1 flex flex-wrap items-center gap-2 ${checkOptional ? "text-muted" : "text-warning"}`}>
                      <span>{checkOptional ? `Note: ${c.fix_hint}` : `Fix: ${c.fix_hint}`}</span>
                      {cmdMatch && (
                        <button
                          type="button"
                          onClick={() => {
                            navigator.clipboard
                              ?.writeText(cmdMatch[1])
                              .then(() => toast.info("Command copied", cmdMatch[1]))
                              .catch(() => {});
                          }}
                          className="inline-flex items-center gap-1 rounded-[5px] border border-border bg-raised px-1.5 py-0.5 font-mono text-[11px] text-secondary hover:bg-hover hover:text-primary cursor-pointer"
                        >
                          <Copy size={11} /> {cmdMatch[1]}
                        </button>
                      )}
                    </div>
                  )}
                </div>
                <StatusBadge tone={c.ok ? "ok" : checkOptional ? "neutral" : "warn"}>
                  {c.ok ? "OK" : checkOptional ? "Optional" : "Missing"}
                </StatusBadge>
              </div>
            );
          })}
        </div>
      </div>
    );
  };

  return (
    <div className="mx-auto flex w-full max-w-4xl flex-col gap-8 px-6 py-8">
      <PageHeader
        title="System & Logs"
        description="Inspect core standalone requirements, optional integrations, cached assets, and recent flight logs."
        action={
          <div className="flex flex-wrap items-center gap-2">
            {studioStatus && !studioStatus.assets_ready && onBootstrap && (
              <Button size="sm" variant="primary" loading={bootstrapping} onClick={() => void onBootstrap()}>
                {!bootstrapping && <Download size={13} />}
                {bootstrapping ? "Downloading…" : "Download SITL & Models"}
              </Button>
            )}
            <Button size="sm" loading={checkingCaps} onClick={() => void handleRecheckCaps()}>
              {!checkingCaps && <RefreshCw size={13} />} {checkingCaps ? "Checking…" : "Re-check All"}
            </Button>
          </div>
        }
      />

      <Section
        title="Core Requirements"
        description="Everything needed for built-in 3D simulation, AI missions, and real drone telemetry."
      >
        <div className="divide-y divide-border overflow-hidden rounded-[10px] border border-border bg-surface">
          {coreCaps.map((cap) => renderCapabilityCard(cap, false))}
        </div>
      </Section>

      {optionalCaps.length > 0 && (
        <Section
          title="Optional & Advanced Integrations"
          description="Extra simulators, local PyTorch grounder, and remote Tailscale access — not required for standalone flights."
        >
          <details className="group overflow-hidden rounded-[10px] border border-border bg-surface">
            <summary className="flex cursor-pointer select-none items-center justify-between gap-4 px-5 py-3.5 text-[14px] font-medium text-primary transition-colors hover:bg-hover">
              <span>
                Gazebo Harmonic, GTA V, Open-Vocab Grounder &amp; Phone Tailscale
              </span>
              <span className="font-mono text-[12.5px] text-secondary tabular-nums">
                {optionalActiveCount}/{optionalCaps.length} active
              </span>
            </summary>
            <div className="divide-y divide-border border-t border-border">
              {optionalCaps.map((cap) => renderCapabilityCard(cap, true))}
            </div>
          </details>
        </Section>
      )}

      <Section title="Cached Assets & Recent Run Logs">
        <div className="divide-y divide-border overflow-hidden rounded-[10px] border border-border bg-surface text-[13.5px]">
          {Object.entries(studioStatus?.assets || {}).map(([name, info]) => (
            <div key={name} className="flex items-center justify-between gap-4 px-5 py-3">
              <span className="truncate">
                <strong className="font-medium text-primary">{name}</strong> ·{" "}
                <span className="text-secondary">{info.detail}</span>
              </span>
              <StatusBadge tone={info.available ? "ok" : info.optional ? "neutral" : "warn"}>
                {info.available ? "Ready" : info.optional ? "Optional" : "Missing"}
              </StatusBadge>
            </div>
          ))}
          {runs.slice(0, 10).map((r) => (
            <div key={r.name} className="flex items-center justify-between gap-4 px-5 py-3 font-mono text-[13px]">
              <span className="truncate text-primary">{r.name}</span>
              <span className="shrink-0 text-secondary tabular-nums">{(r.bytes / 1024).toFixed(1)} KB</span>
            </div>
          ))}
        </div>
      </Section>
    </div>
  );
}
