import { Download, RefreshCw } from "lucide-react";
import { useEffect, useState } from "react";
import { horusApi } from "../api";
import type { CapabilityItem, NavId, RunLogEntry, SettingsTab, StudioStatus } from "../types";
import { Button } from "./common/Button";
import { CapabilityRequirements } from "./common/CapabilityRequirements";
import { PageHeader, Section, StatusBadge } from "./common/Surface";
import { useToast } from "./common/Toast";

interface Props {
  token: string;
  studioStatus: StudioStatus | null;
  caps: CapabilityItem[];
  bootstrapping?: boolean;
  onBootstrap?: () => Promise<void>;
  onRefreshCaps: (force: boolean) => Promise<CapabilityItem[]>;
  onNavigate: (id: NavId) => void;
  onOpenSettings: (tab?: SettingsTab) => void;
}

export function SystemLogsView({
  token,
  studioStatus,
  caps,
  bootstrapping = false,
  onBootstrap,
  onRefreshCaps,
  onNavigate,
  onOpenSettings,
}: Props) {
  const [checkingCaps, setCheckingCaps] = useState(false);
  const [runs, setRuns] = useState<RunLogEntry[]>([]);
  const toast = useToast();

  useEffect(() => {
    horusApi.getRuns(token).then((r) => setRuns(r.runs)).catch(() => {});
  }, [token]);

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

  const coreCaps = caps.filter((c) => c.tier !== "optional");
  const optionalCaps = caps.filter((c) => c.tier === "optional");
  const optionalActiveCount = optionalCaps.filter((c) => c.ready).length;

  const actionForCap = (id: string): (() => void) | undefined => {
    if (id === "agents" || id === "grounder") return () => onOpenSettings("ai");
    if (id === "phone_pwa") return () => onOpenSettings("phone");
    if (id === "builtin_3d" || id === "gazebo" || id === "gta" || id === "real_drone") {
      return () => onNavigate("worlds");
    }
    return undefined;
  };

  return (
    <div className="mx-auto flex w-full max-w-4xl flex-col gap-8 px-6 py-8">
      <PageHeader
        title="System & Logs"
        description="Inspect core standalone requirements, optional integrations, downloaded runtime assets, and flight logs."
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
          {coreCaps.map((cap) => (
            <CapabilityRequirements
              key={cap.id}
              cap={cap}
              mode="full"
              isOptionalTier={false}
              onAction={actionForCap(cap.id)}
            />
          ))}
        </div>
      </Section>

      {optionalCaps.length > 0 && (
        <Section
          title="Optional & Advanced Integrations"
          description="Extra simulators, local PyTorch grounder, and remote Tailscale access (not required for standalone flights)."
        >
          <details open className="group overflow-hidden rounded-[10px] border border-border bg-surface">
            <summary className="flex cursor-pointer select-none items-center justify-between gap-4 px-5 py-3.5 text-[14px] font-medium text-primary transition-colors hover:bg-hover">
              <span>Gazebo Harmonic, GTA V, Open-Vocab Grounder &amp; Phone Tailscale</span>
              <span className="font-mono text-[12.5px] text-secondary tabular-nums">
                {optionalActiveCount}/{optionalCaps.length} ready
              </span>
            </summary>
            <div className="divide-y divide-border border-t border-border">
              {optionalCaps.map((cap) => (
                <CapabilityRequirements
                  key={cap.id}
                  cap={cap}
                  mode="full"
                  isOptionalTier
                  onAction={actionForCap(cap.id)}
                />
              ))}
            </div>
          </details>
        </Section>
      )}

      <Section
        title="Cached Assets"
        description="Downloaded ArduCopter SITL binary and ONNX perception models cached locally."
      >
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
          {Object.keys(studioStatus?.assets || {}).length === 0 && (
            <div className="px-5 py-4 text-muted">No cached asset metadata loaded yet.</div>
          )}
        </div>
      </Section>

      <Section
        title="Recent Run Logs"
        description="Flight telemetry and mission logs saved from recent sessions."
      >
        <div className="divide-y divide-border overflow-hidden rounded-[10px] border border-border bg-surface text-[13.5px]">
          {runs.slice(0, 10).map((r) => (
            <div key={r.name} className="flex items-center justify-between gap-4 px-5 py-3 font-mono text-[13px]">
              <span className="truncate text-primary">{r.name}</span>
              <span className="shrink-0 text-secondary tabular-nums">{(r.bytes / 1024).toFixed(1)} KB</span>
            </div>
          ))}
          {runs.length === 0 && (
            <div className="px-5 py-4 text-muted">No flight run logs recorded yet.</div>
          )}
        </div>
      </Section>
    </div>
  );
}
