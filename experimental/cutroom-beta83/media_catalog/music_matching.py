"""Local, explainable BGM/window matching. No recognition or exact song-ID claims."""
import hashlib
import json
from pathlib import Path
import subprocess
import numpy as np

RATE = 4000
HOP = 128
FRAME = 512
VERSION = "music-match-1"


def audio_samples(path):
    result = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-vn", "-t", "600",
                             "-ar", str(RATE), "-ac", "1", "-f", "f32le", "pipe:1"], capture_output=True, timeout=90)
    if result.returncode:
        raise ValueError("MUSIC_AUDIO_UNREADABLE: 音频无法分析")
    samples = np.frombuffer(result.stdout, dtype="<f4").copy()
    if len(samples) < RATE or np.max(np.abs(samples)) < 1e-5:
        raise ValueError("MUSIC_AUDIO_EMPTY: 没有足够的有效声音")
    return samples


def features(samples):
    frames = np.lib.stride_tricks.sliding_window_view(samples,FRAME)[::HOP]
    spectrum = np.abs(np.fft.rfft(frames*np.hanning(FRAME),axis=1))
    power = spectrum.sum(axis=1)+1e-9
    frequency = np.fft.rfftfreq(FRAME,1/RATE)
    brightness = float(np.mean((spectrum*frequency).sum(axis=1)/power)/(RATE/2))
    bass = float(np.mean(spectrum[:,frequency<250].sum(axis=1)/power))
    rms = np.sqrt(np.mean(frames**2,axis=1))
    # Spectral changes are a rhythm proxy; speech can also generate onsets.
    flux = np.maximum(np.diff(spectrum,axis=0),0).sum(axis=1)
    flux = np.maximum(flux-np.median(flux),0)
    centered = flux-np.mean(flux)
    lo,hi = int(60*RATE/HOP/170),int(60*RATE/HOP/65)
    energy = float(np.dot(centered,centered))
    correlations = [float(np.dot(centered[:-lag],centered[lag:])/max(energy,1e-9)) for lag in range(lo,hi+1)]
    lag = lo+int(np.argmax(correlations))
    confidence = max(0.0,max(correlations))
    return {"brightness":brightness,"bass":bass,
            "pulse_density":float(np.mean(flux>np.mean(flux)+np.std(flux))),
            "dynamics":float(np.std(rms)/max(float(np.mean(rms)),1e-6)),
            "rms":float(np.mean(rms)),"bpm":round(60*RATE/HOP/lag,1) if confidence>=.2 else None,
            "tempo_confidence":round(confidence,3)}


def distance(reference, candidate):
    value = 2*abs(reference["brightness"]-candidate["brightness"])+abs(reference["bass"]-candidate["bass"])
    value += 2*abs(reference["pulse_density"]-candidate["pulse_density"])
    value += .25*abs(reference["dynamics"]-candidate["dynamics"])
    if reference["bpm"] and candidate["bpm"]:
        ratio = candidate["bpm"]/reference["bpm"]
        value += min(abs(np.log2(ratio)),abs(np.log2(ratio*2)),abs(np.log2(ratio/2)))*.3
    return float(value)


def choose_window(samples, duration, reference):
    total = len(samples)/RATE
    span = min(duration,total)
    starts = list(np.arange(0,max(0,total-span)+.01,2.0)) or [0.0]
    candidates=[]
    for start in starts:
        window = samples[int(start*RATE):int((start+span)*RATE)]
        if len(window)<RATE: continue
        profile=features(window)
        # Avoid nearly silent intros and wildly changing sections under narration.
        penalty=.5 if profile["rms"]<.008 else 0
        candidates.append((distance(reference,profile)+penalty,float(start),profile))
    score,start,profile=min(candidates,key=lambda x:(x[0],x[1]))
    return {"score":round(score,4),"start":round(start,3),"duration":round(span,3),
            "loop_required":total<duration,"features":profile}


def recommend_bgm(root, reference_source, duration, route="信息流广告", script=""):
    from .packaging_plan import load_all_manifests, _eligible_items, resolve_manifest_asset_path
    reference_path=Path(reference_source)
    with reference_path.open("rb") as handle:
        reference_hash=hashlib.file_digest(handle,"sha256").hexdigest()
    cache=Path(root)/"runtime/music-matching"
    cache.mkdir(parents=True,exist_ok=True)
    reference=features(audio_samples(reference_path))
    ranking=[]
    for item in _eligible_items(load_all_manifests(),"bgm",route):
        path=resolve_manifest_asset_path("bgm",item["id"])
        with path.open("rb") as handle: signature=hashlib.file_digest(handle,"sha256").hexdigest()
        key=hashlib.sha256(f"{VERSION}:{signature}:{reference_hash}:{duration}".encode()).hexdigest()
        cached=cache/f"{key}.json"
        if cached.is_file(): window=json.loads(cached.read_text("utf-8"))
        else:
            window=choose_window(audio_samples(path),duration,reference)
            cached.write_text(json.dumps(window),"utf-8")
        # Descriptive tags are a small tie-breaker, never the whole ranking.
        terms=set(item.get("mood","").replace("、","，").split("，"))
        tag_matches=[term for term in terms if term and term in script]
        ranking.append({**window,"score":round(window["score"]-.025*len(tag_matches),4),
                        "id":item["id"],"name":item["name"],"source":str(path),"sha256":signature,
                        "gain":item.get("default_gain",.12),"mood":item.get("mood"),"tag_matches":tag_matches})
    if not ranking: raise ValueError("MUSIC_NO_ELIGIBLE_TRACK: 没有可用的授权音乐")
    ranking.sort(key=lambda x:(x["score"],x["id"]))
    selected=ranking[0]
    return {"version":VERSION,"selected":selected,"ranking":ranking,"reference_features":reference,
            "reference_sha256":reference_hash,"fade_in":.35,"fade_out":1.5,
            "confidence":"approximate","reason":"按参考片音色明暗、低频比例、节奏变化和动态匹配，并避开过静的开头；参考音频含人声，节奏为粗估。"}
