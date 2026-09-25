"""みんなのランキング。

ステージをクリアするたびに、サーバが自分で計算した結果 (タイム・ランク) と、
そのステージを遊んでいた間の往復遅延を記録する。ブラウザから点数を受け取らないので、ずるはできない。

  * 順位は 1 人 1 行: ニックネームごと (無ければその回のセッションごと) に、ステージ別の最速記録で並べる
  * 経路別の集計: 同じコースでも、遅い経路ではタイムがどれだけ伸びるかを並べて見せる。
    経路の名前は MORIO_NET_FILE (JSON の "short"、無ければ "label") から取る。無ければ遅延の大きさで分ける
  * MORIO_BOARD にパスを書くと記録を追記して、再起動しても残す。
    未指定ならメモリだけ (Raspberry Pi の SD カードには書かない)
"""
import hashlib
import json
import os
import statistics
import time
import unicodedata

NAME_MAX = 10          # ニックネームの最大文字数
KEEP = 5000            # 記録の上限 (超えたら古いものから捨てる)
TOP = 10
NAMELESS = "ななしさん"
# 経路の名前が無いときの分け方: (この遅延[ms]未満なら, 表示名)
RTT_BUCKETS = [(50, "遅延 50ms 未満"), (200, "遅延 50〜200ms"), (500, "遅延 200〜500ms"),
               (float("inf"), "遅延 500ms 以上")]


def clean_name(raw) -> str:
    """表示してよい形にそろえる: 全角英数は半角に、制御文字などは落とし、NAME_MAX 文字まで"""
    s = unicodedata.normalize("NFKC", str(raw or ""))
    s = "".join(" " if c.isspace() else c for c in s                 # 改行やタブは空白に
                if c.isspace() or not unicodedata.category(c).startswith("C"))
    return " ".join(s.split())[:NAME_MAX]


def name_key(name: str) -> str:
    return clean_name(name).casefold()


def run_key(sid, name) -> str:
    """順位をまとめる単位。ニックネームがあればそれ、無ければその回のセッション"""
    name = clean_name(name)
    return name.casefold() if name else "sid:" + str(sid)


def public_id(key: str) -> str:
    """ブラウザに見せる行の目印。セッション ID (操作の鍵) を漏らさないようハッシュにする"""
    return hashlib.sha256(key.encode()).hexdigest()[:12]


def net_label(path=None):
    """今の経路の名前 (MORIO_NET_FILE の {"short": ...} か {"label": ...})。読めなければ None"""
    path = path or os.environ.get("MORIO_NET_FILE")
    if not path:
        return None
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        label = d.get("short") or d.get("label")
    except (OSError, ValueError, AttributeError):
        return None
    return str(label)[:40] if label else None


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _valid_run(r):
    """保存ファイルから読んだ記録が、集計に使える形か (欠けた行・型違いの行は読み飛ばす)"""
    return (isinstance(r, dict) and isinstance(r.get("key"), str) and bool(r["key"])
            and isinstance(r.get("stage"), int) and not isinstance(r["stage"], bool)
            and _num(r.get("time")) and _num(r.get("at")) and isinstance(r.get("name"), str)
            and (r.get("rtt") is None or _num(r["rtt"]))
            and (r.get("net") is None or isinstance(r["net"], str)))


def _bucket(rtt):
    if rtt is None:
        return "遅延 不明"
    return next(label for lim, label in RTT_BUCKETS if rtt < lim)


class Board:
    def __init__(self, path=None):
        self.path = path
        self.runs = []                 # 古い順
        self.hidden = set()            # 非表示にしたニックネーム (name_key)
        self.version = 0
        self.lines = 0                 # 保存ファイルの行数 (増えすぎたら今の中身だけで書き直す)
        self._cache = (None, None)
        self._load()

    # ---- 保存 (1 行 1 操作の追記。途中で落ちても前の行までは読める) ----------------
    def _load(self):
        if not self.path:
            return
        try:
            with open(self.path, encoding="utf-8") as f:
                for line in f:
                    self.lines += 1
                    try:
                        self._apply(json.loads(line))
                    except (ValueError, KeyError, TypeError, AttributeError):
                        continue
        except OSError:
            return
        if self.lines > 2 * KEEP:
            self._compact()

    def _apply(self, op):
        kind = op.get("op")
        if kind == "run":
            if not _valid_run(op["run"]):
                raise ValueError("incomplete run")
            self.runs.append(op["run"])
            if len(self.runs) > KEEP:
                del self.runs[:len(self.runs) - KEEP]
        elif kind in ("hide", "unhide"):
            if not isinstance(op["key"], str):
                raise ValueError("bad key")
            if kind == "hide":
                self.hidden.add(op["key"])
            else:
                self.hidden.discard(op["key"])
        self.version += 1

    def _write(self, op):
        self._apply(op)
        if not self.path:
            return
        try:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(op, ensure_ascii=False) + "\n")
            self.lines += 1
        except OSError:
            return                     # 書けなくてもゲームは止めない (メモリには残る)
        if self.lines > 2 * KEEP:
            self._compact()

    def _compact(self):
        """メモリは KEEP 件で打ち切っているので、ファイルも今の中身 (記録と非表示) だけで書き直す"""
        ops = [{"op": "run", "run": r} for r in self.runs] + \
              [{"op": "hide", "key": k} for k in sorted(self.hidden)]
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                f.writelines(json.dumps(op, ensure_ascii=False) + "\n" for op in ops)
            os.replace(tmp, self.path)
            self.lines = len(ops)
        except OSError:
            pass

    # ---- 記録 --------------------------------------------------------------------
    def add(self, sid, name, result, net=None, now=None):
        """ステージの結果を 1 件記録し、順位を返す。{"place", "players", "best"}"""
        name = clean_name(name)
        key = run_key(sid, name)
        stage = int(result["stage"])
        prev = self._bests(stage).get(key)
        run = {"name": name, "key": key, "stage": stage,
               "time": float(result["time"]), "rank": result.get("rank"),
               "coins": result.get("coins"), "coins_total": result.get("coins_total"),
               "deaths": result.get("deaths"), "rtt": result.get("rtt"), "net": net,
               "at": round(now if now is not None else time.time(), 1)}
        self._write({"op": "run", "run": run})
        order = self._order(stage)
        hidden = key in self.hidden
        return {"place": None if hidden else order.index(key) + 1,
                "players": len(order),
                "best": prev is None or run["time"] < prev["time"]}

    def hide(self, name) -> str:
        key = name_key(name)
        self._write({"op": "hide", "key": key})
        return key

    def unhide(self, name) -> str:
        key = name_key(name)
        self._write({"op": "unhide", "key": key})
        return key

    def reset(self):
        """全消去 (ファイルも空にする)"""
        self.runs.clear()
        self.hidden.clear()
        self.version += 1
        if self.path:
            try:
                open(self.path, "w", encoding="utf-8").close()
                self.lines = 0
            except OSError:
                pass

    # ---- 集計 --------------------------------------------------------------------
    def _bests(self, stage):
        """name_key -> そのステージの最速記録 (同タイムなら先に出した方)"""
        best = {}
        for r in self.runs:
            if r["stage"] != stage or r["key"] in self.hidden:
                continue
            b = best.get(r["key"])
            if b is None or r["time"] < b["time"]:
                best[r["key"]] = r
        return best

    def _order(self, stage):
        best = self._bests(stage)
        return sorted(best, key=lambda k: (best[k]["time"], best[k]["at"]))

    def _latest_names(self):
        names = {}
        for r in self.runs:
            if r["name"]:
                names[r["key"]] = r["name"]
        return names

    def view(self, stages):
        """GET /game/board の中身。記録が変わったときだけ作り直す"""
        if self._cache[0] == self.version:
            return self._cache[1]
        names = self._latest_names()
        out = []
        for stage in range(stages):
            best = self._bests(stage)
            order = self._order(stage)
            top = [{"id": public_id(k), "name": names.get(k, NAMELESS), "time": best[k]["time"],
                    "rank": best[k]["rank"], "rtt": best[k]["rtt"],
                    "net": best[k]["net"] or _bucket(best[k]["rtt"])}
                   for k in order[:TOP]]
            groups = {}
            for r in self.runs:
                if r["stage"] == stage:
                    groups.setdefault(r["net"] or _bucket(r["rtt"]), []).append(r)
            nets = []
            for label, rs in groups.items():
                rtts = [r["rtt"] for r in rs if r["rtt"] is not None]
                nets.append({"net": label, "runs": len(rs),
                             "players": len({r["key"] for r in rs}),
                             "best": min(r["time"] for r in rs),
                             "median": round(statistics.median(r["time"] for r in rs), 1),
                             "rtt": round(statistics.median(rtts)) if rtts else None})
            # 遅い経路から順に (経路切替ボタンの並びと同じ「悪い → 良い」)
            nets.sort(key=lambda n: -(n["rtt"] if n["rtt"] is not None else -1))
            out.append({"stage": stage, "players": len(order), "top": top, "nets": nets})
        view = {"stages": out, "runs": len(self.runs), "version": self.version}
        self._cache = (self.version, view)
        return view
