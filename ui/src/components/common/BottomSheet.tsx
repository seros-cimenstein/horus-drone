import clsx from "clsx";
import { X } from "lucide-react";
import { type ReactNode, useCallback, useEffect, useId, useRef, useState } from "react";

export interface BottomSheetProps {
  open: boolean;
  onClose: () => void;
  title: string;
  description?: string;
  className?: string;
  children: ReactNode;
}

const EXIT_DURATION_MS = 220;
const DISMISS_VELOCITY_PX_MS = 0.36;

function prefersReducedMotion(): boolean {
  return (
    typeof window !== "undefined" &&
    Boolean(window.matchMedia?.("(prefers-reduced-motion: reduce)").matches)
  );
}

interface GestureState {
  active: boolean;
  dragging: boolean;
  fromHandle: boolean;
  pointerId: number;
  startX: number;
  startY: number;
  currentY: number;
  axis: "none" | "x" | "y";
  captured: boolean;
  samples: Array<{ y: number; t: number }>;
}

const INITIAL_GESTURE: GestureState = {
  active: false,
  dragging: false,
  fromHandle: false,
  pointerId: -1,
  startX: 0,
  startY: 0,
  currentY: 0,
  axis: "none",
  captured: false,
  samples: [],
};

/**
 * Gesture-enabled mobile Bottom Sheet built on top-layer native <dialog>.
 * Supports dragging down from the handle or scrolled-to-top body.
 */
export function BottomSheet({
  open,
  onClose,
  title,
  description,
  className,
  children,
}: BottomSheetProps) {
  const titleId = useId();
  const dialogRef = useRef<HTMLDialogElement | null>(null);
  const backdropRef = useRef<HTMLDivElement | null>(null);
  const sheetRef = useRef<HTMLDivElement | null>(null);
  const bodyRef = useRef<HTMLDivElement | null>(null);

  const [rendered, setRendered] = useState(open);
  const closingRef = useRef(false);
  const closeTimerRef = useRef<number | null>(null);
  const pressedBackdropRef = useRef(false);
  const touchActiveRef = useRef(false);
  const suppressClickUntilRef = useRef(0);
  const dragRef = useRef<GestureState>({ ...INITIAL_GESTURE });

  const clearCloseTimer = useCallback(() => {
    if (closeTimerRef.current !== null) {
      window.clearTimeout(closeTimerRef.current);
      closeTimerRef.current = null;
    }
  }, []);

  const applySheetStyles = useCallback((offsetY: number, animate: boolean, durationMs = 260) => {
    const sheet = sheetRef.current;
    const backdrop = backdropRef.current;
    if (!sheet || !backdrop) return;

    const reduced = prefersReducedMotion();
    const sheetH = Math.max(240, sheet.getBoundingClientRect().height || 360);
    const progress = Math.max(0, Math.min(1, offsetY / sheetH));

    if (animate && !reduced) {
      sheet.style.transition = `transform ${durationMs}ms cubic-bezier(0.22, 1, 0.36, 1)`;
      backdrop.style.transition = `opacity ${Math.min(durationMs, 220)}ms ease-out`;
    } else {
      sheet.style.transition = "none";
      backdrop.style.transition = "none";
    }

    sheet.style.transform = reduced ? "translate3d(0, 0, 0)" : `translate3d(0, ${offsetY}px, 0)`;
    backdrop.style.opacity = String(Math.max(0, 1 - progress * 0.9));
  }, []);

  const finishClose = useCallback(() => {
    clearCloseTimer();
    closingRef.current = false;
    setRendered(false);
    const dialog = dialogRef.current;
    if (dialog && (dialog.open || dialog.hasAttribute("open"))) {
      if (typeof dialog.close === "function") {
        dialog.close();
      } else {
        dialog.removeAttribute("open");
      }
    }
  }, [clearCloseTimer]);

  const animateOutAndClose = useCallback(
    (notifyParent: boolean) => {
      if (closingRef.current) return;
      if (prefersReducedMotion()) {
        finishClose();
        if (notifyParent) onClose();
        return;
      }

      closingRef.current = true;
      const sheet = sheetRef.current;
      const backdrop = backdropRef.current;
      if (sheet && backdrop) {
        const sheetH = Math.max(280, sheet.getBoundingClientRect().height || 360);
        sheet.style.transition = `transform ${EXIT_DURATION_MS}ms cubic-bezier(0.32, 0.72, 0, 1)`;
        backdrop.style.transition = `opacity ${EXIT_DURATION_MS}ms ease-out`;
        sheet.style.transform = `translate3d(0, ${sheetH + 24}px, 0)`;
        backdrop.style.opacity = "0";
      }

      if (notifyParent) onClose();
      clearCloseTimer();
      closeTimerRef.current = window.setTimeout(finishClose, EXIT_DURATION_MS + 20);
    },
    [clearCloseTimer, finishClose, onClose],
  );

  const settleGesture = useCallback(
    (cancelled: boolean) => {
      const ds = dragRef.current;
      if (!ds.dragging) return;
      ds.dragging = false;
      suppressClickUntilRef.current = Date.now() + 350;

      if (cancelled) {
        applySheetStyles(0, true);
        return;
      }

      const sheetH = sheetRef.current?.getBoundingClientRect().height || 360;
      const firstSample = ds.samples[0];
      const lastSample = ds.samples[ds.samples.length - 1];
      const dt = firstSample && lastSample ? Math.max(1, lastSample.t - firstSample.t) : 100;
      const velocity = firstSample && lastSample ? (lastSample.y - firstSample.y) / dt : 0;

      const dismissThreshold = Math.min(110, sheetH * 0.26);
      if (ds.currentY >= dismissThreshold || (ds.currentY > 28 && velocity > DISMISS_VELOCITY_PX_MS)) {
        animateOutAndClose(true);
      } else {
        applySheetStyles(0, true, 280);
      }
    },
    [animateOutAndClose, applySheetStyles],
  );

  useEffect(() => {
    if (open) {
      clearCloseTimer();
      closingRef.current = false;
      setRendered(true);
      return;
    }
    if (rendered && !closingRef.current) {
      animateOutAndClose(false);
    }
  }, [animateOutAndClose, clearCloseTimer, open, rendered]);

  useEffect(() => {
    if (!rendered) return;
    const dialog = dialogRef.current;
    if (!dialog) return;

    if (!dialog.open) {
      if (typeof dialog.showModal === "function") {
        dialog.showModal();
      } else {
        dialog.setAttribute("open", "");
      }
    }

    const sheet = sheetRef.current;
    const backdrop = backdropRef.current;
    if (!sheet || !backdrop) return;

    if (prefersReducedMotion()) {
      sheet.style.transition = "none";
      sheet.style.transform = "translate3d(0, 0, 0)";
      backdrop.style.transition = "none";
      backdrop.style.opacity = "1";
      return;
    }

    sheet.style.transition = "none";
    sheet.style.transform = "translate3d(0, 100%, 0)";
    backdrop.style.transition = "none";
    backdrop.style.opacity = "0";

    const raf1 = window.requestAnimationFrame(() => {
      void sheet.offsetHeight;
      sheet.style.transition = "transform 300ms cubic-bezier(0.22, 1, 0.36, 1)";
      backdrop.style.transition = "opacity 220ms ease-out";
      sheet.style.transform = "translate3d(0, 0, 0)";
      backdrop.style.opacity = "1";
    });

    return () => window.cancelAnimationFrame(raf1);
  }, [rendered]);

  useEffect(() => {
    if (!rendered) return;
    const sheet = sheetRef.current;
    if (!sheet) return;

    const onTouchStart = (e: TouchEvent) => {
      if (closingRef.current || e.touches.length !== 1) return;
      const touch = e.touches[0]!;
      const target = e.target as HTMLElement | null;
      const fromHandle = Boolean(target?.closest('[data-sheet-handle="true"]'));
      touchActiveRef.current = true;

      dragRef.current = {
        active: true,
        dragging: false,
        fromHandle,
        pointerId: touch.identifier,
        startX: touch.clientX,
        startY: touch.clientY,
        currentY: 0,
        axis: "none",
        captured: false,
        samples: [{ y: touch.clientY, t: performance.now() }],
      };
    };

    const onTouchMove = (e: TouchEvent) => {
      const ds = dragRef.current;
      if (!ds.active || e.touches.length !== 1) return;
      const touch = e.touches[0]!;
      const dx = touch.clientX - ds.startX;
      const dy = touch.clientY - ds.startY;

      if (ds.axis === "none") {
        if (Math.abs(dx) < 5 && Math.abs(dy) < 5) return;
        if (Math.abs(dx) > Math.abs(dy)) {
          ds.active = false;
          return;
        }
        const scrollTop = bodyRef.current?.scrollTop ?? 0;
        if (!ds.fromHandle && (dy < 0 || scrollTop > 1)) {
          ds.active = false;
          return;
        }
        ds.axis = "y";
        ds.dragging = true;
      }

      if (!ds.dragging) return;
      if (e.cancelable) e.preventDefault();

      const offsetY = dy >= 0 ? dy : dy * 0.14;
      ds.currentY = offsetY;

      const now = performance.now();
      ds.samples.push({ y: touch.clientY, t: now });
      while (ds.samples.length > 2 && now - ds.samples[0]!.t > 90) {
        ds.samples.shift();
      }

      applySheetStyles(offsetY, false);
    };

    const onTouchEnd = () => {
      touchActiveRef.current = false;
      const ds = dragRef.current;
      if (!ds.active) return;
      ds.active = false;
      settleGesture(false);
    };

    const onTouchCancel = () => {
      touchActiveRef.current = false;
      const ds = dragRef.current;
      if (!ds.active) return;
      ds.active = false;
      settleGesture(true);
    };

    sheet.addEventListener("touchstart", onTouchStart, { passive: true });
    sheet.addEventListener("touchmove", onTouchMove, { passive: false });
    sheet.addEventListener("touchend", onTouchEnd, { passive: true });
    sheet.addEventListener("touchcancel", onTouchCancel, { passive: true });

    return () => {
      sheet.removeEventListener("touchstart", onTouchStart);
      sheet.removeEventListener("touchmove", onTouchMove);
      sheet.removeEventListener("touchend", onTouchEnd);
      sheet.removeEventListener("touchcancel", onTouchCancel);
    };
  }, [applySheetStyles, rendered, settleGesture]);

  useEffect(() => clearCloseTimer, [clearCloseTimer]);

  const handlePointerDown = (e: React.PointerEvent<HTMLDivElement>) => {
    if (closingRef.current || touchActiveRef.current) return;
    if (e.pointerType === "mouse" && e.button !== 0) return;

    const target = e.target as HTMLElement;
    if (target.closest('[data-sheet-close="true"]')) return;

    const fromHandle = Boolean(target.closest('[data-sheet-handle="true"]'));
    const scrollTop = bodyRef.current?.scrollTop ?? 0;
    if (!fromHandle && scrollTop > 1) return;

    dragRef.current = {
      active: true,
      dragging: false,
      fromHandle,
      pointerId: e.pointerId,
      startX: e.clientX,
      startY: e.clientY,
      currentY: 0,
      axis: "none",
      captured: false,
      samples: [{ y: e.clientY, t: performance.now() }],
    };
  };

  const handlePointerMove = (e: React.PointerEvent<HTMLDivElement>) => {
    if (touchActiveRef.current) return;
    const ds = dragRef.current;
    if (!ds.active || ds.pointerId !== e.pointerId) return;

    const dx = e.clientX - ds.startX;
    const dy = e.clientY - ds.startY;

    if (ds.axis === "none") {
      if (Math.abs(dx) < 6 && Math.abs(dy) < 6) return;
      if (Math.abs(dx) > Math.abs(dy)) {
        ds.active = false;
        return;
      }
      if (!ds.fromHandle && dy < 0) {
        ds.active = false;
        return;
      }
      ds.axis = "y";
      ds.dragging = true;
      if (!ds.captured && typeof e.currentTarget.setPointerCapture === "function") {
        try {
          e.currentTarget.setPointerCapture(e.pointerId);
          ds.captured = true;
        } catch {
          // ignore
        }
      }
    }

    if (!ds.dragging) return;

    const offsetY = dy >= 0 ? dy : dy * 0.14;
    ds.currentY = offsetY;

    const now = performance.now();
    ds.samples.push({ y: e.clientY, t: now });
    while (ds.samples.length > 2 && now - ds.samples[0]!.t > 90) {
      ds.samples.shift();
    }

    applySheetStyles(offsetY, false);
  };

  const handlePointerEnd = (e: React.PointerEvent<HTMLDivElement>, cancelled = false) => {
    if (touchActiveRef.current) return;
    const ds = dragRef.current;
    if (!ds.active || ds.pointerId !== e.pointerId) return;
    ds.active = false;

    if (
      ds.captured &&
      typeof e.currentTarget.hasPointerCapture === "function" &&
      e.currentTarget.hasPointerCapture(e.pointerId)
    ) {
      e.currentTarget.releasePointerCapture(e.pointerId);
    }

    settleGesture(cancelled);
  };

  return (
    <dialog
      ref={dialogRef}
      aria-labelledby={titleId}
      {...({ closedby: "any" } as Record<string, string>)}
      onCancel={(e) => {
        e.preventDefault();
        animateOutAndClose(true);
      }}
      className="horus-sheet m-0 h-dvh max-h-none w-screen max-w-none overflow-hidden border-0 bg-transparent p-0 text-primary shadow-none open:flex open:flex-col"
    >
      {rendered && (
        <div className="relative flex h-full w-full flex-col justify-end">
          <div
            ref={backdropRef}
            aria-hidden="true"
            data-sheet-backdrop="true"
            onPointerDown={(e) => {
              if (e.target === e.currentTarget) {
                pressedBackdropRef.current = true;
              }
            }}
            onPointerUp={(e) => {
              if (pressedBackdropRef.current && e.target === e.currentTarget) {
                pressedBackdropRef.current = false;
                animateOutAndClose(true);
              } else {
                pressedBackdropRef.current = false;
              }
            }}
            className="fixed inset-0 bg-black/60 backdrop-blur-[2px] will-change-[opacity]"
          />

          <div
            ref={sheetRef}
            onPointerDown={handlePointerDown}
            onPointerMove={handlePointerMove}
            onPointerUp={(e) => handlePointerEnd(e, false)}
            onPointerCancel={(e) => handlePointerEnd(e, true)}
            onDragStart={(e) => e.preventDefault()}
            onClickCapture={(e) => {
              if (Date.now() < suppressClickUntilRef.current) {
                e.preventDefault();
                e.stopPropagation();
              }
            }}
            className={clsx(
              "relative z-10 mx-auto flex max-h-[85dvh] w-full max-w-[540px] flex-col rounded-t-[20px] border-x border-t border-border-strong/80 bg-raised text-primary shadow-2xl select-none will-change-transform",
              className,
            )}
          >
            <div data-sheet-handle="true" className="shrink-0 touch-none">
              <div className="flex cursor-grab justify-center pt-2.5 pb-1.5 active:cursor-grabbing">
                <span aria-hidden="true" className="h-1 w-9 rounded-full bg-border-strong" />
              </div>

              <div className="flex items-center justify-between gap-3 px-5 pt-1 pb-3">
                <div className="min-w-0">
                  <h2
                    id={titleId}
                    className="truncate text-[16px] leading-[22px] font-semibold tracking-tight text-primary"
                  >
                    {title}
                  </h2>
                  {description && (
                    <p className="mt-0.5 truncate text-[12.5px] text-muted">{description}</p>
                  )}
                </div>

                <button
                  type="button"
                  data-sheet-close="true"
                  onClick={() => animateOutAndClose(true)}
                  aria-label="Close sheet"
                  className="inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-surface text-secondary transition-colors hover:bg-hover hover:text-primary active:scale-95 cursor-pointer"
                >
                  <X size={16} strokeWidth={1.75} aria-hidden="true" />
                </button>
              </div>
            </div>

            <div
              ref={bodyRef}
              className="min-h-0 flex-1 overflow-y-auto overscroll-contain px-4 pt-0.5 pb-[max(1.25rem,env(safe-area-inset-bottom,0px))]"
            >
              {children}
            </div>
          </div>
        </div>
      )}
    </dialog>
  );
}
