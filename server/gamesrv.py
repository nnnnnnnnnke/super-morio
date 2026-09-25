"""スーパモリオ 常駐ゲームサーバ (gevent + WebSocket)。

従来は「HTTPリクエストが来たら、その分だけ時間を進める」方式だった。
これには構造的な問題があった:

  * 経路が遅いとリクエストが詰まり、更新が 8Hz まで落ちる
  * リクエスト間に完結した短いキー入力が丸ごと消える
  * 追いつき上限(MAX_CATCHUP)を超えた実時間が捨てられ、
    世界の進行とタイマーがずれる

そこでサーバに 60Hz の常駐ループを置き、世界は実時間で進み続ける形にした。
ネットワークが遅れて影響するのは「入力が届く時刻」と「絵が見える時刻」だけ
になり、遅延の教材としてもむしろ純粋になる。

  * 状態はプロセス内メモリ (Pi は 7.8GB あるので tmpfs+JSON+flock は不要)
  * WebSocket が使えれば入力は即送信・状態は 30Hz でプッシュ
  * 使えない環境のために HTTP ポーリングも同じ状態を共有して残す

gamerun.py (gevent の pywsgi) から単一プロセスで起動すること。
gunicorn は WebSocket の Upgrade を 400 で弾くので使わない。
プロセスを増やすと状態が分裂する。
"""
import os
import time

from flask import Blueprint, jsonify, request

import game as G

game_bp = Blueprint("gamesrv", __name__)

TICK_HZ = 60
PUSH_HZ = 30
SESSION_TTL = 600           # 無通信でこの秒数を超えたセッションは破棄
MAX_SESSIONS = 64           # 暴走・悪意ある大量生成の上限

_sessions = {}              # sid -> {"s":state, "keys":..., "seen":t, "socks":set()}
_loop = None


def _blank(sid):
    return {"s": G._new_state(), "keys": {}, "seen": time.time(),
            "socks": set(), "seq": 0}


def _sess(sid, reset=False):
    e = _sessions.get(sid)
    if e is None:
        if len(_sessions) >= MAX_SESSIONS:
            _reap(force=True)
        if len(_sessions) >= MAX_SESSIONS:
            return None
        e = _sessions[sid] = _blank(sid)
    elif reset:
        socks = e["socks"]
        e.update(_blank(sid))
        e["socks"] = socks
    e["seen"] = time.time()
    return e


def _reap(force=False):
    now = time.time()
    for sid, e in list(_sessions.items()):
        idle = now - e["seen"]
        if idle > SESSION_TTL or (force and idle > 30 and not e["socks"]):
            _sessions.pop(sid, None)


def _apply_keys(e, keys):
    """入力を反映。ジャンプの押下エッジはここで1回だけ立てる。"""
    s, old = e["s"], e["keys"]
    if keys.get("jump") and not old.get("jump"):
        s["jbuf"] = G.JBUF_F
    e["keys"] = {k: bool(keys.get(k)) for k in ("left", "right", "jump", "run")}


def _snapshot(e):
    s = e["s"]
    st = G.STAGES[s["stage"]]
    return {
        "seq": e["seq"],
        "stage": s["stage"],
        "x": round(s["x"], 1), "y": round(s["y"], 1),
        "vx": round(s["vx"], 2), "vy": round(s["vy"], 2),
        "face": s["face"], "skid": s["skid"],
        "on_ground": s["on_ground"], "invuln": s["invuln"],
        "dead": s["dead"], "cp": s["cp"],
        "coins": s["coins"], "total_coins": s["total_coins"],
        "score": s["score"], "fx": s["fx"],
        "cleared": s["cleared"],
        "deaths": s["deaths"], "stomps": s["stomps"],
        "event": s["event"], "events": s.get("events", []),
        "enemies": [[round(en["x"], 1), round(en["y"], 1), G._enemy_code(en),
                     1 if en["vx"] >= 0 else -1, en["squash"],
                     1 if (en["mode"] == "shell" and
                           en["revive"] >= G.SHELL_REVIVE_F - G.SHELL_WIGGLE_F) else 0]
                    for en in s["enemies"]],
        "shield": s.get("shield", False),
        "star": s.get("star", 0),
        "used_q": s.get("used_q", []),
        "bump": s.get("bump", [-1, 0]),
        "items": [[round(it["x"], 1), round(it["y"], 1), it["emerge"]]
                  for it in s.get("items", [])],
        "crumbles": [[c["ph"], c["t"]] for c in s.get("crumbles", [])],
        "roul": (s["steps"] // G.ROULETTE_F) % len(G.ROULETTE_SEQ),
        "steps": s["steps"],
        "bullets": [[round(b["x"], 1), round(b["y"], 1)] for b in s.get("bullets", [])],
        "gseq": s.get("gseq", 0),
        "chain": s.get("chain", 0),
        "result": s.get("result") if (s.get("result_t", 0) > 0 or s["cleared"]) else None,
        "result_t": s.get("result_t", 0),
        "boss": ([round(s["boss"]["x"], 1), round(s["boss"]["y"], 1),
                  s["boss"]["mode"], -1 if s["x"] < s["boss"]["x"] else 1,
                  s["boss"]["anim"]] if s.get("boss") else None),
        "flames": [[round(f["x"], 1), round(f["y"], 1)] for f in s.get("flames", [])],
        "bridge": s.get("bridge", 0),
        "phase": s.get("phase", "play"),
        "text_i": s.get("text_i", -1),
        "plants": [[st["pipes"][pl["pi"]][0] + st["pipes"][pl["pi"]][2] / 2,
                    st["pipes"][pl["pi"]][1], round(pl["off"], 1)]
                   for pl in s.get("plants", [])],
        "elapsed": round(s["clear_time"] if s["cleared"] and s["clear_time"]
                         else time.time() - s["started"], 1),
        "clear_time": s["clear_time"],
        "srv": "ws",
    }


def _run_loop():
    """全セッションを 60Hz で進める常駐ループ。"""
    import gevent
    import json as _json
    step = 1.0 / TICK_HZ
    nxt = time.time()
    frame = 0
    while True:
        now = time.time()
        if now < nxt:
            gevent.sleep(nxt - now)
        nxt += step
        if nxt < time.time() - 0.5:      # 大きく遅れたら追いつきを諦める
            nxt = time.time()
        frame += 1
        push = (frame % max(1, TICK_HZ // PUSH_HZ)) == 0

        for sid, e in list(_sessions.items()):
            s = e["s"]
            # イベントとスコアポップは「送るまで」貯める
            if push:
                s["event"] = ""
                s.setdefault("events", []).clear()
                s["fx"] = []
            G._step(s, e["keys"])
            if not push or not e["socks"]:
                continue
            e["seq"] += 1
            msg = _json.dumps(_snapshot(e))
            for ws in list(e["socks"]):
                try:
                    ws.send(msg)
                except Exception:
                    e["socks"].discard(ws)
        if frame % (TICK_HZ * 30) == 0:
            _reap()


def ensure_loop():
    global _loop
    if _loop is None:
        import gevent
        _loop = gevent.spawn(_run_loop)
    return _loop


def _safe_sid(raw):
    sid = "".join(c for c in str(raw) if c.isalnum())[:32]
    return sid or None


@game_bp.route("/game/world")
def world():
    return jsonify(G.WORLD)


@game_bp.route("/game/ws", websocket=True)
def ws_route():
    """WebSocket。クライアントは入力を即送り、状態を 30Hz で受け取る。"""
    import simple_websocket
    import json as _json
    ensure_loop()
    # ping_interval は使わない: pingは別グリーンレットから送られ、60Hz配信と
    # 同じソケットに同時書き込みしてフレームを壊す (Invalid frame header)。
    # 30Hzで常時データが流れるので keepalive としても不要。
    ws = simple_websocket.Server(request.environ)
    e = None
    try:
        while True:
            raw = ws.receive(timeout=60)
            if raw is None:
                if ws.connected:
                    continue
                break
            try:
                d = _json.loads(raw)
            except ValueError:
                continue
            sid = _safe_sid(d.get("sid"))
            if not sid:
                break
            if e is None or d.get("reset"):
                if e is not None:
                    e["socks"].discard(ws)
                e = _sess(sid, reset=bool(d.get("reset")))
                if e is None:
                    break
                e["socks"].add(ws)
            e["seen"] = time.time()
            _apply_keys(e, d.get("keys") or {})
    except Exception:
        pass
    finally:
        if e is not None:
            e["socks"].discard(ws)
        try:
            ws.close()
        except Exception:
            pass
    return ""


@game_bp.route("/game/tick", methods=["POST"])
def tick():
    """HTTP ポーリング。WebSocket が使えない場合の代替経路。
    世界を進めるのは常駐ループなので、ここでは入力の投函と状態の取得だけ。"""
    ensure_loop()
    d = request.get_json(silent=True) or {}
    sid = _safe_sid(d.get("sid"))
    if not sid:
        return jsonify({"error": "bad sid"}), 400
    e = _sess(sid, reset=bool(d.get("reset")))
    if e is None:
        return jsonify({"error": "busy"}), 503
    _apply_keys(e, d.get("keys") or {})
    snap = _snapshot(e)
    snap["seq"] = d.get("seq", 0)
    snap["srv"] = "http"
    # HTTP 経路では応答を返した時点でイベントを消費する
    s = e["s"]
    s["event"] = ""
    s.setdefault("events", []).clear()
    s["fx"] = []
    return jsonify(snap)


@game_bp.route("/game/stats")
def stats():
    return jsonify({"sessions": len(_sessions),
                    "sockets": sum(len(e["socks"]) for e in _sessions.values()),
                    "tick_hz": TICK_HZ, "push_hz": PUSH_HZ,
                    "pid": os.getpid()})
