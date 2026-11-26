import clsx from "clsx";

export type DotStatus = "ok" | "warn" | "danger" | "idle";

export interface StatusDotProps {
  status: DotStatus;
  label?: string;
  className?: string;
}

const DOT_CLASS: Record<DotStatus, string> = {
  ok: "bg-success",
  warn: "bg-warning",
  danger: "bg-danger",
  idle: "bg-muted",
};

export function StatusDot({ status, label, className }: StatusDotProps) {
  return (
    <span className={clsx("inline-flex items-center gap-2 text-[13px] text-secondary", className)}>
      <span aria-hidden="true" className={clsx("inline-block h-2 w-2 shrink-0 rounded-full", DOT_CLASS[status])} />
      {label ? <span className="truncate">{label}</span> : null}
    </span>
  );
}
