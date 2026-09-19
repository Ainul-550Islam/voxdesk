"use client";

import { useEffect, useState } from "react";
import { api, ApiError } from "@/lib/api";
import { useTenantId } from "@/lib/hooks";
import KvList from "@/components/kv-list";
import type { AgentConfig, LlmPreset } from "@/lib/types";

export default function AgentPage() {
  const tenantId = useTenantId();
  const [config, setConfig] = useState<AgentConfig | null>(null);
  const [presets, setPresets] = useState<LlmPreset[]>([]);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!tenantId) return;
    let cancelled = false;
    Promise.all([api.agentConfig(tenantId), api.llmPresets()])
      .then(([configRes, presetRows]) => {
        if (cancelled) return;
        setConfig(configRes);
        setPresets(presetRows);
      })
      .catch((err) => {
        if (!cancelled) {
          setError(err instanceof ApiError ? err.message : "Failed to load agent settings");
        }
      });
    return () => {
      cancelled = true;
    };
  }, [tenantId]);

  if (error) return <div className="error-banner">{error}</div>;
  if (!config) return <div className="loading">Loading…</div>;

  return (
    <>
      <header className="page-head">
        <h1>Agent settings</h1>
        <p className="muted">
          {config.agent_name} · {config.twilio_number}
        </p>
      </header>

      <KvList
        title="Personality"
        entries={{
          agent_name: config.agent_name,
          greeting: config.greeting,
          system_prompt_extra: config.system_prompt_extra,
        }}
      />

      <KvList
        title="Model"
        entries={{
          llm_preset: config.llm_preset,
          llm_provider: config.llm_provider,
          llm_model: config.llm_model,
          temperature: config.temperature,
        }}
      />

      <KvList
        title="Voice & speech"
        entries={{
          voice_id: config.voice_id,
          language: config.language,
          humanize: config.humanize,
          vad_stop_secs: config.vad_stop_secs,
          speech_speed: config.speech_speed,
        }}
      />

      <KvList
        title="Call behaviour"
        entries={{
          timezone: config.timezone,
          business_open: config.business_open,
          business_close: config.business_close,
          appointment_minutes: config.appointment_minutes,
          escalation_number: config.escalation_number,
          notify_sms_number: config.notify_sms_number,
        }}
      />

      <KvList
        title="Compliance & channels"
        entries={{
          record_calls: config.record_calls,
          recording_disclaimer: config.recording_disclaimer,
          sms_enabled: config.sms_enabled,
          whatsapp_enabled: config.whatsapp_enabled,
          ivr_enabled: config.ivr_enabled,
        }}
      />

      <section className="card">
        <h2>LLM presets</h2>
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Preset</th>
                <th>Provider</th>
                <th>Model</th>
                <th>Latency</th>
                <th>Notes</th>
              </tr>
            </thead>
            <tbody>
              {presets.map((preset) => (
                <tr key={preset.key}>
                  <td>{preset.brand}</td>
                  <td>{preset.provider}</td>
                  <td>{preset.model}</td>
                  <td>{preset.latency_ms} ms</td>
                  <td>{preset.notes}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>
    </>
  );
}
