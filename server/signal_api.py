"""Tiny browser-to-browser accept/reject demo with presence heartbeats.

This is intentionally separate from dating eligibility and proximity matching. A
browser is online while it refreshes its short-lived presence lease. The recipient
can poll its latest signal; a laptop-side band bridge turns it into a light.
"""
from __future__ import annotations

import threading
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field

PRESENCE_TTL_S = 20
SIGNAL_TTL_S = 120


class PresenceIn(BaseModel):
    session_id: str = Field(min_length=8, max_length=64)


class SignalIn(BaseModel):
    to: str = Field(min_length=1, max_length=128, description="Recipient user ID or exact display name")
    answer: Literal["accept", "reject"]


def register(app: FastAPI, db, user_dep) -> None:
    # This demo state is intentionally ephemeral and scoped to one Render instance.
    # Heartbeats refresh every 6 seconds; after a restart all clients simply reappear.
    presence: dict[tuple[str, str], float] = {}  # (user id, browser tab session) -> last heartbeat
    latest: dict[str, dict] = {}                  # recipient user id -> latest signal
    counter = {"seq": 0}
    lock = threading.Lock()

    def online_users(now: float) -> list[dict]:
        with lock:
            for key, seen_at in list(presence.items()):
                if now - seen_at > PRESENCE_TTL_S:
                    del presence[key]
            ids = {uid for (uid, _session), seen_at in presence.items()
                   if now - seen_at <= PRESENCE_TTL_S}
        if not ids:
            return []
        placeholders = ",".join("?" for _ in ids)
        rows = db.read(f"SELECT id, display_name FROM users WHERE id IN ({placeholders}) ORDER BY display_name",
                       tuple(ids))
        return [{"id": row["id"], "name": row["display_name"], "online": True} for row in rows]

    @app.get("/api/connect/me")
    def connect_me(uid: str = Depends(user_dep)):
        row = db.one("SELECT id, display_name FROM users WHERE id = ?", (uid,))
        return {"id": row["id"], "name": row["display_name"]}

    @app.post("/api/connect/heartbeat")
    def heartbeat(body: PresenceIn, uid: str = Depends(user_dep)):
        now = db.now()
        with lock:
            presence[(uid, body.session_id)] = now
        row = db.one("SELECT id, display_name FROM users WHERE id = ?", (uid,))
        others = [person for person in online_users(now) if person["id"] != uid]
        return {"me": {"id": row["id"], "name": row["display_name"], "online": True},
                "online_clients": others, "presence_ttl_seconds": PRESENCE_TTL_S,
                "server_time": now}

    @app.delete("/api/connect/heartbeat")
    def leave(session_id: str, uid: str = Depends(user_dep)):
        with lock:
            presence.pop((uid, session_id), None)
        return {"ok": True}

    @app.get("/api/connect/people")
    def people(uid: str = Depends(user_dep)):
        now = db.now()
        return {"online_clients": [person for person in online_users(now) if person["id"] != uid],
                "server_time": now}

    @app.post("/api/signal")
    def send_signal(body: SignalIn, uid: str = Depends(user_dep)):
        target = db.one("SELECT id, display_name FROM users WHERE id = ?", (body.to,))
        if target is None:
            target = db.one("SELECT id, display_name FROM users WHERE LOWER(display_name) = ?",
                            (body.to.strip().lower(),))
        if target is None:
            raise HTTPException(404, "no such person")
        if target["id"] == uid:
            raise HTTPException(409, "cannot signal yourself")
        sender = db.one("SELECT display_name FROM users WHERE id = ?", (uid,))
        now = db.now()
        with lock:
            counter["seq"] += 1
            event = {"seq": counter["seq"], "answer": body.answer, "from": sender["display_name"],
                     "from_id": uid, "to": target["id"], "created_at": now,
                     "expires_at": now + SIGNAL_TTL_S}
            latest[target["id"]] = event
        return {"ok": True, "seq": event["seq"], "to": target["id"], "expires_in_seconds": SIGNAL_TTL_S}

    @app.get("/api/signal")
    def read_signal(uid: str = Depends(user_dep)):
        now = db.now()
        with lock:
            signal = latest.get(uid)
            if signal is not None and signal["expires_at"] <= now:
                latest.pop(uid, None)
                signal = None
            else:
                signal = dict(signal) if signal else None
        if signal is not None:
            signal.pop("expires_at", None)
        return {"signal": signal, "server_time": now}
