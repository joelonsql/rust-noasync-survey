#!/usr/bin/env python3
"""Live dashboard for the rust-noasync crates.io survey.

Pure stdlib HTTP server + Server-Sent Events + psycopg3 LISTEN/NOTIFY.
No external assets, no framework. Start from a TERMINAL (GUI-launched procs
inherit a 256 soft FD limit):  ulimit -n 4096 && python3 dashboard.py
"""
from __future__ import annotations

import json
import queue
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import psycopg

DSN = "host=127.0.0.1 port=5433 dbname=rust_crates application_name=noasync-dashboard"
ADDR = ("127.0.0.1", 8787)
CHANNEL = "noasync_progress"
MIN_INTERVAL = 0.25   # broadcast cap: <=4/s
HEARTBEAT = 10.0      # refresh even without NOTIFY (ETA/throughput decay)

QUERIES: dict[str, str] = {
    "counts": """
        SELECT count(*) AS total,
               count(*) FILTER (WHERE state='pending')   AS pending,
               count(*) FILTER (WHERE state='running')   AS running,
               count(*) FILTER (WHERE state='done')      AS done,
               count(*) FILTER (WHERE state='exhausted') AS exhausted,
               count(*) FILTER (WHERE finished_at > now() - interval '15 minutes') AS done_15m
        FROM noasync.probe""",
    "overall": """
        SELECT status, count(*) AS n,
               round(100.0*count(*)/sum(count(*)) OVER (),1) AS pct
        FROM noasync.current_results GROUP BY status ORDER BY n DESC""",
    "headline": """
        SELECT count(*) FILTER (WHERE survived) AS passed,
               count(*) FILTER (WHERE in_denominator) AS denom,
               round(100.0*count(*) FILTER (WHERE survived)
                     / NULLIF(count(*) FILTER (WHERE in_denominator),0),2) AS survival_pct
        FROM noasync.current_results""",
    "buckets": """
        SELECT t.label, t.cutoff,
               count(*) FILTER (WHERE cr.in_denominator) AS denom,
               count(*) FILTER (WHERE cr.survived) AS passed,
               round(100.0*count(*) FILTER (WHERE cr.survived)
                     / NULLIF(count(*) FILTER (WHERE cr.in_denominator),0),1) AS survival_pct
        FROM (VALUES ('top 100',100),('top 1k',1000),('top 10k',10000),
                     ('top 100k',100000),('all probed',NULL)) AS t(label,cutoff)
        JOIN noasync.current_results cr ON t.cutoff IS NULL OR cr.pop_rank <= t.cutoff
        GROUP BY t.label,t.cutoff ORDER BY t.cutoff NULLS LAST""",
    "estimate": """
        WITH wm AS (SELECT max(rand_key) AS w FROM noasync.probe
                    WHERE claimed_via='random' AND state='done'),
        s AS (SELECT count(*) AS sampled,
                     count(*) FILTER (WHERE cr.in_denominator)::float8 AS n,
                     count(*) FILTER (WHERE cr.survived)::float8 AS k
              FROM noasync.current_results cr, wm WHERE cr.rand_key <= wm.w)
        SELECT sampled, n::int AS denom, k::int AS passed,
               round((100*k/n)::numeric,2) AS p_hat,
               round((100*((k/n+1.9208/n)/(1+3.8416/n)
                     -1.96/(1+3.8416/n)*sqrt((k/n)*(1-k/n)/n+0.9604/(n*n))))::numeric,2) AS ci_lo,
               round((100*((k/n+1.9208/n)/(1+3.8416/n)
                     +1.96/(1+3.8416/n)*sqrt((k/n)*(1-k/n)/n+0.9604/(n*n))))::numeric,2) AS ci_hi
        FROM s WHERE n > 0""",
    "top_blamed": """
        SELECT blamed_crate_name AS culprit, count(*) AS kills,
               count(*) FILTER (WHERE status='fail_async_dep') AS as_dependency,
               count(*) FILTER (WHERE status='fail_async_direct') AS direct,
               round(100.0*count(*)/sum(count(*)) OVER (),1) AS pct
        FROM noasync.current_results
        WHERE status IN ('fail_async_direct','fail_async_dep')
        GROUP BY 1 ORDER BY kills DESC LIMIT 15""",
    "recent": """
        SELECT cr.crate_name, cr.version_num, cr.status, cr.blamed_crate_name,
               cr.finished_at, left(d.first_error,400) AS first_error
        FROM noasync.current_results cr
        LEFT JOIN noasync.probe_diagnostics d ON d.result_id = cr.result_id
        WHERE cr.status <> 'pass'
        ORDER BY cr.finished_at DESC NULLS LAST LIMIT 12""",
    "workers": """
        SELECT claimed_by, crate_name, version_num, claimed_via,
               extract(epoch FROM now()-claimed_at)::int AS running_s
        FROM noasync.probe WHERE state='running' ORDER BY claimed_at""",
}


def fetch_stats() -> dict:
    out: dict = {"generated_at": time.time()}
    with psycopg.connect(DSN, autocommit=True) as conn:
        for name, sql in QUERIES.items():
            cur = conn.execute(sql)
            cols = [d.name for d in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
            out[name] = rows
    return out


class Hub:
    def __init__(self) -> None:
        self._clients: set[queue.Queue] = set()
        self._lock = threading.Lock()

    def register(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=2)
        with self._lock:
            self._clients.add(q)
        return q

    def unregister(self, q: queue.Queue) -> None:
        with self._lock:
            self._clients.discard(q)

    def broadcast(self, payload: str) -> None:
        with self._lock:
            clients = list(self._clients)
        for q in clients:
            try:
                q.put_nowait(payload)
            except queue.Full:
                try:
                    q.get_nowait()
                    q.put_nowait(payload)
                except queue.Empty:
                    pass


HUB = Hub()


def listener_loop() -> None:
    last = 0.0
    while True:
        try:
            with psycopg.connect(DSN, autocommit=True) as conn:
                conn.execute(f"LISTEN {CHANNEL}")
                while True:
                    got = False
                    for _ in conn.notifies(timeout=HEARTBEAT):
                        got = True
                        break
                    # coalesce a burst, then respect the broadcast cap
                    dt = time.time() - last
                    if got and dt < MIN_INTERVAL:
                        time.sleep(MIN_INTERVAL - dt)
                    try:
                        for _ in conn.notifies(timeout=0):
                            pass
                    except Exception:
                        pass
                    try:
                        HUB.broadcast(json.dumps(fetch_stats(), default=str))
                        last = time.time()
                    except Exception as e:  # transient DB hiccup: keep the loop alive
                        HUB.broadcast(json.dumps({"error": str(e)}))
        except Exception:
            time.sleep(2.0)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # silence
        pass

    def do_GET(self):
        if self.path == "/" or self.path.startswith("/index"):
            self._send(200, "text/html; charset=utf-8", PAGE.encode())
        elif self.path == "/api/stats":
            self._send(200, "application/json", json.dumps(fetch_stats(), default=str).encode())
        elif self.path == "/events":
            self._sse()
        else:
            self._send(404, "text/plain", b"not found")

    def _send(self, code, ctype, body):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _sse(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        q = HUB.register()
        try:
            self.wfile.write(b"retry: 3000\n\n")
            self.wfile.write(b"data: " + json.dumps(fetch_stats(), default=str).encode() + b"\n\n")
            self.wfile.flush()
            while True:
                try:
                    payload = q.get(timeout=HEARTBEAT)
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    continue
                self.wfile.write(b"data: " + payload.encode() + b"\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            HUB.unregister(q)


PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>rust-noasync — crates.io survival survey</title>
<style>
:root{--bg:#f9f9f7;--surface:#fcfcfb;--ink:#0b0b0b;--sec:#52514e;--muted:#898781;
--grid:#e1e0d9;--accent:#2a78d6;--pass:#0ca30c;--direct:#d03b3b;--dep:#ec835a;--other:#fab219;--excl:#a9a8a2}
@media (prefers-color-scheme:dark){:root{--bg:#0d0d0d;--surface:#1a1a19;--ink:#fff;--sec:#c3c2b7;
--muted:#898781;--grid:#2c2c2a;--accent:#3987e5;--pass:#2fb62f;--direct:#e05555;--dep:#f0946f;--other:#f7c033;--excl:#6b6a65}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);
font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;line-height:1.45}
.wrap{max-width:1100px;margin:0 auto;padding:24px 20px 60px}
h1{font-size:20px;margin:0 0 2px}.sub{color:var(--muted);font-size:13px;margin-bottom:20px}
.card{background:var(--surface);border:1px solid var(--grid);border-radius:12px;padding:18px 20px;margin-bottom:16px}
.hero{text-align:center;padding:28px 20px}
.hero .big{font-size:56px;font-weight:650;line-height:1;letter-spacing:-1px}
.hero .ci{color:var(--sec);font-size:14px;margin-top:8px}
.track{height:8px;background:var(--grid);border-radius:5px;margin:14px auto 4px;max-width:520px;position:relative}
.band{position:absolute;height:100%;background:var(--accent);opacity:.35;border-radius:5px}
.point{position:absolute;width:3px;height:16px;top:-4px;background:var(--accent);border-radius:2px}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px}
.kpi{background:var(--surface);border:1px solid var(--grid);border-radius:10px;padding:14px 16px}
.kpi .v{font-size:26px;font-weight:620;font-variant-numeric:tabular-nums}
.kpi .l{color:var(--muted);font-size:12px;margin-top:2px}
.row{display:flex;align-items:center;gap:10px;margin:5px 0;font-size:14px}
.row .lab{width:150px;color:var(--sec);flex:none}
.bar{height:16px;border-radius:3px;background:var(--accent);min-width:2px}
.row .val{font-variant-numeric:tabular-nums;color:var(--sec);font-size:13px}
.stack{display:flex;height:22px;border-radius:5px;overflow:hidden;background:var(--grid);margin:6px 0 12px}
.seg{height:100%}
.chip{display:inline-block;width:10px;height:10px;border-radius:3px;margin-right:6px;vertical-align:middle}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{text-align:left;padding:4px 8px;border-bottom:1px solid var(--grid);vertical-align:top}
td.n{font-variant-numeric:tabular-nums;text-align:right}
pre{margin:2px 0 0;font-size:11px;color:var(--sec);white-space:pre-wrap;word-break:break-word;max-height:48px;overflow:hidden}
h2{font-size:14px;color:var(--sec);margin:0 0 10px;font-weight:600}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:16px}@media(max-width:760px){.grid2{grid-template-columns:1fr}}
.dot{color:var(--muted);font-size:12px}
</style></head><body><div class="wrap">
<h1>rust-noasync — crates.io survival survey</h1>
<div class="sub" id="sub">connecting…</div>

<div class="card hero">
  <div class="dot">estimated whole-registry survival (random sample, 95% CI)</div>
  <div class="big" id="hero">—</div>
  <div class="track" id="track"><div class="band" id="band"></div><div class="point" id="point"></div></div>
  <div class="ci" id="ci"></div>
</div>

<div class="kpis" id="kpis"></div>

<div class="card"><h2>overall outcome breakdown</h2>
  <div class="stack" id="stack"></div><table id="overall"></table></div>

<div class="grid2">
  <div class="card"><h2>survival by popularity</h2><div id="buckets"></div></div>
  <div class="card"><h2>top async culprits</h2><div id="blamed"></div></div>
</div>

<div class="card"><h2>running now</h2><table id="workers"></table></div>
<div class="card"><h2>recent failures</h2><table id="recent"></table></div>
</div>
<script>
const C={pass:'var(--pass)',pass_trivial:'var(--pass)',fail_async_direct:'var(--direct)',
fail_async_dep:'var(--dep)',fail_other:'var(--other)',excluded_broken:'var(--excl)',
excluded_resolve:'var(--excl)',excluded_resource:'var(--excl)',harness_error:'var(--excl)'};
const $=id=>document.getElementById(id);
function esc(s){const d=document.createElement('div');d.textContent=s==null?'':String(s);return d.innerHTML;}
function fmt(n){return n==null?'—':Number(n).toLocaleString();}
function render(d){
 if(d.error){$('sub').textContent='db: '+d.error;return;}
 const c=(d.counts||[{}])[0]||{}, h=(d.headline||[{}])[0]||{}, e=(d.estimate||[])[0];
 $('sub').textContent=`${fmt(c.done)} / ${fmt(c.total)} probed · updated ${new Date().toLocaleTimeString()}`;
 if(e){$('hero').textContent=e.p_hat+'%';$('ci').textContent=`${e.ci_lo}%–${e.ci_hi}% · n=${fmt(e.denom)} of the random sample`;
  $('band').style.left=e.ci_lo+'%';$('band').style.width=(e.ci_hi-e.ci_lo)+'%';$('point').style.left=e.p_hat+'%';}
 else{$('hero').textContent=(h.survival_pct!=null?h.survival_pct+'%':'—');$('ci').textContent='awaiting random-queue completions';}
 const rate=(c.done_15m||0)/15, eta=rate>0?Math.round((c.pending||0)/rate):null;
 const kp=[['probed',`${fmt(c.done)}/${fmt(c.total)}`],['probes/min',rate?rate.toFixed(1):'—'],
  ['ETA (min)',eta!=null?fmt(eta):'—'],['running',fmt(c.running)],
  ['fail_other (canary)',fmt(((d.overall||[]).find(r=>r.status==='fail_other')||{}).n||0)]];
 $('kpis').innerHTML=kp.map(([l,v])=>`<div class="kpi"><div class="v">${esc(v)}</div><div class="l">${esc(l)}</div></div>`).join('');
 const ov=d.overall||[],tot=ov.reduce((a,r)=>a+Number(r.n),0)||1;
 $('stack').innerHTML=ov.map(r=>`<div class="seg" style="width:${100*r.n/tot}%;background:${C[r.status]||'var(--excl)'}" title="${esc(r.status)}: ${r.n}"></div>`).join('');
 $('overall').innerHTML='<tr><th></th><th>status</th><th class="n">n</th><th class="n">%</th></tr>'+
  ov.map(r=>`<tr><td><span class="chip" style="background:${C[r.status]||'var(--excl)'}"></span></td><td>${esc(r.status)}</td><td class="n">${fmt(r.n)}</td><td class="n">${esc(r.pct)}</td></tr>`).join('');
 const bmax=100;
 $('buckets').innerHTML=(d.buckets||[]).map(b=>`<div class="row"><span class="lab">${esc(b.label)}</span><div class="bar" style="width:${(b.survival_pct||0)/bmax*260}px"></div><span class="val">${b.survival_pct==null?'—':b.survival_pct+'%'} (${fmt(b.passed)}/${fmt(b.denom)})</span></div>`).join('')||'<div class=dot>no data</div>';
 const bl=d.top_blamed||[],kmax=Math.max(1,...bl.map(r=>Number(r.kills)));
 $('blamed').innerHTML=bl.map(r=>`<div class="row"><span class="lab">${esc(r.culprit)}</span><div class="bar" style="width:${r.kills/kmax*220}px;background:var(--dep)"></div><span class="val">${fmt(r.kills)} <span class=dot>(${r.direct}d/${r.as_dependency}dep)</span></span></div>`).join('')||'<div class=dot>no async failures yet</div>';
 $('workers').innerHTML='<tr><th>worker</th><th>crate</th><th>q</th><th class="n">s</th></tr>'+
  (d.workers||[]).map(w=>`<tr><td>${esc(w.claimed_by)}</td><td>${esc(w.crate_name)} ${esc(w.version_num)}</td><td>${esc(w.claimed_via)}</td><td class="n">${fmt(w.running_s)}</td></tr>`).join('');
 $('recent').innerHTML='<tr><th>crate</th><th>status</th><th>blame</th><th>error</th></tr>'+
  (d.recent||[]).map(r=>`<tr><td>${esc(r.crate_name)} ${esc(r.version_num)}</td><td><span class="chip" style="background:${C[r.status]||'var(--excl)'}"></span>${esc(r.status)}</td><td>${esc(r.blamed_crate_name||'')}</td><td><pre>${esc(r.first_error||'')}</pre></td></tr>`).join('');
}
const es=new EventSource('/events');
es.onmessage=e=>{try{render(JSON.parse(e.data))}catch(x){}};
es.onerror=()=>{$('sub').textContent='reconnecting…';};
</script></body></html>"""


def main() -> None:
    threading.Thread(target=listener_loop, daemon=True).start()
    srv = ThreadingHTTPServer(ADDR, Handler)
    srv.daemon_threads = True
    print(f"dashboard on http://{ADDR[0]}:{ADDR[1]}")
    srv.serve_forever()


if __name__ == "__main__":
    main()
