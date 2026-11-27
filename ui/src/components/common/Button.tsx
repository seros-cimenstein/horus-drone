import clsx from "clsx";
import { Loader2, Square } from "lucide-react";
import type { ButtonHTMLAttributes, ReactNode } from "react";

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: "primary" | "secondary" | "ghost" | "danger";
  size?: "sm" | "md" | "lg";
  loading?: boolean;
  children: ReactNode;
}

const VARIANT_CLASSES: Record<NonNullable<ButtonProps["variant"]>, string> = {
  primary:
    "bg-accent text-on-accent border-transparent shadow-[0_1px_2px_rgb(0_0_0/0.12)] hover:bg-accent/90 active:bg-accent/85 active:shadow-[inset_0_1px_2px_rgb(0_0_0/0.22)]",
  secondary:
    "bg-raised text-primary border-border shadow-[0_1px_2px_rgb(0_0_0/0.06)] hover:bg-hover hover:border-border-strong active:bg-hover active:shadow-[inset_0_1px_2px_rgb(0_0_0/0.14)]",
  ghost:
    "bg-transparent text-secondary border-transparent hover:bg-hover hover:text-primary active:bg-border/50",
  danger:
    "bg-danger text-white border-transparent shadow-[0_1px_2px_rgb(0_0_0/0.14)] hover:bg-danger/90 active:bg-danger/85 active:shadow-[inset_0_1px_2px_rgb(0_0_0/0.24)]",
};

const SIZE_CLASSES: Record<NonNullable<ButtonProps["size"]>, string> = {
  sm: "h-9 px-3.5 text-[13.5px] rounded-[8px]",
  md: "h-10 px-4 text-[14px] rounded-[8px]",
  lg: "h-11 px-5 text-[14.5px] rounded-[9px]",
};

export function Button({
  variant = "secondary",
  size = "md",
  loading = false,
  disabled,
  className,
  type = "button",
  children,
  ...rest
}: ButtonProps) {
  return (
    <button
      type={type}
      disabled={disabled || loading}
      aria-busy={loading || undefined}
      className={clsx(
        "inline-flex transform-gpu select-none items-center justify-center gap-2 border font-medium whitespace-nowrap transition-[background-color,border-color,color,box-shadow,transform] duration-150 ease-[cubic-bezier(0.2,0,0,1)] cursor-pointer active:scale-[0.97] disabled:pointer-events-none disabled:opacity-45 disabled:active:scale-100",
        VARIANT_CLASSES[variant],
        SIZE_CLASSES[size],
        className,
      )}
      {...rest}
    >
      {loading && <Loader2 size={14} className="shrink-0 animate-spin" />}
      {children}
    </button>
  );
}

export interface StopSessionButtonProps {
  onClick: () => void;
  size?: ButtonProps["size"];
  loading?: boolean;
  disabled?: boolean;
  title?: string;
  className?: string;
  label?: string;
}

export function StopSessionButton({
  onClick,
  size = "sm",
  loading = false,
  disabled = false,
  title,
  className,
  label = "Stop Session",
}: StopSessionButtonProps) {
  return (
    <Button
      size={size}
      variant="danger"
      loading={loading}
      disabled={disabled}
      title={title}
      className={className}
      onClick={onClick}
    >
      {!loading && <Square size={13} />} {loading ? "Stopping…" : label}
    </Button>
  );
}
