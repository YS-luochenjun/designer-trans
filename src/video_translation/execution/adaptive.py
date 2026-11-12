"""Agent-directed local production with durable decisions and scoped media tools."""
from __future__ import annotations

from video_translation.domain.limits import allows_new_call

import hashlib
import json
from copy import deepcopy
from pathlib import Path

from video_translation.application.tasks import Conflict
from video_translation.domain.production import SourceDesign, CleanAction
from video_translation.integrations.agents.errors import CapabilityUnavailable, ConfirmedJobFailure, UnknownExternalResult
from video_translation.persistence.store import now
from .clean_policy import QUALITY_RESOURCE_ID, coverage_summary, recent_history, validate_quality
from .frame_clock import frame_window, boundary_indices, request_time
from .candidate_recovery import (
    archive_summary, candidate_diagnostics, rebase_obstacle,
    retained_archive_evidence, sampled_candidate_changes,
)
from .clean_progress import progress, inspection_key, reconsider_accepted_source, reuse_trial_inspection
from .clean_deferrals import defer_source
from .scope_review import pending_scope_review, confirm_scope_review
from .acceptance_disputes import dispute_acceptance
from .clean_contract import execution_contract
from .clean_experience import method_advisories, record_local_experience
from .temporal_work import temporal_work_orders
from video_translation.application.task_storage import retired_candidates


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


class AdaptiveProduction:
    def __init__(self, executor, task, node):
        self.e, self.task, self.node = executor, task, node
        self.service, self.store = executor.service, executor.store
        self.directory = self.service.node_directory(task['id'], node)
        from .efficient_media import visual_for
        self.media = visual_for(executor, task)
        self.source = self.service.checked_path(self.store.require('asset', node['asset_id']))
        self.prepared = executor.output(task, node, 'prepare')
        self.inventory = deepcopy(executor.output(task, node, 'observe'))
        if node['definition_id'] != 'clean':
            clean = next((n for n in task['nodes'] if n['definition_id'] == 'clean'
                          and n['asset_id'] == node['asset_id']), None)
            overrides = (clean or {}).get('output', {}).get('text_overrides', {})
            for item in self.inventory['texts']:
                if item['id'] in overrides:
                    item.update(overrides[item['id']])
        self.texts = {t['id']: t for t in self.inventory['texts'] if t['treatment'] == 'translate'}
        self.duration = self.prepared['media']['duration_ms']

    def active(self):
        if self.store.require('task', self.task['id'])['status'] == 'cancelled':
            raise Conflict('任务已取消，停止后续制作')

    def fonts(self):
        candidates = []
        explicit = self.e.media.font
        if explicit:
            candidates.append(Path(explicit))
        project = self.service.settings.root / 'config' / 'fonts'
        if project.is_dir():
            candidates.extend(sorted(project.glob('*.ttf'))[:30])
            candidates.extend(sorted(project.glob('*.otf'))[:30])
        candidates += [Path(p) for p in (
            'C:/Windows/Fonts/msyh.ttc', 'C:/Windows/Fonts/msyhbd.ttc',
            'C:/Windows/Fonts/arial.ttf', 'C:/Windows/Fonts/arialbd.ttf',
            'C:/Windows/Fonts/meiryo.ttc', 'C:/Windows/Fonts/malgun.ttf',
            '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
            '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf',
            '/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc')]
        result = {}
        for path in candidates:
            if path.is_file() and not path.is_symlink():
                result[path.name] = str(path.resolve())
        if result:
            result['default'] = next(iter(result.values()))
        return result

    def frames(self, video, times, label, key):
        times = sorted(set(int(t) for t in times))
        if not times or len(times) > 12 or any(t < 0 or t >= self.duration for t in times):
            raise ValueError('补充观察时间点必须在原片内，单次最多12帧')
        result = self.media.capture(video, self.directory / key, times, timeline_duration_ms=self.duration)
        for frame in result:
            frame['label'] = (f"{label} · 实际帧 {frame['sampled_time_ms']:.3f}ms"
                              f" · 帧号 {frame['frame_index']}")
        return result

    def times(self, items=None):
        values = []
        for item in (items or list(self.texts.values())):
            values += [item['start_ms'], (item['start_ms'] + item['end_ms']) // 2, max(item['start_ms'], item['end_ms'] - 1)]
        return sorted(set(values))[:12] or [0]

    def candidate_times(self, candidate):
        _, clock = self.media._info(self.source)
        first, stop = frame_window(candidate['start_ms'], candidate['end_ms'], clock['rate'])
        stop = min(stop, clock['frames'])
        indices = set(boundary_indices(candidate['start_ms'], candidate['end_ms'],
                                       clock['rate'], clock['frames']))
        indices.update(first + (stop - 1 - first) * i // 4 for i in range(1, 4))
        indices.add(min(first + 1, stop - 1))
        return sorted(request_time(i, clock['rate']) for i in indices if 0 <= i < clock['frames'])

    def verified(self, record):
        path = Path(record['path']).resolve()
        if not path.is_relative_to(self.service.settings.runtime / 'tasks' / self.task['id']):
            inherited = {Path(r['path']).resolve(): r['sha256'] for r in self.task.get('rework_inputs', [])}
            if path != self.source and inherited.get(path) != record['sha256']:
                raise Conflict('制作产物不属于当前任务')
        if not path.is_file() or self.service.hash_file(path) != record['sha256']:
            raise Conflict('已完成制作产物缺失或变化，请核对后恢复，不自动重做收费操作')
        if getattr(getattr(self, 'media', None), 'is_manifest', lambda p: False)(path):
            self.media._chain(path)
        return path

    def pipeline_sample_times(self, candidate, requested):
        times = sorted(set(requested))
        if len(times) > 8 or any(t < 0 for t in times):
            raise ValueError('链路观察须使用裁切相对时间，单次最多8个非负时间点')
        for key in ('input', 'generated', 'conformed'):
            _, clock = self.media._info(self.verified(candidate['pipeline'][key]))
            duration = min(candidate['pipeline']['duration_ms'], clock['duration_ms'])
            if any(t >= duration for t in times):
                raise ValueError('链路观察时间超出已保存媒体时长；不截断或替换请求时间')
        return times

    def pipeline_frames(self, candidate, turn, requested=None):
        """Inspect retained provider evidence without another external request."""
        pipeline = candidate.get('pipeline')
        if not pipeline:
            raise ValueError('候选没有已登记的模型链路文件；保留候选并请求本地诊断')
        if pipeline.get('media_type') == 'still_image':
            from .image_pipeline import still_frames
            return still_frames(self, pipeline)
        requested = self.pipeline_sample_times(candidate, requested or [])
        result = []
        for key, label in (('input', '模型输入'), ('generated', '模型原始返回'),
                           ('conformed', '本地缩放结果')):
            path = self.verified(pipeline[key])
            _, clock = self.media._info(path)
            # Compare elapsed time, not frame ordinal across different model fps.
            duration = min(pipeline['duration_ms'], clock['duration_ms'])
            times = requested or sorted({0, int(duration * .1), int(duration * .4),
                                         max(0, int(duration * .6) - 1)})
            frames = self.media.capture(path, self.directory / f'pipeline-{turn}-{key}', times)
            for frame in frames:
                frame['label'] = (f"{label} · 裁切相对时间 {frame['sampled_time_ms']:.3f}ms"
                                  f" · 局部帧 {frame['frame_index']}（非原片帧号）")
            result.extend(frames)
        return result

    def reject_candidate(self, state, action, *, reviewed_archive=None):
        candidate = state.get('candidate')
        if not candidate or candidate['id'] != action['candidate_id']:
            candidate = reviewed_archive
            if not candidate or candidate['id'] != action['candidate_id']:
                raise ValueError('拒绝必须绑定当前候选或本轮已观察的历史候选')
        if any(c['id'] == candidate['id'] for c in state.get('accepted', [])):
            raise ValueError('已接受结果不能通过历史拒绝操作撤销')
        self.verified(candidate)
        action['source_id'] = candidate['source_id']
        rejected = {**candidate, 'review_status': 'rejected', 'rejection': {
            'reason': action['reason'], 'evidence': action['evidence'],
            'quality': action.get('quality'), 'at': now()}}
        if candidate.get('rejection'):
            rejected.setdefault('rejection_history', []).append(candidate['rejection'])
        archive = state.setdefault('candidate_archive', [])
        archive[:] = [c for c in archive if c['id'] != candidate['id']]
        archive.append(rejected)
        if (state.get('candidate') or {}).get('id') == candidate['id']:
            state['candidate'] = None
            state.pop('pipeline_review', None)
        if state.get('archive_review') == candidate['id']:
            state.pop('archive_review', None)

    def decision(self, inputs, model, frames, *, key):
        from video_translation.persistence.transaction import TransactionStore
        from video_translation.domain.limits import budget_policy, testing_unlimited

        self.active()
        if testing_unlimited(self.task):
            inputs = {**inputs, "budget_policy": budget_policy(self.task)}
        from video_translation.application.task_supervision import live_directions
        directions = live_directions(self.service, self.task["id"], self.node["id"])
        if directions:
            inputs = {**inputs, "route_directions": directions}
        self._sent_directions = digest(directions)
        from video_translation.application.visual_recovery import decision_key, reserve_decision
        key = decision_key(self.store, self.task, self.node, key)
        from video_translation.application import clean_route_recovery
        key = clean_route_recovery.decision_key(self.store, self.task, self.node, key)
        identity = digest({'task': self.task['id'], 'node': self.node['id'], 'key': key,
                           'inputs': inputs, 'schema': model.model_json_schema(),
                           'resources': self.task.get('resource_snapshot'),
                           'models': self.task.get('model_snapshot'),
                           'agent_engine': self.task.get('agent_engine'),
                           'frames': [{'time_ms': f['time_ms'], 'label': f.get('label'),
                                       'group': f.get('group'),
                                       'sha256': self.service.hash_file(Path(f['path']))} for f in frames]})
        record = {'id': identity, 'task_id': self.task['id'], 'node_id': self.node['id'],
                  'stage': self.node['definition_id'], 'status': 'running', 'created_at': now(), 'key': key}
        # Reserve before releasing the database writer lock. A second process or
        # thread cannot read an absent intent and independently call the provider.
        with self.store.connection() as connection:
            transaction = TransactionStore(self.store, connection)
            from video_translation.application.agent_recovery import replaced_with_authorization
            reserve_decision(self.service, transaction, self.task, self.node, key, identity, inputs)
            clean_route_recovery.reserve_decision(
                self.service, transaction, self.task, self.node, key, identity, inputs)
            previous = transaction.get('production_decision', identity)
            if previous and previous['status'] == 'succeeded':
                return model.model_validate(previous['result']).model_dump()
            outstanding = [r for r in transaction.list('production_decision')
                           if r.get('task_id') == self.task['id'] and r.get('node_id') == self.node['id']
                           and r.get('status') in {'running', 'unknown', 'invalid_response'}
                           and not replaced_with_authorization(transaction, r, 'production_decision')]
            if outstanding:
                raise UnknownExternalResult('上次Agent制作决策待核实，不能重复提交或通过更换输入绕过')
            before_operations = {op['id'] for op in transaction.list('operation')
                                 if op.get('node_id') == self.node['id']}
            transaction.put('production_decision', record)
        try:
            result, consumed = self.e.call_agent(self.task, self.node, inputs, model, frames,
                                                decision_id=identity[:20])
        except UnknownExternalResult:
            self.store.update('production_decision', identity, lambda r: r.update(status='unknown'))
            raise
        except Exception:
            # paid() records provider success before call_agent validates the
            # schema/writes its receipt. That local failure is not a failed call.
            with self.store.connection() as connection:
                transaction = TransactionStore(self.store, connection)
                operations = [op for op in transaction.list('operation')
                              if op.get('node_id') == self.node['id'] and op['id'] not in before_operations]
                unresolved = any(op.get('status') in {'running', 'unknown'} for op in operations)
                received = any(op.get('status') == 'succeeded' for op in operations)
                status = 'unknown' if unresolved else 'invalid_response' if received else 'failed'
                transaction.update('production_decision', identity, lambda r: r.update(status=status))
            raise
        record.update(status='succeeded', result=result, media=consumed, finished_at=now())
        self.store.put('production_decision', record)
        return result

    def analyze(self):
        if not self.texts:
            return {'lettering': [], 'scene_boundaries_ms': [], '_skip': '没有需要替换的画面文字'}
        result = {'lettering': [], 'scene_boundaries_ms': [], 'observations': []}
        items = list(self.texts.values())
        for batch_index in range(0, len(items), 4):
            batch = items[batch_index:batch_index + 4]
            observed_times = self.times(batch)
            frames = self.frames(self.source, observed_times, '原片', f'source-design-{batch_index}')
            part = self.decision({'inventory': {**self.inventory, 'texts': batch}, 'media': self.prepared['media'],
                'available_fonts': list(self.fonts()), 'observed_times_ms': observed_times,
                'instructions': '保存源设计而不是决定整片工具。只为本批实例记录字体、描边、布局、显隐、轨迹、背景与遮挡。evidence_times_ms必须来自实际给出的observed_times_ms。证据不足写uncertainty，不能伪造。'},
                SourceDesign, frames, key=f'source-design-{batch_index}')
            if {v['source_id'] for v in part['lettering']} != {v['id'] for v in batch}:
                raise ValueError('源设计批次遗漏文字或混入其他批次实例')
            if any(not set(v['evidence_times_ms']).issubset(observed_times) for v in part['lettering']):
                raise ValueError('源设计引用了并未提供的图像时间')
            result['lettering'].extend(part['lettering'])
            result['scene_boundaries_ms'].extend(part['scene_boundaries_ms'])
            result['observations'].extend(part['observations'])
        result['scene_boundaries_ms'] = sorted(set(result['scene_boundaries_ms']))
        ids = [v['source_id'] for v in result['lettering']]
        if set(ids) != set(self.texts) or len(ids) != len(set(ids)):
            raise ValueError('源设计记录必须覆盖且只覆盖全部待译文字')
        speech_ids = {s['id'] for s in self.inventory['speech']}
        for entry in result['lettering']:
            if entry['speech_id'] and entry['speech_id'] not in speech_ids:
                raise ValueError('口播字幕关联了不存在的人声实例')
            item = self.texts[entry['source_id']]
            if any(t < 0 or t >= self.duration for t in entry['evidence_times_ms']):
                raise ValueError('源设计观察时间超出原片')
            self.check_motion(entry['motion'], item)
        result['source_sha256'] = self.service.hash_file(self.source)
        self.service.write_json(self.task['id'], self.node, 'source-design.json', result)
        return result

    @staticmethod
    def check_motion(points, item):
        times = [p['time_ms'] for p in points]
        if times != sorted(set(times)) or any(t < item['start_ms'] or t > item['end_ms'] for t in times):
            raise ValueError('文字轨迹必须按原片时间递增并落在实例区间内')

    def capabilities(self):
        tools = {'inspect': '按原片时间查看原片和当前候选的真实帧',
                 'inspect_archive': '按historical_candidates的ID只读检查历史候选（包括旧基底或已拒绝候选），不恢复、不生成；没有链路文件时比较原片/当前底片/历史候选',
                 'rebase_candidate': '将已检查、未拒绝的历史候选局部重贴到当前底片；旧基底必须在接受链且后续修复无时空重叠；生成新候选重新验收，不调用外部模型',
                 'defer': '用start_ms/end_ms暂缓有证据的困难短段，其他actionable_intervals_ms继续；省略起止表示整个实例暂缓，须有全段依据；暂缓不算完成',
                 'inspect_pipeline': '读取已保存的模型输入、原始返回及缩放结果，不重投模型。times_ms为裁切相对毫秒，最多8点；省略时使用默认采样。以各媒体实际帧号核对，不将补时帧当编辑帧',
                 'reject_candidate': '拒绝当前候选或本轮inspect_archive已观察的历史候选，保存证据；不撤销已接受结果，不清除其他候选，保留底片与未解决范围',
                 'revise_scope': '按真实观察纠正文字时段/扩展运动包络。仅修改region可同时省略start_ms/end_ms以保留当前时段；修改时间须同时提供起止值。缩短估计时段须用absent_times_ms列出被排除区间的无该文字帧，并在times_ms提供新边界内侧帧证据；原始清单保留',
                 'confirm_scope': '仅用于scope_review指定的已接受记录：检查当前底片在包络内的首尾内外帧及内部帧，填写candidate_id、evidence、quality。若acceptance_conflicts存在，逐项核对历史残字/质量质疑，不能因历史高分直接确认。通过只恢复该已接受时段的覆盖，不生成、不新增接受数量、不消除其他时段缺口',
                 'consult': '按resource_index中的ID查阅任务冻结的技能/知识，再据内容选择方法',
                 'restore_candidate': '从reusable_candidates选candidate_id恢复未接受的历史候选，重新观察打分；不调用生成模型，仅允许基于当前已接受底片的候选',
                 'flat': '仅适用于实测均匀源背景；工具会拒绝不匹配底面',
                 'donor': '取同源干净帧/区域补底，需核对纹理、光照、运动',
                 'local_surface': '本地低纹理表面修复。surface_repair.model默认linear，axis默认horizontal要求左右干净边缘；vertical要求上下干净边缘，保留原差异门禁。model=plane新增四周各3像素真实干净环带的明暗平面拟合，固定最大误差12/RMS4，含发丝/字形/硬分界或缺环带则拒绝；适用于平滑明暗渐变，不恢复复杂纹理。须先观察短段完整字形和采样带，提供区域关键帧/evidence；protected_foreground保护前景但不豁免采样门禁。不是自动跟踪/分割，拟合成功不等于质量通过；失败不扩大色块或放宽阈值',
                 'tracked_donor': '同镜头干净供体按已观察区域轨迹逐帧移动缩放，并用protected_foreground多边形关键帧保留移动前景、逐帧检查未保护边缘亮度。须tracked_donor关键帧覆盖起止、donor_time_ms与donor_region；参见execution_contract。显式逐帧保护已实现，自动分割/旋转/透视未实现；不能把缺少自动分割泛化为没有遮挡保护，不凭跟踪分数接受',
                 'accept': '查看候选后接受当前局部修复', 'finish': '全部文字区间覆盖后结束清底'}
        from .motion_tracking import available
        if available():
            tools['track'] = 'CPU初始模板逐帧位置/小幅缩放跟踪；指定同镜头start_ms/end_ms、首帧region及evidence，最多10秒300帧。只返回待复核轨迹，遮挡或低置信停止，不自动修改范围/覆盖/源设计'
        if (not self.task.get('review_draft_authorization')
                and allows_new_call(self.task, 'max_video_credits') and self.e.kie_for(self.task)):
            tools['wan'] = '按同一镜头裁取局部视频连续补底，最多10秒；不足2秒自动复制末帧补足模型输入，不跨镜头凑时长。只贴回真实编辑时段'
        if allows_new_call(self.task, 'max_image_calls') and self.e.image_ready(self.task):
            tools['image_edit'] = '参考当前真实裁切帧自然去字补底，返回局部图；运动背景须另行验证连续性'
        from .clean_references import available_references
        from .imagegen_bridge import can_request
        if can_request(self):
            from .work_autonomy import enabled
            tools['image_reference'] = (
                'request_reference: source_id, one actually observed source times_ms, removal-only prompt, '
                'evidence. The project Codex worker executes native imagegen and returns a synthetic still. '
                + ('Choose further references or frames autonomously when justified. '
                   if enabled(self.task) else 'One request per source, bounded task allowance. ')
                + 'No KIE fallback or unknown replay.')
        if available_references(self):
            tools['reference_plate'] = ('使用clean_generated_references中已登记的内置imagegen参考图，'
                '指定reference_id并自主选择短段region；只本地裁切贴回，不新增模型请求。'
                '最多2秒且包含参考帧，不跨已知镜头，不支持前景polygon，不允许整帧静止替换。'
                '单张图不自动匹配运动/光照，须审阅实际视频边界和时间序列，不要求轻微纹理完美。')
        return tools

    def clean(self):
        if not self.texts:
            sha = self.service.hash_file(self.source)
            return {'path': str(self.source), 'sha256': sha, 'quality_passed': True,
                    'clean_review': {'sha256': sha, 'scope': 'clean_plate',
                                     'evidence': ['Observed inventory has no text marked for replacement'],
                                     'source_ids': []},
                    'accepted': [], '_skip': '原片没有待替换画面文字'}
        design = self.e.output(self.task, self.node, 'visual_analyze')
        policy_resource = self.e.catalog.consult(self.task, self.node, [QUALITY_RESOURCE_ID])[0]
        quality_policy = json.loads(policy_resource['content'])
        state_id = digest({'task': self.task['id'], 'node': self.node['id'], 'source': self.service.hash_file(self.source), 'design': design})
        state = self.store.get('production_session', state_id)
        if state is None:
            existing = [s for s in self.store.list('production_session')
                        if s.get('task_id') == self.task['id'] and s.get('node_id') == self.node['id']]
            if existing:
                raise CapabilityUnavailable(
                    '源设计已变化但已有清底会话；先核对原接受记录与新运动范围，不能另建会话丢失已修复内容')
        state = state or {
            'id': state_id, 'task_id': self.task['id'], 'node_id': self.node['id'], 'turn': 0,
            'base': {'path': str(self.source), 'sha256': self.service.hash_file(self.source)},
            'accepted': [], 'candidate': None, 'history': [], 'times_ms': self.times(), 'finished': False}
        for source_id, override in state.get('text_overrides', {}).items():
            self.texts[source_id].update(override)
        base = self.verified(state['base'])
        from .review_draft import authorized, is_review_draft, handoff, reconsider_handoff
        if reconsider_handoff(self, state):
            self.store.put('production_session', state)
        if state.get('draft_handoff') and is_review_draft(state['draft_handoff'], self.task):
            if state['draft_handoff'].get('source_representation_sha256',
                                         state['draft_handoff']['sha256']) != state['base']['sha256']:
                raise CapabilityUnavailable('审看草稿基底变化，须核对新结果')
            return state['draft_handoff']
        from ..application.materialization_recovery import resume_handoff
        recovered = resume_handoff(self, state)
        if recovered is not None:
            return recovered
        if not state.get('coverage_clock'):
            _, coverage_clock = self.media._info(self.source)
            state['coverage_clock'] = {k: coverage_clock[k] for k in ('rate', 'frames')}
        if state['finished']:
            from .clean_output import finished_output
            result = finished_output(self, state)
            from .stage_quality import require_clean_plate
            require_clean_plate(result)
            if progress(self.texts, state)['unresolved_instances']:
                raise CapabilityUnavailable('清底完成记录仍有未覆盖区间，须复核原结果，不能直接译字')
            return result
        if self.task.get('production_policy', {}).get('mode') == 'completion_first':
            raise CapabilityUnavailable('旧完成优先路线已停用：先恢复逐阶段质量检查，不能将有残字底片交给译字')
        from video_translation.domain.limits import effective_limit
        limit = effective_limit(self.task, 'max_visual_steps', None)
        from .work_autonomy import enabled as open_work
        if open_work(self.task):
            limit = None
        while limit is None or state['turn'] < limit:
            self.active()
            if self.store.require('task', self.task['id']).get('control_paused'):
                return {'_paused': True}
            from .resource_budget import guard_clean_turn, guard_storage
            guard_clean_turn(self.task, state)
            guard_storage(self.service, self.task)
            summary = progress(self.texts, state)
            from .convergence import guard as guard_convergence
            guard_convergence(self, state, summary)
            from .review_draft import bound_work
            bound_work(self, state, summary)
            summary = progress(self.texts, state)
            from .work_conclusions import preserve_output_reserve
            if preserve_output_reserve(self, state):
                return handoff(self, state, summary)
            from .clean_supervision import pending as pending_feedback, acknowledge
            feedback = pending_feedback(self, state)
            if (state.get('completion_feedback_stop', {}).get('execution_binding') == state.get('execution_binding')
                    and state.get('completion_feedback_stop') and not feedback):
                state['routing_stop'] = state['completion_feedback_stop']
                return handoff(self, state, summary)
            scope_review = None
            if (not state.get('candidate') and state.get('pending_turn') is None
                    and not self.node.get('external_pending') and not state.get('archive_review')
                    and any(a.get('needs_scope_review') for a in state['accepted'])):
                _, scope_clock = self.media._info(self.source)
                scope_review = pending_scope_review(self.texts, state, scope_clock)
                if scope_review:
                    state['times_ms'] = scope_review['times_ms']
            if ((summary['all_deferred'] and not state.get('candidate') or feedback)
                    and state.get('pending_turn') is None and not self.node.get('external_pending')
                    and not state.get('archive_review') and not scope_review):
                from .clean_routing import route_deferred
                assigned = route_deferred(self, state, {**summary, 'work_feedback': feedback})
                if feedback:
                    acknowledge(self, state, feedback, assigned)
                if assigned:
                    continue
                state['progress'] = summary
                self.store.put('production_session', state)
                if authorized(self.task):
                    return handoff(self, state, summary)
                disputed = sum(bool(a.get('needs_scope_review')) for a in state['accepted'])
                raise CapabilityUnavailable(
                    '局部清底等待能力或证据：全部剩余实例已暂缓，未新增Agent调用。'
                    f"剩余{summary['unresolved_instances']}个文字实例；{disputed}段历史接受待复核。"
                    '路由结论：' + (state.get('routing_stop') or {}).get('reason', '当前证据与已处理请求相同') +
                    '保留清底工作版和接受结果；明确补充方法/证据后再逐实例解除暂缓，暂不叠加译文。')
            turn = state['turn']
            base = self.verified(state['base'])
            comparison = self.verified(state['candidate']) if state['candidate'] else base
            if state['candidate'] and not state.get('archive_review') and not state.get('pipeline_review'):
                # Eight timestamps * three videos fit the 24-image agent contract.
                _, clock = self.media._info(self.source)
                required_times = [request_time(i, clock['rate']) for i in boundary_indices(
                    state['candidate']['start_ms'], state['candidate']['end_ms'],
                    clock['rate'], clock['frames'])]
                state['times_ms'] = sorted(set(required_times + [
                    t for t in state['times_ms'] if t not in required_times][:8-len(required_times)]))
            from .inspection_evidence import normal_frames
            frames = ([] if state.get('archive_review') or state.get('pipeline_review')
                      else normal_frames(self, state, base, comparison, turn))
            pipeline_review = state.get('pipeline_review') == (state.get('candidate') or {}).get('id') and bool(state.get('pipeline_review'))
            if pipeline_review:
                frames = self.pipeline_frames(state['candidate'], turn, state.get('pipeline_times_ms'))
            archived_review = next((c for c in state.get('candidate_archive', [])
                                    if c['id'] == state.get('archive_review')), None)
            from .clean_references import reference_frames, available_references
            if not archived_review and not pipeline_review and len(frames) < 24:
                frames += reference_frames(self, state)
            archive_changes = None
            archive_pipeline_review = bool(
                archived_review and archived_review.get('pipeline')
                and not state.get('archive_requested_times'))
            if archived_review:
                if archive_pipeline_review:
                    frames = self.pipeline_frames(archived_review, turn, state.get('pipeline_times_ms'))
                else:
                    _, archive_clock = self.media._info(self.source)
                    frames, archive_changes = retained_archive_evidence(
                        self, state, archived_review, turn, archive_clock)
            archive_diagnostics = None
            if archived_review:
                _, archive_clock = self.media._info(self.source)
                archive_diagnostics = candidate_diagnostics(archived_review, archive_clock)
                archive_diagnostics['sampled_pixel_changes'] = archive_changes or sampled_candidate_changes(
                    archived_review, state['base']['sha256'], frames,
                    baseline_label='当前底片', candidate_label='历史候选')
            # A pending paid operation belongs to the original durable decision.
            # Changing prompts/schema during recovery must never create a replacement.
            saved = [r for r in self.store.list('production_decision')
                     if r.get('task_id') == self.task['id'] and r.get('node_id') == self.node['id']
                     and r.get('key') == f'clean-{turn}' and r.get('status') == 'succeeded'
                     and (self.node.get('external_pending') or state.get('pending_turn') == turn)]
            if len(saved) > 1:
                raise Conflict('同一制作轮存在多个决策，请核对后恢复')
            from .context_history import retain_context_history
            history_ref = (retain_context_history(self, state)
                           if not saved and self.task.get('agent_engine') == 'codex' else None)
            action = CleanAction.model_validate(saved[0]['result']).model_dump() if saved else self.decision({
                'retained_history': history_ref,
                'inventory': self.inventory, 'source_design': design,
                'clean_plate_review_requirements': {
                    'review_sha256': state['base']['sha256'],
                    'reviewed_source_ids': sorted(self.texts),
                    'finish_requires': 'All intervals covered, explicit whole-plate quality score and evidence. '
                                       'Review current plate for residual source text; patch count is not approval.',
                },
                'available_tools': self.capabilities(),
                'execution_contract': execution_contract(),
                'coverage_clock': state.get('coverage_clock'),
                'method_reassessment': state.get('method_reassessment'),
                'agent_role': 'work',
                'runtime_work_policy': {
                    'tools': 'native_codex' if self.task.get('agent_engine') == 'codex' else 'configured_transport',
                    'autonomy': 'open' if open_work(self.task) else 'bounded',
                    'fixed_action_limit': not open_work(self.task),
                    'single_reference_per_source': not open_work(self.task),
                    'image_executor': 'project_codex_worker',
                    'cost_ledger_pause_cancel_and_no_unknown_replay': True},
                'review_draft_policy': self.task.get('review_draft_authorization'),
                'output_goal': self.task.get('output_goal', 'quality_approved'),
                'route_work_order': state.get('route_work_order'),
                'new_execution': state.get('new_execution'),
                'work_conclusions': list(state.get('work_conclusions', {}).values()),
                'continuous_work_orders': temporal_work_orders(self, state),
                'historical_retirement': retired_candidates(self.store, state),
                'clean_generated_references': available_references(self, state['base']['sha256']),
                'method_experience_advisories': method_advisories(self.store),
                'accepted': [{k: v for k, v in item.items() if k not in {'evidence', 'path'}}
                             for item in state['accepted']],
                'quality_policy_ref': {k: policy_resource[k] for k in ('id', 'version', 'sha256')},
                'review_calibration': {
                    'rule': '一两帧非文字的轻微边缘/纹理变化允许记录后继续；帧少不豁免真实残字或明显损伤。'
                            '硬伤填写quality.findings的major分类、实际帧号与观看影响；uncertain先补查。'
                            '只看静帧不能声称已正常播放。分数不自动提高，不要求逐帧完美。',
                    'current_candidate_reassessment': (state.get('candidate') or {}).get('quality_reassessment'),
                },
                'required_resource_ids': [QUALITY_RESOURCE_ID] + [
                    r['id'] for r in self.task['resource_snapshot'].get('skills', [])
                    if r['id'] == 'video-translation.media-review'
                    and r.get('status') == 'active' and 'clean' in r.get('nodes', [])] + [
                    r['id'] for r in self.task['resource_snapshot'].get('knowledge', [])
                    if r['id'] == 'video-translation.methods' and r.get('status') == 'active'
                    and (not r.get('nodes') or 'clean' in r['nodes'])],
                'work_queue': coverage_summary(self.texts, state),
                'progress': summary,
                'scope_review': scope_review,
                'acceptance_disputes': state.get('acceptance_disputes', []),
                'historical_candidates': archive_summary(state),
                'archive_under_review': archived_review['id'] if archived_review else None,
                'archive_diagnostics': archive_diagnostics,
                'kie_budget': self.budget_snapshot(),
                'reusable_candidates': [{k: v for k, v in candidate.items() if k != 'path'}
                    for candidate in state.get('candidate_archive', [])
                    if candidate.get('input_sha256') == state['base']['sha256']
                    and candidate.get('review_status') not in {'rejected', 'needs_human_review'}
                    and candidate.get('id') != (state.get('candidate') or {}).get('id')],
                'pipeline_inspection': state.get('pipeline_inspection'),
                'review_mode': 'scope_review' if scope_review else 'pipeline_diagnosis' if pipeline_review or archive_pipeline_review else 'archive_diagnosis' if archived_review else 'candidate_review',
                'excluded_wan_instances': self.wan_exclusions(state),
                'wan_exclusions_scope': '仅限制新的生成，不代替当前候选质量判断。受控quality_reassessment'
                                        '允许对已保存结果重新审阅；旧拒绝保留，不因此自动接受或授权重投。',
                'video_model_trial': self.task.get('clean_video_trial'),
                'local_diagnostics': state.get('local_diagnostics', [])[-3:],
                'tracking_reports': state.get('tracking_reports', [])[-2:],
                'rejected_candidates': [{k: c.get(k) for k in ('id', 'source_id', 'rejection')}
                    for c in state.get('candidate_archive', []) if c.get('review_status') == 'rejected'][-8:],
                'frame_clock': [{k: f[k] for k in ('label', 'time_ms', 'sampled_time_ms',
                                'frame_index', 'next_frame_time_ms', 'exclusive_end_ms')}
                                for f in frames if f.get('kind') not in {'generated_reference', 'still_pipeline'}],
                'observed_times_ms': sorted({f['time_ms'] for f in frames if f.get('time_ms') is not None}),
                'time_basis': 'still_image' if frames and all(f.get('kind') == 'still_pipeline' for f in frames) else 'crop_relative' if pipeline_review or archive_pipeline_review else 'source_absolute',
                'baseline_sha256': state['base']['sha256'],
                'observation_delivery': {
                    'window_id': state.get('visible_observation_key'),
                    'reattached': state.get('visible_observation_key') in state.get('observation_reattachments', {}),
                    'rule': 'Retained frames may be reattached once when not currently visible; no new quality verdict.'},
                'baseline_findings': state.get('baseline_findings', []),
                'resource_mode': 'on_demand', 'resource_ids': state.get('resource_ids', []),
                'candidate': {k:v for k,v in (state['candidate'] or {}).items() if k != 'path'},
                'history': recent_history(state['history']), 'remaining_steps': None if limit is None else limit - turn,
                'instructions': (
                    '你是work agent，负责真实视频观察、局部方法选择、提出处理参数和候选审阅；执行器实际运行工具。'
                    '完整候选优先时，错误与拒绝会交给Route统筹，不必等待所有实例失败。'
                    '按实际观看影响保留普通瑕疵；不为追求满分反复inspect。'
                    '逐帧是媒体计算，不是每帧一轮Agent；优先对已观察且方法适用的连续范围一次处理和审阅。'
                    '不要为压低轮数跨剪辑或遮挡强行合并，也不要无证据逐帧拆解稳定连续文字。'
                    'context_projection为有界历史视图，省略不代表不存在；需要旧细节时按retained_history的路径与SHA只读查ID，再inspect_archive观察实际媒体。'
                    '若有route_work_order，先回答该实例的具体证据问题再决定修复或说明真实缺口，不把路由建议当成已完成观察。'
                    'execution_contract说明当前已实现能力；冻结知识说明选型和质量要求。显式protected_foreground已实现，自动分割未实现，二者不能混淆。'
                    '当前budget为费用依据；历史obstacle里的旧余额或旧能力说明不是当前事实。'
                    '若video_model_trial存在，仅指定source可使用Wan3试修一次，最多64 credits；'
                    '先观察实际短段再选几何，不能跨镜头或清除其他实例的损伤禁用，失败不再投。'
                    'work_queue.acceptance_conflict_resolutions列出旧争议的后续复核：核对SHA和时段，不能继续把已纠正的旧说法当现状；'
                    '复核只适用于所列接受时段，不解封其他暂缓片段，也不证明其他文字属于允许保留项。'
                    'method_experience_advisories是带原接受证据的选型参考，不是通用规则；不能复制旧视频坐标/供体或把历史通过当作当前视频通过。'
                    '按实例/时段选择工具。候选实际观察后按quality_policy打分，达到80分且无硬伤就accept，保留轻微缺陷说明，不追求满分。'
                    '若scope_review存在，优先核对该已接受时段的当前完整画面与acceptance_conflicts；历史高分不能替代观察。'
                    '残字移到旧包络之外时先依据实际帧revise_scope修正运动包络，不能把空白旧区域的修复当作去字。'
                    '确实仍达标才confirm_scope，不重复accept。不能用此动作消除该时段之外的残字缺口。'
                    '每轮检查work_queue全局剩余项；同一区域反复失败且还有未尝试区域时先推进其他实例，不在小标签上耗尽整个流程。'
                    '全历史方法与失败摘要在work_queue，不仅参考最近10轮。'
                    'historical_candidates包含旧基底和拒绝记录；可inspect_archive只读诊断，不能把列表为空当成文件丢失。'
                    '历史已被新候选取代的帧率错误不代表仍未修复，须查看真实历史候选。'
                    '单个转场失败不证明整个实例不可处理；按实际镜头/姿态/遮挡划分窗口，先观察稳定短段及入出边界。'
                    'defer使用start_ms/end_ms只登记已证实困难范围；其他actionable_intervals_ms继续，不以省略时段将未检查余段全部暂缓。'
                    '仅全段有明确不适用证据时省略defer起止；不能反复检查相同暂缓范围，也不能把暂缓记成覆盖或缩短源实例隐藏残字。'
                    'rebase_candidate仅本地重贴，不修复模型原始损伤；重合成仍需正常三方边界验收。'
                    '可复用且有用的候选先观察；旧候选缺链路或质量失败时可放弃，依据原片另选方法，不强制恢复旧产物。'
                    '候选损坏且归因不明时inspect_pipeline读取已有模型输入/原始返回/缩放结果，不产生外部调用。'
                    '链路诊断图是裁切相对时间，不是原片帧；该轮不能accept，需要inspect回到正常三方审阅。'
                    '确认候选不合格后reject_candidate保存失败证据并清除当前候选，优先推进其他未解决实例。'
                    '模型原始返回已存在结构损伤时，本地重贴不能修复；改变处理方法前先观察实际证据。'
                    'archive_diagnostics保留原参数与预期帧；sampled_pixel_changes以经路径/SHA核验的历史输入测量提供帧，不用当前底片替代历史输入。'
                    '视觉认为未变化而像素证据显示变化时，先核对帧号和三方图角色，再判断残字；像素变化不证明去字或质量通过。'
                    '每次repair和accept前核对完整画面内目标字形实际位置是否落在拟修改区域；initial inventory的region只是估计。'
                    '若变化矩形只落在无目标文字的空白区域，即使拟合通过或像素发生变化也不是有效去字，先revise_scope而非重复旧几何。'
                    '候选检查同时对照原片、修复前底片和候选：原片与底片差异属于已有问题，底片与候选差异才是本次改动。'
                    '必须检查修复首尾内外帧，不能用中间帧替代；边界帧切到人物且出现残字或损伤时不得接受。'
                    '服务商已确认失败与结果未知不同；已确认失败记入历史后重新选择方案，禁止重复同一失败参数。'
                    'time_ms是请求时间，真实图像对应sampled_time_ms和frame_index；不要用请求毫秒推断精确切点。'
                    '结束在最后已观察文字帧时使用该帧exclusive_end_ms，避免包含下一幕第一帧。'
                    '短于2秒的同镜头Wan裁切会补尾帧到2秒；不要为满足时长跨镜头取人物或无关画面。'
                    '原始文字清单是初始估计；发现时段错误先inspect，再revise_scope。'
                    '缩短时须在absent_times_ms列出被排除的无该文字帧，并提供新边界内侧文字帧；不能用缩短隐藏残字。'
                    'finish前逐实例确认覆盖；无法处理的真实硬伤明确blocked，不伪造成功。')},
                CleanAction, frames, key=f'clean-{turn}')
            from video_translation.application.task_supervision import live_directions
            # A saved decision resumes its original external request before new directions.
            if not saved and self._sent_directions != digest(
                    live_directions(self.service, self.task["id"], self.node["id"])):
                state.setdefault('held_decisions', []).append({
                    'turn': turn, 'decision': action, 'reason': 'New Route direction arrived during this decision'})
                state['turn'] += 1
                self.store.put('production_session', state)
                continue
            state['history'].append({'turn': turn, 'decision': action,
                                     'input_base_sha256': state['base']['sha256']})
            from .convergence import record_observation
            record_observation(self, state, action, frames)
            if scope_review and action['action'] in {'confirm_scope', 'defer', 'blocked'}:
                state.setdefault('scope_review_attempts', {})[scope_review['key']] = {
                    'turn': turn, 'action': action['action'],
                }
            state.pop('pipeline_review', None)
            state.pop('pipeline_times_ms', None)
            state.pop('archive_review', None)
            state.pop('archive_requested_times', None)
            from .viewing_review import guard_candidate_verdict, guard_review_only
            guard_review_only(self, state, action)
            if action['action'] == 'request_reference':
                from .imagegen_bridge import enqueue
                return enqueue(self, state, action, frames)
            work_order = state.get('route_work_order') or {}
            from .clean_authority import repair_obstacle
            authority_error = repair_obstacle(work_order, action)
            if authority_error:
                state['history'][-1]['tool_error'] = authority_error
                state['turn'] += 1
                self.store.put('production_session', state)
                from .review_draft import complete_candidate
                if complete_candidate(self.task):
                    continue
                raise CapabilityUnavailable(authority_error)
            policy_ref = {k: policy_resource[k] for k in ('id', 'version', 'sha256')}
            if guard_candidate_verdict(self, state, action, frames, quality_policy, policy_ref):
                continue
            if archived_review:
                state.setdefault('archive_inspections', {})[archived_review['id']] = turn
                if (action['action'] in {'blocked', 'defer'} and action.get('quality')
                        and action['quality'].get('hard_failures')
                        and any(a['id'] == action.get('candidate_id') for a in state['accepted'])):
                    try:
                        self.verified(archived_review)
                        observed = {f['frame_index'] for f in frames
                                    if '历史候选' in f['label']}
                        dispute_acceptance(state, archived_review, action, observed,
                                           archive_clock, turn, f'clean-{turn}')
                    except ValueError as exc:
                        state['history'][-1]['tool_error'] = str(exc)
            if action['action'] in {'defer', 'blocked'} and action.get('source_id') in self.texts:
                try:
                    defer_source(self.texts, state, action)
                except ValueError as exc:
                    state['history'][-1]['tool_error'] = str(exc)
                    state['turn'] += 1
                    self.store.put('production_session', state)
                    continue
                if not state.get('candidate'):
                    state['turn'] += 1
                    self.store.put('production_session', state)
                    continue
            if action['action'] in {'blocked', 'defer'}:
                state['turn'] += 1
                self.store.put('production_session', state)
                raise CapabilityUnavailable('局部清底需要补充能力或证据：' + action['reason'])
            if action['action'] == 'consult':
                if not action['knowledge_ids']:
                    raise ValueError('查阅技能/知识必须指定实际资源ID')
                self.e.catalog.consult(self.task, self.node, action['knowledge_ids'])
                state['resource_ids'] = sorted(set(state.get('resource_ids', []) + action['knowledge_ids']))
            elif action['action'] == 'track':
                from .motion_tracking import track_region
                item = self.texts.get(action['source_id'])
                if not item or not item['start_ms'] <= action['start_ms'] < action['end_ms'] <= item['end_ms']:
                    raise ValueError('跟踪范围必须落在当前文字实例内')
                key = digest({'source': self.service.hash_file(self.source), 'action': action, 'tracker': 1})
                cached = self.store.get('motion_track', key)
                if not cached:
                    try:
                        report = track_region(self.media, self.source, start_ms=action['start_ms'],
                                              end_ms=action['end_ms'], region=action['region'])
                    except ValueError as exc:
                        report = {'status': 'invalid_seed_or_range', 'verified': False,
                                  'failure': str(exc), 'frames': []}
                    cached = {'id': key, 'task_id': self.task['id'], 'source_id': item['id'], **report}
                    self.store.put('motion_track', cached)
                state.setdefault('tracking_reports', []).append(cached)
                state['times_ms'] = sorted(set([action['start_ms'], action['end_ms'] - 1]
                                               + [int(r['sampled_time_ms']) for r in cached['frames'][::max(1, len(cached['frames']) // 6)]]))[:8]
            elif action['action'] == 'inspect':
                from .inspection_evidence import allow_reattachment, window_key
                times = action['times_ms']
                if any(t < 0 or t >= self.duration for t in times):
                    raise ValueError('观察时间超出原片')
                key = inspection_key(state['base']['sha256'], action.get('source_id'), times,
                                     state.get('text_overrides', {}))
                semantic_key = window_key(self, state, times)
                # A candidate needs a fresh review even when its requested times coincide.
                if (not state.get('candidate') and (
                        key in state.get('inspection_requests', {})
                        or semantic_key in state.get('inspection_window_requests', {}))
                        and not reuse_trial_inspection(state, self.task.get('clean_video_trial'),
                                                      action.get('source_id'), key)
                        and not allow_reattachment(self, state, times)):
                    state['turn'] += 1
                    state['history'][-1]['tool_error'] = '相同底片和范围已检查；需要新证据或具体修复方法'
                    self.store.put('production_session', state)
                    from .review_draft import complete_candidate
                    if complete_candidate(self.task):
                        continue
                    raise CapabilityUnavailable('已停止重复观察：底片、范围和时间点均未变化；保留原观察记录')
                state.setdefault('inspection_requests', {})[key] = turn
                state.setdefault('inspection_window_requests', {})[semantic_key] = turn
                state['times_ms'] = times
            elif action['action'] in {'inspect_archive', 'rebase_candidate'}:
                try:
                    archived = next((c for c in state.get('candidate_archive', [])
                                     if c['id'] == action['candidate_id']), None)
                    if not archived:
                        raise ValueError('历史候选不存在')
                    self.verified(archived)
                    if action['action'] == 'inspect_archive':
                        times = action['times_ms']
                        if any(t < 0 or t >= self.duration for t in times):
                            raise ValueError('历史观察时间超出原片；未替换为默认采样')
                        if len(times) > 6:
                            raise ValueError('历史候选对照每次最多6个时间点；请按图片上限缩小检查范围')
                        state['archive_review'] = archived['id']
                        state['archive_requested_times'] = times
                        state['times_ms'] = times or self.candidate_times(archived)
                    else:
                        state['candidate'] = self.rebase_candidate(archived, state, turn)
                        state.setdefault('candidate_archive', []).append(dict(state['candidate']))
                        state['times_ms'] = self.candidate_times(state['candidate'])
                except (ValueError, Conflict) as exc:
                    state['history'][-1]['tool_error'] = str(exc)
            elif action['action'] == 'inspect_pipeline':
                from .image_pipeline import request
                try:
                    request(self, state, action)
                except (ValueError, Conflict) as exc:
                    state['history'][-1]['tool_error'] = str(exc)
            elif action['action'] == 'reject_candidate':
                self.reject_candidate(state, action, reviewed_archive=archived_review)
            elif action['action'] == 'restore_candidate':
                archived = next((c for c in state.get('candidate_archive', [])
                                 if c['id'] == action['candidate_id']), None)
                if (not archived or archived.get('review_status') in {'rejected', 'needs_human_review'}
                        or archived.get('input_sha256') != state['base']['sha256']):
                    state['history'][-1]['tool_error'] = '候选不存在或基底已变化，不能覆盖已接受结果'
                else:
                    self.verified(archived)
                    state['candidate'] = dict(archived)
                    state['times_ms'] = self.candidate_times(archived)
            elif action['action'] == 'revise_scope':
                try:
                    self.revise_scope(action, state)
                except ValueError as exc:
                    state['history'][-1]['tool_error'] = str(exc)
            elif action['action'] == 'repair':
                if state.get('candidate') and not any(c['id'] == state['candidate']['id']
                                                     for c in state.get('candidate_archive', [])):
                    state.setdefault('candidate_archive', []).append(dict(state['candidate']))
                state['pending_turn'] = turn
                # Save the decision before dispatch; retries resume this action only.
                entry = state['history'].pop()
                from .convergence import reserve_action
                try:
                    reserve_action(self, state, action, resuming=saved)
                except CapabilityUnavailable as exc:
                    from .review_draft import complete_candidate
                    if not complete_candidate(self.task):
                        raise
                    state['history'].append(entry)
                    entry['tool_error'] = str(exc)
                    state.pop('pending_turn', None)
                    state['turn'] += 1
                    self.store.put('production_session', state)
                    continue
                self.store.put('production_session', state)
                state['history'].append(entry)
                try:
                    if not saved and action['method'] == 'wan' and action['source_id'] in self.wan_exclusions(state):
                        raise ValueError('该实例已有Wan主体/结构损伤证据，停止Wan重试；选择本地或图片方法，缺能力时明确说明')
                    state['candidate'] = self.repair(action, base, turn)
                    state.setdefault('candidate_archive', []).append(dict(state['candidate']))
                    state['times_ms'] = self.candidate_times(state['candidate'])
                except (ValueError, CapabilityUnavailable, ConfirmedJobFailure) as exc:
                    state['history'][-1]['tool_error'] = str(exc)[:1500]
                    state['candidate'] = None
                state.pop('pending_turn', None)
                self.node['external_pending'] = False
            elif action['action'] == 'accept':
                if pipeline_review or archived_review:
                    raise ValueError('链路诊断不能代替原片/底片/候选三方验收')
                candidate = state['candidate']
                if not candidate or action['candidate_id'] != candidate['id']:
                    raise ValueError('不能接受不存在或已被替换的候选')
                self.verified(candidate)
                _, clock = self.media._info(self.source)
                required = set(boundary_indices(candidate['start_ms'], candidate['end_ms'],
                                                clock['rate'], clock['frames']))
                observed = {f['frame_index'] for f in frames if '当前局部修复候选' in f['label']}
                if not required.issubset(observed):
                    raise ValueError('候选首尾内外帧未完整观察，不能接受')
                if candidate.get('input_sha256') != state['base']['sha256']:
                    raise ValueError('候选基底已经变化，必须重新合成后复核')
                score = validate_quality(action.get('quality'), quality_policy)
                state['coverage_clock'] = {k: clock[k] for k in ('rate', 'frames')}
                state['accepted'].append({k:v for k,v in candidate.items() if k != 'path'} |
                                         {'evidence': action['evidence'], 'quality': action.get('quality'), 'score': score,
                                          'quality_policy_ref': policy_ref,
                                          'boundary_review': {'required_frames': sorted(required),
                                              'observed_frames': sorted(observed), 'baseline_sha256': state['base']['sha256']}})
                state['base'] = {'path': candidate['path'], 'sha256': candidate['sha256']}
                state['candidate'] = None
                reconsider_accepted_source(self.texts, state, candidate)
            elif action['action'] == 'confirm_scope':
                observed = {f['frame_index'] for f in frames if '已接受底片' in f['label']}
                try:
                    _, current_clock = self.media._info(self.source)
                    confirm_scope_review(state, scope_review, action, observed, current_clock, quality_policy)
                except ValueError as exc:
                    state['history'][-1]['tool_error'] = str(exc)
            elif action['action'] == 'finish':
                if state['candidate']:
                    raise ValueError('尚有未决定的修复候选')
                if any(row['remaining_intervals_ms'] for row in coverage_summary(self.texts, state)):
                    raise ValueError('不能完成清底：存在尚未覆盖的文字时段')
                if (action.get('review_sha256') != state['base']['sha256']
                        or sorted(action.get('reviewed_source_ids', [])) != sorted(self.texts)):
                    raise ValueError('清底复核必须绑定当前底片SHA并逐项覆盖全部文字ID')
                score = validate_quality(action.get('quality'), quality_policy)
                state['clean_review'] = {
                    'sha256': state['base']['sha256'], 'scope': 'clean_plate',
                    'source_ids': action['reviewed_source_ids'], 'score': score,
                    'quality': action['quality'], 'evidence': action['evidence'],
                    'media_boundary': 'Agent inspected supplied frames; final full-video human review remains required',
                    'decision_key': f'clean-{turn}',
                }
                state['finished'] = True
                state['evidence'] = action['evidence']
            from .work_conclusions import retain
            retain(state, action, turn=turn,
                   base_sha256=state['history'][-1]['input_base_sha256'],
                   frames=frames, tool_error=state['history'][-1].get('tool_error'))
            state['turn'] += 1
            self.store.put('production_session', state)
            if action['action'] == 'accept':
                record_local_experience(self.store, state, self.texts)
            self.service.write_json(self.task['id'], self.node, 'production-session.json', state)
            if state['finished']:
                from .clean_output import finished_output
                return finished_output(self, state)
        if (authorized(self.task) and not state.get('candidate')
                and state.get('pending_turn') is None and not self.node.get('external_pending')):
            state['routing_stop'] = {'reason': 'Authorized clean decision allowance exhausted; '
                                              'preserving current base and unresolved intervals'}
            return handoff(self, state, progress(self.texts, state))
        raise CapabilityUnavailable('已达到局部制作决策次数上限，保存当前结果与未完成项；请检查后调整任务')

    def wan_exclusions(self, state):
        from .fidelity import enabled, wan_exclusions
        return wan_exclusions(state) if enabled(self.task) else {}

    def rebase_candidate(self, candidate, state, turn):
        obstacle = rebase_obstacle(candidate, state)
        if obstacle:
            raise ValueError(obstacle)
        if candidate['id'] not in state.get('archive_inspections', {}):
            raise ValueError('先inspect_archive检查真实历史结果，再决定是否重合成')
        previous = next((c for c in state.get('candidate_archive', [])
                         if c.get('rebased_from') == candidate['id']
                         and c.get('input_sha256') == state['base']['sha256']), None)
        if previous:
            if previous.get('review_status') == 'rejected':
                raise ValueError('该历史结果在当前基底已重合成并被拒绝，不重复')
            self.verified(previous)
            return dict(previous)
        old = self.verified(candidate)
        base = self.verified(state['base'])
        patch = self.directory / f'archive-patch-{turn}.mp4'
        target = self.candidate_target(f'archive-rebased-{turn}')
        self.media.clip(old, patch, candidate['start_ms'], candidate['end_ms'], candidate['region'])
        self.media.patch_video(base, patch, target, start_ms=candidate['start_ms'],
                               end_ms=candidate['end_ms'], region=candidate['region'])
        artifact = self.service.artifact(self.task['id'], self.node, target,
                                        self.candidate_kind(target), '历史补片重合成（待验收）')
        return {k: candidate[k] for k in ('source_id', 'start_ms', 'end_ms', 'region', 'method')} | {
            'id': artifact['id'], 'path': str(target), 'sha256': artifact['sha256'],
            'input_sha256': state['base']['sha256'], 'rebased_from': candidate['id'],
            'review_status': 'needs_review', 'recovery': 'local_pixels_only',
        }

    def budget_snapshot(self):
        from .credit_budget import kie_budget_snapshot
        from video_translation.persistence.transaction import TransactionStore
        with self.store.connection() as db:
            task = TransactionStore(self.store, db).require('task', self.task['id'])
            snapshot = kie_budget_snapshot(db, task)
        from video_translation.application.clean_video_trial import trial_task
        video = trial_task(task)['model_snapshot']['models']['video']
        from video_translation.integrations.providers.video_pricing import video_estimate, WAN3_PROTOCOL
        resolution = video['options']['resolution']
        price = video['estimated_credits_per_second'][resolution]
        snapshot['video_quote'] = {
            'model': video['model'], 'resolution': resolution,
            'credits_per_second': price,
            'minimum_reserved_credits': video_estimate(video, resolution, 2000),
            'basis': ('Frozen task price; input reference plus output duration, both rounded up. '
                      'Local reference padded to integer seconds, minimum 2 seconds.'
                      if video.get('protocol') == WAN3_PROTOCOL else
                      'Frozen task price; ceil(padded duration seconds), minimum 2 seconds. Execution rechecks atomically.'),
        }
        snapshot['as_of'] = now()
        from video_translation.integrations.providers.image_pricing import image_quote
        snapshot['image_quote'] = image_quote(task.get('image_snapshot'))
        snapshot['policy'] = (
            '这是当前账本，优先于历史错误中的旧余额；实际提交仍原子核验费用。'
            '图片/音频新调用无可靠报价时禁止提交。不得绕过预算；Agent费用独立且未知。'
            '有余额不代表可重试已拒绝的相同参数或被禁用的Wan实例。')
        return snapshot

    def revise_scope(self, action, state):
        item = self.texts.get(action['source_id'])
        if item is None:
            raise ValueError('范围修订必须绑定已存在的文字实例')
        start = item['start_ms'] if action['start_ms'] is None else action['start_ms']
        end = item['end_ms'] if action['end_ms'] is None else action['end_ms']
        if not 0 <= start < end <= self.duration:
            raise ValueError('修订时段须在原片内')
        observed = set(state['times_ms'])
        if not action['times_ms'] or not set(action['times_ms']) <= observed or not action['evidence']:
            raise ValueError('范围修订必须引用本轮已提供的观察帧和证据')
        absent = set(action.get('absent_times_ms', []))
        if not absent <= observed:
            raise ValueError('无文字帧必须来自本轮已提供的观察')
        frame_ms = 1000 / self.media._info(self.source)[1]['fps']
        if start > item['start_ms'] and not (
            any(item['start_ms'] <= t < start for t in absent)
            and any(start <= t <= start + frame_ms * 2 for t in action['times_ms'] if t not in absent)
        ):
            raise ValueError('不能缩短起点而缺少排除区间无字帧与新边界内侧文字帧')
        if end < item['end_ms'] and not (
            any(end <= t < item['end_ms'] for t in absent)
            and any(end - frame_ms * 2 <= t < end for t in action['times_ms'] if t not in absent)
        ):
            raise ValueError('不能缩短终点而缺少排除区间无字帧与新边界内侧文字帧')
        if ((start < item['start_ms'] and not any(start <= t < item['start_ms'] for t in action['times_ms']))
                or (end > item['end_ms'] and not any(item['end_ms'] <= t < end for t in action['times_ms']))):
            raise ValueError('扩展时段必须包含该新增区间的实际观察证据，请先inspect')
        region = action.get('region') or item['region']
        old = item['region']
        if (region['x'] > old['x'] + 1e-6 or region['y'] > old['y'] + 1e-6
                or region['x'] + region['width'] < old['x'] + old['width'] - 1e-6
                or region['y'] + region['height'] < old['y'] + old['height'] - 1e-6):
            raise ValueError('只允许扩展运动包络，不能缩小以隐藏未处理文字')
        override = {'start_ms': start, 'end_ms': end, 'region': region}
        state.setdefault('scope_revisions', []).append({
            'source_id': item['id'], 'before': {k: item[k] for k in override}, 'after': override,
            'reason': action['reason'], 'evidence': action['evidence'], 'times_ms': action['times_ms'],
            'absent_times_ms': sorted(absent),
        })
        if region != old:
            for entry in state['accepted']:
                if entry['source_id'] == item['id']:
                    entry['needs_scope_review'] = True
        item.update(override)
        state.setdefault('text_overrides', {})[item['id']] = override
        state['candidate'] = None

    def repair(self, action, base, turn):
        item = self.texts.get(action['source_id'])
        if item is None:
            raise ValueError('局部修复必须绑定当前待译文字实例')
        start = action['start_ms'] if action['start_ms'] is not None else item['start_ms']
        end = action['end_ms'] if action['end_ms'] is not None else item['end_ms']
        if not item['start_ms'] <= start < end <= item['end_ms']:
            raise ValueError('修复时间必须位于所选文字实例内，分段处理无需裁掉其余视频')
        region = action['region'] or item['region']
        original_region = item['region']
        epsilon = 1e-6
        if (region['x'] > original_region['x'] + epsilon or region['y'] > original_region['y'] + epsilon
                or region['x'] + region['width'] < original_region['x'] + original_region['width'] - epsilon
                or region['y'] + region['height'] < original_region['y'] + original_region['height'] - epsilon):
            raise ValueError('修复区域必须完整覆盖当前文字的运动包络；不能修小块后把整实例标记完成')
        method = action['method']
        if method not in self.capabilities():
            raise CapabilityUnavailable('当前任务没有可用或获准的该制作工具')
        target = self.candidate_target(f'repair-{turn}')
        if method == 'tracked_donor':
            from .tracked_donor import apply_tracked_donor
            apply_tracked_donor(self.media, base, target, start_ms=start, end_ms=end, envelope=region,
                               donor_time_ms=action['donor_time_ms'], donor_region=action['donor_region'],
                               recipe=action['tracked_donor'], occlusion=action.get('protected_foreground', []))
        elif method == 'local_surface':
            from .surface_repair import restore_surface
            restore_surface(self.media, base, target, start_ms=start, end_ms=end, envelope=region,
                            recipe=action['surface_repair'],
                            occlusion=action.get('protected_foreground', []))
        elif method in {'flat','donor'}:
            self.media.restore(base, target, start_ms=start, end_ms=end, region=region, method=method,
                               background=action['background'], donor_time_ms=action['donor_time_ms'], donor_region=action['donor_region'])
        elif method == 'reference_plate':
            from .clean_references import repair_reference
            repair_reference(self, action, base, target, turn, region, start, end)
        elif method == 'image_edit':
            from PIL import Image
            frame = self.frames(base, [start], '修复参考帧', f'image-input-{turn}')[0]
            image = Image.open(frame['path']).convert('RGB')
            x, y, w, h = self.media.pixel_region(region, image.width, image.height)
            crop = self.directory / f'reference-{turn}.png'
            image.crop((x,y,x+max(1,w),y+max(1,h))).save(crop)
            generated = self.directory / f'edited-{turn}.png'
            self.e.edit_image(self.task,self.node,crop,generated,action['prompt'] or
                'Remove only the lettering, outline and shadows; reconstruct the actual background. Preserve subject and texture; no new words or objects.', transparent=False)
            with Image.open(generated) as edited:
                edited.convert('RGB').resize((max(1,w),max(1,h)),Image.Resampling.LANCZOS).save(self.directory/f'plate-{turn}.png')
            self.media.restore(base,target,start_ms=start,end_ms=end,region=region,method='image',image_path=self.directory/f'plate-{turn}.png')
        else:
            from video_translation.application.clean_video_trial import trial_task
            video_task = trial_task(self.task)
            trial = self.task.get('clean_video_trial')
            if trial and action['source_id'] != trial['source_id']:
                raise ValueError('Wan3单次试修仅限指定实例')
            if region['width'] >= 1 - 1e-6 and region['height'] >= 1 - 1e-6:
                raise ValueError('Wan只允许按文字实例局部裁切，请选择保护主体的局部区域，不能上传全画幅视频')
            context_start = action.get('context_start_ms')
            context_end = action.get('context_end_ms')
            context_start = start if context_start is None else context_start
            context_end = end if context_end is None else context_end
            if not 0 <= context_start <= start < end <= context_end <= self.duration:
                raise ValueError('Wan上下文必须在原片内并包含编辑时段')
            if not 0 < context_end-context_start <= 10000:
                raise ValueError('Wan真实上下文须在0–10秒内；不足2秒只补尾帧，不跨镜头')
            if trial:
                from video_translation.integrations.providers.video_pricing import video_estimate
                spec = video_task['model_snapshot']['models']['video']
                if video_estimate(spec, spec['options']['resolution'],
                                  context_end-context_start) > trial['max_credits']:
                    raise ValueError('Wan3试修上下文超出单次64 credits额度，请观察更短的同镜头片段')
            clip = self.directory / f'clip-{turn}.mp4'
            info = self.media.clip(base,clip,context_start,context_end,region)
            reference_clip = clip
            from video_translation.integrations.providers.video_pricing import reference_duration_ms
            model_duration = reference_duration_ms(info['duration_ms'],
                video_task['model_snapshot']['models']['video']['protocol'])
            padded_ms = max(0, model_duration - info['duration_ms'])
            if padded_ms:
                reference_clip = self.directory / f'clip-padded-{turn}.mp4'
                self.e.media._run([self.e.media.ffmpeg, '-v', 'error', '-y', '-i', clip, '-an',
                    '-vf', f'tpad=stop_mode=clone:stop_duration={padded_ms/1000}',
                    '-t', str(model_duration/1000), '-c:v', 'libx264rgb', '-crf', '0', '-pix_fmt', 'rgb24', reference_clip])
                info = self.e.media.probe(reference_clip)
            model_clip = reference_clip
            width, height = info['width'], info['height']
            if not 1 / 8 <= width / height <= 8:
                raise ValueError('Wan局部宽高比必须在1:8至8:1，请在原片内扩大上下文区域')
            if min(width, height) < 240:
                import math
                scale = 240 / min(width, height)
                model_width, model_height = math.ceil(width * scale / 2) * 2, math.ceil(height * scale / 2) * 2
                if max(model_width, model_height) > 4096:
                    raise ValueError('局部区域无法在模型尺寸限制内缩放')
                model_clip = self.directory / f'model-clip-{turn}.mp4'
                self.e.media._run([self.e.media.ffmpeg, '-v', 'error', '-y', '-i', reference_clip, '-an',
                    '-vf', f'scale={model_width}:{model_height}:flags=lanczos', '-t', str(model_duration/1000),
                    '-fs', '100000000', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', model_clip])
                info = self.e.media.probe(model_clip)
                if abs(info['duration_ms'] - model_duration) > 100:
                    raise ValueError('模型输入裁切转码不完整')
            generated = self.directory/f'wan-{turn}.mp4'
            prompt = 'Remove only the existing lettering and its outline/shadow. Restore continuous natural background, preserve all subjects and camera motion. Do not add translated text, new objects or a new scene. ' + action['prompt']
            if padded_ms:
                prompt += ' The final repeated frames are padding only; do not introduce another scene.'
            if (context_start, context_end) != (start, end):
                prompt += (f' The edit window is {(start-context_start)/1000:.3f}'
                           f' to {(end-context_start)/1000:.3f} seconds within this clip;'
                           ' surrounding frames are context. Preserve transitions and reflections.')
            identity = {'source_sha256':self.service.hash_file(model_clip),'prompt':prompt,'model':self.e.kie_for(video_task).video_model,
                        'selection':video_task['model_snapshot']['sha256'],'resolution':self.e.kie_for(video_task).video_options['resolution'],
                        'duration_ms':info['duration_ms'],'region':region,'start_ms':start,'end_ms':end,
                        'context_start_ms':context_start,'context_end_ms':context_end}
            if trial:
                identity.update(clean_video_trial=trial['id'], source_id=action['source_id'])
            self.e.kie(video_task,self.node,'video_edit',identity,
                lambda client,key,event:client.video_edit(model_clip,generated,prompt,media=info,request_key=key,on_event=event))
            conformed = self.directory/f'wan-conformed-{turn}.mp4'
            self.e.media.conform_video_edit(reference_clip,generated,conformed)
            patch = conformed
            if padded_ms or (context_start, context_end) != (start, end):
                patch = self.directory/f'wan-edit-window-{turn}.mp4'
                import math
                from fractions import Fraction
                _, clock = self.media._info(base)
                rate = Fraction(clock['rate'])
                # The crop begins at the first included source frame, not at the
                # requested millisecond. Subtract frame indices before trimming.
                first = math.ceil(start * rate / 1000)
                offset = first - math.ceil(context_start * rate / 1000)
                count = math.ceil(end * rate / 1000) - first
                self.media.clip_frames(conformed, patch, offset, count)
            self.media.patch_video(base,patch,target,start_ms=start,end_ms=end,region=region,
                                   blend_core=item['region'])
        artifact = self.service.artifact(self.task['id'],self.node,target,self.candidate_kind(target),
                                         f'局部清底候选 {turn+1}')
        return {'id':artifact['id'],'path':str(target),'sha256':artifact['sha256'],'source_id':item['id'],
                **({'pipeline': {'media_type': 'still_image',
                    **{key: {'path': str(path), 'sha256': self.service.hash_file(path)}
                       for key, path in (('input', crop), ('generated', generated),
                                         ('conformed', self.directory / f'plate-{turn}.png'))}}}
                   if method == 'image_edit' else {}),
                **({'reference_id': action['reference_id'], 'image_provider': 'codex_builtin_imagegen'}
                   if method == 'reference_plate' else {}),
                **({'pipeline': {
                    **{key: {'path': str(path), 'sha256': self.service.hash_file(path)}
                       for key, path in (('input', model_clip), ('generated', generated), ('conformed', conformed))},
                    'duration_ms': info['duration_ms'],
                    'context_start_ms': context_start,
                }} if method == 'wan' else {}),
                **({'context_start_ms': context_start, 'context_end_ms': context_end} if method == 'wan' else {}),
                **({'video_model': video_task['model_snapshot']['models']['video']['model'],
                    'video_trial_id': trial['id'] if trial else None} if method == 'wan' else {}),
                **({'surface_repair': action['surface_repair'],
                    'protected_foreground': action.get('protected_foreground', [])} if method == 'local_surface' else {}),
                **({'tracked_donor': action['tracked_donor'], 'donor_time_ms': action['donor_time_ms'],
                    'donor_region': action['donor_region'],
                    'protected_foreground': action.get('protected_foreground', [])} if method == 'tracked_donor' else {}),
                'start_ms':start,'end_ms':end,'region':region,'method':method,'input_sha256':self.service.hash_file(base)}

    def candidate_target(self, stem):
        suffix = '.vpatch.json' if hasattr(self.media, 'workspace') else '.mp4'
        return self.directory / (stem + suffix)

    def candidate_kind(self, path):
        return 'video_patch' if str(path).endswith('.vpatch.json') else 'video'
