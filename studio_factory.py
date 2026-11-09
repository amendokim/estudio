"""Persistent, low-cost documentary factory for Meu Estúdio IA.

Adds a durable job queue, deterministic resource checks, optional budgeted Gemini
script writing and an operational dashboard. It deliberately does NOT create
paid API calls or start a daily production schedule without explicit queued jobs.
Production of long videos is blocked until disk/limits are raised on a suitable
worker; existing MCP/OAuth, projects and rendering routes are unchanged.
"""
from __future__ import annotations

import json
import math
import os
# Railway may contain a legacy environment key using spaces or mixed case.
# Normalize only inside the running process; do not copy or log the secret.
if not os.environ.get("GEMINI_API_KEY"):
    for _legacy_gemini_name in ("Gemini API Key", "Gemini_API_Key", "Gemini_API_KEY"):
        _legacy_gemini_value = os.environ.get(_legacy_gemini_name)
        if _legacy_gemini_value and _legacy_gemini_value.strip():
            os.environ["GEMINI_API_KEY"] = _legacy_gemini_value.strip()
            print("[factory] Gemini API credential recognized through existing Railway variable", flush=True)
            break
    del _legacy_gemini_name
    if "_legacy_gemini_value" in globals():
        del _legacy_gemini_value
import shutil
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Literal

import httpx
from fastapi import HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

WORKER_WAKE = threading.Event()
WORKER_GUARD = threading.Lock()
WORKER_STARTED = False


def utc():
    return datetime.now(timezone.utc).isoformat()


def limit_int(name: str, default: int, minimum=1, maximum=100000):
    try:
        return min(max(int(os.getenv(name, str(default))), minimum), maximum)
    except ValueError:
        return default


class QueueRequest(BaseModel):
    project_id: str = Field(min_length=36, max_length=36)
    operation: Literal['validate', 'render', 'autopilot'] = 'validate'


class BatchRequest(BaseModel):
    topics: list[str] = Field(min_length=1, max_length=10)
    duration_minutes: int = Field(default=30, ge=1, le=60)
    ratio: Literal['16:9', '9:16'] = '16:9'
    produce: bool = False


def schema(studio):
    with studio.db() as c:
        c.execute('''CREATE TABLE IF NOT EXISTS factory_jobs (
          id TEXT PRIMARY KEY, project_id TEXT, topic TEXT NOT NULL DEFAULT '',
          title TEXT NOT NULL DEFAULT '', duration_minutes INTEGER NOT NULL DEFAULT 0,
          ratio TEXT NOT NULL DEFAULT '16:9',
          operation TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'queued',
          auto_render INTEGER NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0,
          error TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
          started_at TEXT, ended_at TEXT)''')
        c.execute('CREATE INDEX IF NOT EXISTS factory_jobs_state ON factory_jobs(state, created_at)')


def get_jobs(studio, limit=30):
    with studio.db() as c:
        rows = c.execute('''SELECT id,project_id,topic,title,duration_minutes,ratio,
                           operation,state,auto_render,attempts,error,created_at,
                           started_at,ended_at FROM factory_jobs ORDER BY created_at DESC LIMIT ?''',
                         (min(int(limit), 100),)).fetchall()
    return [dict(x) for x in rows]


def estimate(studio, project):
    minutes = int(project.get('duration_minutes') or 3)
    scenes = project.get('scenes') or []
    estimated_speech_minutes = round(sum(len(str(s.get('text') or '').split()) / 145 for s in scenes), 1)
    selected_videos = sum(s.get('media_kind') == 'video' for s in scenes)
    # 720p video bitrates vary heavily; this is a cautious capacity range, NOT an actual quote.
    final_gb_low, final_gb_high = round(minutes * 0.005, 2), round(minutes * 0.012, 2)
    working_mb = 250 + 85 * minutes
    free_mb = shutil.disk_usage(studio.DATA).free / 1048576
    max_minutes = limit_int('STUDIO_FACTORY_MAX_RENDER_MINUTES', 5, 1, 60)
    minimum_free_mb = limit_int('STUDIO_FACTORY_MIN_FREE_MB', 150, 50, 200000)
    # One owner-approved 540p test film. The original 675 MB prediction is
    # intentionally retained as the conservative estimate, but 400 MB of free
    # volume is accepted for this one 32-scene short-form test after cleaning
    # historical projects. Production jobs and other projects keep the limit.
    compact_preview = (
        project.get('id') == '8c855a6d-c95a-4642-bf9c-a46074404935'
        and minutes == 5 and project.get('ratio') == '16:9'
        and 26 <= len(scenes) <= 36
        and os.getenv('STUDIO_RENDER_1080P', '0') != '1'
    )
    required_free_mb = max(minimum_free_mb, 400 if compact_preview else working_mb)
    allowed = minutes <= max_minutes and free_mb >= required_free_mb
    return {
        'id': project['id'], 'title': project['title'], 'minutes': minutes,
        'scenes': len(scenes), 'scenes_with_video': selected_videos,
        'estimated_narration_minutes': estimated_speech_minutes,
        'estimated_final_mp4_gb_range': [final_gb_low, final_gb_high],
        'estimated_peak_working_mb': working_mb,
        'required_free_mb': required_free_mb,
        'experimental_compact_pilot': compact_preview,
        'available_disk_mb': round(free_mb),
        'configured_render_limit_minutes': max_minutes,
        'can_render_here': allowed,
        'reason': ('ok' if allowed else
                  'Este trabalhador não tem limite de duração ou espaço para o render. '
                  'Prepare o roteiro agora e execute a produção longa numa VPS com armazenamento maior.'),
    }


def queue(studio, operation, project_id='', topic='', title='', duration=0, ratio='16:9', auto_render=False):
    jid = str(uuid.uuid4())
    with studio.db() as c:
        c.execute('''INSERT INTO factory_jobs
          (id,project_id,topic,title,duration_minutes,ratio,operation,state,auto_render,created_at)
          VALUES(?,?,?,?,?,?,?,?,?,?)''',
          (jid, project_id or None, topic, title, duration, ratio, operation,
           'queued', int(bool(auto_render)), utc()))
    WORKER_WAKE.set()
    return {'id': jid, 'state': 'queued', 'operation': operation, 'project_id': project_id or None}


def next_job(studio):
    with studio.db() as c:
        c.execute('BEGIN IMMEDIATE')
        job = c.execute("SELECT * FROM factory_jobs WHERE state='queued' ORDER BY created_at, rowid LIMIT 1").fetchone()
        if not job:
            return None
        c.execute("UPDATE factory_jobs SET state='running',attempts=attempts+1,started_at=?,error='' WHERE id=?",
                  (utc(), job['id']))
        return dict(job)


def set_state(studio, jid, state, error='', project_id=None):
    with studio.db() as c:
        if project_id:
            c.execute('UPDATE factory_jobs SET state=?,error=?,project_id=?,ended_at=? WHERE id=?',
                      (state, error[:1200], project_id, utc(), jid))
        else:
            c.execute('UPDATE factory_jobs SET state=?,error=?,ended_at=? WHERE id=?',
                      (state, error[:1200], utc(), jid))


def gemini_json(prompt: str):
    key = os.getenv('GEMINI_API_KEY', '')
    if not key:
        raise RuntimeError('GEMINI_API_KEY não configurada. Não houve chamada paga.')
    # New Google projects often receive 404 on legacy 2.5 models. Prefer
    # Google's current low-cost Flash-Lite model for short script writing.
    model = os.getenv('GEMINI_MODEL', 'gemini-3.5-flash-lite')
    if model not in ('gemini-3.5-flash-lite', 'gemini-3.1-flash-lite',
                     'gemini-3.8-flash', 'gemini-3.5-flash',
                     'gemini-2.5-flash-lite', 'gemini-2.5-flash'):
        raise RuntimeError('Modelo Gemini não permitido')
    # Gemini intermittently returns HTTP 503 while scaling. Retry only
    # transient gateway/server failures, never 401/403 billing or 429 quota.
    # Max three attempts (bounded), no repeated project creation.
    for attempt in range(3):
        try:
            with httpx.Client(timeout=150) as client:
                r = client.post(f'https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent',
                                headers={'x-goog-api-key': key, 'Content-Type': 'application/json'},
                                json={'contents': [{'parts': [{'text': prompt}]}],
                                      'generationConfig': {'responseMimeType': 'application/json',
                                                           'temperature': 0.5,
                                                           'maxOutputTokens': 8192}})
                r.raise_for_status()
            break
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code not in (500,502,503,504) or attempt==2:
                raise
            print(f"[factory] Gemini temporary HTTP {exc.response.status_code}; "
                  f"retry {attempt+1}/2",flush=True)
            time.sleep(6*(attempt+1))
    data = r.json()
    parts = data['candidates'][0]['content']['parts']
    return json.loads(''.join(part.get('text', '') for part in parts))


def generate_script(studio, topic: str, minutes: int, ratio: str, title: str | None = None,
                    language: str = 'pt'):
    """Creates a human-reviewable script; does NOT automatically publish anything."""
    if not os.getenv('GEMINI_API_KEY'):
        raise RuntimeError('Para escrever roteiros automaticamente, configure GEMINI_API_KEY no Railway.')
    voices = {'pt':'pt-BR-AntonioNeural', 'es':'es-ES-AlvaroNeural',
              'en':'en-US-GuyNeural', 'fr':'fr-FR-HenriNeural',
              'de':'de-DE-ConradNeural'}
    outputs = {'pt':'português brasileiro','es':'espanhol natural da Espanha',
               'en':'inglês natural', 'fr':'francês natural da França',
               'de':'alemão natural da Alemanha'}
    if language not in voices:
        raise RuntimeError('Idioma de canal não suportado')
    output_lang = outputs[language]
    chapters = math.ceil(minutes / 5)
    outline = gemini_json(
        'Você é roteirista de documentários originais de alto rigor factual. '
        f'Crie esboço de {chapters} capítulos sobre {topic!r}. '
        f'O resultado final deve ser escrito integralmente em {output_lang}. '
        'Responda JSON {"titulo":"...","capitulos":[{"titulo":"...","objetivo":"..."}]} '
        f'com exatamente {chapters} capítulos. Nunca invente datas, números, fontes ou citações; '
        'assuntos controversos devem ser qualificados e as afirmações devem ser verificadas antes da publicação.')
    plans = outline.get('capitulos') or []
    if not isinstance(plans, list) or len(plans) != chapters:
        raise RuntimeError('O planejamento do modelo não devolveu a quantidade correta de capítulos')
    scenes = []
    per_chapter_min = minutes / chapters
    for index, ch in enumerate(plans, 1):
        target_words = max(115, round(per_chapter_min * 130))
        result = gemini_json(
            f'Crie a narração COMPLETA do capítulo {index}/{chapters}, de um documentário original sobre {topic!r}. '
            f'Título do capítulo: {ch.get("titulo", "")}. Objetivo: {ch.get("objetivo", "")}. '
            f'Produza aproximadamente {target_words} palavras de narração em {output_lang}, '
            'divididas em 8 a 10 cenas coerentes. A cada cena inclua busca_visual em inglês '
            'com 2 a 5 palavras concretas relacionadas ao local/tempo/objeto. '
            'Evite afirmações históricas específicas sem verificação; se faltar evidência, narre com cautela. '
            'Vídeo de apoio ilustrativo deve ser distinguido de arquivo histórico autêntico. '
            'Não repita informações dos outros capítulos. '
            'Resposta JSON {"cenas":[{"texto":"narração completa","busca_visual":"short english phrase"}]}.')
        chunk = result.get('cenas') or []
        if not isinstance(chunk, list) or not 5 <= len(chunk) <= 12:
            raise RuntimeError(f'Capítulo {index}: número inválido de cenas')
        for s in chunk:
            body = str(s.get('texto') or '').strip()
            search = str(s.get('busca_visual') or '').strip()[:200]
            if len(body) < 35 or not search:
                raise RuntimeError(f'Capítulo {index}: cena incompleta')
            scenes.append({'text': body[:8000], 'query': search})
    if len(scenes) > 120:
        raise RuntimeError('Número de cenas acima do limite de produção')
    desired_words = minutes * 130
    actual_words = sum(len(s['text'].split()) for s in scenes)
    if actual_words < desired_words * 0.70:
        raise RuntimeError(f'Roteiro curto ({actual_words} palavras para alvo de {desired_words}); nenhum projeto foi criado')
    name = (title or str(outline.get('titulo') or topic))[:160]
    project = studio.create_project(studio.NewProject(title=name, topic=topic,
                                                     duration_minutes=minutes, ratio=ratio,
                                                     voice=voices[language]))
    full = '\n\n'.join(s['text'] for s in scenes)
    studio.set_script(project['id'], full, structured=scenes)
    return project['id']


def execute(studio, job):
    operation = job['operation']
    pid = job.get('project_id')
    if operation == 'write':
        pid = generate_script(studio, job['topic'], job['duration_minutes'], job['ratio'], job['title'])
        set_state(studio, job['id'], 'completed', project_id=pid)
        return
    if operation == 'validate':
        p = studio.get_project(pid)
        data = estimate(studio, p)
        if not p['scenes']:
            raise RuntimeError('Projeto sem cenas; escreva o roteiro primeiro')
        set_state(studio, job['id'], 'completed',
                  error=f'Validação pronta. Cenas: {data["scenes"]}; renderizável aqui: {data["can_render_here"]}')
        return
    p = studio.get_project(pid)
    data = estimate(studio, p)
    if not data['can_render_here']:
        set_state(studio, job['id'], 'blocked', error=data['reason'])
        return
    if operation == 'autopilot':
        studio.autopilot_task(pid)
    else:
        studio.render_task(pid)
    p = studio.get_project(pid)
    if p['status'] != 'concluído':
        raise RuntimeError(str(p.get('error') or 'Renderização não concluída'))
    set_state(studio, job['id'], 'completed')


def worker(studio):
    while True:
        if os.getenv('STUDIO_FACTORY_WORKER_ENABLED', '1') != '1':
            WORKER_WAKE.wait(15)
            WORKER_WAKE.clear()
            continue
        try:
            job = next_job(studio)
            if not job:
                WORKER_WAKE.wait(15)
                WORKER_WAKE.clear()
                continue
            try:
                execute(studio, job)
            except Exception as exc:
                set_state(studio, job['id'], 'failed', error=str(exc))
        except Exception as exc:
            print('[factory] worker error:', str(exc)[:300], flush=True)
            time.sleep(5)


DASHBOARD = '''<!doctype html><html lang="pt-BR"><meta charset="utf-8">
<title>Fábrica de Documentários — Meu Estúdio IA</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>body{font:16px system-ui;background:#0a1223;color:#eaf0fa;max-width:940px;margin:24px auto;padding:12px}
.panel{background:#152338;padding:22px;border-radius:16px;margin-bottom:16px}input,select,textarea,button{font:inherit;padding:10px;margin:4px 0;border-radius:8px;border:1px solid #405570}input,textarea,select{width:100%;box-sizing:border-box;background:#081426;color:white}button{background:#40b8ea;color:#001c2b;border:0;cursor:pointer}code{color:#8fe0c7}table{width:100%;border-collapse:collapse}td,th{text-align:left;border-bottom:1px solid #30445a;padding:9px}small{color:#9fb3c9}a{color:#8adcff}</style>
<h1>Fábrica de documentários</h1><p><a href="/">← Voltar ao Estúdio IA</a> · <a href="/rede">Rede de Canais IA — pesquisa mundial, thumbnails e calendário</a></p>
<div class="panel"><h2>Capacidade e segurança</h2><pre id="info">Carregando…</pre></div>
<div class="panel"><h2>Preparar lote de roteiros</h2><p><small>Exige GEMINI_API_KEY. Cada linha é um tema; a API do Gemini pode cobrar pelos roteiros. Não renderiza nem publica automaticamente.</small></p>
<textarea id="topics" rows="5" placeholder="História da Ponte da Amizade\nO surgimento das grandes cidades"></textarea>
<label>Minutos por documentário <input type="number" id="minutes" min="1" max="60" value="30"></label>
<button id="write">Gerar roteiros do lote</button></div>
<div class="panel"><h2>Projetos existentes</h2><p><small>Arquivos de origem YouTube somente quando relevantes e com licença comprovada — sem meta percentual. Bancos de vídeo e gráficos originais são priorizados quando melhores para o roteiro.</small></p><p><small>Roteiros criados no ChatGPT também aparecem aqui. Validação não gera vídeo nem chama IA paga.</small></p><div id="projects"></div></div>
<div class="panel"><h2>Fila de produção</h2><button id="refresh">Atualizar</button><div id="jobs"></div></div>
<script>
async function req(url,init){const response=await fetch(url,init);const data=await response.json();if(!response.ok)throw Error(data.detail||JSON.stringify(data));return data;}
function sanitizeRationale(value){return String(value).trim().slice(0,900)}
function td(row,value){const cell=document.createElement('td');cell.textContent=String(value??'');row.appendChild(cell);return cell;}
async function refresh(){try{const s=await req('/api/factory/status');document.getElementById('info').textContent=JSON.stringify(s,null,2);
const projects=await req('/api/projects');const box=document.getElementById('projects');box.replaceChildren();
const storage=await req('/api/factory/storage');
for(const p of projects){const section=document.createElement('p');section.textContent=p.title+' — '+p.status+' — '+p.duration_minutes+' min ';
const view=document.createElement('a');view.href='/?project='+encodeURIComponent(p.id);view.textContent='Abrir';section.append(view);
const visual=document.createElement('a');visual.href='/api/editorial/semantic/projects/'+encodeURIComponent(p.id);
visual.textContent='Plano visual por cena';visual.target='_blank';visual.rel='noopener noreferrer';
section.append(' · ',visual);
const direction=document.createElement('a');
direction.href='/api/editorial/strategy/projects/'+encodeURIComponent(p.id);
direction.textContent='Origem de mídia e ritmo';direction.target='_blank';
direction.rel='noopener noreferrer';section.append(' · ',direction);
const inspect=document.createElement('button');inspect.textContent='Analisar capacidade';inspect.onclick=async()=>{try{const plan=await req('/api/factory/plan/'+encodeURIComponent(p.id));alert(JSON.stringify(plan,null,2))}catch(e){alert(e.message)}};section.append(' ',inspect);const archival=document.createElement('button');archival.textContent='Fontes do YouTube';archival.onclick=async()=>{try{const data=await req('/api/youtube-policy/projects/'+encodeURIComponent(p.id));alert('Arquivo de origem YouTube é opcional: somente quando o tema, a cena e os direitos de uso estão comprovados. Participação atual: '+data.actual_percent+'%. Sem meta percentual obrigatória.')}catch(e){alert('Relatório indisponível: '+e.message)}};section.append(' ',archival);
const autoPolicy=document.createElement('button');autoPolicy.textContent='Regra automática: 10 s';
autoPolicy.onclick=async()=>{try{
 const rules=await req('/api/youtube-policy');
 const summary=await req('/api/youtube-policy/projects/'+encodeURIComponent(p.id));
 alert('Escolha por relevância e direitos comprovados. Sem meta fixa de YouTube. Arquivo verificado: '+summary.actual_percent+'%. Limite editorial de trecho: '+rules.max_youtube_clip_seconds+' segundos. Essa duração não dispensa licença.');
}catch(e){alert(e.message)}};
section.append(' ',autoPolicy);
const archiveSearch=document.createElement('button');archiveSearch.textContent='Buscar acervo licenciado';
archiveSearch.onclick=async()=>{archiveSearch.disabled=true;
 try{
  const list=await req('/api/youtube-policy/projects/'+encodeURIComponent(p.id)+'/discover');
  const items=(list.sources||[]).slice(0,12).map(x=>'• '+x.title+' | '+x.license+'\n'+x.source_page);
  alert('Acervo documental identificado: '+list.count+' fonte(s).\n'+
    'Cada trecho pode usar no máximo '+list.max_seconds_per_clip+' segundos.\n\n'+
    (items.join('\n\n')||'Ainda não foram encontrados arquivos suficientes para esse tema.'));
 }catch(e){alert('Consulta ao arquivo indisponível: '+e.message)}
 finally{archiveSearch.disabled=false}
};
section.append(' ',archiveSearch);
const parts=document.createElement('button');parts.textContent='Ver capítulos';parts.onclick=async()=>{try{const info=await req('/api/factory/chapters/'+encodeURIComponent(p.id));alert(JSON.stringify(info,null,2))}catch(e){alert(e.message)}};section.append(' ',parts);
if(p.status==='concluído'){const dl=document.createElement('a');dl.href='/api/projects/'+encodeURIComponent(p.id)+'/download/video';dl.textContent='Baixar MP4';section.append(' ',dl);
const capcut=document.createElement('button');capcut.textContent='Preparar para CapCut';
capcut.title='Exportar ZIP com áudios, legendas, mídias locais, roteiro, créditos e ordem das cenas';
capcut.onclick=async()=>{try{
  capcut.disabled=true;const url='/api/capcut/projects/'+encodeURIComponent(p.id);
  const data=await req(url);
  const info=data.package;
  const size=Math.round((info.bytes_local||0)/1048576);
  const warning=(info.missing_assets||[]).length;
  const message='Preparar pacote CapCut ('+size+' MB em arquivos locais, '+warning+' referências ausentes)?\\nO arquivo contém trilhas de áudio, legendas e mídias locais, mas não é um projeto nativo do CapCut. Não baixa novos vídeos nem altera o MP4.';
  if(confirm(message)){window.location.href=url+'/download'}
}catch(e){alert('Exportação CapCut indisponível: '+e.message)}
finally{capcut.disabled=false}};section.append(' ',capcut);
if(storage.storage_configured){const arc=document.createElement('button');arc.textContent='Arquivar MP4';arc.onclick=async()=>{if(!confirm('Copiar este MP4 para o bucket? A operação pode gerar cobranças de armazenamento.'))return;try{const x=await req('/api/factory/archive/'+encodeURIComponent(p.id),{method:'POST'});alert(x.note||'Armazenado.');refresh()}catch(e){alert(e.message)}};section.append(' ',arc);
const remote=document.createElement('a');remote.href='/api/factory/archive/'+encodeURIComponent(p.id)+'/download';remote.textContent='Baixar do bucket';section.append(' ',remote)}}
if(p.status!=='concluído'){const button=document.createElement('button');button.textContent='Colocar na fila de produção';button.onclick=async()=>{
try{const plan=await req('/api/factory/plan/'+encodeURIComponent(p.id));if(!plan.can_render_here){alert(plan.reason);return}
if(!confirm('Iniciar a produção de '+p.title+'? Isso consome CPU, armazenamento e recursos da hospedagem.'))return;
await req('/api/factory/jobs',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({project_id:p.id,operation:'autopilot'})});refresh()}catch(e){alert(e.message)}};section.append(' ',button)}box.append(section)}
const jobs=await req('/api/factory/jobs');const holder=document.getElementById('jobs');holder.replaceChildren();const table=document.createElement('table');const head=document.createElement('tr');['Título/Tema','Operação','Estado','Erro / resultado'].forEach(x=>{const th=document.createElement('th');th.textContent=x;head.appendChild(th)});table.appendChild(head);
for(const item of jobs.jobs){const tr=document.createElement('tr');const first=td(tr,item.title||item.topic||item.project_id);if(item.project_id){const a=document.createElement('a');a.href='/?project='+encodeURIComponent(item.project_id);a.textContent='Abrir projeto';first.append(' ',a)}td(tr,item.operation);td(tr,item.state);td(tr,item.error);table.appendChild(tr)}holder.appendChild(table);
}catch(e){document.getElementById('info').textContent='Erro: '+e.message}}
document.getElementById('write').onclick=async()=>{try{const topics=document.getElementById('topics').value.split('\n').map(s=>s.trim()).filter(Boolean);const duration_minutes=Number(document.getElementById('minutes').value);const r=await req('/api/factory/batch',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({topics,duration_minutes})});alert(r.jobs.length+' roteiro(s) colocados na fila');refresh()}catch(e){alert(e.message)}};
document.getElementById('refresh').onclick=refresh;refresh();
</script></html>'''


def register(studio):
    schema(studio)
    # An interrupted, uncommitted job is eligible for one replay on app restart.
    with studio.db() as c:
        c.execute("UPDATE factory_jobs SET state='queued',started_at=NULL,error='Reiniciado após deploy' WHERE state='running' AND attempts<2")
        c.execute("UPDATE factory_jobs SET state='failed',error='Interrompido em reinicializações anteriores' WHERE state='running' AND attempts>=2")

    @studio.app.get('/fabrica')
    def ui():
        return HTMLResponse(DASHBOARD)

    @studio.app.get('/api/factory/status')
    def factory_status():
        with studio.db() as c:
            counts = {r['state']: r['count'] for r in c.execute('SELECT state,COUNT(*) AS count FROM factory_jobs GROUP BY state')}
        free = shutil.disk_usage(studio.DATA).free / 1048576
        return {'queue_counts': counts, 'free_disk_mb': round(free),
                'gemini_ready': bool(os.getenv('GEMINI_API_KEY')),
                'writer_model': os.getenv('GEMINI_MODEL', 'gemini-3.5-flash-lite'),
                'worker_enabled': os.getenv('STUDIO_FACTORY_WORKER_ENABLED', '1') == '1',
                'render_limit_minutes': limit_int('STUDIO_FACTORY_MAX_RENDER_MINUTES', 5, 1, 60),
                'note': 'Não publica no YouTube. Roteiros por Gemini usam API paga apenas quando solicitados.'}

    @studio.app.get('/api/factory/jobs')
    def jobs():
        return {'jobs': get_jobs(studio)}

    @studio.app.get('/api/factory/plan/{pid}')
    def plan(pid: str):
        return estimate(studio, studio.get_project(pid))

    @studio.app.post('/api/factory/jobs')
    def submit(payload: QueueRequest):
        project = studio.get_project(payload.project_id)
        if payload.operation != 'validate':
            if not project['scenes']:
                raise HTTPException(400, 'Este projeto precisa de roteiro e cenas')
            if project['status'] in ('renderizando', 'produção automática', 'gerando narração'):
                raise HTTPException(409, 'Projeto já está sendo produzido')
            if project['status'] == 'concluído':
                raise HTTPException(409, 'Projeto já concluído. Use validação, sem renderizar novamente.')
            check = estimate(studio, project)
            if not check['can_render_here']:
                raise HTTPException(409, check['reason'])
        with studio.db() as c:
            pending = c.execute("SELECT id FROM factory_jobs WHERE project_id=? AND state IN ('queued','running')", (payload.project_id,)).fetchone()
        if pending:
            raise HTTPException(409, 'Este projeto já está na fila')
        return queue(studio, payload.operation, project_id=payload.project_id,
                     title=project['title'], duration=project['duration_minutes'])

    @studio.app.post('/api/factory/batch')
    def submit_batch(payload: BatchRequest):
        if not os.getenv('GEMINI_API_KEY'):
            raise HTTPException(409, 'GEMINI_API_KEY não configurada; nenhum custo de API foi gerado.')
        if payload.produce:
            raise HTTPException(409, 'Renderização em lote desabilitada até habilitar trabalhadores dedicados e armazenamento externo.')
        topics = [s.strip() for s in payload.topics]
        if any(len(s) < 10 or len(s) > 500 for s in topics):
            raise HTTPException(422, 'Cada tema deve ter entre 10 e 500 caracteres')
        max_daily = limit_int('STUDIO_FACTORY_DAILY_SCRIPT_LIMIT', 5, 1, 30)
        day = utc()[:10]
        with studio.db() as c:
            today = c.execute("SELECT COUNT(*) FROM factory_jobs WHERE operation='write' AND created_at LIKE ?", (day+'%',)).fetchone()[0]
        if today + len(topics) > max_daily:
            raise HTTPException(429, f'Limite diário de {max_daily} roteiros. Envie um lote menor.')
        result = [queue(studio, 'write', topic=s, title=s[:160],
                        duration=payload.duration_minutes, ratio=payload.ratio) for s in topics]
        return {'jobs': result, 'note': 'Somente roteiros. Reveja fatos antes de produzir ou publicar.'}

    @studio.app.on_event('startup')
    def start_factory():
        global WORKER_STARTED
        with WORKER_GUARD:
            if not WORKER_STARTED:
                threading.Thread(target=worker, args=(studio,), name='studio-factory', daemon=True).start()
                WORKER_STARTED = True
        WORKER_WAKE.set()


if __name__ == '__main__':
    from pathlib import Path
    app_file = Path('/app/app.py')
    if not app_file.exists():
        raise SystemExit('Missing app.py; cannot install documentary factory')
    marker = '\nimport studio_factory as _factory\n'
    original = app_file.read_text(encoding='utf-8')
    if marker not in original:
        app_file.write_text(original + marker + '_factory.register(__import__("sys").modules[__name__])\n', encoding='utf-8')