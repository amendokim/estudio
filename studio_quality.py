"""Accessible, safe-area documentary captions and duration/aspect quality checks.

The addon keeps the existing cinematic titles, render hooks, media licenses,
MCP and authentication unchanged. It does not call a paid service, download
remote video or attempt to extend a short script with empty footage.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

CAPTION_MAX_WORDS = 6
CAPTION_MAX_CHARS = 35


def _segments(text: str) -> list[str]:
    """Short readable phrases; never split or drop words."""
    cleaned = re.sub(r"\s+", " ", text or "").strip()
    if not cleaned:
        return []
    words = cleaned.split()
    chunks = []
    group = []
    for word in words:
        proposed = " ".join(group + [word])
        if group and (len(group) >= CAPTION_MAX_WORDS or len(proposed) > CAPTION_MAX_CHARS):
            chunks.append(" ".join(group))
            group = []
        group.append(word)
    if group:
        chunks.append(" ".join(group))
    # Keep the last caption from flashing only one or two orphan words.
    if len(chunks) > 1 and len(chunks[-1].split()) <= 2:
        previous = chunks[-2].split()
        last = chunks[-1].split()
        while len(last) < 3 and len(previous) > 3:
            candidate = [previous[-1], *last]
            if len(" ".join(candidate)) > CAPTION_MAX_CHARS:
                break
            last = candidate
            previous.pop()
        chunks[-2] = " ".join(previous)
        chunks[-1] = " ".join(last)
    return chunks


def _ass_stamp(seconds: float) -> str:
    n = max(0, round(seconds * 100))
    hh, n = divmod(n, 360000)
    mm, n = divmod(n, 6000)
    ss, cs = divmod(n, 100)
    return f"{hh}:{mm:02d}:{ss:02d}.{cs:02d}"


def _srt_stamp(seconds: float) -> str:
    n = max(0, round(seconds * 1000))
    hh, n = divmod(n, 3600000)
    mm, n = divmod(n, 60000)
    ss, ms = divmod(n, 1000)
    return f"{hh:02d}:{mm:02d}:{ss:02d},{ms:03d}"


def _ass_escape(text: str) -> str:
    return text.replace('\\', r'\\').replace('{', r'\{').replace('}', r'\}')


def _caption_events(project: dict, width: int, height: int):
    t = 0.0
    ass_events = []
    srt_events = []
    index = 1
    for scene in project.get("scenes") or []:
        duration = max(1.0, float(scene.get("duration") or 1))
        chunks = _segments(str(scene.get("text") or ""))
        weight = sum(max(1, len(x.split())) for x in chunks)
        cursor = t
        for chunk in chunks:
            portion = duration * max(1, len(chunk.split())) / max(1, weight)
            start = cursor
            end = min(t + duration, cursor + portion)
            if end - start > .36:
                # Subtitles have their own style, so cinematic title overlays stay intact.
                ass_events.append(f"Dialogue: 0,{_ass_stamp(start)},{_ass_stamp(end)},Caption,,0,0,0,,{_ass_escape(chunk)}")
                srt_events.extend((str(index), f"{_srt_stamp(start)} --> {_srt_stamp(end)}", chunk, ""))
                index += 1
            cursor = end
        t += duration
    return ass_events, srt_events, t


def rewrite_subtitles(project: dict, ass: Path, srt: Path, width: int, height: int):
    existing = ass.read_text(encoding="utf-8")
    if '[Events]' not in existing:
        raise RuntimeError('Subtitle source has no ASS event section')
    before, after = existing.split('[Events]', 1)
    # Remove existing default captions only; keep overlay titles and credits.
    events = [line for line in after.splitlines()
              if not line.lstrip().startswith('Dialogue: 0,') and
              not line.lstrip().startswith('Dialogue: 0 ,')]
    font = max(22, min(33, round(height * (0.038 if width > height else 0.027))))
    horizontal = max(55, round(width * (0.095 if width > height else 0.085)))
    bottom = max(78, round(height * (0.115 if width > height else 0.075)))
    style = (f'Style: Caption,DejaVu Sans,{font},&H00FFFFFF,&H0000FFFF,'
             f'&H00202020,&H70000000,0,0,0,0,100,100,0,0,1,2,0,2,'
             f'{horizontal},{horizontal},{bottom},1\n')
    if 'Style: Caption,' not in before:
        before += style
    new_events, new_srt, _ = _caption_events(project, width, height)
    tmp_ass = ass.with_suffix('.ass.tmp')
    tmp_srt = srt.with_suffix('.srt.tmp')
    tmp_ass.write_text(before + '[Events]\n' + '\n'.join(events).rstrip() + '\n' +
                       '\n'.join(new_events) + '\n', encoding='utf-8')
    tmp_srt.write_text('\n'.join(new_srt) + '\n', encoding='utf-8')
    tmp_ass.replace(ass)
    tmp_srt.replace(srt)
    return {'captions': len(new_events), 'resolution': f'{width}x{height}',
            'font_px': font, 'margin_horizontal_px': horizontal, 'margin_bottom_px': bottom}


def _probe(studio, path: Path):
    if not path.is_file() or path.stat().st_size < 1000:
        return {}
    cp = subprocess.run([studio.FFPROBE, '-v', 'error', '-select_streams', 'v:0',
                         '-show_entries', 'stream=width,height:format=duration',
                         '-of', 'json', str(path)], capture_output=True,
                        text=True, timeout=15)
    if cp.returncode:
        return {}
    import json
    result = json.loads(cp.stdout)
    dimensions = (result.get('streams') or [{}])[0]
    return {'width': dimensions.get('width'), 'height': dimensions.get('height'),
            'duration_seconds': round(float((result.get('format') or {}).get('duration') or 0), 2)}


def project_qa(studio, pid: str):
    p = studio.get_project(pid)
    width, height = (1280, 720) if p.get('ratio') == '16:9' else (720, 1280)
    target = int(p.get('duration_minutes') or 0) * 60
    scenes = p.get('scenes') or []
    scene_seconds = sum(max(1, float(s.get('duration') or 1)) for s in scenes)
    path = studio.project_dir(pid) / 'export' / 'video_final.mp4'
    actual = _probe(studio, path)
    warnings = []
    if target and scene_seconds < target * .9:
        warnings.append('Roteiro/narração mais curto que a duração planejada; amplie o texto ou ajuste a duração declarada.')
    if actual and target and actual['duration_seconds'] < target * .9:
        warnings.append('MP4 final ficou significativamente mais curto que o objetivo.')
    if actual and actual.get('width') and actual.get('height'):
        ratio = actual['width'] / actual['height']
        expected = width / height
        if abs(ratio - expected) > .02:
            warnings.append('Proporção do arquivo MP4 não corresponde ao formato solicitado.')
    generic = sum(s.get('media_provider') in ('pixabay', 'pexels') for s in scenes)
    if generic:
        warnings.append(f'{generic} cena(s) usam bancos genéricos. Revisar coerência visual e identificar imagens ilustrativas.')
    return {'project_id': pid, 'target_seconds': target,
            'scene_timeline_seconds': round(scene_seconds, 2),
            'target_ratio': p.get('ratio'), 'intended_resolution': f'{width}x{height}',
            'file': actual or None, 'warnings': warnings,
            'paid_external_services_used_by_this_check': False}


def install(studio):
    original = studio.subtitles_for

    def quality_subtitles(project, output_dir, width, height):
        ass, srt = original(project, output_dir, width, height)
        details = rewrite_subtitles(project, ass, srt, width, height)
        print(f"[studio-quality] Caption safe area: {details}", flush=True)
        return ass, srt

    studio.subtitles_for = quality_subtitles

    @studio.app.get('/api/quality/projects/{pid}')
    def qa_route(pid: str):
        return project_qa(studio, pid)


if __name__ == '__main__':
    app_file = Path('/app/app.py')
    if not app_file.exists():
        raise SystemExit('Missing /app/app.py')
    mark = '\nimport studio_quality as _studio_quality\n'
    original = app_file.read_text(encoding='utf-8')
    if mark not in original:
        app_file.write_text(original + mark + '_studio_quality.install(__import__("sys").modules[__name__])\n', encoding='utf-8')