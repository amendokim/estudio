"""Isolated 90-second editorial-quality experiment for Meu Estudio IA.

Runs only on explicit POST. Reuses the narration and licensed footage from an
existing completed project, preserves its exports, and never publishes a video.
Gemini visual checking is opt-in and bounded to 16 paid calls per preview.
"""
from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path

import httpx
from fastapi import HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, Field

VERSION = "editor-v3-pilot-1"
RENDER_LOCK = threading.Lock()
MAX_VISION_CALLS = 16
MIN_FREE_MB = 850
MAX_REPEATS = 2
STATE = {}
STATE_LOCK = threading.Lock()


class PreviewRequest(BaseModel):
    duration_seconds: int = Field(default=90, ge=45, le=120)
    use_vision: bool = False


def _stamp():
    return datetime.now(timezone.utc).isoformat()


def _state(pid, **updates):
    with STATE_LOCK:
        STATE[pid] = {**STATE.get(pid, {}), **updates, "updated_at": _stamp()}
        return dict(STATE[pid])


def _run(studio, args, timeout=300):
    done = subprocess.run(
        [studio.FFMPEG, "-hide_banner", "-loglevel", "error", "-nostdin", "-y", *args],
        capture_output=True, text=True, timeout=timeout)
    if done.returncode:
        raise RuntimeError("FFmpeg V3: " + done.stderr[-650:])


def _seconds(studio, path):
    done = subprocess.run(
        [studio.FFPROBE, "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True, timeout=25)
    try:
        return float(done.stdout.strip()) if done.returncode == 0 else 0.0
    except ValueError:
        return 0.0


def _free(studio):
    return shutil.disk_usage(studio.DATA).free / 1048576


def _folder(studio, pid):
    return studio.project_dir(pid) / "export" / "editor_v3_pilot"


def _pick_scenes(project, seconds):
    picked, total = [], 0.0
    for scene in project.get("scenes") or []:
        if total >= seconds - .01:
            break
        item = dict(scene)
        item["duration"] = round(min(max(.25, float(item.get("duration") or 0)),
                                      seconds - total), 3)
        if item["duration"] < .25:
            break
        picked.append(item)
        total += item["duration"]
    if total < seconds - 2:
        raise RuntimeError("Projeto não oferece narração/cenas suficientes para o piloto")
    return picked, total


def _asset_id(item):
    return str(item.get("media_url") or item.get("media_local") or "")


def _clip(studio, pid, scene, asset, index):
    import studio_dynamic_montage as montage
    if not montage.valid_asset(studio, asset):
        raise RuntimeError("Mídia sem proveniência/licença aprovada")
    scene = {**scene, **asset}
    scene["id"] = str(scene["id"]) + "-v3-" + hashlib.sha256(
        _asset_id(asset).encode()).hexdigest()[:14]
    local = studio.fetch_clip(scene, pid)
    if not local or not Path(local).is_file():
        raise RuntimeError("Arquivo de mídia indisponível")
    return Path(local)


def _frame(studio, path, out):
    duration = _seconds(studio, path)
    if duration <= 0:
        raise RuntimeError("Não foi possível inspecionar a duração do vídeo")
    _run(studio, ["-ss", str(min(1.5, duration / 2)), "-i", str(path),
                  "-vf", "scale=512:-2", "-frames:v", "1", "-q:v", "6",
                  str(out)], timeout=50)
    if not out.is_file() or out.stat().st_size > 350_000:
        raise RuntimeError("Frame visual indisponível ou excessivamente grande")


def _vision(studio, path, text, folder, budget):
    """Inspect actual pixels, never the clip filename or seller tags only."""
    if budget["used"] >= MAX_VISION_CALLS:
        return None
    key = os.getenv("GEMINI_API_KEY", "")
    if not key:
        raise RuntimeError("Análise visual solicitada, mas GEMINI_API_KEY não existe")
    thumb = folder / ("frame_" + hashlib.sha256(
        (str(path) + text[:70]).encode()).hexdigest()[:14] + ".jpg")
    if not thumb.is_file():
        _frame(studio, path, thumb)
    budget["used"] += 1
    prompt = (
        "Você é um editor de documentários. Avalie SOMENTE o frame visível, "
        "comparando com esta fala: " + text[:400] + "\n"
        "Uma imagem genérica do mesmo tema NÃO basta. "
        "Responda JSON com relevance (0-5), subject (frase curta), "
        "reason (frase curta). Atribua 4 ou 5 somente quando a imagem "
        "representa concretamente algo dito. Não invente pessoas, países "
        "ou eventos que não são evidentes no frame.")
    model = os.getenv("STUDIO_V3_VISION_MODEL", "gemini-2.5-flash")
    if model not in ("gemini-2.5-flash", "gemini-2.5-flash-lite"):
        raise RuntimeError("Modelo de análise visual não permitido")
    payload = {
        "contents": [{"parts": [
            {"text": prompt},
            {"inline_data": {"mime_type": "image/jpeg",
                             "data": base64.b64encode(thumb.read_bytes()).decode()}}
        ]}],
        "generationConfig": {"temperature": 0, "maxOutputTokens": 280,
                             "responseMimeType": "application/json"}
    }
    with httpx.Client(timeout=50) as client:
        response = client.post(
            "https://generativelanguage.googleapis.com/v1beta/models/"
            + model + ":generateContent",
            headers={"x-goog-api-key": key}, json=payload)
        response.raise_for_status()
    parts = response.json()["candidates"][0]["content"]["parts"]
    result = json.loads("".join(x.get("text", "") for x in parts))
    score = result.get("relevance")
    if not isinstance(score, (int, float)) or not 0 <= score <= 5:
        raise RuntimeError("Gemini devolveu nota visual inválida")
    return {"score": float(score), "subject": str(result.get("subject", ""))[:90],
            "reason": str(result.get("reason", ""))[:160]}


def _choose(studio, pid, project, index, shot, pool, length,
            counts, checked, folder, use_vision, budget):
    import studio_dynamic_montage as montage
    last = counts.get("_last", "")
    ranked = []
    for asset in pool:
        key = _asset_id(asset)
        if not key or counts.get(key, 0) >= MAX_REPEATS:
            continue
        if asset.get("media_kind") != "video":
            continue
        meta = montage.candidate_score(shot, asset, 0)
        own = key == _asset_id(montage.asset_from_scene(project["scenes"][index]))
        if meta < 5 and not own:
            continue
        ranked.append((meta + (6 if own else 0) - counts.get(key, 0) * 8
                       - (25 if last == key else 0), asset))
    ranked.sort(key=lambda pair: -pair[0])
    # Samples are only considered real coverage after ffprobe validates length.
    for score, asset in ranked[:8]:
        key = _asset_id(asset)
        try:
            local = _clip(studio, pid, project["scenes"][index], asset, index)
            if _seconds(studio, local) + .03 < length:
                continue
            verdict = checked.get((index, key))
            if use_vision and verdict is None and budget["used"] < MAX_VISION_CALLS:
                verdict = _vision(studio, local, shot["scene_excerpt"],
                                  folder, budget)
                checked[index, key] = verdict
            if use_vision and verdict and verdict["score"] < 3:
                continue
            return asset, local, verdict, score
        except (OSError, RuntimeError, subprocess.TimeoutExpired, httpx.HTTPError,
                ValueError, KeyError, IndexError) as exc:
            print("[editor-v3] media candidate skipped: "
                  + type(exc).__name__, flush=True)
            continue
    raise RuntimeError(
        f"Cena {index + 1}: não encontrei clipes relevantes e não repetidos. "
        "Prévia bloqueada em vez de criar preenchimento artificial.")


def _plan(studio, pid, project, seconds, use_vision, folder):
    import studio_dynamic_montage as montage
    import studio_semantic_media as semantic
    scenes, actual = _pick_scenes(project, seconds)
    p = dict(project)
    p["scenes"] = scenes
    story, source = semantic.load_plan(studio, p, allow_ai=False, write=False)
    lengths = []
    elapsed = 0
    for scene in scenes:
        # Opening accelerates; body varies with the narration's punctuation.
        intensity = sum(str(scene.get("text", "")).count(c) for c in "?!")
        pace = (3.4 if elapsed < 20 else 4.6) - min(.6, intensity * .15)
        cut_count = max(1, math.ceil(scene["duration"] / pace))
        lengths.append([round(scene["duration"] / cut_count, 3)] * cut_count)
        lengths[-1][-1] = round(scene["duration"] -
                                sum(lengths[-1][:-1]), 3)
        elapsed += scene["duration"]
    counts = {"_last": ""}
    budget = {"used": 0}
    checked = {}
    timeline = []
    missing_verification = 0
    for index, (scene, seq) in enumerate(zip(scenes, lengths)):
        shot = story["scenes"][index]
        pool = montage.source_pool(studio, p, index, shot)
        if not pool:
            raise RuntimeError(f"Cena {index+1} sem material apropriado")
        parts = []
        for j, length in enumerate(seq):
            asset, local, verdict, meta = _choose(
                studio, pid, p, index, shot, pool, length,
                counts, checked, folder, use_vision, budget)
            key = _asset_id(asset)
            counts[key] = counts.get(key, 0) + 1
            counts["_last"] = key
            if use_vision and verdict is None:
                missing_verification += 1
            parts.append({
                "seconds": length, "asset": asset, "path": str(local),
                "visual_match": verdict, "metadata_score": meta,
                "style": ["subtle_pan", "close_detail", "wide"][j % 3],
                "editorial_intent": shot.get("visual_intent") or "context"})
        timeline.append({"narration": str(scene.get("text") or "")[:200],
                         "seconds": scene["duration"], "shots": parts})
    return p, {
        "version": VERSION, "duration_seconds": round(actual, 3),
        "storyboard_source": source, "vision_calls": budget["used"],
        "unverified_shots": missing_verification, "timeline": timeline}


def _qa(plan, use_vision):
    shots = [s for item in plan["timeline"] for s in item["shots"]]
    total = sum(s["seconds"] for s in shots)
    unique = set(_asset_id(s["asset"]) for s in shots)
    originals = [s for s in shots if s["asset"].get("media_provider") == "original"]
    long_shots = [s for s in shots if s["seconds"] > 6.5]
    verified = [s for s in shots if (s.get("visual_match") or {}).get("score", 0) >= 3]
    errors = []
    if len(shots) < math.ceil(plan["duration_seconds"] / 6):
        errors.append("Pouca variedade de cortes para a duração solicitada")
    if len(unique) < max(5, math.ceil(len(shots) / 3)):
        errors.append("Poucas fontes diferentes; repetição excessiva")
    if originals:
        errors.append("O piloto deve usar filmagens reais, não cartões gráficos")
    if long_shots:
        errors.append("Há tomadas longas demais")
    if abs(total - plan["duration_seconds"]) > .4:
        errors.append("Cortes não acompanham a duração da narração")
    if use_vision and len(verified) < math.ceil(len(shots) * .6):
        errors.append("Amostragem visual insuficiente para aprovar as cenas")
    return {"approved": not errors, "errors": errors,
            "shots": len(shots), "distinct_sources": len(unique),
            "average_shot_seconds": round(total / max(1, len(shots)), 2),
            "verified_shots": len(verified),
            "original_graphic_shots": len(originals)}


def _shot_render(studio, shot, pos, dest):
    src = shot["path"]
    duration = float(shot["seconds"])
    media_seconds = _seconds(studio, src)
    if media_seconds < duration - .03:
        raise RuntimeError("Mídia curta; repetição silenciosa bloqueada")
    offset = min(max(0., media_seconds - duration - .2), pos * 1.7 % max(1., media_seconds))
    # Overscale 6-9% before moving the crop: the legacy scale/scale cancelled
    # its zoom. This expression actually moves the crop every frame.
    margin = (1.07 if shot["style"] == "wide" else 1.12)
    w = math.ceil(1280 * margin / 2) * 2
    h = math.ceil(720 * margin / 2) * 2
    xf = "20*sin(n/33)" if pos % 2 else "-20*sin(n/29)"
    yf = "12*sin(n/40)" if pos % 2 else "-12*sin(n/34)"
    vf = (
        f"scale={w}:{h}:force_original_aspect_ratio=increase,"
        f"crop=1280:720:x='(in_w-out_w)/2+{xf}':"
        f"y='(in_h-out_h)/2+{yf}',"
        "fps=24,setsar=1,eq=contrast=1.035:saturation=1.015,"
        "unsharp=5:5:0.22:5:5:0.0,format=yuv420p")
    temp = dest.with_suffix(".partial.mp4")
    try:
        _run(studio, ["-ss", f"{offset:.3f}", "-i", src,
                      "-t", f"{duration:.3f}", "-vf", vf, "-an",
                      "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                      "-pix_fmt", "yuv420p", "-movflags", "+faststart",
                      str(temp)], timeout=max(100, int(duration * 24)))
        if _seconds(studio, temp) < duration - .23:
            raise RuntimeError("Corte exportado incompleto")
        temp.replace(dest)
    finally:
        temp.unlink(missing_ok=True)


def _concat_file(items, path):
    path.write_text("".join(
        "file '" + str(x.resolve()).replace("'", "'\\''") + "'\n" for x in items),
        encoding="utf-8")


def _title_ass(path, title, seconds):
    """Small documentary title and unobtrusive chapter labels, not full cards."""
    def esc(x):
        return re.sub(r"[\r\n{}]", " ", str(x)).replace("\\", "")
    heading = esc(title)[:62]
    data = (
        "[Script Info]\nScriptType: v4.00+\nPlayResX: 1280\nPlayResY: 720\n"
        "[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, "
        "SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, "
        "StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, "
        "Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        "Style: Title,DejaVu Sans,42,&H00FFFFFF,&H00FFFFFF,&H00121D27,"
        "&H99000000,-1,0,0,0,100,100,0,0,1,2,1,7,70,70,100,1\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, "
        "MarginR, MarginV, Effect, Text\n"
        f"Dialogue: 0,0:00:00.40,0:00:03.80,Title,,0,0,0,,{heading}\n")
    path.write_text(data, encoding="utf-8")


def _render(studio, pid, project, p, plan, folder):
    import studio_longform as longform
    _state(pid, status="rendering", progress=5)
    fragments = []
    for i, shot in enumerate(s for entry in plan["timeline"] for s in entry["shots"]):
        if _free(studio) < 220:
            raise RuntimeError("Disco baixo durante render; mantendo original intocado")
        dest = folder / f"shot_{i:03}.mp4"
        _shot_render(studio, shot, i, dest)
        fragments.append(dest)
        _state(pid, progress=5 + round(70 * (i+1) / len([
            s for e in plan["timeline"] for s in e["shots"]])))
    cuts_txt = folder / "cut_list.txt"
    _concat_file(fragments, cuts_txt)
    silent = folder / "picture.mp4"
    _run(studio, ["-f", "concat", "-safe", "0", "-i", str(cuts_txt),
                  "-c", "copy", "-movflags", "+faststart", str(silent)],
         timeout=300)
    voice = studio.safe_local_media(project.get("narration") or "")
    if voice and _seconds(studio, voice) < plan["duration_seconds"] - 1.3:
        raise RuntimeError("Áudio original insuficiente para 90 segundos")
    if not voice:
        raise RuntimeError("Narração original não encontrada")
    title = folder / "titles.ass"
    _title_ass(title, project.get("title") or "Estúdio IA", plan["duration_seconds"])
    ass = str(title.resolve()).replace("\\", "/").replace(":", r"\:").replace("'", r"\'")
    # Reuse the actual subtitling chain instead of forcing a title-only edit.
    caption_ass, _ = studio.subtitles_for(p, folder, 1280, 720)
    captions = str(Path(caption_ass).resolve()).replace("\\", "/").replace(":", r"\:").replace("'", r"\'")
    args = ["-i", str(silent), "-i", str(voice)]
    has_music = bool(project.get("soundtrack"))
    if has_music:
        music = studio.safe_local_media(project["soundtrack"])
        if music:
            args += ["-stream_loop", "-1", "-i", str(music)]
        else:
            has_music = False
    args += ["-vf", f"ass={ass},ass={captions}"]
    if has_music:
        args += ["-filter_complex", longform.AUDIO_MIX_FILTER,
                 "-map", "0:v:0", "-map", "[aout]"]
    else:
        args += ["-map", "0:v:0", "-map", "1:a:0"]
    temp = folder / "final.partial.mp4"
    final = folder / "quality_v3_preview.mp4"
    try:
        _run(studio, args + [
            "-t", f"{plan['duration_seconds']:.3f}",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-c:a", "aac", "-b:a", "160k", "-pix_fmt", "yuv420p",
            "-movflags", "+faststart", str(temp)], timeout=1100)
        if _seconds(studio, temp) < plan["duration_seconds"] - 1.3:
            raise RuntimeError("Render final incompleto")
        temp.replace(final)
    finally:
        temp.unlink(missing_ok=True)
    return final


def _do(studio, pid, request):
    try:
        if not RENDER_LOCK.acquire(blocking=False):
            raise RuntimeError("Outro piloto V3 está renderizando")
        try:
            project = studio.get_project(pid)
            if project.get("status") != "concluído":
                raise RuntimeError("Selecione um projeto já concluído para reutilizar a narração")
            if _free(studio) < MIN_FREE_MB:
                raise RuntimeError("É preciso no mínimo 850 MB livres no volume")
            folder = _folder(studio, pid)
            folder.mkdir(parents=True, exist_ok=True)
            p, plan = _plan(studio, pid, project, request.duration_seconds,
                            request.use_vision, folder)
            qa = _qa(plan, request.use_vision)
            (folder / "editorial_plan.json").write_text(
                json.dumps({"plan": plan, "quality": qa, "vision_requested":
                            request.use_vision}, ensure_ascii=False, indent=2),
                encoding="utf-8")
            if not qa["approved"]:
                raise RuntimeError("Revisão de qualidade: " + "; ".join(qa["errors"]))
            file = _render(studio, pid, project, p, plan, folder)
            _state(pid, status="completed", progress=100, quality=qa,
                   video_url=f"/api/editor-v3/projects/{pid}/video",
                   file_bytes=file.stat().st_size)
        finally:
            RENDER_LOCK.release()
    except Exception as exc:
        _state(pid, status="failed", error=type(exc).__name__ + ": " + str(exc)[:500],
               progress=0)
        print("[editor-v3] Preview failed: " + type(exc).__name__
              + " " + str(exc)[:240], flush=True)


PAGE = """<!DOCTYPE html><html lang="pt-BR"><meta charset="utf-8">
<title>Editor V3 · Piloto de Qualidade</title><meta name="viewport"
content="width=device-width,initial-scale=1">
<style>body{background:#0b1320;color:#eaf1f9;font:16px system-ui;max-width:850px;
margin:30px auto;padding:15px}section{background:#18263a;border-radius:14px;padding:22px}
button,select{padding:12px;margin:8px;border-radius:8px;font:inherit}
button{background:#4ce1b2;border:none;cursor:pointer}video{max-width:100%}
small{color:#bac7dc}</style><h1>Editor V3 · Piloto de 90 segundos</h1>
<section><p>Seleciona cenas de um vídeo concluído, valida a coerência,
recorta dinamicamente, cria título e mistura a voz original. Não publica.</p>
<select id="p"></select><label><input id="vision" type="checkbox">
Analisar quadros com Gemini (até 16 chamadas cobradas pela sua API)</label>
<p><button id="go">Produzir prévia</button></p>
<pre id="status"></pre><div id="out"></div>
<small>Vídeos existentes e canais não são alterados. Uma prévia que não passa
nos critérios é bloqueada em vez de receber cenas genéricas.</small></section>
<script>
let pid='';const p=document.getElementById('p'),st=document.getElementById('status'),
out=document.getElementById('out');
async function req(url,opt){let r=await fetch(url,opt);let x=await r.json();
if(!r.ok)throw Error(x.detail||JSON.stringify(x));return x}
async function init(){try{const a=await req('/api/projects');for(const i of a)
if(i.status==='concluído'){let o=document.createElement('option');o.value=i.id;
o.textContent=i.title;p.append(o)}}catch(e){st.textContent=e.message}}
async function poll(){pid=p.value;if(!pid)return;try{let a=await req(
'/api/editor-v3/projects/'+encodeURIComponent(pid)+'/status');
st.textContent=JSON.stringify(a,null,2);out.replaceChildren();
if(a.video_url){let v=document.createElement('video');v.controls=true;
v.src=a.video_url;out.append(v)}if(a.status==='rendering'||a.status==='planning')
setTimeout(poll,4500)}catch(e){st.textContent=e.message}}
document.getElementById('go').onclick=async()=>{
pid=p.value;if(!pid)return;out.replaceChildren();
try{await req('/api/editor-v3/projects/'+encodeURIComponent(pid)+'/preview',{
method:'POST',headers:{'Content-Type':'application/json'},
body:JSON.stringify({duration_seconds:90,use_vision:document.getElementById('vision').checked})
});poll()}catch(e){st.textContent=e.message}};init();
</script></html>"""


def install(studio):
    @studio.app.get("/editor-v3")
    def editor_page():
        return HTMLResponse(PAGE)

    @studio.app.post("/api/editor-v3/projects/{pid}/preview")
    def preview(pid: str, request: PreviewRequest):
        project = studio.get_project(pid)
        if project.get("status") != "concluído":
            raise HTTPException(409, "Selecione projeto concluído")
        with STATE_LOCK:
            ongoing = STATE.get(pid, {}).get("status") in ("planning", "rendering")
            if ongoing:
                raise HTTPException(409, "Piloto já em processamento")
            STATE[pid] = {"status": "planning", "progress": 0, "started": _stamp(),
                          "vision_requested": request.use_vision}
        threading.Thread(target=_do, args=(studio, pid, request),
                         daemon=True, name="editor-v3-pilot").start()
        return {"status": "planning", "project_id": pid,
                "monitor": f"/api/editor-v3/projects/{pid}/status"}

    @studio.app.get("/api/editor-v3/projects/{pid}/status")
    def preview_status(pid: str):
        studio.get_project(pid)
        with STATE_LOCK:
            result = dict(STATE.get(pid, {"status": "not_started"}))
        if not result.get("video_url") and (
                _folder(studio, pid) / "quality_v3_preview.mp4").is_file():
            result["video_url"] = f"/api/editor-v3/projects/{pid}/video"
            result["status"] = "completed"
        return result

    @studio.app.get("/api/editor-v3/projects/{pid}/video")
    def preview_video(pid: str):
        studio.get_project(pid)
        file = _folder(studio, pid) / "quality_v3_preview.mp4"
        if not file.is_file():
            raise HTTPException(404, "Prévia não disponível")
        return FileResponse(file, media_type="video/mp4",
                            filename="estudio_editor_v3_preview.mp4")


def self_test():
    scenes = [{"duration": 22.5, "text": "Cena A"},
              {"duration": 80, "text": "Cena B"}]
    picked, seconds = _pick_scenes({"scenes": scenes}, 90)
    assert len(picked) == 2 and abs(seconds - 90) < .01
    assert picked[1]["duration"] == 67.5
    shot = {"seconds": 90/23, "asset": {"media_url": "a",
            "media_provider": "pixabay"}, "visual_match": {"score": 4}}
    plan = {"duration_seconds": 90, "timeline": [
        {"shots": [{**shot, "asset": {**shot["asset"], "media_url": str(i//2)}}
                   for i in range(23)]}]}
    qa = _qa(plan, False)
    assert qa["approved"] and qa["shots"] == 23, qa
    assert MAX_VISION_CALLS <= 16
    print("[editor-v3] self-test PASS scene-trim, dynamic-coverage, QA", flush=True)


if __name__ == "__main__":
    self_test()
    app = Path("/app/app.py")
    if not app.is_file():
        raise SystemExit("Missing /app/app.py")
    code = app.read_text(encoding="utf-8")
    marker = "\nimport studio_editor_v3_preview as _editor_v3\n"
    if marker not in code:
        app.write_text(code + marker +
                       '_editor_v3.install(__import__("sys").modules[__name__])\n',
                       encoding="utf-8")
