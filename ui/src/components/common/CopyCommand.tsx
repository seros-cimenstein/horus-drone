import { Check, Copy } from "lucide-react";
import { useState } from "react";
import { useToast } from "./Toast";

export function CopyCommand({ command, label }: { command: string; label?: string }) {
  const [copied, setCopied] = useState(false);
  const toast = useToast();

  const handleCopy = () => {
    navigator.clipboard
      ?.writeText(command)
      .then(() => {
        setCopied(true);
        toast.info(label ? `Copied ${label}` : "Command copied");
        window.setTimeout(() => setCopied(false), 1500);
      })
      .catch(() => {});
  };

  return (
    <div className="flex items-center justify-between gap-2 rounded-[8px] border border-border bg-canvas px-3 py-2">
      <code className="min-w-0 overflow-x-auto font-mono text-[12px] text-primary whitespace-nowrap">
        {command}
      </code>
      <button
        type="button"
        onClick={handleCopy}
        aria-label={`Copy ${label || command}`}
        className="inline-flex shrink-0 select-none items-center gap-1 rounded-[6px] border border-border bg-raised px-2 py-1 text-[11.5px] font-medium text-secondary transition-colors hover:bg-hover hover:text-primary cursor-pointer"
      >
        {copied ? <Check size={12} className="text-success" /> : <Copy size={12} />}
        {copied ? "Copied" : "Copy"}
      </button>
    </div>
  );
}
