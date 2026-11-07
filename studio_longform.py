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


def clip_key(scene: dict, width: int, height: int) -> str:
    material = {name: scene.get(name) for name in (
        "id", "media_url", "media_local", "media_kind", "media_provider", "duration", "text")}
    material.update(width=width, height=height, version="longform-v1")
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
        preset = os.getenv("STUDIO_LONGFORM_PRESET", "veryfast")
        if preset not in ("ultrafast", "superfast", "veryfast", "faster", "fast"):
            preset = "veryfast"
        for index, scene in enumerate(scenes):
            _check_free(studio, 160)
            duration = max(1.0, float(scene["duration"]))
            clip = intermediates / f"scene_{index:03d}.mp4"
            checkpoint = intermediates / f"scene_{index:03d}.sha256"
            digest = clip_key(scene, width, height)
            valid = (checkpoint.is_file() and checkpoint.read_text().strip() == digest
                     and _duration(studio, clip) >= duration - 0.4)
            if not valid:
                checkpoint.unlink(missing_ok=True)
                media = studio.fetch_clip(scene, pid)
                if media is None:
                    raise RuntimeError(f"Cena {index + 1}: mídia indisponível. "
                                       "A montagem longa exige fontes visuais selecionadas.")
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
            args += ["-filter_complex", "[1:a]volume=1[a0];[2:a]volume=0.10[a1];"
                     "[a0][a1]amix=inputs=2:duration=first[aout]", "-map", "0:v:0",
                     "-map", "[aout]"]
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

    @studio.app.get("/api/factory/chapters/{pid}")
    def chapter_plan(pid: str):
        return describe(studio, pid)


if __name__ == "__main__":
    target = Path("/app/app.py")
    if not target.is_file():
        raise SystemExit("/app/app.py missing")
    marker = "\nimport studio_longform as _longform\n"
    current = target.read_text(encoding="utf-8")
    if marker not in current:
        target.write_text(current + marker + '_longform.register(__import__("sys").modules[__name__])\n',
                          encoding="utf-8")