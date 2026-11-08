"""One-time real YouTube Data API verification for the connected pilot channel.

At startup (only after YOUTUBE_DATA_API_KEY is configured), perform ONE official
96-hour search plus a videos.list batch. Save success/failure, count and ranked
references. Optionally create one original Spanish pilot packaging draft, never
queue a paid script, render a video, or post to YouTube.

Never log or return API keys, OAuth credentials or platform secrets.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from datetime import timedelta
from pathlib import Path

from fastapi import HTTPException

import studio_channel_network as net

_PROBE_VERSION = "youtube-live-96h-v1"


def schema(studio):
    with studio.db() as db:
        db.execute("""CREATE TABLE IF NOT EXISTS channel_network_api_probes(
            id TEXT PRIMARY KEY,
            channel_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            completed_at TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL,
            result_count INTEGER NOT NULL DEFAULT 0,
            best_video_title TEXT NOT NULL DEFAULT '',
            best_video_url TEXT NOT NULL DEFAULT '',
            idea_id TEXT NOT NULL DEFAULT '',
            error TEXT NOT NULL DEFAULT ''
        )""")


def candidate_channel(studio):
    channels = net.list_channels(studio)
    if not channels:
        return None
    # The initial owner pilot is Spanish; never choose a random unrelated
    # channel if multiple are available.
    spanish = [x for x in channels if x["language"] == "es"]
    return (spanish or channels)[0]


def probe_key(channel_id):
    key = os.environ.get("YOUTUBE_DATA_API_KEY", "")
    # A digest is only for idempotency. Never reveal even this digest to clients.
    digest = hashlib.sha256(key.encode()).hexdigest()[:12]
    return _PROBE_VERSION + ":" + channel_id + ":" + digest


def state(studio, cid):
    with studio.db() as db:
        row = db.execute("""SELECT channel_id,created_at,completed_at,status,result_count,
                            best_video_title,best_video_url,idea_id,error
                            FROM channel_network_api_probes
                            WHERE channel_id=? ORDER BY created_at DESC LIMIT 1""",
                         (cid,)).fetchone()
    return dict(row) if row else None


def run(studio, force=False):
    if not os.getenv("YOUTUBE_DATA_API_KEY"):
        raise HTTPException(409, "YOUTUBE_DATA_API_KEY ainda não está configurada")
    channel = candidate_channel(studio)
    if not channel:
        raise HTTPException(409, "Cadastre pelo menos um canal para testar a pesquisa")
    k = probe_key(channel["id"])
    with studio.db() as db:
        previous = db.execute("SELECT * FROM channel_network_api_probes WHERE id=?", (k,)).fetchone()
        if previous and not force:
            # Do not repeat a search every deploy; errors can be rechecked
            # explicitly via the UI only.
            if previous["status"] == "ok":
                return state(studio, channel["id"])
            if previous["status"] in ("started", "failed"):
                return state(studio, channel["id"])
        db.execute("""INSERT OR REPLACE INTO channel_network_api_probes
            (id,channel_id,created_at,status) VALUES (?,?,?,?)""",
                   (k,channel["id"],net.iso(net.now()),"started"))
    try:
        # This clone excludes extra search_terms: ONE search.list (~100 quota
        # units) + ONE videos.list (1 unit), not three expensive searches.
        test_channel = {**channel, "niche": "estoicismo",
                        "search_terms": []}
        snapshot = net._report_snapshot(studio, test_channel, "estoicismo")
        results = snapshot.get("results") or []
        if not results:
            raise RuntimeError("Sem resultados públicos recentes para 'estoicismo'")
        best = results[0]
        # Create an editorially distinct concept, not a rewritten competitor title.
        idea_id = ""
        if channel["language"] == "es":
            # Idempotent bootstrapping: only one original test idea per source.
            with studio.db() as db:
                exists = db.execute("""SELECT id FROM channel_network_ideas
                    WHERE channel_id=? AND topic=? LIMIT 1""",
                    (channel["id"], "cómo poner límites sin perder la calma")).fetchone()
            if exists:
                idea_id = exists["id"]
            else:
                try:
                    pkg = net.create_idea(
                        studio, channel, "cómo poner límites sin perder la calma", snapshot
                    )
                    idea_id = pkg["id"]
                except (HTTPException, OSError, ValueError):
                    pass
        with studio.db() as db:
            db.execute("""UPDATE channel_network_api_probes SET status='ok',
                completed_at=?,result_count=?,best_video_title=?,
                best_video_url=?,idea_id=?,error='' WHERE id=?""",
                (net.iso(net.now()),len(results),
                 best.get("title","")[:150],best.get("url","")[:200],
                 idea_id,k))
        print("[network-api-probe] VERIFIED YouTube 96h search. results=%d; "
              "top_recent_title=%s; pilot_idea=%s; uploads=disabled" %
              (len(results),best.get("title","")[:120],bool(idea_id)),flush=True)
    except Exception as exc:
        message = str(exc)
        if isinstance(exc, HTTPException):
            message = str(exc.detail)
        # We never include upstream response bodies (which could repeat URLs
        # with a key). Only an internal diagnostic message.
        message = message[:240] if "key=" not in message else "Google API falhou"
        with studio.db() as db:
            db.execute("""UPDATE channel_network_api_probes
                SET status='failed',completed_at=?,error=?
                WHERE id=?""",
                (net.iso(net.now()),message,k))
        print("[network-api-probe] FAILED: "+type(exc).__name__+"; "+message,flush=True)
    return state(studio,channel["id"])


def install(studio):
    schema(studio)

    @studio.app.get("/api/network/live-test")
    def get_live_test():
        ch = candidate_channel(studio)
        return {"configured":bool(os.getenv("YOUTUBE_DATA_API_KEY")),
                "channel":ch["name"] if ch else None,
                "latest":state(studio,ch["id"]) if ch else None,
                "window_hours":96,
                "uploads_enabled":False,
                "note":"Teste real com quota da API oficial. Sem scripts pagos nem uploads."}

    @studio.app.post("/api/network/live-test")
    def recheck():
        return {"latest":run(studio,force=True)}

    @studio.app.on_event("startup")
    def startup_verification():
        if os.getenv("YOUTUBE_DATA_API_KEY"):
            def job():
                time.sleep(3)
                try:
                    run(studio)
                except Exception as exc:
                    print("[network-api-probe] unavailable: "+type(exc).__name__,flush=True)
            threading.Thread(target=job,name="youtube-live-api-check",daemon=True).start()


if __name__ == "__main__":
    root=Path("/app/app.py")
    if not root.is_file():
        raise SystemExit("Missing studio app.py")
    marker="\nimport studio_channel_api_probe as _channel_api_probe\n"
    data=root.read_text(encoding="utf-8")
    if marker not in data:
        root.write_text(data+marker+
                        '_channel_api_probe.install(__import__("sys").modules[__name__])\n',
                        encoding="utf-8")
