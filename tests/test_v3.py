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
def check(name, cond, detail=""):
    (P if cond else F).append(name + ((" — "+str(detail)) if not cond else ""))

import contextlib
@contextlib.contextmanager
def flat(ground=None):
    """物理テスト用の平坦な世界。頭上の足場・柱・穴を排除する。"""
    st=g.STAGES[0]
    keep={k:st[k] for k in ("ground","ledges","pipes","ceiling","enemies","coins",
                            "cannons","lifts","qblocks","springs")}
    st["ground"]=ground or [(0,g.GROUND_Y,3600,40)]
    st["ledges"]=[]; st["pipes"]=[]; st["ceiling"]=[]; st["coins"]=[]
    st["cannons"]=[]; st["lifts"]=[]; st["qblocks"]=[]; st["springs"]=[]
    try: yield
    finally: st.update(keep)

def fresh(x=100.0):
    s=g._new_state(); s["x"]=x; s["y"]=float(g.GROUND_Y-g.PLAYER_H)
    s["on_ground"]=True; s["invuln"]=0; s["jump_prev"]=False
    s["enemies"]=[]; s["plants"]=[]
    return s

def frames(s,k,n):
    for _ in range(n): tstep(s,k)

mk=lambda kind,x,mode="patrol",vx=0.0:{"kind":kind,"mode":mode,"x":float(x),
    "y":float(g.GROUND_Y-g.ENEMY_H),"vx":vx,"lo":float(x),"hi":float(x),
    "alive":True,"squash":0,"revive":0,"grace":0,"chain":0}

with flat():
    # ---- 1. 可変ジャンプ ----
    def jump_height(hold):
        s=fresh(); top=s["y"]
        tstep(s,{"jump":True}); s["jump_prev"]=True
        for i in range(150):
            tstep(s,{"jump": i<hold}); top=min(top,s["y"])
            if s["on_ground"]: break
        return (g.GROUND_Y-g.PLAYER_H)-top
    h_tap,h_hold=jump_height(3),jump_height(90)
    check("可変ジャンプ(タップ<ホールド)", h_tap<h_hold*0.62, f"tap={h_tap:.0f} hold={h_hold:.0f}")
    check("ホールド高さ110-140px", 110<=h_hold<=140, f"{h_hold:.0f}")

    # ---- 2. 走りジャンプ ----
    s=fresh()
    for _ in range(90): tstep(s,{"right":True,"run":True})
    v_run=s["vx"]; top=s["y"]
    tstep(s,{"right":True,"run":True,"jump":True}); s["jump_prev"]=True
    for _ in range(150):
        tstep(s,{"right":True,"run":True,"jump":True}); top=min(top,s["y"])
        if s["on_ground"]: break
    h_run=(g.GROUND_Y-g.PLAYER_H)-top
    check("走りで最高速", abs(v_run-g.MAX_RUN)<0.01, f"{v_run:.2f}")
    check("走りジャンプ>歩きホールド", h_run>h_hold, f"run={h_run:.0f} walk={h_hold:.0f}")

    # ---- 3. 慣性とスキッド ----
    s=fresh()
    for _ in range(90): tstep(s,{"right":True,"run":True})
    frames(s,{},1); check("離した直後に速度が残る", abs(s["vx"])>3.5, s["vx"])
    n=0
    while abs(s["vx"])>0 and n<300: tstep(s,{}); n+=1
    s2=fresh()
    for _ in range(90): tstep(s2,{"right":True,"run":True})
    m=0; skidded=False
    while s2["vx"]>0 and m<300:
        tstep(s2,{"left":True}); skidded=skidded or s2["skid"]; m+=1
    check("スキッド発生", skidded)
    check("スキッドは摩擦より速く止まる", m<n, f"skid={m}f rel={n}f")

    # ---- 4. 空中は摩擦なし ----
    s=fresh()
    for _ in range(90): tstep(s,{"right":True,"run":True})
    tstep(s,{"right":True,"run":True,"jump":True}); s["jump_prev"]=True
    v0=s["vx"]
    for _ in range(15): tstep(s,{"jump":True})
    check("空中入力なし→vx維持", not s["on_ground"] and abs(s["vx"]-v0)<0.001, f"{v0:.2f}->{s['vx']:.2f} air={not s['on_ground']}")

    # ---- 5. ジャンプバッファ ----
    s=fresh()
    tstep(s,{"jump":True}); s["jump_prev"]=True
    frames(s,{"jump":False},40); s["jump_prev"]=False
    tstep(s,{"jump":True}); s["jump_prev"]=True
    jumped=False
    for _ in range(30):
        tstep(s,{"jump":True})
        if s["vy"]<-4: jumped=True; break
    check("ジャンプバッファ(先行入力)", jumped)

    # ---- 6. 押しっぱなしで自動連続ジャンプしない ----
    s=fresh()
    tstep(s,{"jump":True}); s["jump_prev"]=True
    landed=0; rejumped=False
    for _ in range(240):
        tstep(s,{"jump":True})
        if s["on_ground"]: landed+=1
        if landed>3 and s["vy"]<-4: rejumped=True
    check("押しっぱなしで再ジャンプしない", not rejumped)

    # ---- 7. きのこへい踏み+チェーン ----
    s=fresh(600.0); s["enemies"]=[mk("goomba",600.0)]
    s["y"]=float(g.GROUND_Y-g.ENEMY_H-g.PLAYER_H-60); s["on_ground"]=False; s["vy"]=2.0
    sc0=s["score"]; frames(s,{},40)
    check("きのこへい踏み", s["stomps"]==1 and not s["enemies"][0]["alive"])
    check("チェーン100点", s["score"]-sc0==100, s["score"]-sc0)

    # ---- 8. コウラ化 (踏んだ瞬間を確認) ----
    s=fresh(500.0); s["enemies"]=[mk("koopa",500.0)]
    s["y"]=float(g.GROUND_Y-g.ENEMY_H-g.PLAYER_H-60); s["on_ground"]=False; s["vy"]=2.0
    became=False
    for _ in range(40):
        tstep(s,{})
        if s["enemies"][0]["mode"]=="shell": became=True; break
    check("コウラ化", became)

    # ---- 8b. 真上バウンド後の再接触で蹴りが出る (本家: 静止コウラを踏む=蹴る) ----
    frames(s,{},60)
    check("静止コウラ再接触で滑走", s["enemies"][0]["mode"]=="slide", s["enemies"][0]["mode"])
    check("蹴り直後は無傷(grace)", s["deaths"]==0)

    # ---- 9. 横から触れて蹴る + 連鎖500点 ----
    s=fresh(500.0)
    sh=mk("koopa",560.0,"shell"); tgt=mk("goomba",800.0)
    s["enemies"]=[sh,tgt]; sc1=s["score"]
    for _ in range(120):
        tstep(s,{"right":True})
        if sh["mode"]=="slide": break
    check("横から蹴れる", sh["mode"]=="slide" and sh["vx"]>0, f"{sh['mode']} vx={sh['vx']}")
    check("蹴った側は無傷", s["deaths"]==0)
    for _ in range(120):
        tstep(s,{})
        if not tgt["alive"]: break
    check("滑走コウラが敵を倒す", not tgt["alive"])
    check("コウラ連鎖500点", s["score"]-sc1==500, s["score"]-sc1)

    # ---- 10. 滑走コウラを踏んで停止 ----
    s=fresh(1000.0)
    sh=mk("koopa",1000.0,"slide",g.SHELL_V)
    s["enemies"]=[sh]
    s["y"]=float(g.GROUND_Y-g.ENEMY_H-g.PLAYER_H-30); s["on_ground"]=False; s["vy"]=3.0
    s["x"]=sh["x"]+18                       # 滑走の進行方向へ少し先回り
    stopped=False
    for _ in range(30):
        tstep(s,{})
        if sh["mode"]=="shell": stopped=True; break
        if s["dead"]>0: break
    check("滑走コウラを踏んで停止", stopped, f"mode={sh['mode']} dead={s['dead']}")

    # ---- 11. 滑走コウラに横から触れると被弾 ----
    s=fresh(1500.0)
    sh=mk("koopa",1560.0,"slide",-g.SHELL_V)
    s["enemies"]=[sh]
    hurt=False
    for _ in range(60):
        tstep(s,{})
        if s["deaths"]>0: hurt=True; break
    check("滑走コウラ側面で被弾", hurt)

    # ---- 12. コウラ復活 ----
    s=fresh(100.0)
    sh=mk("koopa",600.0,"shell")
    s["enemies"]=[sh]
    frames(s,{},g.SHELL_REVIVE_F+10)
    check("コウラ復活", sh["mode"]=="walk", sh["mode"])

    # ---- 13. チェーンは着地でリセット ----
    s=fresh(); s["chain"]=4
    s["y"]-=50; s["on_ground"]=False; s["vy"]=1
    frames(s,{},40)
    check("着地でチェーンリセット", s["chain"]==0)

# ---- 14. 滑走コウラは穴に落ちて消える (穴あり地形) ----
with flat(ground=[(0,g.GROUND_Y,600,40),(900,g.GROUND_Y,2700,40)]):
    s=fresh(100.0)
    sh=mk("koopa",400.0,"slide",g.SHELL_V)
    s["enemies"]=[sh]
    gone=False
    for _ in range(600):
        tstep(s,{})
        if not sh["alive"]: gone=True; break
    check("滑走コウラが穴で消滅", gone, f"x={sh['x']:.0f} y={sh['y']:.0f}")

# ---- 15. 通常ステージ: 横から敵で被弾 ----
s=fresh(300.0); s["enemies"]=[mk("goomba",300.0+g.PLAYER_W+10)]
frames(s,{"right":True},30)
check("横から触れて被弾", s["deaths"]==1)

print(f"\n合格 {len(P)} / 失敗 {len(F)}")
for x in F: print("  ★FAIL:", x)
sys.exit(1 if F else 0)
