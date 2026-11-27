import { AlertCircle, AlertTriangle, Check, Info } from "lucide-react";
import { Button } from "./Button";

export type BannerTone = "danger" | "warning" | "info" | "success";

export interface StatusBannerProps {
  tone: BannerTone;
  title: string;
  detail?: string;
  variant?: "bar" | "inline";
  action?: {
    label: string;
    onClick: () => void;
  };
  onDismiss?: () => void;
}

function ToneGlyph({ tone }: { tone: BannerTone }) {
  if (tone === "danger") {
    return (
      <span
        aria-hidden="true"
        className="inline-flex h-5 w-5 shrink-0 items-center justify-center rounded-full bg-danger/15 text-danger"
      >
        <AlertTriangle size={13} strokeWidth={2} />
      </span>
    );
  }
  if (tone === "warning") {
    return (
      <span
        aria-hidden="true"
        className="inline-flex h-5 w-5 shrink-0 items-center justify-center rounded-full bg-warning/15 text-warning"
      >
        <AlertCircle size={13} strokeWidth={2} />
      </span>
    );
  }
  if (tone === "success") {
    return (
      <span
        aria-hidden="true"
        className="inline-flex h-5 w-5 shrink-0 items-center justify-center rounded-full bg-success/15 text-success"
      >
        <Check size={13} strokeWidth={2.25} />
      </span>
    );
  }
  return (
    <span
      aria-hidden="true"
      className="inline-flex h-5 w-5 shrink-0 items-center justify-center rounded-full bg-hover text-secondary"
    >
      <Info size={13} strokeWidth={2} />
    </span>
  );
}

export function StatusBanner({
  tone,
  title,
  detail,
  variant = "bar",
  action,
  onDismiss,
}: StatusBannerProps) {
  const isMultiline = Boolean(detail && detail.includes("\n"));
  const containerClass =
    variant === "inline"
      ? "flex flex-wrap items-center justify-between gap-3 rounded-[8px] border border-border bg-canvas px-3.5 py-2.5"
      : "flex shrink-0 flex-wrap items-center justify-between gap-3 border-b border-border bg-surface px-4 py-2.5 md:px-5";

  return (
    <div role={tone === "info" || tone === "success" ? "status" : "alert"} className={containerClass}>
      <div className="flex min-w-0 flex-1 items-start gap-2.5">
        <ToneGlyph tone={tone} />
        <div className="min-w-0 flex-1 leading-[20px]">
          <span className="text-[13.5px] font-medium text-primary">{title}</span>
          {detail &&
            (isMultiline ? (
              <pre className="mt-1 whitespace-pre-wrap break-words font-mono text-[12px] leading-relaxed text-secondary">
                {detail}
              </pre>
            ) : (
              <>
                {" "}
                <span className="mt-0.5 block text-[13px] text-secondary sm:mt-0 sm:ml-1.5 sm:inline">
                  {detail}
                </span>
              </>
            ))}
        </div>
      </div>

      {(action || onDismiss) && (
        <div className="flex shrink-0 items-center gap-2">
          {action && (
            <Button size="sm" variant={tone === "danger" ? "danger" : "secondary"} onClick={action.onClick}>
              {action.label}
            </Button>
          )}
          {onDismiss && (
            <Button size="sm" variant="primary" onClick={onDismiss}>
              Dismiss
            </Button>
          )}
        </div>
      )}
    </div>
  );
}
