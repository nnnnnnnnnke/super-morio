import importlib.util, sys, types
fake=types.ModuleType("flask")
fake.Blueprint=lambda *a,**k: types.SimpleNamespace(route=lambda *a,**k:(lambda f:f))
fake.jsonify=lambda *a,**k:None; fake.request=None
sys.modules["flask"]=fake
spec=importlib.util.spec_from_file_location("g", sys.argv[1])
g=importlib.util.module_from_spec(spec); sys.modules["g"]=g; spec.loader.exec_module(g)


def tstep(s, k):
    """本番の tick と同じく、ジャンプのエッジ検出をティック単位で行う。"""
    if k.get("jump") and not s.get("jump_prev"):
        s["jbuf"] = g.JBUF_F
    g._step(s, k)
    s["jump_prev"] = bool(k.get("jump"))

P=[]; F=[]
def check(n,c,d=""):
    (P if c else F).append(n+(f" — {d}" if not c else ""))

CASTLE=2
def castle_state(x=100.0):
    s=g._new_state(); g._enter_stage(s,CASTLE)
    s["x"]=x; s["y"]=float(g.GROUND_Y-g.PLAYER_H); s["on_ground"]=True
    s["invuln"]=0; s["jump_prev"]=False; s["plants"]=[]
    return s

st=g.STAGES[CASTLE]
st["firebars"]=[]          # ボットは回転を読めないので城テストでは外す (専用テストは別途)

# ---- 1. 初期状態 ----
s=castle_state()
check("ボス生成", s["boss"] and s["boss"]["mode"]=="walk")
check("橋12枚", s["bridge"]==12)
check("phase=play", s["phase"]=="play")

# ---- 2. ボスは徘徊し、範囲を出ない ----
for _ in range(1200): tstep(s,{})
b=s["boss"]
check("ボス徘徊範囲内", abs(b["x"]-st["boss_home"])<=st["boss_patrol"]+1, f"x={b['x']:.0f}")
check("ボスは橋の上", abs(b["y"]-(g.GROUND_Y-g.BOSS_H))<60, f"y={b['y']:.0f}")

# ---- 3. 炎が出る・当たると死ぬ ----
s=castle_state(300.0)
fired=False
for _ in range(600):
    tstep(s,{})
    if s["flames"]: fired=True; break
check("炎が発射される", fired)
if fired:
    f=s["flames"][0]
    f["x"]=s["x"]; f["y"]=s["y"]+10          # プレイヤーに直撃させる
    tstep(s,{})
    check("炎で被弾", s["deaths"]==1)

# ---- 4. コッパに触れると被弾 ----
s=castle_state()
s["x"]=s["boss"]["x"]-g.PLAYER_W+4; s["y"]=s["boss"]["y"]+4; s["on_ground"]=False
tstep(s,{})
check("コッパ接触で被弾", s["deaths"]==1)

# ---- 5. 溶岩で即ミス ----
s=castle_state()
s["x"]=760.0; s["y"]=418.0; s["on_ground"]=False; s["vy"]=1.0
s["bridge"]=0                                  # 橋なし状態で落とす
died=False
for _ in range(30):
    tstep(s,{})
    if s["deaths"]>0: died=True; break
check("溶岩で即ミス", died)

# ---- 6. 走りジャンプでコッパを飛び越えられる (勝ち筋の検証) ----
s=castle_state(430.0)                          # 助走地点
crossed=False; died_at=None
jumped=-1
for i in range(1200):
    k={"right":True,"run":True}
    if jumped<0 and s["x"]>620 and s["on_ground"]:
        jumped=i
    if 0<=jumped and i-jumped<40:
        k["jump"]=True                     # 40フレームのホールドジャンプ
    tstep(s,k); s["jump_prev"]=bool(k.get("jump"))
    if s["deaths"]>0: died_at=i; break
    if s["x"]>st["boss_home"]+g.BOSS_W+st["boss_patrol"]+10: crossed=True; break
check("走りジャンプでコッパを飛び越えられる", crossed, f"died_at={died_at} x={s['x']:.0f}")

# ---- 7. 斧 → 崩落 → コッパ落下 → 救出 → クリア (全シーケンス) ----
s=castle_state(1100.0)
ax=st["axe"]
s["x"]=ax[0]-10.0; s["y"]=float(g.GROUND_Y-g.PLAYER_H)
for _ in range(30):
    tstep(s,{"right":True})
    if s["phase"]=="collapse": break
check("斧でcollapse開始", s["phase"]=="collapse")
n0=s["bridge"]
for _ in range(20*g.BRIDGE_FALL_EVERY): tstep(s,{})
check("橋が崩れていく", s["bridge"]<n0, f"{n0}->{s['bridge']}")
for _ in range(3000):
    tstep(s,{})
    if s["phase"]=="walkout": break
check("コッパ落下→walkout", s["phase"]=="walkout", s["phase"])
check("コッパ消滅", s["boss"]["mode"]=="gone", s["boss"]["mode"])
for _ in range(3000):
    tstep(s,{})
    if s["phase"]=="text": break
check("プーチ姫の前に到達", s["phase"]=="text", f"x={s['x']:.0f}")
for _ in range(g.ENDING_TEXTS*g.ENDING_TEXT_F+30):
    tstep(s,{})
    if s["cleared"]: break
check("台詞後にクリア", s["cleared"] and s["clear_time"] is not None)
check("5000点ボーナス入り", s["score"]>=5000, s["score"])

# ---- 8. かみつき草: 近いと出ない / 離れると出る / 触れると被弾 ----
s2=g._new_state()                              # stage0 (かみつき草は pipes[1]=1990)
s2["x"]=100.0; s2["invuln"]=0; s2["enemies"]=[]
pl=s2["plants"][0]
out=False
for _ in range(600):
    tstep(s2,{})
    if pl["ph"]=="out": out=True; break
check("離れていればかみつき草が出る", out, f"off={pl['off']:.0f} ph={pl['ph']}")
s3=g._new_state(); s3["invuln"]=0; s3["enemies"]=[]
px,py,pw,ph_=g.STAGES[0]["pipes"][1]
s3["x"]=float(px-40); s3["y"]=float(g.GROUND_Y-g.PLAYER_H); s3["on_ground"]=True
pl3=s3["plants"][0]
for _ in range(600): tstep(s3,{})
check("近いとかみつき草は出ない", pl3["off"]==0, f"off={pl3['off']:.0f}")
# 出ている状態で突っ込む
s4=g._new_state(); s4["invuln"]=0; s4["enemies"]=[]
pl4=s4["plants"][0]; s4["x"]=100.0
for _ in range(600):
    tstep(s4,{})
    if pl4["ph"]=="out": break
# 実プレイの被弾経路: 土管の上を跳び越えようとして頭上から降ってくる
s4["x"]=float(px+pw/2-g.PLAYER_W/2); s4["y"]=float(py-70)
s4["on_ground"]=False; s4["vy"]=2.0
hurt=False
for _ in range(20):
    tstep(s4,{})
    if s4["deaths"]==1: hurt=True; break
check("かみつき草に触れて被弾", hurt)

# ---- 9. 旗の高さボーナス ----
s5=g._new_state(); s5["invuln"]=0
s5["x"]=g.STAGES[0]["goal"][0]-30.0; s5["y"]=float(g.GROUND_Y-g.PLAYER_H)
s5["on_ground"]=True; sc=s5["score"]
for _ in range(10):
    tstep(s5,{"right":True})
    if s5["gseq"]>0: break
check("旗越えで+100と演出開始", s5["gseq"]>0 and s5["score"]-sc==100, f"gseq={s5['gseq']} +{s5['score']-sc}")
for _ in range(g.GSEQ_F+10):
    tstep(s5,{})
    if s5["stage"]==1: break
check("演出後にステージ2へ", s5["stage"]==1, f"stage={s5['stage']}")

print(f"\n合格 {len(P)} / 失敗 {len(F)}")
for x in F: print("  ★FAIL:", x)
sys.exit(1 if F else 0)
