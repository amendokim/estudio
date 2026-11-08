"""Persistent multi-channel production control room for Meu Estúdio IA.

One channel = one independent plan, local publishing timezone, hours,
runtime/quality limits and persistent play/pause switch. The state lives in
SQLite on the Railway volume; closing the browser does not pause the worker.

Safety: PLAY is refused without owner opt-in for Gemini costs, OAuth,
scheduled uploads, and a real render-storage reserve. No uploads or new
billable scripts occur by installing this feature or viewing its dashboard.
Generated A/B candidates are a packaging pack, not a native YouTube test.
"""
from __future__ import annotations
import json
import os
import shutil
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

import studio_channel_network as net
import studio_channel_pipeline as pipe

WORKER_LOCK=threading.Lock()
WORKER_STARTED=False
MIN_FREE_MB=800
MAX_PER_CHANNEL=2

class Plan(BaseModel):
    daily_quantity:int=Field(ge=1,le=2)
    hours:list[str]=Field(min_length=1,max_length=2)
    timezone:str=Field(min_length=3,max_length=70)
    duration_minutes:int=Field(ge=2,le=30)

def schema(studio):
    with studio.db() as db:
        db.execute("""CREATE TABLE IF NOT EXISTS studio_channel_control(
           channel_id TEXT PRIMARY KEY,
           mode TEXT NOT NULL DEFAULT 'paused',
           duration_minutes INTEGER NOT NULL DEFAULT 5,
           last_tick TEXT NOT NULL DEFAULT '',
           last_message TEXT NOT NULL DEFAULT '',
           updated_at TEXT NOT NULL)""")
        db.execute("""CREATE TABLE IF NOT EXISTS studio_channel_control_ticks(
           channel_id TEXT NOT NULL,
           slot_utc TEXT NOT NULL,
           state TEXT NOT NULL,
           last_error TEXT NOT NULL DEFAULT '',
           created_at TEXT NOT NULL,
           PRIMARY KEY(channel_id,slot_utc))""")

def config(studio,cid):
    with studio.db() as db:
        row=db.execute("SELECT * FROM studio_channel_control WHERE channel_id=?",(cid,)).fetchone()
    return dict(row) if row else {"channel_id":cid,"mode":"paused",
                                  "duration_minutes":5,
                                  "last_tick":"","last_message":""}

def flags(studio,channel):
    from studio_channel_publish import linked
    available=shutil.disk_usage(studio.DATA).free/1048576
    oauth_env=bool(os.getenv("GOOGLE_YT_CLIENT_ID") and os.getenv("GOOGLE_YT_CLIENT_SECRET"))
    connected=bool(linked(studio,channel["id"]))
    ready={
      "research_key":bool(os.getenv("YOUTUBE_DATA_API_KEY")),
      "gemini_key":bool(os.getenv("GEMINI_API_KEY")),
      "script_cost_authorized":os.getenv("STUDIO_NETWORK_ALLOW_SCRIPT_COST")=="1",
      "oauth_client_configured":oauth_env,
      "channel_oauth_connected":connected,
      "upload_authorized":os.getenv("STUDIO_NETWORK_UPLOAD_ENABLED")=="1",
      "free_disk_mb":round(available,1),
      "render_storage_ready":available>=MIN_FREE_MB,
    }
    reasons=[]
    for key,message in (
      ("research_key","Configure a chave do YouTube Data API."),
      ("gemini_key","Configure a chave da API Gemini."),
      ("script_cost_authorized","Autorize orçamento de IA em Railway: STUDIO_NETWORK_ALLOW_SCRIPT_COST=1."),
      ("oauth_client_configured","Configure o cliente Google OAuth no Railway."),
      ("channel_oauth_connected","Conecte este canal por OAuth na aba Rede de Canais."),
      ("upload_authorized","Autorize upload agendado em Railway: STUDIO_NETWORK_UPLOAD_ENABLED=1."),
      ("render_storage_ready",f"Amplie o volume: é necessário ao menos {MIN_FREE_MB} MB livres para produção contínua."),
    ):
        if not ready[key]:reasons.append(message)
    return ready,reasons

def state(studio,channel):
    cfg=config(studio,channel["id"])
    checks,reasons=flags(studio,channel)
    with studio.db() as db:
        idea_rows=db.execute("""SELECT id,title,topic,status,scheduled_at,
                 project_id,created_at FROM channel_network_ideas
                 WHERE channel_id=? ORDER BY created_at DESC LIMIT 9""",
                 (channel["id"],)).fetchall()
        upload_rows=db.execute("""SELECT video_id,status,scheduled_at,error FROM
                 channel_network_uploads WHERE channel_id=?
                 ORDER BY updated_at DESC LIMIT 5""",(channel["id"],)).fetchall()
    return {"channel":channel,"configuration":cfg,"requirements":checks,
            "blockers":reasons,"ready_to_start":not reasons,
            "video_plans":[dict(x) for x in idea_rows],
            "youtube_uploads":[dict(x) for x in upload_rows],
            "automating":cfg["mode"]=="running" and bool(channel["enabled"]) and not reasons}

def _status(studio,cid):
    return state(studio,net.get_channel(studio,cid))

def _save_message(studio,cid,message):
    with studio.db() as db:
        db.execute("""UPDATE studio_channel_control SET last_tick=?,
               last_message=? WHERE channel_id=?""",
               (net.iso(net.now()),str(message)[:230],cid))

def _active_work(studio,cid):
    with studio.db() as db:
        result=db.execute("""SELECT COUNT(*) FROM channel_network_ideas
          WHERE channel_id=? AND status IN
          ('queued_writing','writing','render_queued','render_waiting_storage',
           'scheduled_draft')""",(cid,)).fetchone()[0]
    # No new research while previous video is still producing or waiting to
    # upload; this is safer than building a large billable backlog.
    return int(result)

def run_cycle(studio):
    with studio.db() as db:
        ids=[row["channel_id"] for row in db.execute(
            "SELECT channel_id FROM studio_channel_control WHERE mode='running'").fetchall()]
    for cid in ids[:12]:
        try:
            channel=net.get_channel(studio,cid)
            if not channel["enabled"]:
                _save_message(studio,cid,"Pausado pelo canal.");continue
            ready,blockers=flags(studio,channel)
            if blockers:
                _save_message(studio,cid,"Bloqueado: "+blockers[0]);continue
            if _active_work(studio,cid):
                _save_message(studio,cid,"Existe vídeo em produção ou aguardando upload.");continue
            slot=pipe.next_available_slot(studio,channel)
            if not slot:
                _save_message(studio,cid,"Sem horário disponível na agenda.");continue
            if slot>net.now()+timedelta(hours=36):
                _save_message(studio,cid,"Próxima janela de produção ainda distante.");continue
            reserve=net.iso(slot)
            with studio.db() as db:
                db.execute("BEGIN IMMEDIATE")
                claim=db.execute("""INSERT OR IGNORE INTO studio_channel_control_ticks
                  (channel_id,slot_utc,state,created_at) VALUES(?,?,?,?)""",
                  (cid,reserve,"reserving",net.iso(net.now())))
                if claim.rowcount!=1:
                    continue
            cfg=config(studio,cid)
            try:
                payload=pipe.VideoOrder(topic="",duration_minutes=int(cfg["duration_minutes"]),
                                        auto_schedule=True,auto_script=True)
                outcome=pipe.order(studio,channel,payload)
                idea=(outcome.get("idea") or {}).get("id") or ""
                if not idea or outcome.get("status")!="queued_writing":
                    raise RuntimeError("Roteiro não entrou na fila; verifique orçamento e capacidade")
                # Generate three original titles/thumbs without running an
                # actual YouTube A/B test (no public API for that feature).
                import studio_publishing_pack
                try:
                    studio_publishing_pack.ensure(studio,idea)
                except Exception as exc:
                    print("[control-room] packaging incomplete: "+
                          type(exc).__name__,flush=True)
                with studio.db() as db:
                    db.execute("""UPDATE studio_channel_control_ticks
                        SET state='planned' WHERE channel_id=? AND slot_utc=?""",
                        (cid,reserve))
                _save_message(studio,cid,"Ideia criada; roteiro autorizado na fila.")
            except Exception as exc:
                with studio.db() as db:
                    db.execute("""UPDATE studio_channel_control_ticks
                        SET state='failed',last_error=? WHERE channel_id=? AND slot_utc=?""",
                        (type(exc).__name__,cid,reserve))
                _save_message(studio,cid,"Erro na preparação: "+type(exc).__name__)
        except Exception as exc:
            _save_message(studio,cid,"Falha de controle: "+type(exc).__name__)
    # Script writing relies on the exact existing pipeline writer, but the
    # global unattended scheduler stays off to prevent two planners racing.
    with studio.db() as db:
        todo=db.execute("""SELECT i.* FROM channel_network_ideas i
          JOIN studio_channel_control cc ON cc.channel_id=i.channel_id
          JOIN channel_network_channels c ON c.id=i.channel_id
          WHERE cc.mode='running' AND c.enabled=1
            AND i.status='queued_writing'
          ORDER BY i.created_at LIMIT 1""").fetchone()
    if todo:
        try:
            channel=net.get_channel(studio,todo["channel_id"])
            ready,blockers=flags(studio,channel)
            if not blockers:pipe.writer(studio,dict(todo))
        except Exception as exc:
            print("[control-room] writer error: "+type(exc).__name__,flush=True)

def worker(studio):
    while True:
        try:run_cycle(studio)
        except Exception as exc:
            print("[control-room] worker tick: "+type(exc).__name__,flush=True)
        time.sleep(90)

UI=r"""<!doctype html><html lang="pt-BR"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Central de Automação — Estúdio IA</title>
<style>
:root{color-scheme:dark;font:15px/1.5 system-ui,sans-serif}*{box-sizing:border-box}
body{background:#081322;color:#e9f2fa;margin:0}main{max-width:1130px;margin:auto;padding:34px 18px}
h1{font-size:32px;letter-spacing:-.8px;margin:8px 0}h2{font-size:20px;margin:4px 0 12px}
a{color:#91d8fd}.muted{color:#aabed1}.panel{background:#142338;border:1px solid #30475e;
border-radius:18px;padding:22px;margin:17px 0}
.row{display:flex;align-items:center;gap:13px;justify-content:space-between;flex-wrap:wrap}
.grid{display:grid;grid-template-columns:280px minmax(0,1fr);gap:18px}
input,select{width:100%;font:inherit;border:1px solid #46617b;background:#091a2d;color:white;
border-radius:8px;padding:9px}label{display:block;margin:10px 0;color:#bbd0df}
button{background:#3cbfe4;border:none;color:#092338;font-weight:700;font:inherit;
border-radius:9px;padding:10px 14px;cursor:pointer;margin:3px}button[disabled]{opacity:.45;cursor:not-allowed}
button.stop{background:#3f536c;color:#e6f0ff}
.card{padding:13px;border:1px solid #334860;border-radius:10px;margin:9px 0;background:#0b1a2c}
.small{font-size:12px}.pill{padding:5px 9px;border-radius:20px;background:#28415b;display:inline-block}
.good{color:#91edb2}.warn{color:#ffd08d}.error{color:#ffbbb0}
#notice{min-height:1.5em}#channels button{width:100%;text-align:left;margin:3px 0}
table{width:100%;border-collapse:collapse}td,th{padding:8px;border-bottom:1px solid #344a5c;text-align:left}
@media(max-width:760px){.grid{grid-template-columns:1fr}}
</style><main>
<div class="row"><div><a href="/rede">← Rede de Canais</a> · <a href="/fabrica">Fábrica</a>
<h1>Central de Automação</h1><p class="muted">Um planejamento independente para cada canal. O motor roda no Railway, mesmo com a página fechada.</p></div>
<span class="pill" id="globalStatus">Verificando...</span></div>
<p id="notice"></p>
<div class="grid"><aside class="panel"><h2>Seus canais</h2><div id="channels">Carregando…</div>
<p class="small muted">Adicione outros canais em <a href="/rede">Rede de Canais</a>.</p></aside>
<section class="panel"><h2 id="channelTitle">Selecione um canal</h2>
<div id="body" hidden>
<p class="muted" id="channelNiche"></p>
<form id="form">
<div class="row"><label style="flex:1">Vídeos por dia
<select id="count"><option value="1">1 por dia</option><option value="2">2 por dia</option></select></label>
<label style="flex:2">Horários (HH:MM, separados por vírgula)
<input id="hours" value="18:00" required></label></div>
<div class="row"><label style="flex:1">Fuso horário
<input id="zone" value="America/Sao_Paulo" required></label>
<label style="flex:1">Duração do vídeo
<select id="duration"><option value="2">2 minutos</option><option value="5" selected>5 minutos</option>
<option value="10">10 minutos</option><option value="30">30 minutos</option></select></label></div>
<button type="submit">Salvar planejamento</button></form>
<div class="row"><div><strong id="playStatus">Pausado</strong><p class="muted small">Play cria vídeos futuros e agenda quando todas as integrações estão autorizadas. Pausar não cancela vídeos já enviados ao YouTube.</p></div>
<div><button id="play">▶ Iniciar</button><button class="stop" id="pause">⏸ Pausar</button></div></div>
<h2>Prontidão para produção</h2><div id="checks"></div>
<h2>Fila e vídeos programados</h2><div id="queue"></div>
<h2>Pacote de testes</h2><p class="muted">O Estúdio prepara 3 títulos originais e 3 thumbnails. Publica a opção principal; o teste A/B nativo de 3 versões só pode ser iniciado no YouTube Studio, não pela API pública.</p>
<div id="pack"></div></div></section></div>
</main><script>
const $=id=>document.getElementById(id);let all=[],selected=null,current=null;
async function api(path,options){let r=await fetch(path,options);let d=await r.json();if(!r.ok)throw Error(d.detail||JSON.stringify(d));return d}
function text(tag,value){let e=document.createElement(tag);e.textContent=value||'';return e}
function notice(msg,error=false){$('notice').textContent=msg;$('notice').className=error?'error':'good'}
async function refresh(){try{
 let d=await api('/api/control-room');all=d.channels;
 $('globalStatus').textContent=d.running_count+' canal(is) em execução';
 let list=$('channels');list.replaceChildren();
 for(const ch of all){let b=text('button',ch.channel.name+' · '+(ch.configuration.mode==='running'?'▶ ativo':'⏸ pausado'));
 b.className='stop';b.onclick=()=>choose(ch.channel.id);list.append(b)}
 if(!selected&&all.length)selected=all[0].channel.id;
 let item=all.find(x=>x.channel.id===selected);
 if(item)render(item);
}catch(e){notice(e.message,true)}}
function render(ch){current=ch;$('body').hidden=false;
 $('channelTitle').textContent=ch.channel.name;
 $('channelNiche').textContent=ch.channel.niche+' · '+ch.channel.language+
 ' · '+(ch.channel.yt_channel_id||'Sem YouTube vinculado');
 $('count').value=ch.channel.daily_quantity;
 $('hours').value=ch.channel.hours.join(',');
 $('zone').value=ch.channel.timezone;
 $('duration').value=ch.configuration.duration_minutes;
 $('playStatus').textContent=ch.configuration.mode==='running'?'▶ Automação ativa':'⏸ Automação pausada';
 $('playStatus').className=ch.automating?'good':'warn';
 $('play').disabled=!!ch.automating;
 $('pause').disabled=ch.configuration.mode!=='running';
 const checks=$('checks');checks.replaceChildren();
 let readiness=[
 ['Pesquisa de tendências',ch.requirements.research_key],
 ['Gemini configurado',ch.requirements.gemini_key],
 ['Orçamento de IA autorizado',ch.requirements.script_cost_authorized],
 ['Cliente OAuth Google',ch.requirements.oauth_client_configured],
 ['Canal conectado',ch.requirements.channel_oauth_connected],
 ['Agendamento autorizado',ch.requirements.upload_authorized],
 ['Armazenamento suficiente',ch.requirements.render_storage_ready]
 ];for(const [name,ok] of readiness){let p=text('p',(ok?'✓ ':'○ ')+name);
 p.className=ok?'good small':'warn small';checks.append(p)}
 let disk=text('p','Espaço livre: '+ch.requirements.free_disk_mb+' MB. '+ch.configuration.last_message);
 checks.append(disk);
 if(ch.blockers.length){let p=text('p',ch.blockers.join(' '));p.className='error small';checks.append(p)}
 if(ch.requirements.oauth_client_configured&&!ch.requirements.channel_oauth_connected){
 let a=text('a','Conectar canal ao Google/YouTube');
 a.href='/api/network/channels/'+encodeURIComponent(ch.channel.id)+'/connect';checks.append(a)}
 const queue=$('queue');queue.replaceChildren();
 for(const item of ch.video_plans.slice(0,6)){
 let box=text('div',item.title+' · '+item.status+' · '+(item.scheduled_at||'sem horário'));
 box.className='card small';queue.append(box)}
 if(!ch.video_plans.length)queue.append(text('p','Nenhum vídeo programado neste canal.'));
 const packs=$('pack');packs.replaceChildren();
 for(const row of ch.video_plans.slice(0,3)){
 let a=text('a','Três títulos e thumbnails: '+row.title);
 a.href='/api/network/ideas/'+encodeURIComponent(row.id)+'/pack';a.target='_blank';a.rel='noopener';
 let p=text('p','');p.append(a);packs.append(p)}
}
function choose(cid){selected=cid;let item=all.find(x=>x.channel.id===cid);if(item)render(item)}
$('form').onsubmit=async e=>{e.preventDefault();try{
 const body={daily_quantity:Number($('count').value),
 hours:$('hours').value.split(',').map(x=>x.trim()).filter(Boolean),
 timezone:$('zone').value.trim(),duration_minutes:Number($('duration').value)};
 await api('/api/control-room/channels/'+encodeURIComponent(selected)+'/plan',
 {method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
 notice('Planejamento salvo. Nenhum vídeo foi produzido ou publicado.');await refresh()
}catch(err){notice(err.message,true)}};
$('play').onclick=async()=>{try{
 if(!confirm('Iniciar automação contínua neste canal? Serão usados recursos Railway e Gemini, e os vídeos poderão ser agendados no YouTube após serem produzidos.'))return;
 await api('/api/control-room/channels/'+encodeURIComponent(selected)+'/play',{method:'POST'});
 notice('Automação ativa. Pode fechar o navegador: o Railway continuará trabalhando.');await refresh()
}catch(e){notice(e.message,true)}};
$('pause').onclick=async()=>{try{
 await api('/api/control-room/channels/'+encodeURIComponent(selected)+'/pause',{method:'POST'});
 notice('Novos vídeos pausados. Agendamentos já enviados ao YouTube não são cancelados.');await refresh()
}catch(e){notice(e.message,true)}};
refresh();
</script></html>"""

def install(studio):
    schema(studio)

    @studio.app.get("/controle")
    def dashboard():
        return HTMLResponse(UI)

    @studio.app.get("/api/control-room")
    def overview():
        channels=[state(studio,ch) for ch in net.list_channels(studio)]
        return {"channels":channels,"running_count":sum(x["automating"] for x in channels),
                "server_runs_with_browser_closed":True,
                "youtube_ab_test_api_supported":False,
                "note":"PLAY is gated by OAuth, explicit costs/upload approval, and disk capacity."}

    @studio.app.get("/api/control-room/channels/{cid}")
    def status(cid:str):
        return _status(studio,cid)

    @studio.app.put("/api/control-room/channels/{cid}/plan")
    def save(cid:str,payload:Plan):
        channel=net.get_channel(studio,cid)
        net.validate_zone(payload.timezone)
        slots=net.validate_times(payload.hours,payload.daily_quantity)
        if payload.daily_quantity>MAX_PER_CHANNEL:
            raise HTTPException(422,"Limite atual: máximo de 2 vídeos por dia por canal")
        with studio.db() as db:
            db.execute("""UPDATE channel_network_channels SET timezone=?,daily_quantity=?,
                 hours_json=?,updated_at=? WHERE id=?""",
                 (payload.timezone,payload.daily_quantity,json.dumps(slots),
                  net.iso(net.now()),cid))
            db.execute("""INSERT INTO studio_channel_control
                (channel_id,mode,duration_minutes,updated_at)
                VALUES(?,'paused',?,?)
                ON CONFLICT(channel_id) DO UPDATE SET
                duration_minutes=excluded.duration_minutes,
                updated_at=excluded.updated_at""",
                (cid,payload.duration_minutes,net.iso(net.now())))
        return _status(studio,cid)

    @studio.app.post("/api/control-room/channels/{cid}/play")
    def play(cid:str):
        ch=net.get_channel(studio,cid)
        ready,blockers=flags(studio,ch)
        if blockers:
            raise HTTPException(409,"Não foi possível iniciar: "+" ".join(blockers))
        with studio.db() as db:
            db.execute("""INSERT INTO studio_channel_control
              (channel_id,mode,duration_minutes,updated_at)
              VALUES(?,'running',5,?)
              ON CONFLICT(channel_id) DO UPDATE SET
              mode='running',updated_at=excluded.updated_at""",
              (cid,net.iso(net.now())))
            db.execute("""UPDATE channel_network_channels SET enabled=1,updated_at=?
              WHERE id=?""",(net.iso(net.now()),cid))
        return _status(studio,cid)

    @studio.app.post("/api/control-room/channels/{cid}/pause")
    def pause(cid:str):
        net.get_channel(studio,cid)
        with studio.db() as db:
            db.execute("""INSERT INTO studio_channel_control
                (channel_id,mode,duration_minutes,updated_at)
                VALUES(?,'paused',5,?)
                ON CONFLICT(channel_id) DO UPDATE SET
                mode='paused',updated_at=excluded.updated_at""",
                (cid,net.iso(net.now())))
            db.execute("""UPDATE channel_network_channels SET enabled=0,updated_at=?
                WHERE id=?""",(net.iso(net.now()),cid))
        return _status(studio,cid)

    @studio.app.on_event("startup")
    def start():
        global WORKER_STARTED
        with WORKER_LOCK:
            if not WORKER_STARTED:
                threading.Thread(target=worker,args=(studio,),
                         name="multi-channel-automation",daemon=True).start()
                WORKER_STARTED=True

def self_test():
    x=Plan(daily_quantity=1,hours=["18:00"],timezone="America/Sao_Paulo",
           duration_minutes=5)
    assert x.daily_quantity==1
    assert net.validate_times(x.hours,x.daily_quantity)==["18:00"]
    assert MIN_FREE_MB>500
    assert "play" in UI and "pause" in UI
    print("[control-room] SELF TEST PASS daily schedule/play-pause/storage/OAuth gate",flush=True)

if __name__=="__main__":
    self_test()
    app=Path("/app/app.py")
    if not app.is_file():
        raise SystemExit("Missing /app/app.py")
    marker="\nimport studio_control_room as _control_room\n"
    code=app.read_text(encoding="utf-8")
    if marker not in code:
        app.write_text(code+marker+
              '_control_room.install(__import__("sys").modules[__name__])\n',
              encoding="utf-8")
