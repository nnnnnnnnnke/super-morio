"""みんなのランキングと遅延測定のテスト。  uv run tests/test_board.py server/game.py"""
import importlib, json, os, sys, tempfile, time, types
fake=types.ModuleType("flask")
fake.Blueprint=lambda *a,**k: types.SimpleNamespace(route=lambda *a,**k:(lambda f:f))
fake.jsonify=lambda *a,**k:None; fake.request=None
sys.modules["flask"]=fake
sys.path.insert(0, os.path.dirname(os.path.abspath(sys.argv[1])))
g=importlib.import_module("game"); B=importlib.import_module("board"); srv=importlib.import_module("gamesrv")

P=[]; F=[]
def check(n,c,d=""):
    (P if c else F).append(n+(f" — {d}" if not c else ""))

def res(stage, t, rtt=None, net=None):
    return {"stage": stage, "rank": "A", "time": t, "coins": 3, "coins_total": 5, "deaths": 0,
            "rtt": rtt, "net": net}

tmp=tempfile.mkdtemp()

# ---- 1. ニックネームの正規化 ----
check("全角英数は半角に", B.clean_name("ＡＢＣ１２３")=="ABC123", B.clean_name("ＡＢＣ１２３"))
check("制御文字・改行を落とす", B.clean_name("a\u0000b\nc​d")=="ab cd", repr(B.clean_name("a\u0000b\nc​d")))
check("前後と連続の空白をそろえる", B.clean_name("  も  り  ")=="も り")
check("10文字まで", len(B.clean_name("あ"*30))==B.NAME_MAX)
check("None や数値も受け付ける", B.clean_name(None)=="" and B.clean_name(42)=="42")

# ---- 2. 順位: 1人1行・最速記録・同タイムは先着 ----
b=B.Board()
i1=b.add("s1","もりお",res(0,50.0),now=1)
i2=b.add("s2","ぴーち",res(0,40.0),now=2)
i3=b.add("s1","もりお",res(0,45.0),now=3)          # 自己ベスト更新 (2位のまま)
i4=b.add("s1","もりお",res(0,60.0),now=4)          # 遅い記録は順位に影響しない
i5=b.add("s3","コッパ",res(0,45.0),now=5)          # 同タイムは先に出した方が上
check("初回は1位/1人・ベスト", i1=={"place":1,"players":1,"best":True}, i1)
check("速い人が入ると1位", i2["place"]==1 and i2["players"]==2, i2)
check("自己ベスト更新", i3["best"] and i3["place"]==2, i3)
check("遅い記録はベストでない", not i4["best"] and i4["place"]==2, i4)
check("同タイムは先着が上", i5["place"]==3, i5)
v=b.view(3)
top=v["stages"][0]["top"]
check("上位は1人1行", [r["name"] for r in top]==["ぴーち","もりお","コッパ"], [r["name"] for r in top])
check("行のタイムは最速", top[1]["time"]==45.0, top[1])
check("大文字小文字は同じ人", b.add("s9","MORIO",res(1,10.0))["players"]==1 and
      b.add("s8","morio",res(1,9.0))["players"]==1)

# ---- 3. ななしさんはセッションごと、セッションIDは外に出さない ----
b=B.Board()
b.add("secretsid1","",res(0,30.0)); b.add("secretsid2","",res(0,31.0))
v=b.view(3); dump=json.dumps(v,ensure_ascii=False)
check("名前なしは別々の行", v["stages"][0]["players"]==2)
check("名前なしは ななしさん", all(r["name"]==B.NAMELESS for r in v["stages"][0]["top"]))
check("セッションIDが漏れない", "secretsid" not in dump and "sid:" not in dump)
check("行の目印はハッシュ", all(len(r["id"])==12 for r in v["stages"][0]["top"]))

# ---- 4. 経路別の集計 (遅い経路から順) と、名前が無いときの遅延区分 ----
b=B.Board()
for i,(t,rtt,net) in enumerate([(120,1130,"地球 2 周"),(150,1140,"地球 2 周"),(60,480,"地球 1 周"),
                                (40,9,"国内だけ"),(44,10,"国内だけ"),(48,11,"国内だけ")]):
    b.add(f"s{i}",f"p{i}",res(0,t,rtt),net=net)
nets=b.view(3)["stages"][0]["nets"]
check("遅い経路から並ぶ", [n["net"] for n in nets]==["地球 2 周","地球 1 周","国内だけ"], [n["net"] for n in nets])
check("まんなかのタイム", nets[0]["median"]==135.0 and nets[2]["median"]==44.0, nets)
check("人数と遅延", nets[2]["players"]==3 and nets[2]["rtt"]==10, nets[2])
b=B.Board(); b.add("a","x",res(0,50,20)); b.add("b","y",res(0,90,700)); b.add("c","z",res(0,70,None))
labels=[n["net"] for n in b.view(3)["stages"][0]["nets"]]
check("経路名が無ければ遅延で分ける", labels==["遅延 500ms 以上","遅延 50ms 未満","遅延 不明"], labels)

# ---- 5. 非表示・全消去・保存と読み直し ----
path=os.path.join(tmp,"board.jsonl")
b=B.Board(path)
b.add("s1","わるいなまえ",res(0,10.0)); b.add("s2","よいこ",res(0,20.0))
b.hide("わるいなまえ")
top=b.view(3)["stages"][0]["top"]
check("非表示にした名前は消える", [r["name"] for r in top]==["よいこ"], top)
check("非表示の人の新記録は順位なし", b.add("s1","わるいなまえ",res(0,5.0))["place"] is None)
b2=B.Board(path)
check("再起動しても記録が残る", len(b2.runs)==3 and b2.view(3)["stages"][0]["top"][0]["name"]=="よいこ")
b2.unhide("わるいなまえ")
check("非表示の取り消し", B.Board(path).view(3)["stages"][0]["top"][0]["name"]=="わるいなまえ")
b2.reset()
check("全消去 (ファイルも空)", B.Board(path).runs==[] and os.path.getsize(path)==0)
b2.add("s5","のこる",res(0,33.0))
with open(path,"a") as f:
    f.write('{"op":"run","run":{"stage":0}}\n{broken\n{"op":"hide","key":5}\n[1]\n'
            '{"op":"run","run":{"key":"k","stage":"0","time":1,"at":1,"name":"x"}}\n')
b3=B.Board(path)
try: v3=b3.view(3); ok=True
except Exception as ex: ok=False; v3=repr(ex)
check("欠けた行・型違いの行は読み飛ばして表示できる", ok and [r["name"] for r in v3["stages"][0]["top"]]==["のこる"], v3)
b=B.Board(os.path.join(tmp,"no","such","dir.jsonl"))
check("書けない場所でも止まらない", b.add("s","n",res(0,1.0))["place"]==1)
old=B.KEEP; B.KEEP=5
b=B.Board()
for i in range(8): b.add(f"s{i}",f"p{i}",res(0,float(i)))
check("記録の上限", len(b.runs)==5 and b.runs[0]["time"]==3.0, len(b.runs))
cp=os.path.join(tmp,"compact.jsonl"); b=B.Board(cp)
b.add("h","わるい",res(0,1.0)); b.hide("わるい")
for i in range(30): b.add(f"s{i}",f"p{i}",res(0,float(i+10)))
nlines=sum(1 for _ in open(cp))
check("ファイルも上限を超えたら書き直す", nlines<=2*B.KEEP, nlines)
b4=B.Board(cp)
check("書き直しても最新の記録と非表示が残る", [r["name"] for r in b4.runs]==[f"p{i}" for i in range(25,30)]
      and "わるい" in b4.hidden, ([r["name"] for r in b4.runs], b4.hidden))
with open(cp,"a") as f:
    for i in range(40): f.write(json.dumps({"op":"run","run":{**b4.runs[0],"at":i}},ensure_ascii=False)+"\n")
B.Board(cp); nlines=sum(1 for _ in open(cp))
check("読み込み時に大きすぎたら書き直す", nlines<=2*B.KEEP, nlines)
B.KEEP=old
b=B.Board(); b.add("s","n",res(0,1.0)); v1=b.view(3)
check("変化が無ければ集計を使い回す", b.view(3) is v1)
b.add("s","n",res(0,0.5))
check("記録が増えたら作り直す", b.view(3) is not v1)

# ---- 6. 経路の名前ファイル ----
nf=os.path.join(tmp,"route.json")
with open(nf,"w") as f: json.dump({"mode":"bad","label":"地球 2 周ルート (設定ミス)","short":"地球 2 周"},f)
check("short を優先", B.net_label(nf)=="地球 2 周")
with open(nf,"w") as f: json.dump({"label":"高遅延の道"},f)
check("無ければ label", B.net_label(nf)=="高遅延の道")
with open(nf,"w") as f: f.write("[1,2")
check("壊れていれば None", B.net_label(nf) is None and B.net_label(os.path.join(tmp,"nope")) is None)

# ---- 7. ステージ結果に遅延 (中央値) と経路 (多数決) が付く ----
s=g._new_state()
s["st_rtt"]=[1130,1100,10,1140,1120]; s["st_net"]=["地球 2 周"]*4+["国内だけ"]
g._stage_result(s,g.STAGES[0])
check("遅延は中央値", s["result"]["rtt"]==1120, s["result"])
check("経路は多数決", s["result"]["net"]=="地球 2 周", s["result"])
check("結果ごとに合図が進む", s["results_n"]==1)
g._enter_stage(s,1)
check("ステージが変わると測り直し", s["st_rtt"]==[] and s["st_net"]==[])
s=g._new_state(); g._stage_result(s,g.STAGES[0])
check("測れていなければ None", s["result"]["rtt"] is None and s["result"]["net"] is None)

# ---- 8. サーバの遅延測定 (st を返すと 往復 = いま - st - held) ----
srv.BOARD=B.Board()
srv._net[0],srv._net[1]=None,time.time()+3600      # 経路ファイルは読まない
e=srv._blank("abc123"); s=e["s"]
now=time.time()*1000
srv._apply_meta(e,{"name":"ＭＯＲＩＯ","st":now-1150,"held":20})
check("往復は いま-st-held", abs(s["rtt"]-1130)<30, s.get("rtt"))
check("名前は正規化して持つ", e["name"]=="MORIO")
check("自分の行の目印", e["bk"]==B.public_id(B.run_key("abc123","MORIO")))
for _ in range(40): srv._apply_meta(e,{"st":time.time()*1000-10,"held":0})
check("平滑で新しい値に寄る", s["rtt"]<60, s["rtt"])
check("サンプルは0.5秒おき", len(s["st_rtt"])==1, s["st_rtt"])
before=s["rtt"]
for bad in ({"st":"x","held":0},{"st":True,"held":0},{"st":time.time()*1000+5000,"held":0},
            {"st":time.time()*1000-60000,"held":0},{"held":3}):
    srv._apply_meta(e,bad)
check("おかしな値は無視", s["rtt"]==before)
snap=srv._snapshot(e)
check("状態に st・rtt・bk が載る", isinstance(snap["st"],int) and snap["rtt"]==round(s["rtt"]) and snap["bk"]==e["bk"], snap.get("rtt"))

# ---- 9. ループがクリアを拾ってランキングへ (順位が評価カードに載る) ----
e=srv._blank("zzz"); srv._apply_meta(e,{"name":"ぴーち"})
e["s"]["st_rtt"]=[500]; e["s"]["st_net"]=["地球 1 周"]
g._stage_result(e["s"],g.STAGES[0])
n=e["s"].get("results_n",0)
if n!=e.get("results_seen",0):
    e["results_seen"]=n; srv._record(e)
r=e["s"]["result"]
check("クリアが記録される", len(srv.BOARD.runs)==1 and srv.BOARD.runs[0]["name"]=="ぴーち")
check("順位が結果に載る", r.get("place")==1 and r.get("players")==1, r)
check("経路と遅延も記録", srv.BOARD.runs[0]["net"]=="地球 1 周" and srv.BOARD.runs[0]["rtt"]==500)
e2=srv._blank("zzz")
check("やり直しで合図は0から", e2["results_seen"]==0 and e2["s"].get("results_n",0)==0)

# ---- 10. 管理操作はトークンが必要 ----
class Req:
    def __init__(self,d): self.d=d
    def get_json(self,silent=False): return self.d
calls=[]
srv.jsonify=lambda *a,**k: (calls.append(a[0] if a else k), types.SimpleNamespace(headers={}))[1]
srv.request=Req({"token":"x","action":"reset"})
os.environ.pop("MORIO_ADMIN_TOKEN",None)
out=srv.board_admin()
check("トークン未設定なら使えない", isinstance(out,tuple) and out[1]==403 and len(srv.BOARD.runs)==1)
os.environ["MORIO_ADMIN_TOKEN"]="s3cret"
out=srv.board_admin()
check("トークン違いは拒否", isinstance(out,tuple) and out[1]==403 and len(srv.BOARD.runs)==1)
srv.request=Req({"token":"s3cret","action":"hide","name":"ぴーち"}); srv.board_admin()
check("正しいトークンで非表示", srv.BOARD.view(3)["stages"][0]["top"]==[])
srv.request=Req({"token":"s3cret","action":"reset"}); srv.board_admin()
check("正しいトークンで全消去", srv.BOARD.runs==[])
srv.request=Req({"token":"s3cret","action":"nope"}); out=srv.board_admin()
check("知らない操作は 400", isinstance(out,tuple) and out[1]==400)

print("\n".join("  OK  "+x for x in P))
print("\n".join("  NG  "+x for x in F))
print(f"合格 {len(P)} / 失敗 {len(F)}")
sys.exit(1 if F else 0)
