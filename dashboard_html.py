"""网关 Web 页面的静态 HTML（登录页 + 仪表盘）。

之所以独立成文件：页面的 HTML/CSS/JS 比较长，和 gateway_host.py 的业务逻辑分开更好维护。
占位符 ``__TOKEN__`` 由 gateway_host.py 用 ``json.dumps(token)`` 替换成 JS 字符串字面量，
因此页面里的 token 不会因特殊字符破坏脚本。
"""

LOGIN_HTML = """<!DOCTYPE html><html lang="zh"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Gateway 登录</title><style>body{font-family:system-ui,sans-serif;max-width:400px;margin:4rem auto;padding:0 1rem;background:#f5f5f5}h1{text-align:center}input{width:100%;padding:10px;margin:8px 0;border:1px solid #ddd;border-radius:6px;font-size:15px;box-sizing:border-box}button{width:100%;padding:10px;background:#3b82f6;color:#fff;border:none;border-radius:6px;font-size:15px;cursor:pointer}button:hover{background:#2563eb}</style></head><body><h1>Log MCP Gateway</h1><p>请输入 API Token（即 local.token 内容）：</p><form id="f"><input type="password" id="tk" placeholder="Bearer Token" autofocus><button type="submit">进入仪表盘</button></form><script>document.getElementById('f').addEventListener('submit',function(e){e.preventDefault();window.location.href='/dashboard?token='+encodeURIComponent(document.getElementById('tk').value)})</script></body></html>"""

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="zh"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Gateway 仪表盘</title>
<style>
body{font-family:system-ui,sans-serif;max-width:1080px;margin:1.5rem auto;padding:0 1rem;background:#f5f5f5;color:#111}
table{width:100%;border-collapse:collapse;background:#fff;border-radius:8px;overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,.1)}
th,td{padding:9px 12px;text-align:left;border-bottom:1px solid #eee;font-size:14px;vertical-align:middle}
th{background:#f8f9fa;font-weight:600}
tr:last-child td{border-bottom:none}
.connected{color:#22c55e}.disconnected{color:#ef4444}.connecting{color:#f59e0b}.needs_reauth{color:#f97316}
button{padding:5px 12px;border:none;border-radius:4px;cursor:pointer;font-size:13px}
.btn-connect{background:#3b82f6;color:#fff}.btn-connect:hover{background:#2563eb}
.btn-cancel{background:#e5e7eb;color:#333}.btn-cancel:hover{background:#d1d5db}
.btn-danger{background:#fee2e2;color:#b91c1c}.btn-danger:hover{background:#fecaca}
.form-row{display:flex;gap:8px;align-items:center;margin:8px 0;flex-wrap:wrap}
input{padding:6px 10px;border:1px solid #ddd;border-radius:4px;font-size:14px}
.hidden{display:none}
#err{background:#fef2f2;color:#dc2626;padding:10px;border-radius:6px;margin-bottom:1rem}
#msg{background:#f0fdf4;color:#15803d;padding:10px;border-radius:6px;margin-bottom:1rem}
.header{display:flex;justify-content:space-between;align-items:center;margin-bottom:1rem}
h1{margin:0;font-size:20px}h2{margin:2rem 0 .5rem;font-size:17px}
.note{color:#888;font-size:13px;line-height:1.7}
code{background:#f3f4f6;padding:1px 5px;border-radius:3px;font-size:13px}
.tag{display:inline-block;padding:1px 7px;border-radius:10px;font-size:12px;background:#eef2ff;color:#4338ca}
.v-allowed{color:#16a34a;font-weight:600}.v-rejected{color:#dc2626;font-weight:600}.v-unknown{color:#d97706;font-weight:600}
</style></head><body>
<div class="header"><h1>Gateway 仪表盘</h1><span class="note">资产状态每 10s 自动刷新</span></div>
<div id="err" class="hidden"></div>
<div id="msg" class="hidden"></div>

<table><thead><tr><th>资产名</th><th>状态</th><th>最后心跳</th><th>断开原因</th><th>操作</th></tr></thead><tbody id="tb"></tbody></table>

<h2>自定义命令白名单 <span class="tag" id="al-count"></span></h2>
<p class="note">
当 <code>execute_system_query</code> 提示 <b>UNKNOWN</b> 时，说明该命令不在网关内置白/黑名单里。
在这里登记命令前缀即可放行，<b>保存后下一次请求立即生效，无需重启 gateway</b>。<br>
匹配规则：按 token 前缀匹配（登记 <code>docker compose ls</code> 只放行以这三个 token 开头的命令）。
黑名单命令（<code>rm</code>、<code>sudo</code>、<code>docker rm</code> 等）与“禁止重定向/串联/命令替换”等通用规则仍然优先，无法通过这里绕过。
</p>
<div class="form-row">
  <input id="in-name" placeholder="命令，如 htop / ifconfig / docker compose ls" style="width:320px">
  <input id="in-denied" placeholder="禁用的 flag（可选，逗号分隔，如 -d,--delay）" style="width:280px">
  <input id="in-note" placeholder="备注（可选）" style="width:180px">
  <button class="btn-connect" onclick="addEntry()">添加 / 更新</button>
</div>
<div class="form-row">
  <input id="in-test" placeholder="试运行校验：输入一条完整命令，看网关会怎么判定" style="width:420px">
  <button class="btn-cancel" onclick="checkCmd()">校验</button>
  <span id="test-out" class="note"></span>
</div>
<table><thead><tr><th>命令</th><th>禁用的 flag</th><th>备注</th><th>添加时间</th><th>操作</th></tr></thead><tbody id="alb"></tbody></table>
<p class="note" id="al-path"></p>

<script>
var T=__TOKEN__;
function H(extra){var h={Authorization:'Bearer '+T};if(extra){for(var k in extra)h[k]=extra[k];}return h;}
function E(m){var e=document.getElementById('err');e.textContent=m;e.classList.remove('hidden');setTimeout(function(){e.classList.add('hidden');},6000);}
function OK(m){var e=document.getElementById('msg');e.textContent=m;e.classList.remove('hidden');setTimeout(function(){e.classList.add('hidden');},4000);}
function el(tag,text,cls){var n=document.createElement(tag);if(text!==undefined&&text!==null)n.textContent=text;if(cls)n.className=cls;return n;}
function lookup(o,s){var m={connected:'\\u2705 已连接',disconnected:'\\u274e 断连',connecting:'\\u23f3 认证中',needs_reauth:'\\u26a0\\ufe0f 需重认证'};return m[s]||s;}

/* ── 资产连接状态 ───────────────────────────── */
var openForm=null;
function renderConnections(cs){
  var tb=document.getElementById('tb');tb.textContent='';
  cs.forEach(function(c){
    var tr=el('tr');tr.id='row-'+c.server;
    tr.appendChild(el('td',c.server));
    tr.appendChild(el('td',lookup(c.state),c.state));
    tr.appendChild(el('td',c.last_heartbeat||'\\u2014'));
    tr.appendChild(el('td',c.disconnect_reason||'\\u2014'));
    var td=el('td');
    if(c.state!=='connecting'){
      var b=el('button','\\u8fde\\u63a5','btn-connect');
      b.onclick=function(){showForm(c.server);};
      td.appendChild(b);
    } else { td.appendChild(el('span','\\u8ba4\\u8bc1\\u4e2d...','note')); }
    tr.appendChild(td);tb.appendChild(tr);

    var ftr=el('tr');ftr.id='fm-'+c.server;ftr.className='hidden';
    var ftd=el('td');ftd.colSpan=5;
    var row=el('div',null,'form-row');
    var pw=el('input');pw.type='password';pw.id='pw-'+c.server;pw.placeholder='JumpServer 密码';pw.style.width='180px';
    var mf=el('input');mf.type='text';mf.id='mf-'+c.server;mf.placeholder='MFA 验证码';mf.style.width='120px';
    var ok=el('button','\\u786e\\u8ba4\\u8fde\\u63a5','btn-connect');ok.onclick=function(){doConnect(c.server);};
    var no=el('button','\\u53d6\\u6d88','btn-cancel');no.onclick=function(){hideForm(c.server);};
    row.appendChild(pw);row.appendChild(mf);row.appendChild(ok);row.appendChild(no);
    ftd.appendChild(row);ftr.appendChild(ftd);tb.appendChild(ftr);
  });
  if(openForm){var f=document.getElementById('fm-'+openForm);if(f)f.classList.remove('hidden');}
}
async function loadConnections(){
  try{
    var r=await fetch('/dashboard/status',{headers:H()});
    if(!r.ok)throw new Error(r.status+' '+(((await r.json()).detail)||''));
    var d=await r.json();
    /* 有展开中的密码表单时不重绘，避免输入被清空 */
    if(document.querySelector('[id^=fm-]:not(.hidden)'))return;
    renderConnections(d.connections);
  }catch(e){E(e.message);}
}
function showForm(s){document.querySelectorAll('[id^=fm-]').forEach(function(e){e.classList.add('hidden');});openForm=s;document.getElementById('fm-'+s).classList.remove('hidden');}
function hideForm(s){openForm=null;document.getElementById('fm-'+s).classList.add('hidden');}
async function doConnect(s){
  var p=document.getElementById('pw-'+s).value,m=document.getElementById('mf-'+s).value;
  if(!p){E('\\u8bf7\\u8f93\\u5165\\u5bc6\\u7801');return;}
  try{
    var r=await fetch('/dashboard/connect',{method:'POST',headers:H({'Content-Type':'application/json'}),body:JSON.stringify({server:s,password:p,mfa_code:m})});
    var d=await r.json();if(!r.ok)throw new Error(d.detail||'\\u8fde\\u63a5\\u5931\\u8d25');
    hideForm(s);await loadConnections();
  }catch(e){E(e.message);}
}

/* ── 自定义命令白名单 ───────────────────────── */
function renderAllowlist(entries){
  var tb=document.getElementById('alb');tb.textContent='';
  document.getElementById('al-count').textContent='共 '+entries.length+' 条';
  if(!entries.length){
    var tr=el('tr'),td=el('td','\\u6682\\u65e0\\u81ea\\u5b9a\\u4e49\\u767d\\u540d\\u5355\\uff0c\\u4e0a\\u65b9\\u8f93\\u5165\\u547d\\u4ee4\\u540e\\u70b9\\u300c\\u6dfb\\u52a0\\u300d\\u3002','note');
    td.colSpan=5;tr.appendChild(td);tb.appendChild(tr);return;
  }
  entries.forEach(function(e){
    var tr=el('tr');
    tr.appendChild(el('td',e.name));
    tr.appendChild(el('td',(e.denied&&e.denied.length)?e.denied.join(', '):'\\u2014'));
    tr.appendChild(el('td',e.note||'\\u2014'));
    tr.appendChild(el('td',e.added_at||'\\u2014'));
    var td=el('td'),b=el('button','\\u5220\\u9664','btn-danger');
    b.onclick=function(){delEntry(e.name);};
    td.appendChild(b);tr.appendChild(td);tb.appendChild(tr);
  });
}
async function loadAllowlist(){
  try{
    var r=await fetch('/dashboard/allowlist',{headers:H()});
    var d=await r.json();
    if(!r.ok)throw new Error(d.detail||'\\u52a0\\u8f7d\\u5931\\u8d25');
    renderAllowlist(d.entries||[]);
    document.getElementById('al-path').textContent='配置文件：'+(d.path||'')+(d.warnings&&d.warnings.length?('　⚠ '+d.warnings.join('; ')):'');
  }catch(e){E(e.message);}
}
async function addEntry(){
  var name=document.getElementById('in-name').value.trim();
  if(!name){E('\\u8bf7\\u8f93\\u5165\\u547d\\u4ee4');return;}
  var denied=document.getElementById('in-denied').value.split(',').map(function(s){return s.trim();}).filter(Boolean);
  var note=document.getElementById('in-note').value.trim();
  try{
    var r=await fetch('/dashboard/allowlist',{method:'POST',headers:H({'Content-Type':'application/json'}),body:JSON.stringify({name:name,note:note,denied:denied})});
    var d=await r.json();if(!r.ok)throw new Error(d.detail||'\\u4fdd\\u5b58\\u5931\\u8d25');
    document.getElementById('in-name').value='';document.getElementById('in-denied').value='';document.getElementById('in-note').value='';
    renderAllowlist(d.entries||[]);OK('\\u5df2\\u4fdd\\u5b58\\uff0c\\u7acb\\u5373\\u751f\\u6548: '+name);
  }catch(e){E(e.message);}
}
async function delEntry(name){
  if(!confirm('\\u5220\\u9664\\u767d\\u540d\\u5355\\u9879\\u300c'+name+'\\u300d\\uff1f\\u5220\\u9664\\u540e\\u8be5\\u547d\\u4ee4\\u4f1a\\u91cd\\u65b0\\u8fd4\\u56de UNKNOWN\\u3002'))return;
  try{
    var r=await fetch('/dashboard/allowlist?name='+encodeURIComponent(name),{method:'DELETE',headers:H()});
    var d=await r.json();if(!r.ok)throw new Error(d.detail||'\\u5220\\u9664\\u5931\\u8d25');
    renderAllowlist(d.entries||[]);OK('\\u5df2\\u5220\\u9664: '+name);
  }catch(e){E(e.message);}
}
async function checkCmd(){
  var cmd=document.getElementById('in-test').value.trim();
  var out=document.getElementById('test-out');
  if(!cmd){out.textContent='';return;}
  try{
    var r=await fetch('/dashboard/allowlist/check',{method:'POST',headers:H({'Content-Type':'application/json'}),body:JSON.stringify({command:cmd})});
    var d=await r.json();
    if(!r.ok)throw new Error(d.detail||'\\u6821\\u9a8c\\u5931\\u8d25');
    out.textContent=d.verdict.toUpperCase()+' — '+(d.message||'\\u53ef\\u4ee5\\u6267\\u884c');
    out.className='v-'+d.verdict;
  }catch(e){out.textContent=e.message;out.className='v-rejected';}
}

loadConnections();
setInterval(loadConnections,10000);
loadAllowlist();
</script>
</body></html>"""
