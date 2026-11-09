"""Single low-credit Free-tier voice audition; no professional-library API TTS.

Reads default/premade voices accessible to the API key, ranks native Spanish
first, and generates one <= 300-character audio sample only when explicitly
enabled. Does not change the production voice or the premium selection.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path

import httpx
from fastapi import HTTPException
from fastapi.responses import FileResponse

VERSION = "eleven-free-default-voice-trial-v1"
RUN = "free-default-spanish-sample-2026-11-09-v1"
SAMPLE = (
    "Aprender a decir no sin culpa es una forma de recuperar tu paz. "
    "Los estoicos entendían que no podemos controlar las expectativas de los demás, "
    "pero sí podemos decidir cómo responder. Hoy descubrirás cómo poner límites "
    "con serenidad, firmeza y respeto."
)


def directory(studio):
    target = Path(studio.DATA) / "voice_curator" / "free_default_trial"
    target.mkdir(parents=True, exist_ok=True)
    return target


def _write_atomic(path, obj):
    tmp = path.with_suffix(path.suffix + ".partial")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _safe_error(resp):
    try:
        detail = (resp.json() or {}).get("detail") or {}
        if isinstance(detail, dict):
            name = str(detail.get("status") or detail.get("code") or "unknown")
        else:
            name = "unknown"
    except (ValueError, AttributeError):
        name = "unknown"
    return re.sub("[^a-zA-Z0-9_-]", "", name)[:60]


def ranked(voice):
    labels = voice.get("labels") or {}
    if not isinstance(labels, dict):
        labels = {}
    name = str(voice.get("name") or "")
    description = str(voice.get("description") or "")
    accent = str(labels.get("accent") or voice.get("accent") or "")
    language = str(labels.get("language") or voice.get("language") or "")
    gender = str(labels.get("gender") or voice.get("gender") or "")
    case = str(labels.get("use_case") or voice.get("use_case") or "")
    category = str(voice.get("category") or "")
    searchable = " ".join((name, description, accent, language, case)).lower()
    accent_l = accent.lower()
    language_l = language.lower()
    gender_l = gender.lower()
    spanish_accent = any(word in accent_l for word in (
        "spanish", "spain", "españa", "castilian", "castellano",
        "madrid", "es-es", "peninsular"))
    spanish_language = any(word in language_l for word in (
        "spanish", "español", "espanol", "es-es"))
    # An explicitly Spanish/Castilian identity beats generic multilingual fluency.
    native = spanish_accent or spanish_language
    score = (120 if spanish_accent else 90 if spanish_language else 0)
    if gender_l in ("male", "masculino", "man", "masculine"):
        score += 30
    elif gender_l and gender_l not in ("unknown", "none"):
        score -= 80
    if any(w in searchable for w in ("narration", "narrator",
        "narrador", "documentary", "documental", "storyteller",
        "audiobook", "cultural")):
        score += 30
    if any(w in searchable for w in ("deep", "calm", "warm",
        "sereno", "mature", "profund", "reflective", "soothing")):
        score += 15
    if any(w in searchable for w in ("shouting", "cartoon",
        "childlike", "advertising", "hyperactive")):
        score -= 35
    # Only default/premade voices may be tried under Free-tier API.
    if category not in ("premade", "default"):
        return None
    voice_id = str(voice.get("voice_id") or "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{10,100}", voice_id):
        return None
    return {
        "voice_id": voice_id,
        "name": name[:100],
        "accent": accent[:100],
        "language": language[:100],
        "gender": gender[:30],
        "category": category,
        "score": score,
        "native_spanish_confirmed": native,
    }


def _load(studio):
    file = directory(studio) / "result.json"
    if not file.exists():
        return {"state": "not_started"}
    try:
        return json.loads(file.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return {"state": "unknown"}


def execute(studio):
    root = directory(studio)
    claim = root / "claim.json"
    try:
        with claim.open("x", encoding="utf-8") as handle:
            json.dump({"run": RUN, "claimed_at": time.time()}, handle)
    except FileExistsError:
        return

    def finish(state, **rest):
        _write_atomic(root / "result.json",
                      {"state": state, "run": RUN, **rest})
    try:
        secret = os.getenv("ELEVENLABS_API_KEY", "")
        if not secret:
            raise RuntimeError("Chave ElevenLabs não configurada")
        if len(SAMPLE) > 300:
            raise RuntimeError("Texto excedeu limite seguro de teste")
        with httpx.Client(timeout=90, follow_redirects=False,
                          headers={"xi-api-key": secret}) as client:
            response = client.get("https://api.elevenlabs.io/v2/voices",
                params={"voice_type": "default", "page_size": 100},
                timeout=20)
            if response.status_code != 200:
                raise RuntimeError("Listagem default HTTP " +
                    str(response.status_code) + " / " + _safe_error(response))
            payload = response.json()
            choices = [ranked(v) for v in payload.get("voices") or []
                       if isinstance(v, dict)]
            choices = [x for x in choices if x]
            # Default filter is mandatory; never fall back to a community voice.
            choices.sort(key=lambda v: (-v["score"], v["name"]))
            print("[free-voice] default candidates=" + str(len(choices)) +
                  " top=" + ", ".join(x["name"] + " (" +
                  str(x["score"]) + ")" for x in choices[:4]), flush=True)
            if not choices:
                raise RuntimeError("Sem voz padrão elegível na API Free")
            selected = choices[0]
            # Do not claim a native Spanish accent when we cannot verify one.
            request_body = {
                "text": SAMPLE,
                "model_id": "eleven_multilingual_v2",
                "voice_settings": {
                    "stability": 0.50,
                    "similarity_boost": 0.73,
                    "style": 0.15,
                    "use_speaker_boost": True,
                    "speed": 0.95
                },
            }
            sample_path = root / "sample.mp3"
            temp = root / "sample.partial.mp3"
            temp.unlink(missing_ok=True)
            try:
                with client.stream(
                    "POST",
                    "https://api.elevenlabs.io/v1/text-to-speech/" +
                        selected["voice_id"],
                    params={"output_format": "mp3_44100_128"},
                    headers={"Content-Type": "application/json"},
                    json=request_body,
                ) as result:
                    if result.status_code != 200:
                        raise RuntimeError("TTS default HTTP " +
                            str(result.status_code) + " / " +
                            _safe_error(result))
                    size = 0
                    with temp.open("wb") as out:
                        for chunk in result.iter_bytes(chunk_size=65536):
                            size += len(chunk)
                            if size > 3 * 1024 * 1024:
                                raise RuntimeError("Áudio excedeu limite")
                            out.write(chunk)
                import studio_elevenlabs_tts as tts
                length = tts._media_duration(studio, temp)
                if length < 3 or length > 55:
                    raise RuntimeError("MP3 de teste inválido")
                temp.replace(sample_path)
            finally:
                temp.unlink(missing_ok=True)
            finish("completed", selected=selected,
                   characters=len(SAMPLE), seconds=round(length, 2),
                   sample_url="/api/voice/free-trial/sample.mp3",
                   mode="free_default_voice_only",
                   production_voice_unchanged=True)
            print("[free-voice] COMPLETED name="+selected["name"]+
                  " native_spanish="+str(selected["native_spanish_confirmed"])+
                  " chars="+str(len(SAMPLE))+
                  " duration="+str(round(length,1)),flush=True)
    except Exception as exc:
        finish("failed", error=str(exc)[:250],
               production_voice_unchanged=True)
        print("[free-voice] FAILED "+str(exc)[:230],flush=True)


def install(studio):
    @studio.app.get("/api/voice/free-trial/status")
    def status():
        row = _load(studio)
        return {"trial": row, "premium_voice_preserved": True,
                "free_test_only": True,
                "no_youtube_publication": True}

    @studio.app.get("/api/voice/free-trial/sample.mp3")
    def download():
        out = directory(studio) / "sample.mp3"
        if _load(studio).get("state") != "completed" or not out.is_file():
            raise HTTPException(404, "Amostra ainda não concluída")
        return FileResponse(out, media_type="audio/mpeg",
                            filename="codigo_fudoshin_vozelevenlabs_free.mp3")

    @studio.app.on_event("startup")
    def once():
        if os.getenv("STUDIO_FREE_VOICE_TRIAL_ONCE", "0") == "1":
            threading.Thread(target=execute, args=(studio,),
                daemon=True, name="elevenlabs-free-voice-once").start()


def self_test():
    good = {"voice_id":"JBFqnCBsd6RMkjVDRZzb",
            "name":"Dramatic narrator", "category":"premade",
            "labels":{"gender":"male", "language":"Spanish",
                      "accent":"Spanish Spain",
                      "use_case":"Narration"}}
    ranked_good=ranked(good)
    assert ranked_good and ranked_good["score"] >= 170
    assert ranked_good["native_spanish_confirmed"]
    assert ranked({**good, "category":"professional"}) is None
    assert ranked({**good, "labels":{"gender":"male",
        "language":"English","accent":"British"}})["score"] < ranked_good["score"]
    assert len(SAMPLE) < 300
    print("[free-voice] SELF-TEST PASS free-premade-only, Spanish narrator ranking, "
          "one-shot 300-character cap, no build API calls",flush=True)


if __name__ == "__main__":
    self_test()
    path = Path("/app/app.py")
    if not path.is_file():
        raise SystemExit("App file missing")
    mark = "\nimport studio_free_voice_test as _free_voice_test\n"
    program = path.read_text(encoding="utf-8")
    if mark not in program:
        path.write_text(program + mark +
            '_free_voice_test.install(__import__("sys").modules[__name__])\n',
            encoding="utf-8")
