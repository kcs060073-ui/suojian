"""FFT/HPSS onset candidates and fitted grids using existing numpy/scipy.
Drum names denote frequency bands, not semantic instrument recognition.
"""
import json
import subprocess
import sys
import numpy as np
from scipy.ndimage import median_filter
from scipy.signal import stft,find_peaks,fftconvolve

source,target=sys.argv[1:3]
ffmpeg=sys.argv[3] if len(sys.argv)>3 else 'ffmpeg'
sr=22050; hop=256
process=subprocess.run([ffmpeg,'-v','error','-i',source,'-vn','-t','600','-ac','1','-ar',str(sr),'-f','s16le','pipe:1'],capture_output=True,timeout=90)
if process.returncode: raise RuntimeError(process.stderr.decode(errors='replace'))
y=np.frombuffer(process.stdout,dtype='<i2').astype(float)/32768
if len(y)<2048: raise ValueError('音频太短，无法估计节拍')
frequency,times,spectrum=stft(y,fs=sr,nperseg=1024,noverlap=1024-hop,boundary=None)
mag=np.abs(spectrum)
harmonic=median_filter(mag,size=(1,17)); percussive=median_filter(mag,size=(17,1))
mask=percussive**2/(percussive**2+harmonic**2+1e-12)
transient=mag*mask
def envelope(lo=None,hi=None):
    subset=transient if lo is None else transient[(frequency>=lo)&(frequency<=hi)]
    return np.maximum(0,np.diff(subset,axis=1,prepend=subset[:,:1])).sum(axis=0)
def peaks(env):
    indices,_=find_peaks(env,distance=max(1,round(.1*sr/hop)),prominence=max(float(np.std(env)*.45),1e-8))
    return indices
env=envelope(); indices=peaks(env); onset=times[indices]; hits=[]
for kind,lo,hi in [('kick',40,160),('snare-low',150,500),('snare-high',1000,3000),('hihat',6000,10000)]:
    band=envelope(lo,hi)
    for index in peaks(band): hits.append({'t':float(times[index]),'s':float(band[index]),'k':kind,'classification':'frequency-band-candidate'})
snare_low=[h for h in hits if h['k']=='snare-low']; snare_high=[h for h in hits if h['k']=='snare-high']
for low in snare_low:
    match=next((h for h in snare_high if abs(h['t']-low['t'])<.033),None)
    if match: hits.append({'t':(low['t']+match['t'])/2,'s':min(low['s'],match['s']),'k':'snare','classification':'two-band-coincidence-candidate'})
candidates=[]
correlation=fftconvolve(env,env[::-1],mode='full')[len(env)-1:]
minimum=max(1,round(60/180*sr/hop)); maximum=min(len(correlation)-1,round(60/60*sr/hop))
if len(onset)>=3 and maximum>minimum:
    lag=int(np.argmax(correlation[minimum:maximum+1])+minimum)
    base_period=lag*hop/sr
    strongest=sorted(indices,key=lambda i:env[i],reverse=True)[:30]
    for factor in (.5,1,2):
        period=base_period/factor
        def score_phase(phase):
            grid=np.arange(phase,len(y)/sr,period)
            error=np.array([np.min(abs(t-onset)) for t in grid])
            return float(np.mean(error<.033)),-float(np.mean(error))
        phase=max((float(times[i]%period) for i in strongest),key=score_phase)
        grid=np.arange(phase,len(y)/sr,period)
        nearest=np.array([onset[np.argmin(abs(t-onset))] for t in grid])
        valid=np.abs(grid-nearest)<min(.08,period*.15)
        if np.sum(valid)>=3:
            fitted_period,fitted_phase=np.linalg.lstsq(np.stack([np.arange(len(grid))[valid],np.ones(np.sum(valid))],axis=1),nearest[valid],rcond=None)[0]
            if fitted_period>0: period=float(fitted_period); phase=float(fitted_phase%period)
        grid=np.arange(phase,len(y)/sr,period)
        signed=np.array([onset[np.argmin(abs(t-onset))]-t for t in grid]); error=np.abs(signed)
        drift=float(np.polyfit(grid,signed,1)[0]*(len(y)/sr)*1000) if len(error)>2 else None
        candidates.append({'bpm':60/period,'T':period,'t0':phase,'match':float(np.mean(error<=.033)),'mean_abs_ms':float(np.mean(error)*1000),'max_abs_ms':float(np.max(error)*1000),'drift_ms':drift})
    winner=max(candidates,key=lambda c:(c['match'],-c['mean_abs_ms']))
    grid=[float(t) for t in np.arange(winner['t0'],len(y)/sr,winner['T'])]
else: winner={'bpm':None,'T':None,'t0':None,'match':0}; grid=[]
rms=[]
for start in range(0,len(y),hop*4):
    chunk=y[start:start+hop*4]; rms.append({'t':start/sr,'v':float(np.sqrt(np.mean(chunk**2)))})
sections=[]
for start in np.arange(0,len(y)/sr,4):
    values=[x['v'] for x in rms if start<=x['t']<start+4]
    if values: sections.append({'from':float(start),'to':min(float(start+4),len(y)/sr),'rms':float(np.mean(values)),'onsetCount':int(np.sum((onset>=start)&(onset<start+4)))})
report={**winner,'beats':grid,'hits':sorted(hits,key=lambda h:h['t']),'rms':rms,'sections':sections,'candidates':candidates,'method':'numpy-scipy/FFT-HPSS/least-squares-grid/frequency-band-onsets','verified':False,
        'thresholds':{'match':.98,'mean_abs_ms':10,'drift_ms':5},'automaticGatePassed':bool(winner.get('match',0)>=.98 and (winner.get('mean_abs_ms') or 999)<10 and abs(winner.get('drift_ms') or 999)<5),
        'notes':['频段鼓点是候选，不是乐器识别；变速曲需分段拟合','半倍/双倍候选均保留供用户选择；不自动强制改时间线']}
with open(target,'w',encoding='utf-8') as f: json.dump(report,f,ensure_ascii=False,indent=2)
