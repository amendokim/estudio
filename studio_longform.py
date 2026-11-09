"""Resumable, disk-aware long-form FFmpeg renderer for Meu Estúdio IA.

Opt-in activation only for projects above 5 minutes. Uses persistent, atomic
per-scene checkpoints and a single final encoding pass, avoiding joined.mp4.
Never bypasses the factory's storage and duration checks. No external AI API.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from fastapi import HTTPException

MIN_LONG_MINUTES = 6
MIN_VIDEO_RATIO = 0.8

AUDIO_MIX_FILTER = (
    "[1:a]aformat=sample_rates=48000:channel_layouts=stereo,"
    "volume=1,asplit=2[voice_side][voice_mix];"
    "[2:a]aformat=sample_rates=48000:channel_layouts=stereo,"
    "volume=0.17[bed];"
    "[bed][voice_side]sidechaincompress=threshold=0.025:"
    "ratio=6:attack=35:release=650[duck];"
    "[voice_mix][duck]amix=inputs=2:duration=first:"
    "dropout_transition=0:normalize=0[aout]"
)


def clip_key(scene: dict, width: int, height: int) -> str:
    material = {name: scene.get(name) for name in (
        "id", "media_url", "media_local", "media_kind", "media_provider", "duration", "text")}
    material.update(width=width, height=height, version="longform-v2-dynamic-editor")
    return hashlib.sha256(json.dumps(material, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def chapters(project: dict, chapter_minutes: int = 5) -> list[dict]:
    """Map a project to five-minute editing chapters without altering scenes."""
    chapter_minutes = max(1, min(int(chapter_minutes), 15))
    target = chapter_minutes * 60
    result: list[dict] = []
    offset = 0.0
    chapter_start = 0.0
    scene_start = 0
    for index, scene in enumerate(project.get("scenes") or []):
        duration = max(1.0, float(scene.get("duration") or 8))
        if offset > chapter_start and offset + duration - chapter_start > target and index > scene_start:
            result.append({"number": len(result) + 1, "start_seconds": round(chapter_start, 2),
                           "end_seconds": round(offset, 2), "scene_start": scene_start + 1,
                           "scene_end": index})
            chapter_start, scene_start = offset, index
        offset += duration
    if scene_start < len(project.get("scenes") or []):
        result.append({"number": len(result) + 1, "start_seconds": round(chapter_start, 2),
                       "end_seconds": round(offset, 2), "scene_start": scene_start + 1,
                       "scene_end": len(project["scenes"])})
    return result


def _run(studio: Any, args: list[str], timeout: float = 3600):
    program = studio.FFMPEG
    if not program:
        raise RuntimeError("FFmpeg indisponível")
    cp = subprocess.run([program, "-hide_banner", "-loglevel", "error", "-nostdin", "-y", *args],
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                        timeout=timeout, check=False)
    if cp.returncode:
        raise RuntimeError("FFmpeg: " + cp.stderr[-1500:])


def _duration(studio: Any, path: Path) -> float:
    if not path.is_file() or path.stat().st_size < 200:
        return 0.0
    cp = subprocess.run([studio.FFPROBE, "-v", "error", "-show_entries", "format=duration",
                         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=35)
    if cp.returncode:
        return 0.0
    try:
        return float(cp.stdout.strip())
    except ValueError:
        return 0.0


def _check_free(studio: Any, min_mb: int = 250):
    free_mb = shutil.disk_usage(studio.DATA).free // (1024 * 1024)
    if free_mb < min_mb:
        raise RuntimeError(f"Espaço insuficiente durante a montagem ({free_mb} MB livres, "
                           f"mínimo {min_mb} MB). Os segmentos existentes foram preservados para retomada.")


def _atomic_encode(studio: Any, args: list[str], path: Path, timeout: float):
    temp = path.with_name(path.stem + ".partial" + path.suffix)
    temp.unlink(missing_ok=True)
    try:
        _run(studio, [*args, str(temp)], timeout=timeout)
        if _duration(studio, temp) < 0.5:
            raise RuntimeError("Arquivo parcial FFmpeg inválido")
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def _check_source_ratio(p: dict):
    scenes = p.get("scenes") or []
    total = sum(max(1., float(s.get("duration") or 8)) for s in scenes)
    video = sum(max(1., float(s.get("duration") or 8)) for s in scenes
                if s.get("media_kind") == "video" and
                (s.get("media_url") or s.get("media_local")))
    ratio = video / total if total else 0
    if ratio < MIN_VIDEO_RATIO:
        raise RuntimeError(f"Apenas {ratio:.1%} de vídeo selecionado; mínimo "
                           "80%. Complete as mídias reais e licenciadas antes de renderizar.")
    return ratio


def render(studio: Any, pid: str):
    """Synchronous entrypoint; queue worker/restart handles retries.

    Existing short-form renderer remains unchanged. Long-form rendering does not
    automatically call a paid API or publish videos.
    """
    try:
        p = studio.get_project(pid)
        scenes = p.get("scenes") or []
        if not scenes:
            raise RuntimeError("O projeto não contém cenas")
        # Long-form documentary percentages are verified against FINAL microtakes,
        # not the larger narration scenes. The YouTube70 module audits 70/30 below.
        import studio_youtube_70_documentary as doc70
        documentary_microshots=(doc70.is_documentary(p) and
            os.getenv("STUDIO_YOUTUBE70_STRICT","1")=="1" and
            os.getenv("STUDIO_LONGFORM_DYNAMIC_EDIT","1")=="1")
        if not documentary_microshots:
            _check_source_ratio(p)
        if not p.get("narration"):
            raise RuntimeError("Narração ausente; gere a narração antes da montagem")
        _check_free(studio)
        width, height = (1280, 720) if p["ratio"] == "16:9" else (720, 1280)
        out = studio.project_dir(pid) / "export"
        out.mkdir(parents=True, exist_ok=True)
        intermediates = out / "longform_segments"
        intermediates.mkdir(parents=True, exist_ok=True)
        completed: list[Path] = []
        credits: list[str] = []
        cinematic = os.getenv("STUDIO_LONGFORM_DYNAMIC_EDIT", "1") == "1"
        montage = None
        if cinematic:
            import studio_dynamic_montage as dynamic
            montage = dynamic.build_plan(studio, p)
            if documentary_microshots:
                montage = doc70.enrich_montage(studio, p, montage)
            (out / "montage_plan_v2.json").write_text(
                json.dumps(montage,indent=2,ensure_ascii=False),encoding="utf-8")
            print(f"[longform-editor] planned {sum(len(x['shots']) for x in montage['timeline'])} distinct-paced cuts across {len(scenes)} scenes; searches={montage['source_searches']}",flush=True)
        preset = os.getenv("STUDIO_LONGFORM_PRESET", "veryfast")
        if preset not in ("ultrafast", "superfast", "veryfast", "faster", "fast"):
            preset = "veryfast"
        for index, scene in enumerate(scenes):
            _check_free(studio, 160)
            duration = max(1.0, float(scene["duration"]))
            clip = intermediates / f"scene_{index:03d}.mp4"
            checkpoint = intermediates / f"scene_{index:03d}.sha256"
            digest = clip_key(scene, width, height)
            if montage:
                digest = hashlib.sha256((digest + json.dumps(
                    montage["timeline"][index],sort_keys=True,ensure_ascii=False)).encode()).hexdigest()
            valid = (checkpoint.is_file() and checkpoint.read_text().strip() == digest
                     and _duration(studio, clip) >= duration - 0.4)
            if not valid:
                checkpoint.unlink(missing_ok=True)
                if montage:
                    sources = dynamic.make_scene(studio,pid,p,index,
                        montage["timeline"][index],intermediates,clip,preset)
                    credits.extend(
                        f"Cena {x['scene']} / corte {x['shot']} | {x['provider']} | "
                        f"{x['author']} | {x['url']}" for x in sources)
                else:
                    media = studio.fetch_clip(scene, pid)
                    if media is None:
                        raise RuntimeError(f"Cena {index + 1}: mídia indisponível")
                    input_args = (["-stream_loop", "-1", "-i", str(media)] if
                                  scene["media_kind"] == "video" else
                                  ["-loop", "1", "-framerate", "24", "-i", str(media)])
                    filt = (f"scale={width}:{height}:force_original_aspect_ratio=increase,"
                            f"crop={width}:{height},setsar=1,fps=24,format=yuv420p")
                    _atomic_encode(studio, [*input_args, "-t", f"{duration:.3f}",
                                            "-vf", filt, "-an", "-c:v", "libx264",
                                            "-preset", preset, "-crf", "26", "-pix_fmt", "yuv420p",
                                            "-r", "24"], clip,
                                   timeout=max(300, duration * 20))
                checkpoint.write_text(digest)
            completed.append(clip)
            if scene.get("source_page"):
                credits.append(f"Cena {index+1} | {scene.get('media_provider', '')} | "
                               f"{scene.get('author', '')} | {scene.get('source_page', '')}")
            studio.update_project(pid, status="renderizando", progress=5 + int(78 * (index + 1) / len(scenes)))
        concat = intermediates / "clips.txt"
        concat.write_text("".join("file '" + str(clip.resolve()).replace("'", "'\\''") + "'\n"
                                  for clip in completed), encoding="utf-8")
        _, srt = studio.subtitles_for(p, out, width, height)
        ass_path = str((out / "legendas.ass").resolve()).replace("\\", "/").replace(":", r"\:").replace("'", r"\'")
        narration = studio.safe_local_media(p["narration"])
        args = ["-f", "concat", "-safe", "0", "-i", str(concat), "-i", str(narration)]
        if p.get("soundtrack"):
            music = studio.safe_local_media(p["soundtrack"])
            args += ["-stream_loop", "-1", "-i", str(music)]
        args += ["-vf", f"ass={ass_path}", "-c:v", "libx264", "-preset", preset,
                 "-crf", "26", "-pix_fmt", "yuv420p"]
        if p.get("soundtrack"):
            # Voice-first mix: soft background bed automatically ducks under
            # spoken narration and recovers gently through natural pauses.
            # FFmpeg-only; no paid mixing services or outside music API.
            args += ["-filter_complex", AUDIO_MIX_FILTER,
                     "-map", "0:v:0","-map","[aout]"]
        else:
            args += ["-map", "0:v:0", "-map", "1:a:0"]
        args += ["-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart"]
        final = out / "video_final.mp4"
        _atomic_encode(studio, args, final, timeout=max(3600, sum(float(s["duration"]) for s in scenes) * 6))
        actual = _duration(studio, final)
        expected = sum(float(s["duration"]) for s in scenes)
        if actual < expected * 0.95:
            final.unlink(missing_ok=True)
            raise RuntimeError(f"Duração do vídeo curta: {actual:.1f}s de {expected:.1f}s esperados")
        (out / "fontes_midia.txt").write_text("\n".join(credits) or "Nenhuma fonte informada", encoding="utf-8")
        (out / "projeto.json").write_text(json.dumps(p, indent=2, ensure_ascii=False), encoding="utf-8")
        studio.update_project(pid, status="concluído", progress=100, error="")
        if os.getenv("STUDIO_LONGFORM_KEEP_SEGMENTS", "0") != "1":
            shutil.rmtree(intermediates, ignore_errors=True)
        print(f"[longform] complete: {pid}, {actual:.1f}s, {len(scenes)} scenes", flush=True)
    except Exception as exc:
        studio.update_project(pid, status="erro", error=str(exc)[:1000], progress=0)
        print(f"[longform] failed: {pid}, {str(exc)[:300]}", flush=True)



def narration_key(scene: dict, voice: str) -> str:
    return hashlib.sha256((voice + "\n" + str(scene.get('text') or '')).encode()).hexdigest()


def narrate(studio: Any, pid: str):
    """Resumable TTS using per-scene audio + integrity markers.

    Only used on long projects and never calls a paid text-generation API.
    The Edge TTS backend is external, so availability and terms can vary.
    """
    try:
        import edge_tts
        p = studio.get_project(pid)
        scenes = p['scenes']
        if not scenes:
            raise RuntimeError('Roteiro sem cenas para narração')
        _check_free(studio, 120)
        voice = p['voice']
        folder = studio.project_dir(pid) / 'audio'
        folder.mkdir(parents=True, exist_ok=True)
        completed = []

        async def run():
            for i, scene in enumerate(scenes):
                path = folder / f'narration_{i:03d}.mp3'
                marker = folder / f'narration_{i:03d}.sha256'
                digest = narration_key(scene, voice)
                duration = (_duration(studio, path) if marker.is_file() and
                            marker.read_text().strip() == digest else 0.)
                if duration < 0.3:
                    marker.unlink(missing_ok=True)
                    partial = path.with_name(path.stem + '.partial' + path.suffix)
                    for attempt in range(3):
                        try:
                            partial.unlink(missing_ok=True)
                            await edge_tts.Communicate(scene['text'], voice=voice).save(str(partial))
                            duration = _duration(studio, partial)
                            if duration < 0.3:
                                raise RuntimeError('TTS gerou áudio vazio')
                            partial.replace(path)
                            marker.write_text(digest)
                            break
                        except Exception:
                            partial.unlink(missing_ok=True)
                            if attempt == 2:
                                raise
                            await asyncio.sleep(2 ** attempt)
                completed.append(path)
                with studio.db() as conn:
                    conn.execute('UPDATE scenes SET duration=? WHERE id=? AND project_id=?',
                                 (max(1., duration), scene['id'], pid))
                studio.update_project(pid, status='gerando narração',
                                      progress=10 + int(70 * (i+1)/len(scenes)))

        asyncio.run(run())
        files = folder / 'narration_longform.txt'
        files.write_text(''.join("file '" + str(path.resolve()).replace("'", "'\\''") + "'\n"
                                 for path in completed), encoding='utf-8')
        output = folder / 'narration_full.mp3'
        _atomic_encode(studio, ['-f','concat','-safe','0','-i',str(files), '-c:a',
                                'libmp3lame','-b:a','160k'], output,
                       timeout=max(300,len(scenes)*20))
        studio.update_project(pid, narration=str(output.relative_to(studio.MEDIA)),
                              status='narração pronta', progress=85, error='')
        print(f'[longform] narration complete: {pid}, {len(completed)} scenes',flush=True)
    except Exception as exc:
        studio.update_project(pid,status='erro',error=str(exc)[:1000],progress=0)
        print(f'[longform] narration failed: {pid}, {str(exc)[:300]}',flush=True)


def describe(studio: Any, pid: str) -> dict:
    p = studio.get_project(pid)
    out = studio.project_dir(pid) / "export" / "longform_segments"
    expected = len(p["scenes"])
    valid = 0
    width, height = (1280, 720) if p["ratio"] == "16:9" else (720, 1280)
    for i, scene in enumerate(p["scenes"]):
        clip = out / f"scene_{i:03d}.mp4"
        marker = out / f"scene_{i:03d}.sha256"
        if marker.exists() and marker.read_text().strip() == clip_key(scene, width, height) and clip.is_file():
            valid += 1
    return {"project_id": pid, "chapter_plan": chapters(p), "scene_count": expected,
            "checkpointed_scenes": valid, "progress_percent": round(100 * valid / max(1, expected)),
            "expected_seconds": round(sum(float(s.get("duration") or 8) for s in p["scenes"]), 1),
            "renderer": "FFmpeg resumable v1", "status": p.get("status"),
            "note": "A renderização longa depende de worker e disco suficientes. "
                    "A função não chama APIs de IA pagas."}


def register(studio: Any):
    original = studio.render_task

    def render_with_checkpoints(pid: str):
        p = studio.get_project(pid)
        if int(p.get("duration_minutes") or 0) < MIN_LONG_MINUTES:
            return original(pid)
        # Prevent an inadvertent long render on the 500MB Railway instance.
        import studio_factory as factory
        capacity = factory.estimate(studio, p)
        if not capacity["can_render_here"]:
            studio.update_project(pid, status="erro", progress=0, error=capacity["reason"][:1000])
            return
        return render(studio, pid)

    studio.render_task = render_with_checkpoints

    original_tts = studio.tts_task

    def narration_with_checkpoints(pid: str):
        p = studio.get_project(pid)
        if int(p.get('duration_minutes') or 0) < MIN_LONG_MINUTES:
            return original_tts(pid)
        return narrate(studio, pid)

    studio.tts_task = narration_with_checkpoints

    original_autofill = studio._auto_find_media

    def fill_longform_media(pid: str, limit: int):
        p = studio.get_project(pid)
        if int(p.get('duration_minutes') or 0) < MIN_LONG_MINUTES:
            return original_autofill(pid, limit)
        budget = max(16, min(int(os.getenv('STUDIO_LONGFORM_MEDIA_SCAN_LIMIT','64')), 120))
        result = None
        previous_missing = len([x for x in p['scenes'] if not x.get('media_url') and
                                not x.get('media_local')])
        for _ in range((budget + 15) // 16):
            result = original_autofill(pid, 16)
            remaining = len([x for x in studio.get_project(pid)['scenes']
                             if not x.get('media_url') and not x.get('media_local')])
            if not remaining or remaining >= previous_missing:
                break
            previous_missing = remaining
        return result

    studio._auto_find_media = fill_longform_media

    original_autopilot = studio.autopilot_task

    def safe_autopilot(pid: str):
        p = studio.get_project(pid)
        if int(p.get('duration_minutes') or 0) >= MIN_LONG_MINUTES:
            import studio_factory as factory
            estimate = factory.estimate(studio, p)
            if not estimate['can_render_here']:
                studio.update_project(pid, status='erro', progress=0,
                                      error=estimate['reason'][:1000])
                return
        return original_autopilot(pid)

    studio.autopilot_task = safe_autopilot

    # The dashboard is kept in the factory extension; expose chapter progress here.
    import studio_factory as factory
    old_html = factory.DASHBOARD
    needle = "section.append(' ',inspect);"
    if "Ver capítulos" not in old_html and needle in old_html:
        extra = """section.append(' ',inspect);
const parts=document.createElement('button');parts.textContent='Ver capítulos';parts.onclick=async()=>{try{const info=await req('/api/factory/chapters/'+encodeURIComponent(p.id));alert(JSON.stringify(info,null,2))}catch(e){alert(e.message)}};section.append(' ',parts);"""
        factory.DASHBOARD = old_html.replace(needle, extra)

    @studio.app.get("/api/factory/chapters/{pid}")
    def chapter_plan(pid: str):
        return describe(studio, pid)


def _self_test_audio_mix():
    """Validate the precise FFmpeg audio graph during the Railway Docker build."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("FFmpeg missing during audio-mix self-test")
    cp = subprocess.run([
        ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin",
        "-f", "lavfi", "-i", "color=c=black:s=16x16:d=0.3",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=0.3",
        "-f", "lavfi", "-i", "sine=frequency=220:duration=0.3",
        "-filter_complex", AUDIO_MIX_FILTER, "-map", "[aout]",
        "-t", "0.25", "-f", "null", "-"
    ], capture_output=True, text=True, check=False, timeout=15)
    if cp.returncode:
        raise RuntimeError("FFmpeg audio mix self-test failed: " + cp.stderr[-800:])
    print("[longform] AUDIO MIX SELF-TEST PASS voice/duck stereo split", flush=True)


if __name__ == "__main__":
    _self_test_audio_mix()
    target = Path("/app/app.py")
    if not target.is_file():
        raise SystemExit("/app/app.py missing")
    marker = "\nimport studio_longform as _longform\n"
    current = target.read_text(encoding="utf-8")
    if marker not in current:
        target.write_text(current + marker + '_longform.register(__import__("sys").modules[__name__])\n',
                          encoding="utf-8")