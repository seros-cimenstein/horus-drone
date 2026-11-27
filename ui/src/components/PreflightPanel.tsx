import { RefreshCw } from "lucide-react";
import type { PreflightItem } from "../types";
import { Button } from "./common/Button";
import { StatusBadge } from "./common/Surface";

interface Props {
  items: PreflightItem[];
  loading: boolean;
  onRefresh: () => void;
}

export function PreflightPanel({ items, loading, onRefresh }: Props) {
  return (
    <div className="flex flex-1 flex-col gap-3 overflow-y-auto p-4">
      <div className="flex items-start justify-between gap-3">
        <p className="text-[13px] text-secondary">
          Live safety check of autopilot heartbeat, ToF proximity sensors, battery level, and camera stream freshness.
        </p>
        <Button size="sm" className="shrink-0" loading={loading} onClick={onRefresh}>
          {!loading && <RefreshCw size={13} />} {loading ? "Checking…" : "Refresh"}
        </Button>
      </div>

      <div className="divide-y divide-border overflow-hidden rounded-[10px] border border-border bg-canvas">
        {items.map((item) => (
          <div key={item.id} className="flex items-center justify-between gap-3 px-4 py-3">
            <div className="min-w-0">
              <div className="text-[13.5px] font-medium text-primary">{item.label}</div>
              <div className="mt-0.5 text-[12.5px] text-secondary">{item.detail}</div>
            </div>
            <StatusBadge
              tone={item.status === "pass" ? "ok" : item.status === "warn" ? "warn" : "danger"}
              className="shrink-0"
            >
              {item.status}
            </StatusBadge>
          </div>
        ))}
        {items.length === 0 && (
          <div className="px-4 py-6 text-center text-[13.5px] text-muted">
            Start a session to run live pre-flight checks.
          </div>
        )}
      </div>
    </div>
  );
}
