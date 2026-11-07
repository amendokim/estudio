"""One-time, safe bootstrap of an owner-connected channel and research idea.

User-specific metadata lives ONLY in Railway environment variables, never in
GitHub source code. Registration is idempotent, never enables publishing or
daily automation, and only seeds a draft thumbnail/idea.
"""
from __future__ import annotations

import json
import os
import re
import uuid
from pathlib import Path
from zoneinfo import ZoneInfo

def install(studio):
    name=os.getenv("STUDIO_SEED_CHANNEL_NAME","").strip()
    youtube_id=os.getenv("STUDIO_SEED_CHANNEL_YOUTUBE_ID","").strip()
    niche=os.getenv("STUDIO_SEED_CHANNEL_NICHE","").strip()
    lang=os.getenv("STUDIO_SEED_CHANNEL_LANGUAGE","es").strip()
    if not name or not youtube_id or not niche:
        return
    if (not re.fullmatch(r"UC[A-Za-z0-9_-]{22}",youtube_id) or
        lang not in ("es","pt","en") or len(name)>90 or len(niche)>120):
        print("[channel-bootstrap] Invalid channel metadata; skipped",flush=True)
        return
    zone=os.getenv("STUDIO_SEED_CHANNEL_TIMEZONE","America/Sao_Paulo")
    try:
        ZoneInfo(zone)
    except Exception:
        print("[channel-bootstrap] Invalid timezone; skipped",flush=True)
        return
    import studio_channel_network as net
    with studio.db() as c:
        row=c.execute("SELECT id FROM channel_network_channels WHERE yt_channel_id=?",
                      (youtube_id,)).fetchone()
        if not row:
            cid=str(uuid.uuid5(uuid.NAMESPACE_URL,"youtube:"+youtube_id))
            when=net.iso(net.now())
            c.execute("""INSERT OR IGNORE INTO channel_network_channels
                (id,name,niche,language,timezone,daily_quantity,hours_json,
                 search_terms_json,yt_channel_id,enabled,created_at,updated_at)
                 VALUES(?,?,?,?,?,?,?,?,?,0,?,?)""",
                 (cid,name,niche,lang,zone,1,json.dumps(["18:00"]),
                  json.dumps(["estoicismo","autocontrol","stoicism"]),youtube_id,when,when))
            print("[channel-bootstrap] Channel pilot registered with schedule DISABLED",
                  flush=True)
        else:
            cid=row["id"]
    topic=os.getenv("STUDIO_SEED_IDEA_TOPIC","").strip()
    if not topic:
        return
    if len(topic)>115:
        topic=topic[:115]
    with studio.db() as c:
        existing=c.execute("SELECT id FROM channel_network_ideas WHERE channel_id=?",
                           (cid,)).fetchone()
    if existing:
        return
    try:
        refs=json.loads(os.getenv("STUDIO_SEED_TREND_REFERENCES","[]"))
        if not isinstance(refs,list) or not (1<=len(refs)<=6):
            raise ValueError("Expected 1-6 seed research references")
        checked=os.getenv("STUDIO_SEED_TREND_CHECKED_AT","")
        results=[]
        for x in refs:
            if not isinstance(x,dict):
                continue
            vid=x.get("id")
            title=x.get("title")
            if (not re.fullmatch(r"[A-Za-z0-9_-]{11}",str(vid or "")) or
                not isinstance(title,str) or not (6<len(title)<155)):
                continue
            results.append({"title":title,"url":"https://www.youtube.com/watch?v="+vid,
                            "trend_score":float(x.get("estimated_vph") or 0),
                            "published_at":x.get("published_at")})
        if not results:
            return
        channel=net.get_channel(studio,cid)
        snapshot={"results":results}
        idea=net.create_idea(studio,channel,topic,snapshot)
        with studio.db() as c:
            data=json.loads(c.execute("SELECT analysis_json FROM channel_network_ideas WHERE id=?",
                                      (idea["id"],)).fetchone()["analysis_json"])
            data["seed_source"]="Pesquisa pontual pelo vidIQ, não é atualização automática no Railway"
            data["seed_checked_at"]=checked
            data["seed_mode"]="manual_first_test"
            c.execute("UPDATE channel_network_ideas SET analysis_json=? WHERE id=?",
                      (json.dumps(data,ensure_ascii=False),idea["id"]))
        print("[channel-bootstrap] Original Spanish draft and branded thumbnail created; "
              "no YouTube upload or automatic schedule",flush=True)
    except (ValueError,TypeError,KeyError,OSError) as exc:
        print("[channel-bootstrap] Idea seed skipped: "+type(exc).__name__,flush=True)

if __name__=="__main__":
    app=Path("/app/app.py")
    if not app.exists():
        raise SystemExit("Missing app.py")
    mark="\nimport studio_channel_bootstrap as _channel_bootstrap\n"
    text=app.read_text(encoding="utf-8")
    if mark not in text:
        app.write_text(text+mark+
                       '_channel_bootstrap.install(__import__("sys").modules[__name__])\n',
                       encoding="utf-8")
