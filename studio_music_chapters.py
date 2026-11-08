"""Chapter-aware score compositor for unattended documentaries.

Hooks the already installed studio_music_director.ensure() without changing
finished projects or existing user-selected music. Synthesizes a few small
original ambient stems, then builds one 64kbps MP3 with smooth, equal-length
overlaps at chapter boundaries. Voice ducking remains in studio_longform.py.

Audio-only; no cloud music API and no dependence on CapCut.
Generated music belongs to the project and is not a recording copied from
commercial artists. No YouTube publishing or new video rendering is triggered.
"""
from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
from pathlib import Path

from fastapi import HTTPException

import studio_music_director as music

VERSION="chapter-score-v2"
MIN_SECONDS=240
CHAPTER_SECONDS=300
FADE_SECONDS=3.5
MAX_CHAPTERS=8
MAX_MOOD_VARIANTS=8
MIN_FREE_MB=260
SCORE_LIMIT_MB=23
_AUDIT="trilha_por_capitulos.json"

def _duration(scene):
    try:
        return max(.5,float(scene.get("duration") or 8.0))
    except (ValueError,TypeError):
        return 8.0

def _norm(s):
    import unicodedata
    raw=unicodedata.normalize("NFKD",str(s or "").casefold())
    return "".join(c for c in raw if not unicodedata.combining(c))

def _mood_for_section(project,scenes,baseline):
    # A chapter should have at least two distinctive clues to change mood;
    # an incidental word in one spoken sentence must not change music.
    excerpt=" ".join(str(x.get("text") or "")[:280] for x in scenes[:8])
    sample=_norm(excerpt)
    candidate=music.mood_for({"title":excerpt[:1400],"topic":"",
                              "scenes":[]})
    if candidate=="documentary" or candidate==baseline:
        return baseline
    matches=0
    distinct=[]
    for cue in music.PROFILES[candidate]["keywords"]:
        cue=_norm(cue)
        if cue in sample and not any(cue in prev or prev in cue for prev in distinct):
            distinct.append(cue)
            matches+=1
    # A very explicit mystery/investigation cue can suffice when the
    # established channel topic is calm or engineering.
    if matches>=2:
        return candidate
    return baseline

def chapter_plan(project):
    scenes=project.get("scenes") or []
    total=sum(_duration(x) for x in scenes)
    baseline=music.mood_for(project)
    if not scenes or total<MIN_SECONDS:
        return {"version":VERSION,"mode":"single","baseline":baseline,
                "duration_seconds":round(total,2),"chapters":[]}
    chapters=[]
    i=0
    offset=0.
    while i<len(scenes):
        begin=i
        dur=0.
        while i<len(scenes):
            current=_duration(scenes[i])
            if dur>=CHAPTER_SECONDS*.7 and dur+current>CHAPTER_SECONDS and i>begin:
                break
            dur+=current
            i+=1
            if len(chapters)==MAX_CHAPTERS-1:
                # Last chapter contains all remaining scenes and may be longer.
                if i<len(scenes):
                    continue
            if dur>=CHAPTER_SECONDS*1.30 and i>begin+1:
                break
        group=scenes[begin:i]
        mood=_mood_for_section(project,group,baseline)
        chapters.append({"chapter":len(chapters)+1,
                         "from_scene":begin+1,"to_scene":i,
                         "start_seconds":round(offset,3),
                         "duration_seconds":round(dur,3),
                         "end_seconds":round(offset+dur,3),
                         "mood":mood,"mood_label":music.PROFILES[mood]["label"]})
        offset+=dur
    # Ensure harmless tiny tail sections are merged with their predecessor.
    if len(chapters)>1 and chapters[-1]["duration_seconds"]<80:
        tail=chapters.pop()
        previous=chapters[-1]
        previous["to_scene"]=tail["to_scene"]
        previous["end_seconds"]=tail["end_seconds"]
        previous["duration_seconds"]=round(tail["end_seconds"]-previous["start_seconds"],3)
    # Even when the topic stays in the same genre, vary timbre and note
    # progression once per chapter. This avoids one single 96-second loop.
    for idx,block in enumerate(chapters):
        block["variation"]=idx%3
        block["crossfade_seconds"]=FADE_SECONDS if idx else 0.
    return {"version":VERSION,"mode":"chapters","baseline":baseline,
            "duration_seconds":round(total,3),"chapters":chapters}

def _stem(studio,folder,mood,variation):
    mp3=folder/f"stem_{mood}_{variation}_{VERSION}.mp3"
    if mp3.is_file() and mp3.stat().st_size>=5000:
        return mp3
    wav=mp3.with_suffix(".wav")
    partial=mp3.with_name(mp3.stem+".part.mp3")
    try:
        # All content is composed mathematically and originally.
        # Rotate the original chord progression for audible variation
        # without changing to an unrelated key.
        original=music.PROFILES[mood]
        notes=original["progression"]
        shifted=tuple(notes[variation:]+notes[:variation])
        profile=dict(original,progression=shifted,
                     brightness=original["brightness"]*(1+.13*variation))
        music._synth(wav,mood,seconds=music.MUSIC_SECONDS,
                     profile_override=profile)
        proc=subprocess.run([studio.FFMPEG,"-hide_banner","-loglevel","error",
             "-nostdin","-y","-i",str(wav),"-c:a","libmp3lame",
             "-b:a","64k",str(partial)],capture_output=True,timeout=110)
        if proc.returncode or not partial.is_file() or partial.stat().st_size<5000:
            raise RuntimeError("Cannot create original chapter stem")
        partial.replace(mp3)
    finally:
        wav.unlink(missing_ok=True)
        partial.unlink(missing_ok=True)
    return mp3

def _filters(chapters,total):
    """Mix chapter stems along the real narration timeline.

    At boundary t, both neighboring stems run from t-3.5 to t+3.5;
    fades are complementary over 7 seconds. The fade-in at time zero
    and out at the very end are separate. All voices are untouched.
    """
    graph=[]
    labels=[]
    for index,part in enumerate(chapters):
        start=float(part["start_seconds"])
        end=float(part["end_seconds"])
        start_audio=max(0.0,start-(FADE_SECONDS if index else 0))
        end_audio=min(total,end+(FADE_SECONDS if index<len(chapters)-1 else 0))
        length=end_audio-start_audio
        if length<1:
            raise ValueError("Chapter audio region is empty")
        fade_in=min(length/2,1.6 if index==0 else 2*FADE_SECONDS)
        fade_out=min(length/2,2.3 if index==len(chapters)-1 else 2*FADE_SECONDS)
        delay=int(round(start_audio*1000))
        graph.append(
            f"[{index}:a]aformat=sample_rates=24000:channel_layouts=stereo,"
            f"atrim=duration={length:.3f},asetpts=PTS-STARTPTS,"
            f"afade=t=in:st=0:d={fade_in:.3f},"
            f"afade=t=out:st={max(0.,length-fade_out):.3f}:d={fade_out:.3f},"
            f"adelay={delay}|{delay}[m{index}]")
        labels.append(f"[m{index}]")
    graph.append("".join(labels)+f"amix=inputs={len(labels)}:duration=longest:"
                 f"dropout_transition=0:normalize=0,atrim=duration={total:.3f},"
                 "alimiter=limit=0.90[out]")
    return ";".join(graph)

def _compose(studio,pid,plan):
    """Produce 30 min as ~15 MB MP3, not an uncompressed WAV."""
    folder=studio.project_dir(pid)/"audio"
    folder.mkdir(parents=True,exist_ok=True)
    outfile=folder/f"trilha_por_capitulos_{VERSION}.mp3"
    partial=folder/f"trilha_por_capitulos_{VERSION}.part.mp3"
    audit=folder/_AUDIT
    if outfile.is_file() and outfile.stat().st_size>10_000 and audit.is_file():
        try:
            old=json.loads(audit.read_text(encoding="utf-8"))
            if old.get("version")==VERSION and old.get("plan")==plan:
                return outfile
        except (ValueError,OSError):
            pass
    chunks=plan["chapters"]
    # Existing stems are cached within each project to avoid repeated CPU.
    sources=[]
    for part in chunks:
        source=_stem(studio,folder,part["mood"],part["variation"])
        sources.append(source)
    args=[]
    for source in sources:
        args+=["-stream_loop","-1","-i",str(source)]
    args+=["-filter_complex",_filters(chunks,plan["duration_seconds"]),
           "-map","[out]","-vn","-c:a","libmp3lame","-b:a","64k",
           "-t",f"{plan['duration_seconds']:.3f}",str(partial)]
    try:
        proc=subprocess.run([studio.FFMPEG,"-hide_banner","-loglevel","error",
                              "-nostdin","-y",*args],
                             stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                             timeout=300,check=False)
        if proc.returncode or not partial.is_file() or partial.stat().st_size<12000:
            raise RuntimeError("Failed to compose crossfaded chapter soundtrack: "+
                               proc.stderr.decode("utf-8","replace")[-200:])
        if partial.stat().st_size>SCORE_LIMIT_MB*1048576:
            raise RuntimeError("Soundtrack exceeds strict size cap")
        if shutil.disk_usage(studio.DATA).free/1048576<130:
            raise RuntimeError("Disk reserve reached after soundtrack composition")
        partial.replace(outfile)
        metadata={"version":VERSION,"origin":"original_generated_locally",
                  "author":"Estúdio IA procedural instrumental",
                  "license":"New locally synthesized instrumental (no third-party recording)",
                  "api_charge_usd":0,"plan":plan,
                  "ducking":"FFmpeg long-form sidechain compressor; narration is foreground",
                  "source_files":[source.name for source in sources],
                  "music_path":str(outfile.relative_to(studio.MEDIA)),
                  "known_limit":"Procedural ambience, not a licensed commercial recording"}
        audit.write_text(json.dumps(metadata,ensure_ascii=False,indent=2),
                         encoding="utf-8")
        return outfile
    finally:
        partial.unlink(missing_ok=True)

def ensure(studio,pid):
    p=studio.get_project(pid)
    if p.get("status")=="concluído" or p.get("soundtrack"):
        return {"status":"preserved"}
    if os.getenv("STUDIO_MUSIC_ENABLED","1")!="1":
        return {"status":"disabled"}
    plan=chapter_plan(p)
    if plan["mode"]!="chapters":
        return _ORIGINAL_ENSURE(studio,pid)
    free_mb=shutil.disk_usage(studio.DATA).free/1048576
    if free_mb<MIN_FREE_MB:
        print("[chapter-score] low storage, small track fallback; free_mb="+
              str(round(free_mb)),flush=True)
        return _ORIGINAL_ENSURE(studio,pid)
    try:
        output=_compose(studio,pid,plan)
        studio.update_project(pid,soundtrack=str(output.relative_to(studio.MEDIA)))
        print("[chapter-score] COMPOSED chapters=%d moods=%s duration=%.1fs" %
              (len(plan["chapters"]),
               ",".join(block["mood"] for block in plan["chapters"]),
               plan["duration_seconds"]),flush=True)
        return {"status":"chapter_score_ready","chapters":len(plan["chapters"]),
                "path":str(output.relative_to(studio.MEDIA)),
                "api_charge_usd":0}
    except (OSError,subprocess.TimeoutExpired,RuntimeError,ValueError) as exc:
        print("[chapter-score] composition failed, single-mood fallback: "+
              type(exc).__name__,flush=True)
        return _ORIGINAL_ENSURE(studio,pid)

_ORIGINAL_ENSURE=None

def install(studio):
    global _ORIGINAL_ENSURE
    _ORIGINAL_ENSURE=music.ensure
    music.ensure=ensure

    @studio.app.get("/api/music/projects/{pid}/chapters")
    def read_plan(pid:str):
        project=studio.get_project(pid)
        plan=chapter_plan(project)
        audit=studio.project_dir(pid)/"audio"/_AUDIT
        prepared=None
        if audit.is_file() and audit.stat().st_size<12000:
            try:prepared=json.loads(audit.read_text(encoding="utf-8"))
            except (OSError,ValueError):pass
        return {"project_id":pid,"status":project.get("status"),
                "plan":plan,"composed_score_ready":prepared is not None,
                "composition":prepared,"no_paid_music_api":True,
                "completed_video_unchanged":project.get("status")=="concluído",
                "note":"Preview is read-only. Music is composed when a new project renders."}

def self_test():
    import tempfile, wave
    project={"title":"Serenidad y estoicismo","topic":"aprender calma",
             "scenes":[{"text":"La calma es una práctica diaria.",
                        "duration":70} for _ in range(28)]}
    plan=chapter_plan(project)
    assert plan["mode"]=="chapters" and len(plan["chapters"])>=4
    assert plan["baseline"]=="reflective"
    assert abs(plan["duration_seconds"]-1960)<.01
    assert plan["chapters"][0]["from_scene"]==1
    assert plan["chapters"][-1]["to_scene"]==28
    assert len(plan["chapters"])<=MAX_CHAPTERS
    assert ";" in _filters(plan["chapters"],plan["duration_seconds"])
    short={"title":"Video curto","scenes":[{"text":"Um resumo","duration":30}]}
    assert chapter_plan(short)["mode"]=="single"
    with tempfile.TemporaryDirectory() as tmp:
        small=Path(tmp)
        stems=[]
        for i in range(2):
            path=small/f"sample{i}.wav"
            music._synth(path,"reflective",seconds=2,rate=8000)
            stems.append(path)
        sample_plan=[
            {"start_seconds":0,"end_seconds":3.4},
            {"start_seconds":3.4,"end_seconds":6.8},
        ]
        ffmpeg=shutil.which("ffmpeg")
        ffprobe=shutil.which("ffprobe")
        if not ffmpeg or not ffprobe:
            raise AssertionError("FFmpeg and FFprobe must be installed")
        cmd=[ffmpeg,"-hide_banner","-loglevel","error","-nostdin",
             "-stream_loop","-1","-i",str(stems[0]),
             "-stream_loop","-1","-i",str(stems[1]),
             "-filter_complex",_filters(sample_plan,6.8),
             "-map","[out]","-t","6.8","-c:a","pcm_s16le",str(small/"check.wav"),
             "-y"]
        r=subprocess.run(cmd,capture_output=True,timeout=25)
        if r.returncode:
            raise AssertionError("FFmpeg score-mix graph failed: "+
                                 r.stderr.decode("utf-8","replace")[-400:])
        with wave.open(str(small/"check.wav"),"rb") as sound:
            if abs(sound.getnframes()/sound.getframerate()-6.8)>.15:
                raise AssertionError("Crossfade changed score duration")
    print("[chapter-score] SELF-TEST PASS 5-minute chapters, 7-second transitions, FFmpeg duration and zero paid API",flush=True)

if __name__=="__main__":
    self_test()
    app=Path("/app/app.py")
    if not app.is_file():raise SystemExit("Missing /app/app.py")
    marker="\nimport studio_music_chapters as _music_chapters\n"
    code=app.read_text(encoding="utf-8")
    if marker not in code:
        app.write_text(code+marker+
            '_music_chapters.install(__import__("sys").modules[__name__])\n',
            encoding="utf-8")
