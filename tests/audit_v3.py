import importlib.util, sys, types, itertools
fake=types.ModuleType("flask")
fake.Blueprint=lambda *a,**k: types.SimpleNamespace(route=lambda *a,**k:(lambda f:f))
fake.jsonify=lambda *a,**k:None; fake.request=None
sys.modules["flask"]=fake
spec=importlib.util.spec_from_file_location("g", sys.argv[1])
g=importlib.util.module_from_spec(spec); sys.modules["g"]=g; spec.loader.exec_module(g)

def jump_sim(vx, hold=True):
    """踏切速度 vx で全ホールドジャンプ。(最高上昇, 滞空フレーム)"""
    for below, v0, gh, gf in g.JUMP_TABLE:
        if abs(vx) < below: break
    vy, y, top, t = v0, 0.0, 0.0, 0
    while True:
        grav = gh if (hold and vy < 0) else gf
        if abs(vy) < g.APEX_V: grav *= g.APEX_GRAV      # 頂点補正を実物理と揃える
        vy = min(vy + grav, g.MAX_FALL)
        y += vy; top = min(top, y); t += 1
        if y >= 0: return -top, t

JH_walk, T_walk = jump_sim(g.MAX_WALK)
JH_run,  T_run  = jump_sim(g.MAX_RUN)
JD_walk = T_walk * g.MAX_WALK
JD_run  = T_run  * g.MAX_RUN
print(f"歩きジャンプ 高さ{JH_walk:.0f}px 距離{JD_walk:.0f}px ({T_walk}f) / "
      f"走りジャンプ 高さ{JH_run:.0f}px 距離{JD_run:.0f}px ({T_run}f)")
print(f"コヨーテ{g.COYOTE_F}f バッファ{g.JBUF_F}f 最大落下{g.MAX_FALL}")

allok=True
for st in g.STAGES:
    f=[]
    gs=sorted(st["ground"] + list(st.get("bridge") or []))   # 吊り橋は初期状態で固い
    for a,b in zip(gs,gs[1:]):
        gap=b[0]-(a[0]+a[2]); need=gap+g.PLAYER_W
        # 地面の穴: 歩きジャンプで越えられること (走り不要)
        if need > JD_walk: f.append(f"穴{a[0]+a[2]}(要{need:.0f} 歩き{JD_walk:.0f})")
    qrects=[(qx,qy,g.QBLOCK_W,g.QBLOCK_H) for qx,qy,_ in st.get("qblocks",[])]
    sprects=[(sx,sy,g.SPRING_W,g.SPRING_H) for sx,sy in st.get("springs",[])]
    solids=st["ledges"]+st["pipes"]+st["ceiling"]+qrects+sprects
    for a,b in itertools.combinations(solids,2):
        if a[0]<b[0]+b[2] and a[0]+a[2]>b[0] and a[1]<b[1]+b[3] and a[1]+a[3]>b[1]:
            f.append(f"重なり{a}/{b}")
    surf=[("地",x,x+w,y) for x,y,w,h in st["ground"] + list(st.get("bridge") or [])] \
        +[("リ",lx,lx+lw,lcy-lamp) for lx,lw,lcy,lamp,lom in st.get("lifts",[])] \
        +[("跳",sx-20,sx+g.SPRING_W+20,sy) for sx,sy in st.get("springs",[])] \
        +[("足",x,x+w,y) for x,y,w,h in st["ledges"]] \
        +[("柱",x,x+w,y) for x,y,w,h in st["pipes"]]
    for lx,ly,lw,lh in st["ledges"]+st["pipes"]:
        ok=False
        for nm,sx,ex,sy in surf:
            if sy<=ly: continue
            rise=sy-ly
            # 足場到達: 走りジャンプ許容。高さは天井にぶつからない範囲かも確認
            if rise>JH_run: continue
            d = 0 if (sx<lx+lw and ex>lx) else (lx-ex if lx>ex else sx-(lx+lw))
            if d+g.PLAYER_W<=JD_run: ok=True; break
        if not ok: f.append(f"到達不能({lx},{ly})")
        for cx,cy,cw,ch in st["ceiling"]:
            if lx<cx+cw and lx+lw>cx and cy+ch>ly-g.PLAYER_H:
                f.append(f"足場({lx},{ly})と天井{cx}が干渉")
    for i,(k,ex,lo,hi) in enumerate(st["enemies"]):
        for px,py,pw,ph in st["pipes"]:
            if lo<px+pw and hi+g.ENEMY_W>px: f.append(f"敵{i}が柱{px}と干渉")
        if k=="para": continue                       # はねガメは空中パトロール
        if not any(x<=lo and hi+g.ENEMY_W<=x+w for x,y,w,h in st["ground"]):
            f.append(f"敵{i}が地面外")
        gx,gy,gw,gh=st["goal"]
        if lo<gx+gw and hi+g.ENEMY_W>gx: f.append(f"敵{i}がゴール干渉")
    for qi,(qx,qy,kind) in enumerate(st.get("qblocks",[])):
        qb=qy+g.QBLOCK_H
        floor=None
        for x,y,w,h in st["ground"]+st["ledges"]:
            if x-10<=qx and qx+g.QBLOCK_W<=x+w+10 and y>qb:
                floor=y if floor is None else min(floor,y)
        if floor is None: f.append(f"?ブロック{qi}の下に床なし")
        else:
            gap=floor-qb
            if gap < g.PLAYER_H+6: f.append(f"?ブロック{qi}の下が狭すぎ({gap:.0f}px)")
            if gap-g.PLAYER_H > JH_walk: f.append(f"?ブロック{qi}に頭が届かない(要{gap-g.PLAYER_H:.0f})")
    for si,(sx,sy) in enumerate(st.get("springs",[])):
        if not any(x<=sx and sx+g.SPRING_W<=x+w and abs(y-(sy+g.SPRING_H))<2
                   for x,y,w,h in st["ground"]):
            f.append(f"ばね{si}が地面に接していない")
    for li,(lx,lw,lcy,lamp,lom) in enumerate(st.get("lifts",[])):
        sweep=(lx,lcy-lamp,lw,2*lamp+12)
        for o in solids+st["ground"]:
            if sweep[0]<o[0]+o[2] and sweep[0]+sweep[2]>o[0] and \
               sweep[1]<o[1]+o[3] and sweep[1]+sweep[3]>o[1]:
                f.append(f"リフト{li}の掃引域が{o[:2]}と重なる")
    for i,(cx,cy) in enumerate(st["checkpoints"]):
        if not any(x<=cx<=x+w for x,y,w,h in st["ground"]): f.append(f"中間{i}地面無し")
    for cx,cy in st["coins"]:
        ok=False
        for nm,sx,ex,sy in surf:
            reach = 235+22 if nm=="跳" else JH_run+22    # ばねは跳躍235px
            if sy>cy and sy-cy<=reach and sx-70<=cx<=ex+70: ok=True; break
        if not ok: f.append(f"コイン({cx},{cy})不可")
    print(f"【{st['name']}】", "✅ 合格" if not f else "★ " + " / ".join(f))
    allok = allok and not f
print("総合:", "✅ 全ステージ合格" if allok else "★ 要修正")
sys.exit(0 if allok else 1)
