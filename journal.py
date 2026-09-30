#!/usr/bin/env python3
"""AI Trading Journal - recebe webhooks do TradingView e regista as trades (só stdlib).
PowerShell:
  $env:JOURNAL_SECRET="a-tua-chave"; $env:ANTHROPIC_API_KEY="sk-ant-..."; python journal.py
Dashboard: http://localhost:8000/?k=a-tua-chave
"""
import json, os, re, sqlite3, urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

SECRET = os.getenv("JOURNAL_SECRET", "muda-isto")  # chave do dashboard
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", SECRET)  # igual ao "Webhook secret" do script Pine
API_KEY = os.getenv("ANTHROPIC_API_KEY")
MODEL = os.getenv("JOURNAL_MODEL", "claude-sonnet-5-5")
PORT = int(os.getenv("PORT", "8000"))


def db():
    c = sqlite3.connect(os.getenv("JOURNAL_DB", "journal.db"))
    c.row_factory = sqlite3.Row
    c.execute("create table if not exists t(id text primary key, symbol text, side text, entry real, sl real,"
              " tp real, exit real, t_in text, t_out text, r real, note text default '')")
    return c


def iso(ms=None):
    ts = float(ms) / 1000 if ms else datetime.now(timezone.utc).timestamp()
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds")


def webhook(d):
    if d.get("secret") != WEBHOOK_SECRET:
        return 403
    act = d.get("action")
    if act not in ("TV_FILL_CONFIRM", "JOURNAL_EXIT"):
        return 200  # PLACE_LIMIT, MOVE_SL, etc. não interessam ao journal
    sym = str(d["symbol"]).split(":")[-1]
    base = f'{sym}:{d["id"]}'
    when = iso(d.get("tms"))
    c = db()
    if act == "TV_FILL_CONFIRM":
        side = "long" if d["side"].lower() in ("buy", "long") else "short"
        # trade importada dos logs que entretanto chegou em direto: fica a versão em direto
        c.execute("delete from t where symbol=? and side=? and id like ? and abs(strftime('%s',t_in)-strftime('%s',?))<=600",
                  (sym, side, sym + ":imp-%", when))
        c.execute("insert into t(id,symbol,side,entry,sl,tp,t_in) values(?,?,?,?,?,?,?) on conflict(id) do update set "
                  "entry=excluded.entry, sl=excluded.sl, tp=excluded.tp", (f"{base}:{when}", sym, side, d["entry"], d["sl"], d["tp"], when))
    else:
        t = c.execute("select * from t where symbol=? and substr(id,1,?)=? and exit is null order by t_in desc limit 1",
                      (sym, len(base) + 1, base + ":")).fetchone()
        if not t:
            return 404
        risk = abs(t["entry"] - t["sl"]) or 1e-9  # R sempre sobre o risco ORIGINAL (SL do fill)
        move = d["exit"] - t["entry"] if t["side"] == "long" else t["entry"] - d["exit"]
        r = d["r"] if d.get("r") is not None and "50" in str(d.get("result", "")) else round(move / risk, 3)
        c.execute("update t set exit=?, t_out=?, r=? where id=?", (d["exit"], when, r, t["id"]))
    c.commit()
    return 200


ALIAS = {
    "id": ["id", "tradeid", "trade_id"], "symbol": ["symbol", "ticker", "pair", "sym"],
    "side": ["side", "dir", "direction"], "t_in": ["filltime", "fill_time", "fill", "entrytime"],
    "t_out": ["exittime", "exit_time", "closetime"], "entry": ["entry", "entryprice", "fillprice"],
    "sl0": ["sl0", "slorig", "sl_orig", "originalsl", "initialsl", "slinitial", "slinicial"],
    "sl": ["sl", "stop", "stoploss"], "tp": ["tp", "target", "takeprofit"], "exit": ["exit", "exitprice", "exitpx"],
}


def _num(v):
    try:
        x = float(v)
        return x if x == x else None
    except (TypeError, ValueError):
        return None


def _ts(v, off=0):
    if v in (None, "", "na", "NaN", "0"):
        return None
    try:
        x = float(v)
        return iso(x if x > 1e11 else x * 1000)
    except ValueError:
        pass
    for f in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M"):
        try:  # horas sem fuso são tratadas como UTC
            return (datetime.strptime(v.strip(), f).replace(tzinfo=timezone.utc) - timedelta(hours=off)).isoformat(timespec="seconds")
        except ValueError:
            pass
    return None


def parse_log(text, default_symbol=""):
    out, skipped, warns = [], 0, set()
    for line in text.splitlines():
        if "SELFTRADE" not in line:
            continue
        body = line.split("SELFTRADE", 1)[1].strip(" :-")
        parts = [p.strip() for p in body.strip("| ").split("|")]
        if len(parts) >= 12 and parts[2].upper() in ("LONG", "SHORT"):  # formato real do script: SELFTRADE|par|id|lado|...
            xk = {k.lower(): v for k, v in (p.split("=", 1) for p in parts[12:] if "=" in p)}
            sym, lng = parts[0].split(":")[-1], parts[2].upper() == "LONG"
            entry, sl_o, tp, ex = _num(parts[6]), _num(parts[7]), _num(parts[8]), _num(parts[10])
            t_in, t_out = _ts(parts[4], 1), _ts(parts[5], 1)  # horas do log em GMT+1
            if entry is None or sl_o is None or t_in is None:
                skipped += 1
                continue
            r = _num(xk.get("total_r"))
            if r is None and ex is not None:
                r = round(((ex - entry) if lng else (entry - ex)) / (abs(entry - sl_o) or 1e-9), 3)
            out.append({"id": f"{sym}:imp-{t_in}", "symbol": sym, "side": "long" if lng else "short", "entry": entry,
                        "sl": sl_o, "tp": tp, "exit": ex, "t_in": t_in, "t_out": t_out, "r": r})
            continue
        kv = {}
        if "{" in body:
            try:
                kv = {k.lower(): str(v) for k, v in json.loads(body[body.index("{"):]).items()}
            except ValueError:
                pass
        if not kv:
            for p in re.split(r"\s*[|;]\s*|,\s*(?=[A-Za-z_]\w*\s*[=:])", body):
                m = re.match(r"\s*([A-Za-z_]\w*)\s*[=:]\s*(.*)", p)
                if m:
                    kv[m.group(1).lower()] = m.group(2).strip().strip('"')
        if len(kv) < 4:  # formato "k=v k=v" só com espaços
            kv = {k.lower(): v.strip('"') for k, v in re.findall(r'(\w+)=("[^"]*"|\d{4}-\d\d-\d\d[ T]\d\d:\d\d(?::\d\d)?|\d\d/\d\d/\d{4} \d\d:\d\d(?::\d\d)?|\S+)', body)}
        g = lambda n: next((kv[a] for a in ALIAS[n] if a in kv), None)
        entry, sl0, sl, tp, ex = (_num(g(n)) for n in ("entry", "sl0", "sl", "tp", "exit"))
        exr = str(g("exit") or "").upper()  # "exit" pode ser o motivo (TP/SL) em vez do preço
        if ex is None and "TP" in exr:
            ex = tp
        if ex is None and "SL" in exr:
            ex = sl
        side, t_in = (g("side") or "").lower(), _ts(g("t_in"))
        sl_o = sl0 if sl0 is not None else sl
        if entry is None or sl_o is None or t_in is None or side not in ("buy", "long", "sell", "short"):
            skipped += 1
            continue
        if sl0 is None:
            warns.add("O log não traz o SL original: o R usa o campo SL, que pode ser o SL já movido.")
        sym = g("symbol") or default_symbol
        if not sym:
            warns.add("Sem par no log: preenche o campo 'Par'.")
        risk = abs(entry - sl_o) or 1e-9
        lng = side in ("buy", "long")
        closed = ex not in (None, 0.0)
        out.append({"id": f"{sym}:{g('id') or t_in}", "symbol": sym, "side": "long" if lng else "short", "entry": entry,
                    "sl": sl_o, "tp": tp, "exit": ex if closed else None, "t_in": t_in, "t_out": _ts(g("t_out")),
                    "r": round(((ex - entry) if lng else (entry - ex)) / risk, 3) if closed else None})
    return out, skipped, sorted(warns)


def do_import(text, symbol, dry):
    rows, skipped, warns = parse_log(text, symbol)
    if not dry:
        c = db()
        for t in rows:  # upsert: reimportar não duplica nem apaga notas
            c.execute("insert into t(id,symbol,side,entry,sl,tp,exit,t_in,t_out,r) values(:id,:symbol,:side,:entry,:sl,"
                      ":tp,:exit,:t_in,:t_out,:r) on conflict(id) do update set symbol=:symbol,side=:side,entry=:entry,"
                      "sl=:sl,tp=:tp,exit=:exit,t_in=:t_in,t_out=:t_out,r=:r", t)
        c.commit()
    return {"n": len(rows), "skipped": skipped, "warnings": warns, "sample": rows[:3]}


def ai_review():
    if not API_KEY:
        return "Define ANTHROPIC_API_KEY para ativar a análise com IA."
    rows = db().execute("select symbol,side,t_in,r,note from t where r is not null order by t_in desc limit 150").fetchall()
    if not rows:
        return "Ainda não há trades fechadas."
    prompt = ("Analisa este journal de trades (R = múltiplos do risco original). Procura padrões por par, direção, "
              "hora e dia da semana, aponta o que está a funcionar e o que não está, e sugere 3 ajustes concretos. "
              "Sê direto e não inventes o que os dados não mostram.\n" + json.dumps([dict(r) for r in rows]))
    req = urllib.request.Request("https://api.anthropic.com/v1/messages", method="POST",
        headers={"x-api-key": API_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
        data=json.dumps({"model": MODEL, "max_tokens": 1200, "messages": [{"role": "user", "content": prompt}]}).encode())
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            return "".join(b.get("text", "") for b in json.load(r)["content"])
    except Exception as e:
        return f"Erro na API: {e}"


PAGE = """<!doctype html><html lang=pt><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>Trading Journal</title>
<style>
:root{--bg:#eef0f3;--ink:#1c2430;--mut:#6b7686;--line:#d5dae1;--win:#1f7a4d;--loss:#b3402f;--acc:#274c8f}
*{box-sizing:border-box}body{margin:0;padding:20px;background:var(--bg);color:var(--ink);font:14px/1.5 system-ui,sans-serif}
main{max-width:1000px;margin:auto}h1{font-size:22px;margin:0 0 16px}
.stats{display:flex;flex-wrap:wrap;gap:24px;padding:12px 0;border-block:1px solid var(--line);margin-bottom:16px}
.stats b{display:block;font-size:20px;font-variant-numeric:tabular-nums}.stats span{color:var(--mut)}
svg{width:100%;height:160px;background:#fff;border:1px solid var(--line);margin-bottom:16px}
.wrap{overflow-x:auto}table{width:100%;border-collapse:collapse;background:#fff;font-variant-numeric:tabular-nums}
th,td{padding:6px 10px;border-bottom:1px solid var(--line);text-align:left;white-space:nowrap}th{color:var(--mut);font-weight:500}
.w{color:var(--win)}.l{color:var(--loss)}input{width:100%;min-width:160px;border:0;background:transparent;font:inherit;color:inherit}
button{margin:16px 0 8px;padding:8px 14px;border:0;background:var(--acc);color:#fff;font:inherit;cursor:pointer}
button:focus-visible,input:focus-visible{outline:2px solid var(--acc)}h2{font-size:16px;margin:24px 0 8px}textarea{width:100%;font:12px monospace;border:1px solid var(--line);padding:6px}#isy{border:1px solid var(--line);background:#fff;padding:6px;margin-top:6px}#io{white-space:pre-wrap;font-size:12px}#ai{white-space:pre-wrap;background:#fff;padding:12px;border:1px solid var(--line)}
</style><main><h1>Trading Journal</h1><div class=stats id=s></div><svg id=g viewBox="0 0 600 160" preserveAspectRatio=none></svg>
<div class=wrap><table><thead><tr><th>Entrada<th>Par<th>Lado<th>Entry<th>SL<th>TP<th>Exit<th>R<th>Nota</tr></thead><tbody id=b></tbody></table></div>
<h2>Importar logs (SELFTRADE)</h2><textarea id=il rows=5 placeholder="Cola aqui as linhas dos Pine Logs"></textarea><input id=isy placeholder="Par, se o log não o indicar (ex: GBPUSD)"><button onclick=imp(true)>Pré-visualizar</button> <button onclick=imp(false)>Importar</button><pre id=io></pre><button onclick=review()>Analisar com IA</button><div id=ai>Sem análise ainda.</div></main>
<script>
const K=new URLSearchParams(location.search).get('k')||'';
const f=(n,d=2)=>n==null?'-':(+n).toFixed(d);
async function load(){
 const T=await(await fetch('/api/trades?k='+K)).json(),R=T.filter(t=>t.r!=null).map(t=>t.r);
 const gp=R.filter(r=>r>0).reduce((a,b)=>a+b,0),gl=-R.filter(r=>r<=0).reduce((a,b)=>a+b,0);
 let e=0,pk=0,dd=0;const p=[0];R.forEach(r=>{e+=r;p.push(e);pk=Math.max(pk,e);dd=Math.max(dd,pk-e)});
 s.innerHTML=[['Trades',R.length],['Win rate',R.length?f(100*R.filter(r=>r>0).length/R.length,1)+'%':'-'],
  ['Profit factor',gl?f(gp/gl):'-'],['Total R',f(e)],['Max drawdown',f(dd)+' R']].map(x=>`<div><b>${x[1]}</b><span>${x[0]}</span></div>`).join('');
 const lo=Math.min(...p),hi=Math.max(...p,lo+1),X=i=>i*600/Math.max(p.length-1,1),Y=v=>150-(v-lo)/(hi-lo)*140;
 g.innerHTML=`<line x1=0 x2=600 y1=${Y(0)} y2=${Y(0)} stroke="#d5dae1"/><polyline fill=none stroke="#274c8f" stroke-width=2 vector-effect=non-scaling-stroke points="${p.map((v,i)=>X(i)+','+Y(v)).join(' ')}"/>`;
 b.innerHTML=T.slice().reverse().map(t=>`<tr><td>${t.t_in.replace('T',' ').slice(0,16)}<td>${t.symbol}<td>${t.side}<td>${t.entry}<td>${t.sl}<td>${t.tp}<td>${t.exit??'aberta'}
  <td class="${t.r>0?'w':'l'}">${f(t.r)}<td><input value="${(t.note||'').replace(/"/g,'&quot;')}" aria-label="Nota" onchange="note('${t.id}',this.value)"></tr>`).join('');
}
const post=(u,d)=>fetch(u,{method:'POST',body:JSON.stringify({k:K,...d})});
const note=(id,n)=>post('/api/note',{id,note:n});
async function imp(dry){const r=await(await post('/api/import',{text:il.value,symbol:isy.value,dry})).json();io.textContent=r.error||(dry?'Pré-visualização: ':'Importadas: ')+r.n+' trades, '+r.skipped+' linhas ignoradas.\\n'+r.warnings.join('\\n')+(dry?'\\n'+JSON.stringify(r.sample,null,1):'');if(!dry)load()}
async function review(){ai.textContent='A analisar...';ai.textContent=(await(await post('/api/ai',{})).json()).text}
load();setInterval(load,15000);
</script></html>"""


class H(BaseHTTPRequestHandler):
    def reply(self, code, body, ct="application/json"):
        b = (body if isinstance(body, str) else json.dumps(body)).encode()
        self.send_response(code)
        self.send_header("Content-Type", ct + "; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        u = urlparse(self.path)
        if parse_qs(u.query).get("k", [""])[0] != SECRET:
            return self.reply(403, "Chave em falta: abre /?k=A_TUA_CHAVE", "text/plain")
        if u.path == "/api/trades":
            return self.reply(200, [dict(r) for r in db().execute("select * from t order by t_in")])
        self.reply(200, PAGE, "text/html")

    def do_POST(self):
        try:
            d = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            if self.path == "/webhook":
                return self.reply(webhook(d), {"ok": True})
            if d.get("k") != SECRET:
                return self.reply(403, {})
            if self.path == "/api/note":
                c = db(); c.execute("update t set note=? where id=?", (d["note"], d["id"])); c.commit()
                return self.reply(200, {"ok": True})
            if self.path == "/api/import":
                return self.reply(200, do_import(d.get("text", ""), d.get("symbol", "").strip(), bool(d.get("dry"))))
            if self.path == "/api/ai":
                return self.reply(200, {"text": ai_review()})
            self.reply(404, {})
        except Exception as e:
            self.reply(400, {"error": str(e)})

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    if SECRET == "muda-isto":
        print("Aviso: define JOURNAL_SECRET antes de expor isto à internet.")
    print(f"Journal em http://localhost:{PORT}/?k={SECRET}")
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
