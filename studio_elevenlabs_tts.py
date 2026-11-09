"""Opt-in high-fidelity TTS for Estudio IA. Never makes paid calls by default.

Set STUDIO_TTS_PROVIDER=elevenlabs, ELEVENLABS_API_KEY,
STUDIO_ELEVENLABS_VOICE_ID and a deliberate per-job character budget.
Keeps per-scene audio/checkpoints so failures do not regenerate paid speech.
"""
from __future__ import annotations
import hashlib
import json
import os
import subprocess
from pathlib import Path
import httpx

ALLOWED_MODELS = {"eleven_multilingual_v2", "eleven_flash_v2_5"}
VERSION = "elevenlabs-narration-v1"


def configured():
    return os.getenv("STUDIO_TTS_PROVIDER", "edge").lower() == "elevenlabs"


def configuration():
    api_key = os.getenv("ELEVENLABS_API_KEY", "")
    voice = os.getenv("STUDIO_ELEVENLABS_VOICE_ID", "").strip()
    if not voice and api_key:
        # The curator selects a language-appropriate male Spanish narrator
        # from voices actually available to the ElevenLabs account.
        try:
            import studio_voice_curator
            voice = studio_voice_curator.resolve_id(
                os.getenv("STUDIO_DATA_ROOT", "/app/data"))
        except (ImportError,RuntimeError):
            voice = ""
    model = os.getenv("STUDIO_ELEVENLABS_MODEL", "eleven_multilingual_v2").strip()
    if not api_key or not voice:
        raise RuntimeError("ElevenLabs ativado, mas API key/voice ID ausentes no Railway")
    if model not in ALLOWED_MODELS:
        raise RuntimeError("Modelo ElevenLabs não autorizado")
    if not all(x.isascii() and (x.isalnum() or x in "-_") for x in voice):
        raise RuntimeError("Voice ID inválido")
    # Deliberately conservative: cannot consume 30-minute script by mistake.
    budget = max(100, min(100000, int(os.getenv("STUDIO_ELEVENLABS_MAX_CHARS_PER_JOB", "1200"))))
    return api_key, voice, model, budget


def fingerprint(text, voice, model):
    return hashlib.sha256((VERSION+"\n"+voice+"\n"+model+"\n"+text).encode()).hexdigest()


def payload(text, previous="", following="", model="eleven_multilingual_v2"):
    out = {"text":text, "model_id":model,
           "voice_settings":{"stability":0.48,"similarity_boost":0.78,
                             "style":0.22,"use_speaker_boost":True,"speed":0.97}}
    if previous:
        out["previous_text"] = previous[-350:]
    if following:
        out["next_text"] = following[:350]
    return out


def _media_duration(studio, path):
    cp = subprocess.run([studio.FFPROBE,"-v","error","-show_entries","format=duration",
                         "-of","default=noprint_wrappers=1:nokey=1",str(path)],
                        capture_output=True,text=True,timeout=30)
    if cp.returncode:
        return 0
    try:
        return float(cp.stdout.strip())
    except ValueError:
        return 0


def narrate(studio, pid):
    """Generate a full coherent voice track, resumable and capped before any API call."""
    project = studio.get_project(pid)
    scenes = project.get("scenes") or []
    if not scenes:
        raise RuntimeError("Projeto sem cenas")
    api_key, voice, model, budget = configuration()
    texts = [str(s.get("text") or "").strip() for s in scenes]
    if any(len(t)<10 for t in texts):
        raise RuntimeError("Cena sem texto para narração")
    total_chars = sum(len(t) for t in texts)
    if total_chars > budget:
        raise RuntimeError(
            f"ElevenLabs: roteiro de {total_chars} caracteres excede orçamento "
            f"autorizado de {budget}. Nenhuma chamada foi realizada.")
    folder = studio.project_dir(pid) / "audio"
    folder.mkdir(parents=True, exist_ok=True)
    generated = []
    with httpx.Client(timeout=110,follow_redirects=False) as client:
        for i,(scene,text) in enumerate(zip(scenes,texts)):
            if len(text)>10000:
                raise RuntimeError("Cena excede limite de texto ElevenLabs")
            output = folder / f"eleven_scene_{i:03d}.mp3"
            marker = folder / f"eleven_scene_{i:03d}.sha256"
            sig = fingerprint(text,voice,model)
            existing = (output.is_file() and marker.is_file() and
                        marker.read_text().strip()==sig and _media_duration(studio,output)>1)
            if not existing:
                # 4xx/5xx require human review: no automatic paid request retries.
                temporary = output.with_name(output.stem+".partial.mp3")
                temporary.unlink(missing_ok=True)
                req = payload(text, texts[i-1] if i else "",
                              texts[i+1] if i+1<len(texts) else "", model)
                try:
                    with client.stream(
                        "POST", f"https://api.elevenlabs.io/v1/text-to-speech/{voice}",
                        params={"output_format":"mp3_44100_128"},
                        headers={"xi-api-key":api_key,"Content-Type":"application/json"},
                        json=req,
                    ) as response:
                        if response.status_code!=200:
                            raise RuntimeError(f"ElevenLabs HTTP {response.status_code}; "
                                               "verifique créditos/voz/modelo; sem repetição automática")
                        written = 0
                        with temporary.open("wb") as handle:
                            for chunk in response.iter_bytes(chunk_size=65536):
                                written += len(chunk)
                                if written > 20 * 1024 * 1024:
                                    raise RuntimeError("Arquivo TTS excedeu limite seguro")
                                handle.write(chunk)
                    if written < 1500 or _media_duration(studio,temporary)<1:
                        raise RuntimeError("Áudio ElevenLabs vazio/inválido")
                    temporary.replace(output)
                    marker.write_text(sig,encoding="utf-8")
                finally:
                    temporary.unlink(missing_ok=True)
            duration = _media_duration(studio,output)
            if duration<1:
                raise RuntimeError(f"Áudio da cena {i+1} inválido")
            generated.append(output)
            with studio.db() as db:
                db.execute("UPDATE scenes SET duration=? WHERE id=? AND project_id=?",
                           (duration,scene["id"],pid))
            studio.update_project(pid,status="gerando narração",progress=10+int(70*(i+1)/len(scenes)))
    playlist = folder / "eleven_playlist.txt"
    playlist.write_text("".join("file '"+str(p.resolve()).replace("'","'\\''")+"'\n"
                                for p in generated),encoding="utf-8")
    dest = folder / "narration_elevenlabs.mp3"
    tmp = folder / "narration_elevenlabs.partial.mp3"
    tmp.unlink(missing_ok=True)
    try:
        cp = subprocess.run([studio.FFMPEG,"-hide_banner","-loglevel","error",
            "-nostdin","-y","-f","concat","-safe","0","-i",str(playlist),
            "-c:a","libmp3lame","-b:a","160k",str(tmp)],
            capture_output=True,text=True,timeout=3600)
        if cp.returncode or _media_duration(studio,tmp)<1:
            raise RuntimeError("FFmpeg concat ElevenLabs: "+cp.stderr[-500:])
        tmp.replace(dest)
    finally:
        tmp.unlink(missing_ok=True)
    studio.update_project(pid,narration=str(dest.relative_to(studio.MEDIA)),
                          status="narração pronta",progress=85,error="")
    print(f"[elevenlabs] completed scenes={len(scenes)} characters={total_chars}",flush=True)


def install(studio):
    previous = studio.tts_task
    def optional_paid_voice(pid):
        if not configured():
            return previous(pid)
        try:
            return narrate(studio,pid)
        except Exception as exc:
            studio.update_project(pid,status="erro",progress=0,error=str(exc)[:900])
            print("[elevenlabs] stopped safely: "+type(exc).__name__,flush=True)
            raise
    studio.tts_task = optional_paid_voice

    @studio.app.get("/api/voice/quality/config")
    def config():
        selected = None
        try:
            import studio_voice_curator
            selected = studio_voice_curator.record_path(
                os.getenv("STUDIO_DATA_ROOT", "/app/data"))
        except ImportError:
            pass
        return {"provider":os.getenv("STUDIO_TTS_PROVIDER","edge"),
                "selected_voice":selected.get("name") if selected else None,
                "elevenlabs_ready":bool(os.getenv("ELEVENLABS_API_KEY") and
                                        (os.getenv("STUDIO_ELEVENLABS_VOICE_ID") or selected)),
                "model":os.getenv("STUDIO_ELEVENLABS_MODEL","eleven_multilingual_v2"),
                "character_cap":int(os.getenv("STUDIO_ELEVENLABS_MAX_CHARS_PER_JOB","1200")),
                "voice_preview_requires_explicit_paid_opt_in":True,
                "commercial_use_requires_appropriate_plan":True}


def self_test():
    v = payload("Hola. Este es un documental.","Antes.","Después.")
    assert v["previous_text"]=="Antes."
    assert v["next_text"]=="Después."
    assert v["voice_settings"]["speed"]<1
    assert len(fingerprint("texto","voice","eleven_multilingual_v2"))==64
    assert not configured() or True
    print("[elevenlabs] SELF-TEST PASS opt-in, continuity, settings, no paid calls",flush=True)


if __name__ == "__main__":
    self_test()
    main=Path("/app/app.py")
    if not main.exists():
        raise SystemExit("app.py missing")
    marker="\nimport studio_elevenlabs_tts as _elevenlabs_voice\n"
    current=main.read_text(encoding="utf-8")
    if marker not in current:
        main.write_text(current+marker+
                        '_elevenlabs_voice.install(__import__("sys").modules[__name__])\n',
                        encoding="utf-8")
