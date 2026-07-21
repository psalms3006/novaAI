"""
server_extra.py
════════════════
UI broadcast + Flask UI/phone HTTP servers. Extracted from nova.py (Phase 2).
Zero behavior change. _memory_texts / _planner accessed via _nova. (owned
by nova.py, mutated in main()); everything else here is never reassigned.
"""
from __future__ import annotations
import json, webbrowser
from typing import Any

import nova_state
import nova as _nova
log = _nova.log
HAS_FLASK = _nova.HAS_FLASK
_Flask = _nova._Flask if HAS_FLASK else None
_flask_request = _nova._flask_request if HAS_FLASK else None
_flask_jsonify = _nova._flask_jsonify if HAS_FLASK else None
HAS_GEMINI = _nova.HAS_GEMINI
GEMINI_API_KEY = _nova.GEMINI_API_KEY
NOVA_SYSTEM_PROMPT = _nova.NOVA_SYSTEM_PROMPT
UI_PORT = _nova.UI_PORT
PHONE_PORT = _nova.PHONE_PORT
FORCE_OFFLINE = _nova.FORCE_OFFLINE
_ui_clients = _nova._ui_clients
_ui_lock = _nova._ui_lock
_call_gemini_chat = _nova._call_gemini_chat
_execute_tool_sync = _nova._execute_tool_sync
agent_process = _nova.agent_process
build_memory_context = _nova.build_memory_context
is_online = _nova.is_online
get_local_ip = _nova.get_local_ip
run_offline_loop = _nova.run_offline_loop
think_offline = _nova.think_offline

def _broadcast_ui(data: dict) -> None:
    """Broadcast a message to all connected UI WebSocket clients."""
    with _ui_lock:
        dead = []
        for client in _ui_clients:
            try:
                client.send(json.dumps(data))
            except Exception:
                dead.append(client)
        for d in dead:
            if d in _ui_clients:
                _ui_clients.remove(d)


# ══════════════════════════════════════════════════════════════════════════════
#  3D WEB UI — JARVIS-inspired holographic interface
# ══════════════════════════════════════════════════════════════════════════════

NOVA_UI_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
<title>NOVA — Neural Operating Voice Assistant</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/three.js/r128/three.min.js"></script>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  :root {
    --nova-cyan: #00d4ff;
    --nova-glow: #00d4ff44;
    --nova-dark: #050a14;
    --nova-panel: #0a1525;
    --nova-border: #1a3a5c;
    --nova-text: #c0e8ff;
    --nova-dim: #4a7a9a;
    --nova-alert: #ff6b35;
    --nova-success: #00ff88;
  }
  body {
    font-family: 'Segoe UI', system-ui, sans-serif;
    background: var(--nova-dark);
    color: var(--nova-text);
    height: 100dvh;
    overflow: hidden;
    display: flex;
    flex-direction: column;
  }
  #header {
    display: flex; align-items: center; justify-content: space-between;
    padding: 8px 20px;
    background: linear-gradient(180deg, #0a1a2a 0%, var(--nova-dark) 100%);
    border-bottom: 1px solid var(--nova-border);
    z-index: 100;
  }
  #header-left { display: flex; align-items: center; gap: 12px; }
  #nova-logo {
    font-size: 22px; font-weight: 800; letter-spacing: 6px;
    color: var(--nova-cyan);
    text-shadow: 0 0 20px var(--nova-glow), 0 0 40px var(--nova-glow);
  }
  #nova-sub { font-size: 9px; color: var(--nova-dim); letter-spacing: 2px; text-transform: uppercase; }
  #header-right { display: flex; align-items: center; gap: 16px; }
  .status-dot {
    width: 8px; height: 8px; border-radius: 50%;
    background: var(--nova-success);
    box-shadow: 0 0 8px var(--nova-success), 0 0 16px var(--nova-success);
    animation: pulse-dot 2s ease-in-out infinite;
  }
  .status-dot.offline { background: var(--nova-alert); box-shadow: 0 0 8px var(--nova-alert); }
  @keyframes pulse-dot { 0%,100%{opacity:1;transform:scale(1)} 50%{opacity:0.5;transform:scale(0.85)} }
  #clock { font-family: 'Courier New', monospace; font-size: 14px; color: var(--nova-cyan); }
  #main { flex: 1; display: flex; overflow: hidden; }
  #left-panel {
    width: 260px; min-width: 200px;
    background: var(--nova-panel);
    border-right: 1px solid var(--nova-border);
    padding: 16px;
    display: flex; flex-direction: column; gap: 12px;
    overflow-y: auto;
  }
  .panel-title {
    font-size: 10px; letter-spacing: 3px; text-transform: uppercase;
    color: var(--nova-dim); border-bottom: 1px solid var(--nova-border);
    padding-bottom: 6px; margin-bottom: 4px;
  }
  .sys-item { display: flex; justify-content: space-between; align-items: center; padding: 6px 0; font-size: 12px; }
  .sys-label { color: var(--nova-dim); }
  .sys-value { color: var(--nova-cyan); font-family: monospace; }
  .progress-bar { height: 4px; background: #0a2030; border-radius: 2px; overflow: hidden; margin-top: 4px; }
  .progress-fill {
    height: 100%; background: linear-gradient(90deg, var(--nova-cyan), var(--nova-success));
    border-radius: 2px; transition: width 0.5s ease;
    box-shadow: 0 0 8px var(--nova-glow);
  }
  .progress-fill.warn { background: linear-gradient(90deg, #ffaa00, var(--nova-alert)); }
  #center { flex: 1; display: flex; flex-direction: column; position: relative; }
  #core-canvas { position: absolute; top: 0; left: 0; width: 100%; height: 100%; z-index: 1; pointer-events: none; }
  #chat-area {
    flex: 1; overflow-y: auto; padding: 20px;
    display: flex; flex-direction: column; gap: 14px;
    z-index: 2; position: relative;
    scrollbar-width: thin; scrollbar-color: var(--nova-border) transparent;
  }
  #chat-area::-webkit-scrollbar { width: 4px; }
  #chat-area::-webkit-scrollbar-thumb { background: var(--nova-border); border-radius: 2px; }
  .msg {
    max-width: 75%; padding: 12px 16px; border-radius: 16px;
    font-size: 14px; line-height: 1.6; word-break: break-word;
    animation: msg-in 0.3s ease-out; backdrop-filter: blur(10px);
  }
  @keyframes msg-in { from{opacity:0;transform:translateY(10px) scale(0.95)} to{opacity:1;transform:translateY(0) scale(1)} }
  .msg-user {
    align-self: flex-end;
    background: linear-gradient(135deg, #0d3a5c 0%, #0a2540 100%);
    border: 1px solid #1a5a8a; border-bottom-right-radius: 4px; color: #e0f0ff;
  }
  .msg-nova {
    align-self: flex-start;
    background: linear-gradient(135deg, #0a2a1a 0%, #0a1a0a 100%);
    border: 1px solid #1a5a3a; border-bottom-left-radius: 4px; color: #c0f0d0;
  }
  .msg-nova .msg-name { font-size: 10px; color: var(--nova-success); font-weight: 700; letter-spacing: 2px; text-transform: uppercase; margin-bottom: 4px; }
  .msg-user .msg-name { font-size: 10px; color: var(--nova-cyan); font-weight: 700; letter-spacing: 2px; text-transform: uppercase; margin-bottom: 4px; text-align: right; }
  .msg-time { font-size: 9px; color: var(--nova-dim); margin-top: 6px; font-family: monospace; }
  .msg-user .msg-time { text-align: right; }
  #right-panel {
    width: 300px; min-width: 240px;
    background: var(--nova-panel); border-left: 1px solid var(--nova-border);
    padding: 16px; display: flex; flex-direction: column; gap: 12px; overflow-y: auto;
  }
  #activity-log { flex: 1; overflow-y: auto; font-family: 'Courier New', monospace; font-size: 11px; line-height: 1.8; color: var(--nova-dim); }
  .log-entry { padding: 2px 0; border-bottom: 1px solid #0a1a2a; }
  .log-entry.sys { color: var(--nova-success); }
  .log-entry.user { color: var(--nova-cyan); }
  .log-entry.error { color: var(--nova-alert); }
  #file-drop {
    border: 2px dashed var(--nova-border); border-radius: 12px;
    padding: 24px; text-align: center; cursor: pointer; transition: all 0.3s ease;
  }
  #file-drop:hover, #file-drop.dragover { border-color: var(--nova-cyan); background: rgba(0,212,255,0.05); }
  #file-drop svg { width: 32px; height: 32px; fill: var(--nova-dim); margin-bottom: 8px; }
  #file-drop p { font-size: 11px; color: var(--nova-dim); }
  #input-bar {
    display: flex; gap: 10px; padding: 12px 20px;
    background: linear-gradient(0deg, #0a1a2a 0%, var(--nova-dark) 100%);
    border-top: 1px solid var(--nova-border); z-index: 100;
  }
  #msg-input {
    flex: 1; background: #0a1525; border: 1px solid var(--nova-border);
    border-radius: 24px; padding: 10px 20px; color: var(--nova-text); font-size: 14px; outline: none; transition: border-color 0.3s;
  }
  #msg-input:focus { border-color: var(--nova-cyan); box-shadow: 0 0 12px var(--nova-glow); }
  #msg-input::placeholder { color: var(--nova-dim); }
  .btn {
    width: 42px; height: 42px; border-radius: 50%;
    border: 1px solid var(--nova-border); background: #0a1525;
    color: var(--nova-cyan); cursor: pointer;
    display: flex; align-items: center; justify-content: center;
    transition: all 0.3s; font-size: 16px;
  }
  .btn:hover { border-color: var(--nova-cyan); box-shadow: 0 0 12px var(--nova-glow); background: rgba(0,212,255,0.1); }
  .btn.mic { background: linear-gradient(135deg, #0a3a2a, #0a1a0a); border-color: #1a5a3a; }
  .btn.mic:hover { border-color: var(--nova-success); box-shadow: 0 0 12px rgba(0,255,136,0.3); }
  .btn.mic.active { background: linear-gradient(135deg, #1a5a3a, #0a3a2a); border-color: var(--nova-success); animation: mic-pulse 1s ease-in-out infinite; }
  @keyframes mic-pulse { 0%,100%{box-shadow:0 0 8px rgba(0,255,136,0.3)} 50%{box-shadow:0 0 20px rgba(0,255,136,0.6)} }
  #listening-overlay {
    position: fixed; bottom: 80px; left: 50%; transform: translateX(-50%);
    background: rgba(5,10,20,0.9); border: 1px solid var(--nova-success);
    border-radius: 24px; padding: 12px 28px;
    display: none; align-items: center; gap: 12px;
    backdrop-filter: blur(10px); z-index: 200; box-shadow: 0 0 30px rgba(0,255,136,0.2);
  }
  #listening-overlay.active { display: flex; }
  .wave-bar { width: 3px; background: var(--nova-success); border-radius: 2px; animation: wave 0.5s ease-in-out infinite alternate; }
  .wave-bar:nth-child(1){height:8px;animation-delay:0s}
  .wave-bar:nth-child(2){height:16px;animation-delay:0.1s}
  .wave-bar:nth-child(3){height:24px;animation-delay:0.2s}
  .wave-bar:nth-child(4){height:16px;animation-delay:0.3s}
  .wave-bar:nth-child(5){height:8px;animation-delay:0.4s}
  @keyframes wave { from{transform:scaleY(0.5);opacity:0.5} to{transform:scaleY(1);opacity:1} }
  @media (max-width:1024px) { #left-panel{width:200px} #right-panel{width:240px} }
  @media (max-width:768px) { #left-panel,#right-panel{display:none} #nova-logo{font-size:16px;letter-spacing:3px} #clock{font-size:11px} }
  .typing { display:flex; gap:4px; padding:8px 12px; align-self:flex-start; }
  .typing-dot { width:6px; height:6px; border-radius:50%; background:var(--nova-success); opacity:0.4; animation:typing-bounce 1.2s infinite; }
  .typing-dot:nth-child(2){animation-delay:0.2s}
  .typing-dot:nth-child(3){animation-delay:0.4s}
  @keyframes typing-bounce{0%,80%,100%{opacity:0.4;transform:scale(1)}40%{opacity:1;transform:scale(1.3)}}
</style>
</head>
<body>
<div id="header">
  <div id="header-left">
    <div>
      <div id="nova-logo">N.O.V.A</div>
      <div id="nova-sub">Neural Operating Voice Assistant</div>
    </div>
  </div>
  <div id="header-right">
    <div class="status-dot" id="status-dot"></div>
    <div id="clock">00:00:00</div>
  </div>
</div>
<div id="main">
  <div id="left-panel">
    <div class="panel-title">System Monitor</div>
    <div class="sys-item"><span class="sys-label">CPU</span><span class="sys-value" id="cpu-val">0%</span></div>
    <div class="progress-bar"><div class="progress-fill" id="cpu-bar" style="width:0%"></div></div>
    <div class="sys-item"><span class="sys-label">RAM</span><span class="sys-value" id="ram-val">0%</span></div>
    <div class="progress-bar"><div class="progress-fill" id="ram-bar" style="width:0%"></div></div>
    <div class="sys-item"><span class="sys-label">NET</span><span class="sys-value" id="net-val">0 KB/s</span></div>
    <div class="sys-item"><span class="sys-label">GPU</span><span class="sys-value" id="gpu-val">N/A</span></div>
    <div class="panel-title" style="margin-top:12px">Connection</div>
    <div class="sys-item"><span class="sys-label">Mode</span><span class="sys-value" id="mode-val">OFFLINE</span></div>
    <div class="sys-item"><span class="sys-label">Brain</span><span class="sys-value" id="brain-val">Gemini/TinyLlama</span></div>
    <div class="sys-item"><span class="sys-label">STT</span><span class="sys-value" id="stt-val">faster-whisper</span></div>
    <div class="sys-item"><span class="sys-label">TTS</span><span class="sys-value" id="tts-val">pyttsx3</span></div>
    <div class="panel-title" style="margin-top:12px">Shortcuts</div>
    <div class="sys-item"><span class="sys-label">F1</span><span class="sys-value">Mute</span></div>
    <div class="sys-item"><span class="sys-label">F11</span><span class="sys-value">Fullscreen</span></div>
    <div class="sys-item"><span class="sys-label">Ctrl+K</span><span class="sys-value">Clear</span></div>
  </div>
  <div id="center">
    <canvas id="core-canvas"></canvas>
    <div id="chat-area">
      <div class="msg msg-nova">
        <div class="msg-name">NOVA</div>
        <div>System online. I am NOVA, your Neural Operating Voice Assistant. How may I assist you today?</div>
        <div class="msg-time" id="first-time"></div>
      </div>
    </div>
  </div>
  <div id="right-panel">
    <div class="panel-title">Activity Log</div>
    <div id="activity-log">
      <div class="log-entry sys">SYS: NOVA v3.4 initialized</div>
      <div class="log-entry sys">SYS: Vision model: gemini-2.0-flash</div>
      <div class="log-entry sys">SYS: Offline brain: Gemini REST / TinyLlama</div>
    </div>
    <div class="panel-title">File Upload</div>
    <div id="file-drop">
      <svg viewBox="0 0 24 24"><path d="M19.35 10.04C18.67 6.59 15.64 4 12 4 9.11 4 6.6 5.64 5.35 8.04 2.34 8.36 0 10.91 0 14c0 3.31 2.69 6 6 6h13c2.76 0 5-2.24 5-5 0-2.64-2.05-4.78-4.65-4.96zM14 13v4h-4v-4H7l5-5 5 5h-3z"/></svg>
      <p>Drop files here<br>or click to browse</p>
    </div>
  </div>
</div>
<div id="input-bar">
  <button class="btn mic" id="mic-btn" title="Hold to speak">&#127908;</button>
  <input type="text" id="msg-input" placeholder="Type a command or question..." autocomplete="off">
  <button class="btn" id="send-btn" title="Send">&#9658;</button>
</div>
<div id="listening-overlay">
  <div class="wave-bar"></div><div class="wave-bar"></div><div class="wave-bar"></div>
  <div class="wave-bar"></div><div class="wave-bar"></div>
  <span style="font-size:12px;color:var(--nova-success);font-family:monospace;">LISTENING...</span>
</div>
<script>
function updateClock(){const n=new Date();document.getElementById('clock').textContent=n.toLocaleTimeString('en-US',{hour12:false});}
setInterval(updateClock,1000);updateClock();
function updateSysMonitor(){
  const cpu=Math.floor(Math.random()*30)+5,ram=Math.floor(Math.random()*40)+20;
  document.getElementById('cpu-val').textContent=cpu+'%';
  document.getElementById('cpu-bar').style.width=cpu+'%';
  document.getElementById('cpu-bar').className='progress-fill'+(cpu>80?' warn':'');
  document.getElementById('ram-val').textContent=ram+'%';
  document.getElementById('ram-bar').style.width=ram+'%';
  document.getElementById('ram-bar').className='progress-fill'+(ram>80?' warn':'');
  document.getElementById('net-val').textContent=Math.floor(Math.random()*100)+' KB/s';
}
setInterval(updateSysMonitor,2000);updateSysMonitor();
document.getElementById('first-time').textContent=new Date().toLocaleTimeString('en-US',{hour:'2-digit',minute:'2-digit'});
const canvas=document.getElementById('core-canvas');
const scene=new THREE.Scene();
const camera=new THREE.PerspectiveCamera(60,canvas.clientWidth/canvas.clientHeight,0.1,1000);
const renderer=new THREE.WebGLRenderer({canvas,alpha:true,antialias:true});
renderer.setSize(canvas.clientWidth,canvas.clientHeight);
renderer.setPixelRatio(Math.min(window.devicePixelRatio,2));
camera.position.z=5;
const coreGeo=new THREE.SphereGeometry(0.6,32,32);
const coreMat=new THREE.MeshBasicMaterial({color:0x00d4ff,transparent:true,opacity:0.15,wireframe:true});
const core=new THREE.Mesh(coreGeo,coreMat);scene.add(core);
const glowGeo=new THREE.SphereGeometry(0.4,16,16);
const glowMat=new THREE.MeshBasicMaterial({color:0x00ff88,transparent:true,opacity:0.3});
const glow=new THREE.Mesh(glowGeo,glowMat);scene.add(glow);
const rings=[];
for(let i=0;i<3;i++){
  const g=new THREE.TorusGeometry(1.2+i*0.5,0.01,8,64);
  const m=new THREE.MeshBasicMaterial({color:i===0?0x00d4ff:(i===1?0x00ff88:0x4a7a9a),transparent:true,opacity:0.3+i*0.1});
  const r=new THREE.Mesh(g,m);r.rotation.x=Math.PI/2+(i*0.3);r.rotation.y=i*0.5;scene.add(r);rings.push(r);
}
const pGeo=new THREE.BufferGeometry();
const pArr=new Float32Array(600);for(let i=0;i<600;i++)pArr[i]=(Math.random()-0.5)*8;
pGeo.setAttribute('position',new THREE.BufferAttribute(pArr,3));
const pMat=new THREE.PointsMaterial({size:0.02,color:0x00d4ff,transparent:true,opacity:0.6});
const particles=new THREE.Points(pGeo,pMat);scene.add(particles);
let t=0;
function animate(){requestAnimationFrame(animate);t+=0.01;core.rotation.y+=0.005;core.rotation.x+=0.002;glow.scale.setScalar(1+Math.sin(t*2)*0.1);glow.rotation.y-=0.01;rings.forEach((r,i)=>{r.rotation.z+=0.003*(i%2===0?1:-1);r.rotation.x+=0.001;});particles.rotation.y+=0.0005;renderer.render(scene,camera);}
animate();
window.addEventListener('resize',()=>{const w=canvas.clientWidth,h=canvas.clientHeight;camera.aspect=w/h;camera.updateProjectionMatrix();renderer.setSize(w,h);});
const chatArea=document.getElementById('chat-area');
const msgInput=document.getElementById('msg-input');
const sendBtn=document.getElementById('send-btn');
const micBtn=document.getElementById('mic-btn');
const listeningOverlay=document.getElementById('listening-overlay');
const activityLog=document.getElementById('activity-log');
function addLog(text,type='sys'){const e=document.createElement('div');e.className='log-entry '+type;e.textContent=text;activityLog.appendChild(e);activityLog.scrollTop=activityLog.scrollHeight;}
function appendMsg(text,isUser){
  const msg=document.createElement('div');msg.className='msg '+(isUser?'msg-user':'msg-nova');
  const name=document.createElement('div');name.className='msg-name';name.textContent=isUser?'YOU':'NOVA';
  const body=document.createElement('div');body.textContent=text;
  const timeEl=document.createElement('div');timeEl.className='msg-time';timeEl.textContent=new Date().toLocaleTimeString('en-US',{hour:'2-digit',minute:'2-digit'});
  msg.appendChild(name);msg.appendChild(body);msg.appendChild(timeEl);chatArea.appendChild(msg);chatArea.scrollTop=chatArea.scrollHeight;
}
function showTyping(){const t=document.createElement('div');t.className='typing';t.id='typing-indicator';for(let i=0;i<3;i++){const d=document.createElement('div');d.className='typing-dot';t.appendChild(d);}chatArea.appendChild(t);chatArea.scrollTop=chatArea.scrollHeight;}
function hideTyping(){const el=document.getElementById('typing-indicator');if(el)el.remove();}
let ws=null;
function connectWS(){
  const proto=window.location.protocol==='https:'?'wss:':'ws:';
  ws=new WebSocket(proto+'//'+window.location.host+'/ws');
  ws.onopen=()=>{addLog('SYS: WebSocket connected','sys');document.getElementById('status-dot').classList.remove('offline');};
  ws.onmessage=(e)=>{
    const data=JSON.parse(e.data);
    if(data.type==='nova_speak'){hideTyping();appendMsg(data.text,false);addLog('NOVA: '+data.text.substring(0,60),'sys');}
    else if(data.type==='nova_typing'){showTyping();}
    else if(data.type==='system'){addLog('SYS: '+data.text,'sys');}
  };
  ws.onclose=()=>{document.getElementById('status-dot').classList.add('offline');addLog('SYS: Connection lost, reconnecting...','error');setTimeout(connectWS,3000);};
  ws.onerror=()=>{document.getElementById('status-dot').classList.add('offline');};
}
connectWS();
async function sendMsg(){
  const text=msgInput.value.trim();if(!text)return;msgInput.value='';
  appendMsg(text,true);addLog('YOU: '+text.substring(0,60),'user');showTyping();
  try{
    if(ws&&ws.readyState===WebSocket.OPEN){ws.send(JSON.stringify({type:'chat',message:text}));}
    else{
      const res=await fetch('/chat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({message:text})});
      const data=await res.json();hideTyping();appendMsg(data.reply||'(no response)',false);
    }
  }catch(e){hideTyping();appendMsg('Connection error: '+e.message,false);addLog('ERR: '+e.message,'error');}
}
sendBtn.addEventListener('click',sendMsg);
msgInput.addEventListener('keypress',e=>{if(e.key==='Enter')sendMsg();});
let micActive=false;
micBtn.addEventListener('mousedown',()=>{micActive=true;micBtn.classList.add('active');listeningOverlay.classList.add('active');if(ws&&ws.readyState===WebSocket.OPEN)ws.send(JSON.stringify({type:'mic_start'}));});
micBtn.addEventListener('mouseup',()=>{micActive=false;micBtn.classList.remove('active');listeningOverlay.classList.remove('active');if(ws&&ws.readyState===WebSocket.OPEN)ws.send(JSON.stringify({type:'mic_stop'}));});
micBtn.addEventListener('mouseleave',()=>{if(micActive){micActive=false;micBtn.classList.remove('active');listeningOverlay.classList.remove('active');}});
micBtn.addEventListener('touchstart',(e)=>{e.preventDefault();micBtn.dispatchEvent(new Event('mousedown'));});
micBtn.addEventListener('touchend',(e)=>{e.preventDefault();micBtn.dispatchEvent(new Event('mouseup'));});
const fileDrop=document.getElementById('file-drop');
fileDrop.addEventListener('dragover',(e)=>{e.preventDefault();fileDrop.classList.add('dragover');});
fileDrop.addEventListener('dragleave',()=>fileDrop.classList.remove('dragover'));
fileDrop.addEventListener('drop',(e)=>{e.preventDefault();fileDrop.classList.remove('dragover');const files=e.dataTransfer.files;if(files.length>0){addLog('SYS: File dropped: '+files[0].name,'sys');appendMsg('Uploaded: '+files[0].name,true);}});
fileDrop.addEventListener('click',()=>{const input=document.createElement('input');input.type='file';input.onchange=(e)=>{if(e.target.files.length>0){addLog('SYS: File selected: '+e.target.files[0].name,'sys');appendMsg('Uploaded: '+e.target.files[0].name,true);}};input.click();});
document.addEventListener('keydown',(e)=>{
  if(e.key==='F11'){e.preventDefault();if(!document.fullscreenElement)document.documentElement.requestFullscreen();else document.exitFullscreen();}
  if(e.key==='F1'){e.preventDefault();addLog('SYS: Microphone muted','sys');}
  if(e.ctrlKey&&e.key==='k'){e.preventDefault();chatArea.innerHTML='';addLog('SYS: Chat cleared','sys');}
});
</script>
</body>
</html>"""


# ══════════════════════════════════════════════════════════════════════════════
#  UI SERVER — Flask with WebSocket support
# ══════════════════════════════════════════════════════════════════════════════

def run_ui_server(meta: dict) -> None:
    if not HAS_FLASK:
        print("❌ Flask not installed. Run: pip install flask flask-sock")
        print("   Falling back to text mode...")
        run_offline_loop(meta)
        return

    has_sock = False
    try:
        import importlib.util as _importlib_util
        if _importlib_util.find_spec("flask_sock") is not None:
            has_sock = True
        else:
            raise ImportError
    except ImportError:
        print("⚠️  flask-sock not installed. WebSocket disabled. Run: pip install flask-sock")

    app = _Flask("NOVA_UI")
    import logging as _logging
    _logging.getLogger("werkzeug").setLevel(_logging.ERROR)

    @app.route("/")
    def index() -> str:
        return NOVA_UI_HTML

    @app.route("/chat", methods=["POST"])
    def chat() -> Any:
        data    = _flask_request.get_json(force=True, silent=True) or {}
        message = str(data.get("message", "")).strip()
        if not message:
            return _flask_jsonify({"reply": "I didn't receive a message."})
        try:
            _broadcast_ui({"type": "nova_typing"})
            reply = None
            if HAS_GEMINI and GEMINI_API_KEY and is_online() and not FORCE_OFFLINE:
                mem_ctx     = build_memory_context(meta, query=message)
                sys_content = NOVA_SYSTEM_PROMPT
                if mem_ctx:
                    sys_content += f"\n\nMEMORY:\n{mem_ctx}"
                gemini_messages = [
                    {"role": "system", "content": sys_content},
                    {"role": "user",   "content": message},
                ]
                gemini_result = _call_gemini_chat(gemini_messages, use_tools=True)
                if gemini_result:
                    if gemini_result["tool_calls"]:
                        followup = list(gemini_messages) + [
                            {"role": "assistant", "content": gemini_result["text"], "tool_calls": gemini_result["tool_calls"]}
                        ]
                        for tc in gemini_result["tool_calls"]:
                            tool_result = _execute_tool_sync(tc["name"], tc["args"], meta)
                            followup.append({"role": "tool", "content": tool_result, "tool_call_id": tc.get("id")})
                        final = _call_gemini_chat(followup, use_tools=False)
                        reply = final["text"].strip() if final else "Done."
                    else:
                        reply = gemini_result["text"].strip()
            if not reply:
                agent_reply = agent_process(message, meta)
                reply = agent_reply if agent_reply else think_offline(message, meta)
            _broadcast_ui({"type": "nova_speak", "text": reply})
        except Exception as e:
            reply = f"Error: {e}"
        return _flask_jsonify({"reply": reply})

    @app.route("/memory")
    def memory() -> Any:
        return _flask_jsonify({"user": meta.get("user_name", ""), "facts": nova_state._memory_texts})

    if has_sock:
        from flask_sock import Sock as _Sock  # noqa: F811
        sock = _Sock(app)

        @sock.route("/ws")
        def websocket(ws: Any) -> None:
            with _ui_lock:
                _ui_clients.append(ws)
            try:
                ws.send(json.dumps({"type": "system", "text": "Connected to NOVA v3.4"}))
                while True:
                    msg = ws.receive()
                    if msg is None:
                        break
                    try:
                        data = json.loads(msg)
                        if data.get("type") == "chat":
                            user_msg = str(data.get("message", "")).strip()
                            if user_msg:
                                _broadcast_ui({"type": "nova_typing"})
                                reply = None
                                if HAS_GEMINI and GEMINI_API_KEY and is_online() and not FORCE_OFFLINE:
                                    mem_ctx     = build_memory_context(meta, query=user_msg)
                                    sys_content = NOVA_SYSTEM_PROMPT
                                    if mem_ctx:
                                        sys_content += f"\n\nMEMORY:\n{mem_ctx}"
                                    gemini_messages = [
                                        {"role": "system", "content": sys_content},
                                        {"role": "user",   "content": user_msg},
                                    ]
                                    gemini_result = _call_gemini_chat(gemini_messages, use_tools=True)
                                    if gemini_result:
                                        if gemini_result["tool_calls"]:
                                            followup = list(gemini_messages) + [
                                                {"role": "assistant", "content": gemini_result["text"], "tool_calls": gemini_result["tool_calls"]}
                                            ]
                                            for tc in gemini_result["tool_calls"]:
                                                tool_result = _execute_tool_sync(tc["name"], tc["args"], meta)
                                                followup.append({"role": "tool", "content": tool_result, "tool_call_id": tc.get("id")})
                                            final = _call_gemini_chat(followup, use_tools=False)
                                            reply = final["text"].strip() if final else "Done."
                                        else:
                                            reply = gemini_result["text"].strip()
                                if not reply:
                                    agent_reply = agent_process(user_msg, meta)
                                    reply = agent_reply if agent_reply else think_offline(user_msg, meta)
                                _broadcast_ui({"type": "nova_speak", "text": reply})
                        elif data.get("type") == "mic_start":
                            _broadcast_ui({"type": "system", "text": "Listening..."})
                        elif data.get("type") == "mic_stop":
                            _broadcast_ui({"type": "system", "text": "Processing..."})
                    except Exception as e:
                        log.error(f"WS message error: {e}")
            except Exception:
                pass
            finally:
                with _ui_lock:
                    if ws in _ui_clients:
                        _ui_clients.remove(ws)

    local_ip = get_local_ip()
    print("\n" + "=" * 60)
    print("[NOVA] 🌐 3D UI Server starting...")
    print(f"[NOVA] 🌐 Open in browser: http://{local_ip}:{UI_PORT}")
    print(f"[NOVA] 🌐 Local access:    http://127.0.0.1:{UI_PORT}")
    print("[NOVA] 📱 Works on phone too — same WiFi network")
    print("=" * 60 + "\n")

    try:
        webbrowser.open(f"http://127.0.0.1:{UI_PORT}")
    except Exception:
        pass

    if nova_state._planner is not None:
        nova_state._planner.set_speak(lambda t: _broadcast_ui({"type": "nova_speak", "text": t}))

    try:
        app.run(host="0.0.0.0", port=UI_PORT, debug=False, use_reloader=False, threaded=True)
    except KeyboardInterrupt:
        print("\n[NOVA] 🌐 UI server stopped.")


# ══════════════════════════════════════════════════════════════════════════════
#  PHONE SERVER
# ══════════════════════════════════════════════════════════════════════════════

PHONE_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0">
<title>NOVA</title>
<style>
  *{box-sizing:border-box;margin:0;padding:0}
  body{font-family:-apple-system,sans-serif;background:#0a0a0f;color:#e0e0e0;height:100dvh;display:flex;flex-direction:column}
  #header{padding:12px 16px;background:#12121a;border-bottom:1px solid #222;display:flex;align-items:center;gap:10px}
  #header .dot{width:10px;height:10px;border-radius:50%;background:#00ff88;box-shadow:0 0 8px #00ff88;animation:pulse 2s infinite}
  @keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}
  #header h1{font-size:18px;font-weight:700;color:#00ff88;letter-spacing:2px}
  #header small{font-size:11px;color:#555;margin-left:auto}
  #chat{flex:1;overflow-y:auto;padding:16px;display:flex;flex-direction:column;gap:12px}
  .msg{max-width:82%;padding:10px 14px;border-radius:18px;font-size:14px;line-height:1.5;white-space:pre-wrap;word-break:break-word}
  .user{background:#1a3a5c;align-self:flex-end;border-bottom-right-radius:4px}
  .nova{background:#1a2a1a;align-self:flex-start;border-bottom-left-radius:4px;border:1px solid #00ff8833}
  .nova .name{font-size:11px;color:#00ff88;font-weight:600;margin-bottom:4px}
  .typing{display:flex;gap:4px;padding:14px}
  .typing span{width:7px;height:7px;border-radius:50%;background:#00ff88;animation:bounce 1.2s infinite}
  .typing span:nth-child(2){animation-delay:.2s}.typing span:nth-child(3){animation-delay:.4s}
  @keyframes bounce{0%,80%,100%{transform:scale(.6)}40%{transform:scale(1)}}
  #input-area{padding:10px 12px;background:#12121a;border-top:1px solid #222;display:flex;gap:8px}
  #msg-input{flex:1;background:#1e1e28;border:1px solid #333;border-radius:22px;padding:10px 16px;color:#e0e0e0;font-size:15px;outline:none}
  #msg-input:focus{border-color:#00ff88}
  #send-btn{background:#00ff88;color:#000;border:none;border-radius:50%;width:42px;height:42px;font-size:18px;cursor:pointer;display:flex;align-items:center;justify-content:center;flex-shrink:0;font-weight:bold}
  #send-btn:disabled{background:#333;color:#666}
</style>
</head>
<body>
<div id="header"><div class="dot"></div><h1>NOVA</h1><small>FUTO AI</small></div>
<div id="chat"><div class="msg nova"><div class="name">NOVA</div>Online and ready. How can I help?</div></div>
<div id="input-area">
  <input id="msg-input" type="text" placeholder="Type a message..." autocomplete="off">
  <button id="send-btn" onclick="sendMsg()">&#9658;</button>
</div>
<script>
const chat=document.getElementById('chat'),input=document.getElementById('msg-input'),btn=document.getElementById('send-btn');
input.addEventListener('keypress',e=>{if(e.key==='Enter')sendMsg();});
function appendMsg(text,cls){const d=document.createElement('div');d.className='msg '+cls;if(cls==='nova'){const nm=document.createElement('div');nm.className='name';nm.textContent='NOVA';d.appendChild(nm);}d.appendChild(document.createTextNode(text));chat.appendChild(d);chat.scrollTop=chat.scrollHeight;return d;}
function showTyping(){const d=document.createElement('div');d.className='msg nova';d.id='typing';d.innerHTML='<div class="name">NOVA</div><div class="typing"><span></span><span></span><span></span></div>';chat.appendChild(d);chat.scrollTop=chat.scrollHeight;}
function hideTyping(){const d=document.getElementById('typing');if(d)d.remove();}
async function sendMsg(){
  const text=input.value.trim();if(!text)return;input.value='';btn.disabled=true;
  appendMsg(text,'user');showTyping();
  try{const res=await fetch('/chat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({message:text})});const data=await res.json();hideTyping();appendMsg(data.reply||'(no response)','nova');}
  catch(e){hideTyping();appendMsg('Connection error: '+e.message,'nova');}
  btn.disabled=false;input.focus();
}
</script>
</body>
</html>"""


def run_phone_server(meta: dict) -> None:
    if not HAS_FLASK:
        print("❌ Flask not installed. Run: pip install flask")
        print("   Falling back to text mode...")
        run_offline_loop(meta)
        return

    app = _Flask("NOVA_Phone")
    import logging as _logging
    _logging.getLogger("werkzeug").setLevel(_logging.ERROR)

    @app.route("/")
    def index() -> str:
        return PHONE_HTML

    @app.route("/chat", methods=["POST"])
    def chat() -> Any:
        data    = _flask_request.get_json(force=True, silent=True) or {}
        message = str(data.get("message", "")).strip()
        if not message:
            return _flask_jsonify({"reply": "I didn't receive a message."})
        try:
            reply = None
            if HAS_GEMINI and GEMINI_API_KEY and is_online() and not FORCE_OFFLINE:
                mem_ctx     = build_memory_context(meta, query=message)
                sys_content = NOVA_SYSTEM_PROMPT
                if mem_ctx:
                    sys_content += f"\n\nMEMORY:\n{mem_ctx}"
                gemini_messages = [
                    {"role": "system", "content": sys_content},
                    {"role": "user",   "content": message},
                ]
                gemini_result = _call_gemini_chat(gemini_messages, use_tools=True)
                if gemini_result:
                    if gemini_result["tool_calls"]:
                        followup = list(gemini_messages) + [
                            {"role": "assistant", "content": gemini_result["text"], "tool_calls": gemini_result["tool_calls"]}
                        ]
                        for tc in gemini_result["tool_calls"]:
                            tool_result = _execute_tool_sync(tc["name"], tc["args"], meta)
                            followup.append({"role": "tool", "content": tool_result, "tool_call_id": tc.get("id")})
                        final = _call_gemini_chat(followup, use_tools=False)
                        reply = final["text"].strip() if final else "Done."
                    else:
                        reply = gemini_result["text"].strip()
            if not reply:
                agent_reply = agent_process(message, meta)
                reply = agent_reply if agent_reply else think_offline(message, meta)
        except Exception as e:
            reply = f"Error: {e}"
        return _flask_jsonify({"reply": reply})

    @app.route("/memory")
    def memory() -> Any:
        return _flask_jsonify({"user": meta.get("user_name", ""), "facts": nova_state._memory_texts})

    local_ip = get_local_ip()
    print("\n[NOVA] 📱 Phone server starting...")
    print(f"[NOVA] 🌐 Open on your phone: http://{local_ip}:{PHONE_PORT}")
    print("[NOVA] 📶 Make sure phone and laptop are on the same WiFi network.")
    print("[NOVA] 🔴 Press Ctrl+C to stop.\n")

    if nova_state._planner is not None:
        nova_state._planner.set_speak(lambda t: print(f"\n⏰ Reminder: {t}"))

    try:
        app.run(host="0.0.0.0", port=PHONE_PORT, debug=False, use_reloader=False)
    except KeyboardInterrupt:
        print("\n[NOVA] 📱 Phone server stopped.")


# ══════════════════════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════════════════════

