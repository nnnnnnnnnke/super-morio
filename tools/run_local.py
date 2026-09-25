"""ローカルでスーパモリオを動かす最小サーバ。

    uv run --with flask --with gevent --with simple-websocket tools/run_local.py
    → http://localhost:5099/morio

本番では nginx が静的ページ配信と /game/* の中継 (WebSocket Upgrade 込み) を
担当する。ここではその両方を1プロセスで代行する。
"""
import os
import sys

from gevent import monkey

monkey.patch_all()

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "server"))

from gevent.pywsgi import WSGIServer  # noqa: E402
from flask import Flask, send_from_directory  # noqa: E402

import gamesrv  # noqa: E402

app = Flask(__name__)
app.register_blueprint(gamesrv.game_bp)


@app.route("/")
@app.route("/morio")
def page():
    return send_from_directory(os.path.join(ROOT, "web"), "index.html")


gamesrv.ensure_loop()

if __name__ == "__main__":
    bind = os.environ.get("MORIO_BIND", "127.0.0.1:5099")
    host, _, port = bind.rpartition(":")
    print(f"スーパモリオ: http://{bind}/morio", flush=True)
    WSGIServer((host or "127.0.0.1", int(port)), app, log=None).serve_forever()
