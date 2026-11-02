"""Hypit owns final Film composition; Remotion supplies the authored animated picture."""
import json
import math
import shutil
import subprocess
import time
from pathlib import Path


def compose(config, run, visual, voice, music, output, phase, reuse_build=None, music_gain=1):
    if type(music_gain) not in {int, float} or not math.isfinite(music_gain) or not 0 <= music_gain <= 2:
        raise ValueError("Invalid music gain")
    cli = Path(config["hypit_cli"])
    project = Path(config["hypit_project"])
    profile = project / "hypit.runtime.json"
    work = project / "runs" / run
    work.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(visual, work / "visual.mp4")
    shutil.copyfile(voice, work / "voice.mp3")
    if music:
        shutil.copyfile(music, work / "music.m4a")
    (work / "look.svs").write_text('<?svml using="@hypit/recipe@1"?>\n<sheet version="1">film.main { background: #0c0d12; }</sheet>', encoding="utf-8")
    music_source = ('<asset:Audio id="music-file" src="./music.m4a"/>\n'
        '<mediaop:Normalize id="music" source={music-file} video="none" audio="default" span-authority="audio" clock={clock}/>') if music else ''
    music_clip = f'<audio:Clip id="bgm" source={{music.media}} during={{music-window}} gain="{music_gain}"/>' if music else ''
    source = '''<?svml using="@hypit/markup@1"?>
<svml>
<import as="asset" from="@hypit/media@1"/>
<import as="mediaop" from="@hypit/media-operations@1"/>
<import as="time" from="@hypit/timeline-author@1"/>
<import as="space" from="@hypit/spatial@1"/>
<import as="visual" from="@hypit/visual-track@1"/>
<import as="audio" from="@hypit/audio-track@1"/>
<import as="film" from="@hypit/film@1"/>
<import as="html" from="@hypit/html-video@1"/>
<import as="look" source="./look.svs"/>
<asset:Video id="picture-file" src="./visual.mp4"/>
<asset:Audio id="voice-file" src="./voice.mp3"/>
<time:Clock id="clock" frame-rate="30"/>
<mediaop:Normalize id="picture" source={picture-file} video="primary-moving" audio="none" span-authority="video" clock={clock}/>
<mediaop:Normalize id="voice" source={voice-file} video="none" audio="default" span-authority="audio" clock={clock}/>
MUSIC_SOURCE
<time:Timeline id="program" clock={clock} end="1067f"/>
<time:Window id="voice-window" timeline={program.timeline} from="start" until="end"/>
<time:Window id="music-window" timeline={program.timeline} from="start" until="end"/>
<space:Canvas id="canvas" width="1280" height="720"/>
<space:Frame id="full" within={canvas.bounds} left="0%" top="0%" right="100%" bottom="100%"/>
<visual:Track id="picture-track" timeline={program.timeline}>
<visual:Clip media={picture.media} during={program.window} frame={full} z="0" fit="contain"/>
</visual:Track>
<audio:Track id="sound" timeline={program.timeline}>
<audio:Clip id="voice-track" source={voice.media} during={voice-window} gain="VOICE_GAIN"/>
MUSIC_CLIP
</audio:Track>
<film:Film id="main" canvas={canvas.canvas} timeline={program.timeline} appearance={look.film.main}>
<film:Track source={picture-track.visual}/><film:Track source={sound.audio}/>
</film:Film>
<html:Video id="final" composition={main.composition} timeline={program.timeline}/>
</svml>
'''.replace('MUSIC_SOURCE', music_source).replace('MUSIC_CLIP', music_clip).replace('VOICE_GAIN', '0.9' if music else '1')
    (work / "main.svml").write_text(source, encoding="utf-8")
    reuse = ''
    if reuse_build:
        for name in ('picture', 'voice', 'music') if music else ('picture', 'voice'):
            reuse += f'<build-record id="kept-{name}" build="{reuse_build}" output="{name}.media"/>\n'
            reuse += f'<satisfy output="{name}.media" candidate="kept-{name}"/>\n'
    (work / "main.svrun").write_text('<?svml using="@hypit/markup/run@1"?>\n<svrun version="1"><author source="./main.svml"/><target output="final.video"/>\n' + reuse + '</svrun>', encoding="utf-8")
    prefix = ["node", str(cli)]

    def command(args, name, runtime=False):
        cmd = prefix + args + ["--project", str(project), "--json"]
        if runtime:
            cmd += ["--runtime", str(profile)]
        process = subprocess.run(cmd, capture_output=True, timeout=120, cwd=project)
        (work / (name + ".json")).write_bytes(process.stdout)
        (work / (name + ".log")).write_bytes(process.stderr)
        try:
            payload = json.loads(process.stdout)
        except (ValueError, UnicodeDecodeError):
            raise RuntimeError("Hypit " + name + " 没有返回可读结果；记录已保存") from None
        if process.returncode and (name != "status" or not payload.get("build")):
            raise RuntimeError("Hypit " + name + " 未完成；详细记录已保存在本机工程")
        return payload

    phase("Hypit 正在检查本机工程")
    command(["runtime", "up"], "runtime", True)
    command(["check", str(work / "main.svrun")], "check")
    command(["plan", str(work / "main.svrun")], "plan", True)
    receipt = command(["build", str(work / "main.svrun")], "receipt", True)
    build_id = receipt["build"]["id"]
    phase("Hypit 正在合成画面与独立音轨", hypitBuildId=build_id)
    deadline = time.monotonic() + 1000
    while True:
        status = command(["status", build_id], "status", True)
        build = status.get("build", status)
        work_status = build.get("work", {})
        outcome = work_status.get("outcome") or build.get("result", {}).get("state")
        phase("Hypit 正在合成画面与独立音轨", hypitBuildId=build_id)
        if outcome in {"complete", "completed"}:
            break
        if outcome in {"failed", "cancelled", "error"}:
            raise RuntimeError("Hypit 合成未成功，记录已保留：" + str(build.get("failure", outcome)))
        if time.monotonic() > deadline:
            command(["cancel", build_id], "cancel", True)
            raise RuntimeError("Hypit 合成超时，本次任务已取消")
        time.sleep(3)
    command(["get", build_id, "--output", "final.video", "--to", str(output)], "export")
    return {"buildId": build_id, "engine": "Hypit 0.3.1 + Remotion picture", "projectRun": str(work)}
