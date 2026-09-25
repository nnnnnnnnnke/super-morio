import importlib.util, sys, types, contextlib
fake=types.ModuleType("flask")
fake.Blueprint=lambda *a,**k: types.SimpleNamespace(route=lambda *a,**k:(lambda f:f))
fake.jsonify=lambda *a,**k:None; fake.request=None
sys.modules["flask"]=fake
spec=importlib.util.spec_from_file_location("g", sys.argv[1])
g=importlib.util.module_from_spec(spec); sys.modules["g"]=g; spec.loader.exec_module(g)

P=[]; F=[]
def check(n,c,d=""):
    (P if c else F).append(n+(f" — {d}" if not c else ""))

def tstep(s,k):
    if k.get("jump") and not s.get("jump_prev"): s["jbuf"]=g.JBUF_F
    g._step(s,k); s["jump_prev"]=bool(k.get("jump"))

@contextlib.contextmanager
def world(**kw):
    st=g.STAGES[0]
    keys=("ground","ledges","pipes","ceiling","enemies","coins","plants",
          "qblocks","springs","conveyors","ice","crumble","cannons","lifts")
    keep={k:st[k] for k in keys}
    st["ground"]=kw.get("ground") or [(0,g.GROUND_Y,3600,40)]
    for k in keys[1:]:
        st[k]=kw.get(k) or []
    try: yield
    finally: st.update(keep)

def fresh(x=100.0):
    s=g._new_state()
    st=g.STAGES[0]
    s["x"]=x; s["y"]=float(g.GROUND_Y-g.PLAYER_H); s["on_ground"]=True
    s["invuln"]=0; s["jump_prev"]=False
    s["enemies"]=[]; s["plants"]=[]
    s["crumbles"]=[{"ph":0,"t":0} for _ in st.get("crumble",[])]
    s["used_q"]=[]; s["items"]=[]
    return s

mk=lambda kind,x,mode="patrol",vx=0.0,y=None:{"kind":kind,"mode":mode,"x":float(x),
    "y":float(g.GROUND_Y-g.ENEMY_H) if y is None else float(y),
    "y0":float(g.GROUND_Y-g.ENEMY_H) if y is None else float(y),
    "vx":vx,"lo":float(x),"hi":float(x),
    "alive":True,"squash":0,"revive":0,"grace":0,"chain":0}

def bump_block(s, qx):
    """ブロックの真下から頭突きする"""
    s["x"]=float(qx+2); s["y"]=float(g.GROUND_Y-g.PLAYER_H); s["on_ground"]=True
    for i in range(200):
        tstep(s,{"jump":True})
        if s["used_q"]: return True
        if s["on_ground"] and i>30: break
    return bool(s["used_q"])

# ---- 1. コインブロック ----
with world(qblocks=[(300,268,"coin")]):
    s=fresh(); sc=s["score"]
    ok=bump_block(s,300)
    check("コインブロック起動", ok and s["score"]-sc==200, f"+{s['score']-sc}")
    used=len(s["used_q"])
    s["jump_prev"]=False
    bump_block(s,300)
    check("2回目は出ない", len(s["used_q"])==used and s["score"]-sc==200)

# ---- 2. 隠しブロック: 下からのみ出現 / 上からは素通り ----
with world(qblocks=[(300,232,"hidden2000")]):
    s=fresh(); sc=s["score"]
    # 上から落ちても何も起きない (実体なし)
    s["x"]=302.0; s["y"]=100.0; s["on_ground"]=False; s["vy"]=2.0
    for _ in range(120): tstep(s,{})
    check("隠しブロックは上から素通り", not s["used_q"] and s["on_ground"])
    ok=bump_block(s,300)
    check("下から突くと出現 (+2000)", ok and s["score"]-sc==2000, f"+{s['score']-sc}")
    check("出現後は足場になる", (300,232,g.QBLOCK_W,g.QBLOCK_H) in g._plats(g.STAGES[0],s))

# ---- 3. キノコ: せり上がり→歩行→取得でシールド→被弾を1回肩代わり ----
with world(qblocks=[(300,268,"shield")]):
    s=fresh()
    bump_block(s,300)
    check("キノコ出現", len(s["items"])==1)
    s["x"]=600.0; s["y"]=374.0; s["on_ground"]=True; s["vy"]=0.0
    for _ in range(600):
        tstep(s,{})                     # キノコは右へ歩くので進行方向で待ち受ける
        if s["shield"]: break
    check("キノコ取得でシールド", s["shield"] and not s["items"], f"shield={s['shield']}")
    s["enemies"]=[mk("goomba", s["x"]+g.PLAYER_W+8)]
    s["invuln"]=0
    for _ in range(30): tstep(s,{"right":True})
    check("シールドが被弾を肩代わり", s["deaths"]==0 and not s["shield"] and s["invuln"]>0)
    s["invuln"]=0; s["enemies"]=[mk("goomba", s["x"]+g.PLAYER_W+8)]
    for _ in range(30): tstep(s,{"right":True})
    check("シールド無しの2発目はミス", s["deaths"]==1)

# ---- 4. ルーレット: 叩いた瞬間の絵柄が当たる ----
with world(qblocks=[(300,268,"roulette")]):
    for want,(idx) in [("star",2),("c1000",1)]:
        s=fresh()
        s["steps"]=idx*g.ROULETTE_F+1          # 絵柄を固定
        sc=s["score"]
        bump_block(s,300)
        if want=="star":
            check("ルーレット: スターが当たる", s["star"]>0)
        else:
            check("ルーレット: 1000点が当たる", s["score"]-sc==1000, f"+{s['score']-sc}")

# ---- 5. スター: 触れるだけで撃破、時間切れで終了 ----
with world():
    s=fresh(); s["star"]=g.STAR_F
    s["enemies"]=[mk("goomba", 200.0), mk("koopa", 260.0)]
    sc=s["score"]
    for _ in range(120): tstep(s,{"right":True})
    check("スター中は触れた敵を撃破", all(not e["alive"] for e in s["enemies"]) and s["deaths"]==0)
    check("スター連鎖 200+400", s["score"]-sc==600, f"+{s['score']-sc}")
    s["star"]=2
    for _ in range(5): tstep(s,{})
    check("スター終了", s["star"]==0 and s["star_chain"]==0)

# ---- 6. ばね: 通常ジャンプより高く、長押しでさらに高い ----
with world(springs=[(400,396)]):
    def spring_height(hold):
        s=fresh(300.0)
        s["x"]=402.0; s["y"]=300.0; s["on_ground"]=False; s["vy"]=2.0
        top=1000; bounced=False
        for i in range(400):
            tstep(s,{"jump":hold})
            if s["vy"]<-5: bounced=True
            if bounced: top=min(top,s["y"])
            if bounced and s["on_ground"]: break
        return (g.GROUND_Y-g.PLAYER_H)-top
    h_norm=spring_height(False); h_held=spring_height(True)
    check("ばねは走りジャンプ(139px)より高い", h_norm>150, f"{h_norm:.0f}px")
    check("ばね+長押しでさらに高い", h_held>h_norm+20, f"{h_held:.0f} vs {h_norm:.0f}")

# ---- 7. ベルトコンベア: 入力なしで流される ----
with world(conveyors=[(200,400,1)]):
    s=fresh(300.0)
    x0=s["x"]
    for _ in range(60): tstep(s,{})
    check("ベルトで流される", s["x"]-x0>40, f"+{s['x']-x0:.0f}px")

# ---- 8. 氷床: 止まるまでの距離が伸びる ----
def stop_dist(ice):
    with world(ice=[(0,3600)] if ice else None):
        s=fresh(100.0)
        s["vx"]=g.MAX_RUN                # 同じ初速から滑らせて比べる
        x0=s["x"]; n=0
        while abs(s["vx"])>0 and n<900: tstep(s,{}); n+=1
        return s["x"]-x0
d_ice, d_norm = stop_dist(True), stop_dist(False)
check("氷床で制動距離が伸びる", d_ice > d_norm*2, f"氷{d_ice:.0f}px / 通常{d_norm:.0f}px")

# ---- 9. 崩れる床: 乗ると揺れ→崩落→落ちる→復活 ----
with world(ground=[(0,g.GROUND_Y,300,40),(700,g.GROUND_Y,2900,40)],
           crumble=[(300,368,120)]):
    s=fresh(100.0)
    s["x"]=330.0; s["y"]=float(368-g.PLAYER_H); s["on_ground"]=False; s["vy"]=1.0
    landed=False; fell=False
    for i in range(g.CRUMBLE_SHAKE_F+80):
        tstep(s,{})
        if s["on_ground"] and abs((s["y"]+g.PLAYER_H)-368)<3: landed=True
        if s["crumbles"][0]["ph"]==2: fell=True
        if s["dead"]>0: break
    check("崩れ床: 乗って揺れて崩落", landed and fell,
          f"landed={landed} ph={s['crumbles'][0]['ph']}")
    check("崩落後は落下してミス", s["dead"]>0 or s["deaths"]>0 or not s["on_ground"])
    for _ in range(g.CRUMBLE_GONE_F+10): tstep(s,{})
    check("崩れ床が復活", s["crumbles"][0]["ph"]==0)

# ---- 10. はねガメ: 上下に舞う / 踏むとコウラ族化して落ちる ----
with world():
    s=fresh(100.0)
    e=mk("para", 500.0, y=296.0); e["vx"]=0.0
    s["enemies"]=[e]
    ys=set()
    for _ in range(200): tstep(s,{}); ys.add(round(e["y"]))
    check("はねガメが上下に舞う", max(ys)-min(ys)>20, f"振幅{max(ys)-min(ys)}px")
    s["x"]=e["x"]+1; s["y"]=e["y"]-g.PLAYER_H-20; s["on_ground"]=False; s["vy"]=3.0
    for _ in range(30):
        tstep(s,{})
        if e["kind"]=="koopa": break
    check("踏むと羽が取れる", e["kind"]=="koopa" and e["mode"]=="walk" and e["alive"])
    for _ in range(120): tstep(s,{})
    check("地面に降りて歩く", abs(e["y"]-(g.GROUND_Y-g.ENEMY_H))<3, f"y={e['y']:.0f}")

# ---- 11. ブロック突き上げで上の敵を倒す (SMB) ----
with world(qblocks=[(300,268,"coin")]):
    s=fresh()
    e=mk("goomba", 302.0, y=268-g.ENEMY_H); s["enemies"]=[e]
    bump_block(s,300)
    check("突き上げで上の敵を倒す", not e["alive"])

# ---- 13. ファイアバー: 決定論で回り、当たると被弾 ----
CASTLE=2
s=g._new_state(); g._enter_stage(s,CASTLE)
s["invuln"]=0; s["enemies"]=[]
fb=g.STAGES[CASTLE]["firebars"][0]
import math as _m
# 玉が真横(右)に来る steps を探して、その位置に立たせる
hit=False
s["x"]=float(fb[0]+g.FIREBAR_R*2-10); s["y"]=float(fb[1]-g.PLAYER_H/2); s["on_ground"]=False; s["vy"]=0.0
for i in range(300):
    g._step(s,{})
    if s["dead"]>0 or s["deaths"]>0: hit=True; break
check("ファイアバーに当たると被弾", hit, f"{i}f経過")

# ---- 14. きのこへい大行列: 6体が同位相で行進 ----
s2=g._new_state(); g._enter_stage(s2,1)
parade=[e for e in s2["enemies"] if e["kind"]=="goomba" and e["lo"]>=3190.0]
check("大行列は6体", len(parade)==6, len(parade))
gaps0=[parade[i+1]["x"]-parade[i]["x"] for i in range(5)]
for _ in range(300): g._step(s2,{})
gaps1=[parade[i+1]["x"]-parade[i]["x"] for i in range(5)]
check("行進しても間隔が保たれる", all(abs(a-b)<1 for a,b in zip(gaps0,gaps1)), f"{gaps1}")

# ---- 15. ずどん砲台: 周期発射・踏める・当たると被弾 ----
st0=g.STAGES[0]
with world(ground=[(0,g.GROUND_Y,3600,40)]):
    st0["pipes"]=[(500,330,60,80)]; st0["cannons"]=[0]
    try:
        s=fresh(900.0)
        s["steps"]=g.CANNON_P              # 発射判定は step 冒頭の steps 値で行われる
        g._step(s,{}); s["jump_prev"]=False
        check("砲台が周期で発射", len(s["bullets"])==1, len(s["bullets"]))
        b=s["bullets"][0]
        check("弾は土管の上から左へ", b["vx"]<0 and abs(b["y"]-(330-22))<1, f"y={b['y']}")
        # 側面に当たると被弾
        s["x"]=b["x"]-g.PLAYER_W-20; s["y"]=float(b["y"]-g.PLAYER_H+g.BULLET_H); s["on_ground"]=False; s["vy"]=0.0
        hurt=False
        for _ in range(60):
            tstep(s,{})
            if s["deaths"]>0: hurt=True; break
        check("弾に触れて被弾", hurt)
        # 踏める
        s2=fresh(900.0); s2["steps"]=g.CANNON_P
        g._step(s2,{}); s2["jump_prev"]=False
        b2=s2["bullets"][0]
        s2["x"]=b2["x"]-30; s2["y"]=float(b2["y"]-g.PLAYER_H-24); s2["on_ground"]=False; s2["vy"]=3.0
        sc2=s2["score"]; stomped=False
        for _ in range(40):
            tstep(s2,{})
            if not s2["bullets"]: stomped=True; break
            if s2["deaths"]>0: break
        check("弾を踏んで撃破+200", stomped and s2["score"]-sc2==200, f"+{s2['score']-sc2} deaths={s2['deaths']}")
    finally:
        st0["pipes"]=[]; st0["cannons"]=[]

# ---- 16. 上下リフト: 動く・乗れる・下降中も足が離れない ----
with world(ground=[(0,g.GROUND_Y,600,40),(1200,g.GROUND_Y,2400,40)]):
    st0["lifts"]=[(800,70,240,90,0.013)]
    try:
        import math as _m
        s=fresh(100.0)
        # 上端付近の steps に合わせて上に落とす
        s["steps"]=int((1.5*_m.pi)/0.013)        # sin=-1 → y=150 (最上点、ここから下降)
        ly=240+_m.sin(s["steps"]*0.013)*90
        ly=240+_m.sin(s["steps"]*0.013)*90       # steps確定後に再計算
        s["x"]=815.0; s["y"]=float(ly-g.PLAYER_H-8); s["on_ground"]=False; s["vy"]=1.0
        onlift=0
        for i in range(240):                      # 下降局面で240f乗り続ける
            tstep(s,{})
            if s["on_ground"]: onlift+=1
        check("下降するリフトに乗り続けられる", onlift>200, f"接地{onlift}/240f")
        check("リフトと一緒に降下した", s["y"]>ly-g.PLAYER_H+40, f"y={s['y']:.0f} 開始{ly-g.PLAYER_H:.0f}")
        # (下降: sin が -1→0 に向かい ly が増える = 画面下へ)
    finally:
        st0["lifts"]=[]

# ---- 17. 二段ジャンプ ----
with world():
    s=fresh(100.0)
    tstep(s,{"jump":True})                     # 1段目
    for _ in range(20): tstep(s,{"jump":True}) # 上昇
    tstep(s,{"jump":False})                    # 一度離す
    y1=s["y"]; tstep(s,{"jump":True})          # 空中で2段目 (新しいエッジ)
    rise2=False
    for _ in range(10):
        tstep(s,{"jump":True})
        if s["vy"]<-4: rise2=True
    check("二段ジャンプで再上昇", rise2, f"vy={s['vy']:.1f}")
    # 3段目は出ない
    tstep(s,{"jump":False}); tstep(s,{"jump":True})
    third=False
    for _ in range(6):
        tstep(s,{"jump":True})
        if s["vy"]<-5: third=True
    check("三段目は出ない", not third)
    # 着地でリセット
    n=0
    while not s["on_ground"] and n<300: tstep(s,{}); n+=1
    check("着地で回数リセット", s["jumps"]==0)

# ---- 18. ステージ評価 ----
s=g._new_state(); s["invuln"]=0; s["enemies"]=[]; s["plants"]=[]
s["x"]=g.STAGES[0]["goal"][0]-30.0; s["y"]=float(g.GROUND_Y-g.PLAYER_H); s["on_ground"]=True
for _ in range(10):
    tstep(s,{"right":True})
    if s["gseq"]>0: break
for _ in range(g.GSEQ_F+10):
    tstep(s,{})
    if s["stage"]==1: break
check("評価が生成される", s.get("result") is not None and s.get("result_t",0)>0)
if s.get("result"):
    r=s["result"]
    check("評価はランクと統計を持つ", r["rank"] in "SABC" and "time" in r and r["stage"]==0,
          str(r))

print(f"\n合格 {len(P)} / 失敗 {len(F)}")
for x in F: print("  ★FAIL:", x)
sys.exit(1 if F else 0)
