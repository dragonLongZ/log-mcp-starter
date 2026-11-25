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

<h2>资产配置 <span class="tag" id="tg-count"></span></h2>
<p class="note">
  资产与模块保存在网关的 SQLite 数据库里（<code id="tg-db"></code>），<b>保存后立即生效、无需重启</b>：
  新资产马上出现在上表并可点「连接」，已连接资产只改日志目录不会踢掉会话。<br>
  「以现有资产为模板」会把该资产的 <code>ssh_username</code> 与全部模块一起填进表单，
  同一套模块的机器只需要改资产名和用户名。
</p>
<div id="tg-msg" class="hidden"></div>
<div class="form-row">
  <input id="tg-name" placeholder="资产名，如 server-10.0.0.11-order-api" style="width:300px">
  <input id="tg-env" placeholder="环境，如 生产 / 测试 / 研发" style="width:160px">
  <select id="tg-kind" onchange="kindChanged()" style="padding:6px 10px;border:1px solid #ddd;border-radius:4px;font-size:14px">
    <option value="linux">Linux 资产</option>
    <option value="k8s">K8S 资产</option>
  </select>
  <select id="tg-copy" onchange="copyFrom(this.value)" style="padding:6px 10px;border:1px solid #ddd;border-radius:4px;font-size:14px">
    <option value="">以现有资产为模板…</option>
  </select>
</div>
<div class="form-row">
  <input id="tg-desc" placeholder="说明（模型用描述判断业务归属，写清楚一点）" style="width:640px">
</div>
<div class="form-row">
  <input id="tg-user" placeholder="ssh_username，如 user#root#10.0.0.11" style="width:340px">
</div>
<div class="form-row" id="tg-k8s-row" style="display:none">
  <input id="tg-k8s-ctx" placeholder="k8s context（仅展示）" style="width:200px">
  <input id="tg-k8s-ns" placeholder="常用命名空间，逗号分隔（可选）" style="width:340px">
</div>
<div id="tg-modules"></div>
<div class="form-row">
  <button class="btn-cancel" onclick="addModuleRow()">+ 添加模块</button>
  <button class="btn-cancel" onclick="validateTarget()">试运行校验</button>
  <button class="btn-connect" onclick="saveTarget()">保存资产</button>
  <button class="btn-cancel" onclick="resetTargetForm()">清空表单</button>
  <span id="tg-valid" class="note"></span>
</div>
<table><thead><tr><th>资产名</th><th>环境</th><th>类型</th><th>ssh_username</th><th>模块</th><th>状态</th><th>操作</th></tr></thead><tbody id="tgb"></tbody></table>

<details style="margin-top:1rem">
  <summary class="note" style="cursor:pointer">批量导入 / 导出（迁移、备份、把配置回灌到 example 文件）</summary>
  <p class="note">导入内容支持 YAML（含 <code>targets:</code> 段）或 JSON；同一个资产名会整条覆盖。先「试运行」确认没有条目被拒。</p>
  <textarea id="tg-import" placeholder="把 log_targets.yaml 的内容粘贴到这里…" style="width:100%;height:160px;padding:8px;border:1px solid #ddd;border-radius:6px;font-family:ui-monospace,monospace;font-size:13px;box-sizing:border-box"></textarea>
  <div class="form-row">
    <button class="btn-cancel" onclick="importTargets(true)">试运行</button>
    <button class="btn-connect" onclick="importTargets(false)">导入</button>
    <a id="tg-export" class="note" href="#" style="margin-left:8px">导出当前完整配置</a>
    <span id="tg-import-out" class="note"></span>
  </div>
  <pre id="tg-import-detail" class="note" style="white-space:pre-wrap"></pre>
</details>

<h2>自定义命令白名单 <span class="tag" id="al-count"></span></h2>
<p class="note">
当 <code>execute_system_query</code> 提示 <b>UNKNOWN</b> 时，说明该命令不在网关内置白/黑名单里。
在这里登记命令前缀即可放行，<b>保存后下一次请求立即生效，无需重启 gateway</b>。<br>
匹配规则：按 token 前缀匹配（登记 <code>docker compose ls</code> 只放行以这三个 token 开头的命令）。
黑名单命令（<code>rm</code>、<code>sudo</code>、<code>docker rm</code> 等）与“禁止重定向/串联/命令替换”等通用规则仍然优先，无法通过这里绕过。
</p>
<div class="form-row">
  <input id="in-name" placeholder="命令，如 jcmd / htop / docker compose ls" style="width:320px">
  <input id="in-denied" placeholder="禁用参数（可选，逗号分隔，如 VM.set_flag,GC.run 或 -d,--delay" style="width:320px">
  <input id="in-note" placeholder="备注（可选）" style="width:180px">
  <button class="btn-connect" onclick="addEntry()">添加 / 更新</button>
</div>
<div id="al-msg" class="hidden"></div>
<div class="form-row">
  <input id="in-test" placeholder="试运行校验：输入一条完整命令，看网关会怎么判定" style="width:420px">
  <button class="btn-cancel" onclick="checkCmd()">校验</button>
  <span id="test-out" class="note"></span>
</div>
<table><thead><tr><th>命令</th><th>禁用参数</th><th>备注</th><th>添加时间</th><th>操作</th></tr></thead><tbody id="alb"></tbody></table>
<p class="note" id="al-path"></p>

<script>
var T=__TOKEN__;
function H(extra){var h={Authorization:'Bearer '+T};if(extra){for(var k in extra)h[k]=extra[k];}return h;}
function E(m){var e=document.getElementById('err');e.textContent=m;e.classList.remove('hidden');setTimeout(function(){e.classList.add('hidden');},6000);}
function OK(m){var e=document.getElementById('msg');e.textContent=m;e.classList.remove('hidden');setTimeout(function(){e.classList.add('hidden');},4000);}
/* 白名单面板就地提示：页面滚动到底部时也能看到结果，不会“点了没反应”。 */
function AE(m,ok){
  var e=document.getElementById('al-msg');
  e.textContent=m;
  e.style.cssText=ok
    ?'background:#f0fdf4;color:#15803d;padding:10px;border-radius:6px;margin:8px 0'
    :'background:#fef2f2;color:#dc2626;padding:10px;border-radius:6px;margin:8px 0';
  e.classList.remove('hidden');
  clearTimeout(e._t);
  e._t=setTimeout(function(){e.classList.add('hidden');},8000);
}
function el(tag,text,cls){var n=document.createElement(tag);if(text!==undefined&&text!==null)n.textContent=text;if(cls)n.className=cls;return n;}
function lookup(s){var m={connected:'\\u2705 已连接',disconnected:'\\u274e 断连',connecting:'\\u23f3 认证中',needs_reauth:'\\u26a0\\ufe0f 需重认证'};return m[s]||s||'\\u2014';}

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

/* ── 资产配置 ───────────────────────────────── */
var targetCache=[];
function TG(m,ok){
  var e=document.getElementById('tg-msg');
  e.textContent=m;
  e.style.cssText=ok
    ?'background:#f0fdf4;color:#15803d;padding:10px;border-radius:6px;margin:8px 0'
    :'background:#fef2f2;color:#dc2626;padding:10px;border-radius:6px;margin:8px 0';
  e.classList.remove('hidden');
  clearTimeout(e._t);
  e._t=setTimeout(function(){e.classList.add('hidden');},9000);
}
function kindChanged(){
  var k=document.getElementById('tg-kind').value;
  document.getElementById('tg-k8s-row').style.display=k==='k8s'?'flex':'none';
}
function addModuleRow(name,dir,desc){
  var box=document.getElementById('tg-modules');
  var row=el('div',null,'form-row tg-mod');
  var n=el('input');n.className='m-name';n.placeholder='模块名';n.style.width='180px';n.value=name||'';
  var d=el('input');d.className='m-dir';d.placeholder='日志目录，如 /data/logs/xxx';d.style.width='320px';d.value=dir||'';
  var s=el('input');s.className='m-desc';s.placeholder='模块说明';s.style.width='260px';s.value=desc||'';
  var b=el('button','\\u5220\\u9664','btn-danger');
  b.onclick=function(){row.remove();};
  row.appendChild(n);row.appendChild(d);row.appendChild(s);row.appendChild(b);
  box.appendChild(row);
}
function clearModules(){document.getElementById('tg-modules').textContent='';}
function readModules(){
  var out={},mods=document.querySelectorAll('#tg-modules .tg-mod');
  var dup=false;
  mods.forEach(function(row){
    var n=row.querySelector('.m-name').value.trim();
    var d=row.querySelector('.m-dir').value.trim();
    var s=row.querySelector('.m-desc').value.trim();
    if(!n&&!d)return;
    if(out[n])dup=true;
    out[n]={log_dir:d,description:s};
  });
  if(dup)throw new Error('\\u6a21\\u5757\\u540d\\u91cd\\u590d');
  return out;
}
function resetTargetForm(){
  ['tg-name','tg-env','tg-desc','tg-user','tg-k8s-ctx','tg-k8s-ns'].forEach(function(id){
    document.getElementById(id).value='';
  });
  document.getElementById('tg-kind').value='linux';
  document.getElementById('tg-copy').value='';
  kindChanged();clearModules();
  document.getElementById('tg-valid').textContent='';
}
function copyFrom(name){
  var t=null;
  targetCache.forEach(function(item){if(item.name===name)t=item;});
  if(!t)return;
  document.getElementById('tg-env').value=t.env||'';
  document.getElementById('tg-desc').value=t.description||'';
  document.getElementById('tg-user').value=t.ssh_username||'';
  document.getElementById('tg-kind').value=t.kind||'linux';
  kindChanged();
  clearModules();
  Object.keys(t.modules||{}).forEach(function(m){
    addModuleRow(m,t.modules[m].log_dir,t.modules[m].description);
  });
  if(t.k8s){
    document.getElementById('tg-k8s-ctx').value=t.k8s.context||'';
    document.getElementById('tg-k8s-ns').value=(t.k8s.namespaces||[]).join(',');
  }
  TG('\\u5df2\\u4ee5 '+name+' \\u4e3a\\u6a21\\u677f\\u586b\\u5165\\uff0c\\u8bb0\\u5f97\\u6539\\u8d44\\u4ea7\\u540d\\u4e0e ssh_username',true);
}
function targetBody(){
  var k=document.getElementById('tg-kind').value;
  return {
    name:document.getElementById('tg-name').value.trim(),
    env:document.getElementById('tg-env').value.trim(),
    description:document.getElementById('tg-desc').value.trim(),
    kind:k,
    ssh_username:document.getElementById('tg-user').value.trim(),
    modules:readModules(),
    k8s_context:k==='k8s'?document.getElementById('tg-k8s-ctx').value.trim():'',
    k8s_namespaces:k==='k8s'
      ? document.getElementById('tg-k8s-ns').value.split(',').map(function(s){return s.trim();}).filter(Boolean)
      : []
  };
}
async function validateTarget(){
  var b;
  try{b=targetBody();}catch(e){TG(e.message,false);return;}
  var out=document.getElementById('tg-valid');
  try{
    var r=await fetch('/dashboard/targets/validate',{method:'POST',headers:H({'Content-Type':'application/json'}),body:JSON.stringify(b)});
    var d=await r.json();
    if(d.valid){out.textContent='\\u2705 \\u6821\\u9a8c\\u901a\\u8fc7';out.className='note v-allowed';}
    else{out.textContent='\\u274c '+d.error;out.className='note v-rejected';}
  }catch(e){out.textContent=e.message;out.className='note v-rejected';}
}
async function saveTarget(){
  var b;
  try{b=targetBody();}catch(e){TG(e.message,false);return;}
  if(!b.name){TG('\\u8bf7\\u586b\\u8d44\\u4ea7\\u540d',false);return;}
  try{
    var r=await fetch('/dashboard/targets',{method:'POST',headers:H({'Content-Type':'application/json'}),body:JSON.stringify(b)});
    var d=await r.json();
    if(!r.ok)throw new Error(d.detail||'\\u4fdd\\u5b58\\u5931\\u8d25');
    resetTargetForm();renderTargets(d);await loadConnections();
    TG('\\u5df2\\u4fdd\\u5b58 '+b.name+'\\uff0c\\u5df2\\u751f\\u6548\\uff1a\\u53ef\\u76f4\\u63a5\\u5728\\u4e0a\\u8868\\u70b9\\u300c\\u8fde\\u63a5\\u300d',true);
  }catch(e){TG('\\u4fdd\\u5b58\\u5931\\u8d25\\uff1a'+e.message,false);}
}
function editTarget(name){
  targetCache.forEach(function(t){
    if(t.name!==name)return;
    document.getElementById('tg-name').value=t.name;
    document.getElementById('tg-env').value=t.env||'';
    document.getElementById('tg-desc').value=t.description||'';
    document.getElementById('tg-user').value=t.ssh_username||'';
    document.getElementById('tg-kind').value=t.kind||'linux';
    kindChanged();clearModules();
    Object.keys(t.modules||{}).forEach(function(m){
      addModuleRow(m,t.modules[m].log_dir,t.modules[m].description);
    });
    if(t.k8s){
      document.getElementById('tg-k8s-ctx').value=t.k8s.context||'';
      document.getElementById('tg-k8s-ns').value=(t.k8s.namespaces||[]).join(',');
    }
    window.scrollTo(0,0);
  });
}
async function deleteTarget(name){
  if(!confirm('\\u5220\\u9664\\u8d44\\u4ea7\\u300c'+name+'\\u300d\\uff1f\\u5176 SSH \\u8fde\\u63a5\\u4f1a\\u88ab\\u5173\\u95ed\\uff0c\\u6a21\\u5757\\u914d\\u7f6e\\u4e00\\u5e76\\u5220\\u9664\\u3002'))return;
  try{
    var r=await fetch('/dashboard/targets?name='+encodeURIComponent(name),{method:'DELETE',headers:H()});
    var d=await r.json();
    if(!r.ok)throw new Error(d.detail||'\\u5220\\u9664\\u5931\\u8d25');
    renderTargets(d);await loadConnections();TG('\\u5df2\\u5220\\u9664\\uff1a'+name,true);
  }catch(e){TG('\\u5220\\u9664\\u5931\\u8d25\\uff1a'+e.message,false);}
}
async function toggleTarget(name,enabled){
  try{
    var r=await fetch('/dashboard/targets/enabled',{method:'POST',headers:H({'Content-Type':'application/json'}),body:JSON.stringify({name:name,enabled:enabled})});
    var d=await r.json();
    if(!r.ok)throw new Error(d.detail||'\\u64cd\\u4f5c\\u5931\\u8d25');
    renderTargets(d);await loadConnections();
    TG((enabled?'\\u5df2\\u542f\\u7528\\uff1a':'\\u5df2\\u505c\\u7528\\uff1a')+name,true);
  }catch(e){TG(e.message,false);}
}
function renderTargets(d){
  targetCache=d.targets||[];
  document.getElementById('tg-count').textContent='共 '+targetCache.length+' 个资产';
  document.getElementById('tg-db').textContent=d.db_path||'';
  document.getElementById('tg-export').href='/dashboard/targets/export?token='+encodeURIComponent(T);
  var sel=document.getElementById('tg-copy');
  var cur=sel.value;
  sel.textContent='';
  sel.appendChild(el('option','\\u4ee5\\u73b0\\u6709\\u8d44\\u4ea7\\u4e3a\\u6a21\\u677f\\u2026'));
  sel.firstChild.value='';
  targetCache.forEach(function(t){
    var o=el('option',t.name);o.value=t.name;sel.appendChild(o);
  });
  sel.value=cur||'';

  var tb=document.getElementById('tgb');tb.textContent='';
  if(!targetCache.length){
    var tr0=el('tr'),td0=el('td','\\u6682\\u65e0\\u8d44\\u4ea7\\uff0c\\u4e0a\\u65b9\\u586b\\u4e00\\u4e2a\\u540e\\u70b9\\u300c\\u4fdd\\u5b58\\u8d44\\u4ea7\\u300d\\u3002','note');
    td0.colSpan=7;tr0.appendChild(td0);tb.appendChild(tr0);return;
  }
  targetCache.forEach(function(t){
    var tr=el('tr');
    tr.appendChild(el('td',t.name));
    tr.appendChild(el('td',t.env||'\\u2014'));
    tr.appendChild(el('td',t.kind==='k8s'?'K8S':'Linux'));
    tr.appendChild(el('td',t.ssh_username||'\\u2014'));
    var mods=Object.keys(t.modules||{});
    tr.appendChild(el('td',mods.length?mods.join(', '):'\\u2014'));
    var st=t.enabled?(t.connection?lookup(t.connection.state):'\\u672a\\u8fde\\u63a5'):'\\u5df2\\u505c\\u7528';
    tr.appendChild(el('td',st,t.enabled?(t.connection?t.connection.state:''):'disconnected'));
    var td=el('td');
    var eb=el('button','\\u7f16\\u8f91','btn-cancel');eb.onclick=function(){editTarget(t.name);};
    td.appendChild(eb);
    var tb2=el('button',t.enabled?'\\u505c\\u7528':'\\u542f\\u7528','btn-cancel');
    tb2.onclick=function(){toggleTarget(t.name,!t.enabled);};
    td.appendChild(tb2);
    var db2=el('button','\\u5220\\u9664','btn-danger');db2.onclick=function(){deleteTarget(t.name);};
    td.appendChild(db2);
    tr.appendChild(td);tb.appendChild(tr);
  });
}
async function loadTargets(){
  try{
    var r=await fetch('/dashboard/targets',{headers:H()});
    var d=await r.json();
    if(!r.ok)throw new Error(d.detail||'\\u52a0\\u8f7d\\u5931\\u8d25');
    renderTargets(d);
    if(d.warnings&&d.warnings.length)TG('\\u914d\\u7f6e\\u544a\\u8b66\\uff1a'+d.warnings.join('\\uff1b'),false);
  }catch(e){TG('\\u52a0\\u8f7d\\u8d44\\u4ea7\\u5931\\u8d25\\uff1a'+e.message,false);}
}
async function importTargets(dry){
  var content=document.getElementById('tg-import').value;
  var out=document.getElementById('tg-import-out');
  var detail=document.getElementById('tg-import-detail');
  if(!content.trim()){out.textContent='\\u8bf7\\u5148\\u7c98\\u8d34\\u5185\\u5bb9';return;}
  try{
    var r=await fetch('/dashboard/targets/import',{method:'POST',headers:H({'Content-Type':'application/json'}),body:JSON.stringify({content:content,dry_run:!!dry})});
    var d=await r.json();
    if(!r.ok)throw new Error(d.detail||'\\u5bfc\\u5165\\u5931\\u8d25');
    out.textContent=(dry?'\\u8bd5\\u8fd0\\u884c\\uff1a':'\\u5df2\\u5bfc\\u5165\\uff1a')+'\\u63a5\\u53d7 '+d.accepted.length+' \\u6761\\uff0c\\u62d2\\u7edd '+d.rejected.length+' \\u6761';
    detail.textContent=(d.rejected||[]).map(function(x){return '\\u2717 '+x.name+'\\uff1a'+x.error;}).join('\\n');
    if(!dry){await loadTargets();await loadConnections();}
  }catch(e){out.textContent=e.message;detail.textContent='';}
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
    document.getElementById('al-path').textContent='存储位置（与资产同库）：'+(d.path||'')+(d.warnings&&d.warnings.length?('　⚠ '+d.warnings.join('; ')):'');
  }catch(e){AE('\\u52a0\\u8f7d\\u767d\\u540d\\u5355\\u5931\\u8d25\\uff1a'+e.message,false);E(e.message);}
}
async function addEntry(){
  var name=document.getElementById('in-name').value.trim();
  if(!name){AE('\\u8bf7\\u8f93\\u5165\\u547d\\u4ee4',false);return;}
  var denied=document.getElementById('in-denied').value.split(',').map(function(s){return s.trim();}).filter(Boolean);
  var note=document.getElementById('in-note').value.trim();
  try{
    var r=await fetch('/dashboard/allowlist',{method:'POST',headers:H({'Content-Type':'application/json'}),body:JSON.stringify({name:name,note:note,denied:denied})});
    var d=await r.json();if(!r.ok)throw new Error(d.detail||'\\u4fdd\\u5b58\\u5931\\u8d25');
    document.getElementById('in-name').value='';document.getElementById('in-denied').value='';document.getElementById('in-note').value='';
    renderAllowlist(d.entries||[]);AE('\\u5df2\\u4fdd\\u5b58\\uff0c\\u7acb\\u5373\\u751f\\u6548\\uff1a'+name+(denied.length?('\\uff08\\u7981\\u7528 '+denied.join(', ')+'\\uff09'):''),true);
  }catch(e){AE('\\u4fdd\\u5b58\\u5931\\u8d25\\uff1a'+e.message,false);E(e.message);}
}
async function delEntry(name){
  if(!confirm('\\u5220\\u9664\\u767d\\u540d\\u5355\\u9879\\u300c'+name+'\\u300d\\uff1f\\u5220\\u9664\\u540e\\u8be5\\u547d\\u4ee4\\u4f1a\\u91cd\\u65b0\\u8fd4\\u56de UNKNOWN\\u3002'))return;
  try{
    var r=await fetch('/dashboard/allowlist?name='+encodeURIComponent(name),{method:'DELETE',headers:H()});
    var d=await r.json();if(!r.ok)throw new Error(d.detail||'\\u5220\\u9664\\u5931\\u8d25');
    renderAllowlist(d.entries||[]);AE('\\u5df2\\u5220\\u9664\\uff1a'+name,true);
  }catch(e){AE('\\u5220\\u9664\\u5931\\u8d25\\uff1a'+e.message,false);E(e.message);}
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
loadTargets();
</script>
</body></html>"""
