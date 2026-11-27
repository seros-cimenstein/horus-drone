import { AlertCircle, ArrowRight, CheckCircle2, Copy, Download } from "lucide-react";
import type { CapabilityCheck, CapabilityItem } from "../../types";
import { Button } from "./Button";
import { StatusBadge } from "./Surface";
import { useToast } from "./Toast";

interface Props {
  cap: CapabilityItem;
  mode?: "full" | "inline";
  isOptionalTier?: boolean;
  alwaysExpandWhenIncomplete?: boolean;
  actionLabel?: string;
  onAction?: () => void;
  onBootstrap?: () => Promise<void>;
  bootstrapping?: boolean;
}

function CheckRow({ check, compact }: { check: CapabilityCheck; compact: boolean }) {
  const toast = useToast();
  const checkOptional = Boolean(check.optional);
  const cmdMatch = !check.ok && check.fix_hint ? check.fix_hint.match(/`([^`]+)`/) : null;

  return (
    <div className={`flex items-start justify-between gap-3 ${compact ? "py-2 text-[12.5px]" : "py-3 text-[13px]"}`}>
      <div className="min-w-0 flex-1">
        <div className="font-medium text-primary">{check.name}</div>
        <div className="text-secondary">{check.detail}</div>
        {!check.ok && check.fix_hint && (
          <div className={`mt-1 flex flex-wrap items-center gap-2 ${checkOptional ? "text-muted" : "text-warning"}`}>
            <span>{check.fix_hint}</span>
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
      <StatusBadge tone={check.ok ? "ok" : checkOptional ? "neutral" : "warn"} className="shrink-0">
        {check.ok ? "OK" : checkOptional ? "Optional" : "Required"}
      </StatusBadge>
    </div>
  );
}

export function CapabilityRequirements({
  cap,
  mode = "inline",
  isOptionalTier = cap.tier === "optional",
  alwaysExpandWhenIncomplete = true,
  actionLabel = "Configure",
  onAction,
  onBootstrap,
  bootstrapping = false,
}: Props) {
  const requiredChecks = cap.checks.filter((c) => !c.optional);
  const optionalChecks = cap.checks.filter((c) => Boolean(c.optional));
  const missingRequired = requiredChecks.filter((c) => !c.ok);
  const hasMissingRequired = missingRequired.length > 0;

  if (mode === "full") {
    return (
      <div>
        <div className="flex items-center justify-between gap-4 px-5 py-3.5">
          <div className="min-w-0">
            <div className="text-[14.5px] font-medium text-primary">{cap.title}</div>
            <div className="mt-0.5 text-[13px] text-secondary">{cap.summary}</div>
          </div>
          <div className="flex shrink-0 items-center gap-2">
            {onAction && (
              <Button size="sm" variant="ghost" onClick={onAction}>
                {actionLabel} <ArrowRight size={12} />
              </Button>
            )}
            <StatusBadge tone={cap.ready ? "ok" : hasMissingRequired && !isOptionalTier ? "warn" : "neutral"}>
              {cap.ready ? "Ready" : hasMissingRequired ? "Needs setup" : "Optional"}
            </StatusBadge>
          </div>
        </div>
        <div className="divide-y divide-border border-t border-border bg-canvas px-5">
          {cap.checks.map((check) => (
            <CheckRow key={check.name} check={check} compact={false} />
          ))}
        </div>
      </div>
    );
  }

  const okCount = cap.checks.filter((c) => c.ok).length;
  const totalCount = cap.checks.length;
  const optionalReadyCount = optionalChecks.filter((c) => c.ok).length;
  const showExpanded = hasMissingRequired && alwaysExpandWhenIncomplete;

  if (!showExpanded) {
    const readyLabel = hasMissingRequired
      ? `${cap.summary} (${okCount}/${totalCount} checks)`
      : `Ready (${okCount}/${totalCount} checks)`;
    return (
      <details className="rounded-[8px] border border-border bg-canvas px-3.5 py-2 text-[12.5px] text-secondary">
        <summary className="flex cursor-pointer select-none items-center justify-between gap-2 font-medium text-primary">
          <span className="inline-flex min-w-0 items-center gap-2">
            {hasMissingRequired ? (
              <AlertCircle size={14} className="shrink-0 text-secondary" />
            ) : (
              <CheckCircle2 size={14} className="shrink-0 text-success" />
            )}
            <span className="truncate">{readyLabel}</span>
          </span>
          <span className="shrink-0 text-[12px] font-normal text-muted">Details</span>
        </summary>
        <div className="mt-2 divide-y divide-border border-t border-border pt-1">
          {cap.checks.map((check) => (
            <CheckRow key={check.name} check={check} compact />
          ))}
        </div>
      </details>
    );
  }

  return (
    <div className="rounded-[8px] border border-border bg-canvas p-3.5 text-[12.5px]">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex min-w-0 items-center gap-2">
          <AlertCircle size={14} className="shrink-0 text-warning" />
          <span className="font-medium text-primary">
            {isOptionalTier ? "To use this feature" : "Required to run"}
          </span>
          <span className="text-secondary">({cap.summary})</span>
        </div>
        {onBootstrap && !cap.ready && (
          <Button size="sm" variant="primary" loading={bootstrapping} onClick={() => void onBootstrap()}>
            {!bootstrapping && <Download size={13} />}
            {bootstrapping ? "Downloading…" : "Download SITL & Models"}
          </Button>
        )}
      </div>

      <div className="mt-2.5 divide-y divide-border border-t border-border pt-1">
        {missingRequired.map((check) => (
          <CheckRow key={check.name} check={check} compact />
        ))}
      </div>

      {optionalChecks.length > 0 && (
        <details className="mt-2 border-t border-border pt-2 text-[12px] text-muted">
          <summary className="cursor-pointer select-none font-medium text-secondary hover:text-primary">
            Optional extras ({optionalReadyCount}/{optionalChecks.length} available)
          </summary>
          <div className="mt-1.5 divide-y divide-border">
            {optionalChecks.map((check) => (
              <CheckRow key={check.name} check={check} compact />
            ))}
          </div>
        </details>
      )}
    </div>
  );
}
