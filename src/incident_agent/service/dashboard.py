# ruff: noqa: E501
"""One static page. It holds no secret: the token is typed by the viewer and kept only in this tab's memory."""

PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Incident agent</title><style>
:root{--bg:#fff;--fg:#14171a;--mut:#5b6670;--line:#d9dee3;--ok:#1a7f37;--warn:#9a6700;--bad:#b42318}
@media (prefers-color-scheme:dark){:root{--bg:#0f1317;--fg:#e8ecef;--mut:#98a3ad;--line:#2a323a;--ok:#4ac26b;--warn:#d4a72c;--bad:#f97066}}
body{font:15px/1.5 system-ui,sans-serif;background:var(--bg);color:var(--fg);margin:0 auto;max-width:960px;padding:16px}
h1{font-size:20px}input,button{font:inherit;padding:6px 10px;border:1px solid var(--line);border-radius:6px;background:var(--bg);color:var(--fg)}
table{width:100%;border-collapse:collapse;margin:12px 0}td,th{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line)}
.s-completed{color:var(--ok)}.s-failed{color:var(--bad)}.s-waiting,.s-running,.s-queued{color:var(--warn)}.mut{color:var(--mut)}
pre{white-space:pre-wrap;border:1px solid var(--line);padding:8px;border-radius:6px}
</style></head><body><h1>Incident agent <span class="mut" id="mode"></span></h1>
<p><input id="tok" type="password" placeholder="service token" autocomplete="off"> <button id="go">Load</button>
 <span class="mut" id="msg"></span></p>
<p><input id="inc" placeholder="INC-001 or LAB-001"> <button id="sub">Investigate</button></p>
<table><thead><tr><th>Run</th><th>Incident</th><th>Status</th></tr></thead><tbody id="rows"></tbody></table>
<h2>Detail</h2><pre id="detail" class="mut">Select a run.</pre>
<script>
let T="";const $=i=>document.getElementById(i);
async function api(p,o={}){o.headers=Object.assign({"Authorization":"Bearer "+T},o.headers||{});
 const r=await fetch(p,o);const j=await r.json().catch(()=>({}));if(!r.ok)throw new Error(j.error||r.status);return j}
function cell(t,c){const d=document.createElement("td");d.textContent=t==null?"":String(t);if(c)d.className=c;return d}
async function load(){try{const j=await api("/api/runs");$("mode").textContent="("+j.mode+" mode)";$("msg").textContent="";
 const b=$("rows");b.replaceChildren();for(const r of j.runs){const tr=document.createElement("tr");
 const a=cell(r.run_id);a.style.cursor="pointer";a.onclick=()=>show(r.run_id);tr.append(a,cell(r.incident_id),cell(r.status,"s-"+r.status));b.append(tr)}}
 catch(e){$("msg").textContent=e.message}}
async function show(id){try{$("detail").textContent=JSON.stringify(await api("/api/runs/"+encodeURIComponent(id)),null,2)}catch(e){$("detail").textContent=e.message}}
$("go").onclick=()=>{T=$("tok").value;load()};
$("sub").onclick=async()=>{try{await api("/api/incidents",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({incident_id:$("inc").value.trim()})});load()}catch(e){$("msg").textContent=e.message}};
setInterval(()=>{if(T)load()},5000);
</script></body></html>
"""
