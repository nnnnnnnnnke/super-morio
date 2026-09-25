"""スーパモリオ v3 - サーバ権威型の横スクロールアクション.

遅延の体感用。ブラウザは「キー入力を送る」「返ってきた状態を描く」だけで、
物理演算・当たり判定・敵とコウラの挙動はすべてサーバが行う。

物理は SMB1 系の設計を踏襲:
  - 60Hz 固定サブステップ (1フレーム = 1/60秒。HTTPティックは約20Hzだが、
    経過時間ぶんのフレームをまとめて進めるので実時間で一定)
  - 慣性: 歩行/走行の加速度、離した時の摩擦、逆入力のスキッド(急ブレーキ)
  - 可変ジャンプ: 踏切時の速度で初速と重力の組が決まり、
    ボタンを離すと「速度を切る」のではなく「重力を強める」
  - 空中は摩擦なし。空中加速は現在速度で歩行/走行の加速度を選ぶ
  - 踏みつけチェーン (着地でリセット)、コウラの蹴り/滑走/復活

遅延デモとしての意図的な非忠実ポイント:
  - コヨーテタイム 12f とジャンプバッファ 8f を入れる
    (0.5秒遅れて届く入力でも「操作が成立する」ようにするため)

gunicorn は複数ワーカーで動くためセッション状態は tmpfs 上の JSON に置き、
fcntl で排他する。SDカードには書かない。
"""
import fcntl
import json
import math
import os
import random
import tempfile
import time

from flask import Blueprint, jsonify, request

game_bp = Blueprint("game", __name__)

_SHM = "/dev/shm" if os.path.isdir("/dev/shm") else tempfile.gettempdir()
STATE_DIR = os.environ.get("YOHGAME_STATE", os.path.join(_SHM, "yohtube-game"))

SESSION_TTL = 600
STATE_V = 8                # 互換のない旧状態を捨てるためのバージョン
STEP_DT = 1.0 / 60.0       # 1フレーム = 1/60秒
MAX_CATCHUP = 36           # 一度に進める上限 (0.6秒。詰まりからの暴走防止)

# --- 世界の寸法 -------------------------------------------------------------
VIEW_W, VIEW_H = 800, 450
PLAYER_W, PLAYER_H = 26, 36
ENEMY_W, ENEMY_H = 28, 26
GROUND_Y = 410

# --- 物理定数 (px / frame @60Hz) --------------------------------------------
# SMB1 の値を世界スケール(約1.5倍)に合わせて再調整したもの。比率は本家準拠:
#   走り = 歩き x1.63 / スキッド摩擦 = 通常摩擦 x2 / 走りジャンプは初速+17%
MIN_WALK = 0.15
MAX_WALK = 2.4
MAX_RUN = 3.9
ACC_WALK = 0.056           # 約43fで歩行最高速
ACC_RUN = 0.085            # 約46fで走行最高速
DEC_REL = 0.078            # 入力を離した時の摩擦
DEC_SKID = 0.156           # 逆入力(スキッド)の減速。摩擦の2倍
SKID_TURN = 0.9            # これを下回ったら向きを反転
MAX_FALL = 6.5

# --- ジャンプ: 定数を直接いじらず「到達したい高さと時間」から逆算する ----------
# こうするとレベル設計と直結する。「この足場は3タイル上」を数値に写せる。
#   初速  v0 = -2h / t_up
#   上昇重力 g_up  = 2h / t_up^2
#   落下重力 g_down = 2h / t_down^2
# 落下を上昇より速くするのが現代の定番 (t_down < t_up)。キビキビする。
def _jump_from(height, t_up, t_down):
    """height[px], t_up/t_down[秒] -> (初速, 上昇重力, 落下重力) すべて px/frame"""
    fu, fd = t_up / STEP_DT, t_down / STEP_DT      # 秒 -> フレーム
    return (-2.0 * height / fu, 2.0 * height / (fu * fu), 2.0 * height / (fd * fd))


# 踏切時の |vx| で選ぶ: (この速度未満なら, 高さpx, 上昇秒, 落下秒)
JUMP_DESIGN = [
    (1.6,          112.0, 0.62, 0.40),   # 静止〜歩き出し
    (3.7,          128.0, 0.66, 0.40),   # 歩き
    (float("inf"), 138.0, 0.62, 0.35),   # ダッシュ: 高く、落ちも速い
]
JUMP_TABLE = [(lim,) + _jump_from(h, tu, td) for lim, h, tu, td in JUMP_DESIGN]
GRAV_FALL_DEFAULT = _jump_from(112.0, 0.62, 0.40)[2]   # 縁から歩き落ちする時の重力
# ばねも同じ導出: 通常190px / 押しながら235px (走りジャンプ139pxより明確に高い)。
# 上昇と下降を同じ時間にすると、ボタンを離していても設計高さがそのまま出る
# (可変ジャンプの「離すと重力が強まる」機構の影響を受けない)。
SPRING_JUMP = _jump_from(190.0, 0.52, 0.52)
SPRING_JUMP_HELD = _jump_from(235.0, 0.58, 0.50)
# 二段ジャンプ (現代の標準装備): 空中でもう一度だけ跳べる。
# 遅延で着地点を誤っても空中で修正できる = 遅延下の理不尽さを大きく下げる
DOUBLE_JUMP = _jump_from(100.0, 0.50, 0.36)

# 頂点付近の補正。ここが無いと「頂点が一瞬すぎて足場を狙えない」と言われる。
# --- 仕掛け (様々なゲームの古典から) ------------------------------------------
# ?ブロック (SMB): 下から叩くと中身が出る。ルーレットブロックは表示中の絵柄が当たる
QBLOCK_W, QBLOCK_H = 30, 28
BUMP_F = 10                # 叩かれた時のバウンド演出フレーム
ROULETTE_SEQ = ["c200", "c1000", "star", "shield"]
ROULETTE_F = 40            # 絵柄1つの表示フレーム (0.66秒)。遅延0.5秒だと先読みが要る
STAR_F = 8 * 60            # スター無敵 (SMB)
STAR_CHAIN = [200, 400, 800, 1000, 2000, 4000, 5000, 8000]
SHIELD_SCORE = 1000
ITEM_W, ITEM_H = 24, 24    # キノコ
ITEM_V = 1.1
ITEM_EMERGE_F = 45
# ばね (SMB/ソニック): 通常ジャンプより高く飛ぶ。長押しでさらに伸びる
SPRING_W, SPRING_H = 30, 14
# ベルトコンベア (SMB3工場面): 乗っている間、横に流される
BELT_V = 0.9
# 氷床 (SMB3): 加速も減速も効きが悪い
ICE_MULT = 0.25
# 崩れる床 (Celeste/SMB断崖): 乗ると揺れて落ち、しばらくすると復活
CRUMBLE_SHAKE_F, CRUMBLE_GONE_F = 45, 180   # 揺れ0.75秒 (遅延0.5秒+反応猶予)
# はねガメ (SMB): 空を上下に舞うコウラ族。踏むと羽が取れて普通のコウラ族になる
PARA_AMP, PARA_SPD = 22.0, 0.045
# ファイアバー (SMB城): steps から決定論的に角度が決まる。状態を一切持たない
# = 周期が読めるので、遅延があっても「予測」で勝てることを示す仕掛け
FIREBAR_R = 22             # 玉と玉の間隔
# ずどん砲台 (SMB): 土管の上に乗り、周期的に弾を撃つ。発射は steps から決定論
# = 周期を読めば遅延があっても避けられる。発射前には砲口が光って予告する
CANNON_P = 240             # 発射周期 (4秒)
CANNON_WARN_F = 50         # 予告時間 (0.83秒 > 遅延0.5秒: 遅い経路でも予告が届く)
BULLET_W, BULLET_H = 18, 14
BULLET_V = 1.8
# 上下リフト (SMB): steps の正弦波で決定論的に動く足場。状態を持たない
# 旗の演出 (SMB): ポールを掴んで滑り降り、少し歩いてから次のステージへ
GSEQ_F = 75                # 演出全体 (滑り45f + 歩き30f)
RESULT_F = 300             # ステージ評価バナーの表示時間 (5秒)
GSEQ_WALK = 30

APEX_V = 1.5           # |vy| がこれ以下なら頂点付近とみなす
APEX_GRAV = 0.55       # 頂点付近は重力を弱めて滞空の"溜め"を作る
APEX_SPEED = 1.12      # 頂点付近は水平速度を少し伸ばす("すっと抜ける"感触)
CORNER_PX = 5          # 天井の角に頭が当たったら、この範囲で横にずらして通す

STOMP_V, STOMP_V_HELD = -4.5, -6.0
COYOTE_F = 12              # 足場を離れて跳べる猶予
JBUF_F = 8                 # 着地前のジャンプ先行入力
INVULN_F = 90
DEATH_F = 24        # 0.4秒。ここを伸ばすと体感難易度が跳ね上がる
SQUASH_F = 36

ENEMY_V = 0.85
SHELL_V = 4.5              # コウラ滑走 (走行よりやや速い)
SHELL_REVIVE_F = 8 * 60    # コウラ復活まで
SHELL_WIGGLE_F = 2 * 60    # 復活予告(足が出る)の時間
KICK_GRACE_F = 12          # 蹴った直後は自分に当たらない

# --- かみつき草 (土管から出る。プレイヤーが近いと出てこない) ------------------
PLANT_W, PLANT_H = 22, 34
PLANT_HIDE_F, PLANT_RISE_F, PLANT_OUT_F, PLANT_SINK_F = 110, 36, 100, 36
PLANT_NEAR = 70            # この距離内にプレイヤーがいる間は出てこない

# --- コッパ (ボス) ------------------------------------------------------------
BOSS_W, BOSS_H = 44, 48
BOSS_SPEED = 0.6           # プレイヤー走行(3.9)より明確に遅い = 飛び越えられる
BOSS_JUMP_V, BOSS_GRAV = -5.0, 0.35
BOSS_JUMP_EVERY = (200, 330)
BOSS_FIRE_EVERY = (150, 260)
FLAME_W, FLAME_H = 24, 10
FLAME_V = 2.0
FLAME_LIFE = 220
FLAME_DIP_AT, FLAME_DIP_FOR = 45, 12   # 途中で一段下がる本家軌道
BRIDGE_FALL_EVERY = 6      # 崩落: 6フレームに1枚
ENDING_TEXTS = 3           # クライアント側の台詞行数
ENDING_TEXT_F = 170        # 1行あたりの表示フレーム

# --- 旗ポール: 掴んだ高さでボーナス (SMB1の 100-5000 準拠) ---------------------
FLAG_TIERS = [(120, 5000), (90, 2000), (60, 800), (30, 400), (0, 100)]

CHAIN = [100, 200, 400, 500, 800, 1000, 2000, 4000, 5000, 8000]
SHELL_CHAIN = [500, 800, 1000, 2000, 4000, 5000, 8000]
COIN_SCORE = 200

# --- ステージ定義 (到達性はシミュレーション監査済み) -------------------------
STAGE1 = {
    "name": "ちじょう", "theme": "field", "world_w": 3600,
    "ground": [
        (0, GROUND_Y, 640, 40), (730, GROUND_Y, 530, 40), (1350, GROUND_Y, 710, 40),
        (2150, GROUND_Y, 570, 40), (2810, GROUND_Y, 790, 40),
    ],
    "ledges": [
        (300, 320, 120, 20), (540, 245, 110, 20), (860, 320, 130, 20),
        (1060, 245, 110, 20), (1450, 320, 130, 20), (1690, 245, 120, 20),
        (1860, 320, 120, 20), (2260, 320, 130, 20), (2500, 245, 120, 20),
        (2900, 320, 130, 20), (3150, 245, 120, 20),
    ],
    "pipes": [(1180, 330, 60, 80), (1990, 330, 60, 80), (3320, 330, 60, 80)],
    "ceiling": [],
    "coins": [
        (360, 280), (595, 205), (685, 330), (925, 280), (1115, 205), (1305, 330),
        (1515, 280), (1750, 205), (1920, 280), (2325, 280), (2560, 205), (3210, 205),
        (2185, 235), (2185, 175),            # ばねで跳ばないと届かないご褒美
        (3105, 130), (3140, 175),            # リフトの上のご褒美
        # 穴の上の弧 = 「ここをこう跳ぶ」の教示 (任天堂文法)
        (660, 330), (685, 305), (710, 330),
        (1280, 330), (1305, 305), (1330, 330),
        (2075, 330), (2105, 305), (2135, 330),
    ],
    "enemies": [
        ("goomba", 500, 440, 610),           # 最初の一体は平地で安全に学ばせる
        ("goomba", 900, 750, 1140), ("koopa", 1550, 1370, 1950),
        ("goomba", 2350, 2170, 2680), ("koopa", 3050, 2830, 3280),
        ("para", 2760, 2660, 2870),          # 穴の上を舞うはねガメ (SMBの定番配置)
    ],
    "plants": [1],                       # pipes[1] (x=1990) にかみつき草
    # 仕掛け: ?ブロック(コイン/キノコ/ルーレット) + 隠しブロック + ばね
    "qblocks": [(200, 268, "coin"), (1010, 268, "shield"),
                (1620, 268, "roulette"), (250, 232, "hidden2000")],
    "springs": [(2170, 396)],
    "conveyors": [],
    "ice": [],
    "crumble": [],
    "cannons": [0],                      # pipes[0] (x=1180) の上に砲台
    "lifts": [(3080, 70, 240, 90, 0.013)],   # (x, 幅, 中心y, 振幅, 角速度) 周期約8秒
    "goal": (3480, 290, 40, 120),
    "checkpoints": [(40.0, 360.0), (760.0, 374.0), (1380.0, 374.0),
                    (2180.0, 374.0), (2840.0, 374.0)],
}
STAGE2 = {
    "name": "どうくつ", "theme": "cave", "world_w": 3600,
    "ground": [
        (0, GROUND_Y, 560, 40), (650, GROUND_Y, 500, 40), (1240, GROUND_Y, 540, 40),
        (1870, GROUND_Y, 580, 40), (2540, GROUND_Y, 560, 40), (3190, GROUND_Y, 410, 40),
    ],
    "ledges": [
        (280, 320, 110, 20), (490, 245, 100, 20), (760, 320, 120, 20),
        (1310, 320, 120, 20), (1530, 245, 110, 20), (1930, 320, 120, 20),
        (2160, 270, 110, 20), (2600, 320, 120, 20), (2810, 245, 110, 20),
        (3240, 320, 110, 20),
    ],
    "pipes": [(1000, 330, 60, 80), (2320, 330, 60, 80), (3020, 330, 60, 80)],
    "ceiling": [
        (0, 0, 3600, 40), (860, 40, 380, 130), (2020, 40, 340, 150), (2900, 40, 380, 120),
    ],
    "coins": [
        (330, 280), (540, 205), (605, 330), (815, 280), (1080, 300), (1195, 330),
        (1365, 280), (1585, 205), (1985, 280), (2215, 230), (2655, 280), (3295, 280),
        (465, 235), (465, 175),              # ばねのご褒美
        # 崩れ橋の上の弧 (渡る勇気へのご褒美)
        (1170, 330), (1196, 308), (1222, 330),
        (1800, 330), (1826, 308), (1852, 330),
    ],
    "enemies": [
        ("goomba", 800, 670, 960), ("koopa", 1400, 1260, 1740),
        ("goomba", 2000, 1890, 2280), ("koopa", 2700, 2560, 2960),
        # きのこへい大行列: 踏み→バウンド→次、がサーバ内で完結する。
        # 遅延があっても最初の一歩だけ決めれば連鎖が続く=サーバ権威の種明かし。
        # 各自「中心±40」を巡回させると全員が同時に折り返し、隊列が崩れない
        ("goomba", 3230, 3190, 3270), ("goomba", 3270, 3230, 3310),
        ("goomba", 3310, 3270, 3350), ("goomba", 3350, 3310, 3390),
        ("goomba", 3390, 3350, 3430), ("goomba", 3430, 3390, 3470),
        ("para", 2490, 2410, 2580),          # 穴の上のはねガメ
    ],
    "plants": [1, 2],                    # pipes[1](2320), pipes[2](3020)
    "qblocks": [(920, 268, "coin"), (1650, 268, "roulette"), (2740, 268, "shield")],
    "springs": [(450, 396)],
    "conveyors": [(1950, 300, -1)],      # 進行方向と逆に流れるベルト
    "ice": [(1300, 400)],                # クリスタルの氷床
    "crumble": [(1146, 368, 100), (1776, 368, 100)],   # 穴に架かる崩れ橋
    "cannons": [0],                      # pipes[0] (x=1000) の上に砲台
    "lifts": [],
    "goal": (3500, 290, 40, 120),
    "checkpoints": [(40.0, 360.0), (680.0, 374.0), (1270.0, 374.0),
                    (1900.0, 374.0), (2570.0, 374.0), (3210.0, 374.0)],
}
# 第3ステージ: コッパの城。溶岩の吊り橋を渡り、斧に触れて橋を落とす。
# 勝ち筋は「走ってコッパを飛び越える」。コッパは踏めない・倒さなくてよい。
STAGE3 = {
    "name": "コッパじょう", "theme": "castle", "world_w": 2400,
    "ground": [(0, GROUND_Y, 560, 40), (1040, GROUND_Y, 1360, 40)],
    "ledges": [(150, 320, 110, 20), (330, 250, 110, 20)],
    "pipes": [],
    "ceiling": [(0, 0, 2400, 40)],
    "coins": [(190, 280), (380, 210), (90, 330), (700, 330), (860, 330), (470, 330)],
    "enemies": [],
    "plants": [],
    "qblocks": [], "springs": [], "conveyors": [], "ice": [], "crumble": [],
    "cannons": [], "lifts": [],
    # 橋の入口上空で回る。復帰地点(470)は圏外・コッパ(730-)とも重ならない。
    # 「回転を読んで渡る」→「コッパを跳び越える」の二段構え
    "firebars": [(620, 318, 3, 0.026)],  # (中心x, 中心y, 玉数, 角速度rad/f) 周期約4秒
    "goal": (2360, 290, 40, 120),        # 使わない (boss_stage)
    "checkpoints": [(40.0, 360.0), (470.0, 374.0)],
    "boss_stage": True,
    "bridge": [(560 + i * 40, GROUND_Y, 40, 14) for i in range(12)],
    "lava": (540, 424, 520, 26),
    "axe": (1150, 344, 26, 40),
    "npc": (2150, GROUND_Y),             # プーチ姫の立ち位置 (足元)
    "boss_home": 778.0,
    "boss_patrol": 48,
}
STAGES = [STAGE1, STAGE2, STAGE3]


def _plats(st, s=None):
    base = st["ground"] + st["ledges"] + st["pipes"] + st["ceiling"]
    if s is not None:
        for qi, (qx, qy, kind) in enumerate(st.get("qblocks", [])):
            if kind == "hidden2000" and qi not in s.get("used_q", []):
                continue                                   # 隠しブロックは開封まで実体なし
            base = base + [(qx, qy, QBLOCK_W, QBLOCK_H)]
        base = base + [(sx, sy, SPRING_W, SPRING_H) for sx, sy in st.get("springs", [])]
        for ci, (cx, cy, cw) in enumerate(st.get("crumble", [])):
            crs = s.get("crumbles", [])
            if ci < len(crs) and crs[ci]["ph"] < 2:        # 崩落中(=2)は消える
                base = base + [(cx, cy, cw, 14)]
        for lx, lw, lcy, lamp, lom in st.get("lifts", []):
            ly = lcy + math.sin(s.get("steps", 0) * lom) * lamp
            base = base + [(lx, ly, lw, 12)]               # 上下リフト (決定論)
    if s is not None and st.get("boss_stage"):
        base = base + st["bridge"][:s.get("bridge", 0)]   # 残っている橋だけ固い
    return base


WORLD = {
    "view_w": VIEW_W, "view_h": VIEW_H,
    "player_w": PLAYER_W, "player_h": PLAYER_H,
    "enemy_w": ENEMY_W, "enemy_h": ENEMY_H, "ground_y": GROUND_Y,
    "stages": [dict(st) for st in STAGES],
    "stage_count": len(STAGES),
}


def _path(sid):
    safe = "".join(c for c in sid if c.isalnum())[:32]
    return os.path.join(STATE_DIR, safe + ".json") if safe else None


def _sign(v):
    return (v > 0) - (v < 0)


def _hit(ax, ay, aw, ah, bx, by, bw, bh):
    return ax < bx + bw and ax + aw > bx and ay < by + bh and ay + ah > by


def _new_enemies(st):
    out = []
    for k, x, lo, hi in st["enemies"]:
        y0 = 296.0 if k == "para" else float(GROUND_Y - ENEMY_H)
        out.append({"kind": k, "mode": "patrol", "x": float(x), "y": y0, "y0": y0,
                    "vx": ENEMY_V, "lo": float(lo), "hi": float(hi), "alive": True,
                    "squash": 0, "revive": 0, "grace": 0, "chain": 0})
    return out


def _enter_stage(s, idx):
    """ステージ読み込み。累計(スコア/ミス/踏破)は持ち越す。"""
    st = STAGES[idx]
    s["stage"] = idx
    s["x"], s["y"] = st["checkpoints"][0]
    s["vx"] = s["vy"] = 0.0
    s["face"] = 1
    s["on_ground"] = False
    s["coyote"] = 0
    s["jbuf"] = 0
    s["gh"], s["gf"] = 0.15, GRAV_FALL_DEFAULT
    s["jumping"] = False
    s["skid"] = False
    s["invuln"] = INVULN_F
    s["dead"] = 0
    s["cp"] = 0
    s["coins"] = []
    s["chain"] = 0
    s["enemies"] = _new_enemies(st)
    s["used_q"] = []
    s["bump"] = [-1, 0]
    s["bullets"] = []
    s["gseq"] = 0
    s["jumps"] = 0
    s["st_start"] = time.time()
    s["st_deaths"] = 0
    s["st_rtt"] = []                 # このステージを遊んだ間の往復遅延 (gamesrv が約0.5秒ごとに足す)
    s["st_net"] = []                 # 同じく、そのときの経路の名前
    s["items"] = []
    s["crumbles"] = [{"ph": 0, "t": 0} for _ in st.get("crumble", [])]
    s["plants"] = [{"pi": i, "ph": "hidden", "t": PLANT_HIDE_F, "off": 0.0}
                   for i in st.get("plants", [])]
    if st.get("boss_stage"):
        s["boss"] = {"x": st["boss_home"], "y": float(GROUND_Y - BOSS_H),
                     "vx": -BOSS_SPEED, "vy": 0.0, "mode": "walk",
                     "jt": random.randint(*BOSS_JUMP_EVERY),
                     "ft": random.randint(*BOSS_FIRE_EVERY), "anim": 0}
        s["bridge"] = len(st["bridge"])
        s["flames"] = []
        s["phase"] = "play"
        s["ph_t"] = 0
        s["text_i"] = -1
    else:
        s["boss"] = None
        s["flames"] = []
        s["phase"] = "play"


def _new_state():
    s = {
        "v": STATE_V,
        "vx": 0.0, "vy": 0.0, "cleared": False,
        "deaths": 0, "stomps": 0, "steps": 0,
        "total_coins": 0, "score": 0,
        "jump_prev": False,
        "shield": False, "star": 0, "star_chain": 0,
        "started": time.time(), "clear_time": None, "last_t": time.time(),
        "event": "", "fx": [],
    }
    _enter_stage(s, 0)
    return s


def _ev(s, name):
    """イベント通知。バッチ内で複数発生しても上書きされないようリストで運ぶ。"""
    s["event"] = name
    if len(s.setdefault("events", [])) < 6:
        s["events"].append(name)


def _fx(s, x, y, text):
    if len(s["fx"]) < 8:
        s["fx"].append([round(x), round(y), str(text)])


def _die(s):
    if s["dead"] > 0:
        return
    s["dead"] = DEATH_F
    s["vx"] = s["vy"] = 0.0
    s["deaths"] += 1
    s["st_deaths"] = s.get("st_deaths", 0) + 1
    s["coyote"] = s["jbuf"] = 0
    _ev(s, "die")


def _hurt(s):
    """敵・飛び道具・かみつき草による被弾。スター無敵中は無効。
    キノコのシールドは1回だけ肩代わりする (SMBの2段階ダメージ猶予)。"""
    if s.get("star", 0) > 0 or s["invuln"] > 0 or s["dead"] > 0:
        return
    if s.get("shield"):
        s["shield"] = False
        s["invuln"] = INVULN_F
        _ev(s, "shrink")
        return
    _die(s)


def _activate_q(s, st, qi):
    """?ブロック起動。上に乗っている敵は突き上げで倒せる (SMB準拠)。"""
    qx, qy, kind = st["qblocks"][qi]
    s["used_q"].append(qi)
    s["bump"] = [qi, BUMP_F]
    _ev(s, "bump")
    if kind in ("coin", "hidden2000"):
        pts = 200 if kind == "coin" else 2000
        s["score"] += pts
        _fx(s, qx + QBLOCK_W / 2, qy - 12, pts)
        _ev(s, "coin")
    elif kind == "shield":
        s["items"].append({"x": float(qx + 3), "y": float(qy), "vx": ITEM_V,
                           "vy": 0.0, "emerge": ITEM_EMERGE_F})
        _ev(s, "sprout")
    elif kind == "roulette":
        # 叩いた瞬間に「表示されている」絵柄が当たる。遅延0.5秒の経路では
        # 0.5秒先の絵柄を狙って叩く必要がある = 遅延そのものが遊びになる
        prize = ROULETTE_SEQ[(s["steps"] // ROULETTE_F) % len(ROULETTE_SEQ)]
        if prize in ("c200", "c1000"):
            pts = 200 if prize == "c200" else 1000
            s["score"] += pts
            _fx(s, qx + QBLOCK_W / 2, qy - 12, pts)
            _ev(s, "coin")
        elif prize == "star":
            s["star"] = STAR_F
            s["star_chain"] = 0
            _fx(s, qx + QBLOCK_W / 2, qy - 12, "スター!")
            _ev(s, "star")
        else:
            s["items"].append({"x": float(qx + 3), "y": float(qy), "vx": ITEM_V,
                               "vy": 0.0, "emerge": ITEM_EMERGE_F})
            _ev(s, "sprout")
    for e in s["enemies"]:
        if e["alive"] and e["mode"] != "corpse" and                 abs((e["y"] + ENEMY_H) - qy) < 6 and                 e["x"] + ENEMY_W > qx and e["x"] < qx + QBLOCK_W:
            _kill_enemy(e)
            s["score"] += 100
            _fx(s, e["x"], e["y"], 100)
            _ev(s, "shellhit")


def _stage_result(s, st):
    """ステージ評価 (現代の定番: クリアにランクを付けて再挑戦の動機を作る)。"""
    got, tot = len(s["coins"]), len(st["coins"])
    t = round(time.time() - s.get("st_start", s["started"]), 1)
    d = s.get("st_deaths", 0)
    if d == 0 and got == tot:
        rank = "S"
    elif d <= 1 and got >= tot * 0.7:
        rank = "A"
    elif d <= 3:
        rank = "B"
    else:
        rank = "C"
    rtts = sorted(s.get("st_rtt") or [])
    nets = [n for n in s.get("st_net") or [] if n]
    s["result"] = {"stage": s["stage"], "rank": rank, "time": t,
                   "coins": got, "coins_total": tot, "deaths": d,
                   # ランキングに添える「このステージを遊んだ間の遅延」(中央値。測れていなければ None) と、
                   # いちばん長く使っていた経路の名前 (途中で経路が直っても、遅い経路で遊んだ記録を取り違えない)
                   "rtt": rtts[len(rtts) // 2] if rtts else None,
                   "net": max(set(nets), key=nets.count) if nets else None}
    s["result_t"] = RESULT_F
    s["results_n"] = s.get("results_n", 0) + 1     # gamesrv がランキングへ送る合図


def _respawn(s):
    st = STAGES[s["stage"]]
    s["x"], s["y"] = st["checkpoints"][s["cp"]]
    s["vx"] = s["vy"] = 0.0
    s["invuln"] = INVULN_F
    s["dead"] = 0
    s["jumping"] = False


def _chain_score(s, x, y):
    i = min(s["chain"], len(CHAIN) - 1)
    s["score"] += CHAIN[i]
    _fx(s, x, y, CHAIN[i])
    s["chain"] += 1


def _shell_chain_score(s, e, x, y):
    i = min(e["chain"], len(SHELL_CHAIN) - 1)
    s["score"] += SHELL_CHAIN[i]
    _fx(s, x, y, SHELL_CHAIN[i])
    e["chain"] += 1


def _kill_enemy(en):
    """滑走コウラ・敵同士の衝突などによる撃破。"""
    en["alive"] = False
    en["squash"] = SQUASH_F
    en["mode"] = "corpse"


def _kick_shell(s, e, direction):
    e["mode"] = "slide"
    e["vx"] = SHELL_V * (direction if direction else (s["face"] or 1))
    e["grace"] = KICK_GRACE_F
    e["revive"] = 0
    e["chain"] = 0
    _ev(s, "kick")


def _enemy_physics(e, plats, slow):
    """物理挙動する敵 (コウラ滑走/復活後の歩行)。壁で反転、穴に落ちる。"""
    v = e["vx"] if not slow else _sign(e["vx"]) * ENEMY_V
    e["x"] += v
    for px, py, pw, ph in plats:
        if _hit(e["x"], e["y"], ENEMY_W, ENEMY_H, px, py, pw, ph):
            if v > 0:
                e["x"] = px - ENEMY_W
            else:
                e["x"] = px + pw
            e["vx"] = -e["vx"]
            break
    # 重力と着地
    e.setdefault("vy", 0.0)
    e["vy"] = min(e["vy"] + GRAV_FALL_DEFAULT, MAX_FALL)
    e["y"] += e["vy"]
    for px, py, pw, ph in plats:
        if _hit(e["x"], e["y"], ENEMY_W, ENEMY_H, px, py, pw, ph):
            if e["vy"] > 0:
                e["y"] = py - ENEMY_H
            else:
                e["y"] = py + ph
            e["vy"] = 0.0
            break


def _step_plants(s, st):
    """かみつき草。hidden→rise→out→sink の周期。近いと出てこない。"""
    for pl in s["plants"]:
        px, py, pw, ph_ = st["pipes"][pl["pi"]]
        cx = px + pw / 2
        pl["t"] -= 1
        if pl["ph"] == "hidden":
            if pl["t"] <= 0:
                near = abs((s["x"] + PLAYER_W / 2) - cx) < PLANT_NEAR
                on_pipe = s["on_ground"] and px - PLAYER_W < s["x"] < px + pw
                if not near and not on_pipe:
                    pl["ph"], pl["t"] = "rise", PLANT_RISE_F
        elif pl["ph"] == "rise":
            pl["off"] = PLANT_H * (1 - pl["t"] / PLANT_RISE_F)
            if pl["t"] <= 0:
                pl["ph"], pl["t"], pl["off"] = "out", PLANT_OUT_F, PLANT_H
        elif pl["ph"] == "out":
            if pl["t"] <= 0:
                pl["ph"], pl["t"] = "sink", PLANT_SINK_F
        elif pl["ph"] == "sink":
            pl["off"] = PLANT_H * (pl["t"] / PLANT_SINK_F)
            if pl["t"] <= 0:
                pl["ph"], pl["t"], pl["off"] = "hidden", PLANT_HIDE_F, 0.0
        # 触れたら被弾 (踏めない)
        if pl["off"] > 10 and s["dead"] == 0 and s["invuln"] <= 0:
            hx, hy = cx - PLANT_W / 2, py - pl["off"]
            if _hit(s["x"], s["y"], PLAYER_W, PLAYER_H, hx, hy, PLANT_W, pl["off"]):
                _hurt(s)


def _step_boss(s, st, plats):
    """コッパ: 橋の上を徘徊し、たまに跳び、炎を吐く。踏めない。"""
    b = s["boss"]
    if b["mode"] == "gone":
        return
    if b["mode"] == "fall":
        b["vy"] = min(b["vy"] + 0.4, 7.0)
        b["y"] += b["vy"]
        if b["y"] > VIEW_H + 100:
            b["mode"] = "gone"
        return
    # 徘徊 (橋が自分の下に残っている間だけ)
    b["x"] += b["vx"]
    if b["x"] < st["boss_home"] - st["boss_patrol"]:
        b["x"] = st["boss_home"] - st["boss_patrol"]; b["vx"] = BOSS_SPEED
    if b["x"] > st["boss_home"] + st["boss_patrol"]:
        b["x"] = st["boss_home"] + st["boss_patrol"]; b["vx"] = -BOSS_SPEED
    # 重力と着地
    b["vy"] = min(b["vy"] + BOSS_GRAV, 6.0)
    b["y"] += b["vy"]
    on_g = False
    for px, py, pw, ph_ in plats:
        if _hit(b["x"], b["y"], BOSS_W, BOSS_H, px, py, pw, ph_):
            if b["vy"] > 0:
                b["y"] = py - BOSS_H; on_g = True
            else:
                b["y"] = py + ph_
            b["vy"] = 0.0
    # 足場が消えたら落ちる (斧の崩落後)
    if not on_g and b["vy"] > 2.5 and s["phase"] != "play":
        b["mode"] = "fall"
        _ev(s, "bossfall")
        s["score"] += 5000
        _fx(s, b["x"] + BOSS_W / 2, b["y"], 5000)
        return
    if b["anim"] > 0:
        b["anim"] -= 1
    if s["phase"] != "play":
        return
    # ジャンプ
    b["jt"] -= 1
    if on_g and b["jt"] <= 0:
        b["vy"] = BOSS_JUMP_V
        b["jt"] = random.randint(*BOSS_JUMP_EVERY)
    # 炎 (プレイヤーの高さを狙い分ける本家軌道)
    b["ft"] -= 1
    if b["ft"] <= 0 and len(s["flames"]) < 3:
        aim_high = (s["y"] + PLAYER_H) < (b["y"] + 24)
        d = -1 if s["x"] < b["x"] else 1
        s["flames"].append({"x": b["x"] + (0 if d < 0 else BOSS_W - FLAME_W),
                            "y": b["y"] + (4 if aim_high else 26),
                            "vx": FLAME_V * d, "vy": 0.0, "t": FLAME_LIFE})
        b["ft"] = random.randint(*BOSS_FIRE_EVERY)
        b["anim"] = 24
        _ev(s, "flame")


def _step_flames(s):
    alive = []
    for f in s["flames"]:
        f["t"] -= 1
        if f["t"] == FLAME_LIFE - FLAME_DIP_AT:
            f["vy"] = 0.6                       # 一段下がる
        if f["t"] == FLAME_LIFE - FLAME_DIP_AT - FLAME_DIP_FOR:
            f["vy"] = 0.0
        f["x"] += f["vx"]; f["y"] += f["vy"]
        if f["t"] > 0 and -50 < f["x"] < 4000:
            alive.append(f)
        if s["dead"] == 0 and s["invuln"] <= 0 and                 _hit(s["x"], s["y"], PLAYER_W, PLAYER_H, f["x"], f["y"], FLAME_W, FLAME_H):
            _hurt(s)
    s["flames"] = alive


def _step(s, keys):
    """1フレーム (1/60秒) 進める。"""
    if s["cleared"]:
        return
    st = STAGES[s["stage"]]
    plats = _plats(st, s)

    for e in s["enemies"]:
        if e["squash"] > 0:
            e["squash"] -= 1
        if e["grace"] > 0:
            e["grace"] -= 1
    if s["bump"][1] > 0:
        s["bump"][1] -= 1
    if s.get("result_t", 0) > 0:
        s["result_t"] -= 1
    for c in s["crumbles"]:
        if c["ph"] == 1:                     # 揺れている
            c["t"] -= 1
            if c["t"] <= 0:
                c["ph"], c["t"] = 2, CRUMBLE_GONE_F
                _ev(s, "crumble")
        elif c["ph"] == 2:                   # 崩落中 → 時間で復活
            c["t"] -= 1
            if c["t"] <= 0:
                c["ph"] = 0

    if s["dead"] > 0:
        s["dead"] -= 1
        if s["dead"] == 0:
            _respawn(s)
        return

    # ---- 旗の演出 (ポールを掴んで滑り降り → 城へ歩く → 次のステージ) -----------
    if s.get("gseq", 0) > 0:
        s["gseq"] -= 1
        gx, gy, gw, gh = st["goal"]
        if s["gseq"] > GSEQ_WALK:                    # 滑り降り
            s["x"] = gx + gw / 2 - PLAYER_W - 2
            s["y"] = min(s["y"] + 4.0, GROUND_Y - PLAYER_H)
            s["vx"] = s["vy"] = 0.0
            s["face"] = -1                           # ポールの方を向く
        else:                                        # 城へ歩く
            s["x"] += 2.2
            s["y"] = float(GROUND_Y - PLAYER_H)
            s["face"] = 1
            s["on_ground"] = True
        if s["gseq"] == 0:
            _stage_result(s, st)
            if s["stage"] + 1 < len(STAGES):
                _enter_stage(s, s["stage"] + 1)
                _ev(s, "stage_clear")
            else:
                s["cleared"] = True
                s["clear_time"] = round(time.time() - s["started"], 1)
                _ev(s, "goal")
        return

    # ---- 城ステージのフェーズ (斧 → 崩落 → コッパ落下 → 救出) ----------------
    scripted = False
    if st.get("boss_stage"):
        ph = s["phase"]
        if ph == "collapse":
            scripted = True
            keys = {}
            s["ph_t"] += 1
            if s["bridge"] > 0 and s["ph_t"] % BRIDGE_FALL_EVERY == 0:
                s["bridge"] -= 1
                _ev(s, "bridge")
                plats = _plats(st, s)
            if s["bridge"] <= 0 and s["boss"]["mode"] == "walk":
                s["boss"]["mode"] = "fall"          # 足場ごと落ちる
                _ev(s, "bossfall")
                s["score"] += 5000
                _fx(s, s["boss"]["x"] + BOSS_W / 2, s["boss"]["y"], 5000)
            if s["boss"]["mode"] == "gone":
                s["phase"], s["ph_t"] = "pause", 0
        elif ph == "pause":
            scripted = True
            keys = {}
            s["ph_t"] += 1
            if s["ph_t"] >= 40:                     # 一拍の静止 (余韻)
                s["phase"], s["ph_t"] = "walkout", 0
        elif ph == "walkout":
            scripted = True
            keys = {"right": True, "run": False}
            if s["x"] >= st["npc"][0] - 80:
                s["phase"], s["ph_t"] = "text", 0
                s["text_i"] = 0
                _ev(s, "rescue")
        elif ph == "text":
            scripted = True
            keys = {}
            s["ph_t"] += 1
            if s["ph_t"] >= ENDING_TEXT_F:
                s["ph_t"] = 0
                s["text_i"] += 1
                if s["text_i"] >= ENDING_TEXTS:
                    _stage_result(s, st)
                    s["cleared"] = True
                    s["clear_time"] = round(time.time() - s["started"], 1)
                    _ev(s, "goal")
                    return

    # ---- 敵 ----------------------------------------------------------------
    for e in s["enemies"]:
        if not e["alive"]:
            continue
        m = e["mode"]
        if m == "patrol":
            e["x"] += e["vx"]
            if e["x"] <= e["lo"]:
                e["x"] = e["lo"]; e["vx"] = abs(e["vx"])
            elif e["x"] >= e["hi"]:
                e["x"] = e["hi"]; e["vx"] = -abs(e["vx"])
            if e["kind"] == "para":          # はねガメは上下に舞う
                e["y"] = e["y0"] + math.sin(s["steps"] * PARA_SPD + e["lo"]) * PARA_AMP
        elif m == "walk":
            _enemy_physics(e, plats, slow=True)
            if e["y"] > VIEW_H + 80:
                e["alive"] = False
        elif m == "shell":
            e["revive"] += 1
            if e["revive"] >= SHELL_REVIVE_F:
                # プレイヤーに重なったまま復活しない
                if not _hit(s["x"], s["y"], PLAYER_W, PLAYER_H,
                            e["x"], e["y"], ENEMY_W, ENEMY_H):
                    e["mode"] = "walk"
                    e["vx"] = ENEMY_V * (-1 if s["x"] > e["x"] else 1)
                    e["revive"] = 0
        elif m == "slide":
            _enemy_physics(e, plats, slow=False)
            if e["y"] > VIEW_H + 80:
                e["alive"] = False
            else:
                # 他の敵を巻き込んで倒す (連鎖スコア)
                for o in s["enemies"]:
                    if o is e or not o["alive"] or o["mode"] == "corpse":
                        continue
                    if _hit(e["x"], e["y"], ENEMY_W, ENEMY_H,
                            o["x"], o["y"], ENEMY_W, ENEMY_H):
                        _kill_enemy(o)
                        _shell_chain_score(s, e, o["x"], o["y"])
                        _ev(s, "shellhit")

    # ---- プレイヤー水平 ------------------------------------------------------
    dirn = (1 if keys.get("right") else 0) - (1 if keys.get("left") else 0)
    run = bool(keys.get("run"))
    s["skid"] = False
    fr = 1.0
    if s["on_ground"] and abs((s["y"] + PLAYER_H) - GROUND_Y) < 2:
        cxm = s["x"] + PLAYER_W / 2
        if any(ix <= cxm <= ix + iw for ix, iw in st.get("ice", [])):
            fr = ICE_MULT                    # 氷床: 加速も減速も効かない
    if s["on_ground"]:
        if dirn != 0:
            if s["vx"] != 0 and _sign(s["vx"]) != dirn:
                s["skid"] = True                       # 逆入力: スキッド
                s["vx"] += DEC_SKID * fr * dirn
                if abs(s["vx"]) < SKID_TURN:
                    s["vx"] = SKID_TURN * dirn         # 反転して通常加速へ
                    s["face"] = dirn
                    s["skid"] = False
            else:
                s["face"] = dirn
                acc = (ACC_RUN if run else ACC_WALK) * fr
                mx = MAX_RUN if run else MAX_WALK
                if abs(s["vx"]) < MIN_WALK:
                    s["vx"] = MIN_WALK * dirn
                elif abs(s["vx"]) > mx:                # Bを離した直後: 摩擦で戻す
                    s["vx"] -= DEC_REL * _sign(s["vx"])
                else:
                    s["vx"] = max(-mx, min(mx, s["vx"] + acc * dirn))
        else:
            if abs(s["vx"]) <= DEC_REL * fr:
                s["vx"] = 0.0
            else:
                s["vx"] -= DEC_REL * fr * _sign(s["vx"])
    else:
        # 空中: 摩擦なし。加速度は「現在の速度」で決める (本家準拠)
        if dirn != 0:
            acc = ACC_RUN if abs(s["vx"]) >= MAX_WALK else ACC_WALK
            cap = MAX_RUN * (APEX_SPEED if abs(s["vy"]) < APEX_V else 1.0)
            s["vx"] = max(-cap, min(cap, s["vx"] + acc * dirn))
            s["face"] = dirn

    # ---- ジャンプ (バッファ + コヨーテ。エッジ検出は tick 側で1回だけ行う。
    #      フレーム毎に行うとバッチ内で再アームされ、1押下で2段ジャンプする) ----
    s["coyote"] = COYOTE_F if s["on_ground"] else max(0, s["coyote"] - 1)
    if s["jbuf"] > 0:
        s["jbuf"] -= 1
        if s["coyote"] > 0:
            for spd_below, v0, gh, gf in JUMP_TABLE:
                if abs(s["vx"]) < spd_below:
                    s["vy"], s["gh"], s["gf"] = v0, gh, gf
                    break
            s["on_ground"] = False
            s["jumping"] = True
            s["coyote"] = s["jbuf"] = 0
            s["jumps"] = 1
            _ev(s, "jump")
        elif s.get("jumps", 0) < 2:
            # 二段ジャンプ。コヨーテ猶予が残っている間は必ず1段目扱いに
            # なるので「崖から落ちた直後に2段目から始まる」バグは起きない
            v0, gh, gf = DOUBLE_JUMP
            s["vy"], s["gh"], s["gf"] = v0, gh, gf
            s["jumping"] = True
            s["jumps"] = 2
            s["jbuf"] = 0
            _ev(s, "jump2")

    # 重力: A押下中かつ上昇中のみ弱い重力 (可変ジャンプの核心)
    g = s["gh"] if (s["jumping"] and keys.get("jump") and s["vy"] < 0) else s["gf"]
    if abs(s["vy"]) < APEX_V:
        g *= APEX_GRAV                       # 頂点付近をゆっくりにして狙えるようにする
    s["vy"] = min(s["vy"] + g, MAX_FALL)

    # ---- 移動と衝突 (軸分離: X→Y) --------------------------------------------
    s["x"] += s["vx"]
    for px, py, pw, ph in plats:
        if _hit(s["x"], s["y"], PLAYER_W, PLAYER_H, px, py, pw, ph):
            # 足元補正: 足先が浅く (8px以内) 食い込んだだけなら壁ではなく床。
            # 横に弾かず Y 解決に任せる。上昇リフトに乗り続けるにも必須
            if s["vy"] >= 0 and (s["y"] + PLAYER_H) - py <= 8:
                continue
            s["x"] = px - PLAYER_W if s["vx"] > 0 else px + pw
            s["vx"] = 0.0
    s["x"] = max(0.0, min(s["x"], st["world_w"] - PLAYER_W))

    prev_feet = s["y"] + PLAYER_H
    was_air = not s["on_ground"]
    s["y"] += s["vy"]

    # 仕掛けの索引 (矩形→種別)。ばね/崩れ床/?ブロックは着地・頭突きで挙動が変わる
    qmap = {}
    for _qi, (_qx, _qy, _qk) in enumerate(st.get("qblocks", [])):
        if _qk == "hidden2000" and _qi not in s["used_q"]:
            continue
        qmap[(_qx, _qy, QBLOCK_W, QBLOCK_H)] = _qi
    springset = {(_sx, _sy, SPRING_W, SPRING_H) for _sx, _sy in st.get("springs", [])}
    crmap = {(_cx, _cy, _cw, 14): _ci
             for _ci, (_cx, _cy, _cw) in enumerate(st.get("crumble", []))}

    # 隠しブロック: 実体が無いので、下から突き上げた時だけ出現する (SMB)
    if s["vy"] < 0:
        prev_top = prev_feet - PLAYER_H
        for qi, (qx, qy, kind) in enumerate(st.get("qblocks", [])):
            if kind != "hidden2000" or qi in s["used_q"]:
                continue
            if prev_top >= qy + QBLOCK_H and _hit(
                    s["x"], s["y"], PLAYER_W, PLAYER_H, qx, qy, QBLOCK_W, QBLOCK_H):
                s["y"] = qy + QBLOCK_H
                s["vy"] = 0.0
                _activate_q(s, st, qi)
                break

    s["on_ground"] = False
    for px, py, pw, ph in plats:
        if _hit(s["x"], s["y"], PLAYER_W, PLAYER_H, px, py, pw, ph):
            if s["vy"] > 0:
                rect = (px, py, pw, ph)
                if rect in springset:                      # ばね: 大きく跳ね返す
                    s["y"] = py - PLAYER_H
                    v0, gh, gf = SPRING_JUMP_HELD if keys.get("jump") else SPRING_JUMP
                    s["vy"], s["gh"], s["gf"] = v0, gh, gf
                    s["jumping"] = True
                    s["jumps"] = 1                     # ばねの後も空中で1回跳べる
                    _ev(s, "spring")
                    continue
                s["y"] = py - PLAYER_H
                s["on_ground"] = True
                s["jumping"] = False
                s["vy"] = 0.0
                ci = crmap.get(rect)
                if ci is not None and s["crumbles"][ci]["ph"] == 0:
                    s["crumbles"][ci] = {"ph": 1, "t": CRUMBLE_SHAKE_F}
                    _ev(s, "rumble")
            else:
                qi = qmap.get((px, py, pw, ph))
                if qi is not None:                         # ?ブロックを下から叩いた
                    s["y"] = py + ph
                    s["vy"] = 0.0
                    if qi not in s["used_q"]:
                        _activate_q(s, st, qi)
                    continue
                # 天井の角に頭をぶつけた: 少し横にずらせば通れるなら通す。
                # 手間の割に効果が最も大きい補正。無いと角で不自然に止まる。
                fixed = False
                for off in range(1, CORNER_PX + 1):
                    for d in (1, -1):
                        nx = s["x"] + off * d
                        if not any(_hit(nx, s["y"], PLAYER_W, PLAYER_H, *q) for q in plats):
                            s["x"] = nx
                            fixed = True
                            break
                    if fixed:
                        break
                if not fixed:
                    s["y"] = py + ph
                    s["vy"] = 0.0
    if s["on_ground"] and was_air:
        s["chain"] = 0                                 # 着地でチェーンリセット
        s["gf"] = GRAV_FALL_DEFAULT
        s["jumps"] = 0

    # ベルトコンベア: 乗っている間だけ流される
    if s["on_ground"] and abs((s["y"] + PLAYER_H) - GROUND_Y) < 2:
        cxm2 = s["x"] + PLAYER_W / 2
        for bx, bw, bd in st.get("conveyors", []):
            if bx <= cxm2 <= bx + bw:
                s["x"] += BELT_V * bd
                break

    if s["y"] > VIEW_H + 80:
        _die(s)
        return

    # ---- 敵との相互作用 -------------------------------------------------------
    if s["invuln"] > 0:
        s["invuln"] -= 1
    if s["star"] > 0:
        s["star"] -= 1
        if s["star"] == 0:
            s["star_chain"] = 0
    for e in s["enemies"]:
        if not e["alive"] or e["mode"] == "corpse" or e["grace"] > 0:
            continue
        if not _hit(s["x"], s["y"], PLAYER_W, PLAYER_H,
                    e["x"], e["y"], ENEMY_W, ENEMY_H):
            continue
        if s["star"] > 0:                              # スター無敵: 触れるだけで撃破
            _kill_enemy(e)
            sci = min(s["star_chain"], len(STAR_CHAIN) - 1)
            s["score"] += STAR_CHAIN[sci]
            _fx(s, e["x"], e["y"], STAR_CHAIN[sci])
            s["star_chain"] += 1
            _ev(s, "shellhit")
            continue
        stomp = s["vy"] > 0 and prev_feet <= e["y"] + 8
        m = e["mode"]
        if stomp:
            bounce = STOMP_V_HELD if keys.get("jump") else STOMP_V
            if e["kind"] == "goomba":
                _kill_enemy(e)
                _chain_score(s, e["x"], e["y"])
                s["stomps"] += 1
                _ev(s, "stomp")
            elif e["kind"] == "para" and m == "patrol":    # 羽が取れて地上へ
                e["kind"] = "koopa"
                e["mode"] = "walk"
                e["vx"] = ENEMY_V * (1 if e["vx"] >= 0 else -1)
                _chain_score(s, e["x"], e["y"])
                s["stomps"] += 1
                _ev(s, "stomp")
            elif m in ("patrol", "walk"):              # コウラにする
                e["mode"] = "shell"
                e["vx"] = 0.0
                e["revive"] = 0
                _chain_score(s, e["x"], e["y"])
                s["stomps"] += 1
                _ev(s, "stomp")
            elif m == "slide":                          # 滑走中を踏む→停止
                e["mode"] = "shell"
                e["vx"] = 0.0
                e["revive"] = 0
                e["grace"] = KICK_GRACE_F
                _ev(s, "stomp")
            elif m == "shell":                          # 静止コウラを踏む→蹴る
                _kick_shell(s, e, 1 if s["x"] + PLAYER_W / 2 <= e["x"] + ENEMY_W / 2 else -1)
            s["y"] = e["y"] - PLAYER_H
            s["vy"] = bounce
            s["jumping"] = True
            s["jumps"] = 1                             # 踏んだ後も空中で1回跳べる
            s["gh"], s["gf"] = 0.15, GRAV_FALL_DEFAULT
        else:
            if m == "shell":                            # 横から触れる→蹴る
                _kick_shell(s, e, 1 if s["x"] + PLAYER_W / 2 <= e["x"] + ENEMY_W / 2 else -1)
            else:
                _hurt(s)
                if s["dead"] > 0:
                    return

    # ---- ずどん砲台: steps 決定論で発射。予告はクライアントが同じ式で描く -------
    for ci in st.get("cannons", []):
        px_, py_, pw_, _ph2 = st["pipes"][ci]
        if (s["steps"] + ci * 97) % CANNON_P == 0 and len(s["bullets"]) < 6:
            s["bullets"].append({"x": float(px_ - BULLET_W), "y": float(py_ - 22),
                                 "vx": -BULLET_V})
            _ev(s, "shoot")
    kept_b = []
    for b2 in s["bullets"]:
        b2["x"] += b2["vx"]
        if b2["x"] < -60 or b2["x"] > st["world_w"] + 60:
            continue
        if abs(b2["x"] - s["x"]) > 700 and b2["vx"] < 0 and b2["x"] < s["x"]:
            continue                                   # 十分離れたら消す
        if _hit(s["x"], s["y"], PLAYER_W, PLAYER_H,
                b2["x"], b2["y"], BULLET_W, BULLET_H):
            if s["star"] > 0:                          # スター: 弾も撃破
                s["score"] += 200
                _fx(s, b2["x"], b2["y"], 200)
                _ev(s, "shellhit")
                continue
            if s["vy"] > 0 and prev_feet <= b2["y"] + 6:   # 踏める (SMB準拠)
                s["score"] += 200
                _fx(s, b2["x"], b2["y"], 200)
                s["vy"] = STOMP_V_HELD if keys.get("jump") else STOMP_V
                s["y"] = b2["y"] - PLAYER_H
                s["jumping"] = True
                s["stomps"] += 1
                _ev(s, "stomp")
                continue
            _hurt(s)
            if s["dead"] > 0:
                return
        kept_b.append(b2)
    s["bullets"] = kept_b

    # ---- リフトの床スナップ: 下降するリフトに吸い付く (現代の定石) --------------
    if not s["on_ground"] and s["vy"] >= 0 and not s["jumping"]:
        for lx, lw, lcy, lamp, lom in st.get("lifts", []):
            ly = lcy + math.sin(s["steps"] * lom) * lamp
            if s["x"] + PLAYER_W > lx and s["x"] < lx + lw and \
                    0 <= ly - (s["y"] + PLAYER_H) <= 7:
                s["y"] = ly - PLAYER_H
                s["on_ground"] = True
                s["vy"] = 0.0
                break

    # ---- キノコ (ブロックからせり上がり、歩き、取るとシールド) -----------------
    kept_items = []
    for it in s["items"]:
        if it["emerge"] > 0:
            it["emerge"] -= 1
            it["y"] -= ITEM_H / ITEM_EMERGE_F
            kept_items.append(it)
            continue
        it["x"] += it["vx"]
        for px, py, pw, ph in plats:
            if _hit(it["x"], it["y"], ITEM_W, ITEM_H, px, py, pw, ph):
                it["x"] = px - ITEM_W if it["vx"] > 0 else px + pw
                it["vx"] = -it["vx"]
                break
        it["vy"] = min(it["vy"] + GRAV_FALL_DEFAULT, MAX_FALL)
        it["y"] += it["vy"]
        for px, py, pw, ph in plats:
            if _hit(it["x"], it["y"], ITEM_W, ITEM_H, px, py, pw, ph):
                it["y"] = py - ITEM_H if it["vy"] > 0 else py + ph
                it["vy"] = 0.0
                break
        if it["y"] > VIEW_H + 80:
            continue
        if _hit(s["x"], s["y"], PLAYER_W, PLAYER_H, it["x"], it["y"], ITEM_W, ITEM_H):
            s["score"] += SHIELD_SCORE
            _fx(s, it["x"], it["y"] - 10, SHIELD_SCORE)
            s["shield"] = True
            _ev(s, "powerup")
            continue
        kept_items.append(it)
    s["items"] = kept_items

    # ---- かみつき草 / コッパ / 炎 / 溶岩 / 斧 --------------------------------
    if s["plants"] and not scripted:
        _step_plants(s, st)
        if s["dead"] > 0:
            return
    if st.get("boss_stage"):
        _step_boss(s, st, plats)
        if s["phase"] == "play":
            _step_flames(s)
            if s["dead"] > 0:
                return
            # ファイアバー: steps から決定論で回る炎の棒
            for fx0, fy0, nballs, om in st.get("firebars", []):
                ang = s["steps"] * om
                ca, sa = math.cos(ang), math.sin(ang)
                for k in range(1, nballs + 1):
                    bx2 = fx0 + ca * FIREBAR_R * k - 6
                    by2 = fy0 + sa * FIREBAR_R * k - 6
                    if s["dead"] == 0 and s["invuln"] <= 0 and \
                            _hit(s["x"], s["y"], PLAYER_W, PLAYER_H, bx2, by2, 12, 12):
                        _hurt(s)
                        break
            if s["dead"] > 0:
                return
            # 溶岩: 触れたら即ミス
            lv = st["lava"]
            if _hit(s["x"], s["y"], PLAYER_W, PLAYER_H, *lv):
                _die(s)
                return
            # コッパ本体: 踏めない。触れたら被弾
            b = s["boss"]
            if b["mode"] == "walk" and s["invuln"] <= 0 and                     _hit(s["x"], s["y"], PLAYER_W, PLAYER_H, b["x"], b["y"], BOSS_W, BOSS_H):
                _hurt(s)
                if s["dead"] > 0:
                    return
            # 斧: 触れたら勝利シーケンス開始
            if _hit(s["x"], s["y"], PLAYER_W, PLAYER_H, *st["axe"]):
                s["phase"], s["ph_t"] = "collapse", 0
                s["vx"] = 0.0
                s["flames"] = []          # 漂っていた炎は演出のため消す
                _ev(s, "axe")

    # ---- コイン / 中間地点 / ゴール --------------------------------------------
    for i, (cx, cy) in enumerate(st["coins"]):
        if i not in s["coins"] and \
                _hit(s["x"], s["y"], PLAYER_W, PLAYER_H, cx - 11, cy - 11, 22, 22):
            s["coins"].append(i)
            s["total_coins"] += 1
            s["score"] += COIN_SCORE
            _fx(s, cx, cy - 14, COIN_SCORE)
            _ev(s, "coin")

    for i, (cx, _cy) in enumerate(st["checkpoints"]):
        if i > s["cp"] and s["x"] >= cx:
            s["cp"] = i
            _ev(s, "checkpoint")

    # ゴールは「旗の線を越えたら」成立 (矩形判定だと走りジャンプで
    # 旗の上を飛び越えてしまい、二度とクリアできなくなる)。
    # 城ステージのクリアは斧→救出シーケンスが担う。
    if not st.get("boss_stage") and s["gseq"] == 0 and \
            s["x"] + PLAYER_W >= st["goal"][0]:
        # 旗の掴み高さボーナス (SMB1: 高いほど高得点、最上部5000)
        h_above = (GROUND_Y - (s["y"] + PLAYER_H))
        for need, pts in FLAG_TIERS:
            if h_above >= need:
                s["score"] += pts
                _fx(s, st["goal"][0] + 20, max(120, s["y"]), pts)
                break
        s["gseq"] = GSEQ_F                           # 掴んで滑り降りる演出へ
        s["invuln"] = GSEQ_F + 30
        _ev(s, "flag")

    s["steps"] += 1


def _enemy_code(e):
    """描画コード: 0=非表示 1=きのこへい 2=コウラ族(歩行) 3=コウラ(静止) 4=滑走
    5=きのこへい(つぶれ) 6=コウラ(やられ)"""
    if not e["alive"]:
        if e["squash"] > 0:
            return 5 if e["kind"] == "goomba" else 6
        return 0
    if e["kind"] == "goomba":
        return 1
    if e["kind"] == "para" and e["mode"] == "patrol":
        return 7
    return {"patrol": 2, "walk": 2, "shell": 3, "slide": 4}.get(e["mode"], 2)


def _cleanup():
    now = time.time()
    try:
        for f in os.listdir(STATE_DIR):
            p = os.path.join(STATE_DIR, f)
            if now - os.path.getmtime(p) > SESSION_TTL:
                os.remove(p)
    except OSError:
        pass


@game_bp.route("/game/world")
def world():
    return jsonify(WORLD)


@game_bp.route("/game/tick", methods=["POST"])
def tick():
    data = request.get_json(silent=True) or {}
    sid = str(data.get("sid", ""))
    path = _path(sid)
    if not path:
        return jsonify({"error": "bad sid"}), 400
    keys = data.get("keys") or {}
    reset = bool(data.get("reset"))

    os.makedirs(STATE_DIR, exist_ok=True)
    with open(path + ".lock", "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        try:
            with open(path, encoding="utf-8") as f:
                s = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            s = _new_state()
        if reset or s.get("v") != STATE_V:
            s = _new_state()
        else:
            # リクエスト数ではなく経過時間でフレームを進める。
            # 遅延で HTTP が詰まっても世界は実時間で進み、
            # 遅れるのは「見えている絵」だけになる。
            now = time.time()
            elapsed = now - s.get("last_t", now)
            n = max(1, min(int(round(elapsed / STEP_DT)), MAX_CATCHUP))
            s["event"] = ""
            s["fx"] = []
            s["events"] = []
            if keys.get("jump") and not s.get("jump_prev"):
                s["jbuf"] = JBUF_F               # エッジはティックで1回だけ
            for _ in range(n):
                _step(s, keys)
            s["jump_prev"] = bool(keys.get("jump"))
            # MAX_CATCHUP で切り捨てた実時間は最大0.5秒まで持ち越す
            # (タブ復帰などの巨大な差分で世界が吹き飛ぶのは防ぐ)
            rem = max(0.0, min(elapsed - n * STEP_DT, 0.5))
            s["last_t"] = now - rem
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(s, f)
        os.replace(tmp, path)

    if s["steps"] % 1800 == 0:
        _cleanup()

    st = STAGES[s["stage"]]
    return jsonify({
        "seq": data.get("seq", 0),
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
        "event": s["event"],
        "events": s.get("events", []),
        "enemies": [[round(e["x"], 1), round(e["y"], 1), _enemy_code(e),
                     1 if e["vx"] >= 0 else -1, e["squash"],
                     1 if (e["mode"] == "shell" and
                           e["revive"] >= SHELL_REVIVE_F - SHELL_WIGGLE_F) else 0]
                    for e in s["enemies"]],
        "shield": s.get("shield", False),
        "star": s.get("star", 0),
        "used_q": s.get("used_q", []),
        "bump": s.get("bump", [-1, 0]),
        "items": [[round(it["x"], 1), round(it["y"], 1), it["emerge"]]
                  for it in s.get("items", [])],
        "crumbles": [[c["ph"], c["t"]] for c in s.get("crumbles", [])],
        "roul": (s["steps"] // ROULETTE_F) % len(ROULETTE_SEQ),
        "steps": s["steps"],
        "bullets": [[round(b2["x"], 1), round(b2["y"], 1)] for b2 in s.get("bullets", [])],
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
        "plants": [[STAGES[s["stage"]]["pipes"][pl["pi"]][0]
                    + STAGES[s["stage"]]["pipes"][pl["pi"]][2] / 2,
                    STAGES[s["stage"]]["pipes"][pl["pi"]][1],
                    round(pl["off"], 1)] for pl in s.get("plants", [])],
        "elapsed": round(s["clear_time"] if s["cleared"] and s["clear_time"]
                         else time.time() - s["started"], 1),
        "clear_time": s["clear_time"],
    })
