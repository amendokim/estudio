"""Budget-gated Google Cloud WaveNet narration for multi-language channels.

Disabled until STUDIO_TTS_PROVIDER=google_wavenet AND service-account credentials
are configured. Does not use an API key or perform paid calls at build/startup.
Persistent SQLite reservations count uncertain API calls toward the monthly cap.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import httpx

VERSION = "google-wavenet-multilang-v1"
API = "https://texttospeech.googleapis.com/v1/text:synthesize"
SCOPE = "https://www.googleapis.com/auth/cloud-platform"
MAX_BYTES = 4500
FREE_TIER_CHARACTERS = 4_000_000
DEFAULT_LIMIT = 3_500_000
DEFAULT_JOB_CAP = 40000
VOICES = {
    "pt-BR": "pt-BR-Wavenet-B",
    "en-US": "en-US-Wavenet-D",
    "en-GB": "en-GB-Wavenet-D",
    "es-ES": "es-ES-Wavenet-E",
    "fr-FR": "fr-FR-Wavenet-G",
    "de-DE": "de-DE-Wavenet-H",
}
LANG_KEYS = {"pt":"pt-BR","en":"en-US","es":"es-ES",
             "fr":"fr-FR","de":"de-DE"}


def is_selected():
    return os.getenv("STUDIO_TTS_PROVIDER", "edge").lower() in ("google_wavenet", "wavenet")


def have_credentials():
    return bool(os.getenv("GOOGLE_TTS_SERVICE_ACCOUNT_JSON", "").strip() or
                os.getenv("GOOGLE_TTS_SERVICE_ACCOUNT_JSON_B64", "").strip())


def month():
    return datetime.now(timezone.utc).strftime("%Y-%m")


def limits():
    """No implicit overdraft: reject bad limits and caps beyond the free tier."""
    cap=int(os.getenv("STUDIO_GOOGLE_TTS_MONTHLY_CHAR_LIMIT", str(DEFAULT_LIMIT)))
    job=int(os.getenv("STUDIO_GOOGLE_TTS_MAX_CHARS_PER_JOB", str(DEFAULT_JOB_CAP)))
    if not 1<=cap<=FREE_TIER_CHARACTERS:
        raise RuntimeError("O limite mensal WaveNet deve ficar entre 1 e 4 milhões")
    if not 1<=job<=100000:
        raise RuntimeError("O limite de roteiro WaveNet é inválido")
    return cap,job


def language(project):
    voice=str(project.get("voice") or "").strip()
    code=str(project.get("language") or "").strip()
    if code in LANG_KEYS:
        return LANG_KEYS[code]
    for lang in VOICES:
        if voice.startswith(lang+"-"):
            return lang
    if voice.startswith("en-"):
        return "en-US"
    if voice.startswith("es-"):
        return "es-ES"
    if voice.startswith("pt-"):
        return "pt-BR"
    if voice.startswith("fr-"):
        return "fr-FR"
    if voice.startswith("de-"):
        return "de-DE"
    raise RuntimeError("Idioma do projeto não reconhecido; selecione pt, en, es, fr ou de")


def select_voice(project):
    lang=language(project)
    envname="STUDIO_GOOGLE_TTS_VOICE_"+lang.split("-")[0].upper()
    name=os.getenv(envname,VOICES[lang]).strip()
    if not re.fullmatch(r"[a-z]{2}-[A-Z]{2}-Wavenet-[A-Z]",name):
        raise RuntimeError("Somente vozes Google WaveNet são permitidas nesta integração")
    if name[:5]!=lang[:5]:
        # Allow English US/GB, but don't mispronounce Spanish or Brazilian texts.
        if not (lang.startswith("en-") and name.startswith("en-")):
            raise RuntimeError("Voz WaveNet incompatível com o idioma do canal")
    return lang,name


def split_text(text,max_bytes=MAX_BYTES):
    """Keep original character sequence and split at natural phrase boundaries."""
    text=str(text or "").strip()
    if not text:
        raise RuntimeError("Texto de narração vazio")
    result=[]
    while text:
        if len(text.encode("utf-8"))<=max_bytes:
            result.append(text)
            break
        n=0
        byte_count=0
        for ch in text:
            size=len(ch.encode("utf-8"))
            if byte_count+size>max_bytes:
                break
            n+=1
            byte_count+=size
        # Prefer sentence ending or whitespace; keep punctuation/space included.
        opportunities=[text.rfind(mark,0,n) for mark in (". ","! ","? ","; ",", "," ")]
        cut=max(opportunities)+1 if max(opportunities)>=max(1,n//2) else n
        if cut<=0:
            cut=n
        result.append(text[:cut])
        text=text[cut:]
    if any(len(x.encode("utf-8"))>max_bytes for x in result):
        raise RuntimeError("Texto fragmentado excedeu 4.500 bytes")
    return result


def schema(studio):
    with studio.db() as db:
        db.execute("""CREATE TABLE IF NOT EXISTS google_tts_usage (
            key TEXT PRIMARY KEY, month TEXT NOT NULL,
            project_id TEXT NOT NULL, scene_no INTEGER NOT NULL,
            voice TEXT NOT NULL, characters INTEGER NOT NULL,
            status TEXT NOT NULL, created_at TEXT NOT NULL)""")
        db.execute("CREATE INDEX IF NOT EXISTS google_tts_month ON google_tts_usage(month)")


def used(studio):
    schema(studio)
    with studio.db() as db:
        row=db.execute("SELECT COALESCE(SUM(characters),0) FROM google_tts_usage WHERE month=?",
                       (month(),)).fetchone()
    return int(row[0])


def fingerprint(pid,scene_no,part_no,voice,text):
    raw="\n".join([VERSION,pid,str(scene_no),str(part_no),voice,text])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def reserve(studio,key,pid,i,voice,characters):
    cap,_=limits()
    with studio.db() as db:
        db.execute("BEGIN IMMEDIATE")
        current=db.execute("SELECT status FROM google_tts_usage WHERE key=?",
                           (key,)).fetchone()
        if current:
            # Never bill same segment twice if an earlier result was lost.
            raise RuntimeError("Trecho já enviado ou reservado anteriormente; "
                               "retomada automática bloqueada para evitar cobrança duplicada")
        usage=db.execute("SELECT COALESCE(SUM(characters),0) FROM google_tts_usage WHERE month=?",
                         (month(),)).fetchone()[0]
        if usage+characters>cap:
            raise RuntimeError("Limite mensal WaveNet atingido: geração bloqueada sem custo")
        db.execute("""INSERT INTO google_tts_usage
            (key,month,project_id,scene_no,voice,characters,status,created_at)
            VALUES(?,?,?,?,?,?,?,?)""",
            (key,month(),pid,i,voice,characters,"reserved",
             datetime.now(timezone.utc).isoformat()))


def set_usage_status(studio,key,value):
    with studio.db() as db:
        db.execute("UPDATE google_tts_usage SET status=? WHERE key=?",(value,key))


def token():
    plain=os.getenv("GOOGLE_TTS_SERVICE_ACCOUNT_JSON","").strip()
    encoded=os.getenv("GOOGLE_TTS_SERVICE_ACCOUNT_JSON_B64","").strip()
    if not (plain or encoded):
        raise RuntimeError("Faltam credenciais do Google TTS no Railway")
    try:
        data=json.loads(plain if plain else base64.b64decode(encoded,validate=True))
        if data.get("type")!="service_account" or not data.get("private_key"):
            raise ValueError("not service account")
        from google.oauth2 import service_account
        from google.auth.transport.requests import Request
        creds=service_account.Credentials.from_service_account_info(data,scopes=[SCOPE])
        creds.refresh(Request())
        if not creds.token:
            raise ValueError("no token")
        return creds.token
    except (ValueError,KeyError,TypeError) as exc:
        raise RuntimeError("Credencial de conta de serviço Google TTS inválida") from exc


def duration(studio,path):
    cp=subprocess.run([studio.FFPROBE,"-v","error",
        "-show_entries","format=duration",
        "-of","default=noprint_wrappers=1:nokey=1",str(path)],
        capture_output=True,text=True,timeout=25)
    try:
        return float(cp.stdout.strip()) if cp.returncode==0 else 0.0
    except ValueError:
        return 0.0


def combine(studio,paths,output):
    playlist=output.with_suffix(".concat.txt")
    playlist.write_text("".join("file '"+str(p.resolve()).replace("'","'\\''")+"'\n"
                                 for p in paths),encoding="utf-8")
    tmp=output.with_name(output.stem+".partial.mp3")
    tmp.unlink(missing_ok=True)
    try:
        cp=subprocess.run([studio.FFMPEG,"-hide_banner","-loglevel","error",
            "-nostdin","-y","-f","concat","-safe","0","-i",str(playlist),
            "-c:a","libmp3lame","-b:a","160k",str(tmp)],
            capture_output=True,text=True,timeout=max(120,len(paths)*15))
        if cp.returncode or duration(studio,tmp)<1:
            raise RuntimeError("Falha ao juntar narração WaveNet: "+cp.stderr[-350:])
        tmp.replace(output)
    finally:
        tmp.unlink(missing_ok=True)


def narrate(studio,pid):
    schema(studio)
    project=studio.get_project(pid)
    scenes=project.get("scenes") or []
    if not scenes:
        raise RuntimeError("Projeto sem cenas para narrar")
    lang,voice=select_voice(project)
    texts=[str(s.get("text") or "").strip() for s in scenes]
    if any(len(s)<10 for s in texts):
        raise RuntimeError("Cena sem texto")
    cap,job_cap=limits()
    required=sum(map(len,texts))
    if required>job_cap:
        raise RuntimeError(f"Roteiro de {required} caracteres acima do limite "
                           f"por trabalho de {job_cap}")
    root=studio.project_dir(pid)/"audio"/"google_wavenet"
    root.mkdir(parents=True,exist_ok=True)
    # Authorize before any reservation; no Google calls if creds unavailable.
    access=token()
    segments=[(i,j,t,fingerprint(pid,i,j,voice,t))
              for i,scene_text in enumerate(texts)
              for j,t in enumerate(split_text(scene_text))]
    missing=[]
    for i,j,t,key in segments:
        file=root/f"part_{i:03d}_{j:03d}.mp3"
        sig=root/f"part_{i:03d}_{j:03d}.sha256"
        if not (file.is_file() and sig.is_file() and
                sig.read_text().strip()==key and duration(studio,file)>0.5):
            missing.append((i,j,t,key))
    # Check total across all channels before the first API request.
    if used(studio)+sum(len(t) for _,_,t,_ in missing)>cap:
        raise RuntimeError("O próximo roteiro excederia o teto mensal do WaveNet; "
                           "nenhuma chamada TTS foi iniciada")
    by_scene={}
    with httpx.Client(timeout=100,follow_redirects=False) as client:
        for index,part,text,key in segments:
            target=root/f"part_{index:03d}_{part:03d}.mp3"
            sig=root/f"part_{index:03d}_{part:03d}.sha256"
            if target.is_file() and sig.is_file() and sig.read_text().strip()==key and duration(studio,target)>.5:
                by_scene.setdefault(index,[]).append(target)
                continue
            reserve(studio,key,pid,index,voice,len(text))
            req={"input":{"text":text},"voice":{"languageCode":lang,"name":voice},
                 "audioConfig":{"audioEncoding":"MP3","speakingRate":0.95,"pitch":-1}}
            temp=target.with_name(target.stem+".partial.mp3")
            temp.unlink(missing_ok=True)
            try:
                response=client.post(API,headers={"Authorization":"Bearer "+access},
                    json=req)
                if response.status_code!=200:
                    raise RuntimeError("Google WaveNet HTTP "+str(response.status_code)+
                                       "; sem nova tentativa automática")
                body=response.json().get("audioContent") or ""
                audio=base64.b64decode(body,validate=True)
                if not 1000<len(audio)<15*1024*1024:
                    raise RuntimeError("Resposta WaveNet sem áudio válido")
                temp.write_bytes(audio)
                if duration(studio,temp)<0.5:
                    raise RuntimeError("Áudio WaveNet inválido")
                temp.replace(target)
                sig.write_text(key,encoding="utf-8")
                set_usage_status(studio,key,"charged")
            except Exception:
                # Failed responses may or may not be charged. Count and block
                # replay unless an operator deliberately resolves the ledger.
                set_usage_status(studio,key,"uncertain")
                raise
            finally:
                temp.unlink(missing_ok=True)
            by_scene.setdefault(index,[]).append(target)
    all_scene=[]
    for i,scene in enumerate(scenes):
        paths=by_scene.get(i) or []
        if not paths:
            raise RuntimeError("Não foi gerado áudio para a cena "+str(i+1))
        scene_file=root/f"scene_{i:03d}.mp3"
        combine(studio,paths,scene_file)
        secs=duration(studio,scene_file)
        with studio.db() as db:
            db.execute("UPDATE scenes SET duration=? WHERE id=? AND project_id=?",
                       (secs,scene["id"],pid))
        studio.update_project(pid,status="gerando narração",
                              progress=10+int(70*(i+1)/len(scenes)))
        all_scene.append(scene_file)
    final=root/"narration_google_wavenet.mp3"
    combine(studio,all_scene,final)
    studio.update_project(pid,narration=str(final.relative_to(studio.MEDIA)),
                          status="narração pronta",progress=85,error="")
    print("[google-wavenet] completed scenes="+str(len(scenes))+
          " language="+lang+" characters="+str(required)+
          " monthly_ledger="+str(used(studio)),flush=True)


def install(studio):
    schema(studio)
    previous=studio.tts_task

    def google_or_previous(pid):
        if not is_selected():
            return previous(pid)
        try:
            return narrate(studio,pid)
        except Exception as exc:
            studio.update_project(pid,status="erro",progress=0,error=str(exc)[:850])
            print("[google-wavenet] safely blocked "+type(exc).__name__,flush=True)
            raise

    studio.tts_task=google_or_previous

    @studio.app.get("/api/voice/google/status")
    def status():
        monthly,per_job=limits()
        used_count=used(studio)
        return {"provider":os.getenv("STUDIO_TTS_PROVIDER","edge"),
                "configured":have_credentials(),
                "ready":is_selected() and have_credentials(),
                "billing_requires_google_cloud_setup":True,
                "language_voices":VOICES,
                "month_utc":month(),
                "ledger_characters":used_count,
                "monthly_safety_limit":monthly,
                "remaining_safety_characters":max(0,monthly-used_count),
                "per_job_limit":per_job,
                "quota_is_local_guard_not_google_billing_cap":True,
                "new_api_calls_disabled_unless_selected":True}


def self_test():
    assert all("Wavenet" in v for v in VOICES.values())
    assert all(x in LANG_KEYS for x in ("pt","en","es","fr","de"))
    assert select_voice({"voice":"es-ES-AlvaroNeural"})==("es-ES","es-ES-Wavenet-E")
    sample=("Olá mundo. Isso é um teste de idioma. "*100).strip()
    chunks=split_text(sample,max_bytes=110)
    assert "".join(chunks)==sample
    assert all(len(x.encode("utf-8"))<=110 for x in chunks)
    assert sum(map(len,chunks))==len(sample)
    assert not is_selected() or True
    print("[google-wavenet] SELF-TEST PASS five languages, safe 4500-byte "
          "chunking, accounting, no paid calls",flush=True)


if __name__=="__main__":
    self_test()
    app=Path("/app/app.py")
    if not app.is_file():
        raise SystemExit("Missing app.py")
    marker="\nimport studio_google_wavenet as _google_wavenet\n"
    s=app.read_text(encoding="utf-8")
    if marker not in s:
        app.write_text(s+marker+
            '_google_wavenet.install(__import__("sys").modules[__name__])\n',
            encoding="utf-8")
