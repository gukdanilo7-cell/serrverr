#!/usr/bin/env python3
"""
TetRun multiplayer server
- Friends, online status, chat
- Lobby / ranked queue
- Shared match state for Runner (climb) & Pacman

Run:
  pip install flask flask-cors
  python server.py

Deploy (Render):
  Build: pip install -r requirements.txt
  Start: python server.py
  Env: PORT is set automatically
"""

from __future__ import annotations
import os, time, json, secrets, threading, hashlib
from flask import Flask, request, jsonify
from flask_cors import CORS

app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}}, supports_credentials=False)

@app.after_request
def add_cors(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS, PUT, DELETE"
    resp.headers["Access-Control-Max-Age"] = "86400"
    return resp

@app.route("/api/<path:path>", methods=["OPTIONS"])
@app.route("/<path:path>", methods=["OPTIONS"])
def cors_preflight(path=""):
    return ("", 204)


DATA_FILE = os.environ.get("TETRUN_DATA", "tetrun_data.json")
ONLINE_SEC = 45          # heartbeat timeout
CHAT_MAX = 100
LOCK = threading.RLock()

# ---------- storage ----------
users: dict = {}       # user -> {pass_hash, nick, avatar, elo, stats, friends:set}
online: dict = {}      # user -> {t, nick, activity, lobby_code, match_id}
chats: dict = {}       # "a|b" -> [{from,to,text,t}]
lobbies: dict = {}     # code -> {host, mode, players[], ranked, started, match_id}
queue: dict = {}       # user -> {mode, t}
matches: dict = {}     # id -> match state
wait_rooms: dict = {}  # code -> wait room

def uid():
    return secrets.token_hex(4)

def chat_key(a, b):
    return "|".join(sorted([a.lower(), b.lower()]))

def hash_pass(p: str) -> str:
    return hashlib.sha256(("tetrun:" + p).encode()).hexdigest()

def now():
    return time.time()

def save():
    try:
        blob = {
            "users": {
                u: {
                    **{k: v for k, v in d.items() if k != "friends"},
                    "friends": list(d.get("friends") or []),
                }
                for u, d in users.items()
            },
            "chats": chats,
        }
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(blob, f)
    except Exception as e:
        print("save err", e)

def load():
    global users, chats
    if not os.path.exists(DATA_FILE):
        return
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            blob = json.load(f)
        users = {}
        for u, d in (blob.get("users") or {}).items():
            users[u] = d
            users[u]["friends"] = set(d.get("friends") or [])
        chats = blob.get("chats") or {}
    except Exception as e:
        print("load err", e)

def prune_online():
    t = now()
    dead = [u for u, o in online.items() if t - o.get("t", 0) > ONLINE_SEC]
    for u in dead:
        online.pop(u, None)

def public_user(u: str):
    d = users.get(u) or {}
    o = online.get(u)
    return {
        "user": u,
        "nick": d.get("nick") or u,
        "avatar": d.get("avatar") or "",
        "elo": int(d.get("elo") or 1000),
        "online": bool(o),
        "activity": (o or {}).get("activity") or ("offline" if not o else "idle"),
        "lobby_code": (o or {}).get("lobby_code"),
        "stats": d.get("stats") or {},
    }

def ensure_user(u: str, nick=None):
    u = (u or "").strip().lower()
    if not u:
        return None
    if u not in users:
        users[u] = {
            "pass_hash": "",
            "nick": nick or u,
            "avatar": "",
            "elo": 1000,
            "stats": {"wins": 0, "losses": 0, "streak": 0},
            "friends": set(),
        }
    elif nick:
        users[u]["nick"] = nick
    return u

def new_match(players, mode="roles", ranked=False):
    """players: list of {user, role?}"""
    mid = "m_" + uid()
    # assign roles
    plist = []
    for i, p in enumerate(players):
        role = p.get("role")
        if not role:
            if mode in ("roles", "runner_thrower"):
                role = "runner" if i == 0 else "thrower"
            else:
                role = "runner"
        plist.append({"user": p["user"], "role": role, "alive": True, "x": 80 + i * 40, "y": 0, "score": 0})
    matches[mid] = {
        "id": mid,
        "mode": mode,
        "ranked": ranked,
        "players": plist,
        "started": now(),
        "start_at": now() + 3,
        "pieces": [],
        "events": [],
        "over": False,
        "winner": None,
        "game": "climb" if mode != "pacman" else "pacman",
    }
    for p in plist:
        if p["user"] in online:
            online[p["user"]]["activity"] = "play"
            online[p["user"]]["match_id"] = mid
    return matches[mid]

# ---------- routes ----------
@app.get("/")
def root():
    return jsonify({"ok": True, "service": "TetRun MP", "version": 2})

@app.get("/api/health")
def health():
    prune_online()
    return jsonify({"ok": True, "online": len(online), "users": len(users)})

@app.post("/api/auth/register")
def auth_register():
    data = request.get_json(force=True, silent=True) or {}
    u = (data.get("user") or "").strip().lower()
    pw = data.get("pass") or ""
    nick = data.get("nick") or u
    if not u or len(u) < 2:
        return jsonify({"error": "bad user"}), 400
    with LOCK:
        if u in users and users[u].get("pass_hash"):
            return jsonify({"error": "exists"}), 400
        ensure_user(u, nick)
        users[u]["pass_hash"] = hash_pass(pw)
        users[u]["nick"] = nick
        save()
        return jsonify({"ok": True, "user": public_user(u), "token": secrets.token_hex(12)})

@app.post("/api/auth/login")
def auth_login():
    data = request.get_json(force=True, silent=True) or {}
    u = (data.get("user") or "").strip().lower()
    pw = data.get("pass") or ""
    nick = data.get("nick") or u
    avatar = data.get("avatar") or ""
    with LOCK:
        ensure_user(u, nick)
        if users[u].get("pass_hash") and users[u]["pass_hash"] != hash_pass(pw):
            # allow soft login if empty pass was never set
            if users[u]["pass_hash"]:
                return jsonify({"error": "bad password"}), 403
        if not users[u].get("pass_hash") and pw:
            users[u]["pass_hash"] = hash_pass(pw)
        users[u]["nick"] = nick or users[u].get("nick") or u
        if avatar:
            users[u]["avatar"] = avatar
        online[u] = {
            "t": now(),
            "nick": users[u]["nick"],
            "activity": "idle",
            "lobby_code": None,
            "match_id": None,
        }
        save()
        return jsonify({"ok": True, "user": public_user(u), "token": secrets.token_hex(12), "elo": users[u]["elo"]})

@app.post("/api/heartbeat")
def heartbeat():
    data = request.get_json(force=True, silent=True) or {}
    u = ensure_user(data.get("user"), data.get("nick"))
    if not u:
        return jsonify({"error": "user"}), 400
    with LOCK:
        prune_online()
        o = online.get(u) or {}
        o["t"] = now()
        o["nick"] = data.get("nick") or o.get("nick") or u
        if data.get("activity"):
            o["activity"] = data["activity"]
        if "stats" in data and isinstance(data["stats"], dict):
            users[u]["stats"] = {**users[u].get("stats") or {}, **data["stats"]}
        online[u] = o
        return jsonify({"ok": True, "online": len(online), "elo": users[u].get("elo", 1000)})

@app.get("/api/online")
def api_online():
    with LOCK:
        prune_online()
        return jsonify({"online": [public_user(u) for u in online.keys()]})

@app.get("/api/friends")
def api_friends():
    u = (request.args.get("user") or "").strip().lower()
    with LOCK:
        prune_online()
        ensure_user(u)
        fr = list(users[u].get("friends") or [])
        return jsonify({
            "me": public_user(u),
            "friends": [public_user(f) for f in fr if f in users or f],
        })

@app.post("/api/friend/add")
def friend_add():
    data = request.get_json(force=True, silent=True) or {}
    u = ensure_user(data.get("user"))
    fr = ensure_user(data.get("friend"))
    if not u or not fr or u == fr:
        return jsonify({"error": "bad"}), 400
    with LOCK:
        users[u].setdefault("friends", set()).add(fr)
        users[fr].setdefault("friends", set()).add(u)  # mutual
        save()
        return jsonify({"ok": True, "friends": [public_user(x) for x in users[u]["friends"]]})

@app.post("/api/friend/remove")
def friend_remove():
    data = request.get_json(force=True, silent=True) or {}
    u = ensure_user(data.get("user"))
    fr = (data.get("friend") or "").strip().lower()
    with LOCK:
        if u and fr in (users.get(u) or {}).get("friends", set()):
            users[u]["friends"].discard(fr)
        if fr in users:
            users[fr].setdefault("friends", set()).discard(u)
        save()
        return jsonify({"ok": True})

@app.get("/api/chat")
def chat_get():
    u = (request.args.get("user") or "").strip().lower()
    w = (request.args.get("with") or "").strip().lower()
    with LOCK:
        msgs = list(chats.get(chat_key(u, w), []))[-CHAT_MAX:]
        return jsonify({"messages": msgs, "msgs": msgs})

@app.post("/api/chat")
def chat_post():
    data = request.get_json(force=True, silent=True) or {}
    frm = (data.get("from") or "").strip().lower()
    to = (data.get("to") or "").strip().lower()
    text = (data.get("text") or "").strip()[:200]
    if not frm or not to or not text:
        return jsonify({"error": "bad"}), 400
    with LOCK:
        ensure_user(frm)
        ensure_user(to)
        key = chat_key(frm, to)
        arr = chats.setdefault(key, [])
        arr.append({"from": frm, "to": to, "text": text, "t": int(now() * 1000)})
        chats[key] = arr[-CHAT_MAX:]
        save()
        return jsonify({"ok": True})

@app.get("/api/profile")
def profile():
    u = (request.args.get("user") or "").strip().lower()
    with LOCK:
        prune_online()
        ensure_user(u)
        return jsonify({"user": public_user(u), "friends": [public_user(f) for f in users[u].get("friends", set())]})


@app.post("/api/invite")
def api_invite():
    data = request.get_json(force=True, silent=True) or {}
    u = ensure_user(data.get("user"))
    fr = ensure_user(data.get("friend"))
    code = (data.get("code") or "").strip().upper()
    if not u or not fr or not code:
        return jsonify({"error": "bad"}), 400
    with LOCK:
        # store as chat message
        key = chat_key(u, fr)
        arr = chats.setdefault(key, [])
        arr.append({"from": u, "to": fr, "text": "[INVITE] Join lobby "+code, "t": int(now()*1000), "invite": code})
        chats[key] = arr[-CHAT_MAX:]
        if fr in online:
            online[fr]["last_invite"] = {"from": u, "code": code, "t": now()}
        save()
        return jsonify({"ok": True, "code": code})

@app.get("/api/top")
def top():
    with LOCK:
        lst = sorted(users.keys(), key=lambda u: users[u].get("elo", 1000), reverse=True)[:30]
        return jsonify({"top": [public_user(u) for u in lst]})

# ----- lobby -----
@app.post("/api/lobby/create")
def lobby_create():
    data = request.get_json(force=True, silent=True) or {}
    u = ensure_user(data.get("user"))
    mode = data.get("mode") or "roles"
    if not u:
        return jsonify({"error": "user"}), 400
    with LOCK:
        code = uid()[:5].upper()
        lobbies[code] = {
            "code": code,
            "host": u,
            "mode": mode,
            "players": [u],
            "ranked": False,
            "started": False,
            "match_id": None,
            "t": now(),
        }
        online.setdefault(u, {"t": now(), "nick": users[u]["nick"]})
        online[u]["activity"] = "wait"
        online[u]["lobby_code"] = code
        return jsonify({"ok": True, "code": code, "lobby": lobbies[code]})

@app.post("/api/lobby/join")
def lobby_join():
    data = request.get_json(force=True, silent=True) or {}
    u = ensure_user(data.get("user"))
    code = (data.get("code") or "").strip().upper()
    with LOCK:
        lob = lobbies.get(code)
        if not lob:
            return jsonify({"error": "not found"}), 404
        if u not in lob["players"]:
            lob["players"].append(u)
        online.setdefault(u, {"t": now(), "nick": users[u]["nick"]})
        online[u]["activity"] = "wait"
        online[u]["lobby_code"] = code
        return jsonify({"ok": True, "lobby": lob})

@app.get("/api/lobby")
def lobby_get():
    code = (request.args.get("code") or "").strip().upper()
    with LOCK:
        prune_online()
        lob = lobbies.get(code)
        if not lob:
            return jsonify({"error": "not found"}), 404
        players = [public_user(p) for p in lob["players"]]
        return jsonify({
            "ok": True,
            "code": lob["code"],
            "mode": lob.get("mode"),
            "host": lob.get("host"),
            "started": bool(lob.get("started")),
            "match_id": lob.get("match_id"),
            "lobby": lob,
            "players": players,
        })

@app.post("/api/lobby/start")
def lobby_start():
    data = request.get_json(force=True, silent=True) or {}
    u = ensure_user(data.get("user"))
    code = (data.get("code") or "").strip().upper()
    with LOCK:
        lob = lobbies.get(code)
        if not lob:
            return jsonify({"error": "not found"}), 404
        if lob["host"] != u:
            return jsonify({"error": "host only"}), 403
        if len(lob["players"]) < 2:
            return jsonify({"error": "need 2 players"}), 400
        mode = lob.get("mode") or "roles"
        m = new_match([{"user": p} for p in lob["players"]], mode=mode)
        lob["started"] = True
        lob["match_id"] = m["id"]
        roles = {p["user"]: p["role"] for p in m["players"]}
        return jsonify({
            "ok": True,
            "match_id": m["id"],
            "match": m,
            "start_at": m["start_at"],
            "mode": mode,
            "roles": roles,
            "server_now": now(),
        })

@app.post("/api/lobby/leave")
def lobby_leave():
    data = request.get_json(force=True, silent=True) or {}
    u = ensure_user(data.get("user"))
    with LOCK:
        for code, lob in list(lobbies.items()):
            if u in lob["players"]:
                lob["players"] = [p for p in lob["players"] if p != u]
                if not lob["players"] or lob["host"] == u:
                    lobbies.pop(code, None)
        if u in online:
            online[u]["lobby_code"] = None
            online[u]["activity"] = "idle"
        return jsonify({"ok": True})

# ----- wait / queue -----
@app.post("/api/wait/create")
def wait_create():
    data = request.get_json(force=True, silent=True) or {}
    u = ensure_user(data.get("user"))
    mode = data.get("mode") or "roles"
    with LOCK:
        code = uid()[:5].upper()
        wait_rooms[code] = {"code": code, "host": u, "mode": mode, "players": [u], "ranked": bool(data.get("ranked")), "t": now()}
        online.setdefault(u, {"t": now(), "nick": users[u]["nick"]})
        online[u]["activity"] = "wait"
        online[u]["lobby_code"] = code
        return jsonify({"ok": True, "code": code})

@app.post("/api/wait/join")
def wait_join():
    data = request.get_json(force=True, silent=True) or {}
    u = ensure_user(data.get("user"))
    code = (data.get("code") or "").strip().upper()
    with LOCK:
        w = wait_rooms.get(code)
        if not w:
            return jsonify({"error": "not found"}), 404
        if u not in w["players"]:
            w["players"].append(u)
        if len(w["players"]) >= 2:
            m = new_match([{"user": p} for p in w["players"][:2]], mode=w.get("mode") or "roles", ranked=w.get("ranked"))
            wait_rooms.pop(code, None)
            return jsonify({"ok": True, "match_id": m["id"], "match": m, "start_at": m["start_at"]})
        return jsonify({"ok": True, "waiting": True, "code": code})

@app.get("/api/wait")
def wait_get():
    code = (request.args.get("code") or "").strip().upper()
    with LOCK:
        w = wait_rooms.get(code)
        if not w:
            # maybe already converted to match
            return jsonify({"error": "not found"}), 404
        return jsonify({"wait": w, "players": [public_user(p) for p in w["players"]]})

@app.post("/api/queue")
def api_queue():
    data = request.get_json(force=True, silent=True) or {}
    u = ensure_user(data.get("user"))
    mode = data.get("mode") or "roles"
    with LOCK:
        prune_online()
        # try match with someone else in queue same mode
        for other, q in list(queue.items()):
            if other != u and q.get("mode") == mode:
                queue.pop(other, None)
                queue.pop(u, None)
                m = new_match([{"user": other}, {"user": u}], mode=mode, ranked=True)
                return jsonify({"ok": True, "match_id": m["id"], "match": m, "start_at": m["start_at"]})
        queue[u] = {"mode": mode, "t": now()}
        online.setdefault(u, {"t": now(), "nick": users[u]["nick"]})
        online[u]["activity"] = "queue"
        return jsonify({"ok": True, "queued": True})

@app.get("/api/queue/status")
def queue_status():
    u = (request.args.get("user") or "").strip().lower()
    with LOCK:
        # if in match
        for mid, m in matches.items():
            if any(p["user"] == u for p in m["players"]) and not m.get("over"):
                return jsonify({"match_id": mid, "match": m, "start_at": m.get("start_at")})
        if u in queue:
            return jsonify({"queued": True})
        return jsonify({"queued": False})

@app.post("/api/queue/cancel")
def queue_cancel():
    data = request.get_json(force=True, silent=True) or {}
    u = ensure_user(data.get("user"))
    with LOCK:
        queue.pop(u, None)
        if u in online:
            online[u]["activity"] = "idle"
        return jsonify({"ok": True})

# ----- match -----
@app.get("/api/match")
def match_get():
    mid = request.args.get("id") or ""
    with LOCK:
        m = matches.get(mid)
        if not m:
            return jsonify({"error": "not found"}), 404
        # enrich player nicks
        players = []
        for p in m["players"]:
            pu = public_user(p["user"])
            players.append({**p, "nick": pu.get("nick"), "elo": pu.get("elo")})
        out = {**m, "players": players, "server_now": now()}
        return jsonify({"match": out, **out})

@app.post("/api/match/pos")
def match_pos():
    data = request.get_json(force=True, silent=True) or {}
    mid = data.get("match_id") or ""
    u = (data.get("user") or "").strip().lower()
    with LOCK:
        m = matches.get(mid)
        if not m:
            return jsonify({"error": "not found"}), 404
        for p in m["players"]:
            if p["user"] == u:
                if "x" in data: p["x"] = data["x"]
                if "y" in data: p["y"] = data["y"]
                if "vx" in data: p["vx"] = data["vx"]
                if "vy" in data: p["vy"] = data["vy"]
                if "alive" in data: p["alive"] = bool(data["alive"])
                if "face" in data: p["face"] = data["face"]
                if "anim" in data: p["anim"] = data["anim"]
                break
        return jsonify({"ok": True, "match": m})

@app.post("/api/match/throw")
def match_throw():
    data = request.get_json(force=True, silent=True) or {}
    mid = data.get("match_id") or ""
    with LOCK:
        m = matches.get(mid)
        if not m:
            return jsonify({"error": "not found"}), 404
        piece = {
            "shape": data.get("shape"),
            "gx": data.get("gx", 0),
            "user": data.get("user"),
            "t": now(),
        }
        m.setdefault("pieces", []).append(piece)
        m["pieces"] = m["pieces"][-30:]
        m.setdefault("events", []).append({"type": "throw", **piece})
        return jsonify({"ok": True, "match": m})

@app.post("/api/match/event")
def match_event():
    data = request.get_json(force=True, silent=True) or {}
    mid = data.get("match_id") or ""
    u = (data.get("user") or "").strip().lower()
    ev = data.get("event") or data.get("type") or "event"
    with LOCK:
        m = matches.get(mid)
        if not m:
            return jsonify({"error": "not found"}), 404
        m.setdefault("events", []).append({"type": ev, "user": u, "t": now(), "data": data})
        if ev in ("die", "dead", "lose"):
            for p in m["players"]:
                if p["user"] == u:
                    p["alive"] = False
            alive = [p for p in m["players"] if p.get("alive")]
            if len(alive) <= 1:
                m["over"] = True
                m["winner"] = alive[0]["user"] if alive else None
                # elo
                if m.get("ranked") and m["winner"]:
                    for p in m["players"]:
                        d = users.get(p["user"])
                        if not d:
                            continue
                        if p["user"] == m["winner"]:
                            d["elo"] = int(d.get("elo", 1000)) + 15
                        else:
                            d["elo"] = max(100, int(d.get("elo", 1000)) - 12)
                    save()
        if ev in ("win", "finish"):
            m["over"] = True
            m["winner"] = u
        return jsonify({"ok": True, "match": m})

if __name__ == "__main__":
    load()
    port = int(os.environ.get("PORT", 5000))
    print(f"TetRun MP on http://0.0.0.0:{port}")
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
