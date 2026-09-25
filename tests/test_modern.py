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
def world(ground=None, ledges=None, ceiling=None):
    st=g.STAGES[0]
    keep={k:st[k] for k in ("ground","ledges","pipes","ceiling","enemies","coins","plants",
                            "cannons","lifts","qblocks","springs")}
    st["ground"]=ground or [(0,g.GROUND_Y,3600,40)]
    st["ledges"]=ledges or []; st["pipes"]=[]; st["ceiling"]=ceiling or []
    st["coins"]=[]; st["enemies"]=[]; st["plants"]=[]
    st["cannons"]=[]; st["lifts"]=[]; st["qblocks"]=[]; st["springs"]=[]
    try: yield
    finally: st.update(keep)

def fresh(x=100.0):
    s=g._new_state(); s["x"]=x; s["y"]=float(g.GROUND_Y-g.PLAYER_H)
    s["on_ground"]=True; s["invuln"]=0; s["jump_prev"]=False
    s["enemies"]=[]; s["plants"]=[]
    return s

# ---- 1. 「距離と時間」の設計値どおりに跳ぶか ----
with world():
    for idx,(lim,h,tu,td) in enumerate(g.JUMP_DESIGN):
        s=fresh()
        # 目的の踏切速度まで加速
        target = 0.0 if idx==0 else (g.MAX_WALK if idx==1 else g.MAX_RUN)
        run = idx==2
        if target:
            for _ in range(90): tstep(s,{"right":True,"run":run})
        top=s["y"]; tt=0
        k={"jump":True}; 
        if target: k.update({"right":True,"run":run})
        tstep(s,k)
        while not s["on_ground"] and tt<200:
            tstep(s,k); top=min(top,s["y"]); tt+=1
        got=(g.GROUND_Y-g.PLAYER_H)-top
        # 頂点補正で設計値より少し高くなる。+25%以内なら設計どおり
        check(f"設計{h:.0f}px通り跳ぶ(段{idx})", h*0.95 <= got <= h*1.28, f"実測{got:.0f}px")

# ---- 2. 頂点の重力減衰: 頂点付近の滞空が伸びる ----
with world():
    def apex_frames(apex_on):
        old=g.APEX_GRAV
        if not apex_on: g.APEX_GRAV=1.0
        s=fresh(); tstep(s,{"jump":True}); n=0; near=0
        while not s["on_ground"] and n<200:
            tstep(s,{"jump":True})
            if abs(s["vy"])<g.APEX_V: near+=1
            n+=1
        g.APEX_GRAV=old
        return near
    on,off = apex_frames(True), apex_frames(False)
    check("頂点付近の滞空が伸びる", on>off, f"補正あり{on}f / なし{off}f")

# ---- 3. 頂点の水平速度ボーナス ----
with world():
    s=fresh()
    for _ in range(90): tstep(s,{"right":True,"run":True})
    tstep(s,{"right":True,"run":True,"jump":True})
    peak=0
    for _ in range(200):
        tstep(s,{"right":True,"run":True,"jump":True})
        if abs(s["vy"])<g.APEX_V: peak=max(peak,s["vx"])
        if s["on_ground"]: break
    check("頂点で水平速度が伸びる", peak > g.MAX_RUN*1.02, f"頂点vx={peak:.2f} 通常{g.MAX_RUN}")

# ---- 4. コーナー補正: 天井の角に当たっても通り抜ける ----
# 天井の右端が x=300。プレイヤーを角の少し左に置いて真上に跳ばせる。
with world(ceiling=[(0,0,300,300)]):
    s=fresh(297.0)     # 左端297・幅26 -> 天井(〜300)とのかぶりは3pxだけ
    y0=s["y"]
    tstep(s,{"jump":True})
    moved=False
    for _ in range(60):
        tstep(s,{"jump":True})
        if s["x"] >= 300: moved=True
        if s["on_ground"]: break
    check("コーナー補正で角を通り抜ける", moved, f"x={s['x']:.1f} (300以上なら補正成功)")

# ---- 5. コーナー補正が効かない場所ではきちんと止まる ----
with world(ceiling=[(0,0,3600,300)]):
    s=fresh(500.0)
    tstep(s,{"jump":True})
    hit=False
    for _ in range(60):
        tstep(s,{"jump":True})
        if s["y"] <= 300+1 and s["vy"]>=0: hit=True; break
    check("広い天井では正しく止まる", hit, f"y={s['y']:.0f} vy={s['vy']:.2f}")

# ---- 6. 高速リスタート: 0.5秒以内 ----
check("死亡から復帰が0.5秒以内", g.DEATH_F/60.0 <= 0.5, f"{g.DEATH_F/60.0:.2f}秒")
with world():
    s=fresh(); s["y"]=float(g.VIEW_H+100); s["on_ground"]=False
    tstep(s,{})
    n=0
    while s["dead"]>0 and n<200: tstep(s,{}); n+=1
    check("実測でも復帰する", s["dead"]==0 and n<=g.DEATH_F+2, f"{n}f")

print(f"\n合格 {len(P)} / 失敗 {len(F)}")
for x in F: print("  ★FAIL:", x)
sys.exit(1 if F else 0)
