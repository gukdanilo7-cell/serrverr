#!/usr/bin/env python3
"""TetRun multiplayer server — pure Python stdlib (no Flask).
Run:  python server.py
Uses PORT from env (Render/Railway) or 8765.
"""
from __future__ import annotations
import json, time, uuid, threading, hashlib, random, string, os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

PORT = int(os.environ.get("PORT", "8765"))
ONLINE_TTL = 30.0

lock = threading.RLock()
users: dict = {}
lobbies: dict = {}
matches: dict = {}
queues: dict = {"first_win": [], "first_die": [], "roles": []}  # roles = asymmetric runner vs thrower

# Persist accounts so Render restarts / sleep don't wipe friends & elo
DATA_DIR = os.environ.get("DATA_DIR", ".")
DATA_FILE = os.path.join(DATA_DIR, "tetrun_users.json")
MAX_AVATAR = 60000  # ~60KB data-URL cap

def clamp_avatar(av):
    av = str(av or "")
    if len(av) > MAX_AVATAR:
        return ""  # too big — drop rather than break JSON
    return av

def save_users():
    try:
        payload = {}
        for u, rec in users.items():
            payload[u] = {
                "pass": rec.get("pass", ""),
                "nick": rec.get("nick", u),
                "elo": int(rec.get("elo", 1000)),
                "friends": list(rec.get("friends") or []),
                "avatar": clamp_avatar(rec.get("avatar") or ""),
                "stats": rec.get("stats") or {},
            }
        tmp = DATA_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        os.replace(tmp, DATA_FILE)
    except Exception as e:
        print("save_users error:", e)

def load_users():
    global users
    try:
        if not os.path.isfile(DATA_FILE):
            return
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            payload = json.load(f)
        for u, rec in (payload or {}).items():
            users[u] = {
                "pass": rec.get("pass", ""),
                "nick": rec.get("nick", u),
                "elo": int(rec.get("elo", 1000)),
                "friends": list(rec.get("friends") or []),
                "avatar": rec.get("avatar") or "",
                "stats": rec.get("stats") or {
                    "wins": 0, "losses": 0, "streak": 0, "best": 0,
                    "points": 0, "games": 0, "timeSec": 0, "pacBest": 0,
                },
                "online": 0,
                "token": "",
            }
        print(f"Loaded {len(users)} users from {DATA_FILE}")
    except Exception as e:
        print("load_users error:", e)

load_users()


def j(data, code=200):
    body = json.dumps(data, ensure_ascii=False).encode("utf-8")
    return code, body


def hash_pass(u, p):
    return hashlib.sha256(f"{u}::{p}::tetrun-mp".encode()).hexdigest()


def public_user(u, full=False):
    rec = users.get(u) or {}
    now = time.time()
    online = (now - rec.get("online", 0)) < ONLINE_TTL
    out = {
        "user": u,
        "nick": rec.get("nick", u),
        "elo": int(rec.get("elo", 1000)),
        "online": online,
        "avatar": rec.get("avatar") or "",
    }
    if full:
        st = rec.get("stats") or {}
        out["stats"] = {
            "wins": int(st.get("wins", 0)),
            "losses": int(st.get("losses", 0)),
            "streak": int(st.get("streak", 0)),
            "best": int(st.get("best", 0)),
            "points": int(st.get("points", 0)),
            "games": int(st.get("games", 0)),
            "timeSec": int(st.get("timeSec", 0)),
            "pacBest": int(st.get("pacBest", 0)),
        }
        out["friends"] = [
            {
                "user": f,
                "nick": (users[f].get("nick") if f in users else f),
                "online": f in users and (now - users[f].get("online", 0)) < ONLINE_TTL,
                "elo": int(users[f].get("elo", 1000)) if f in users else 0,
            }
            for f in rec.get("friends", [])
        ]
    return out


def touch(u):
    if u in users:
        users[u]["online"] = time.time()


def elo_update(ra, rb, score_a):
    ea = 1 / (1 + 10 ** ((rb - ra) / 400))
    eb = 1 - ea
    na = ra + 32 * (score_a - ea)
    nb = rb + 32 * ((1 - score_a) - eb)
    return int(round(na)), int(round(nb))


def make_code():
    for _ in range(50):
        c = "".join(random.choices(string.ascii_uppercase + string.digits, k=5))
        if c not in lobbies:
            return c
    return uuid.uuid4().hex[:5].upper()


def assign_roles(a, b):
    """Random: one runner, one thrower."""
    if random.random() < 0.5:
        return {a: "runner", b: "thrower"}
    return {a: "thrower", b: "runner"}


def try_matchmake(mode):
    q = queues.setdefault(mode, [])
    now = time.time()
    # keep only currently online unique users
    seen = set()
    live = []
    for u in q:
        if u in seen:
            continue
        if u not in users:
            continue
        if now - users[u].get("online", 0) >= ONLINE_TTL:
            continue
        seen.add(u)
        live.append(u)
    q[:] = live
    if len(q) < 2:
        return None
    a = q.pop(0)
    b = q.pop(0)
    mid = uuid.uuid4().hex[:12]
    roles = assign_roles(a, b) if mode == "roles" else {a: "runner", b: "runner"}
    start_at = time.time() + 3.0
    matches[mid] = {
        "id": mid, "a": a, "b": b, "players": [a, b], "mode": mode,
        "status": "countdown", "ranked": True,
        "roles": roles,
        "events": {a: None, b: None},
        "pos": {a: None, b: None},
        "blocks": [],
        "block_seq": 0,
        "start_at": start_at,
        "started": start_at, "winner": None, "loser": None, "alive": [a, b],
    }
    return matches[mid]


def finish_match(m, winner, loser):
    m["status"] = "done"
    m["winner"] = winner
    m["loser"] = loser
    m["elo_before"] = {}
    m["elo_delta"] = {}
    if winner in users and loser in users:
        ra = int(users[winner].get("elo", 1000))
        rb = int(users[loser].get("elo", 1000))
        m["elo_before"] = {winner: ra, loser: rb}
        # ranked: full K=32; friendly lobby: half effect via double-call style — use K=16 by scaling
        if m.get("ranked"):
            na, nb = elo_update(ra, rb, 1)
        else:
            # half strength
            ea = 1 / (1 + 10 ** ((rb - ra) / 400))
            na = int(round(ra + 16 * (1 - ea)))
            nb = int(round(rb + 16 * (0 - (1 - ea))))
        users[winner]["elo"] = na
        users[loser]["elo"] = nb
        m["elo"] = {winner: na, loser: nb}
        m["elo_delta"] = {winner: na - ra, loser: nb - rb}
        sw = users[winner].setdefault("stats", {})
        sl = users[loser].setdefault("stats", {})
        sw["wins"] = int(sw.get("wins", 0)) + 1
        sw["games"] = int(sw.get("games", 0)) + 1
        sw["streak"] = int(sw.get("streak", 0)) + 1
        sw["best"] = max(int(sw.get("best", 0)), sw["streak"])
        sl["losses"] = int(sl.get("losses", 0)) + 1
        sl["games"] = int(sl.get("games", 0)) + 1
        sl["streak"] = 0
        save_users()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print("[%s] %s" % (self.log_date_time_string(), fmt % args))

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type,Authorization")

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def _read(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return {}

    def _send(self, code, body: bytes):
        self.send_response(code)
        self._cors()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        path = u.path
        qs = parse_qs(u.query)
        with lock:
            if path == "/api/health":
                code, body = j({"ok": True, "users": len(users), "matches": len(matches), "online":
                    sum(1 for x in users.values() if time.time() - x.get("online", 0) < ONLINE_TTL)})
            elif path == "/api/friends":
                user = (qs.get("user") or [""])[0].lower()
                if user not in users:
                    code, body = j({"error": "unknown user"}, 404)
                else:
                    touch(user)
                    fl = [public_user(f) for f in users[user].get("friends", []) if f in users]
                    code, body = j({"friends": fl, "me": public_user(user, full=True)})
            elif path == "/api/profile":
                user = (qs.get("user") or [""])[0].lower()
                if user not in users:
                    code, body = j({"error": "not found"}, 404)
                else:
                    code, body = j({"ok": True, "profile": public_user(user, full=True)})
            elif path == "/api/lobby":
                code_s = (qs.get("code") or [""])[0].upper()
                lob = lobbies.get(code_s)
                if not lob:
                    code, body = j({"error": "not found"}, 404)
                else:
                    pl = lob.get("players") or ([lob["host"]] + ([lob["guest"]] if lob.get("guest") else []))
                    code, body = j({
                        "code": code_s,
                        "host": public_user(lob["host"]),
                        "guest": public_user(lob["guest"]) if lob.get("guest") else None,
                        "players": [public_user(p) for p in pl if p in users],
                        "mode": lob["mode"],
                        "status": lob["status"],
                        "match_id": lob.get("match_id"),
                        "roles": lob.get("roles"),
                        "start_at": lob.get("start_at"),
                    })
            elif path == "/api/queue/status":
                user = (qs.get("user") or [""])[0].lower()
                touch(user)
                in_q = None
                for m, q in queues.items():
                    if user in q:
                        in_q = m
                        break
                mid = None
                for m in matches.values():
                    if m["status"] == "playing" and user in (m["a"], m["b"]):
                        mid = m["id"]
                        break
                code, body = j({"queue": in_q, "match_id": mid})
            elif path == "/api/match":
                mid = (qs.get("id") or [""])[0]
                m = matches.get(mid)
                if not m:
                    code, body = j({"error": "not found"}, 404)
                else:
                    if m.get("status") == "countdown" and time.time() >= float(m.get("start_at") or 0):
                        m["status"] = "playing"
                    pl = m.get("players") or [m["a"], m["b"]]
                    code, body = j({
                        "id": m["id"], "mode": m["mode"], "status": m["status"],
                        "ranked": m.get("ranked", False),
                        "a": public_user(m["a"]), "b": public_user(m["b"]),
                        "players": [public_user(p) for p in pl if p in users],
                        "roles": m.get("roles") or {},
                        "events": m["events"], "winner": m.get("winner"),
                        "loser": m.get("loser"), "elo": m.get("elo"), "elo_delta": m.get("elo_delta"), "elo_before": m.get("elo_before"),
                        "pos": m.get("pos") or {},
                        "blocks": m.get("blocks") or [],
                        "block_seq": m.get("block_seq", 0),
                        "start_at": m.get("start_at"),
                        "server_now": time.time(),
                    })
            elif path == "/api/online":
                now = time.time()
                online = [public_user(u) for u, rec in users.items()
                          if now - rec.get("online", 0) < ONLINE_TTL]
                online.sort(key=lambda x: -x.get("elo", 0))
                code, body = j({"online": online, "count": len(online)})
            elif path == "/api/top":
                top = sorted(users.keys(), key=lambda x: users[x].get("elo", 1000), reverse=True)[:30]
                code, body = j({"top": [public_user(x, full=True) for x in top]})
            elif path == "/api/search":
                q = (qs.get("q") or [""])[0].lower().strip()
                found = []
                if q:
                    for uname, rec in users.items():
                        if q in uname or q in (rec.get("nick") or "").lower():
                            found.append(public_user(uname))
                            if len(found) >= 20:
                                break
                code, body = j({"users": found})
            else:
                code, body = j({"error": "not found"}, 404)
        self._send(code, body)

    def do_POST(self):
        path = urlparse(self.path).path
        data = self._read()
        with lock:
            if path == "/api/auth/register":
                user = str(data.get("user") or "").strip().lower()
                nick = str(data.get("nick") or user).strip()[:16] or user
                pw = str(data.get("pass") or "")
                if len(user) < 3 or len(pw) < 4:
                    code, body = j({"error": "bad credentials"}, 400)
                elif user in users:
                    code, body = j({"error": "taken"}, 409)
                else:
                    token = uuid.uuid4().hex
                    users[user] = {
                        "pass": hash_pass(user, pw), "nick": nick, "elo": 1000,
                        "friends": [], "online": time.time(), "token": token,
                        "avatar": "", "stats": {
                            "wins": 0, "losses": 0, "streak": 0, "best": 0,
                            "points": 0, "games": 0, "timeSec": 0, "pacBest": 0,
                        },
                    }
                    save_users()
                    code, body = j({"ok": True, "user": public_user(user, full=True), "token": token})
            elif path == "/api/auth/login":
                user = str(data.get("user") or "").strip().lower()
                pw = str(data.get("pass") or "")
                rec = users.get(user)
                if not rec or rec["pass"] != hash_pass(user, pw):
                    code, body = j({"error": "auth"}, 401)
                else:
                    rec["token"] = uuid.uuid4().hex
                    rec["online"] = time.time()
                    if data.get("nick"):
                        rec["nick"] = str(data["nick"])[:16]
                    if data.get("avatar") is not None:
                        rec["avatar"] = clamp_avatar(str(data.get("avatar") or "")[:MAX_AVATAR])
                    if data.get("stats") and isinstance(data["stats"], dict):
                        st = rec.setdefault("stats", {})
                        for k in ("wins", "losses", "streak", "best", "points", "games", "timeSec", "pacBest"):
                            if k in data["stats"]:
                                try:
                                    st[k] = int(data["stats"][k])
                                except Exception:
                                    pass
                    save_users()
                    code, body = j({"ok": True, "user": public_user(user, full=True), "token": rec["token"], "elo": rec["elo"]})
            elif path == "/api/heartbeat":
                user = str(data.get("user") or "").strip().lower()
                if user in users:
                    touch(user)
                    if data.get("nick"):
                        users[user]["nick"] = str(data["nick"])[:16]
                    if data.get("stats") and isinstance(data["stats"], dict):
                        st = users[user].setdefault("stats", {})
                        for k in ("wins", "losses", "streak", "best", "points", "games", "timeSec", "pacBest"):
                            if k in data["stats"]:
                                try:
                                    st[k] = int(data["stats"][k])
                                except Exception:
                                    pass
                    code, body = j({"ok": True, "elo": users[user]["elo"]})
                else:
                    code, body = j({"error": "unknown"}, 404)
            elif path == "/api/friend/add":
                user = str(data.get("user") or "").strip().lower()
                fr = str(data.get("friend") or "").strip().lower()
                if user not in users:
                    code, body = j({"error": "you are not synced — press Connect first"}, 401)
                elif fr not in users:
                    code, body = j({"error": "user not found — friend must Register in game and press Connect on the same server"}, 404)
                elif user == fr:
                    code, body = j({"error": "self"}, 400)
                else:
                    if fr not in users[user]["friends"]:
                        users[user]["friends"].append(fr)
                    if user not in users[fr]["friends"]:
                        users[fr]["friends"].append(user)
                    touch(user)
                    save_users()
                    code, body = j({"ok": True, "friends": [public_user(f) for f in users[user]["friends"] if f in users]})
            elif path == "/api/friend/remove":
                user = str(data.get("user") or "").strip().lower()
                fr = str(data.get("friend") or "").strip().lower()
                if user in users and fr in users[user]["friends"]:
                    users[user]["friends"] = [x for x in users[user]["friends"] if x != fr]
                if fr in users and user in users[fr]["friends"]:
                    users[fr]["friends"] = [x for x in users[fr]["friends"] if x != user]
                save_users()
                code, body = j({"ok": True})
            elif path == "/api/lobby/create":
                user = str(data.get("user") or "").strip().lower()
                mode = str(data.get("mode") or "roles")
                if mode not in ("first_win", "first_die", "roles"):
                    mode = "roles"
                if user not in users:
                    code, body = j({"error": "login on server first"}, 401)
                else:
                    for c, lob in list(lobbies.items()):
                        if lob["host"] == user or lob.get("guest") == user:
                            del lobbies[c]
                    code_s = make_code()
                    lobbies[code_s] = {
                        "host": user,
                        "players": [user],
                        "mode": mode,
                        "status": "open", "created": time.time(),
                        "match_id": None, "roles": None,
                        "start_at": None,
                    }
                    touch(user)
                    code, body = j({"ok": True, "code": code_s, "mode": mode})
            elif path == "/api/lobby/join":
                user = str(data.get("user") or "").strip().lower()
                code_s = str(data.get("code") or "").strip().upper()
                lob = lobbies.get(code_s)
                if user not in users:
                    code, body = j({"error": "login"}, 401)
                elif not lob:
                    code, body = j({"error": "lobby not found"}, 404)
                elif lob["status"] != "open":
                    code, body = j({"error": "lobby closed"}, 400)
                else:
                    pl = lob.setdefault("players", [lob["host"]])
                    if user in pl:
                        touch(user)
                        code, body = j({"ok": True, "code": code_s, "mode": lob["mode"], "players": [public_user(p) for p in pl]})
                    elif len(pl) >= 4:
                        code, body = j({"error": "full (max 4)"}, 400)
                    else:
                        pl.append(user)
                        # legacy
                        if not lob.get("guest") and user != lob["host"]:
                            lob["guest"] = user
                        touch(user)
                        code, body = j({"ok": True, "code": code_s, "mode": lob["mode"], "players": [public_user(p) for p in pl]})
            elif path == "/api/lobby/start":
                user = str(data.get("user") or "").strip().lower()
                code_s = str(data.get("code") or "").strip().upper()
                lob = lobbies.get(code_s)
                pl = (lob or {}).get("players") or []
                if not lob or lob["host"] != user:
                    code, body = j({"error": "only host can start"}, 400)
                elif len(pl) < 2:
                    code, body = j({"error": "need at least 2 players"}, 400)
                else:
                    mid = uuid.uuid4().hex[:12]
                    mode = lob["mode"]
                    # assign roles for multi
                    roles = {}
                    if mode == "roles":
                        # one thrower random, rest runners
                        thrower = random.choice(pl)
                        for p in pl:
                            roles[p] = "thrower" if p == thrower else "runner"
                    else:
                        for p in pl:
                            roles[p] = "runner"
                    start_at = time.time() + 3.0  # 3s countdown
                    a, b = pl[0], pl[1] if len(pl) > 1 else pl[0]
                    matches[mid] = {
                        "id": mid, "a": a, "b": b,
                        "players": list(pl),
                        "mode": mode,
                        "status": "countdown",  # then playing
                        "ranked": False,
                        "roles": roles,
                        "events": {p: None for p in pl},
                        "pos": {p: None for p in pl},
                        "blocks": [],
                        "block_seq": 0,
                        "start_at": start_at,
                        "started": start_at,
                        "winner": None, "loser": None, "alive": list(pl),
                    }
                    lob["status"] = "countdown"
                    lob["match_id"] = mid
                    lob["roles"] = roles
                    lob["start_at"] = start_at
                    code, body = j({"ok": True, "match_id": mid, "mode": mode, "roles": roles, "start_at": start_at, "players": pl})
            elif path == "/api/lobby/leave":
                user = str(data.get("user") or "").strip().lower()
                for c, lob in list(lobbies.items()):
                    pl = lob.get("players") or []
                    if lob["host"] == user:
                        del lobbies[c]
                    elif user in pl:
                        lob["players"] = [p for p in pl if p != user]
                        if lob.get("guest") == user:
                            lob["guest"] = None
                        lob["status"] = "open"
                        lob["match_id"] = None
                        lob["roles"] = None
                code, body = j({"ok": True})
            elif path == "/api/queue":
                user = str(data.get("user") or "").strip().lower()
                mode = str(data.get("mode") or "roles")
                if mode not in queues:
                    mode = "roles"
                if user not in users:
                    code, body = j({"error": "login"}, 401)
                else:
                    now = time.time()
                    for q in queues.values():
                        if user in q:
                            q.remove(user)
                    queues[mode].append(user)
                    touch(user)
                    m = try_matchmake(mode)
                    if m:
                        opp = m["b"] if m["a"] == user else m["a"]
                        code, body = j({
                            "ok": True, "matched": True, "match_id": m["id"],
                            "mode": mode, "roles": m["roles"],
                            "opponent": public_user(opp),
                            "my_role": m["roles"].get(user),
                            "start_at": m.get("start_at"),
                        })
                    else:
                        waiting = len(queues.get(mode, []))
                        online_n = sum(1 for rec in users.values() if now - rec.get("online", 0) < ONLINE_TTL)
                        code, body = j({
                            "ok": True, "matched": False, "queue": mode,
                            "waiting": waiting, "online": online_n,
                        })
            elif path == "/api/queue/cancel":
                user = str(data.get("user") or "").strip().lower()
                for q in queues.values():
                    if user in q:
                        q.remove(user)
                code, body = j({"ok": True})
            elif path == "/api/match/pos":
                mid = str(data.get("match_id") or "")
                user = str(data.get("user") or "").strip().lower()
                m = matches.get(mid)
                pl = (m or {}).get("players") or ([m["a"], m["b"]] if m else [])
                if not m or user not in pl:
                    code, body = j({"error": "bad match"}, 400)
                else:
                    if m.get("status") == "countdown" and time.time() >= float(m.get("start_at") or 0):
                        m["status"] = "playing"
                    m.setdefault("pos", {})[user] = {
                        "x": float(data.get("x", 0)),
                        "y": float(data.get("y", 0)),
                        "face": bool(data.get("face", True)),
                        "state": str(data.get("state") or "idle")[:12],
                        "nick": str(data.get("nick") or user)[:16],
                        "dead": bool(data.get("dead")),
                        "won": bool(data.get("won")),
                        "t": time.time(),
                    }
                    code, body = j({"ok": True, "pos": m["pos"], "blocks": m.get("blocks") or [], "block_seq": m.get("block_seq", 0), "status": m["status"], "winner": m.get("winner")})
            elif path == "/api/match/throw":
                mid = str(data.get("match_id") or "")
                user = str(data.get("user") or "").strip().lower()
                m = matches.get(mid)
                roles = (m or {}).get("roles") or {}
                if not m or user not in (m["a"], m["b"]):
                    code, body = j({"error": "bad match"}, 400)
                elif m["status"] not in ("playing", "countdown"):
                    code, body = j({"error": "match over"}, 400)
                elif m["status"] == "countdown":
                    code, body = j({"error": "wait for start"}, 400)
                elif roles.get(user) not in ("thrower", None) and m.get("mode") == "roles":
                    code, body = j({"error": "only thrower"}, 403)
                else:
                    shape = data.get("shape")
                    if not isinstance(shape, list) or not shape:
                        code, body = j({"error": "bad shape"}, 400)
                    else:
                        # sanitize shape to 0/1 matrix max 4x4
                        clean = []
                        for row in shape[:4]:
                            if not isinstance(row, list):
                                continue
                            clean.append([1 if c else 0 for c in row[:4]])
                        if not clean:
                            code, body = j({"error": "bad shape"}, 400)
                        else:
                            gx = int(data.get("gx", 0))
                            gx = max(0, min(9, gx))
                            m["block_seq"] = int(m.get("block_seq", 0)) + 1
                            piece = {
                                "id": m["block_seq"],
                                "shape": clean,
                                "gx": gx,
                                "from": user,
                                "t": time.time(),
                            }
                            bl = m.setdefault("blocks", [])
                            bl.append(piece)
                            # keep last 30
                            if len(bl) > 30:
                                m["blocks"] = bl[-30:]
                            code, body = j({"ok": True, "block_seq": m["block_seq"], "id": piece["id"]})
            elif path == "/api/match/event":
                mid = str(data.get("match_id") or "")
                user = str(data.get("user") or "").strip().lower()
                ev = str(data.get("event") or "")  # win | die
                m = matches.get(mid)
                if not m or user not in (m["a"], m["b"]):
                    code, body = j({"error": "bad match"}, 400)
                elif m["status"] != "playing":
                    code, body = j({"ok": True, "status": m["status"], "winner": m.get("winner"), "roles": m.get("roles")})
                else:
                    m["events"][user] = ev
                    winner = loser = None
                    mode = m["mode"]
                    roles = m.get("roles") or {}
                    # roles mode: runner wins by finishing; runner dies = thrower wins; thrower has no "win" alone
                    if mode == "roles":
                        role = roles.get(user)
                        if role == "runner" and ev == "win":
                            winner, loser = user, (m["b"] if user == m["a"] else m["a"])
                        elif role == "runner" and ev == "die":
                            loser, winner = user, (m["b"] if user == m["a"] else m["a"])
                        elif role == "thrower" and ev == "die":
                            # thrower somehow died — runner wins
                            loser, winner = user, (m["b"] if user == m["a"] else m["a"])
                    elif mode == "first_win":
                        if ev == "win":
                            winner, loser = user, (m["b"] if user == m["a"] else m["a"])
                    else:  # first_die
                        if ev == "die":
                            loser, winner = user, (m["b"] if user == m["a"] else m["a"])
                    if winner:
                        finish_match(m, winner, loser)
                    code, body = j({
                        "ok": True, "status": m["status"],
                        "winner": m.get("winner"), "loser": m.get("loser"),
                        "elo": m.get("elo"), "elo_delta": m.get("elo_delta"), "elo_before": m.get("elo_before"), "roles": roles,
                    })
            else:
                code, body = j({"error": "not found"}, 404)
        self._send(code, body)


def main():
    httpd = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"TetRun MP server on http://0.0.0.0:{PORT}")
    print("Health: GET /api/health")
    httpd.serve_forever()


if __name__ == "__main__":
    main()
