import { Check, Copy, KeyRound, PlugZap, Save } from "lucide-react";
import { useEffect, useState } from "react";
import { horusApi } from "../api";
import type { McpConfigsResponse, StudioSettingsView, StudioStatus } from "../types";
import { Button } from "./common/Button";
import { StatusBanner, type BannerTone } from "./common/StatusBanner";
import { Field, Input, PageHeader, Section, Select } from "./common/Surface";

interface Props {
  token: string;
  studioStatus: StudioStatus | null;
  onUpdated: () => void;
}

export function AiMcpView({ token, studioStatus, onUpdated }: Props) {
  const [view, setView] = useState<StudioSettingsView | null>(null);
  const [provider, setProvider] = useState("auto");
  const [model, setModel] = useState("");
  const [baseUrl, setBaseUrl] = useState("");
  const [effort, setEffort] = useState("");
  const [ompCommand, setOmpCommand] = useState("omp");
  const [ompModel, setOmpModel] = useState("");
  const [ompThinking, setOmpThinking] = useState("");
  const [claudeCommand, setClaudeCommand] = useState("claude");
  const [claudeEffort, setClaudeEffort] = useState("");
  const [apiKeyInput, setApiKeyInput] = useState("");
  const [testResult, setTestResult] = useState("");
  const [mcpConfigs, setMcpConfigs] = useState<McpConfigsResponse | null>(null);
  const [includeMcpToken, setIncludeMcpToken] = useState(false);
  const [copiedId, setCopiedId] = useState("");

  useEffect(() => {
    horusApi
      .getStudioSettings(token)
      .then((v) => {
        setView(v);
        setProvider(v.settings.ai_provider);
        setModel(v.settings.model);
        setBaseUrl(v.settings.base_url);
        setEffort(v.settings.reasoning_effort);
        setOmpCommand(v.settings.omp_command || "omp");
        setOmpModel(v.settings.omp_model || "");
        setOmpThinking(v.settings.omp_thinking || "");
        setClaudeCommand(v.settings.claude_command || "claude");
        setClaudeEffort(v.settings.claude_effort || "");
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

  const saveSettings = async () => {
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
      ...(Object.keys(secrets).length ? { secrets } : {}),
    });
    setView(updated);
    setApiKeyInput("");
    onUpdated();
    setTestResult("Saved.");
  };

  const copyText = (id: string, value: string) => {
    navigator.clipboard
      ?.writeText(value)
      .then(() => {
        setCopiedId(id);
        window.setTimeout(() => setCopiedId(""), 1500);
      })
      .catch(() => {});
  };

  const secretKey = secretKeyForProvider(provider);
  const statusText = testResult || view?.ai_status.detail || "";
  const statusTone: BannerTone =
    testResult.startsWith("Failed") || (!testResult && Boolean(view && !view.ai_status.configured))
      ? "warning"
      : testResult.startsWith("OK") || testResult === "Saved." || (!testResult && Boolean(view?.ai_status.configured))
        ? "success"
        : "info";

  return (
    <div className="mx-auto flex w-full max-w-4xl flex-col gap-8 px-6 py-8">
      <PageHeader
        title="AI & MCP"
        description="Configure the built-in AI pilot provider, or copy MCP client configs for Claude Code, Codex, and Pi/OMP."
      />

      <Section
        title="Built-in AI Provider"
        description="Cloud API keys are stored write-only in chmod 0600 secrets storage and never echoed back."
      >
        <div className="flex flex-col gap-5 rounded-[10px] border border-border bg-surface p-5">
          <div className="grid gap-4 sm:grid-cols-2">
            <Field label="AI Provider">
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
              <Field label="Model" hint="blank = default">
                <Input value={model} onChange={(e) => setModel(e.target.value)} placeholder="gpt-4.1 / gemini-2.5-flash" />
              </Field>
            )}

            {(provider === "custom" || provider === "openai" || provider === "auto") && (
              <Field label="Custom Base URL" hint="optional">
                <Input
                  value={baseUrl}
                  onChange={(e) => setBaseUrl(e.target.value)}
                  placeholder="http://127.0.0.1:11434/v1"
                />
              </Field>
            )}

            {provider !== "omp" && provider !== "claude-code" && (
              <Field label="Reasoning Effort">
                <Select value={effort} onChange={(e) => setEffort(e.target.value)}>
                  <option value="">Default</option>
                  <option value="low">Low</option>
                  <option value="medium">Medium</option>
                  <option value="high">High</option>
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
                <Field label="Binary Command">
                  <Input value={ompCommand} onChange={(e) => setOmpCommand(e.target.value)} />
                </Field>
                <Field label="Model Pattern" hint="optional">
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
              <Field label="Claude Binary">
                <Input value={claudeCommand} onChange={(e) => setClaudeCommand(e.target.value)} />
              </Field>
              <Field label="Effort">
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

          {statusText && (
            <StatusBanner
              variant="inline"
              tone={statusTone}
              title={statusText}
              onDismiss={testResult ? () => setTestResult("") : undefined}
            />
          )}

          <div className="flex flex-wrap justify-end gap-2 border-t border-border pt-4">
            <Button
              onClick={() =>
                void horusApi
                  .testAi(token)
                  .then((r) => setTestResult(`${r.ok ? "OK" : "Failed"} (${r.provider}): ${r.detail}`))
              }
            >
              <PlugZap size={14} /> Test Connection
            </Button>
            <Button variant="primary" onClick={() => void saveSettings()}>
              <Save size={14} /> Save Provider
            </Button>
          </div>
        </div>
      </Section>

      <Section
        title="Connect via MCP Clients"
        description={
          mcpConfigs
            ? `Drone board endpoint: ${mcpConfigs.mcp_url} (${mcpConfigs.live_session ? "active session" : "start a session for a live bearer token"})`
            : "Loading MCP configurations…"
        }
        action={
          <label className="inline-flex cursor-pointer items-center gap-2 text-[13.5px] text-secondary">
            <input
              type="checkbox"
              checked={includeMcpToken}
              onChange={() => setIncludeMcpToken((v) => !v)}
            />
            <KeyRound size={14} /> Include active bearer token
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
  );
}
