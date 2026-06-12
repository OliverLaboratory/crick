"""A minimal block explorer UI, served by the node at "/".

It is a single self-contained page that polls the node's own JSON API
(/explorer/data on the same origin, so no CORS), rendering the chain summary,
the active problem and its best solution, and a table of recent blocks.
"""

EXPLORER_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>crick block explorer</title>
<style>
  :root { --bg:#0b0e14; --soft:#11151f; --card:#161b27; --line:#232a3a;
    --text:#dde3ee; --muted:#8b94a7; --accent:#4fd6a5; --blue:#6ea8fe;
    --mono:"SF Mono",ui-monospace,Menlo,Consolas,monospace; }
  *{box-sizing:border-box;margin:0;padding:0}
  body{background:var(--bg);color:var(--text);font:15px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",Inter,sans-serif}
  a{color:var(--blue);text-decoration:none} a:hover{text-decoration:underline}
  .wrap{max-width:1080px;margin:0 auto;padding:0 20px}
  header{display:flex;align-items:center;justify-content:space-between;padding:18px 0;border-bottom:1px solid var(--line);flex-wrap:wrap;gap:10px}
  .logo{font-family:var(--mono);font-weight:700;font-size:19px} .logo span{color:var(--accent)}
  header nav a{color:var(--muted);margin-left:18px;font-size:14px}
  .live{font-family:var(--mono);font-size:12px;color:var(--muted)}
  .live b{color:var(--accent)}
  h2{font-size:14px;text-transform:uppercase;letter-spacing:.07em;color:var(--muted);margin:28px 0 12px}
  .cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px}
  .card{background:var(--card);border:1px solid var(--line);border-radius:11px;padding:15px 16px}
  .card .k{font-size:12px;color:var(--muted);text-transform:uppercase;letter-spacing:.05em}
  .card .v{font-family:var(--mono);font-size:21px;margin-top:5px}
  .card .v.sm{font-size:15px}
  .panel{background:var(--card);border:1px solid var(--line);border-radius:11px;padding:16px 18px;margin-top:12px}
  .panel .pk{font-size:12px;color:var(--muted);text-transform:uppercase;letter-spacing:.05em}
  .panel .pv{margin-top:6px;font-size:15px}
  .panel .pv.mono{font-family:var(--mono);font-size:13px;color:var(--accent);word-break:break-all}
  table{width:100%;border-collapse:collapse;margin-top:6px;font-size:13.5px;display:block;overflow-x:auto}
  th,td{text-align:left;padding:9px 12px;border-bottom:1px solid var(--line);white-space:nowrap}
  th{color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.05em}
  td.mono{font-family:var(--mono)} td.hash{color:var(--blue)}
  .badge{display:inline-block;padding:2px 9px;border-radius:20px;font-size:11px;font-family:var(--mono);border:1px solid}
  .badge.sol{color:var(--accent);border-color:var(--accent);background:#122a22}
  .badge.cls{color:var(--muted);border-color:var(--line);background:var(--soft)}
  footer{color:var(--muted);font-size:12px;text-align:center;padding:30px 0;border-top:1px solid var(--line);margin-top:34px}
  .err{color:#f0a0a0;font-family:var(--mono);font-size:13px;padding:20px 0}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div class="logo">crick<span>⛓</span> explorer</div>
    <nav>
      <span class="live" id="live">connecting…</span>
      <a href="https://oliverlaboratory.com/crick/">About</a>
      <a href="https://oliverlaboratory.com/crick/protocol.html">Protocol</a>
    </nav>
  </header>

  <div id="err"></div>

  <h2>Chain</h2>
  <div class="cards" id="cards"></div>

  <h2>Active problem &amp; best solution</h2>
  <div class="panel">
    <div class="pk">Problem (epoch instance <span id="epoch">–</span>)</div>
    <div class="pv" id="problem">–</div>
    <div class="pk" style="margin-top:14px">Best solution on this instance</div>
    <div class="pv" id="best">–</div>
    <div class="pv mono" id="bestjson" style="margin-top:6px"></div>
  </div>

  <h2>Recent blocks</h2>
  <table>
    <thead><tr><th>#</th><th>type</th><th>difficulty</th><th>miner</th><th>time</th><th>hash</th></tr></thead>
    <tbody id="blocks"></tbody>
  </table>

  <footer>crick — research prototype block explorer · auto-refreshes every 4s</footer>
</div>

<script>
const $ = id => document.getElementById(id);
const short = s => s ? s.slice(0,10)+'…'+s.slice(-6) : '–';
const fmtTime = t => { const d=new Date(t*1000); return d.toLocaleTimeString()+' '+d.toLocaleDateString(); };
const card = (k,v,sm) => `<div class="card"><div class="k">${k}</div><div class="v ${sm?'sm':''}">${v}</div></div>`;

async function refresh(){
  try{
    const r = await fetch('/explorer/data?limit=30',{cache:'no-store'});
    if(!r.ok) throw new Error('HTTP '+r.status);
    const d = await r.json();
    $('err').innerHTML='';
    const s = d.summary;
    $('live').innerHTML = 'height <b>'+s.height+'</b> · live';
    $('cards').innerHTML =
      card('Height', s.height) +
      card('Best score', s.best_score) +
      card('d_b (base)', Math.round(s.d_b)) +
      card('d_r (reduced)', Math.round(s.d_r)) +
      card('Total work', Math.round(s.total_work)) +
      card('Peers', (s.peers||[]).length);
    $('epoch').textContent = '#'+d.epoch_index;
    $('problem').textContent = d.problem;
    $('best').textContent = d.best;
    $('bestjson').textContent = d.best_solution ? JSON.stringify(d.best_solution) : '';
    $('blocks').innerHTML = d.blocks.slice().reverse().map(b =>
      '<tr>'+
      '<td class="mono">'+b.height+'</td>'+
      '<td>'+(b.has_solution?'<span class="badge sol">solution</span>':'<span class="badge cls">classical</span>')+'</td>'+
      '<td class="mono">'+Math.round(b.difficulty)+'</td>'+
      '<td class="mono">'+short(b.miner)+'</td>'+
      '<td>'+fmtTime(b.timestamp)+'</td>'+
      '<td class="mono hash">'+short(b.hash)+'</td>'+
      '</tr>').join('');
  }catch(e){
    $('live').textContent='disconnected';
    $('err').innerHTML='<div class="err">Could not reach the node: '+e.message+'</div>';
  }
}
refresh(); setInterval(refresh, 4000);
</script>
</body>
</html>"""
