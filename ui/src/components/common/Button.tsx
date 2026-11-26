import clsx from "clsx";
import { Square } from "lucide-react";
import type { ButtonHTMLAttributes, ReactNode } from "react";

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: "primary" | "secondary" | "ghost" | "danger";
  size?: "sm" | "md" | "lg";
  children: ReactNode;
}

const VARIANT_CLASSES: Record<NonNullable<ButtonProps["variant"]>, string> = {
  primary: "bg-accent text-on-accent border-transparent hover:opacity-90",
  secondary: "bg-raised text-primary border-border hover:bg-hover hover:border-border-strong",
  ghost: "bg-transparent text-secondary border-transparent hover:bg-hover hover:text-primary",
  danger: "bg-danger text-white border-transparent hover:opacity-90",
};

const SIZE_CLASSES: Record<NonNullable<ButtonProps["size"]>, string> = {
  sm: "h-9 px-3.5 text-[13.5px] rounded-[8px]",
  md: "h-10 px-4 text-[14px] rounded-[8px]",
  lg: "h-11 px-5 text-[14.5px] rounded-[9px]",
};

export function Button({
  variant = "secondary",
  size = "md",
  className,
  type = "button",
  children,
  ...rest
}: ButtonProps) {
  return (
    <button
      type={type}
      className={clsx(
        "inline-flex items-center justify-center gap-2 border font-medium whitespace-nowrap transition-colors duration-100 cursor-pointer disabled:pointer-events-none disabled:opacity-40",
        VARIANT_CLASSES[variant],
        SIZE_CLASSES[size],
        className,
      )}
      {...rest}
    >
      {children}
    </button>
  );
}

export interface StopSessionButtonProps {
  onClick: () => void;
  size?: ButtonProps["size"];
  className?: string;
  label?: string;
}

export function StopSessionButton({
  onClick,
  size = "sm",
  className,
  label = "Stop Session",
}: StopSessionButtonProps) {
  return (
    <Button size={size} variant="danger" className={className} onClick={onClick}>
      <Square size={13} /> {label}
    </Button>
  );
}
