"""Automated one-command documentary order pipeline, per-channel scheduling.

A user submits a channel + optional topic. It researches official YouTube stats,
chooses a distinct framing, saves a branded thumbnail and reserves a slot.
Script writing/rendering only activate when the owner opts in to a real,
budgeted LLM backend and sufficient worker storage. No new paid calls by default.
"""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from pydantic import BaseModel, Field

import studio_channel_network as network

WORKER_STARTED=False
WORKER_GUARD=threading.Lock()

class VideoOrder(BaseModel):
    topic: str = Field(default="", max_length=220)
    duration_minutes: int = Field(default=5, ge=1, le=60)
    auto_schedule: bool = True
    auto_script: bool = True

def auto_script_ready():
    return (bool(os.getenv("GEMINI_API_KEY")) and
            os.getenv("STUDIO_NETWORK_ALLOW_SCRIPT_COST")=="1")

def enabled():
    return (os.getenv("STUDIO_NETWORK_AUTOMATION_ENABLED")=="1" and
            auto_script_ready() and bool(os.getenv("YOUTUBE_DATA_API_KEY")))

def next_available_slot(studio, channel):
    z=ZoneInfo(channel["timezone"])
    current=datetime.now(timezone.utc)
    local=current.astimezone(z)
    for offset in range(15):
        day=local.date()+timedelta(days=offset)
        for hr in channel["hours"][:channel["daily_quantity"]]:
            hour,minute=map(int,hr.split(":"))
            moment=datetime(day.year,day.month,day.day,hour,minute,tzinfo=z)
            if moment.astimezone(timezone.utc)<=current+timedelta(hours=3):
                continue
            with studio.db() as c:
                slot=c.execute("SELECT id FROM channel_network_ideas WHERE channel_id=? AND scheduled_at=?",
                               (channel["id"],network.iso(moment))).fetchone()
                beginning=datetime(day.year,day.month,day.day,tzinfo=z).astimezone(timezone.utc)
                end=(datetime(day.year,day.month,day.day,tzinfo=z)+timedelta(days=1)).astimezone(timezone.utc)
                used=c.execute("SELECT COUNT(*) FROM channel_network_ideas WHERE channel_id=? AND scheduled_at>=? AND scheduled_at<?",
                               (channel["id"],network.iso(beginning),network.iso(end))).fetchone()[0]
            if slot or used>=channel["daily_quantity"]:
                continue
            return moment
    return None

def order(studio, channel, payload):
    topic=payload.topic.strip() or channel["niche"]
    if len(topic)<3:
        raise HTTPException(422,"Tema muito curto")
    data=network._report_snapshot(studio,channel,topic)
    package=network.create_idea(studio,channel,topic,data)
    idea_id=package["id"]
    reservation=None
    if payload.auto_schedule and channel["enabled"]:
        slot=next_available_slot(studio,channel)
        if slot:
            reservation=network.plan_schedule(studio,channel,idea_id,
                    slot.strftime("%Y-%m-%d"),slot.strftime("%H:%M"))
    state="idea_ready"
    if payload.auto_script:
        if auto_script_ready():
            with studio.db() as db:
                db.execute("UPDATE channel_network_ideas SET status='queued_writing',duration_minutes=? WHERE id=?",
                           (payload.duration_minutes,idea_id))
            state="queued_writing"
        else:
            with studio.db() as db:
                db.execute("UPDATE channel_network_ideas SET status='script_backend_missing',duration_minutes=? WHERE id=?",
                           (payload.duration_minutes,idea_id))
            state="script_backend_missing"
    return {"channel_id":channel["id"],"idea":package,
            "research_count":len(data["results"]),"scheduled_slot":reservation,
            "status":state,
            "script_costs_enabled":auto_script_ready(),
            "message":("Proposta original pronta. Para criar roteiros automaticamente: "
                       "configure GEMINI_API_KEY e habilite STUDIO_NETWORK_ALLOW_SCRIPT_COST=1."
                       if state=="script_backend_missing" else
                       "Pesquisa e proposta concluídas; trabalho criado sem publicar no YouTube.")}

def writer(studio,row):
    idea_id=row["id"]
    with studio.db() as db:
        db.execute("BEGIN IMMEDIATE")
        status=db.execute("SELECT status FROM channel_network_ideas WHERE id=?",(idea_id,)).fetchone()
        if not status or status["status"]!="queued_writing":
            return
        db.execute("UPDATE channel_network_ideas SET status='writing' WHERE id=?",(idea_id,))
    if not auto_script_ready():
        with studio.db() as db:
            db.execute("UPDATE channel_network_ideas SET status='script_backend_missing' WHERE id=?",(idea_id,))
        return
    import studio_factory
    # Explicit original perspective and factual validation, not copied phrasing.
    topic=("%s. Ângulo editorial ORIGINAL: %s. Conceito: %s. "
           "Referências de tendência são apenas sinais de interesse; não copie "
           "narrativa, título, thumbnail ou clipes de outros canais. "
           "Crie novas explicações e confira fatos com fontes primárias." %
           (row["topic"],row["angle"],row["hook"]))
    try:
        minutes=max(1,min(60,int(row.get("duration_minutes") or 5)))
        pid=studio_factory.generate_script(
            studio,topic,minutes,"16:9",title=row["title"])
        project=studio.get_project(pid)
        capacity=studio_factory.estimate(studio,project)
        if capacity["can_render_here"]:
            studio_factory.queue(studio,"autopilot",project_id=pid,title=row["title"],
                                 duration=minutes,ratio="16:9")
            new_state="render_queued"
        else:
            new_state="render_waiting_storage"
        with studio.db() as db:
            db.execute("UPDATE channel_network_ideas SET project_id=?,status=? WHERE id=?",
                       (pid,new_state,idea_id))
        print("[channel-pipeline] prepared project for channel; state="+new_state,flush=True)
    except Exception as exc:
        with studio.db() as db:
            db.execute("UPDATE channel_network_ideas SET status='writing_failed' WHERE id=?",
                       (idea_id,))
        print("[channel-pipeline] script creation failed: "+type(exc).__name__,flush=True)

def maintenance(studio):
    # Idempotent workflow; studio_factory handles the actual render job.
    with studio.db() as db:
        pending=db.execute("""SELECT id,project_id,scheduled_at FROM channel_network_ideas
                            WHERE status='render_queued' AND project_id!=''
                            ORDER BY created_at LIMIT 20""").fetchall()
    for row in pending:
        try:
            project=studio.get_project(row["project_id"])
            if project["status"]=="concluído":
                new_status="scheduled_draft" if row["scheduled_at"] else "rendered_unplanned"
            elif project["status"]=="erro":
                new_status="render_failed"
            else:
                continue
            with studio.db() as db:
                db.execute("UPDATE channel_network_ideas SET status=? WHERE id=?",
                           (new_status,row["id"]))
        except Exception:
            pass
    # When capacity becomes sufficient (after a volume upgrade), enqueue waiting
    # projects without regenerating (or paying again for) the scripts.
    import studio_factory
    with studio.db() as db:
        waiting=db.execute("""SELECT id,project_id,title FROM channel_network_ideas
                            WHERE status='render_waiting_storage' AND project_id!=''
                            ORDER BY created_at LIMIT 10""").fetchall()
    for row in waiting:
        try:
            project=studio.get_project(row["project_id"])
            if studio_factory.estimate(studio,project)["can_render_here"]:
                studio_factory.queue(studio,"autopilot",project_id=project["id"],title=row["title"],
                                     duration=int(project["duration_minutes"]))
                with studio.db() as db:
                    db.execute("UPDATE channel_network_ideas SET status='render_queued' WHERE id=?",(row["id"],))
        except Exception:
            pass

def fill_daily_schedule(studio):
    if not enabled():
        return
    # Daily cap across entire network to avoid bot-style upload floods.
    with studio.db() as db:
        count=db.execute("SELECT COUNT(*) FROM channel_network_ideas WHERE created_at>=?",
                         (network.iso(network.now()-timedelta(hours=24)),)).fetchone()[0]
    if count>=network.DAILY_NETWORK_PLAN_CAP:
        return
    for channel in network.list_channels(studio):
        if not channel["enabled"]:
            continue
        available=next_available_slot(studio,channel)
        if not available:
            continue
        # One plan per pass. Each loop waits 90 seconds, reducing API bursts.
        try:
            order(studio,channel,VideoOrder(topic="",duration_minutes=5,
                                           auto_schedule=True,auto_script=True))
        except Exception as exc:
            print("[channel-pipeline] planned search skipped: "+type(exc).__name__,flush=True)
        break

def loop(studio):
    while True:
        try:
            maintenance(studio)
            if enabled():
                with studio.db() as db:
                    row=db.execute("""SELECT id,topic,angle,title,hook,duration_minutes FROM channel_network_ideas
                                     WHERE status='queued_writing'
                                     ORDER BY created_at LIMIT 1""").fetchone()
                if row:
                    writer(studio,dict(row))
                else:
                    fill_daily_schedule(studio)
        except Exception as exc:
            print("[channel-pipeline] tick error: "+type(exc).__name__,flush=True)
        time.sleep(90)

def install(studio):
    @studio.app.post("/api/network/channels/{cid}/order")
    def video_order(cid:str,payload:VideoOrder):
        channel=network.get_channel(studio,cid)
        return order(studio,channel,payload)

    @studio.app.get("/api/network/pipeline-readiness")
    def readiness():
        return {
            "trend_api_ready":bool(os.getenv("YOUTUBE_DATA_API_KEY")),
            "script_backend_ready":bool(os.getenv("GEMINI_API_KEY")),
            "script_cost_opt_in":os.getenv("STUDIO_NETWORK_ALLOW_SCRIPT_COST")=="1",
            "automatic_daily_pipeline_active":enabled(),
            "daily_network_plan_limit":network.DAILY_NETWORK_PLAN_CAP,
            "rendering_requires_free_storage":True,
            "youtube_upload_separately_gated":True,
            "note":"Nenhum serviço pago é chamado até o operador habilitar explicitamente seu orçamento."
        }

    @studio.app.on_event("startup")
    def start_pipeline():
        global WORKER_STARTED
        with WORKER_GUARD:
            if not WORKER_STARTED:
                threading.Thread(target=loop,args=(studio,),name="channel-plan-worker",daemon=True).start()
                WORKER_STARTED=True

if __name__=="__main__":
    assert VideoOrder(topic="Ponte da Amizade").duration_minutes==5
    app=__import__("pathlib").Path("/app/app.py")
    if not app.exists():raise SystemExit("Missing app.py")
    marker="\nimport studio_channel_pipeline as _channel_pipeline\n"
    code=app.read_text(encoding="utf-8")
    if marker not in code:
        app.write_text(code+marker+
                       '_channel_pipeline.install(__import__("sys").modules[__name__])\n',
                       encoding="utf-8")
