"""Word-timed Edge TTS captions and capacity-gated Full HD for Meu Estudio IA.

No paid speech APIs. Edge TTS event offsets are captured with audio bytes, and
subtitle generation replaces only dialogue captions, preserving title overlays.
Existing long-form renderer (>5 minutes) and legacy TTS remain as fallbacks.
"""
from __future__ import annotations
import asyncio
import hashlib
import json
import os
import shutil
from pathlib import Path

MAX_WORDS = 6
MAX_CHARS = 35
RESERVE_1080_BYTES = 2 * 1024**3

def _stamp(seconds, srt=False):
    unit = 1000 if srt else 100
    n = max(0, round(float(seconds) * unit))
    h, n = divmod(n, 3600 * unit)
    m, n = divmod(n, 60 * unit)
    sec, frac = divmod(n, unit)
    if srt:
        return f"{h:02d}:{m:02d}:{sec:02d},{frac:03d}"
    return f"{h}:{m:02d}:{sec:02d}.{frac:02d}"

def _fingerprint(project):
    content = {'voice': project.get('voice'),
               'scenes': [(x.get('id'), x.get('text')) for x in (project.get('scenes') or [])]}
    return hashlib.sha256(json.dumps(content, ensure_ascii=False, sort_keys=True).encode()).hexdigest()

def _groups(words):
    result, current = [], []
    for item in words:
        token = str(item.get('text') or '').strip()
        if not token:
            continue
        if current and (len(current) >= MAX_WORDS or
                        len(' '.join(x['text'] for x in current) + ' ' + token) > MAX_CHARS):
            result.append(current)
            current = []
        current.append({'text': token, 'start': item['start'], 'end': item['end']})
    if current:
        result.append(current)
    return result

def timed_captions(studio, project, ass, srt):
    path = studio.project_dir(project['id']) / 'audio' / 'word_timing.json'
    if not path.is_file() or path.stat().st_size > 1024*1024:
        return 0
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        if data.get('version') != 1 or data.get('fingerprint') != _fingerprint(project):
            return 0
        groups = _groups(data.get('words') or [])
        if len(groups) < 2:
            return 0
        existing = ass.read_text(encoding='utf-8')
        if '[Events]' not in existing or 'Style: Caption,' not in existing:
            return 0
        header, tail = existing.split('[Events]', 1)
        non_captions = [x for x in tail.splitlines() if not x.startswith('Dialogue: 0,')]
        events, srts = [], []
        for i, group in enumerate(groups):
            start = max(0, float(group[0]['start']))
            end = max(start + .3, float(group[-1]['end']) + .1)
            if i+1 < len(groups):
                end = min(end, max(start+.3, float(groups[i+1][0]['start'])-.02))
            phrase = ' '.join(x['text'] for x in group)
            escaped = phrase.replace('\\',r'\\').replace('{',r'\{').replace('}',r'\}')
            events.append('Dialogue: 0,'+_stamp(start)+','+_stamp(end)+
                          ',Caption,,0,0,0,,'+escaped)
            srts += [str(i+1),_stamp(start,True)+' --> '+_stamp(end,True),phrase,'']
        tempass, tempsrt = ass.with_suffix('.ass.new'),srt.with_suffix('.srt.new')
        tempass.write_text(header+'[Events]\n'+'\n'.join(non_captions+events)+'\n',encoding='utf-8')
        tempsrt.write_text('\n'.join(srts)+'\n',encoding='utf-8')
        tempass.replace(ass);tempsrt.replace(srt)
        return len(events)
    except (OSError, ValueError, KeyError, TypeError):
        return 0

def generate_timed_narration(studio, pid):
    import edge_tts
    project = studio.get_project(pid)
    scenes = project['scenes']
    if not scenes:
        raise RuntimeError('Nenhuma cena para narrar')
    folder = studio.project_dir(pid) / 'audio'
    folder.mkdir(parents=True, exist_ok=True)
    pieces, words = [], []

    async def synthesize():
        elapsed = 0.0
        for i, scene in enumerate(scenes):
            output = folder / f'narration_{i:03d}.mp3'
            partial = folder / f'narration_{i:03d}.partial.mp3'
            boundaries = []
            with partial.open('wb') as audio:
                async for event in edge_tts.Communicate(str(scene['text']),voice=project['voice'],boundary='WordBoundary').stream():
                    if event['type']=='audio':
                        audio.write(event['data'])
                    elif event['type']=='WordBoundary':
                        token = str(event.get('text') or '').strip()
                        if not token:
                            continue
                        start = max(0.,float(event.get('offset') or 0)/10_000_000)
                        length = max(0.,float(event.get('duration') or 0)/10_000_000)
                        boundaries.append((token,start,start+length))
            if partial.stat().st_size < 1000:
                raise RuntimeError('Edge TTS não produziu áudio válido')
            partial.replace(output)
            duration = max(1.,float(studio.probe_duration(output)))
            with studio.db() as conn:
                conn.execute('UPDATE scenes SET duration=? WHERE id=?',(duration,scene['id']))
            for token,start,end in boundaries:
                words.append({'text':token,'start':round(elapsed+min(start,duration),4),
                              'end':round(elapsed+min(end,duration),4)})
            elapsed += duration
            pieces.append(output)
            studio.update_project(pid,progress=40+int(40*(i+1)/len(scenes)))
    asyncio.run(synthesize())
    listing=folder/'narration_list.txt'
    listing.write_text(''.join("file '"+str(x.resolve()).replace("'","'\\''")+"'\n" for x in pieces),encoding='utf-8')
    combined=folder/'narration_full.mp3'
    tmp=folder/'narration_full.partial.mp3'
    studio.run_ffmpeg(['-f','concat','-safe','0','-i',str(listing),
                       '-c:a','libmp3lame','-b:a','160k',str(tmp)])
    tmp.replace(combined)
    fresh=studio.get_project(pid)
    report={'version':1,'fingerprint':_fingerprint(fresh),'words':words}
    temp=folder/'word_timing.json.tmp'
    temp.write_text(json.dumps(report,ensure_ascii=False),encoding='utf-8')
    temp.replace(folder/'word_timing.json')
    studio.update_project(pid,narration=str(combined.relative_to(studio.MEDIA)),
                          status='narração pronta',progress=85)
    print(f'[studio-edit] aligned narration: {len(words)} word events',flush=True)

def install(studio):
    old_tts=studio.tts_task
    old_subtitles=studio.subtitles_for
    old_render=studio.render_task

    def audited_render(pid):
        result = old_render(pid)
        p = studio.get_project(pid)
        if p.get('status') == 'concluído':
            try:
                import studio_quality
                q = studio_quality.project_qa(studio, pid)
                source = q.get('file') or {}
                print('[studio-edit] final duration=%.2fs, size=%sx%s, '
                      'word_timing=%s, sources=%s' %
                      (source.get('duration_seconds') or 0,
                       source.get('width') or '?', source.get('height') or '?',
                       (studio.project_dir(pid) / 'audio' / 'word_timing.json').is_file(),
                       q.get('source_mix_percent')), flush=True)
            except (OSError, ValueError, TypeError, KeyError) as exc:
                print('[studio-edit] QA report unavailable: ' + type(exc).__name__,flush=True)
        return result

    def narration(pid):
        project=studio.get_project(pid)
        if not (1 <= int(project.get('duration_minutes') or 0) <= 5):
            return old_tts(pid)
        try:
            return generate_timed_narration(studio,pid)
        except Exception as exc:
            print('[studio-edit] Edge timing unavailable, using fallback: '+
                  type(exc).__name__,flush=True)
            (studio.project_dir(pid)/'audio'/'word_timing.json').unlink(missing_ok=True)
            return old_tts(pid)

    def subtitles(project,out,width,height):
        ass,srt=old_subtitles(project,out,width,height)
        count=timed_captions(studio,project,ass,srt)
        if count:
            print(f'[studio-edit] {count} synchronized captions',flush=True)
        return ass,srt

    studio.tts_task=narration
    studio.subtitles_for=subtitles
    studio.render_task=audited_render

    @studio.app.get('/api/quality/render-readiness/{pid}')
    def render_readiness(pid: str):
        project=studio.get_project(pid)
        free=shutil.disk_usage(studio.DATA).free
        enabled=os.getenv('STUDIO_RENDER_1080P')=='1'
        hd=enabled and free>=RESERVE_1080_BYTES and int(project.get('duration_minutes') or 0)<=5
        manifest=studio.project_dir(pid)/'audio'/'word_timing.json'
        return {'project_id':pid,'word_timing_ready':manifest.is_file(),
                'full_hd_enabled':hd,'target_resolution': '1920x1080' if hd else 'preview',
                'disk_free_mb':round(free/1048576),
                'requires_more_storage':free<RESERVE_1080_BYTES,
                'longform_checkpoint_renderer':int(project.get('duration_minutes') or 0)>5,
                'note':'1080p requer STUDIO_RENDER_1080P=1 e 2GB livres. Sem upscale.'}

if __name__=='__main__':
    # Edge TTS defaults to SentenceBoundary; explicit WordBoundary is essential
    # for timing every 3–6-word subtitle chunk.
    assert _stamp(61.235)=='0:01:01.24'
    assert _stamp(61.235,True)=='00:01:01,235'
    assert len(_groups([{'text':'palavra','start':i,'end':i+.2} for i in range(13)]))==4
    appfile=Path('/app/app.py')
    if not appfile.exists():
        raise SystemExit('Missing app.py')
    src=appfile.read_text(encoding='utf-8')
    default_candidates=(
        "width,height=(1280,720) if p['ratio']=='16:9' else (720,1280)",
        "width,height=(960,540) if p['ratio']=='16:9' else (540,960)",
    )
    old=next((item for item in default_candidates if item in src),None)
    if old is None:
        raise SystemExit('Render resolution constant changed; safety patch refused')
    if 'STUDIO_RENDER_1080P' not in src:
        default=old[len('width,height='):]
        modified=("width,height=((1920,1080) if p['ratio']=='16:9' else (1080,1920))"
                  " if (os.getenv('STUDIO_RENDER_1080P')=='1' "
                  "and shutil.disk_usage(DATA).free>=2147483648) else "+default)
        src=src.replace(old,modified,1)
    marker='\nimport studio_edit_polish as _studio_edit_polish\n'
    if marker not in src:
        src+=marker+'_studio_edit_polish.install(__import__("sys").modules[__name__])\n'
    appfile.write_text(src,encoding='utf-8')
