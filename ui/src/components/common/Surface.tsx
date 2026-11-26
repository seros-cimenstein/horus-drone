import clsx from "clsx";
import type {
  InputHTMLAttributes,
  ReactNode,
  SelectHTMLAttributes,
  TextareaHTMLAttributes,
} from "react";
import { StatusDot, type DotStatus } from "./StatusDot";

export function PageHeader({
  title,
  description,
  action,
  className,
}: {
  title: string;
  description?: string;
  action?: ReactNode;
  className?: string;
}) {
  return (
    <div className={clsx("flex flex-wrap items-end justify-between gap-4 border-b border-border pb-5", className)}>
      <div>
        <h1 className="text-[22px] sm:text-[24px] font-semibold tracking-tight text-primary">{title}</h1>
        {description && <p className="mt-1 text-[14.5px] text-secondary">{description}</p>}
      </div>
      {action && <div className="flex flex-wrap items-center gap-2">{action}</div>}
    </div>
  );
}

export function Section({
  title,
  description,
  action,
  children,
  className,
}: {
  title: string;
  description?: string;
  action?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <section className={clsx("border-b border-border pb-7 last:border-b-0 last:pb-0", className)}>
      <div className="mb-4 flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 className="text-[16px] font-semibold tracking-tight text-primary">{title}</h2>
          {description && <p className="mt-0.5 text-[13.5px] text-muted">{description}</p>}
        </div>
        {action && <div className="flex flex-wrap items-center gap-2">{action}</div>}
      </div>
      {children}
    </section>
  );
}

export function StatusBadge({
  tone = "neutral",
  children,
  className,
}: {
  tone?: "neutral" | "ok" | "warn" | "danger";
  children: ReactNode;
  className?: string;
}) {
  const dot: DotStatus = tone === "neutral" ? "idle" : tone;
  return (
    <span
      className={clsx(
        "inline-flex items-center gap-1.5 rounded-[6px] border border-border bg-raised px-2.5 py-1 text-[12.5px] font-medium text-secondary",
        className,
      )}
    >
      <StatusDot status={dot} />
      <span>{children}</span>
    </span>
  );
}

export function Field({
  label,
  hint,
  children,
  className,
}: {
  label: string;
  hint?: string;
  children: ReactNode;
  className?: string;
}) {
  return (
    <label className={clsx("flex flex-col gap-1.5 text-[13.5px]", className)}>
      <span className="font-medium text-primary">
        {label}
        {hint && <span className="ml-2 font-normal text-[12.5px] text-muted">{hint}</span>}
      </span>
      {children}
    </label>
  );
}

export function SliderField({
  label,
  value,
  valueLabel,
  min,
  max,
  step,
  hint,
  onChange,
}: {
  label: string;
  value: number;
  valueLabel: string;
  min: number;
  max: number;
  step: number;
  hint?: string;
  onChange: (next: number) => void;
}) {
  return (
    <div className="flex flex-col gap-2 rounded-[8px] border border-border bg-surface p-3.5">
      <div className="flex items-center justify-between gap-2">
        <span className="text-[13.5px] font-medium text-primary">{label}</span>
        <span className="font-mono text-[13.5px] font-semibold text-primary tabular-nums">{valueLabel}</span>
      </div>
      <input
        type="range"
        min={min}
        max={max}
        step={step}
        value={value}
        onChange={(e) => onChange(Number(e.target.value))}
        className="w-full"
      />
      {hint && <span className="text-[12.5px] text-muted">{hint}</span>}
    </div>
  );
}

const CONTROL_BASE =
  "w-full rounded-[8px] border border-border bg-raised px-3.5 py-2 text-[14px] text-primary placeholder:text-muted transition-colors hover:border-border-strong focus:border-accent focus:outline-none disabled:opacity-50";

export function Input({ className, ...props }: InputHTMLAttributes<HTMLInputElement>) {
  return <input className={clsx(CONTROL_BASE, "h-10", className)} {...props} />;
}

export function Select({ className, children, ...props }: SelectHTMLAttributes<HTMLSelectElement>) {
  return (
    <select className={clsx(CONTROL_BASE, "h-10", className)} {...props}>
      {children}
    </select>
  );
}

export function Textarea({ className, ...props }: TextareaHTMLAttributes<HTMLTextAreaElement>) {
  return <textarea className={clsx(CONTROL_BASE, "resize-y py-2.5", className)} {...props} />;
}
