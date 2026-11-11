"""Source-shared audio preparation, explicit speaker voices, and bounded local revisions.

Only this module's receipts are written. Translation snapshots and shared provider
configuration stay immutable. Unknown submissions fail closed; a reconciled KIE
task can resume GET/download with new submissions explicitly disabled.
"""
from __future__ import annotations

from video_translation.domain.limits import allows_new_call

from copy import deepcopy
from dataclasses import replace
import hashlib
import json
import math
from pathlib import Path

from video_translation.application.tasks import Conflict
from video_translation.domain.production import AdaptivePlan, SpeechRevision
from video_translation.execution.remote import _inspect_journal, _journal_location, run_kie
from video_translation.integrations.agents.errors import CapabilityUnavailable, UnknownExternalResult
from video_translation.integrations.providers.kie import KieClient, VOICES
from video_translation.integrations.providers.speech import ProviderConfig, SpeechSynthesizer
from video_translation.persistence.store import now


def _key(identity):
    return hashlib.sha256(json.dumps(identity, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class AdaptiveAudio:
    def __init__(self, executor, task, node):
        self.executor, self.task, self.node = executor, task, node
        self.service, self.store, self.media = executor.service, executor.store, executor.media

    def _output(self, definition):
        return self.executor.output(self.task, self.node, definition)

    def _directory(self):
        return self.service.node_directory(self.task["id"], self.node)

    def _current(self):
        current = self.store.require("task", self.task["id"])
        if current["status"] == "cancelled":
            raise Conflict("任务已取消")
        return current

    def _source(self):
        record = self.store.require("asset", self.node["asset_id"])
        path = self.service.checked_path(record)
        digest = self.service.hash_file(path)
        if record.get("sha256") != digest:
            raise Conflict("源媒体哈希已变化，请核对原素材")
        return {"path": str(path), "sha256": digest}

    def _verify(self, receipt):
        try:
            path = self.service.checked_path(receipt)
        except (KeyError, OSError):
            raise Conflict("音频产物缺失；请核对付费回执，不能自动重新收费") from None
        if self.service.hash_file(path) != receipt.get("sha256"):
            raise Conflict("音频产物哈希不一致；不能自动重新收费")
        return path

    def _receipt(self, path):
        return {"path": str(path), "sha256": self.service.hash_file(Path(path))}

    def _verify_background(self, output):
        if output.get("source") != self._source():
            raise Conflict("音频准备的源媒体身份已变化")
        for key in ("source_audio", "prepared_audio"):
            if output.get(key):
                self._verify(output[key])
        separation = output.get("separation")
        if separation:
            for kind in ("background", "vocals", "mixed_background"):
                self._verify(separation[kind])
            if output.get("background_path") != separation["mixed_background"]["path"]:
                raise Conflict("背景音轨路径与已验证回执不一致")
        elif output.get("background_path"):
            raise Conflict("背景音轨缺少分离哈希回执")

    @staticmethod
    def _timeline(speech, duration):
        if not isinstance(duration, (int, float)) or not math.isfinite(duration) or duration <= 0:
            raise ValueError("Audio timeline must have a finite positive duration")
        if len(speech) > 64:
            raise ValueError("Audio timeline supports at most 64 speech segments")
        previous_end, ids = 0, set()
        for item in sorted(speech, key=lambda row: row["start_ms"]):
            start, end = item["start_ms"], item["end_ms"]
            if not all(isinstance(value, (int, float)) and math.isfinite(value) for value in (start, end)):
                raise ValueError("Speech timestamps must be finite")
            if start < previous_end:
                raise ValueError("重叠对白无法由当前 assemble_audio 保留全部角色；请先调整对白时间段")
            if start < 0 or end <= start or end > duration or item["id"] in ids:
                raise ValueError("Speech IDs or timeline boundaries are invalid")
            previous_end = end
            ids.add(item["id"])

    def _skip(self, base):
        return {**base, "path": None, "segments": [], "spoken_texts": {}, "revisions": [],
                "preserve_original_audio": True, "_skip": "没有需要替换的人声，保留原音轨"}

    def prepare(self):
        self._current()
        inventory, prepared = self._output("observe"), self._output("prepare")
        source = self._source()
        duration = prepared["media"]["duration_ms"]
        base = {"source": source, "source_audio": None, "duration_ms": duration,
                "path": None, "background_path": None, "separation": None,
                "preserve_original_audio": False, "speech": deepcopy(inventory["speech"])}
        if not inventory["speech"]:
            return self._skip(base)
        self._timeline(inventory["speech"], duration)
        if not prepared["media"]["has_audio"] or not prepared.get("audio_path"):
            raise ValueError("Speech inventory requires a prepared source audio track")
        prepared_audio = Path(prepared["audio_path"])
        self.service.checked_path({"path": str(prepared_audio)})
        base["prepared_audio"] = self._receipt(prepared_audio)
        preserve = self.executor.preserve_background(self.task, inventory)
        identity = {"task": self.task["id"], "asset": self.node["asset_id"], "source": source,
                    "speech": inventory["speech"], "duration_ms": duration,
                    "prepared_audio": base["prepared_audio"], "preserve_background": preserve,
                    "separation_snapshot": self.task.get("separation_snapshot")}
        key = _key(identity)
        cached = self.store.get("adaptive_audio_prepare", key)
        if cached:
            result = cached["result"]
            self._verify_background(result)
            return result
        if preserve:
            self.executor.check_separation(self.task)
            source_audio = self._directory() / "source-mix.wav"
            self.media.extract_original_audio(Path(source["path"]), source_audio)
            base["source_audio"] = self._receipt(source_audio)
            cache = self.service.settings.runtime / "tasks" / self.task["id"] / "separation" / self.node["asset_id"]
            separation = deepcopy(self.executor.separator.separate(source_audio, cache, media=self.media))
            for kind in ("background", "vocals"):
                path = self._verify(separation[kind])
                self.service.artifact(self.task["id"], self.node, path, "audio")
            background = self._directory() / "dialogue-background.wav"
            self.media.background_for_dialogue(source_audio, Path(separation["background"]["path"]),
                                              background, inventory["speech"])
            separation["mixed_background"] = {**self._receipt(background),
                                               "policy": "original_outside_dialogue_crossfade_40ms"}
            base.update(background_path=str(background), separation=separation)
            self.service.artifact(self.task["id"], self.node, source_audio, "audio", "原音轨混音")
            self.service.artifact(self.task["id"], self.node, background, "audio", "用于混音的背景轨")
            self.service.write_json(self.task["id"], self.node, "separation.json", separation)
        base["limitations"] = (["分离伴奏可能残留原人声或影响音效；须完整听验"] if preserve else
                               ["按任务选择替换原音轨，未保留背景音"])
        self.store.put("adaptive_audio_prepare", {"id": key, "task_id": self.task["id"], "result": base})
        self.service.write_json(self.task["id"], self.node, "audio-preparation.json", base)
        return base

    def _claim(self, kind, key, identity):
        """Persist intent before a paid boundary; only the first claimant may submit."""
        with self.store.connection() as db:
            row = db.execute("SELECT data FROM records WHERE kind=? AND id=?", (kind, key)).fetchone()
            if row:
                record = json.loads(row[0])
                if record.get("identity") != identity or record.get("task_id") != self.task["id"]:
                    raise Conflict("音频请求回执身份不一致")
                if record["status"] != "succeeded" and not (
                    kind == "adaptive_audio_segment" and identity.get("provider") == "kie"
                ):
                    raise UnknownExternalResult("配音或局部修订结果待核对；禁止重复收费请求")
                return record
            current = json.loads(db.execute("SELECT data FROM records WHERE kind='task' AND id=?",
                                            (self.task["id"],)).fetchone()[0])
            if current["status"] == "cancelled":
                raise Conflict("任务已取消")
            if not allows_new_call(current, 'max_paid_calls', current.get('paid_calls', 0)):
                raise Conflict("已达到本任务外部调用次数上限；未发送请求")
            record = {"id": key, "task_id": self.task["id"], "node_id": self.node["id"],
                      "identity": identity, "status": "pending", "created_at": now()}
            db.execute("INSERT INTO records VALUES(?,?,?,?)",
                       (kind, key, json.dumps(record, ensure_ascii=False), record["created_at"]))
        return None

    def _resume_kie_node(self, receipt, identity, config):
        """Use only a reconciled operation's original identity; never create a job."""
        current = self._current()
        original = next((node for node in current["nodes"] if node["id"] == receipt["node_id"]), None)
        if not original or any(original.get(key) != self.node.get(key)
                               for key in ("asset_id", "locale", "definition_id")):
            raise Conflict("KIE恢复节点与原配音身份不一致")
        operation_id = "kie-" + _key({"task": self.task["id"], "node": original["id"],
                                      "kind": "synthesize", "input": identity})
        operation = self.store.get("operation", operation_id)
        if not operation or any(operation.get(key) != value for key, value in {
            "task_id": self.task["id"], "node_id": original["id"], "provider": "kie", "kind": "synthesize"
        }.items()) or operation.get("resume_safe") is not True or not operation.get("external_id"):
            raise UnknownExternalResult("KIE配音尚未核验可恢复的原taskId；请先核对任务回执")
        directory, path = _journal_location(self.service, operation)
        reader = KieClient(config, directory=directory)
        with reader._locked(path):
            observed = _inspect_journal(path, operation, current)
        if not observed["resume_safe"] or observed["external_id"] != operation["external_id"]:
            raise UnknownExternalResult("KIE配音恢复回执无法确认原taskId")
        return original

    def _save_success(self, kind, key, **result):
        self.store.update(kind, key, lambda record: record.update(status="succeeded", **result))

    def _replacement_identity(self, identity, request_node, config):
        """Permit one new request after a journal-confirmed, uncharged server failure."""
        operation_id = "kie-" + _key({"task": self.task["id"], "node": request_node["id"],
                                      "kind": "synthesize", "input": identity})
        operation = self.store.require("operation", operation_id)
        directory, path = _journal_location(self.service, operation)
        reader = KieClient(config, directory=directory)
        with reader._locked(path):
            _inspect_journal(path, operation, self._current())
            journal = json.loads(path.read_text("utf-8"))
        receipt = journal.get("provider_receipt", {})
        if (journal.get("state") == "fail" and journal.get("credits_consumed") == 0
                and str(receipt.get("failCode")) in {"500", "502", "503", "504"}):
            return {**identity, "replacement_for": operation_id,
                    "failed_external_id": journal["task_id"]}
        return None

    def _provider(self, speakers):
        kie_config = self.executor.kie_for(self.task)
        provider = self.executor.speech
        config = kie_config or getattr(provider, "config", None) or ProviderConfig.from_env("tts")
        allowed = VOICES if kie_config else {config.voice}
        for speaker in speakers:
            if speaker.voice not in allowed:
                raise ValueError(f"角色 {speaker.speaker} 的 voice {speaker.voice!r} 不在供应商允许列表中")
        if not kie_config and isinstance(provider, SpeechSynthesizer) and (not config.base_url or not config.model):
            raise CapabilityUnavailable("尚未配置目标语配音供应商")
        return kie_config, config

    def _synthesize(self, speech, text, voice, source, kie_config, config):
        identity = {"version": 1, "task": self.task["id"], "asset": self.node["asset_id"],
                    "source_sha256": source["sha256"], "source_id": speech["id"],
                    "speaker": speech["speaker"], "locale": self.node["locale"], "text": text, "voice": voice,
                    "provider": "kie" if kie_config else "compatible",
                    "base_url": getattr(config, "base_url", "kie"),
                    "model": config.tts_model if kie_config else config.model,
                    "protocol": getattr(config, "tts_protocol", "audio/speech"),
                    "options": {**config.tts_options, "voice_name": voice} if kie_config else {},
                    "model_snapshot": self.task.get("model_snapshot", {}).get("sha256")}
        key = _key(identity)
        cached = self._claim("adaptive_audio_segment", key, identity)
        if cached and cached["status"] == "succeeded":
            return self._verify(cached), key
        cache = self.service.settings.runtime / "tasks" / self.task["id"] / "adaptive-audio" / "segments"
        cache.mkdir(parents=True, exist_ok=True)
        target = cache / f"{key}.wav"
        if kie_config:
            selected = replace(kie_config, voice=voice, tts_options={**kie_config.tts_options, "voice_name": voice})
            request_node = self.node
            request_identity = identity
            if cached:
                request_node = self._resume_kie_node(cached, identity, selected)
                replacement = self._replacement_identity(identity, request_node, selected)
                if replacement:
                    # A fixed replacement identity has its own durable journal.
                    # Unknown or failed replacements never create another generation.
                    request_identity = replacement
                    replacement_id = "kie-" + _key({
                        "task": self.task["id"], "node": request_node["id"],
                        "kind": "synthesize", "input": replacement,
                    })
                    if self.store.get("operation", replacement_id):
                        selected = replace(selected, enabled_model_calls=False)
                else:
                    # Poll/download does not need permission to create a job.
                    selected = replace(selected, enabled_model_calls=False)
            run_kie(self.service, selected, self.task, request_node, "synthesize", request_identity,
                    lambda client, request_key, event: client.synthesize(
                        text, self.node["locale"], target, request_key=request_key, on_event=event))
        else:
            if type(self.executor.speech) is SpeechSynthesizer:
                provider = SpeechSynthesizer(replace(config, voice=voice), client=self.executor.speech.client)
            else:
                # Compatible voices were restricted to config.voice above, so an
                # injected facade needs no mutation or copy of its call counters.
                provider = self.executor.speech
            self.executor.paid(self.task, self.node, "tts", "synthesize",
                               lambda: provider.synthesize(text, self.node["locale"], target))
        self._save_success("adaptive_audio_segment", key, **self._receipt(target))
        return target, key

    def _revise(self, speech, text, raw, key, duration, target_duration, revision):
        inputs = {"source_id": speech["id"], "source_text": speech["text"],
                  "locale": self.node["locale"], "spoken_text": text, "target_duration_ms": target_duration,
                  "actual_duration_ms": duration, "max_speedup": 1.25, "revision": revision,
                  "audio": self._receipt(raw),
                  "instruction": "仅缩短此句口播，保留原文语义、事实、人物及语气；输出目标语言，不修改其它句或翻译快照。"}
        identity = {"task": self.task["id"], "asset": self.node["asset_id"],
                    "locale": self.node["locale"], "segment_key": key, "inputs": inputs}
        decision_id = "speech-revision-" + _key(identity)
        cached = self._claim("adaptive_speech_revision", decision_id, identity)
        if cached:
            evidence = cached["evidence"]
        else:
            result, consumed = self.executor.call_agent(self.task, self.node, inputs, SpeechRevision,
                                                        decision_id=decision_id)
            result = SpeechRevision.model_validate(result).model_dump()
            if result["source_id"] != speech["id"] or result["text"].strip() == text.strip():
                raise ValueError("局部口播修订必须绑定当前句并实际缩短文本")
            evidence = {**result, "decision_id": decision_id, "inputs": inputs, "_media": consumed}
            self._save_success("adaptive_speech_revision", decision_id, evidence=evidence)
        self.service.write_json(self.task["id"], self.node, decision_id + ".json", evidence)
        return evidence

    def voice(self):
        self._current()
        prepared, inventory = self._output("audio_prepare"), self._output("observe")
        self._verify_background(prepared)
        if prepared.get("speech") != inventory["speech"]:
            raise Conflict("对白清单已变化，请先重新准备源音轨")
        if not inventory["speech"]:
            return self._skip(prepared)
        duration = self._output("prepare")["media"]["duration_ms"]
        self._timeline(inventory["speech"], duration)
        translation = self._output("translate")
        if translation.get("locale") != self.node["locale"]:
            raise Conflict("翻译语言与配音语言不一致")
        plan = AdaptivePlan.model_validate({key: value for key, value in self._output("plan").items()
                                            if not key.startswith("_")})
        voices = {item.speaker: item.voice for item in plan.speakers}
        required = {item.get("speaker", "narrator") for item in inventory["speech"]}
        if len(voices) != len(plan.speakers) or not required.issubset(voices):
            raise ValueError("每个对白角色必须有唯一、明确的供应商 voice 选择，不能全部回退 default")
        kie_config, config = self._provider(plan.speakers)
        translations = {item["source_id"]: item["text"] for item in translation["items"]}
        if len(translations) != len(translation["items"]) or any(
            not translations.get(item["id"], "").strip() for item in inventory["speech"]
        ):
            raise ValueError("对白翻译缺失或 source_id 重复")
        segments, spoken_texts, revisions, timing_adjustments = [], {}, [], []
        for item in inventory["speech"]:
            self._current()
            speech = {**item, "speaker": item.get("speaker", "narrator")}
            text, target_duration = translations[speech["id"]], speech["end_ms"] - speech["start_ms"]
            while True:
                raw, key = self._synthesize(speech, text, voices[speech["speaker"]], prepared["source"],
                                            kie_config, config)
                info = self.media.probe(raw)
                actual = info["duration_ms"]
                if not info["has_audio"] or not math.isfinite(actual) or actual <= 0:
                    raise ValueError("配音未返回有效音轨；保留付费回执，不重新生成")
                if actual / target_duration > 1.251 and speech["speaker"].startswith("narrator"):
                    next_start = min(
                        (other["start_ms"] for other in inventory["speech"]
                         if other["start_ms"] > speech["start_ms"]),
                        default=duration,
                    )
                    # Narration may use an existing gap, never overlap the next line
                    # or extend the source. Character dialogue keeps its original window.
                    available = next_start - speech["start_ms"]
                    if actual <= available:
                        target_duration = int(math.ceil(actual))
                        timing_adjustments.append({
                            "source_id": speech["id"], "original_end_ms": speech["end_ms"],
                            "end_ms": speech["start_ms"] + target_duration,
                            "reason": "旁白使用下一句之前的空档，保留完整译文和自然语速",
                        })
                if actual / target_duration <= 1.251:
                    break
                if len(revisions) >= self.task.get("max_revisions", 0):
                    raise ValueError("Speech would require speedup above 25%; local revision limit reached")
                evidence = self._revise(speech, text, raw, key, actual, target_duration, len(revisions) + 1)
                revisions.append(evidence)
                text = evidence["text"]
            fitted = self._directory() / f"fitted-{key}.wav"
            self.media.fit_audio(raw, fitted, target_duration)
            self.service.artifact(self.task["id"], self.node, raw, "audio", "供应商原始配音")
            self.service.artifact(self.task["id"], self.node, fitted, "audio", "对齐后的角色配音")
            segments.append({**self._receipt(fitted), "source_id": speech["id"], "speaker": speech["speaker"],
                             "voice": voices[speech["speaker"]], "text": text,
                             "start_ms": speech["start_ms"], "end_ms": speech["start_ms"] + target_duration,
                             "raw_path": str(raw), "raw_sha256": self.service.hash_file(raw),
                             "actual_duration_ms": actual, "speed": max(.75, min(1.25, actual / target_duration))})
            spoken_texts[speech["id"]] = text
        output = {**prepared, "segments": segments, "spoken_texts": spoken_texts, "revisions": revisions,
                  "timing_adjustments": timing_adjustments,
                  "preserve_original_audio": False}
        self.service.write_json(self.task["id"], self.node, "spoken-texts.json",
                                {"locale": self.node["locale"], "spoken_texts": spoken_texts, "revisions": revisions})
        return output

    def mix(self):
        self._current()
        voice = self._output("audio_voice")
        self._verify_background(voice)
        inventory = self._output("observe")
        if voice.get("speech") != inventory["speech"]:
            raise Conflict("配音对白与源清单不一致，不能合成")
        if voice.get("preserve_original_audio"):
            if inventory["speech"]:
                raise Conflict("仍有待替换对白，不能跳过配音")
            return self._skip(voice)
        segments = voice["segments"]
        expected = {item["id"]: (item["start_ms"], item["end_ms"], item.get("speaker", "narrator"))
                    for item in inventory["speech"]}
        duration = self._output("prepare")["media"]["duration_ms"]
        adjusted = set()
        for adjustment in voice.get("timing_adjustments", []):
            source_id = adjustment["source_id"]
            if source_id not in expected or source_id in adjusted:
                raise Conflict("旁白时长调整身份不一致")
            start, end, speaker = expected[source_id]
            next_start = min((v[0] for v in expected.values() if v[0] > start), default=duration)
            if (not speaker.startswith("narrator") or adjustment["original_end_ms"] != end
                    or not end < adjustment["end_ms"] <= next_start):
                raise Conflict("旁白时长调整越出可用空档")
            expected[source_id] = (start, adjustment["end_ms"], speaker)
            adjusted.add(source_id)
        actual = {item["source_id"]: (item["start_ms"], item["end_ms"], item["speaker"]) for item in segments}
        if expected != actual or voice["spoken_texts"] != {item["source_id"]: item["text"] for item in segments}:
            raise Conflict("配音片段没有完整覆盖角色、时间段及最终口播文本")
        self._timeline([{**item, "id": item["source_id"]} for item in segments], duration)
        for segment in segments:
            self._verify(segment)
            self._verify({"path": segment["raw_path"], "sha256": segment["raw_sha256"]})
        target = self._directory() / "voice.wav"
        self.media.assemble_audio(segments, target, duration)
        artifact = self.service.artifact(self.task["id"], self.node, target, "audio")
        return {**voice, **self._receipt(target), "artifact_id": artifact["id"],
                "preserve_original_audio": False}
