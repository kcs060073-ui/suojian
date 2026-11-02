"""Optional Seedance per-shot execution using the previously verified Cutroom POC."""
from __future__ import annotations
import importlib.util
import json
import shutil
import threading
import time
import uuid
from pathlib import Path
from workbench.production import LOCK, Conflict, read, save
from workbench.vibe_storyboard import Storyboards, digest_file


class Videos:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.boards = Storyboards(self.root)

    def start(self, task_id, payload, worker=None):
        with LOCK:
            task = self.boards._task(task_id, payload)
            state = self.boards.load(task)
            if not state or state['serial'] != payload.get('expectedSerial'):
                raise Conflict('分镜状态已变化，请刷新')
            shot = next((s for s in state['shots'] if s['id'] == payload.get('shotId')), None)
            if not shot:
                raise ValueError('镜头不存在')
            self.boards.require_plans(task_id)
            board = next(b for b in shot['boards'] if b['version'] == shot['selectedBoard'])
            plan = next(p for p in shot['plans'] if p['version'] == shot['selectedPlan'])
            if not any(e['execution_method'] == 'seedance' for e in plan['elements']):
                raise ValueError('请先确认该镜头使用 Seedance 的动效计划')
            prompt = '\n'.join(e['prompt'] for e in plan['elements'] if e['execution_method'] == 'seedance' and e['prompt'])
            if not prompt:
                raise ValueError('动效计划缺少视频提示词')
            runs = self.boards.path(task)/'video-runs'
            for record in runs.glob('*/job.json') if runs.exists() else ():
                previous=read(record,{})
                if previous.get('shotId')==shot['id'] and previous.get('status') in ('queued','submitting','running','poll_timeout','poll_interrupted','submission_unknown'):
                    raise Conflict('该镜头已有进行中或状态未明的视频任务，请先核对原任务')
            duration = shot['end'] - shot['start']
            if duration != int(duration):
                raise ValueError('当前视频接口只接受整数秒镜头，请先调整计划')
            job_id = uuid.uuid4().hex
            folder = self.boards.path(task)/'video-runs'/job_id
            folder.mkdir(parents=True, exist_ok=False)
            source = Path(board['file'])
            shutil.copyfile(source, folder/('reference'+source.suffix))
            job = {'id':job_id, 'taskId':task_id, 'revision':task['revision'], 'inputHash':task['inputHash'],
                   'shotId':shot['id'], 'boardSha256':board['sha256'], 'planSha256':plan['sha256'],
                   'reference':str(folder/('reference'+source.suffix)), 'prompt':prompt,
                   'duration':int(duration), 'status':'queued', 'visualAcceptance':'pending', 'createdAt':time.time()}
            save(folder/'job.json',job)
        threading.Thread(target=worker or self._run, args=(folder,), daemon=True).start()
        return job

    def get(self, task_id, job_id):
        if len(job_id)!=32 or any(c not in '0123456789abcdef' for c in job_id):
            raise ValueError('视频任务编号无效')
        task = self.boards.store.get(task_id)
        job = read(self.boards.path(task)/'video-runs'/job_id/'job.json')
        if not job:
            raise ValueError('视频任务不存在或属于旧任务修订')
        return job

    def list(self, task_id):
        task=self.boards.store.get(task_id)
        folder=self.boards.path(task)/'video-runs'
        records=[read(path) for path in folder.glob('*/job.json')] if folder.exists() else []
        return sorted((r for r in records if r), key=lambda r:r['createdAt'], reverse=True)

    def _run(self, folder):
        job=read(folder/'job.json')
        def update(**values):
            job.update(values,updatedAt=time.time());save(folder/'job.json',job)
        try:
            spec=importlib.util.spec_from_file_location('suojian_seedance_poc', self.root/'outputs/seedance-poc01/seedance_poc.py')
            module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
            from PIL import Image
            with Image.open(job['reference']) as im:
                im.convert('RGB').save(folder/'input-normalized.png')
            body=module.build_request(folder/'input-normalized.png')
            body['content'][0]['text']=job['prompt']
            body['duration']=job['duration']
            redacted=json.loads(json.dumps(body));redacted['content'][1]['image_url']['url']='[image omitted]'
            save(folder/'request.redacted.json', {'endpoint':module.API,'body':redacted})
            token=module.credentials();transport=module.HTTP(token)
            update(status='submitting',model=module.MODEL)
            result,request_id=transport.request('POST','',body)
            provider_id=result.get('id')
            if not isinstance(provider_id,str) or not provider_id or not all(c.isalnum() or c in '_-' for c in provider_id):
                raise ValueError('接口未返回有效任务编号，请勿重复提交')
            update(status='running',providerTaskId=provider_id,requestId=request_id)
            for _ in range(100):
                response,_=transport.request('GET','/'+provider_id)
                status=response.get('status')
                if status=='succeeded':
                    url=response.get('content',{}).get('video_url')
                    if not url:raise ValueError('接口未返回视频地址')
                    info=transport.download(url,folder/'result.mp4')
                    update(status='downloaded',outputSha256=info['sha256'])
                    state=self.boards.get(job['taskId'])
                    current=next(s for s in state['shots'] if s['id']==job['shotId'])
                    board=next((b for b in current['boards'] if b['version']==current['selectedBoard']),None)
                    plan=next((p for p in current['plans'] if p['version']==current['selectedPlan']),None)
                    if not board or not plan or board['sha256']!=job['boardSha256'] or plan['sha256']!=job['planSha256']:
                        raise Conflict('生成期间分镜或计划已变，结果保留但未登记')
                    self.boards.mutate(job['taskId'],'register_clip',{'expectedRevision':job['revision'],
                        'inputHash':job['inputHash'],'expectedSerial':state['serial'],'shotId':job['shotId'],
                        'path':str(folder/'result.mp4'),'executionMethod':'seedance','externalTaskId':provider_id})
                    update(status='pending_review');return
                if status in ('failed','cancelled','expired'):
                    raise ValueError('服务返回 '+status+'：'+str(module.safe(response.get('error'),token)))
                if status not in ('queued','running'):raise ValueError('服务返回未知任务状态')
                time.sleep(6)
            update(status='poll_timeout',error='等待超时；保留原任务编号，未重复提交')
        except Exception as error:
            # The transport removes authorization and signed URLs from provider errors.
            reason=module.safe(str(error),locals().get('token','')) if 'module' in locals() else '本地视频适配器不可用'
            status=job.get('status')
            if status=='downloaded': outcome='downloaded_unregistered'
            elif status=='running': outcome='poll_interrupted'
            elif status=='submitting' and not reason.startswith('HTTP '): outcome='submission_unknown'
            else: outcome='failed'
            update(status=outcome,error=reason)
