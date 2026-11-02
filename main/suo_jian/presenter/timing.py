import json
import re
import subprocess
from pathlib import Path
from .library import fingerprint,save

class TimedASR:
    def __init__(self):
        from funasr import AutoModel
        import torch
        torch.set_num_threads(3)
        hub=Path(__file__).resolve().parents[2]/'model_cache/hub/models'
        self.model=AutoModel(model=str(hub/'iic--SenseVoiceSmall/snapshots/master'),
            vad_model=str(hub/'iic--speech_fsmn_vad_zh-cn-16k-common-pytorch/snapshots/master'),
            device='cpu',ncpu=3,disable_update=True,disable_log=True)
    def transcribe(self,path,work):
        key=fingerprint(path);out=Path(work)/(key+'-timed-v1.json')
        if out.exists():return json.loads(out.read_text(encoding='utf-8'))
        wav=Path(work)/(key+'.wav');wav.parent.mkdir(parents=True,exist_ok=True)
        try:
            subprocess.run(['ffmpeg','-v','error','-y','-threads','1','-i',str(path),'-vn','-ac','1','-ar','16000',str(wav)],check=True,capture_output=True,timeout=180)
            raw=self.model.generate(input=str(wav),batch_size_s=120,output_timestamp=True,language='zh',use_itn=True,
                disable_pbar=True)[0]
            words=raw.get('words',[]);times=raw.get('timestamp',[])
            if not words or len(words)!=len(times):raise ValueError('语音模型没有返回可用的逐词时间，不能按字数猜测切点')
            measured=[{'text':str(w),'start':round(float(t[0])/1000,3),'end':round(float(t[1])/1000,3)}
                      for w,t in zip(words,times) if str(w).strip() and float(t[1])>float(t[0])]
            if not measured or max(w['end'] for w in measured)<=0:raise ValueError('语音时间轴为空')
            result={'text':re.sub(r'<\|[^|]*\|>','',raw['text']).strip(),'words':measured,
                'method':'SenseVoice CTC alignment with VAD offsets; model-estimated word boundaries'}
            save(out,result);return result
        finally:wav.unlink(missing_ok=True)
