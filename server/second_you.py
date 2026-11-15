"""Second You: agent-to-agent matching where every model call and all training stay on the phones.

Each phone runs its own Second You agent (SecondYouKit, Apple's on-device Foundation Models).
The two agents chat before their people meet. This server is deliberately thin:

  (a) it relays the agents' chat messages between the two phones. Messages are sealed on the
      phone (end-to-end); the server sees opaque base64 and keeps them in memory only until the
      other phone picks them up. Nothing from the relay is written to the database.
  (b) it receives each phone's five verdict numbers (three scenario scores, a clash flag, an
      invented flag) and combines them into a match decision. `decide` is a port of
      `AgentSide.evaluate` in second-you/SecondYouKit/Sources/SecondYouKit/Chemistry.swift.
  (c) it stores match state and outcomes (both said yes, date confirmed, went, second date,
      rating) so the match floor can be calibrated (`calibrated_floor`, a port of Feedback.swift).

It never receives training data: no own lines, transcripts, questionnaire answers, profile or
agent card. Request bodies that carry any of those are refused, and there is no route that
accepts them. No paid model API is called from here.

A Second You session hangs off an existing Vexo match (the hidden "someone compatible is
nearby" suggestion). When both people say yes here, the yes is forwarded to that match too.
"""
from __future__ import annotations

import base64
import binascii
import logging
import math
import threading
from collections import deque
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator

from . import lifecycle
from .db import dumps, loads, new_id

UI = Path(__file__).resolve().parent.parent / "ui"
log = logging.getLogger("vexo.second_you")

MATCH_FLOOR = 0.6            # AgentSide.matchFloor
CLASH_PENALTY = 0.35
TARGET_PRECISION = 0.6       # Feedback.targetPrecision
MIN_OUTCOMES = 20            # Feedback.minOutcomes
MIN_ABOVE = 5

RELAY_MAX_BYTES = 8192       # one sealed chat message
RELAY_MAX_QUEUED = 64        # per recipient; older undelivered messages are dropped
RELAY_TTL_S = 300.0

# Keys that would mean training data is leaving the phone. Refused anywhere in a body.
FORBIDDEN_KEYS = {
    "profile", "card", "agent_card", "agentcard", "lines", "own_lines", "transcript", "transcripts",
    "messages", "chat", "answers", "questionnaire", "dealbreakers", "values", "examples",
    "example_bank", "training", "training_data", "style", "dynamics", "vault", "plaintext", "text",
}
PRIVACY_MESSAGE = "Second You training data stays on the phone; the server only takes sealed messages and verdict numbers"

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS sy_sessions (
      id TEXT PRIMARY KEY,
      match_id TEXT NOT NULL UNIQUE,
      user_low TEXT NOT NULL,
      user_high TEXT NOT NULL,
      state TEXT NOT NULL CHECK (state IN ('TALKING','MATCH','NO_MATCH','PASSED','REVEALED','DATE_SET','DONE')),
      low_verdict TEXT,
      high_verdict TEXT,
      score REAL,
      lower_bound REAL,
      upper_bound REAL,
      floor REAL,
      matched INTEGER,
      clash INTEGER,
      invented INTEGER,
      low_accepted INTEGER,
      high_accepted INTEGER,
      low_date INTEGER NOT NULL DEFAULT 0,
      high_date INTEGER NOT NULL DEFAULT 0,
      created_at REAL NOT NULL,
      updated_at REAL NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS sy_outcomes (
      session_id TEXT NOT NULL,
      user_id TEXT NOT NULL,
      went INTEGER NOT NULL,
      second_date INTEGER NOT NULL,
      rating INTEGER,
      lower_bound REAL NOT NULL,
      created_at REAL NOT NULL,
      PRIMARY KEY (session_id, user_id)
    )""",
]


# ------------------------------------------------------------ decision math (pure)

def decide(mine: dict, theirs: dict, floor: float = MATCH_FLOOR) -> dict:
    """Combine both phones' verdicts. Each verdict: {"scores": [3 floats], "clash": bool, "invented": bool}.

    Per scenario: average of both phones. Then mean, sample sd, 0.35 penalty on a clash,
    95% interval; a match needs lower bound >= floor, no clash and nothing invented.
    """
    a, b = list(mine["scores"]), list(theirs["scores"])
    if len(a) != 3 or len(b) != 3:
        raise ValueError("each phone sends exactly three scenario scores")
    per = [(x + y) / 2 for x, y in zip(a, b)]
    n = len(per)
    mean = sum(per) / n
    sd = math.sqrt(sum((p - mean) ** 2 for p in per) / max(1, n - 1))
    clash = bool(mine["clash"] or theirs["clash"])
    invented = bool(mine["invented"] or theirs["invented"])
    penalty = CLASH_PENALTY if clash else 1.0
    score = mean * penalty
    half = 1.96 * sd / math.sqrt(n) * penalty
    lo, hi = max(0.0, score - half), min(1.0, score + half)
    return {"score": score, "interval": [lo, hi], "floor": floor, "clash": clash, "invented": invented,
            "match": lo >= floor and not clash and not invented}


def calibrated_floor(outcomes: list[tuple[float, bool]], default: float = MATCH_FLOOR) -> float:
    """Lowest lower bound at which at least 60% of past matches led to a second date (Feedback.swift)."""
    if len(outcomes) < MIN_OUTCOMES:
        return default
    for thr in sorted({lb for lb, _ in outcomes}):
        above = [second for lb, second in outcomes if lb >= thr]
        if len(above) >= MIN_ABOVE and sum(above) / len(above) >= TARGET_PRECISION:
            return thr
    return default


# ------------------------------------------------------------ relay (memory only)

class Relay:
    """Mailboxes of sealed messages, one per (session, recipient side). Delivered once, then gone."""

    def __init__(self, clock):
        self.clock = clock
        self.lock = threading.Lock()
        self.boxes: dict[tuple[str, str], deque] = {}
        self.counts: dict[str, int] = {}          # how many messages passed through, for the progress bar

    def put(self, session_id: str, to_side: str, ciphertext: str) -> None:
        with self.lock:
            box = self.boxes.setdefault((session_id, to_side), deque(maxlen=RELAY_MAX_QUEUED))
            box.append((self.clock.now(), ciphertext))
            self.counts[session_id] = self.counts.get(session_id, 0) + 1

    def take(self, session_id: str, side: str) -> list[str]:
        now = self.clock.now()
        with self.lock:
            box = self.boxes.pop((session_id, side), deque())
            return [c for at, c in box if now - at <= RELAY_TTL_S]

    def count(self, session_id: str) -> int:
        with self.lock:
            return self.counts.get(session_id, 0)

    def drop(self, session_id: str) -> None:
        with self.lock:
            for key in [k for k in self.boxes if k[0] == session_id]:
                del self.boxes[key]

    def queued(self) -> int:
        with self.lock:
            return sum(len(b) for b in self.boxes.values())


# ------------------------------------------------------------ request bodies

def _find_forbidden(value) -> str | None:
    if isinstance(value, dict):
        for k, v in value.items():
            if str(k).lower().replace("-", "_") in FORBIDDEN_KEYS:
                return str(k)
            found = _find_forbidden(v)
            if found:
                return found
    elif isinstance(value, list):
        for v in value:
            found = _find_forbidden(v)
            if found:
                return found
    return None


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="before")
    @classmethod
    def _no_training_data(cls, data):
        key = _find_forbidden(data)
        if key:
            raise ValueError(f"{PRIVACY_MESSAGE} (refused field {key!r})")
        return data


class StartSession(_Strict):
    match_id: str = Field(min_length=8, max_length=64)


class Sealed(_Strict):
    ciphertext: str = Field(min_length=8, max_length=RELAY_MAX_BYTES * 2)

    @model_validator(mode="after")
    def _base64(self):
        try:
            raw = base64.b64decode(self.ciphertext, altchars=b"-_", validate=True)
        except (binascii.Error, ValueError):
            raise ValueError("ciphertext must be base64 of a message sealed on the phone")
        if len(raw) > RELAY_MAX_BYTES:
            raise ValueError("sealed message too large")
        return self


class Verdict(_Strict):
    scores: list[float] = Field(min_length=3, max_length=3)
    clash: bool
    invented: bool

    @model_validator(mode="after")
    def _range(self):
        if any(not (0.0 <= s <= 1.0) for s in self.scores):
            raise ValueError("scores are between 0 and 1")
        return self


class Answer(_Strict):
    accept: bool


class DateConfirm(_Strict):
    confirm: bool = True


class Outcome(_Strict):
    went: bool
    second_date: bool = False
    rating: int | None = Field(None, ge=1, le=5)


# ------------------------------------------------------------ routes

def ensure_schema(db) -> None:
    with db.tx() as conn:
        for statement in SCHEMA:
            conn.execute(statement)


def register(app: FastAPI, db, cfg, user) -> None:
    """Add the Second You routes. `user` is the app's bearer-token dependency for phone users."""
    ensure_schema(db)
    relay = Relay(db.clock)
    app.state.second_you_relay = relay

    def floor_now(conn) -> float:
        rows = conn.execute("SELECT lower_bound, second_date FROM sy_outcomes").fetchall()
        return calibrated_floor([(r["lower_bound"], bool(r["second_date"])) for r in rows])

    def load(conn, session_id: str, uid: str):
        s = conn.execute("SELECT * FROM sy_sessions WHERE id = ?", (session_id,)).fetchone()
        if s is None:
            raise HTTPException(404, "no such Second You session")
        if uid not in (s["user_low"], s["user_high"]):
            raise HTTPException(403, "not your session")
        return s, ("low" if uid == s["user_low"] else "high")

    def project(conn, s, uid: str) -> dict:
        me = "low" if uid == s["user_low"] else "high"
        them = "high" if me == "low" else "low"
        both_yes = bool(s[f"{me}_accepted"]) and bool(s[f"{them}_accepted"])
        out = {
            "id": s["id"], "match_id": s["match_id"], "state": s["state"],
            "goes_first": me == "low",
            "relayed": relay.count(s["id"]),
            "my_verdict_sent": s[f"{me}_verdict"] is not None,
            "their_verdict_sent": s[f"{them}_verdict"] is not None,
            "decision": None,
            "you_accepted": None if s[f"{me}_accepted"] is None else bool(s[f"{me}_accepted"]),
            "both_yes": both_yes,
            "counterpart": None,                       # hidden until both say yes
            "you_confirmed_date": bool(s[f"{me}_date"]),
            "date_confirmed": bool(s["low_date"]) and bool(s["high_date"]),
            "you_rated": conn.execute("SELECT 1 FROM sy_outcomes WHERE session_id = ? AND user_id = ?",
                                      (s["id"], uid)).fetchone() is not None,
        }
        if s["score"] is not None:
            out["decision"] = {"score": s["score"], "interval": [s["lower_bound"], s["upper_bound"]],
                               "floor": s["floor"], "match": bool(s["matched"]), "clash": bool(s["clash"]),
                               "invented": bool(s["invented"])}
        if both_yes:
            other = s["user_high"] if me == "low" else s["user_low"]
            u = conn.execute("SELECT display_name FROM users WHERE id = ?", (other,)).fetchone()
            out["counterpart"] = {"display_name": u["display_name"] if u else "Your match"}
        return out

    def touch(conn, s, now: float, **fields) -> None:
        fields["updated_at"] = now
        sets = ", ".join(f"{k} = ?" for k in fields)
        conn.execute(f"UPDATE sy_sessions SET {sets} WHERE id = ?", (*fields.values(), s["id"]))

    def reread(conn, session_id: str):
        return conn.execute("SELECT * FROM sy_sessions WHERE id = ?", (session_id,)).fetchone()

    # -- refusal: there is deliberately no way to upload a profile or train on the server
    for path in ("profile", "card", "train", "training-data", "transcripts", "questionnaire"):
        def refuse(_uid: str = Depends(user)):
            raise HTTPException(403, PRIVACY_MESSAGE)
        for method in ("POST", "PUT", "PATCH"):
            app.add_api_route(f"/api/second-you/{path}", refuse, methods=[method],
                              name=f"second_you_refuse_{path}_{method.lower()}")

    @app.get("/api/second-you/current")
    def current(uid: str = Depends(user)):
        """The session for the caller's active Vexo match, if the phones started one."""
        with db.lock:
            conn = db.conn
            row = conn.execute("SELECT match_id FROM active_members WHERE user_id = ?", (uid,)).fetchone()
            if row is None:
                return {"match_id": None, "session": None}
            s = conn.execute("SELECT * FROM sy_sessions WHERE match_id = ?", (row["match_id"],)).fetchone()
            return {"match_id": row["match_id"], "session": project(conn, s, uid) if s else None}

    @app.post("/api/second-you/sessions")
    def start(body: StartSession, uid: str = Depends(user)):
        """Start (or rejoin) the agents' chat for a match you are in. Idempotent."""
        now = db.now()
        with db.tx() as conn:
            m = lifecycle.get_match(conn, body.match_id)
            if m is None:
                raise HTTPException(404, "no such match")
            if uid not in (m["user_low"], m["user_high"]):
                raise HTTPException(403, "not a member of this match")
            s = conn.execute("SELECT * FROM sy_sessions WHERE match_id = ?", (body.match_id,)).fetchone()
            if s is None:
                if m["state"] not in lifecycle.ACTIVE:
                    raise HTTPException(409, "this match has ended")
                conn.execute("INSERT INTO sy_sessions (id, match_id, user_low, user_high, state, created_at,"
                             " updated_at) VALUES (?,?,?,?, 'TALKING', ?, ?)",
                             (new_id(), body.match_id, m["user_low"], m["user_high"], now, now))
                s = conn.execute("SELECT * FROM sy_sessions WHERE match_id = ?", (body.match_id,)).fetchone()
            return {"session": project(conn, s, uid)}

    @app.get("/api/second-you/sessions/{session_id}")
    def get_session(session_id: str, uid: str = Depends(user)):
        with db.lock:
            s, _ = load(db.conn, session_id, uid)
            return {"session": project(db.conn, s, uid)}

    @app.post("/api/second-you/sessions/{session_id}/relay")
    def relay_send(session_id: str, body: Sealed, uid: str = Depends(user)):
        """Pass one sealed agent message to the other phone. Held in memory only, never written to disk."""
        with db.lock:
            s, me = load(db.conn, session_id, uid)
        if s["state"] != "TALKING":
            raise HTTPException(409, "the agents have finished talking")
        relay.put(session_id, "high" if me == "low" else "low", body.ciphertext)
        return {"ok": True}

    @app.get("/api/second-you/sessions/{session_id}/relay")
    def relay_take(session_id: str, uid: str = Depends(user)):
        """Sealed messages waiting for you. Each is handed over once and then forgotten."""
        with db.lock:
            _, me = load(db.conn, session_id, uid)
        return {"items": relay.take(session_id, me)}

    @app.post("/api/second-you/sessions/{session_id}/verdict")
    def verdict(session_id: str, body: Verdict, uid: str = Depends(user)):
        """Your phone's five numbers. Once both are in, the server makes the match decision."""
        now = db.now()
        with db.tx() as conn:
            s, me = load(conn, session_id, uid)
            if s["state"] != "TALKING":
                raise HTTPException(409, "a decision was already made")
            if s[f"{me}_verdict"] is not None:
                raise HTTPException(409, "your phone already sent its verdict")
            mine = {"scores": body.scores, "clash": body.clash, "invented": body.invented}
            touch(conn, s, now, **{f"{me}_verdict": dumps(mine)})
            s = reread(conn, session_id)
            them = "high" if me == "low" else "low"
            if s[f"{them}_verdict"] is not None:
                d = decide(mine, loads(s[f"{them}_verdict"]), floor_now(conn))
                touch(conn, s, now, state="MATCH" if d["match"] else "NO_MATCH", score=d["score"],
                      lower_bound=d["interval"][0], upper_bound=d["interval"][1], floor=d["floor"],
                      matched=int(d["match"]), clash=int(d["clash"]), invented=int(d["invented"]))
                relay.drop(session_id)
                s = reread(conn, session_id)
            return {"session": project(conn, s, uid)}

    @app.post("/api/second-you/sessions/{session_id}/respond")
    def answer(session_id: str, body: Answer, uid: str = Depends(user)):
        """Both have to say yes before names are shown. The answer is also sent to the Vexo match."""
        now = db.now()
        with db.tx() as conn:
            s, me = load(conn, session_id, uid)
            if s["state"] != "MATCH":
                raise HTTPException(409, "there is nothing to answer right now")
            touch(conn, s, now, **{f"{me}_accepted": int(body.accept)})
            s = reread(conn, session_id)
            if not body.accept:
                touch(conn, s, now, state="PASSED")
            elif s["low_accepted"] and s["high_accepted"]:
                touch(conn, s, now, state="REVEALED")
            m = lifecycle.get_match(conn, s["match_id"])
            if m is not None and m["state"] == "SUGGESTED" and not m[f"{me}_accepted"]:
                try:
                    lifecycle.respond(conn, s["match_id"], uid, body.accept, now, cfg)
                except (LookupError, PermissionError, ValueError) as exc:   # the Second You answer still counts
                    log.info("second you %s: vexo match not updated: %s", session_id[:8], exc)
            return {"session": project(conn, reread(conn, session_id), uid)}

    @app.post("/api/second-you/sessions/{session_id}/date")
    def confirm_date(session_id: str, body: DateConfirm, uid: str = Depends(user)):
        """Confirm the date your agents planned. The plan itself stays on the phones."""
        now = db.now()
        with db.tx() as conn:
            s, me = load(conn, session_id, uid)
            if s["state"] != "REVEALED":
                raise HTTPException(409, "you can confirm a date once you both said yes")
            touch(conn, s, now, **{f"{me}_date": int(body.confirm)})
            s = reread(conn, session_id)
            if s["low_date"] and s["high_date"]:
                touch(conn, s, now, state="DATE_SET")
            return {"session": project(conn, reread(conn, session_id), uid)}

    @app.post("/api/second-you/sessions/{session_id}/outcome")
    def outcome(session_id: str, body: Outcome, uid: str = Depends(user)):
        """How the date went. Feeds the calibrated match floor for everyone."""
        now = db.now()
        with db.tx() as conn:
            s, _ = load(conn, session_id, uid)
            if s["state"] not in ("DATE_SET", "DONE"):
                raise HTTPException(409, "rate the date after you both confirmed it")
            if conn.execute("SELECT 1 FROM sy_outcomes WHERE session_id = ? AND user_id = ?",
                            (session_id, uid)).fetchone():
                raise HTTPException(409, "you already rated this date")
            conn.execute("INSERT INTO sy_outcomes (session_id, user_id, went, second_date, rating, lower_bound,"
                         " created_at) VALUES (?,?,?,?,?,?,?)",
                         (session_id, uid, int(body.went), int(body.went and body.second_date), body.rating,
                          s["lower_bound"], now))
            n = conn.execute("SELECT COUNT(*) AS n FROM sy_outcomes WHERE session_id = ?",
                             (session_id,)).fetchone()["n"]
            if n >= 2:
                touch(conn, s, now, state="DONE")
            return {"session": project(conn, reread(conn, session_id), uid)}

    @app.get("/api/second-you/calibration")
    def calibration(_uid: str = Depends(user)):
        with db.lock:
            rows = db.conn.execute("SELECT lower_bound, second_date FROM sy_outcomes").fetchall()
        pts = [(r["lower_bound"], bool(r["second_date"])) for r in rows]
        return {"floor": calibrated_floor(pts), "default_floor": MATCH_FLOOR, "outcomes": len(pts),
                "needed": MIN_OUTCOMES}

    @app.get("/second-you")
    def second_you_page():
        return FileResponse(UI / "second-you.html")
