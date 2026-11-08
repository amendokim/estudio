"""One-time read-only stock relevance check for universal scene director.

Uses at most three Pixabay videos API queries against the actual Spanish pilot
storyboard. Tests metadata relevance, not footage content. Does not download
media, touch the project, render, call Gemini or publish anywhere.
"""
from __future__ import annotations
import json
import os
import threading
from datetime import datetime,timezone
from pathlib import Path

def run(studio):
    import httpx
    import studio_semantic_media as sem
    with studio.db() as db:
        db.execute("""CREATE TABLE IF NOT EXISTS studio_semantic_stock_smoke(
         id TEXT PRIMARY KEY,status TEXT NOT NULL,created_at TEXT NOT NULL,
         query_count INTEGER NOT NULL DEFAULT 0,
         eligible_count INTEGER NOT NULL DEFAULT 0,
         rejected_count INTEGER NOT NULL DEFAULT 0,
         details TEXT NOT NULL DEFAULT '')""")
        candidate=db.execute("""SELECT project_id FROM channel_network_ideas
           WHERE project_id!='' ORDER BY created_at DESC LIMIT 1""").fetchone()
        if not candidate:return
        pid=candidate["project_id"]
        cursor=db.execute("""INSERT OR IGNORE INTO studio_semantic_stock_smoke
          (id,status,created_at) VALUES (?,?,?)""",
          ("pixabay-real-tags-v1","checking",datetime.now(timezone.utc).isoformat()))
        if cursor.rowcount!=1:return
    status="success";queries=0;matched=0;rejected=0;detail=""
    try:
        project=studio.get_project(pid)
        plan,_=sem.load_plan(studio,project,write=False,allow_ai=False)
        scenes=plan.get("scenes") or []
        # One representative scene from the beginning, middle, and final act.
        sample=[scenes[i] for i in (0,len(scenes)//2,len(scenes)-1) if scenes]
        api_key=os.environ.get("PIXABAY_API_KEY","")
        if not api_key:
            raise RuntimeError("Pixabay API key not configured")
        with httpx.Client(timeout=17,follow_redirects=False) as client:
            for shot in sample:
                if not shot.get("queries"):continue
                query=shot["queries"][0]
                response=client.get("https://pixabay.com/api/videos/",
                    params={"key":api_key,"q":query,"lang":"en",
                            "per_page":20,"safesearch":"true"})
                response.raise_for_status()
                queries+=1
                hits=response.json().get("hits") or []
                scores=[sem._relevant_candidate(shot,h.get("tags") or "") for h in hits]
                matched+=sum(score>=sem.MIN_SCORE for score in scores)
                rejected+=sum(score<sem.MIN_SCORE for score in scores)
        detail=("provider_calls="+str(queries)+"; relevant_candidates="+str(matched)
                +"; filtered_candidates="+str(rejected))
        print("[semantic-stock] VERIFIED "+detail+
              "; provider metadata evaluated; no file downloaded",flush=True)
    except Exception as exc:
        status="failed"
        detail=type(exc).__name__
        print("[semantic-stock] FAILED: "+detail+
              "; fallback graphics remain available",flush=True)
    with studio.db() as db:
        db.execute("""UPDATE studio_semantic_stock_smoke SET status=?,
           query_count=?,eligible_count=?,rejected_count=?,details=? WHERE id=?""",
           (status,queries,matched,rejected,detail,"pixabay-real-tags-v1"))

def install(studio):
    @studio.app.on_event("startup")
    def read_only_smoke():
        if os.getenv("PIXABAY_API_KEY"):
            threading.Thread(target=run,args=(studio,),
                             name="pixabay-semantic-stock-smoke",daemon=True).start()

    @studio.app.get("/api/editorial/semantic/stock-check")
    def status():
        with studio.db() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS studio_semantic_stock_smoke(
              id TEXT PRIMARY KEY,status TEXT NOT NULL,created_at TEXT NOT NULL,
              query_count INTEGER NOT NULL DEFAULT 0,
              eligible_count INTEGER NOT NULL DEFAULT 0,
              rejected_count INTEGER NOT NULL DEFAULT 0,
              details TEXT NOT NULL DEFAULT '')""")
            row=db.execute("SELECT * FROM studio_semantic_stock_smoke WHERE id=?",
                           ("pixabay-real-tags-v1",)).fetchone()
        return {"state":dict(row) if row else None,
                "does_not_verify_exact_visual_content":True,
                "note":"Consulta única de metadados de cenas reais, sem download ou render."}

if __name__=="__main__":
    app=Path("/app/app.py")
    if not app.exists():raise SystemExit("Missing app.py")
    marker="\nimport studio_semantic_stock_smoke as _stock_smoke\n"
    code=app.read_text(encoding="utf8")
    if marker not in code:
        app.write_text(code+marker+
                       '_stock_smoke.install(__import__("sys").modules[__name__])\n',
                       encoding="utf8")
