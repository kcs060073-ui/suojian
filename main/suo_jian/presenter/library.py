import hashlib
import json
import subprocess
from pathlib import Path

import cv2
import numpy as np

EXTENSIONS={'.mp4','.mov','.mkv','.webm','.m4v'}
MODEL_ROOT=Path(__file__).resolve().parents[2]/'model_cache/presenter'

def save(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8');temporary.replace(path)

def fingerprint(path):
    path=Path(path);stat=path.stat()
    return hashlib.sha256(f'{path.resolve()}:{stat.st_size}:{stat.st_mtime_ns}'.encode()).hexdigest()[:20]

def probe(path):
    raw=json.loads(subprocess.check_output(['ffprobe','-v','error','-protocol_whitelist','file,pipe','-show_entries',
        'format=duration:stream=codec_type,width,height,color_transfer:stream_side_data=rotation','-of','json',str(path)],timeout=30))
    video=next((s for s in raw.get('streams',[]) if s.get('codec_type')=='video'),None)
    duration=float(raw.get('format',{}).get('duration',0))
    if not video or not .3<duration<1800:raise ValueError('没有可解码的视频或时长不适合')
    return {'duration':duration,'width':video['width'],'height':video['height'],
        'has_audio':any(s.get('codec_type')=='audio' for s in raw['streams']),
        'transfer':video.get('color_transfer','unknown')}

def frame(path,time,metadata):
    filters=[]
    if metadata.get('transfer') in {'smpte2084','arib-std-b67'}:
        filters.append('zscale=t=linear:npl=100,format=gbrpf32le,tonemap=hable,zscale=p=bt709:t=bt709:m=bt709,format=yuv420p')
    filters.append('scale=640:-2')
    data=subprocess.check_output(['ffmpeg','-v','error','-threads','1','-ss',str(time),'-i',str(path),'-vf',','.join(filters),
        '-frames:v','1','-f','image2pipe','-vcodec','mjpeg','pipe:1'],timeout=45)
    image=cv2.imdecode(np.frombuffer(data,np.uint8),cv2.IMREAD_COLOR)
    if image is None:raise ValueError('无法读取视频画面')
    return image

class Faces:
    def __init__(self):
        cv2.setNumThreads(1)
        self.detector=cv2.FaceDetectorYN.create(str(MODEL_ROOT/'face_detection_yunet_2023mar.onnx'),'',(640,640),.85,.3,5000)
        self.recognizer=cv2.FaceRecognizerSF.create(str(MODEL_ROOT/'face_recognition_sface_2021dec.onnx'),'')
    def observe(self,image):
        h,w=image.shape[:2];self.detector.setInputSize((w,h));_,faces=self.detector.detect(image)
        if faces is None:return None
        faces=sorted(faces,key=lambda f:f[2]*f[3],reverse=True)
        main=faces[0]
        if min(main[2:4])<32:return None
        if len(faces)>1 and faces[1][2]*faces[1][3]>main[2]*main[3]*.4:return {'ambiguous':True}
        vector=self.recognizer.feature(self.recognizer.alignCrop(image,main)).flatten()
        vector=vector/(np.linalg.norm(vector)+1e-8)
        return {'feature':vector.tolist(),'center':[float((main[0]+main[2]/2)/w),float((main[1]+main[3]/2)/h)],
                'bbox':[float(v) for v in main[:4]],'ambiguous':False}

def cluster(records):
    groups=[]
    for item in records:
        observations=[o for o in item.get('faces',[]) if o and not o.get('ambiguous')]
        if len(observations)<2 or item.get('ambiguous'):
            item['person_group']=None;continue
        vectors=np.array([o['feature'] for o in observations]);average=vectors.mean(axis=0);average/=np.linalg.norm(average)
        if float(np.min(vectors@average))<.55:
            item['person_group']=None;item['identity_note']='同一素材中的人物外观不稳定，需核对';continue
        scores=[float(np.dot(average,g['anchor'])) for g in groups]
        best=int(np.argmax(scores)) if scores else -1
        if best<0 or scores[best]<.55:
            best=len(groups);groups.append({'id':f'person-{best+1}','anchor':average,'items':[]})
        group=groups[best];group['items'].append(item['id']);item['person_group']=group['id']
        item['person_score']=round(float(np.dot(average,group['anchor'])),4)
    return [{'id':g['id'],'asset_ids':g['items'],'count':len(g['items']),'anchor':g['anchor'].tolist()} for g in groups]

def scan(folder,work,phase=lambda *a,**k:None):
    root=Path(folder).expanduser().resolve(strict=True)
    if not root.is_dir() or root.parent==root:raise ValueError('请选择具体的本机素材文件夹')
    files=sorted(p for p in root.rglob('*') if p.is_file() and not p.is_symlink() and p.suffix.lower() in EXTENSIONS and p.resolve().is_relative_to(root))
    if not files or len(files)>500:raise ValueError('文件夹中应包含 1–500 个视频')
    work=Path(work);cache=work/'library-cache';cache.mkdir(parents=True,exist_ok=True)
    detector=Faces();records=[];errors=[]
    for i,path in enumerate(files):
        phase('正在按出镜人物整理素材',completed=i,total=len(files))
        key=fingerprint(path);stored=cache/(key+'.json')
        if stored.exists():records.append(json.loads(stored.read_text(encoding='utf-8')));continue
        try:
            meta=probe(path)
            observations=[]
            for index,t in enumerate([min(2,meta['duration']*.2),meta['duration']*.5,meta['duration']*.8]):
                image=frame(path,t,meta);obs=detector.observe(image)
                if obs:obs['time']=round(t,3)
                observations.append(obs)
                if index==1:cv2.imencode('.jpg',image,[cv2.IMWRITE_JPEG_QUALITY,85])[1].tofile(str(work/(key+'.jpg')))
            item={'id':key,'path':str(path),'name':path.name,**meta,'faces':observations,
                'ambiguous':any(o and o.get('ambiguous') for o in observations)}
            save(stored,item);records.append(item)
        except Exception as error:errors.append({'name':path.name,'error':str(error)[:300]})
    groups=cluster(records)
    value={'folder':str(root),'assets':records,'groups':groups,'errors':errors,'file_count':len(files),
        'identity_method':'OpenCV YuNet + SFace; conservative cosine threshold 0.55; uncertain footage excluded'}
    save(work/'library.json',value);return value
