"""Original, mood-aware background-score selector for unattended documentary production.

No paid provider, copyrighted recordings, downloads or subscription required.
Generates a soft 96s instrumental bed and uses the existing FFmpeg render
pipeline's loop logic. For long projects, studio_longform.py provides voice-
aware sidechain compression to keep music behind narration.

Does not modify a completed project or overwrite a user-selected soundtrack.
Uses a short temporary WAV (<4MB) and checks the Railway disk reserve.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import struct
import subprocess
import wave
from pathlib import Path
from fastapi import HTTPException

VERSION="original-score-v1"
MUSIC_SECONDS=96
SAMPLE_RATE=16000
MIN_FREE_MB=100
PROFILES={
    "reflective":{
        "label":"Reflexiva · ambient suave",
        "keywords":("estoic","seren","calma","paz interior","filosof","psycholog",
                    "psicolog","medit","reflex","mente","mindful","mindset",
                    "limite","boundaries","respect"),
        "progression":(48,44,46,43), "minor":True,"pulse":0.015,"brightness":0.19,
    },
    "mystery":{
        "label":"Mistério · suspense discreto",
        "keywords":("mister","mystery","investig","crime","segredo","secret",
                    "conspir","enigma","suspense","caso insolito","inexplic"),
        "progression":(45,42,41,44),"minor":True,"pulse":0.105,"brightness":0.25,
    },
    "inspiring":{
        "label":"Inspiradora · superação",
        "keywords":("superac","superaç","motivac","motivação","inspir","sucesso",
                    "success","overcome","victory","vitoria","vitória",
                    "conquista","transformat","esperanca","esperança"),
        "progression":(48,43,45,41),"minor":False,"pulse":0.065,"brightness":0.40,
    },
    "historical":{
        "label":"Histórica · documental",
        "keywords":("historia","história","history","historical","guerra","war",
                    "imperio","império","ancient","antigo","arqueolog","civiliz",
                    "biografia","documentary","documentário","medieval"),
        "progression":(50,46,43,45),"minor":True,"pulse":0.045,"brightness":0.29,
    },
    "science":{
        "label":"Tecnologia · eletrônica ambiental",
        "keywords":("technology","tecnolog","cosmos","space","espaco","espaço",
                    "astronom","univers","inteligencia artificial","inteligência artificial",
                    "robot","comput","digital","algorit","planet","galax"),
        "progression":(50,45,43,48),"minor":False,"pulse":0.075,"brightness":0.44,
    },
    "nature":{
        "label":"Natureza · ambiental",
        "keywords":("nature","natureza","naturaleza","floresta","forest","wildlife",
                    "oceano","ocean","animais","animals","biodivers","montanh",
                    "paisagem","ecologia"),
        "progression":(48,45,43,46),"minor":False,"pulse":0.018,"brightness":0.20,
    },
    "documentary":{
        "label":"Documentário · instrumental discreta",
        "keywords":(),
        "progression":(48,43,45,46),"minor":True,"pulse":0.035,"brightness":0.24,
    },
}

def mood_for(project):
    import unicodedata
    def norm(s):
        x=unicodedata.normalize("NFKD",str(s or "").casefold())
        return "".join(ch for ch in x if not unicodedata.combining(ch))
    lead=norm((project.get("title") or "")+" "+(project.get("topic") or ""))
    context=norm(" ".join(str(s.get("text") or "")[:130] for s in
                          (project.get("scenes") or [])[:12]))
    scored=[]
    for mood,settings in PROFILES.items():
        if mood=="documentary":continue
        # Synonym substrings such as 'ocean'/'oceano' must not count
        # twice for the same subject and overpower the narrative intention.
        marks=sorted(set(norm(m) for m in settings["keywords"]),
                     key=len,reverse=True)
        selected=[]
        for mark in marks:
            if mark in lead and not any(mark in x for x in selected):
                selected.append(mark)
        lead_score=5*len(selected)
        if mood=="mystery" and any(mark in lead for mark in
                                   ("mister","crime","investig","enigma","secret")):
            lead_score+=3
        scored.append((lead_score+sum(1 for m in marks if m in context),mood))
    scored.sort(reverse=True)
    return scored[0][1] if scored and scored[0][0] else "documentary"

def _notes(root,minor):
    # Chord tones are elementary notes, not a reproduction of any song.
    intervals=(0,3,7,10) if minor else (0,4,7,11)
    return tuple(440.0*2**((root+n-69)/12) for n in intervals)

def _synth(path,mood,seconds=MUSIC_SECONDS,rate=SAMPLE_RATE,profile_override=None):
    """Generate gentle original pads with slow changes and quiet arpeggios."""
    profile=profile_override if profile_override is not None else PROFILES[mood]
    chords=[_notes(note,profile["minor"]) for note in profile["progression"]]
    phase_len=seconds/len(chords)
    brightness=float(profile["brightness"])
    pulse=float(profile["pulse"])
    # Stable per-mood instruments; slow movement reduces robotic repetition.
    frames=1024
    with wave.open(str(path),"wb") as wav:
        wav.setnchannels(1);wav.setsampwidth(2);wav.setframerate(rate)
        total=round(seconds*rate)
        for start in range(0,total,frames):
            end=min(total,start+frames)
            packet=bytearray()
            for i in range(start,end):
                t=i/rate
                segment=min(len(chords)-1,int(t/phase_len))
                local=t-segment*phase_len
                fade=min(1.0,max(0.0,(phase_len-local)/2.8))
                enter=min(1.0,max(0.0,local/2.8))
                # Blend adjacent chord pads over a soft 2.8 second transition.
                next_index=(segment+1)%len(chords)
                change=max(0.0,(local-(phase_len-2.8))/2.8)
                change=change*change*(3-2*change)
                def harmonic(chord):
                    out=0.0
                    for j,f in enumerate(chord[:3]):
                        # Gentle detuning and very mild harmonic content.
                        wobble=.0012*math.sin(2*math.pi*(.08+.02*j)*t)
                        a=2*math.pi*(f*(1+wobble))*t
                        out+=(math.sin(a)+(brightness*.45)*math.sin(2*a))/(3.0)
                    return out
                smooth=(1-change)*harmonic(chords[segment])+change*harmonic(chords[next_index])
                # A delicate, brief note pattern; not a loop copied from a song.
                beat=t%(2.9 if mood in ("reflective","nature") else 1.9)
                arp_note=chords[segment][int(t//3)%4]
                arp=math.sin(2*math.pi*arp_note*t)*math.exp(-4.5*beat)
                breathe=.80+.10*math.sin(2*math.pi*t/17)
                level=.33*(smooth*breathe+pulse*arp)
                # Softer intro/outro makes looping less jarring.
                envelope=min(1.0,t/1.7,(seconds-t)/1.7)
                level*=max(0.0,envelope)
                packet+=struct.pack("<h",round(max(-.88,min(.88,level))*32767))
            wav.writeframes(packet)

def ensure(studio,pid):
    project=studio.get_project(pid)
    if project.get("status")=="concluído":
        return {"status":"preserved_completed"}
    if project.get("soundtrack"):
        return {"status":"existing_soundtrack_preserved",
                "soundtrack":project["soundtrack"]}
    if os.getenv("STUDIO_MUSIC_ENABLED","1")!="1":
        return {"status":"disabled"}
    if shutil.disk_usage(studio.DATA).free/1048576<MIN_FREE_MB:
        return {"status":"low_storage","required_free_mb":MIN_FREE_MB}
    mood=mood_for(project)
    dest=studio.project_dir(pid)/"audio"
    dest.mkdir(parents=True,exist_ok=True)
    final=dest/f"trilha_original_{mood}_{VERSION}.mp3"
    temp=dest/f"trilha_original_{mood}_{VERSION}.wav"
    partial=dest/f"trilha_original_{mood}_{VERSION}.partial.mp3"
    if not final.is_file() or final.stat().st_size<6000:
        try:
            _synth(temp,mood)
            cmd=[studio.FFMPEG,"-hide_banner","-loglevel","error",
                 "-nostdin","-y","-i",str(temp),"-c:a","libmp3lame",
                 "-b:a","64k","-ar",str(SAMPLE_RATE),str(partial)]
            proc=subprocess.run(cmd,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                                timeout=90,check=False)
            if proc.returncode or not partial.is_file() or partial.stat().st_size<6000:
                raise RuntimeError("Não foi possível produzir a trilha original")
            partial.replace(final)
        finally:
            temp.unlink(missing_ok=True)
            partial.unlink(missing_ok=True)
    relative=str(final.relative_to(studio.MEDIA))
    studio.update_project(pid,soundtrack=relative)
    report={
        "version":VERSION,"origin":"original_generated_locally",
        "license":"original instrumental track created for this project",
        "mood":mood,"label":PROFILES[mood]["label"],
        "no_external_music_downloads":True,"api_charge_usd":0,
        "track_seconds":MUSIC_SECONDS,"mixing":
            "Low-level loop in short-form; longform FFmpeg voice-aware ducking",
    }
    path=dest/"soundtrack_origin.json"
    path.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
    print(f"[music-director] original mood={mood} project={pid[:8]} file={final.stat().st_size}bytes; Gemini calls=0",flush=True)
    return {"status":"ready",**report,"file":relative}

def self_test():
    import tempfile
    cases=[
        ({"title":"Poner límites con calma","topic":"estoicismo"},"reflective"),
        ({"title":"Mistérios do oceano profundo","topic":"investigação"},"mystery"),
        ({"title":"História do Império Romano","topic":"civilização antiga"},"historical"),
        ({"title":"Inteligência artificial em 2026","topic":"tecnologia"},"science"),
        ({"title":"Os animais da floresta","topic":"natureza"},"nature"),
        ({"title":"Superação e conquistas","topic":"motivação"},"inspiring"),
    ]
    for project,expected in cases:
        got=mood_for(project)
        if got!=expected:raise AssertionError(f"{project['title']}: {got}, expected {expected}")
    with tempfile.TemporaryDirectory() as temp:
        import wave
        dest=Path(temp)/"sample.wav"
        _synth(dest,"reflective",seconds=1.3,rate=8000)
        with wave.open(str(dest),"rb") as wav:
            if wav.getnframes()!=10400:raise AssertionError("Audio sample length")
            if not any(wav.readframes(1024)):raise AssertionError("Silence")
    print("[music-director] SELF-TEST PASS 6 moods, original waveform, no paid API",flush=True)

def install(studio):
    old_render=studio.render_task
    def music_then_render(pid):
        project=studio.get_project(pid)
        if project.get("status")!="concluído":
            try:
                result=ensure(studio,pid)
                if result.get("status")!="ready":
                    print("[music-director] "+result.get("status","unknown"),flush=True)
            except (OSError,RuntimeError,ValueError,KeyError) as exc:
                # Music is optional; narration and project production are not.
                print("[music-director] fallback: "+type(exc).__name__,flush=True)
        return old_render(pid)
    studio.render_task=music_then_render

    @studio.app.get("/api/music/projects/{pid}")
    def status(pid:str):
        project=studio.get_project(pid)
        return {"project_id":pid,"status":project.get("status"),
                "mood_recommended":mood_for(project),
                "mood_label":PROFILES[mood_for(project)]["label"],
                "soundtrack_saved":bool(project.get("soundtrack")),
                "soundtrack_path":project.get("soundtrack") or None,
                "new_paid_provider":False,
                "completed_project_preserved":project.get("status")=="concluído",
                "note":"Músicas criadas localmente; não gera nem renderiza nesta consulta."}

if __name__=="__main__":
    self_test()
    app=Path("/app/app.py")
    if not app.exists():raise SystemExit("Missing app.py")
    marker="\nimport studio_music_director as _music_director\n"
    code=app.read_text(encoding="utf-8")
    if marker not in code:
        app.write_text(code+marker+
            '_music_director.install(__import__("sys").modules[__name__])\n',
            encoding="utf-8")
