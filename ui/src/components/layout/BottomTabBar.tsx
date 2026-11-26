import clsx from "clsx";
import {
  Bot,
  Box,
  ChevronRight,
  Compass,
  FlaskConical,
  Menu,
  Monitor,
  Moon,
  Settings,
  Sliders,
  Smartphone,
  Square,
  Sun,
} from "lucide-react";
import { type ReactNode, useState } from "react";
import { BottomSheet } from "../common/BottomSheet";
import { Button } from "../common/Button";
import { StatusDot } from "../common/StatusDot";

export type BottomNavId = "flight" | "lab" | "worlds" | "ai" | "phone" | "tuning" | "system";
export type BottomThemeMode = "system" | "dark" | "light";

interface Props {
  nav: BottomNavId;
  onSelectNav: (id: BottomNavId) => void;
  running: boolean;
  targetLabel: string;
  statusDotKind: "ok" | "warn" | "danger" | "idle";
  aiConfigured: boolean;
  readyCapsCount: number;
  totalCapsCount: number;
  theme: BottomThemeMode;
  onCycleTheme: () => void;
  onStopSession: () => void;
}

const PRIMARY_TABS: Array<{ id: BottomNavId; label: string; icon: (active: boolean) => ReactNode }> = [
  { id: "flight", label: "Flight", icon: (active) => <Compass size={20} strokeWidth={active ? 2 : 1.7} /> },
  { id: "lab", label: "Manual", icon: (active) => <FlaskConical size={20} strokeWidth={active ? 2 : 1.7} /> },
  { id: "worlds", label: "Drones", icon: (active) => <Box size={20} strokeWidth={active ? 2 : 1.7} /> },
  { id: "ai", label: "AI", icon: (active) => <Bot size={20} strokeWidth={active ? 2 : 1.7} /> },
];

const MORE_ITEMS: Array<{ id: BottomNavId; label: string; icon: ReactNode }> = [
  { id: "phone", label: "Phone App", icon: <Smartphone size={18} strokeWidth={1.65} /> },
  { id: "tuning", label: "Flight Settings", icon: <Sliders size={18} strokeWidth={1.65} /> },
  { id: "system", label: "System & Logs", icon: <Settings size={18} strokeWidth={1.65} /> },
];

const MORE_IDS = new Set<BottomNavId>(["phone", "tuning", "system"]);

export function BottomTabBar({
  nav,
  onSelectNav,
  running,
  targetLabel,
  statusDotKind,
  aiConfigured,
  readyCapsCount,
  totalCapsCount,
  theme,
  onCycleTheme,
  onStopSession,
}: Props) {
  const [moreOpen, setMoreOpen] = useState(false);
  const isMoreActive = moreOpen || MORE_IDS.has(nav);
  const ThemeIcon = theme === "light" ? Sun : theme === "dark" ? Moon : Monitor;

  return (
    <>
      <nav
        aria-label="Mobile navigation"
        className="fixed right-0 bottom-0 left-0 z-30 border-t border-border bg-surface pb-[env(safe-area-inset-bottom,0px)] md:hidden"
      >
        <div className="grid grid-cols-5">
          {PRIMARY_TABS.map((item) => {
            const isActive = !moreOpen && nav === item.id;
            const showAiWarn = item.id === "ai" && !aiConfigured;
            const showLiveDot = item.id === "worlds" && running;
            return (
              <button
                key={item.id}
                type="button"
                aria-current={isActive ? "page" : undefined}
                onClick={() => {
                  setMoreOpen(false);
                  onSelectNav(item.id);
                }}
                className={clsx(
                  "relative flex min-h-14 flex-col items-center justify-center gap-1 px-1 py-1.5 text-[11px] font-medium transition-colors duration-150 active:scale-[0.96] cursor-pointer",
                  isActive ? "text-primary" : "text-secondary hover:text-primary",
                )}
              >
                <span className="relative">
                  {item.icon(isActive)}
                  {showLiveDot && (
                    <span className="absolute -top-0.5 -right-1 h-2 w-2 rounded-full bg-success" />
                  )}
                  {showAiWarn && (
                    <span className="absolute -top-0.5 -right-1 h-2 w-2 rounded-full bg-warning" />
                  )}
                </span>
                <span>{item.label}</span>
              </button>
            );
          })}

          <button
            type="button"
            aria-expanded={moreOpen}
            aria-haspopup="dialog"
            onClick={() => setMoreOpen(true)}
            className={clsx(
              "flex min-h-14 flex-col items-center justify-center gap-1 px-1 py-1.5 text-[11px] font-medium transition-colors duration-150 active:scale-[0.96] cursor-pointer",
              isMoreActive ? "text-primary" : "text-secondary hover:text-primary",
            )}
          >
            <Menu size={20} strokeWidth={isMoreActive ? 2 : 1.7} />
            <span>More</span>
          </button>
        </div>
      </nav>

      <BottomSheet open={moreOpen} onClose={() => setMoreOpen(false)} title="More">
        <div className="space-y-3">
          <ul className="divide-y divide-border overflow-hidden rounded-[14px] border border-border bg-surface">
            {MORE_ITEMS.map((item) => {
              const isCurrent = nav === item.id;
              return (
                <li key={item.id}>
                  <button
                    type="button"
                    onClick={() => {
                      setMoreOpen(false);
                      onSelectNav(item.id);
                    }}
                    className={clsx(
                      "flex min-h-12 w-full items-center justify-between gap-3 px-4 py-3 text-left text-[14.5px] transition-colors active:bg-hover cursor-pointer",
                      isCurrent ? "bg-hover font-semibold text-primary" : "font-medium text-primary hover:bg-hover/70",
                    )}
                  >
                    <span className="flex min-w-0 items-center gap-3">
                      <span className={isCurrent ? "shrink-0 text-primary" : "shrink-0 text-secondary"}>
                        {item.icon}
                      </span>
                      <span className="truncate">{item.label}</span>
                    </span>
                    <span className="flex shrink-0 items-center gap-2">
                      {item.id === "system" && totalCapsCount > 0 && (
                        <span className="font-mono text-[12px] text-muted tabular-nums">
                          {readyCapsCount}/{totalCapsCount}
                        </span>
                      )}
                      <ChevronRight size={15} strokeWidth={1.6} className="text-muted" aria-hidden="true" />
                    </span>
                  </button>
                </li>
              );
            })}
          </ul>

          <div className="flex flex-wrap items-center justify-between gap-3 rounded-[14px] border border-border bg-surface px-4 py-3">
            <div className="min-w-0 flex-1">
              <StatusDot status={statusDotKind} label={targetLabel} />
            </div>
            <div className="flex shrink-0 items-center gap-2">
              {running && (
                <Button
                  size="sm"
                  variant="secondary"
                  onClick={() => {
                    setMoreOpen(false);
                    onStopSession();
                  }}
                >
                  <Square size={12} /> Stop
                </Button>
              )}
              <Button size="sm" variant="secondary" onClick={onCycleTheme}>
                <ThemeIcon size={14} /> Theme: {theme}
              </Button>
            </div>
          </div>
        </div>
      </BottomSheet>
    </>
  );
}
