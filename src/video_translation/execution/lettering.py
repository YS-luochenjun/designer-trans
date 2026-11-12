"""Source-led lettering and inspect/revise loops on a verified clean plate."""
from pathlib import Path
from PIL import Image
from video_translation.domain.limits import allows_new_call

from .adaptive import AdaptiveProduction, digest
from .media import MediaTools
from .visual_media import VisualMedia
from video_translation.domain.production import AdaptivePlan, LetteringDecision
from video_translation.integrations.agents.errors import CapabilityUnavailable
from video_translation.persistence.store import now


class LetteringProduction(AdaptiveProduction):
    def plan(self):
        design = self.e.output(self.task,self.node,'visual_analyze')
        translation = self.e.output(self.task,self.node,'translate')
        if self.e.kie_for(self.task):
            from video_translation.integrations.providers.kie import VOICES
            voices = sorted(VOICES)
        else:
            voice = getattr(getattr(self.e.speech,'config',None),'voice',None)
            voices = [voice] if voice else ['default']
        if not self.texts and not self.inventory['speech']:
            return {'lettering': [], 'speakers': [], 'preserve_music': True, 'notes': [],
                    '_skip': '没有待译文字或人声'}
        result = self.decision({'inventory':self.inventory,'source_design':design,'translation':translation,
            'fonts':list(self.fonts()),'voices':voices,'locale':self.node['locale'],
            'image_artwork_available':allows_new_call(self.task, 'max_image_calls') and self.e.image_ready(self.task,transparent=True),
            'preserve_background':self.e.preserve_background(self.task,self.inventory),
            'revision_feedback':self.task.get('revision_feedback',{}).get(self.node['asset_id']+':'+self.node['locale'],[]),
            'instructions':'逐实例适配字体/描边/间距/轨迹；每角色稳定选择真实可用声线。不要用整片模型路线替代制作。复杂遮挡尚无工具时说明。口播字幕必须按关联speech_id使用最终实际口播定稿。'},
            AdaptivePlan,self.frames(self.source,self.times(),'源文字设计','plan-source'),key='lettering-and-voice-plan')
        self.validate_plan(result)
        return result

    def validate_plan(self,plan):
        ids=[p['source_id'] for p in plan['lettering']]
        if set(ids)!=set(self.texts) or len(ids)!=len(set(ids)):
            raise ValueError('字层计划必须完整逐项覆盖待译文字')
        expected={s['speaker'] for s in self.inventory['speech']}
        speakers=[s['speaker'] for s in plan['speakers']]
        if set(speakers)!=expected or len(speakers)!=len(set(speakers)):
            raise ValueError('声音计划必须为每个实际角色选择稳定声线')
        if self.e.preserve_background(self.task,self.inventory) and not plan['preserve_music']:
            raise ValueError('Agent不能自行放弃保留背景声')
        for entry in plan['lettering']:
            self.check_motion(entry['motion'],self.texts[entry['source_id']])
            if entry['style']['font_id'] not in self.fonts():
                raise ValueError('所选字体在此设备未安装')

    def layers(self, plan):
        translated={v['source_id']:v['text'] for v in self.e.output(self.task,self.node,'translate')['items']}
        spoken=self.e.output(self.task,self.node,'audio_voice').get('spoken_texts',{})
        design={d['source_id']:d for d in self.e.output(self.task,self.node,'visual_analyze')['lettering']}
        source_regions = {t['id']: t['region'] for t in self.e.output(self.task,self.node,'observe')['texts']}
        source_texts = {t['id']: t for t in self.e.output(self.task,self.node,'observe')['texts']}
        layers=[]
        from .review_draft import is_review_draft
        draft = is_review_draft(self.e.output(self.task, self.node, 'clean'), self.task)
        for entry in plan:
            source_id=entry['source_id']
            original=self.texts[source_id]
            from .stage_quality import require_motion_evidence
            draft_limits = []
            try:
                require_motion_evidence(design[source_id])
            except CapabilityUnavailable:
                if not draft:
                    raise
                draft_limits.append('源运动证据不足，草稿按已记录设计近似；未声称运动匹配通过')
            missing_occlusion = design[source_id].get('needs_occlusion') and not design[source_id].get('occlusion')
            if missing_occlusion and not draft:
                raise CapabilityUnavailable('源文字涉及前景遮挡：尚缺有依据的遮挡关键帧蒙版，不能当作普通覆盖完成')
            if missing_occlusion:
                draft_limits.append('缺少前景遮挡关键帧，草稿未验证遮挡效果')
            speech_id=design[source_id].get('speech_id')
            text=spoken.get(speech_id,translated[source_id])
            from .fidelity import enabled, preserve_lettering
            style, motion, fidelity = entry['style'], entry['motion'], {}
            region = original['region']
            if enabled(self.task):
                style, motion, fidelity = preserve_lettering(entry, design[source_id])
                self.check_motion(motion, original)
                # A wider cleanup mask must not enlarge or move the replacement text.
                region = source_regions[source_id]
                fidelity['region_basis'] = 'source_inventory'
                fidelity['cleanup_region'] = original['region']
            if draft_limits:
                fidelity['draft_limitations'] = draft_limits
            occlusion = design[source_id].get('occlusion', [])
            source_text = source_texts[source_id]
            if occlusion and (original['start_ms'], original['end_ms']) != (
                    source_text['start_ms'], source_text['end_ms']):
                # A scope correction changes the layer window, not the observed
                # foreground motion. Sample the same polygon curve at its new edges.
                keys = self.media._occlusion(
                    occlusion, source_text['start_ms'], source_text['end_ms'])
                times = sorted({original['start_ms'], original['end_ms'],
                                *[k['time_ms'] for k in keys
                                  if original['start_ms'] < k['time_ms'] < original['end_ms']]})
                occlusion = [
                    {'time_ms': time, 'points': [{'x': x, 'y': y}
                     for x, y in self.media._polygon_at(keys, time)]} for time in times]
                fidelity['occlusion_window_basis'] = 'source_curve_on_corrected_text_window'
            layers.append({'source_id':source_id,'text':text,'start_ms':original['start_ms'],'end_ms':original['end_ms'],
                'region':region,'style':style,'motion':motion,'fidelity':fidelity,
                'occlusion':occlusion,
                'treatment':entry['treatment'],'artwork_prompt':entry['artwork_prompt'],'rationale':entry['rationale']})
        return layers

    def render(self,plan):
        clean=self.e.output(self.task,self.node,'clean')
        from .stage_quality import require_production_base
        require_production_base(clean, self.task)
        base=self.verified(clean)
        layers=self.layers(plan)
        completion = self.task.get('production_policy', {}).get('mode') == 'stage_verified'
        if completion:
            _, clock = self.media._info(base)
            for layer in layers:
                end = min(layer['end_ms'], clock['duration_ms'])
                if layer['start_ms'] >= end:
                    raise ValueError('必译字层位于真实视频帧之外，不能省略')
                for field in ('motion', 'occlusion'):
                    keys = layer[field]
                    if keys and keys[-1]['time_ms'] > end:
                        if field == 'motion':
                            final = dict(self.media._position(keys, end))
                        else:
                            validated = self.media._occlusion(keys, layer['start_ms'], layer['end_ms'])
                            final = {'points': [{'x': x, 'y': y} for x, y in self.media._polygon_at(validated, end)]}
                        layer[field] = [k for k in keys if k['time_ms'] < end] + [{**final, 'time_ms': end}]
                layer['end_ms'] = end
        font_dependencies = {name: self.service.hash_file(Path(path)) for name, path in self.fonts().items()}
        emoji = Path('C:/Windows/Fonts/seguiemj.ttf')
        if completion and emoji.is_file():
            font_dependencies['emoji_fallback'] = self.service.hash_file(emoji)
        identity=digest({'task':self.task['id'],'node':self.node['id'],'clean_sha256':clean['sha256'],
                         'layers':layers, 'renderer': 2, 'fonts': font_dependencies,
                         'draft_authorization_id': clean.get('draft_authorization_id')})
        prior=self.store.get('production_tool',identity)
        if prior:
            self.verified(prior)
            for artifact in prior.get('layer_artifacts', []):
                self.verified(artifact)
            return prior
        current=base
        artifacts=[]
        info=self.prepared['media']
        for index,layer in enumerate(layers):
            self.active()
            style=dict(layer['style'])
            font_id=style.pop('font_id')
            font=self.fonts().get(font_id)
            if not font:
                raise CapabilityUnavailable('当前设备缺少计划指定的字体')
            tools=VisualMedia(MediaTools(self.e.media.ffmpeg,self.e.media.ffprobe,timeout=self.e.media.timeout,font=font))
            rect=tools.rectangle(info,layer['region'])
            layer_id=digest({'task':self.task['id'],'asset_id':self.node['asset_id'],'source_sha256':self.service.hash_file(self.source),
                'layer':layer,'font_sha256':self.service.hash_file(Path(font)),
                'renderer': 2, 'fallback': font_dependencies.get('emoji_fallback')})
            layer_cached=self.store.get('production_layer',layer_id)
            layer_path=self.directory/f'{layer_id[:12]}-layer.png'
            if layer_cached:
                layer_path=self.verified(layer_cached)
                layer['fidelity'].update(layer_cached.get('render_fidelity', {}))
            elif layer['treatment']=='artwork':
                if not self.e.image_ready(self.task,transparent=True):
                    raise CapabilityUnavailable('当前图片工具不支持已核验的透明艺术字输出')
                reference=self.frames(self.source,[layer['start_ms']],'源文字风格',f'artwork-{index}')[0]
                source_crop=self.directory/f'{identity[:12]}-style-{index}.png'
                with Image.open(reference['path']) as image:
                    image.crop((rect['x'],rect['y'],rect['x']+rect['width'],rect['y']+rect['height'])).save(source_crop)
                prompt=('Rebuild only the lettering as a transparent text asset, preserving the reference lettering design, stroke and effects. '
                        'Write exactly this target text: '+layer['text']+'. No background or other objects. '+layer['artwork_prompt'])
                generated=self.directory/f'{identity[:12]}-artwork-{index}.png'
                self.e.edit_image(self.task,self.node,source_crop,generated,prompt,transparent=True)
                with Image.open(generated) as image:
                    if 'A' not in image.getbands() or image.getchannel('A').getextrema()[0]==255:
                        raise ValueError('艺术字输出没有实际透明背景')
                    image.convert('RGBA').resize((rect['width'],rect['height']),Image.Resampling.LANCZOS).save(layer_path)
            else:
                initial_size = style['font_size']
                fallback_used = []
                while True:
                    try:
                        try:
                            tools.letter_layer(layer['text'],layer_path,width=rect['width'],height=rect['height'],style=style)
                        except ValueError as exc:
                            emoji_font = Path('C:/Windows/Fonts/seguiemj.ttf')
                            if not completion or 'lacks a required glyph' not in str(exc) or not emoji_font.is_file():
                                raise
                            from .font_fallback import render_fallback
                            fallback_used = render_fallback(
                                layer['text'], layer_path, (rect['width'], rect['height']),
                                font, [emoji_font], style)
                        break
                    except ValueError as exc:
                        if not completion or 'Text does not fit' not in str(exc) or style['font_size'] <= 8:
                            raise
                        style['font_size'] -= 1
                if style['font_size'] != initial_size:
                    layer['fidelity']['fit_font_size'] = style['font_size']
                if fallback_used:
                    layer['fidelity']['fallback_fonts'] = [
                        {'path': path, 'sha256': self.service.hash_file(Path(path))}
                        for path in fallback_used]
                    layer['fidelity'].setdefault('limitations', []).append('表情使用真实回退字体单色渲染，颜色与源图可能不同')
            artifact=self.service.artifact(self.task['id'],self.node,layer_path,'image','独立透明译文字层')
            layer_record={'source_id':layer['source_id'],'artifact_id':artifact['id'],'sha256':artifact['sha256'],'path':str(layer_path),
                          'render_fidelity': layer['fidelity']}
            artifacts.append(layer_record)
            if not layer_cached:
                self.store.put('production_layer', {'id':layer_id,'task_id':self.task['id'],'node_id':self.node['id'],
                    'created_at':now(),**layer_record})
            target=self.directory/f'{identity[:12]}-lettering-{index}.mp4'
            tools.typography(current,target,[{'artwork_path':str(layer_path),'start_ms':layer['start_ms'],
                'end_ms':layer['end_ms'],'region':layer['region'],'motion':layer['motion'],'occlusion':layer['occlusion']}])
            current=target
        result={'id':identity,'task_id':self.task['id'],'node_id':self.node['id'],'created_at':now(),
                'path':str(current),'sha256':self.service.hash_file(current),'layers':layers,'layer_artifacts':artifacts,
                'clean_sha256':clean['sha256'],'plan':plan}
        from .review_draft import metadata
        result.update(metadata(clean, self.task))
        self.store.put('production_tool',result)
        self.service.write_json(self.task['id'],self.node,'lettering-layers.json',result)
        return result

    def typography(self):
        from .stage_quality import require_production_base
        require_production_base(self.e.output(self.task,self.node,'clean'), self.task)
        plan=self.e.output(self.task,self.node,'plan')['lettering']
        if not plan:
            clean=self.e.output(self.task,self.node,'clean')
            return {**clean,'plan':[],'layers':[],'_skip':'没有需要制作的译文字层'}
        failed = set()
        repair_evidence = []
        while True:
            self.active()
            try:
                result = self.render(plan)
                if repair_evidence:
                    result["render_repairs"] = repair_evidence
                return result
            except ValueError as exc:
                error = str(exc)[:1200]
            try:
                effective = self.layers(plan)
            except ValueError:
                effective = plan
            # Rationale wording cannot turn the same failed render into a new attempt.
            effective = [{k: v for k, v in layer.items() if k not in {"rationale", "fidelity"}
                          and (k != "artwork_prompt" or layer.get("treatment") == "artwork")}
                         for layer in effective]
            failure = digest({"layers": effective, "error": error})
            if failure in failed:
                raise CapabilityUnavailable("字层修复未改变失败方案，保留原错误：" + error)
            failed.add(failure)
            clean = self.e.output(self.task, self.node, 'clean')
            # The same node owns invalid render parameters; visual review only
            # receives an actually rendered candidate.
            action = self.decision({
                "agent_role": "work", "plan": plan, "render_error": error,
                "source_design": self.e.output(self.task, self.node, "visual_analyze"),
                "translation": self.e.output(self.task, self.node, "translate"),
                "fonts": list(self.fonts()), "candidate_sha256": clean["sha256"],
                "instructions": (
                    "译文字层本地渲染失败，本节点尚未完成。根据实际错误与源设计修正必要字层参数。"
                    "返回revise及修改的字层；不能accept尚未生成的译片，不能以清底代替译片。"
                    "若无可行参数返回blocked并说明具体缺口。保留未受影响实例和原视觉风格。"),
            }, LetteringDecision,
                self.frames(self.source, self.times()[:8], "源文字设计", f"render-repair-{len(failed)}"),
                key="lettering-render-repair-" + failure)
            if action["candidate_sha256"] != clean["sha256"]:
                raise ValueError("字层修订未绑定当前清底")
            changes = {p["source_id"]: p for p in action["revisions"]}
            if (action["action"] != "revise" or not changes or len(changes) != len(action["revisions"])
                    or not set(changes).issubset(self.texts)):
                raise CapabilityUnavailable("字层渲染未完成：" + error + "；" + action["reason"])
            revised = [changes.get(p["source_id"], p) for p in plan]
            if revised == plan:
                raise CapabilityUnavailable("字层修订未改变参数，保留原错误：" + error)
            repair_evidence.append({"render_error": error, "evidence": action["evidence"],
                                    "changed_source_ids": sorted(changes)})
            plan = revised

    def visual(self):
        result=self.e.output(self.task,self.node,'typography')
        from .stage_quality import require_production_base
        require_production_base(self.e.output(self.task,self.node,'clean'), self.task)
        from .review_draft import is_review_draft
        draft = is_review_draft(result, self.task)
        if not self.texts:
            return {**result, "visual_review": "no_translated_text_required",
                    "_skip": "原片没有需要重建的译文字层"}
        plan=result['plan']
        instances=list(self.texts.values())
        batches=[instances[i:i+2] for i in range(0,len(instances),2)]
        batch_index=0
        times=self.times(batches[0])
        base_times=set(times)
        seen_times=set()
        batch_evidence=[]
        revisions=0
        max_revisions=self.task.get('max_revisions',1)
        limit=len(batches)*(max_revisions+1)+max_revisions+2
        for turn in range(limit):
            self.active()
            frames=self.frames(self.source,times,'原片',f'visual-source-{turn}')
            clean=self.e.output(self.task,self.node,'clean')
            frames+=self.frames(self.verified(clean),times,'无字底片',f'visual-clean-{turn}')
            frames+=self.frames(self.verified(result),times,'当前译片',f'visual-target-{turn}')
            seen_times.update(times)
            action=self.decision({'source_design':self.e.output(self.task,self.node,'visual_analyze'),
                'review_source_ids':[item['id'] for item in batches[batch_index]],
                'observed_times_ms':sorted(seen_times),
                'translation':self.e.output(self.task,self.node,'translate'),'plan':plan,
                'rendered_layers': result.get('layers', []),
                'candidate_sha256':result['sha256'],'render_error':result.get('render_error'),
                'remaining_reviews':limit-turn,
                'review_draft': draft, 'known_cleanup_gaps': clean.get('unresolved', []),
                'instructions':'三路对照检查残字、背景接缝、字形布局运动和主体。接受必须是实际看到的当前SHA。字层失败可局部revise，补充inspect；清底问题须blocked，不能用新增底板遮盖。'
                    + ('当前是用户授权的尽力审看草稿。真实缺陷照实记录；不要求细节完美，不将草稿当质量通过。blocked时保留可播放版本供人判断。' if draft else '')},
                LetteringDecision,frames,key=f'visual-review-{turn}')
            if action['candidate_sha256']!=result['sha256']:
                raise ValueError('画面评审未绑定当前候选哈希')
            if action['action']=='accept':
                if result.get('render_error'):
                    raise ValueError('不能接受字层尚未生成的候选')
                if not base_times.issubset(seen_times):
                    raise ValueError('该批文字尚缺完整抽样比较')
                batch_evidence.append({'source_ids':[item['id'] for item in batches[batch_index]],
                    'times_ms':sorted(seen_times),'evidence':action['evidence'],'sha256':result['sha256']})
                batch_index+=1
                if batch_index < len(batches):
                    times=self.times(batches[batch_index])
                    base_times=set(times)
                    seen_times=set()
                    continue
                if draft and any(b.get('known_issue') for b in batch_evidence):
                    return self.draft_visual_result(result, batch_evidence)
                artifact=self.service.artifact(self.task['id'],self.node,Path(result['path']),'video','已检查画面候选')
                return {**result,'artifact_id':artifact['id'],
                    'visual_evidence':[value for batch in batch_evidence for value in batch['evidence']],
                    'visual_batches':batch_evidence,'visual_review':'agent_checked'}
            if action['action']=='blocked':
                if draft and not result.get('render_error'):
                    batch_evidence.append({
                        'source_ids': [item['id'] for item in batches[batch_index]],
                        'times_ms': sorted(seen_times), 'evidence': action['evidence'],
                        'sha256': result['sha256'], 'known_issue': action['reason']})
                    batch_index += 1
                    if batch_index < len(batches):
                        times = self.times(batches[batch_index])
                        base_times, seen_times = set(times), set()
                        continue
                    return self.draft_visual_result(result, batch_evidence)
                raise CapabilityUnavailable('画面对照未通过：'+action['reason'])
            if action['action']=='inspect':
                if not action['times_ms'] or len(set(action['times_ms'])) > 8:
                    raise ValueError('三路补充观察需要1–8个时间点，保证不超过Agent的24张图上限')
                times=action['times_ms']
                continue
            changes={p['source_id']:p for p in action['revisions']}
            if not changes or not set(changes)<=set(self.texts) or len(changes)!=len(action['revisions']):
                raise ValueError('局部修订必须指定当前文字实例')
            for source_id,entry in changes.items():
                self.check_motion(entry['motion'],self.texts[source_id])
            if revisions >= max_revisions:
                if draft and not result.get('render_error'):
                    batch_evidence.append({'evidence': action['evidence'],
                                           'known_issue': '字层局部修订额度已用尽，未完成所有批次审核'})
                    return self.draft_visual_result(result, batch_evidence)
                raise CapabilityUnavailable('当前画面局部修订已达到任务授权上限')
            revisions+=1
            plan=[changes.get(p['source_id'],p) for p in plan]
            batch_index=0
            times=self.times(batches[0])
            base_times=set(times)
            seen_times=set()
            batch_evidence=[]
            try:
                result=self.render(plan)
            except ValueError as exc:
                result={**result,'render_error':str(exc)[:1200],'plan':plan}
        if draft and not result.get('render_error'):
            batch_evidence.append({'evidence': [], 'known_issue': '审看次数已用尽，部分批次尚未完成对照'})
            return self.draft_visual_result(result, batch_evidence)
        raise CapabilityUnavailable('画面对照达到修订上限，保留候选与缺陷记录，尚未通过')

    def draft_visual_result(self, result, batches):
        artifact = self.service.artifact(self.task['id'], self.node, Path(result['path']),
                                         'video', '尽力审看画面草稿（有已知问题）')
        return {**result, 'artifact_id': artifact['id'], 'visual_review': 'draft_checked_with_issues',
                'visual_batches': batches,
                'visual_evidence': [e for b in batches for e in b.get('evidence', [])],
                'limitations': [*result.get('limitations', []),
                                *[b['known_issue'] for b in batches if b.get('known_issue')]]}
