import { useEffect, useState } from "react";
import type { FormEvent } from "react";
import { api, useData } from "../lib/api";
import type {
  AgentEngine,
  ModelChoice,
  ModelSettings,
} from "../lib/types";
import { Select } from "./Select";
import { ErrorBox, Loading } from "./common";
import type { ModelRole } from "../lib/types";
import { ModelReference } from "./ModelReference";
import { modelName, modelRoles } from "./modelPresentation";
import { ManagerSettings } from "./ManagerSettings";

export function ModelSelection({ showManagerSettings = false }: { showManagerSettings?: boolean }) {
  const { data, error, loading, reload } = useData<ModelSettings>(
    "/settings/selection",
  );
  useEffect(() => {
    if (showManagerSettings && !loading) {
      document.getElementById("manager-settings")?.scrollIntoView({ block: "start" });
    }
  }, [showManagerSettings, loading]);
  return (
    <section className="panel model-selection" aria-label="模型与执行引擎">
      <div className="panel-heading">
        <h2>模型与执行引擎</h2>
      </div>
      {(error || loading || !data) && <div className="model-load-state">
        <ErrorBox message={error} />
        {loading ? <Loading /> : !data && <button type="button" className="secondary" onClick={reload}>
          重新读取配置
        </button>}
      </div>}
      {!loading && data && <SelectionEditor initial={data} />}
      <ManagerSettings initiallyOpen={showManagerSettings} />
    </section>
  );
}

function SelectionEditor({ initial }: { initial: ModelSettings }) {
  const [settings, setSettings] = useState(initial);
  const [engine, setEngine] = useState<AgentEngine>(initial.agent_engine);
  const [selection, setSelection] = useState<ModelChoice>(initial.selection);
  const [busy, setBusy] = useState<"engine" | "models" | null>(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const save = async (event: FormEvent, kind: "engine" | "models") => {
    event.preventDefault();
    setBusy(kind);
    setError("");
    setNotice("");
    try {
      const result = await api<ModelSettings>("/settings/selection", {
        method: "PATCH",
        body: JSON.stringify(
          kind === "engine" ? { agent_engine: engine } : { selection },
        ),
      });
      setSettings(result);
      if (kind === "engine") setEngine(result.agent_engine);
      else setSelection(result.selection);
      setNotice("已保存");
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "配置保存失败");
    } finally {
      setBusy(null);
    }
  };
  const updateOption = (role: ModelRole, key: string, value: unknown) => {
    setNotice("");
    setSelection((current) => ({
      ...current,
      options: {
        ...current.options,
        [role]: { ...current.options[role], [key]: value },
      },
    }));
  };
  const option = (role: ModelRole, key: string) =>
    selection.options[role]?.[key] ??
    settings.catalog.models[selection[role]]?.defaults[key];
  const resolution = String(option("video", "resolution") ?? "720p");
  return (
    <>
      <form
        className="form-stack model-engine"
        onSubmit={(event) => void save(event, "engine")}
      >
        <label>
          执行引擎
          <Select
            value={engine}
            disabled={!!busy}
            onChange={(event) => {
              setEngine(event.target.value as AgentEngine);
              setNotice("");
            }}
          >
            <option value="codex">Codex</option>
            <option value="harness">DSH / Harness</option>
            <option value="custom">自定义命令</option>
          </Select>
        </label>
        <button
          className="secondary"
          disabled={!!busy || engine === settings.agent_engine}
        >
          {busy === "engine" ? "保存中…" : "保存引擎"}
        </button>

      </form>
      <form
        className="form-stack model-options"
        onSubmit={(event) => void save(event, "models")}
      >
        <div className="model-grid">
          {modelRoles.map(({ id, label }) => {
            const candidates = Object.entries(settings.catalog.models).filter(
              ([, model]) => model.role === id,
            );
            return (
              <label key={id}>
                {label}
                <Select
                  value={selection[id]}
                  disabled={!!busy}
                  onChange={(event) => {
                    setSelection((current) => ({
                      ...current,
                      [id]: event.target.value,
                      options: { ...current.options, [id]: {} },
                    }));
                    setNotice("");
                  }}
                >
                  {candidates.map(([modelId, model]) => (
                    <option key={modelId} value={modelId}>
                      {modelName(model.label)}
                    </option>
                  ))}
                </Select>
              </label>
            );
          })}
        </div>
        <div className="model-grid">
          <label>
            TTS 音色
            <input
              value={String(option("tts", "voice") ?? "")}
              placeholder="例如 Kore、Puck、Fenrir、Zephyr"
              required
              disabled={!!busy}
              onChange={(event) =>
                updateOption("tts", "voice", event.target.value)
              }
            />
          </label>
          {settings.catalog.models[selection.tts]?.protocol !== "kie_gemini38_tts_v1" && <label>
            TTS 口音
            <input
              value={String(option("tts", "accent") ?? "")}
              placeholder="Neutral"
              disabled={!!busy}
              onChange={(event) =>
                updateOption("tts", "accent", event.target.value)
              }
            />
          </label>}
          <label>
            TTS 温度
            <input
              type="number"
              step="any"
              value={Number(option("tts", "temperature") ?? 1)}
              disabled={!!busy}
              onChange={(event) =>
                updateOption("tts", "temperature", Number(event.target.value))
              }
            />
          </label>
          <label>
            视频分辨率
            <Select
              value={resolution}
              disabled={!!busy}
              onChange={(event) =>
                updateOption("video", "resolution", event.target.value)
              }
            >
              <option value="720p">720p</option>
              <option value="1080p">1080p</option>
            </Select>
          </label>
          {settings.catalog.models[selection.video]?.protocol !== "kie_wan_reference_v1" && <label>
            视频提示词扩展
            <Select
              value={String(option("video", "prompt_extend") ?? false)}
              disabled={!!busy}
              onChange={(event) =>
                updateOption(
                  "video",
                  "prompt_extend",
                  event.target.value === "true",
                )
              }
            >
              <option value="false">关闭</option>
              <option value="true">开启</option>
            </Select>
          </label>}
        </div>
        <div className="actions wrap">
          <button
            className="primary"
            disabled={
              !!busy ||
              JSON.stringify(selection) === JSON.stringify(settings.selection)
            }
          >
            {busy === "models" ? "保存中…" : "保存模型配置"}
          </button>

        </div>
      </form>
      <ModelReference catalog={settings.catalog} selection={selection} />
      <div className="model-feedback">
        <ErrorBox message={error} />
        {notice && <p role="status">{notice}</p>}
      </div>
    </>
  );
}
