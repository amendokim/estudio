"""Automatic Spanish documentary voice curator for Codigo Fudoshin.

Only reads a secret from server-side Railway env. Metadata discovery uses free
read endpoints. One explicitly enabled preview, capped at 350 Spanish chars,
is the only paid operation. Preview/status are independent from publication.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
from pathlib import Path

import httpx
from fastapi import HTTPException
from fastapi.responses import FileResponse

VERSION="fudoshin-voice-curator-v1"
SAMPLE=("A veces, la decisión más valiente no es decir que sí. "
        "Es aprender a decir no, sin culpa y sin miedo a decepcionar a los demás. "
        "La filosofía estoica nos recuerda que la verdadera libertad empieza "
        "cuando dejamos de vivir para satisfacer las expectativas ajenas.")
_LOCK=threading.Lock()


def folder(studio):
    result=Path(studio.DATA)/"voice_curator"
    result.mkdir(parents=True,exist_ok=True)
    return result


def record(studio):
    file=folder(studio)/"selected.json"
    if not file.is_file():
        return None
    try:
        obj=json.loads(file.read_text(encoding="utf-8"))
        if isinstance(obj,dict) and re.fullmatch(r"[A-Za-z0-9_-]{12,100}",
                     str(obj.get("voice_id") or "")):
            return obj
    except (ValueError,OSError):
        return None
    return None


def _words(x):
    return re.sub(r"\\s+"," ",str(x or "").lower()).strip()


def summarize(voice,installed=False):
    labels=voice.get("labels") or {}
    if not isinstance(labels,dict):
        labels={}
    verified=voice.get("verified_languages") or []
    verified_text=" ".join(str(x.get("language") or "")+" "+str(x.get("locale") or "")
                           for x in verified if isinstance(x,dict))
    return {
        "voice_id":str(voice.get("voice_id") or ""),
        "name":str(voice.get("name") or "")[:120],
        "gender":str(labels.get("gender") or voice.get("gender") or "")[:50],
        "accent":str(labels.get("accent") or voice.get("accent") or "")[:120],
        "age":str(labels.get("age") or voice.get("age") or "")[:90],
        "language":str(labels.get("language") or voice.get("language") or "")[:50],
        "locale":str(labels.get("locale") or voice.get("locale") or "")[:50],
        "use_case":str(labels.get("use_case") or voice.get("use_case") or "")[:140],
        "description":str(voice.get("description") or "")[:300],
        "verified_languages":verified_text[:160],
        "category":str(voice.get("category") or "")[:45],
        "public_owner_id":str(voice.get("public_owner_id") or "")[:130],
        "preview_url":str(voice.get("preview_url") or "")[:400],
        "available":bool(installed),
        "notice_period":voice.get("notice_period"),
        "rate_multiplier":voice.get("rate_multiplier"),
    }


def score(v):
    """High weight to Spanish Spain and credible narration labels."""
    merged=_words(" ".join(str(v.get(k) or "") for k in
                     ("name","accent","age","language","locale","description",
                      "use_case","verified_languages")))
    gender=_words(v.get("gender"))
    # A different gender can be an excellent voice but not for this brief.
    if gender and gender not in ("male","masculino","man","masculine"):
        return -100
    points=23 if gender in ("male","masculino","man","masculine") else 0
    if any(t in merged for t in ("es-es","es_es","spanish (spain)","spain","españa",
                                 "castilian","castellano","peninsular","madrid")):
        points+=42
    elif any(t in merged for t in ("spanish","español","espanol","es-")):
        points+=18
    elif any(t in merged for t in ("mexican","mexico","méxico","latino","argentin",
                                   "colombian")):
        points-=35
    else:
        points-=12
    if any(t in merged for t in ("documentary","documental","narrator","narration",
                                 "narrador","storytelling","audiobook","audio book")):
        points+=24
    if any(t in merged for t in ("calm","deep","warm","profund","sereno","serene",
                                 "reflective","meditative","rich","mature")):
        points+=17
    if any(t in merged for t in ("middle","mature","old","adult","senior")):
        points+=7
    if any(t in merged for t in ("expressive","soothing","motivational","educational")):
        points+=5
    if any(t in merged for t in ("cartoon","child","sales","advertis","energetic",
                                 "hyper","excited","gaming")):
        points-=30
    if v.get("available"):
        points+=8
    return points


def _list(client,path,params):
    response=client.get("https://api.elevenlabs.io"+path,params=params)
    if response.status_code!=200:
        return [],response.status_code
    try:
        obj=response.json()
        voices=obj.get("voices") or []
        return voices if isinstance(voices,list) else [],200
    except ValueError:
        return [],502


def _select(studio):
    key=os.getenv("ELEVENLABS_API_KEY","")
    if not key:
        raise RuntimeError("ELEVENLABS_API_KEY não encontrada no Railway")
    # An explicitly set voice ID always takes priority over auto discovery.
    override=os.getenv("STUDIO_ELEVENLABS_VOICE_ID","").strip()
    if override:
        return {"voice_id":override,"name":"Voz configurada manualmente",
                "decision":"explicit_env","version":VERSION}
    saved=record(studio)
    if saved:
        return saved

    with httpx.Client(timeout=18,follow_redirects=False,
                      headers={"xi-api-key":key}) as client:
        known,known_status=_list(client,"/v2/voices",{"page_size":100})
        available=[summarize(x,True) for x in known
                   if isinstance(x,dict) and x.get("voice_id")]
        for item in available:
            item["score"]=score(item)
        available.sort(key=lambda x:-x["score"])
        # Can browse library without consuming audio-generation credits.
        # Searching multiple labels is more robust than relying on a voice name.
        shared=[]
        lib_status=[]
        for params in (
            {"language":"es","gender":"male","search":"narration",
             "page_size":80,"include_custom_rates":"false",
             "include_live_moderated":"false"},
            {"language":"es","gender":"male","search":"documentary",
             "page_size":60,"include_custom_rates":"false",
             "include_live_moderated":"false"},
            {"language":"es","gender":"male","sort":"trending",
             "page_size":60,"include_custom_rates":"false",
             "include_live_moderated":"false"},
        ):
            hits,status=_list(client,"/v1/shared-voices",params)
            lib_status.append(status)
            shared.extend(summarize(x,False) for x in hits
                          if isinstance(x,dict) and x.get("voice_id"))
        # Don't automatically register a professional voice with custom rates.
        best_by_id={}
        for item in shared:
            item["score"]=score(item)
            if (not item.get("rate_multiplier") or
                float(item.get("rate_multiplier") or 1)<=1):
                best_by_id[item["voice_id"]]=item
        shared=sorted(best_by_id.values(),key=lambda x:-x["score"])
        best_installed=available[0] if available else None
        recommended=shared[0] if shared else None
        selected=best_installed
        decision="installed_voice_ranking"
        notice=""
        # A superior licensed professional library voice may be added.
        # This does not create narration and has no hidden retries.
        if (recommended and recommended.get("public_owner_id") and
            os.getenv("STUDIO_VOICE_CURATOR_ALLOW_LIBRARY_ADD","0")=="1" and
            (not selected or recommended["score"] >= selected["score"] + 12)):
            owner=recommended["public_owner_id"]
            voice_id=recommended["voice_id"]
            if re.fullmatch(r"[A-Za-z0-9_-]{12,160}",owner) and re.fullmatch(
                    r"[A-Za-z0-9_-]{12,100}",voice_id):
                try:
                    res=client.post(
                        f"https://api.elevenlabs.io/v1/voices/add/{owner}/{voice_id}",
                        json={"new_name":"Estudio IA - Fudoshin - "+
                              recommended["name"][:48]},
                        timeout=20)
                    if res.status_code==200:
                        new_id=str(res.json().get("voice_id") or "")
                        if re.fullmatch(r"[A-Za-z0-9_-]{12,100}",new_id):
                            selected={**recommended,"voice_id":new_id,"available":True}
                            decision="shared_professional_added"
                    else:
                        notice="Biblioteca retornou HTTP "+str(res.status_code)+                            "; usando voz já disponível"
                except (ValueError,httpx.HTTPError):
                    notice="Voz da biblioteca indisponível; usando voz já disponível"
        if not selected or selected.get("score",-100)<25:
            result={"status":"no_suitable_voice","version":VERSION,
                    "installed_count":len(available),
                    "installed_http_status":known_status,
                    "library_http_statuses":lib_status,
                    "top_installed":available[:4],
                    "top_library":shared[:4],
                    "note":"Nenhuma voz instalada elegível em espanhol. "
                           "Não ativar TTS pago até haver opção compatível."}
            (folder(studio)/"selection_diagnostics.json").write_text(
                json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8")
            raise RuntimeError("Nenhuma voz elegível disponível; verifique "
                               "permissões da chave ou plano ElevenLabs")
        result={"status":"selected","voice_id":selected["voice_id"],
                "name":selected["name"],"accent":selected.get("accent"),
                "gender":selected.get("gender"),"score":selected.get("score"),
                "category":selected.get("category"),"version":VERSION,
                "decision":decision,"selected_at":time.time(),
                "top_installed":available[:4],
                "top_library":shared[:4],"note":notice,
                "installed_http_status":known_status,
                "library_http_statuses":lib_status}
        temp=folder(studio)/"selected.partial.json"
        temp.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8")
        temp.replace(folder(studio)/"selected.json")
        return result


def select(studio):
    with _LOCK:
        return _select(studio)


def resolve_id(data):
    existing=record_path(data)
    if not existing:
        raise RuntimeError("Voz não selecionada. Confira /api/voice/curator/status")
    return existing["voice_id"]


def record_path(data):
    p=Path(data)/"voice_curator"/"selected.json"
    try:
        v=json.loads(p.read_text(encoding="utf-8"))
        if re.fullmatch(r"[A-Za-z0-9_-]{12,100}",str(v.get("voice_id") or "")):
            return v
    except (OSError,ValueError):
        pass
    return None


def audition(studio):
    """One paid sample at most; durable claim before request prevents duplicates."""
    root=folder(studio)
    claim=root/"preview_claim.json"
    if claim.exists():
        return
    # Atomic one-time lock; no second try if a paid request may have occurred.
    try:
        with claim.open("x",encoding="utf-8") as handle:
            json.dump({"state":"claimed","at":time.time()},handle)
    except FileExistsError:
        return
    status_file=root/"preview_status.json"
    def persist(s,**data):
        tmp=root/"preview_status.partial.json"
        tmp.write_text(json.dumps({"status":s,**data},ensure_ascii=False),encoding="utf-8")
        tmp.replace(status_file)
    try:
        obj=select(studio)
        if len(SAMPLE)>350:
            raise RuntimeError("Prévia excede limite de 350 caracteres")
        import studio_elevenlabs_tts as tts
        key=os.getenv("ELEVENLABS_API_KEY","")
        model=os.getenv("STUDIO_ELEVENLABS_MODEL","eleven_multilingual_v2")
        # Extra fail-closed check, regardless of the project's usual budget.
        if model not in tts.ALLOWED_MODELS:
            raise RuntimeError("Modelo não aprovado")
        voice_id=obj["voice_id"]
        payload=tts.payload(SAMPLE,model=model)
        output=root/"fudoshin_voice_preview.mp3"
        tmp=root/"fudoshin_voice_preview.partial.mp3"
        tmp.unlink(missing_ok=True)
        with httpx.Client(timeout=110,follow_redirects=False) as client:
            with client.stream("POST",
                f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}",
                params={"output_format":"mp3_44100_128"},
                headers={"xi-api-key":key,"Content-Type":"application/json"},
                json=payload) as response:
                if response.status_code!=200:
                    raise RuntimeError("ElevenLabs TTS HTTP "+str(response.status_code))
                with tmp.open("wb") as handle:
                    size=0
                    for chunk in response.iter_bytes(65536):
                        size+=len(chunk)
                        if size>4*1024*1024:
                            raise RuntimeError("Prévia excedeu 4 MB")
                        handle.write(chunk)
        length=tts._media_duration(studio,tmp)
        if length<5 or length>90:
            raise RuntimeError("Prévia inválida: duração fora dos limites")
        tmp.replace(output)
        persist("completed",voice= obj["name"],voice_id=voice_id,
                seconds=round(length,2),characters=len(SAMPLE),bytes=output.stat().st_size)
        print(f"[voice-curator] preview completed: {len(SAMPLE)} characters, "
              f"{length:.1f}s, voice={obj['name']}",flush=True)
    except Exception as exc:
        persist("failed",error=type(exc).__name__+": "+str(exc)[:180])
        print("[voice-curator] preview failed "+type(exc).__name__+": "+
              str(exc)[:100],flush=True)


def startup(studio):
    def task():
        try:
            item=select(studio)
            print("[voice-curator] selected voice="+str(item.get("name"))+
                  " / score="+str(item.get("score"))+
                  " / "+str(item.get("decision")),flush=True)
            if os.getenv("STUDIO_VOICE_PREVIEW_ONCE","0")=="1":
                audition(studio)
        except Exception as exc:
            print("[voice-curator] selection failed "+type(exc).__name__+
                  ": "+str(exc)[:140],flush=True)
    threading.Thread(target=task,daemon=True,name="voice-curator-once").start()


def install(studio):
    @studio.app.get("/api/voice/curator/status")
    def voice_status():
        selected=record(studio)
        root=folder(studio)
        files={}
        for name in ("preview_status.json","selection_diagnostics.json"):
            path=root/name
            if path.is_file():
                try:
                    files[name]=json.loads(path.read_text(encoding="utf-8"))
                except ValueError:
                    files[name]={"status":"unreadable"}
        return {"version":VERSION,"api_key_configured":bool(
            os.getenv("ELEVENLABS_API_KEY")),"voice":selected,
            "preview":files.get("preview_status.json"),
            "diagnostics":files.get("selection_diagnostics.json"),
            "preview_url":"/api/voice/curator/preview.mp3" if
                (root/"fudoshin_voice_preview.mp3").is_file() else None}

    @studio.app.get("/api/voice/curator/preview.mp3")
    def preview_audio():
        p=folder(studio)/"fudoshin_voice_preview.mp3"
        if not p.is_file():
            raise HTTPException(404,detail="Prévia ainda não gerada")
        return FileResponse(p,media_type="audio/mpeg",
                            filename="codigo_fudoshin_elevenlabs.mp3")

    @studio.app.on_event("startup")
    def when_ready():
        if os.getenv("STUDIO_VOICE_CURATOR_ENABLED","0")=="1":
            startup(studio)


def self_test():
    assert score({"gender":"male","accent":"Spanish Spain",
                  "use_case":"Narration","description":"Warm deep mature voice"})>70
    assert score({"gender":"female","accent":"Spanish Spain"})<0
    assert score({"gender":"male","accent":"Mexican",
                  "use_case":"advertisement"})<40
    assert len(SAMPLE)<=350
    print("[voice-curator] SELF-TEST PASS ranking, European Spanish preference, "
          "one-shot budget and no build-time API calls",flush=True)


if __name__=="__main__":
    self_test()
    file=Path("/app/app.py")
    if not file.is_file():
        raise SystemExit("Missing /app/app.py")
    mark="\nimport studio_voice_curator as _voice_curator\n"
    s=file.read_text(encoding="utf-8")
    if mark not in s:
        file.write_text(s+mark+
            '_voice_curator.install(__import__("sys").modules[__name__])\n',
            encoding="utf-8")
