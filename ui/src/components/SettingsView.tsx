import {
  Check,
  Copy,
  Globe,
  KeyRound,
  Monitor,
  Moon,
  PlugZap,
  QrCode,
  Save,
  Smartphone,
  Sun,
  Unplug,
  Wifi,
} from "lucide-react";
import { useEffect, useState } from "react";
import { horusApi } from "../api";
import { buildQrMatrix } from "../qr";
import type {
  CapabilityItem,
  McpConfigsResponse,
  PhoneStatus,
  SettingsTab,
  StudioSettingsView,
  StudioStatus,
} from "../types";
import { GrounderSection } from "./GrounderSection";
import { Button } from "./common/Button";
import { CapabilityRequirements } from "./common/CapabilityRequirements";
import { CopyCommand } from "./common/CopyCommand";
import { Field, Input, PageHeader, Section, Select, SliderField, StatusBadge } from "./common/Surface";
import { useToast } from "./common/Toast";

type ThemeMode = "system" | "dark" | "light";

interface Props {
  focusSection?: SettingsTab;
  focusNonce?: number;
  token: string;
  studioStatus: StudioStatus | null;
  caps: CapabilityItem[];
  theme: ThemeMode;
  onChangeTheme: (theme: ThemeMode) => void;
  onUpdated: () => void;
}

export function SettingsView({
  focusSection,
  focusNonce = 0,
  token,
  studioStatus,
  caps,
  theme,
  onChangeTheme,
  onUpdated,
}: Props) {
  const toast = useToast();

  const [view, setView] = useState<StudioSettingsView | null>(null);

  // General
  const [defaultScene, setDefaultScene] = useState("indoor_sar");
  const [prefer3dView, setPrefer3dView] = useState(false);
  const [savingGeneral, setSavingGeneral] = useState(false);

  // AI & Vision
  const [provider, setProvider] = useState("auto");
  const [model, setModel] = useState("");
  const [baseUrl, setBaseUrl] = useState("");
  const [effort, setEffort] = useState("");
  const [ompCommand, setOmpCommand] = useState("omp");
  const [ompModel, setOmpModel] = useState("");
  const [ompThinking, setOmpThinking] = useState("");
  const [claudeCommand, setClaudeCommand] = useState("claude");
  const [claudeEffort, setClaudeEffort] = useState("");
  const [aiMaxTokens, setAiMaxTokens] = useState(8000);
  const [aiTimeoutS, setAiTimeoutS] = useState(180);
  const [aiMaxTurns, setAiMaxTurns] = useState(40);
  const [apiKeyInput, setApiKeyInput] = useState("");
  const [testingAi, setTestingAi] = useState(false);
  const [savingAi, setSavingAi] = useState(false);
  const [mcpConfigs, setMcpConfigs] = useState<McpConfigsResponse | null>(null);
  const [includeMcpToken, setIncludeMcpToken] = useState(false);
  const [copiedId, setCopiedId] = useState("");

  // Flight & Safety
  const [flightSpeed, setFlightSpeed] = useState(1.2);
  const [verticalSpeed, setVerticalSpeed] = useState(0.5);
  const [turnRate, setTurnRate] = useState(30);
  const [arrivalTolerance, setArrivalTolerance] = useState(0.25);
  const [commandTtl, setCommandTtl] = useState(30);
  const [rthDelay, setRthDelay] = useState(30);
  const [gimbalPitch, setGimbalPitch] = useState(-15);
  const [maxFps, setMaxFps] = useState(5);
  const [openVocabHz, setOpenVocabHz] = useState(0.5);
  const [renderWidthPx, setRenderWidthPx] = useState(640);
  const [pointComp, setPointComp] = useState(true);
  const [savingFlight, setSavingFlight] = useState(false);
  const [delayMs, setDelayMs] = useState(studioStatus?.link_shaper.delay_ms ?? 0);
  const [linkCut, setLinkCut] = useState(studioStatus?.link_shaper.link_cut ?? false);
  const [applyingShaper, setApplyingShaper] = useState<string | null>(null);

  // Phone App
  const [phoneBusy, setPhoneBusy] = useState<"start" | "stop" | null>(null);
  const [phoneMode, setPhoneMode] = useState<"lan" | "tailscale">("lan");
  const [phone, setPhone] = useState<PhoneStatus | null>(null);
  const [copiedUrl, setCopiedUrl] = useState(false);

  useEffect(() => {
    if (!focusSection || focusNonce <= 0) return;
    const target = document.getElementById(`settings-${focusSection}`);
    target?.scrollIntoView({ behavior: "smooth", block: "start" });
  }, [focusSection, focusNonce]);

  useEffect(() => {
    setDelayMs(studioStatus?.link_shaper.delay_ms ?? 0);
    setLinkCut(studioStatus?.link_shaper.link_cut ?? false);
  }, [studioStatus?.link_shaper.delay_ms, studioStatus?.link_shaper.link_cut]);

  useEffect(() => {
    horusApi
      .getStudioSettings(token)
      .then((v) => {
        setView(v);
        const s = v.settings;
        setDefaultScene(s.default_scene || "indoor_sar");
        setPrefer3dView(Boolean(s.prefer_3d_view));
        setProvider(s.ai_provider);
        setModel(s.model);
        setBaseUrl(s.base_url);
        setEffort(s.reasoning_effort);
        setOmpCommand(s.omp_command || "omp");
        setOmpModel(s.omp_model || "");
        setOmpThinking(s.omp_thinking || "");
        setClaudeCommand(s.claude_command || "claude");
        setClaudeEffort(s.claude_effort || "");
        setAiMaxTokens(s.ai_max_tokens ?? 8000);
        setAiTimeoutS(s.ai_timeout_s ?? 180);
        setAiMaxTurns(s.ai_max_turns ?? 40);
        setFlightSpeed(s.flight_speed_m_s ?? 1.2);
        setVerticalSpeed(s.vertical_speed_m_s ?? 0.5);
        setTurnRate(s.turn_rate_deg_s ?? 30);
        setArrivalTolerance(s.arrival_tolerance_m ?? 0.25);
        setCommandTtl(s.command_ttl_s ?? 30);
        setRthDelay(s.link_loss_rth_delay_s ?? 30);
        setGimbalPitch(s.default_gimbal_pitch_deg ?? -15);
        setMaxFps(s.camera_max_fps ?? 5);
        setOpenVocabHz(s.open_vocab_hz ?? 0.5);
        setRenderWidthPx(s.perception_render_width_px ?? 640);
        setPointComp(s.perception_point_compensation ?? true);
      })
      .catch(() => {});

    horusApi
      .getPhoneStatus(token)
      .then((p) => {
        setPhone(p);
        if (p.serve_active) setPhoneMode("tailscale");
      })
      .catch(() => {});
  }, [token]);

  useEffect(() => {
    horusApi.getMcpConfigs(token, includeMcpToken).then(setMcpConfigs).catch(() => {});
  }, [token, includeMcpToken, studioStatus?.mcp_url]);

  const secretKeyForProvider = (p: string): string | null => {
    if (p === "openai") return "OPENAI_API_KEY";
    if (p === "anthropic") return "ANTHROPIC_API_KEY";
    if (p === "gemini") return "GEMINI_API_KEY";
    if (p === "openrouter") return "OPENROUTER_API_KEY";
    if (p === "custom") return "CUSTOM_API_KEY";
    return null;
  };

  const saveGeneral = async () => {
    setSavingGeneral(true);
    try {
      const updated = await horusApi.putStudioSettings(token, {
        default_scene: defaultScene,
        prefer_3d_view: prefer3dView,
        theme: theme === "light" ? "light" : "dark",
      });
      setView(updated);
      onUpdated();
      toast.success("General preferences saved");
    } catch (e: unknown) {
      toast.error("Failed to save general settings", e instanceof Error ? e.message : String(e));
    } finally {
      setSavingGeneral(false);
    }
  };

  const saveAiSettings = async () => {
    setSavingAi(true);
    try {
      const secrets: Record<string, string> = {};
      const sk = secretKeyForProvider(provider);
      if (sk && apiKeyInput.trim()) secrets[sk] = apiKeyInput.trim();
      const updated = await horusApi.putStudioSettings(token, {
        ai_provider: provider,
        model,
        base_url: baseUrl,
        reasoning_effort: effort,
        claude_command: claudeCommand,
        claude_effort: claudeEffort,
        omp_command: ompCommand,
        omp_model: ompModel,
        omp_thinking: ompThinking,
        ai_max_tokens: aiMaxTokens,
        ai_timeout_s: aiTimeoutS,
        ai_max_turns: aiMaxTurns,
        ...(Object.keys(secrets).length ? { secrets } : {}),
      });
      setView(updated);
      setApiKeyInput("");
      onUpdated();
      toast.success("AI settings saved", updated.ai_status.detail);
    } catch (e: unknown) {
      toast.error("Failed to save AI settings", e instanceof Error ? e.message : String(e));
    } finally {
      setSavingAi(false);
    }
  };

  const handleTestAi = async () => {
    setTestingAi(true);
    try {
      const r = await horusApi.testAi(token);
      if (r.ok) toast.success("AI connection verified", `${r.provider}: ${r.detail}`);
      else toast.warning("AI connection test failed", `${r.provider}: ${r.detail}`);
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : String(e);
      toast.error("AI connection test failed", msg);
    } finally {
      setTestingAi(false);
    }
  };

  const saveFlightSettings = async () => {
    setSavingFlight(true);
    try {
      const updated = await horusApi.putStudioSettings(token, {
        flight_speed_m_s: flightSpeed,
        vertical_speed_m_s: verticalSpeed,
        turn_rate_deg_s: turnRate,
        arrival_tolerance_m: arrivalTolerance,
        command_ttl_s: commandTtl,
        link_loss_rth_delay_s: rthDelay,
        default_gimbal_pitch_deg: gimbalPitch,
        camera_max_fps: maxFps,
        open_vocab_hz: openVocabHz,
        perception_render_width_px: renderWidthPx,
        perception_point_compensation: pointComp,
      });
      setView(updated);
      onUpdated();
      toast.success("Flight & Safety settings saved", "Restart an active session to apply changes.");
    } catch (e: unknown) {
      toast.error("Failed to save flight settings", e instanceof Error ? e.message : String(e));
    } finally {
      setSavingFlight(false);
    }
  };

  const applyShaper = async (key: string, nextDelay: number, nextCut: boolean) => {
    setApplyingShaper(key);
    setDelayMs(nextDelay);
    setLinkCut(nextCut);
    try {
      await horusApi.setLinkShaper(token, { delay_ms: nextDelay, jitter_ms: 0, link_cut: nextCut });
      onUpdated();
      if (nextCut) {
        toast.warning("AI link cut active", "Watch HOLDING -> auto_rth while telemetry stays live.");
      } else {
        toast.info(nextDelay === 0 ? "AI link latency restored (0 ms)" : `AI link latency set to ${nextDelay} ms`);
      }
    } catch (e: unknown) {
      toast.error("Link shaper failed", e instanceof Error ? e.message : String(e));
    } finally {
      setApplyingShaper(null);
    }
  };

  const handlePhoneServe = async (action: "start" | "stop") => {
    setPhoneBusy(action);
    try {
      const next = await horusApi.phoneServe(token, action);
      setPhone(next);
      toast.success(
        action === "start" ? "Tailscale Serve started" : "Tailscale Serve stopped",
        next.tailnet_url || undefined,
      );
    } catch (e: unknown) {
      toast.error(`Tailscale ${action} failed`, e instanceof Error ? e.message : String(e));
    } finally {
      setPhoneBusy(null);
    }
  };

  const copyText = (id: string, value: string) => {
    navigator.clipboard
      ?.writeText(value)
      .then(() => {
        setCopiedId(id);
        toast.info("Copied to clipboard");
        window.setTimeout(() => setCopiedId(""), 1500);
      })
      .catch(() => {});
  };

  const agentsCap = caps.find((c) => c.id === "agents");
  const grounderCap = caps.find((c) => c.id === "grounder");
  const phoneCap = caps.find((c) => c.id === "phone_pwa");

  const secretKey = secretKeyForProvider(provider);
  const phoneUrl = phone?.tailnet_url || phone?.lan_url || phone?.local_url || "";
  const qrRows = phoneUrl ? buildQrMatrix(phoneUrl) : [];
  const hasLiveMcpSession = Boolean(mcpConfigs?.live_session);

  return (
    <div className="mx-auto flex w-full max-w-4xl flex-col gap-10 px-6 py-8">
      <PageHeader
        title="Settings"
        description="Configure Studio appearance, AI models, visual grounding, flight & camera behavior, and phone access."
      />

      {/* 1. GENERAL & APPEARANCE */}
      <div id="settings-general" className="scroll-mt-20 flex flex-col gap-6">
        <Section
          title="General & Appearance"
          description="Color theme and default Flight Deck startup behavior."
          action={
            <Button size="sm" variant="primary" loading={savingGeneral} onClick={() => void saveGeneral()}>
              {!savingGeneral && <Save size={13} />} {savingGeneral ? "Saving…" : "Save General"}
            </Button>
          }
        >
          <div className="flex flex-col gap-5 rounded-[10px] border border-border bg-surface p-5">
            <div className="flex flex-col gap-2">
              <span className="text-[13.5px] font-medium text-primary">Theme</span>
              <div className="flex flex-wrap gap-2.5">
                {(
                  [
                    { id: "dark" as const, label: "Dark", icon: Moon },
                    { id: "light" as const, label: "Light", icon: Sun },
                    { id: "system" as const, label: "Match System", icon: Monitor },
                  ]
                ).map((opt) => {
                  const Icon = opt.icon;
                  return (
                    <Button
                      key={opt.id}
                      size="sm"
                      variant={theme === opt.id ? "primary" : "secondary"}
                      onClick={() => onChangeTheme(opt.id)}
                    >
                      <Icon size={14} /> {opt.label}
                    </Button>
                  );
                })}
              </div>
            </div>

            <div className="grid gap-4 border-t border-border pt-4 sm:grid-cols-2">
              <Field label="Default 3D World" hint="preferred built-in scene">
                <Select value={defaultScene} onChange={(e) => setDefaultScene(e.target.value)}>
                  {(studioStatus?.scenes || [
                    { name: "indoor_sar", title: "Indoor SAR Room" },
                    { name: "forest_sar", title: "Forest Search & Rescue" },
                    { name: "urban_plaza", title: "Urban Plaza" },
                    { name: "procedural", title: "Procedural Terrain" },
                  ]).map((sc) => (
                    <option key={sc.name} value={sc.name}>
                      {sc.title} ({sc.name})
                    </option>
                  ))}
                </Select>
              </Field>

              <Field label="Initial Stage View" hint="when launching Built-in 3D">
                <Select
                  value={prefer3dView ? "3d" : "camera"}
                  onChange={(e) => setPrefer3dView(e.target.value === "3d")}
                >
                  <option value="camera">Drone Camera Feed</option>
                  <option value="3d">3D Spectator View (with ToF beams)</option>
                </Select>
              </Field>
            </div>
          </div>
        </Section>
      </div>

      {/* 2. AI & VISION */}
      <div id="settings-ai" className="scroll-mt-20 flex flex-col gap-8">
        <Section
          title="Built-in AI Pilot"
          description="Powers the Mission composer in Flight Deck. API keys are saved write-only in chmod 0600 storage and never echoed back."
          action={
            <StatusBadge tone={view?.ai_status.configured ? "ok" : "neutral"}>
              {view?.ai_status.configured ? `${view.ai_status.provider} ready` : "Not configured"}
            </StatusBadge>
          }
        >
          <div className="flex flex-col gap-5 rounded-[10px] border border-border bg-surface p-5">
            {agentsCap && <CapabilityRequirements cap={agentsCap} mode="inline" />}

            <div className="grid gap-4 sm:grid-cols-2">
              <Field label="AI Provider" hint="cloud API or local CLI">
                <Select value={provider} onChange={(e) => setProvider(e.target.value)}>
                  <option value="auto">Auto-detect (from env or saved keys)</option>
                  <option value="openai">OpenAI</option>
                  <option value="anthropic">Anthropic</option>
                  <option value="gemini">Google Gemini</option>
                  <option value="openrouter">OpenRouter</option>
                  <option value="custom">Ollama / Custom OpenAI URL</option>
                  <option value="omp">OMP / Pi Agent CLI</option>
                  <option value="claude-code">Claude Code CLI</option>
                </Select>
              </Field>

              {provider !== "omp" && provider !== "claude-code" && (
                <Field label="Model" hint="leave blank for provider default">
                  <Input value={model} onChange={(e) => setModel(e.target.value)} placeholder="gpt-4.1 / gemini-2.5-flash" />
                </Field>
              )}

              {(provider === "custom" || provider === "openai" || provider === "auto") && (
                <Field label="Custom Base URL" hint="for Ollama, vLLM, or proxies">
                  <Input
                    value={baseUrl}
                    onChange={(e) => setBaseUrl(e.target.value)}
                    placeholder="http://127.0.0.1:11434/v1"
                  />
                </Field>
              )}

              {provider !== "omp" && provider !== "claude-code" && (
                <Field label="Reasoning Effort" hint="thinking budget for reasoning models">
                  <Select value={effort} onChange={(e) => setEffort(e.target.value)}>
                    <option value="">Default</option>
                    <option value="low">Low (faster)</option>
                    <option value="medium">Medium</option>
                    <option value="high">High (deeper planning)</option>
                  </Select>
                </Field>
              )}
            </div>

            {secretKey && (
              <Field
                label={`API Key (${secretKey})`}
                hint={view?.secrets_hints[secretKey] ? `saved ${view.secrets_hints[secretKey]}` : "write-only 0600"}
              >
                <Input
                  type="password"
                  value={apiKeyInput}
                  onChange={(e) => setApiKeyInput(e.target.value)}
                  placeholder="Paste API key to save or replace"
                />
              </Field>
            )}

            {provider === "omp" && (
              <div className="flex flex-col gap-4 border-t border-border pt-4">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <span className="text-[14px] font-medium text-primary">OMP / Pi CLI Settings</span>
                  <div className="flex gap-2">
                    <Button size="sm" variant={ompCommand === "omp" ? "primary" : "secondary"} onClick={() => setOmpCommand("omp")}>
                      Use omp
                    </Button>
                    <Button size="sm" variant={ompCommand === "pi" ? "primary" : "secondary"} onClick={() => setOmpCommand("pi")}>
                      Use pi
                    </Button>
                  </div>
                </div>
                <div className="grid gap-4 sm:grid-cols-3">
                  <Field label="Binary Command" hint="executable on PATH">
                    <Input value={ompCommand} onChange={(e) => setOmpCommand(e.target.value)} />
                  </Field>
                  <Field label="Model Pattern" hint="optional override">
                    <Input value={ompModel} onChange={(e) => setOmpModel(e.target.value)} placeholder="CLI default" />
                  </Field>
                  <Field label="Thinking Level">
                    <Select value={ompThinking} onChange={(e) => setOmpThinking(e.target.value)}>
                      <option value="">Default</option>
                      <option value="off">off</option>
                      <option value="minimal">minimal</option>
                      <option value="low">low</option>
                      <option value="medium">medium</option>
                      <option value="high">high</option>
                      <option value="xhigh">xhigh</option>
                      <option value="max">max</option>
                    </Select>
                  </Field>
                </div>
              </div>
            )}

            {provider === "claude-code" && (
              <div className="grid gap-4 border-t border-border pt-4 sm:grid-cols-2">
                <Field label="Claude Binary" hint="executable on PATH">
                  <Input value={claudeCommand} onChange={(e) => setClaudeCommand(e.target.value)} />
                </Field>
                <Field label="Effort" hint="Claude reasoning level">
                  <Select value={claudeEffort} onChange={(e) => setClaudeEffort(e.target.value)}>
                    <option value="">Default</option>
                    <option value="low">low</option>
                    <option value="medium">medium</option>
                    <option value="high">high</option>
                    <option value="max">max</option>
                  </Select>
                </Field>
              </div>
            )}

            <details className="rounded-[8px] border border-border bg-canvas px-3.5 py-2.5 text-[13px] text-secondary">
              <summary className="cursor-pointer select-none font-medium text-primary">
                Advanced AI Mission Limits
              </summary>
              <div className="mt-3 grid gap-4 sm:grid-cols-3">
                <Field label="Max Output Tokens" hint="1000 to 32000">
                  <Input
                    type="number"
                    min={1000}
                    max={32000}
                    value={aiMaxTokens}
                    onChange={(e) => setAiMaxTokens(Number(e.target.value))}
                  />
                </Field>
                <Field label="Turn Timeout (s)" hint="30 to 600s">
                  <Input
                    type="number"
                    min={30}
                    max={600}
                    value={aiTimeoutS}
                    onChange={(e) => setAiTimeoutS(Number(e.target.value))}
                  />
                </Field>
                <Field label="Max Agent Turns" hint="5 to 100 tool loops">
                  <Input
                    type="number"
                    min={5}
                    max={100}
                    value={aiMaxTurns}
                    onChange={(e) => setAiMaxTurns(Number(e.target.value))}
                  />
                </Field>
              </div>
            </details>

            <div className="flex flex-wrap justify-end gap-2 border-t border-border pt-4">
              <Button loading={testingAi} onClick={() => void handleTestAi()}>
                {!testingAi && <PlugZap size={14} />} {testingAi ? "Testing…" : "Test Connection"}
              </Button>
              <Button variant="primary" loading={savingAi} onClick={() => void saveAiSettings()}>
                {!savingAi && <Save size={14} />} {savingAi ? "Saving…" : "Save AI Settings"}
              </Button>
            </div>
          </div>
        </Section>

        <GrounderSection token={token} grounderCap={grounderCap} onSaved={onUpdated} />

        <Section
          title="Connect External Agents (MCP)"
          description={
            mcpConfigs
              ? `Let external agents like Claude Code, Codex, or Pi/OMP control the drone directly. Endpoint: ${mcpConfigs.mcp_url} (${hasLiveMcpSession ? "live session active" : "start a session to generate a live bearer token"}).`
              : "Loading MCP configurations…"
          }
          action={
            <label
              className={`inline-flex items-center gap-2 text-[13.5px] ${
                hasLiveMcpSession ? "cursor-pointer text-secondary" : "cursor-not-allowed text-muted opacity-70"
              }`}
              title={hasLiveMcpSession ? "Embed the running session bearer token in snippets" : "Start a flight session first to generate an active bearer token"}
            >
              <input
                type="checkbox"
                disabled={!hasLiveMcpSession}
                checked={hasLiveMcpSession && includeMcpToken}
                onChange={() => setIncludeMcpToken((v) => !v)}
              />
              <KeyRound size={14} />
              <span>
                Include active bearer token
                {!hasLiveMcpSession ? " (start a session first)" : ""}
              </span>
            </label>
          }
        >
          <div className="flex flex-col gap-4">
            {(mcpConfigs?.clients || []).map((c) => (
              <div key={c.id} className="flex flex-col gap-3 rounded-[10px] border border-border bg-surface p-5">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <div>
                    <h3 className="text-[15px] font-semibold text-primary">{c.name}</h3>
                    <p className="mt-0.5 text-[13px] text-secondary">{c.description}</p>
                  </div>
                  <div className="flex gap-1.5">
                    <Button size="sm" onClick={() => copyText(c.id, c.snippet)}>
                      {copiedId === c.id ? <Check size={13} /> : <Copy size={13} />}
                      {copiedId === c.id ? "Copied" : "Config"}
                    </Button>
                    <Button size="sm" onClick={() => copyText(`${c.id}-cli`, c.cli)}>
                      {copiedId === `${c.id}-cli` ? <Check size={13} /> : <Copy size={13} />}
                      {copiedId === `${c.id}-cli` ? "Copied" : "CLI"}
                    </Button>
                  </div>
                </div>
                <pre className="overflow-x-auto rounded-[8px] border border-border bg-canvas p-3 font-mono text-[12px] leading-relaxed text-primary">
                  {c.snippet}
                </pre>
                <pre className="overflow-x-auto rounded-[8px] border border-border bg-canvas p-3 font-mono text-[12px] text-secondary">
                  {c.cli}
                </pre>
              </div>
            ))}
          </div>
        </Section>
      </div>

      {/* 3. FLIGHT, CAMERA & SAFETY */}
      <div id="settings-flight" className="scroll-mt-20 flex flex-col gap-8">
        <Section
          title="Flight, Camera & Perception"
          description="Motion speeds, failsafe timing, gimbal angle, and vision rates. Saved values apply when a session starts or restarts."
          action={
            <Button size="sm" variant="primary" loading={savingFlight} onClick={() => void saveFlightSettings()}>
              {!savingFlight && <Save size={13} />} {savingFlight ? "Saving…" : "Save Flight Settings"}
            </Button>
          }
        >
          <div className="flex flex-col gap-5">
            <div className="grid gap-4 sm:grid-cols-2">
              <SliderField
                label="Cruise Speed"
                value={flightSpeed}
                valueLabel={`${flightSpeed.toFixed(1)} m/s`}
                min={0.3}
                max={5.0}
                step={0.1}
                hint="Horizontal travel speed. Indoor scenes cap automatically to 2.0 m/s for safety."
                onChange={setFlightSpeed}
              />
              <SliderField
                label="AI Link-Loss Auto-RTH Delay"
                value={rthDelay}
                valueLabel={`${Math.round(rthDelay)} s`}
                min={5}
                max={120}
                step={5}
                hint="How long the drone holds in place before returning home if the AI pilot stops responding."
                onChange={setRthDelay}
              />
              <SliderField
                label="Default Gimbal Pitch"
                value={gimbalPitch}
                valueLabel={`${Math.round(gimbalPitch)}°`}
                min={-90}
                max={15}
                step={5}
                hint="Starting camera tilt on takeoff. -90° looks straight down; -15° looks slightly ahead."
                onChange={setGimbalPitch}
              />
              <SliderField
                label="Perception Max FPS"
                value={maxFps}
                valueLabel={`${Math.round(maxFps)} fps`}
                min={1}
                max={15}
                step={1}
                hint="Upper frame rate for built-in YOLO detection and ReID person tracking."
                onChange={setMaxFps}
              />
              <SliderField
                label="Open-Vocab Grounding Rate"
                value={openVocabHz}
                valueLabel={`${openVocabHz.toFixed(1)} Hz`}
                min={0.1}
                max={2.0}
                step={0.1}
                hint="How often custom text labels are checked by the Visual Grounder service."
                onChange={setOpenVocabHz}
              />
            </div>

            <details className="rounded-[10px] border border-border bg-surface px-4 py-3 text-[13px] text-secondary">
              <summary className="cursor-pointer select-none font-medium text-primary">
                Advanced Motion &amp; Perception Limits
              </summary>
              <div className="mt-4 grid gap-4 sm:grid-cols-2">
                <SliderField
                  label="Vertical Speed"
                  value={verticalSpeed}
                  valueLabel={`${verticalSpeed.toFixed(1)} m/s`}
                  min={0.2}
                  max={3.0}
                  step={0.1}
                  hint="Climb and descent speed for takeoff, altitude moves, and landing."
                  onChange={setVerticalSpeed}
                />
                <SliderField
                  label="Turn Rate"
                  value={turnRate}
                  valueLabel={`${Math.round(turnRate)} °/s`}
                  min={10}
                  max={120}
                  step={5}
                  hint="Default yaw rotation speed when turning in place."
                  onChange={setTurnRate}
                />
                <SliderField
                  label="Waypoint Arrival Tolerance"
                  value={arrivalTolerance}
                  valueLabel={`${arrivalTolerance.toFixed(2)} m`}
                  min={0.1}
                  max={2.0}
                  step={0.05}
                  hint="How close the drone must get before a go-to waypoint counts as reached."
                  onChange={setArrivalTolerance}
                />
                <SliderField
                  label="Command Lease TTL"
                  value={commandTtl}
                  valueLabel={`${Math.round(commandTtl)} s`}
                  min={5}
                  max={120}
                  step={5}
                  hint="Maximum duration a single motion command runs before timing out."
                  onChange={setCommandTtl}
                />
                <Field label="Render Frame Width" hint="higher = sharper AI views, more CPU">
                  <Select
                    value={String(renderWidthPx)}
                    onChange={(e) => setRenderWidthPx(Number(e.target.value))}
                  >
                    <option value="320">320 px (Low bandwidth)</option>
                    <option value="480">480 px (Balanced)</option>
                    <option value="640">640 px (Default / Sharpest)</option>
                  </Select>
                </Field>
                <div className="flex flex-col justify-center gap-1.5 rounded-[8px] border border-border bg-canvas px-3.5 py-2.5">
                  <label className="inline-flex cursor-pointer items-center gap-2.5 text-[13.5px] font-medium text-primary">
                    <input
                      type="checkbox"
                      checked={pointComp}
                      onChange={() => setPointComp((v) => !v)}
                    />
                    Point Compensation
                  </label>
                  <span className="text-[12px] text-muted">
                    Compensates target bearings for vehicle pitch/roll tilt during motion.
                  </span>
                </div>
              </div>
            </details>
          </div>
        </Section>

        <Section
          title="AI Link-Loss & Latency Simulation"
          description="Fault testing (applies immediately): injects delay or drops ONLY the AI pilot MCP link. Live telemetry and emergency HOLD / LAND / HOME always stay responsive."
          action={
            <StatusBadge tone={linkCut ? "danger" : delayMs > 0 ? "warn" : "ok"} className="font-mono">
              {linkCut ? "AI Link Cut" : delayMs > 0 ? `${delayMs} ms Delay` : "Normal (0 ms)"}
            </StatusBadge>
          }
        >
          <div className="flex flex-col gap-3 rounded-[10px] border border-border bg-surface p-5">
            <p className="text-[13px] text-secondary">
              Use this during an active session to verify that the drone holds position and triggers auto-RTH after the delay configured above.
            </p>
            <div className="flex flex-wrap items-center gap-2.5">
              {[0, 500, 1500, 3000].map((ms) => (
                <Button
                  key={ms}
                  variant={delayMs === ms && !linkCut ? "primary" : "secondary"}
                  loading={applyingShaper === `delay:${ms}`}
                  onClick={() => void applyShaper(`delay:${ms}`, ms, false)}
                >
                  {ms === 0 ? "0 ms (Normal)" : `${ms} ms Latency`}
                </Button>
              ))}
              <Button
                variant={linkCut ? "danger" : "secondary"}
                loading={applyingShaper === "cut"}
                onClick={() => void applyShaper("cut", delayMs, !linkCut)}
              >
                {applyingShaper !== "cut" && <Unplug size={14} />} {linkCut ? "Restore AI Link" : "Cut AI Link"}
              </Button>
            </div>
          </div>
        </Section>
      </div>

      {/* 4. PHONE APP */}
      <div id="settings-phone" className="scroll-mt-20 flex flex-col gap-6">
        <Section
          title="Phone Companion App"
          description="Open HORUS on your phone over local Wi-Fi or Tailscale HTTPS to monitor live video, view the map, and trigger emergency HOLD / LAND / HOME."
          action={
            <StatusBadge tone={phone?.serve_active || phone?.tls_enabled ? "ok" : "neutral"}>
              {phone?.serve_active ? "Tailnet HTTPS" : phone?.tls_enabled ? "LAN HTTPS" : "Local only"}
            </StatusBadge>
          }
        >
          <div className="flex flex-col gap-5">
            {phoneCap && <CapabilityRequirements cap={phoneCap} mode="inline" alwaysExpandWhenIncomplete={false} />}

            <div className="flex flex-col items-start gap-5 rounded-[10px] border border-border bg-surface p-5 sm:flex-row">
              {qrRows.length > 0 ? (
                <svg
                  className="h-36 w-36 shrink-0 rounded-[8px] border border-border bg-white p-2"
                  viewBox={`0 0 ${qrRows.length + 4} ${qrRows.length + 4}`}
                  aria-label="Phone companion QR code"
                >
                  <rect width={qrRows.length + 4} height={qrRows.length + 4} fill="#ffffff" />
                  {qrRows.map((row, r) =>
                    row.map((dark, c) =>
                      dark ? <rect key={`${r}-${c}`} x={c + 2} y={r + 2} width="1" height="1" fill="#09090b" /> : null,
                    ),
                  )}
                </svg>
              ) : (
                <div className="flex h-36 w-36 shrink-0 items-center justify-center rounded-[8px] border border-border bg-raised text-muted">
                  <QrCode size={28} />
                </div>
              )}
              <div className="flex min-w-0 flex-1 flex-col gap-3">
                <div className="text-[13.5px] text-secondary">
                  {phone?.serve_active
                    ? "Active link: encrypted Tailscale HTTPS (works anywhere over cellular/internet)."
                    : phone?.tls_enabled
                      ? `Active link: Local LAN HTTPS (${phone.lan_ip || "0.0.0.0"}), no Tailscale required.`
                      : "Local loopback only right now. Choose Wi-Fi LAN mode or Tailscale below to open on your phone."}
                </div>
                <pre className="overflow-x-auto rounded-[8px] border border-border bg-canvas p-3 font-mono text-[12.5px] text-primary">
                  {phoneUrl || "Start Studio with `horus studio --phone --lan` for a dedicated phone link."}
                </pre>
                <div className="flex flex-wrap gap-2">
                  <Button
                    disabled={!phoneUrl}
                    onClick={() => {
                      navigator.clipboard?.writeText(phoneUrl).then(() => {
                        setCopiedUrl(true);
                        toast.info("Phone URL copied");
                        window.setTimeout(() => setCopiedUrl(false), 1500);
                      });
                    }}
                  >
                    {copiedUrl ? <Check size={14} /> : <Copy size={14} />} {copiedUrl ? "Copied" : "Copy Active URL"}
                  </Button>
                </div>
              </div>
            </div>

            <div className="flex flex-col gap-4 rounded-[10px] border border-border bg-surface p-5">
              <div className="flex flex-wrap gap-2" role="tablist" aria-label="Phone connection mode">
                <Button
                  size="sm"
                  variant={phoneMode === "lan" ? "primary" : "secondary"}
                  onClick={() => setPhoneMode("lan")}
                >
                  <Wifi size={14} /> Local Wi-Fi / Hotspot (No Tailscale)
                </Button>
                <Button
                  size="sm"
                  variant={phoneMode === "tailscale" ? "primary" : "secondary"}
                  onClick={() => setPhoneMode("tailscale")}
                >
                  <Globe size={14} /> Remote Access (Tailscale HTTPS)
                </Button>
              </div>

              {phoneMode === "lan" ? (
                <div className="flex flex-col gap-3 border-t border-border pt-4 text-[13.5px] text-secondary">
                  <p className="text-primary">
                    Best for field hotspots or home Wi-Fi: uses a built-in self-signed HTTPS certificate and works completely offline without Tailscale.
                  </p>
                  <ol className="list-decimal space-y-2 pl-5">
                    <li>Connect your laptop and phone to the same Wi-Fi network or mobile hotspot.</li>
                    <li>
                      Restart HORUS Studio with LAN HTTPS enabled:
                      <div className="mt-1.5">
                        <CopyCommand command="horus studio --phone --lan" label="LAN Phone command" />
                      </div>
                    </li>
                    <li>
                      Scan the QR code above on your phone, accept the one-time browser security prompt for the local self-signed cert, and tap <strong className="text-primary">Add to Home Screen</strong>.
                    </li>
                  </ol>
                  <div className="text-[12.5px] text-muted">
                    Current status:{" "}
                    <strong className="text-primary">
                      {phone?.tls_enabled ? `LAN HTTPS active on ${phone.lan_ip || "0.0.0.0"}` : "Not started with --lan"}
                    </strong>
                  </div>
                </div>
              ) : (
                <div className="flex flex-col gap-3 border-t border-border pt-4 text-[13.5px] text-secondary">
                  <p className="text-primary">
                    Best when your phone is on cellular or a different network: routes strictly over your private Tailscale tailnet with trusted HTTPS certs.
                  </p>
                  <ol className="list-decimal space-y-2 pl-5">
                    <li>Install Tailscale on both your laptop and phone and sign into the same tailnet.</li>
                    <li>
                      Enable <strong className="text-primary">MagicDNS</strong> and <strong className="text-primary">HTTPS Certificates</strong> in your Tailscale admin console.
                    </li>
                    <li>
                      Start Studio with <code className="font-mono text-primary">horus studio --phone</code> and click <strong className="text-primary">Start Tailscale Serve</strong> below.
                    </li>
                  </ol>
                  <div className="text-[12.5px] text-muted">
                    Tailscale CLI:{" "}
                    <strong className="text-primary">{phone?.tailscale_installed ? "installed" : "not installed"}</strong>
                    {" · "}HTTPS certs:{" "}
                    <strong className="text-primary">{phone?.tailscale_certs ? "enabled" : "disabled"}</strong>
                    {phone?.tailscale_dns ? ` · DNS: ${phone.tailscale_dns}` : ""}
                  </div>
                  <div className="flex flex-wrap gap-2">
                    <Button
                      variant="primary"
                      loading={phoneBusy === "start"}
                      disabled={phoneBusy !== null}
                      onClick={() => void handlePhoneServe("start")}
                    >
                      {phoneBusy !== "start" && <Smartphone size={14} />}
                      {phoneBusy === "start" ? "Starting…" : "Start Tailscale Serve"}
                    </Button>
                    <Button
                      loading={phoneBusy === "stop"}
                      disabled={phoneBusy !== null}
                      onClick={() => void handlePhoneServe("stop")}
                    >
                      {phoneBusy === "stop" ? "Stopping…" : "Stop Serve"}
                    </Button>
                  </div>
                </div>
              )}
            </div>
          </div>
        </Section>
      </div>
    </div>
  );
}
