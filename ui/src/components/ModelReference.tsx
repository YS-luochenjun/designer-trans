import { ChevronDown, Coins, ExternalLink } from "lucide-react";
import type { ModelChoice, ModelSettings } from "../lib/types";
import { modelName, modelPricing, modelRoles } from "./modelPresentation";
import "./ModelReference.css";

export function ModelReference({ catalog, selection }: {
  catalog: ModelSettings["catalog"]; selection: ModelChoice;
}) {
  return <details className="model-reference">
    <summary>
      <Coins size={17} />
      <strong>模型价格与使用说明</strong>
      <span>{Object.keys(catalog.models).length} 个模型</span>
      <ChevronDown size={16} className="model-reference-chevron" />
    </summary>
    <div className="model-reference-content" role="region" aria-label="模型价格明细" tabIndex={0}>
      <p className="model-reference-note">按项目价格记录展示，实际费用以供应商账单为准。</p>
      {modelRoles.map(role => <section className="model-reference-group" key={role.id}>
        <h3>{role.label}</h3>
        {Object.entries(catalog.models).filter(([, model]) => model.role === role.id)
          .map(([id, model]) => <article className={`model-reference-row ${selection[role.id] === id ? "is-selected" : ""}`} key={id}>
            <div className="model-reference-name">
              <strong>{modelName(model.label)}</strong>
              {selection[role.id] === id && <span className="pill on">当前选择</span>}
            </div>
            <div className="model-reference-description">
              <p>{modelPricing(model)}</p>
              {typeof model.limitations === "string" && model.limitations &&
                <p className="model-reference-limits">{model.limitations}</p>}
              <div className="model-reference-meta">
                <span>价格记录：{model.pricing_verified_on
                  ? <time dateTime={model.pricing_verified_on}>{model.pricing_verified_on}</time>
                  : "未标注日期"}</span>
                {model.pricing_source && /^https?:\/\//i.test(model.pricing_source) &&
                  <a href={model.pricing_source} target="_blank" rel="noreferrer">
                    价格来源<ExternalLink size={12} />
                  </a>}
              </div>
            </div>
          </article>)}
      </section>)}
    </div>
  </details>;
}
