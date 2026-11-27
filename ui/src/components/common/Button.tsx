import clsx from "clsx";
import { Loader2, Square } from "lucide-react";
import { type ButtonHTMLAttributes, type PointerEvent, type ReactNode, useEffect, useRef, useState } from "react";

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: "primary" | "secondary" | "ghost" | "danger";
  size?: "sm" | "md" | "lg";
  loading?: boolean;
  children: ReactNode;
}

interface Ripple {
  id: number;
  x: number;
  y: number;
  size: number;
}

const VARIANT_CLASSES: Record<NonNullable<ButtonProps["variant"]>, string> = {
  primary:
    "bg-accent text-on-accent border-transparent shadow-[0_1px_2px_rgb(0_0_0/0.1)] hover:bg-accent/90 active:bg-accent/80 active:shadow-none",
  secondary:
    "bg-raised text-primary border-border shadow-[0_1px_2px_rgb(0_0_0/0.06)] hover:bg-hover hover:border-border-strong active:bg-border/60 active:shadow-none",
  ghost:
    "bg-transparent text-secondary border-transparent hover:bg-hover hover:text-primary active:bg-border/50",
  danger:
    "bg-danger text-white border-transparent shadow-[0_1px_2px_rgb(0_0_0/0.12)] hover:bg-danger/90 active:bg-danger/80 active:shadow-none",
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
  onPointerDown,
  children,
  ...rest
}: ButtonProps) {
  const [ripples, setRipples] = useState<Ripple[]>([]);
  const nextIdRef = useRef(0);
  const timeoutsRef = useRef<number[]>([]);

  useEffect(() => {
    return () => {
      for (const t of timeoutsRef.current) window.clearTimeout(t);
    };
  }, []);

  const handlePointerDown = (e: PointerEvent<HTMLButtonElement>) => {
    onPointerDown?.(e);
    if (disabled || loading || e.button > 0) return;
    const rect = e.currentTarget.getBoundingClientRect();
    const sizePx = Math.max(rect.width, rect.height) * 2.2;
    const x = e.clientX ? e.clientX - rect.left : rect.width / 2;
    const y = e.clientY ? e.clientY - rect.top : rect.height / 2;
    const id = ++nextIdRef.current;
    setRipples((prev) => [...prev.slice(-2), { id, x, y, size: sizePx }]);
    const timer = window.setTimeout(() => {
      setRipples((prev) => prev.filter((r) => r.id !== id));
    }, 450);
    timeoutsRef.current.push(timer);
  };

  return (
    <button
      type={type}
      disabled={disabled || loading}
      aria-busy={loading || undefined}
      onPointerDown={handlePointerDown}
      className={clsx(
        "relative inline-flex select-none items-center justify-center gap-2 overflow-hidden border font-medium whitespace-nowrap transition-[background-color,border-color,color,box-shadow,transform] duration-150 ease-out cursor-pointer active:scale-[0.98] disabled:pointer-events-none disabled:opacity-45 disabled:active:scale-100",
        VARIANT_CLASSES[variant],
        SIZE_CLASSES[size],
        className,
      )}
      {...rest}
    >
      {ripples.map((r) => (
        <span
          key={r.id}
          aria-hidden="true"
          onAnimationEnd={() => setRipples((prev) => prev.filter((item) => item.id !== r.id))}
          style={{
            left: `${r.x}px`,
            top: `${r.y}px`,
            width: `${r.size}px`,
            height: `${r.size}px`,
          }}
          className="horus-btn-ripple pointer-events-none absolute rounded-full bg-current"
        />
      ))}
      {loading && <Loader2 size={14} className="shrink-0 animate-spin" />}
      {children}
    </button>
  );
}

export interface StopSessionButtonProps {
  onClick: () => void;
  size?: ButtonProps["size"];
  loading?: boolean;
  className?: string;
  label?: string;
}

export function StopSessionButton({
  onClick,
  size = "sm",
  loading = false,
  className,
  label = "Stop Session",
}: StopSessionButtonProps) {
  return (
    <Button size={size} variant="danger" loading={loading} className={className} onClick={onClick}>
      {!loading && <Square size={13} />} {loading ? "Stopping…" : label}
    </Button>
  );
}
