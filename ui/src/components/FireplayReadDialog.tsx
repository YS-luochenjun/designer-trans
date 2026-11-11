import { useRef, useState } from "react";
import { api, useData } from "../lib/api";
import { ErrorBox, Loading, Modal } from "./common";
import "./FireplayReadDialog.css";

type DeliveryOptions = {
  items: { value: string; label: string }[];
  selected_delivery_types: string[];
  source: string;
  revision: string | null;
};

export function FireplayReadDialog({ onClose, onRead }: { onClose: () => void; onRead: () => void }) {
  const { data, error, loading, reload } = useData<DeliveryOptions>("/fireplay/delivery-options");
  const [selection, setSelection] = useState<string[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState("");
  const inFlight = useRef(false);
  const options = data?.items ?? [];
  const selected = (selection ?? data?.selected_delivery_types ?? []).filter((value) => options.some((option) => option.value === value));
  const read = async () => {
    if (inFlight.current || !selected.length) return;
    inFlight.current = true;
    setBusy(true);
    setActionError("");
    try {
      await api("/fireplay/sync", { method: "POST", body: JSON.stringify({ delivery_types: selected }) });
      onRead();
    } catch (cause) {
      setActionError(cause instanceof Error ? cause.message : "需求读取失败");
    } finally {
      inFlight.current = false;
      setBusy(false);
    }
  };
  return <Modal title="选择 AI 承接交付方式" onClose={() => { if (!inFlight.current) onClose(); }}>
    <div className="form-stack" aria-busy={busy}>
      <p>读取 AI 承接交付非空、且匹配任一选中交付方式的需求。</p>
      <ErrorBox message={actionError || error} />
      {loading ? <Loading /> : <fieldset className="delivery-options" disabled={busy}>
        <legend>交付方式（可多选）</legend>
        {options.map((option) => <label key={option.value}>
          <input type="checkbox" checked={selected.includes(option.value)} onChange={(event) => {
            setActionError("");
            setSelection(event.target.checked ? [...new Set([...selected, option.value])] : selected.filter((value) => value !== option.value));
          }} />
          <span>{option.label}</span>
        </label>)}
        {!options.length && <p>当前没有可用的交付方式。</p>}
      </fieldset>}
      {!loading && !selected.length && <p role="status">请至少选择一种交付方式。</p>}
      <div className="modal-actions">
        <button className="secondary" type="button" disabled={busy} onClick={onClose}>取消</button>
        {error && <button className="secondary" type="button" disabled={busy || loading} onClick={reload}>重试读取选项</button>}
        <button className="primary" type="button" disabled={busy || loading || !selected.length} onClick={() => void read()}>{busy ? "正在读取…" : `读取需求${selected.length ? `（${selected.length} 种）` : ""}`}</button>
      </div>
    </div>
  </Modal>;
}
