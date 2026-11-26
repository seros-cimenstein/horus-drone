import clsx from "clsx";
import type { ReactNode } from "react";

export interface SegmentedControlOption<T extends string> {
  id: T;
  label: string;
  icon?: ReactNode;
  disabled?: boolean;
}

export interface SegmentedControlProps<T extends string> {
  options: readonly SegmentedControlOption<T>[];
  value: T;
  onChange: (value: T) => void;
  ariaLabel?: string;
  size?: "sm" | "md";
  className?: string;
}

export function SegmentedControl<T extends string>({
  options,
  value,
  onChange,
  ariaLabel,
  size = "md",
  className,
}: SegmentedControlProps<T>) {
  return (
    <div
      role="group"
      aria-label={ariaLabel}
      className={clsx(
        "inline-flex flex-wrap items-center gap-1 rounded-[9px] border border-border bg-surface p-1",
        className,
      )}
    >
      {options.map((opt) => {
        const active = opt.id === value;
        return (
          <button
            key={opt.id}
            type="button"
            disabled={opt.disabled}
            aria-pressed={active}
            onClick={() => onChange(opt.id)}
            className={clsx(
              "inline-flex items-center justify-center gap-1.5 rounded-[7px] font-medium whitespace-nowrap transition-colors duration-100 cursor-pointer disabled:pointer-events-none disabled:opacity-40",
              size === "sm" ? "h-8 px-3 text-[13px]" : "h-8 px-3.5 text-[13.5px]",
              active ? "bg-raised text-primary" : "text-secondary hover:text-primary",
            )}
          >
            {opt.icon}
            <span>{opt.label}</span>
          </button>
        );
      })}
    </div>
  );
}
