"""Receive-only WebRTC first; native MPEG-TS HLS/MSE remain compatible fallback."""

PAGE = '''<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>实时预览 · Media Importer</title>
<script>if(location.hash==='#embedded')document.documentElement.classList.add('embedded');</script>
<style>
:root{color-scheme:dark;font-family:system-ui,-apple-system,sans-serif;background:#0b1220;color:#f3f4f6}
*{box-sizing:border-box}body{padding:24px;max-width:1100px;margin:auto}
.page-heading h1{font-size:20px;font-weight:600;margin:0 0 18px}
#state{font-size:13px;color:#dbeafe;line-height:1.5}viewer-video,video{display:block;width:100%;height:100%;min-height:0}
video{object-fit:contain}#screen{aspect-ratio:16/9;background:#030712;border-radius:10px;overflow:hidden}
.playback-bar{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:14px 0}
.playback-status{display:flex;flex-direction:column;gap:4px;min-width:0}#mode{font-size:12px;color:#94a3b8;line-height:1.5;overflow-wrap:anywhere}
nav{display:flex;gap:8px;flex-shrink:0}button{font:inherit;font-size:12px;min-height:40px;padding:8px 12px;background:#253449;color:#e2e8f0;border:1px solid #3b4a60;border-radius:8px;cursor:pointer;white-space:nowrap}
button:hover{background:#33465e}button:focus-visible{outline:2px solid #93c5fd;outline-offset:2px}p{font-size:12px;color:#94a3b8;line-height:1.6;margin:0}
.embedded,.embedded body{height:100%;min-height:0;overflow:hidden}
.embedded body{padding:0;margin:0;max-width:none;display:grid;grid-template-rows:minmax(0,1fr) auto}
.embedded .page-heading,.embedded #detail{display:none}
.embedded #screen{min-height:0;aspect-ratio:auto;border-radius:0}
.embedded .playback-bar{padding:10px 14px;background:#101c2e;border-top:1px solid #243247}
@media(max-width:520px){body{padding:12px}.playback-bar{flex-direction:column;align-items:stretch;gap:8px}.playback-status{flex-direction:row;align-items:center;flex-wrap:wrap;gap:4px 10px}nav button{flex:1;min-height:44px}.embedded .playback-bar{padding:10px 12px}}
</style><body><header class="page-heading"><h1>实时预览</h1></header>
<div id="screen"></div><div class="playback-bar"><div class="playback-status"><span id="state" role="status" aria-live="polite">正在连接画面</span><span id="mode"></span></div>
<nav aria-label="播放控制"><button id="retry" type="button">播放／重新连接</button><button id="compat" type="button">兼容播放</button><button id="sound" type="button" hidden>开启声音</button></nav></div>
<p id="detail">关闭页面不会结束预览。请在相机页面结束预览。</p>
<script type="module">
import {VideoRTC} from './player.js';
customElements.define('viewer-video', VideoRTC);
const screen=document.querySelector('#screen'), label=document.querySelector('#state'), detail=document.querySelector('#detail'), modeLabel=document.querySelector('#mode');
const native=!!document.createElement('video').canPlayType('application/vnd.apple.mpegurl');
const url=new URL('socket', location.href).href.replace(/^http/,'ws');
let player,socket,pc,ended=false,generation=0,mode='',rtcAvailable=false,audioAvailable=false,soundOn=false;
let lastTime=0,lastFrames=0,unchanged=0,started=0,hiddenAt=0,checking=false;
function video(){return mode==='mse'?player?.video:player;}
function dispose(){
 if(pc){pc.onconnectionstatechange=null;pc.onicecandidate=null;pc.ontrack=null;pc.close();pc=null;}
 if(socket){socket.onclose=null;socket.close();socket=null;}
 if(player){if(mode!=='mse'){player.onerror=null;player.pause();player.srcObject=null;player.removeAttribute('src');player.load();}player.remove();}
}
function finish(){
 ended=true;generation++;dispose();label.textContent='预览已结束';modeLabel.textContent='';
 detail.textContent='请从飞书「相机 → 相机页面」重新开启。';
}
function play(){video()?.play().catch(()=>{if(!ended)label.textContent='请点击播放按钮';});}
function fallback(id){if(!ended && generation===id && mode==='webrtc')connect(false,true);}
function connect(preferRTC=true,failed=false,forceMSE=false){
 if(ended)return;
 const id=++generation;dispose();lastTime=0;lastFrames=0;unchanged=0;started=Date.now();
 mode=preferRTC && rtcAvailable && typeof RTCPeerConnection!=='undefined'?'webrtc':native && !forceMSE?'hls':'mse';
 label.textContent='正在连接画面';
 modeLabel.textContent=mode==='webrtc'?'WebRTC · 低延迟模式':
  (mode==='hls'?'HLS':'MSE')+' · 兼容模式'+(failed?' · 低延迟连接未成功，已自动切换':'');
 if(mode==='mse'){
  player=document.createElement('viewer-video');player.mode='mse';player.media=audioAvailable?'video,audio':'video';player.background=false;
  screen.replaceChildren(player);if(player.video){player.video.muted=!soundOn;player.video.volume=1;}player.src=url;return;
 }
 player=document.createElement('video');player.controls=true;player.muted=!soundOn;player.playsInline=true;player.autoplay=true;
 screen.replaceChildren(player);socket=new WebSocket(url);
 if(mode==='hls'){
  player.onerror=()=>{
   if(ended || generation!==id)return;
   if(typeof MediaSource!=='undefined')connect(false,false,true);
   else label.textContent='兼容播放失败，请重新连接';
  };
  player.src=new URL('hls/master.m3u8',location.href).href;play();return;
 }
 const current=player,ws=socket;
 try{
  const peer=pc=new RTCPeerConnection({iceServers:[],bundlePolicy:'max-bundle'});
  const candidates=[];
  peer.addTransceiver('video',{direction:'recvonly'});if(audioAvailable)peer.addTransceiver('audio',{direction:'recvonly'});
  peer.ontrack=event=>{if(generation!==id)return;if(event.streams[0])current.srcObject=event.streams[0];else{if(!current.srcObject)current.srcObject=new MediaStream();current.srcObject.addTrack(event.track);}play();};
  peer.onicecandidate=event=>{if(generation===id && ws.readyState===WebSocket.OPEN)ws.send(JSON.stringify({type:'webrtc/candidate',value:event.candidate?.candidate||''}));};
  peer.onconnectionstatechange=()=>{if(peer.connectionState==='failed')fallback(id);};
  ws.onopen=async()=>{
   try{const offer=await peer.createOffer();await peer.setLocalDescription(offer);
    if(generation===id && ws.readyState===WebSocket.OPEN)ws.send(JSON.stringify({type:'webrtc/offer',value:offer.sdp}));
   }catch{fallback(id);}
  };
  ws.onmessage=async event=>{
   if(generation!==id || typeof event.data!=='string')return;
   try{
    const message=JSON.parse(event.data);
    if(message.type==='webrtc/answer'){
     await peer.setRemoteDescription({type:'answer',sdp:message.value});
     for(const value of candidates.splice(0))await peer.addIceCandidate({candidate:value,sdpMid:'0'});
    }else if(message.type==='webrtc/candidate'){
     if(peer.remoteDescription)await peer.addIceCandidate({candidate:message.value,sdpMid:'0'});
     else if(candidates.length<64)candidates.push(message.value);
    }else if(message.type==='error')fallback(id);
   }catch{fallback(id);}
  };
  ws.onclose=()=>fallback(id);
  setTimeout(()=>{if(generation===id && (!current.videoWidth || current.readyState<2))fallback(id);},10000);
 }catch{fallback(id);}
}
async function start(){
 const id=++generation;
 try{
  const response=await fetch('status',{cache:'no-store'});
  if(id!==generation || ended)return;
  if(response.status===410){finish();return;}
  if(!response.ok)throw Error();
  const status=await response.json();if(id!==generation || ended)return;
  rtcAvailable=!!status.webrtc;audioAvailable=!!status.audio;document.querySelector('#sound').hidden=!audioAvailable;connect(true);
 }catch{if(id===generation && !ended){rtcAvailable=false;connect(false);}}
}
document.querySelector('#retry').onclick=()=>{if(ended)return;if(video()?.paused && video()?.readyState>=2 && !video()?.error)play();else start();};
document.querySelector('#sound').onclick=()=>{soundOn=!soundOn;const v=video();if(v){v.muted=!soundOn;v.volume=1;play();}document.querySelector('#sound').textContent=soundOn?'静音':'开启声音';};
document.querySelector('#compat').onclick=()=>connect(false);
document.addEventListener('visibilitychange',()=>{
 if(document.hidden){hiddenAt=Date.now();return;}
 if(!ended && hiddenAt && Date.now()-hiddenAt>4000)start();
 hiddenAt=0;
});
start();
setInterval(async()=>{
 if(ended || checking || document.hidden)return;
 checking=true;
 const id=generation;
 try{
  const response=await fetch('status',{cache:'no-store'});
  if(ended || id!==generation)return;
  if(response.status===410){finish();return;}
  if(!response.ok)throw Error();
  const current=video();let frames=current?.getVideoPlaybackQuality?.().totalVideoFrames||0,jitter;
  if(mode==='webrtc' && pc){
   const metrics=await pc.getStats();if(ended || id!==generation)return;
   metrics.forEach(item=>{if(item.type==='inbound-rtp' && item.kind==='video'){
    frames=item.framesDecoded||frames;
    if(item.jitterBufferEmittedCount)jitter=1000*item.jitterBufferDelay/item.jitterBufferEmittedCount;
   }});
  }
  if(current?.readyState>=2 && (current.currentTime>lastTime || frames>lastFrames)){
   label.textContent='画面播放中';unchanged=0;
   const report={type:'viewer-stats',frames,seconds:current.currentTime,mode};
   if(Number.isFinite(jitter) && jitter>=0 && jitter<60000)report.jitter_buffer_ms=jitter;
   if(current.buffered?.length){const buffer=current.buffered.end(current.buffered.length-1)-current.currentTime;
    if(Number.isFinite(buffer) && buffer>=0 && buffer<3600)report.buffer_seconds=buffer;}
   if(mode==='mse')player.send(report);else if(socket?.readyState===WebSocket.OPEN)socket.send(JSON.stringify(report));
  }else if(!current?.paused && ++unchanged>=3){label.textContent='等待画面 · 可尝试重新连接';}
  lastTime=current?.currentTime??0;lastFrames=frames;
  if(mode==='webrtc' && unchanged>=4 && Date.now()-started>10000)fallback(id);
  else if(mode==='hls' && unchanged>=8 && Date.now()-started>15000)connect(false);
 }catch{if(!ended && generation===id)label.textContent='连接中断，正在重试；相机预览保持开启';}
 finally{checking=false;}
},2000);
</script></body></html>'''
