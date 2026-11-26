import { Check, Copy, QrCode, RefreshCw, Save, Smartphone } from "lucide-react";
import { useEffect, useState } from "react";
import { horusApi } from "../api";
import { buildQrMatrix } from "../qr";
import type { CapabilityItem, PhoneStatus, RunLogEntry, StudioStatus } from "../types";
import { Button } from "./common/Button";
import { Field, Input, PageHeader, Section, SliderField, StatusBadge } from "./common/Surface";

interface Props {
  section: "tuning" | "phone" | "system";
  token: string;
  studioStatus: StudioStatus | null;
  caps: CapabilityItem[];
  onRefreshCaps: (force: boolean) => void;
  onSaved: () => void;
}

export function SystemPhoneView({ section, token, studioStatus, caps, onRefreshCaps, onSaved }: Props) {
  const [flightSpeed, setFlightSpeed] = useState(1.2);
  const [rthDelay, setRthDelay] = useState(30.0);
  const [gimbalPitch, setGimbalPitch] = useState(-15.0);
  const [maxFps, setMaxFps] = useState(5.0);
  const [openVocabHz, setOpenVocabHz] = useState(0.5);
  const [grounderUrl, setGrounderUrl] = useState("http://127.0.0.1:8100");
  const [saveNotice, setSaveNotice] = useState("");
  const [phone, setPhone] = useState<PhoneStatus | null>(null);
  const [runs, setRuns] = useState<RunLogEntry[]>([]);
  const [copiedUrl, setCopiedUrl] = useState(false);

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
      })
      .catch(() => {});
    if (section === "phone") {
      horusApi.getPhoneStatus(token).then(setPhone).catch(() => {});
    }
    if (section === "system") {
      horusApi.getRuns(token).then((r) => setRuns(r.runs)).catch(() => {});
    }
  }, [section, token]);

  const saveTuning = async () => {
    await horusApi.putStudioSettings(token, {
      flight_speed_m_s: flightSpeed,
      link_loss_rth_delay_s: rthDelay,
      default_gimbal_pitch_deg: gimbalPitch,
      camera_max_fps: maxFps,
      open_vocab_hz: openVocabHz,
      grounder_url: grounderUrl,
    });
    onSaved();
    setSaveNotice("Saved. Restart an active session to apply.");
  };

  const phoneUrl = phone?.tailnet_url || phone?.local_url || "";
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
            <div className="flex flex-col justify-center rounded-[8px] border border-border bg-surface p-3.5">
              <Field label="Grounder Service URL">
                <Input value={grounderUrl} onChange={(e) => setGrounderUrl(e.target.value)} />
              </Field>
            </div>
          </div>

          <div className="flex flex-wrap items-center justify-between gap-3 border-t border-border pt-4">
            <span className="text-[13.5px] text-secondary">{saveNotice}</span>
            <Button variant="primary" onClick={() => void saveTuning()}>
              <Save size={14} /> Save Settings
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
          description="Open HORUS on your phone over local Wi-Fi or encrypted Tailscale HTTPS (`/?view=phone`)."
          action={
            <StatusBadge tone={phone?.serve_active ? "ok" : "neutral"}>
              {phone?.serve_active ? "Tailnet active" : "Local only"}
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
              Tailscale CLI: <strong className="text-primary">{phone?.tailscale_installed ? "installed" : "not installed"}</strong>
              {" · "}HTTPS certs: <strong className="text-primary">{phone?.tailscale_certs ? "enabled" : "disabled"}</strong>
            </div>
            <pre className="overflow-x-auto rounded-[8px] border border-border bg-canvas p-3 font-mono text-[12.5px] text-primary">
              {phoneUrl || "Start Studio with `horus studio --phone` for a dedicated phone link."}
            </pre>
            <div className="flex flex-wrap gap-2">
              <Button
                variant="primary"
                onClick={() => void horusApi.phoneServe(token, "start").then(setPhone).catch((e) => setSaveNotice(String(e)))}
              >
                <Smartphone size={14} /> Start Tailscale Serve
              </Button>
              <Button
                onClick={() => void horusApi.phoneServe(token, "stop").then(setPhone).catch((e) => setSaveNotice(String(e)))}
              >
                Stop Serve
              </Button>
              <Button
                disabled={!phoneUrl}
                onClick={() => {
                  navigator.clipboard?.writeText(phoneUrl).then(() => {
                    setCopiedUrl(true);
                    window.setTimeout(() => setCopiedUrl(false), 1500);
                  });
                }}
              >
                {copiedUrl ? <Check size={14} /> : <Copy size={14} />} {copiedUrl ? "Copied" : "Copy URL"}
              </Button>
            </div>
            {saveNotice && <p className="text-[13px] text-warning">{saveNotice}</p>}
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="mx-auto flex w-full max-w-4xl flex-col gap-8 px-6 py-8">
      <PageHeader
        title="System & Logs"
        description="Inspect dependency checks across all HORUS features, downloaded assets, and recent flight logs."
        action={
          <Button size="sm" onClick={() => onRefreshCaps(true)}>
            <RefreshCw size={13} /> Re-check All
          </Button>
        }
      />

      <Section
        title="System Requirements"
        description="All dependency checks and copy-ready fix hints are shown below."
      >
        <div className="divide-y divide-border overflow-hidden rounded-[10px] border border-border bg-surface">
          {caps.map((cap) => (
            <div key={cap.id}>
              <div className="flex items-center justify-between gap-4 px-5 py-3.5">
                <div className="min-w-0">
                  <div className="text-[14.5px] font-medium text-primary">{cap.title}</div>
                  <div className="mt-0.5 text-[13px] text-secondary">{cap.summary}</div>
                </div>
                <StatusBadge tone={cap.ready ? "ok" : "warn"}>{cap.ready ? "Ready" : "Setup needed"}</StatusBadge>
              </div>
              <div className="divide-y divide-border border-t border-border bg-canvas px-5">
                {cap.checks.map((c) => (
                  <div key={c.name} className="flex items-start justify-between gap-4 py-3 text-[13px]">
                    <div>
                      <div className="font-medium text-primary">{c.name}</div>
                      <div className="text-secondary">{c.detail}</div>
                      {!c.ok && c.fix_hint && <div className="mt-1 text-warning">Fix: {c.fix_hint}</div>}
                    </div>
                    <StatusBadge tone={c.ok ? "ok" : "warn"}>{c.ok ? "OK" : "Missing"}</StatusBadge>
                  </div>
                ))}
              </div>
            </div>
          ))}
        </div>
      </Section>

      <Section title="Cached Assets & Recent Run Logs">
        <div className="divide-y divide-border overflow-hidden rounded-[10px] border border-border bg-surface text-[13.5px]">
          {Object.entries(studioStatus?.assets || {}).map(([name, info]) => (
            <div key={name} className="flex items-center justify-between gap-4 px-5 py-3">
              <span className="truncate">
                <strong className="font-medium text-primary">{name}</strong> ·{" "}
                <span className="text-secondary">{info.detail}</span>
              </span>
              <StatusBadge tone={info.available ? "ok" : "warn"}>{info.available ? "Ready" : "Missing"}</StatusBadge>
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
