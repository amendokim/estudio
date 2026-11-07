"""Opt-in cinematic pilot for a two-minute Ponte da Amizade documentary.

Applies only to a project whose title contains 'piloto cinematográfico'.
Enhancements: original animated schematic border map, original ambient soundtrack,
animated documentary typography and source labels. No paid APIs or copyrighted audio.
Leaves all other projects untouched and preserves existing TTS/MCP/OAuth behavior.
"""
from __future__ import annotations

import math
import os
import wave
import subprocess
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

_MARK = "piloto cinematográfico"


def _is_pilot(project):
    return _MARK in (project.get('title') or '').casefold()


def _font(size, bold=False):
    name = 'DejaVuSans-Bold.ttf' if bold else 'DejaVuSans.ttf'
    path = '/usr/share/fonts/truetype/dejavu/' + name
    return ImageFont.truetype(path, size=size)


def _map_png(path):
    """Design original MAPA ESQUEMÁTICO, not an assertion of street geography."""
    w, h = 1280, 720
    im = Image.new('RGB', (w, h), '#071521')
    px = im.load()
    for y in range(h):
        for x in range(w):
            v = int(13 + 8 * y / h + 4 * math.sin(x/70.0) * math.cos(y/150.0))
            px[x, y] = (v // 2, v+8, v+18)
    d = ImageDraw.Draw(im, 'RGBA')
    d.rounded_rectangle((47, 45, 1233, 674), radius=22, fill=(7, 19, 33, 235), outline=(88, 142, 154, 150), width=2)
    # River meander: vertical by convention only; intentionally not to scale.
    d.polygon([(585,145),(640,120),(672,191),(646,290),(691,365),(666,441),(710,580),(662,650),
               (585,650),(624,560),(590,468),(608,368),(575,286),(620,211)],fill=(20,104,140,245))
    for delta in (-23,-12,0,12,23):
        points=[(620+delta,161),(643+delta,225),(616+delta,303),(650+delta,375),
                (629+delta,461),(669+delta,567)]
        d.line(points, fill=(75,169,187,60), width=3)
    # Schematic bridge deck from west to east.
    d.line([(323,396),(958,396)],fill=(7,18,30,245),width=31)
    d.line([(323,396),(958,396)],fill=(235,199,133,255),width=7)
    d.line([(323,386),(958,386)],fill=(222,233,224,190),width=2)
    d.line([(323,407),(958,407)],fill=(222,233,224,140),width=2)
    d.ellipse((614,370,661,418),fill=(248,216,146,255))
    d.ellipse((628,384,647,403),fill=(15,37,53,255))
    # Map grid and annotation.
    for x in [190,340,490,790,940,1090]:
        d.line([(x,170),(x,600)],fill=(98,131,146,25),width=1)
    for y in [200,285,490,570]:
        d.line([(130,y),(1140,y)],fill=(98,131,146,25),width=1)
    d.text((110,92),'A FRONTEIRA',font=_font(33,True),fill='#e1e8e8')
    d.text((110,139),'MAPA ESQUEMÁTICO  ·  NÃO ESTÁ EM ESCALA',font=_font(16),fill='#91a9b7')
    d.text((183,276),'PARAGUAI',font=_font(30,True),fill='#e8eced')
    d.text((183,315),'CIUDAD DEL ESTE',font=_font(19),fill='#dfb97a')
    d.text((819,276),'BRASIL',font=_font(30,True),fill='#e8eced')
    d.text((819,315),'FOZ DO IGUAÇU',font=_font(19),fill='#dfb97a')
    d.text((715,499),'RIO PARANÁ',font=_font(22),fill='#91cfe1')
    d.text((431,580),'PONTE INTERNACIONAL DA AMIZADE',font=_font(18,True),fill='#e2d0ad')
    d.rounded_rectangle((130,614,1150,643),radius=10,fill=(18,42,56,180))
    d.text((315,621),'BRASIL   ←    CONEXÃO INTERNACIONAL    →   PARAGUAI',font=_font(15),fill='#a7bec5')
    path.parent.mkdir(parents=True,exist_ok=True)
    im.save(path)


def _animated_map(studio, pid, dur=22):
    folder = studio.project_dir(pid) / 'uploads'
    png = folder / 'mapa_esquematico_original.png'
    mp4 = folder / 'mapa_esquematico_animado.mp4'
    if mp4.is_file() and mp4.stat().st_size > 2000:
        return mp4
    _map_png(png)
    cmd=[studio.FFMPEG, '-hide_banner','-loglevel','error','-nostdin','-y',
         '-loop','1','-framerate','24','-i',str(png),'-t',str(dur),
         '-vf',"zoompan=z='min(zoom+0.0003,1.09)':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=1:s=1280x720:fps=24,format=yuv420p",
         '-an','-c:v','libx264','-preset','veryfast','-crf','26','-movflags','+faststart',str(mp4)]
    proc=subprocess.run(cmd,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=150)
    if proc.returncode or not mp4.is_file():
        raise RuntimeError('Não foi possível animar o mapa: '+proc.stderr.decode('utf8','replace')[-450:])
    return mp4


def _make_music(studio, pid):
    p=studio.get_project(pid)
    if p.get('soundtrack'):
        return
    aud=studio.project_dir(pid)/'audio'
    aud.mkdir(parents=True,exist_ok=True)
    wav=aud/'trilha_ambiental_original.wav'
    mp3=aud/'trilha_ambiental_original.mp3'
    if not mp3.is_file() or mp3.stat().st_size < 3000:
        sr, seconds=16000,16
        chords=[(146.8,174.6,220.0),(116.54,146.8,174.6),(130.8,164.8,196.0),(130.8,164.8,220.0)]
        import struct
        with wave.open(str(wav),'wb') as wf:
            wf.setnchannels(1);wf.setsampwidth(2);wf.setframerate(sr)
            out=bytearray()
            for i in range(sr*seconds):
                t=i/sr
                chord=chords[min(3,int(t/4))]
                phase=t % 4
                fade=min(1.0,phase/0.55,(4.0-phase)/0.55)
                pad=sum(math.sin(2*math.pi*hz*t+0.11*math.sin(t*0.4)) for hz in chord)/3
                pad+=0.20*math.sin(2*math.pi*73.4*t)
                pulse=0.035*math.sin(2*math.pi*90*t)*math.exp(-9*(t%2))
                amp=(0.11*pad+0.08*pulse)*max(0.0,fade)
                out+=struct.pack('<h',int(max(-1,min(1,amp))*32767))
            wf.writeframes(out)
        cmd=[studio.FFMPEG,'-hide_banner','-loglevel','error','-nostdin','-y',
             '-i',str(wav),'-c:a','libmp3lame','-q:a','6',str(mp3)]
        done=subprocess.run(cmd,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=40)
        wav.unlink(missing_ok=True)
        if done.returncode:
            raise RuntimeError('Não foi possível finalizar a trilha de áudio')
    studio.update_project(pid,soundtrack=str(mp3.relative_to(studio.MEDIA)))


def _timestamp(t):
    t=max(0,float(t)); hh=int(t//3600);mm=int((t%3600)//60);ss=int(t%60);cs=int((t%1)*100)
    return f'{hh}:{mm:02d}:{ss:02d}.{cs:02d}'


def _subtitle_events(project):
    scenes=project.get('scenes') or []
    if not scenes:
        return ''
    bounds=[0.]
    for scene in scenes:
        bounds.append(bounds[-1]+max(1,float(scene.get('duration') or 8)))
    total=bounds[-1]
    events=[]
    def add(start,end,text,size=29,x=86,y=92,color='&H00F1E3B6&',alignment=7):
        if end<=start:return
        tags=(r'{\an'+str(alignment)+r'\pos('+str(x)+','+str(y)+r')\fs'+str(size)
              +r'\bord1\shad0\c'+color+r'\fad(500,350)}')
        events.append('Dialogue: 8,'+_timestamp(start)+','+_timestamp(end)+',Default,,0,0,0,,'+tags+text)
    add(0.7,min(total,6.8),'PONTE DA AMIZADE',58,76,158,'&H00FAE9C9&')
    add(1.6,min(total,7.2),'UMA FRONTEIRA. DUAS NAÇÕES.',22,81,242)
    if len(bounds)>2:
        add(bounds[1]+0.5,min(bounds[2]-.5,bounds[1]+6),'FOZ DO IGUAÇU  ×  CIUDAD DEL ESTE',22,70,88)
    if len(bounds)>5:
        add(bounds[4]+0.4,min(bounds[5],bounds[4]+6),'553 METROS  |  ENGENHARIA SOBRE O RIO',22,74,92)
    if len(bounds)>7:
        add(bounds[6]+0.4,min(bounds[7],bounds[6]+7),'27 DE MARÇO DE 1965',30,70,92)
    if len(bounds)>9:
        add(bounds[8]+0.4,min(bounds[9],bounds[8]+5),'UMA TRAVESSIA QUE TRANSFORMA CIDADES',21,72,91)
    if len(bounds)>10:
        add(max(0,total-7),max(0,total-1.3),'A HISTÓRIA CONTINUA EM CADA TRAVESSIA',24,70,96)
    for i,scene in enumerate(scenes):
        if scene.get('media_provider') == 'pixabay':
            add(bounds[i]+0.25,bounds[i+1]-0.3,'IMAGENS DE APOIO',14,1085,656,'&H00DBD7D1&',9)
        if scene.get('media_provider') == 'wikimedia' and '2015' in (scene.get('query') or ''):
            add(bounds[i]+0.3,bounds[i+1]-0.5,'ARQUIVO: TVNBR / 2015',14,70,646,'&H00FFFFFF&')
    add(max(0,total-5),max(0,total-1),'TRILHA ORIGINAL  |  FONTES E CRÉDITOS NO PROJETO',14,70,676)
    return '\n'.join(events)+'\n'


def install(studio):
    original_autofill=studio._auto_find_media
    original_subtitles=studio.subtitles_for
    original_render=studio.render_task

    def pilot_search(pid, limit):
        result=original_autofill(pid,limit)
        proj=studio.get_project(pid)
        if not _is_pilot(proj):
            return result
        if len(proj['scenes'])<2:
            return result
        try:
            mp4=_animated_map(studio,pid)
            scene=proj['scenes'][1]
            with studio.db() as connection:
                connection.execute('''UPDATE scenes SET media_url='',media_local=?,media_kind='video',
                   media_provider='original',source_page='',author='Mapa esquemático original Estúdio IA',thumb=''
                   WHERE id=? AND project_id=?''',
                   (str(mp4.relative_to(studio.MEDIA)),scene['id'],pid))
            print('[cinematic] schematic animated map assigned',flush=True)
        except Exception as exc:
            print('[cinematic] map unavailable:',str(exc)[:230],flush=True)
        return result

    def pilot_subtitles(project,out,width,height):
        ass,srt=original_subtitles(project,out,width,height)
        if _is_pilot(project) and width>height:
            with open(ass,'a',encoding='utf-8') as handle:
                handle.write(_subtitle_events(project))
        return ass,srt

    def pilot_render(pid):
        p=studio.get_project(pid)
        if _is_pilot(p):
            try:
                _make_music(studio,pid)
                print('[cinematic] original soundtrack ready',flush=True)
            except Exception as exc:
                print('[cinematic] soundtrack unavailable:',str(exc)[:230],flush=True)
        return original_render(pid)

    studio._auto_find_media=pilot_search
    studio.subtitles_for=pilot_subtitles
    studio.render_task=pilot_render


if __name__=='__main__':
    # Extends the existing app's module-level function hooks after other extensions.
    from pathlib import Path
    app=Path('/app/app.py')
    if not app.exists():
        raise SystemExit('Missing /app/app.py')
    mark='\nimport studio_cinematic_pilot as _cinematic_pilot\n'
    source=app.read_text(encoding='utf8')
    if mark not in source:
        app.write_text(source+mark+'_cinematic_pilot.install(__import__("sys").modules[__name__])\n',encoding='utf8')
