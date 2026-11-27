import { Save } from "lucide-react";
import { useEffect, useState } from "react";
import { horusApi } from "../api";
import type { CapabilityItem, GrounderStatus } from "../types";
import { Button } from "./common/Button";
import { CapabilityRequirements } from "./common/CapabilityRequirements";
import { CopyCommand } from "./common/CopyCommand";
import { Field, Input, Section, Select, StatusBadge } from "./common/Surface";
import { useToast } from "./common/Toast";

interface Props {
  token: string;
  grounderCap?: CapabilityItem;
  onSaved: () => void;
}

export function GrounderSection({ token, grounderCap, onSaved }: Props) {
  const [grounder, setGrounder] = useState<GrounderStatus | null>(null);
  const [backend, setBackend] = useState("auto");
  const [url, setUrl] = useState("http://127.0.0.1:8100");
  const [busy, setBusy] = useState<"start" | "stop" | "save" | null>(null);
  const toast = useToast();

  useEffect(() => {
    horusApi.getGrounder(token).then(setGrounder).catch(() => {});
    horusApi
      .getStudioSettings(token)
      .then((v) => {
        if (v.settings.grounder_backend) setBackend(v.settings.grounder_backend);
        if (v.settings.grounder_url) setUrl(v.settings.grounder_url);
      })
      .catch(() => {});
  }, [token]);

  const handleService = async (action: "start" | "stop") => {
    setBusy(action);
    try {
      const next =
        action === "start" ? await horusApi.startGrounder(token, backend) : await horusApi.stopGrounder(token);
      setGrounder(next);
      toast.success(action === "start" ? "Visual Grounder started" : "Visual Grounder stopped");
    } catch (e: unknown) {
      toast.error(`Grounder ${action} failed`, e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  };

  const handleSave = async () => {
    setBusy("save");
    try {
      await horusApi.putStudioSettings(token, { grounder_backend: backend, grounder_url: url });
      onSaved();
      toast.success("Visual Grounder settings saved");
    } catch (e: unknown) {
      toast.error("Failed to save Grounder settings", e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  };

  return (
    <Section
      title="Visual Grounder (horus-grounder)"
      description="Finds custom text targets (e.g. 'red backpack') on the camera feed using a Cloud VLM API or an optional local PyTorch model."
      action={
        <StatusBadge tone={grounder?.running ? "ok" : "neutral"}>
          {grounder?.running ? `Running (PID ${grounder.pid}${grounder.backend ? ` · ${grounder.backend}` : ""})` : "Stopped"}
        </StatusBadge>
      }
    >
      <div className="flex flex-col gap-4 rounded-[10px] border border-border bg-surface p-5">
        {grounderCap && <CapabilityRequirements cap={grounderCap} mode="inline" alwaysExpandWhenIncomplete={false} />}

        <div className="grid gap-4 sm:grid-cols-2">
          <Field
            label="Detection Backend"
            hint="Cloud VLM needs no local GPU"
          >
            <Select value={backend} onChange={(e) => setBackend(e.target.value)}>
              <option value="auto">Auto (Cloud VLM or Local PyTorch)</option>
              <option value="cloud-vlm">Cloud VLM (Zero Install, uses API key above)</option>
              <option value="grounding-dino-tiny">Grounding DINO (Local PyTorch)</option>
              <option value="owlv2-base-patch16-ensemble">OWLv2 (Local PyTorch)</option>
            </Select>
          </Field>
          <Field label="Service Endpoint URL" hint="local or remote horus-grounder">
            <Input value={url} onChange={(e) => setUrl(e.target.value)} placeholder="http://127.0.0.1:8100" />
          </Field>
        </div>

        <details className="rounded-[8px] border border-border bg-canvas px-3.5 py-2.5 text-[12.5px] text-secondary">
          <summary className="cursor-pointer select-none font-medium text-primary">
            How to choose &amp; set up Grounder backends
          </summary>
          <div className="mt-2.5 grid gap-3 sm:grid-cols-2">
            <div className="flex flex-col gap-1.5">
              <span className="font-medium text-primary">Cloud VLM (Zero Install)</span>
              <span>Reuses your Gemini, OpenAI, or OpenRouter key from Built-in AI Pilot (0 MB local PyTorch install).</span>
            </div>
            <div className="flex flex-col gap-1.5">
              <span className="font-medium text-primary">Local PyTorch (Offline)</span>
              <span>Install optional Torch/Transformers weights only if you need offline open-vocab grounding:</span>
              <CopyCommand command='pip install -e ".[grounder]"' label="grounder extra" />
            </div>
          </div>
        </details>

        <div className="flex flex-wrap items-center justify-between gap-2 border-t border-border pt-4">
          <div className="flex flex-wrap gap-2">
            <Button
              size="sm"
              variant="primary"
              loading={busy === "start"}
              disabled={busy !== null}
              onClick={() => void handleService("start")}
            >
              {busy === "start" ? "Starting…" : "Start Service"}
            </Button>
            <Button
              size="sm"
              loading={busy === "stop"}
              disabled={busy !== null}
              onClick={() => void handleService("stop")}
            >
              {busy === "stop" ? "Stopping…" : "Stop"}
            </Button>
          </div>
          <Button size="sm" loading={busy === "save"} disabled={busy !== null} onClick={() => void handleSave()}>
            {busy !== "save" && <Save size={13} />} {busy === "save" ? "Saving…" : "Save Grounder Settings"}
          </Button>
        </div>
      </div>
    </Section>
  );
}
