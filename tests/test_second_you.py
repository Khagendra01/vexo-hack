"""Second You: decision math, calibration, a relay that stores nothing, and refusal of training data."""
import base64
import json
import math

import pytest
from conftest import Demo

from server import second_you
from server.second_you import calibrated_floor, decide

V_GOOD = {"scores": [0.8, 0.75, 0.7], "clash": False, "invented": False}


def sealed(n: int = 0) -> str:
    return base64.b64encode(f"sealed-box-{n}".encode() * 2).decode()


def to_session(d: Demo) -> str:
    d.step(1)
    d.approach("alice", "bob", -70, 6)
    m = d.match("alice")
    assert m["state"] == "SUGGESTED"
    r = d.client.post("/api/second-you/sessions", json={"match_id": m["id"]}, headers=d.u("alice"))
    assert r.status_code == 200, r.text
    again = d.client.post("/api/second-you/sessions", json={"match_id": m["id"]}, headers=d.u("bob"))
    assert again.json()["session"]["id"] == r.json()["session"]["id"]          # idempotent, one per match
    return r.json()["session"]["id"]


def post(d, who, sid, what, body):
    return d.client.post(f"/api/second-you/sessions/{sid}/{what}", json=body, headers=d.u(who))


# ------------------------------------------------------------ decision math (port of Chemistry.swift)

def test_decide_matches_swift_formula():
    mine = {"scores": [0.9, 0.7, 0.8], "clash": False, "invented": False}
    theirs = {"scores": [0.7, 0.7, 0.6], "clash": False, "invented": False}
    d = decide(mine, theirs)
    per = [0.8, 0.7, 0.7]
    mean = sum(per) / 3
    sd = math.sqrt(sum((p - mean) ** 2 for p in per) / 2)
    assert d["score"] == pytest.approx(mean)
    assert d["interval"][0] == pytest.approx(mean - 1.96 * sd / math.sqrt(3))
    assert d["interval"][1] == pytest.approx(mean + 1.96 * sd / math.sqrt(3))
    assert d["match"] is True


def test_decide_needs_lower_bound_over_floor():
    wobbly = {"scores": [0.95, 0.3, 0.9], "clash": False, "invented": False}
    d = decide(wobbly, wobbly)
    assert d["score"] > 0.6 and d["interval"][0] < 0.6 and d["match"] is False
    assert decide(V_GOOD, V_GOOD, floor=0.9)["match"] is False


def test_clash_penalty_and_invented_block():
    clash = dict(V_GOOD, clash=True)
    d = decide(V_GOOD, clash)
    assert d["clash"] and d["score"] == pytest.approx(0.75 * 0.35) and not d["match"]
    inv = decide(V_GOOD, dict(V_GOOD, invented=True))
    assert inv["invented"] and inv["score"] == pytest.approx(0.75) and not inv["match"]


def test_interval_is_clamped():
    d = decide({"scores": [1, 0, 1], "clash": False, "invented": False},
               {"scores": [1, 0, 1], "clash": False, "invented": False})
    assert 0.0 <= d["interval"][0] <= d["interval"][1] <= 1.0


def test_calibrated_floor_port():
    assert calibrated_floor([(0.7, True)] * 19) == 0.6                  # not enough outcomes yet
    pts = [(0.3, False)] * 10 + [(0.5, False)] * 5 + [(0.65, True)] * 4 + [(0.7, True)] * 3 + [(0.8, False)] * 1
    assert calibrated_floor(pts) == 0.65
    assert calibrated_floor([(0.7, False)] * 25) == 0.6                 # nothing reaches 60%: keep default


# ------------------------------------------------------------ flow through the API

def test_full_flow_hidden_identity_reveal_date_and_rating(demo):
    d = demo
    sid = to_session(d)
    s = d.client.get(f"/api/second-you/sessions/{sid}", headers=d.u("alice")).json()["session"]
    assert s["state"] == "TALKING" and s["counterpart"] is None and s["goes_first"] != \
        d.client.get(f"/api/second-you/sessions/{sid}", headers=d.u("bob")).json()["session"]["goes_first"]

    assert post(d, "alice", sid, "verdict", V_GOOD).json()["session"]["decision"] is None
    assert post(d, "alice", sid, "verdict", V_GOOD).status_code == 409                # once per phone
    s = post(d, "bob", sid, "verdict", {"scores": [0.85, 0.8, 0.75], "clash": False, "invented": False}).json()["session"]
    assert s["state"] == "MATCH" and s["decision"]["match"] and s["counterpart"] is None

    s = post(d, "alice", sid, "respond", {"accept": True}).json()["session"]
    assert s["you_accepted"] and not s["both_yes"] and s["counterpart"] is None
    s = post(d, "bob", sid, "respond", {"accept": True}).json()["session"]
    assert s["state"] == "REVEALED" and s["counterpart"]["display_name"] == "Alice"
    assert d.match("alice")["state"] == "ACCEPTED"                                     # forwarded to the Vexo match

    post(d, "alice", sid, "date", {"confirm": True})
    s = post(d, "bob", sid, "date", {"confirm": True}).json()["session"]
    assert s["state"] == "DATE_SET" and s["date_confirmed"]

    post(d, "alice", sid, "outcome", {"went": True, "second_date": True, "rating": 5})
    assert post(d, "alice", sid, "outcome", {"went": True}).status_code == 409
    s = post(d, "bob", sid, "outcome", {"went": True, "second_date": False, "rating": 3}).json()["session"]
    assert s["state"] == "DONE" and s["you_rated"]
    cal = d.client.get("/api/second-you/calibration", headers=d.u("bob")).json()
    assert cal["outcomes"] == 2 and cal["floor"] == 0.6


def test_no_match_on_clash_and_one_no_passes(demo):
    d = demo
    sid = to_session(d)
    post(d, "alice", sid, "verdict", V_GOOD)
    s = post(d, "bob", sid, "verdict", dict(V_GOOD, clash=True)).json()["session"]
    assert s["state"] == "NO_MATCH" and s["decision"]["clash"]
    assert post(d, "alice", sid, "respond", {"accept": True}).status_code == 409


def test_outsider_cannot_read_or_post():
    d = Demo(people=("alice", "bob", "dan"))
    sid = to_session(d)
    assert d.client.get(f"/api/second-you/sessions/{sid}", headers=d.u("dan")).status_code == 403
    assert post(d, "dan", sid, "relay", {"ciphertext": sealed()}).status_code == 403
    assert post(d, "dan", sid, "verdict", V_GOOD).status_code == 403


# ------------------------------------------------------------ the relay stores nothing

def all_db_text(d: Demo) -> str:
    conn = d.db.conn
    tables = [r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]
    return json.dumps([[list(r) for r in conn.execute(f"SELECT * FROM {t}")] for t in tables], default=str)


def test_relay_delivers_once_and_stores_nothing(demo):
    d = demo
    sid = to_session(d)
    secret = sealed(7)
    assert post(d, "alice", sid, "relay", {"ciphertext": secret}).status_code == 200
    assert secret not in all_db_text(d)                                       # never written to the database
    assert d.client.get(f"/api/second-you/sessions/{sid}/relay", headers=d.u("alice")).json()["items"] == []
    got = d.client.get(f"/api/second-you/sessions/{sid}/relay", headers=d.u("bob")).json()["items"]
    assert got == [secret]
    assert d.client.get(f"/api/second-you/sessions/{sid}/relay", headers=d.u("bob")).json()["items"] == []
    assert d.app.state.second_you_relay.queued() == 0                         # gone from memory too
    assert d.client.get(f"/api/second-you/sessions/{sid}", headers=d.u("bob")).json()["session"]["relayed"] == 1


def test_relay_drops_undelivered_after_decision_and_expiry(demo):
    d = demo
    sid = to_session(d)
    post(d, "alice", sid, "relay", {"ciphertext": sealed(1)})
    d.clock.advance(second_you.RELAY_TTL_S + 1)
    assert d.client.get(f"/api/second-you/sessions/{sid}/relay", headers=d.u("bob")).json()["items"] == []
    post(d, "alice", sid, "relay", {"ciphertext": sealed(2)})
    post(d, "alice", sid, "verdict", V_GOOD)
    post(d, "bob", sid, "verdict", V_GOOD)
    assert d.app.state.second_you_relay.queued() == 0
    assert post(d, "alice", sid, "relay", {"ciphertext": sealed(3)}).status_code == 409


def test_relay_rejects_plaintext(demo):
    d = demo
    sid = to_session(d)
    assert post(d, "alice", sid, "relay", {"ciphertext": "hey! I love hiking too, want to get coffee?"}).status_code == 422
    assert post(d, "alice", sid, "relay", {"text": "hi there"}).status_code == 422


# ------------------------------------------------------------ training data is refused

@pytest.mark.parametrize("path", ["profile", "card", "train", "training-data", "transcripts", "questionnaire"])
def test_profile_and_training_routes_refuse(demo, path):
    r = demo.client.post(f"/api/second-you/{path}", json={"anything": 1}, headers=demo.u("alice"))
    assert r.status_code == 403 and "stays on the phone" in r.json()["detail"]


@pytest.mark.parametrize("extra", [
    {"profile": {"name": "Alice"}}, {"card": "I hate cilantro"}, {"lines": ["my own text"]},
    {"transcript": "..."}, {"answers": {"kids": "yes"}}, {"dealbreakers": ["smoking"]},
    {"meta": {"nested": {"own_lines": ["x"]}}},
])
def test_verdict_refuses_training_payloads(demo, extra):
    d = demo
    sid = to_session(d)
    r = post(d, "alice", sid, "verdict", {**V_GOOD, **extra})
    assert r.status_code == 422 and "stays on the phone" in r.text
    assert "Alice" not in json.dumps([list(r) for r in d.db.conn.execute("SELECT * FROM sy_sessions")])


def test_verdict_rejects_unknown_fields_and_bad_numbers(demo):
    d = demo
    sid = to_session(d)
    assert post(d, "alice", sid, "verdict", {**V_GOOD, "note": "x"}).status_code == 422
    assert post(d, "alice", sid, "verdict", {"scores": [0.5, 0.5], "clash": False, "invented": False}).status_code == 422
    assert post(d, "alice", sid, "verdict", {"scores": [1.5, 0.5, 0.5], "clash": False, "invented": False}).status_code == 422


def test_page_is_served(demo):
    r = demo.client.get("/second-you")
    assert r.status_code == 200 and "Second You" in r.text
    assert demo.client.get("/ui/second-you.js").status_code == 200
