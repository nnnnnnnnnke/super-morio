"""スーパモリオ常駐ゲームサーバの起動スクリプト。

gunicorn は WebSocket の Upgrade リクエストを 400 で弾くので使わない。
gevent の pywsgi を直接使う。プロセス管理は systemd に任せる。

  python3 gamerun.py            # 既定 127.0.0.1:5001
  YOHGAME_BIND=0.0.0.0:5001 python3 gamerun.py
"""
import os
import sys

from gevent import monkey

monkey.patch_all()

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from gevent.pywsgi import WSGIServer  # noqa: E402
from flask import Flask  # noqa: E402

import gamesrv  # noqa: E402


def main():
    app = Flask(__name__)
    app.register_blueprint(gamesrv.game_bp)
    gamesrv.ensure_loop()

    bind = os.environ.get("YOHGAME_BIND", "127.0.0.1:5001")
    host, _, port = bind.rpartition(":")
    srv = WSGIServer((host or "127.0.0.1", int(port)), app, log=None)
    print(f"スーパモリオ常駐サーバ: http://{bind}  "
          f"({gamesrv.TICK_HZ}Hz / push {gamesrv.PUSH_HZ}Hz)", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
