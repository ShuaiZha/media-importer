"""Camera workspace UI. All writes use the existing authorized command queue."""

PAGE = r'''<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>相机 · Media Importer</title>
<style>
:root{color-scheme:light;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;color:#182333;background:#f3f5f8;font-size:14px}
*{box-sizing:border-box}body{margin:0}main{max-width:1320px;margin:auto;padding:24px 28px 32px}
button,select{font:inherit}button{cursor:pointer;border:1px solid #d4dce8;border-radius:9px;background:white;color:#334155;padding:10px 14px;min-height:42px;font-weight:500}
button:hover{background:#eef3ff}button:disabled{opacity:.45;cursor:default}button:focus-visible,select:focus-visible{outline:2px solid #2563eb;outline-offset:3px}
.primary{background:#2563eb;color:white;border-color:#2563eb}.primary:hover{background:#1d4ed8}.danger{color:#b91c1c;border-color:#fecaca;background:#fff5f5}
h1{font-size:23px;margin:4px 0 7px;letter-spacing:-.5px}h2{font-size:15px;margin:0}p{line-height:1.6;margin:8px 0}
.eyebrow{font-size:10px;letter-spacing:1.8px;color:#778398;font-weight:650}header{display:flex;justify-content:space-between;gap:16px;align-items:center}
.muted{color:#718096;font-size:12px}.badge{border-radius:20px;background:#e2e8f0;padding:6px 12px;white-space:nowrap;font-size:12px}.online{background:#dcfce7;color:#166534}
nav{display:flex;gap:6px;margin:20px 0 18px;border-bottom:1px solid #dce2eb;padding-bottom:8px}nav button{border:0;background:transparent;color:#718096}nav button[aria-selected=true]{background:#dfe9ff;color:#1d4ed8}
.layout{display:grid;grid-template-columns:minmax(0,1fr) 336px;gap:20px;align-items:start}
.panel{min-width:0;background:white;border:1px solid #e3e8ef;border-radius:14px;overflow:hidden}.pad{padding:20px}
.topline{display:flex;justify-content:space-between;align-items:center;gap:12px}.preview-head{padding:14px 18px;border-bottom:1px solid #e3e8ef}.preview-head .actions{margin:0}
.screen{box-sizing:content-box;width:100%;background:#0b1220;aspect-ratio:16/9;padding-bottom:88px;position:relative;color:#9eacc0}
.screen iframe{position:absolute;inset:0;display:block;width:100%;height:100%;border:0}
.empty{position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center;text-align:center;padding:24px;gap:10px}
.empty strong{color:#dbe4f0;font-size:17px}.capture-panel{border-top:1px solid #e3e8ef}.capture-panel .topline{align-items:start}
.actions{display:flex;flex-wrap:wrap;gap:8px;margin-top:16px}.metrics{display:flex;flex-wrap:wrap;gap:12px;font-size:13px;color:#526176;margin-top:8px}
.status{font-weight:650;font-size:16px}.dot{display:inline-block;width:8px;height:8px;border-radius:50%;background:#94a3b8;margin-right:8px}.recording .dot{background:#ef4444;box-shadow:0 0 0 4px #fee2e2}
select{width:100%;min-width:0;border:1px solid #d8e0ea;border-radius:8px;background:white;padding:10px;color:#273549;min-height:42px}
label{display:block;color:#526176;font-size:12px;margin-bottom:7px}.field{margin-top:16px}.fieldline{display:flex;gap:8px;align-items:center}.fieldline select{flex:1;min-width:0}.fieldline button{flex:none}
.fields{display:grid;grid-template-columns:minmax(0,1fr)}.fields button{padding:8px 12px;font-size:13px}
.message{padding:12px 16px;border-radius:10px;margin:0 0 18px;background:#e8efff;color:#254ea6;white-space:pre-line;overflow-wrap:anywhere;line-height:1.5}.message.error{background:#fff1e9;color:#9a3412}
.sectionhead{margin-bottom:16px}.grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:16px}
.file{padding:0;text-align:left;overflow:hidden;border-radius:12px}.thumb{aspect-ratio:16/9;background:#e2e8f0;display:flex;justify-content:center;align-items:center;color:#8190a6;overflow:hidden}.thumb img{width:100%;height:100%;object-fit:cover}
.filename{display:block;font-size:14px;color:#1e293b;margin:12px 14px 5px;overflow-wrap:anywhere}.file .muted{display:block;margin:0 14px 14px}.pager{display:flex;justify-content:center;align-items:center;gap:14px;margin-top:20px}
.emptylibrary{text-align:center;padding:40px 12px;color:#718096;line-height:1.6}.libraryhint{font-size:12px;margin:10px 0 18px;color:#718096}
dialog{border:0;border-radius:14px;padding:0;width:min(1000px,95vw);max-height:94vh;background:#101827;color:white;box-shadow:0 24px 80px #0006}dialog::backdrop{background:#0f172aaa}dialog .topline{padding:16px 20px}dialog video,dialog img{display:block;width:100%;max-height:70vh;object-fit:contain;background:black}dialog .muted{padding:0 20px 12px;color:#aebed3}
[hidden]{display:none!important}
@media(min-width:980px){.screen{max-height:calc(100vh - 390px);max-height:calc(100svh - 390px);min-height:220px}}
@media(max-width:979px){main{max-width:820px;padding:20px}.layout{grid-template-columns:minmax(0,1fr)}.fields{grid-template-columns:repeat(2,minmax(0,1fr));gap:0 16px}}
@media(max-width:520px){main{padding:16px 12px 24px}.layout{gap:16px}.grid{grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}.pad{padding:16px}.preview-head{padding:12px 16px}.fields{grid-template-columns:minmax(0,1fr)}header{align-items:start}h1{font-size:21px}.topline{gap:8px}.capture-panel .actions>button{flex:1}.preview-head button{padding:8px 12px}button{min-height:44px}.screen{padding-bottom:96px}}
</style><main>
<header><div><div class="eyebrow">MEDIA IMPORTER</div><h1 id="cameraName">相机</h1><div class="muted" id="connection">正在连接控制服务</div></div><span id="badge" class="badge">连接中</span></header>
<nav aria-label="相机工作区"><button id="shootTab" aria-selected="true">拍摄</button><button id="mediaTab" aria-selected="false">相机素材</button></nav>
<div id="message" class="message" role="status" hidden></div>
<section id="shoot" class="layout"><div class="panel view-panel"><div class="topline preview-head"><h2>实时取景</h2><div class="actions"><button id="previewStart">开启预览</button><button id="previewStop" hidden disabled>结束预览</button></div></div><div id="screen" class="screen"><div class="empty"><strong>预览未开启</strong></div></div><div class="pad capture-panel">
<div class="topline"><div id="shootState" class="status"><span class="dot"></span>等待相机状态</div><button id="refreshState">刷新状态</button></div>
<div id="metrics" class="metrics"></div><p id="specification" class="muted"></p>
<div class="actions"><button id="shutter" class="primary" disabled>开始录像</button><button id="stopRecord" class="danger" disabled>停止录像</button><button id="hilight" hidden>HiLight 标记</button></div>
<p id="previewHint" class="muted" hidden></p></div></div>
<aside class="panel pad"><div class="topline"><h2>拍摄参数</h2><button id="readSettings">读取参数</button></div>
<div id="settingsEmpty" class="emptylibrary">读取相机支持的拍摄参数</div><div id="settings" hidden><div class="field"><label for="preset">相机预设</label><div class="fieldline"><select id="preset"></select><button id="applyPreset">应用</button></div></div><div id="parameters" class="fields"></div></div></aside></section>
<section id="library" class="panel pad" hidden><div class="topline sectionhead"><div><h2>相机素材</h2><p id="libraryCount" class="muted"></p></div><button id="refreshLibrary">刷新素材</button></div><p id="libraryHint" class="libraryhint"></p><div id="files" class="grid"></div><div id="libraryEmpty" class="emptylibrary">点击“刷新素材”读取相机清单</div><div class="pager" id="pager" hidden><button id="previous">上一页</button><span id="pageNumber"></span><button id="next">下一页</button></div></section>

</main><dialog id="replay"><div class="topline"><strong id="replayName"></strong><button id="closeReplay">关闭</button></div><div id="replayContent"></div><p id="replayHint" class="muted"></p></dialog>
<script>
'use strict';
const $=id=>document.getElementById(id), wait=ms=>new Promise(r=>setTimeout(r,ms));
let socket, sequence=0, pending=new Map(), state, busy=false, operations=0, stopping=false, tab='shoot', files=[], page=0, iframePath='', catalogKey='', querying=false, mediaSession=null, reconnecting=false;
const nonce=()=>Array.from(crypto.getRandomValues(new Uint8Array(16)),v=>v.toString(16).padStart(2,'0')).join('');
function message(text,error=false){$('message').hidden=!text;$('message').textContent=text;$('message').classList.toggle('error',error);}
function size(n){if(n<1048576)return(n/1024).toFixed(1)+' KiB';if(n<1073741824)return(n/1048576).toFixed(1)+' MiB';return(n/1073741824).toFixed(1)+' GiB';}
function call(request){return new Promise((resolve,reject)=>{if(socket?.readyState!==1){reject(Error('控制连接已断开，请重新打开页面'));return;}const id=++sequence,timer=setTimeout(()=>{pending.delete(id);reject(Error('等待结果超时，请刷新相机状态；不会自动重复操作'));},45000);pending.set(id,{resolve,reject,timer});socket.send(JSON.stringify({id,request}));});}
async function refresh(){if(querying)return;querying=true;try{state=await call({action:'status'});render();}finally{querying=false;}}
function openSocket(){
 socket=new WebSocket(new URL('control',location.href).href.replace(/^http/,'ws'));
 socket.onopen=()=>{reconnecting=false;refresh().catch(e=>message(e.message,true));};
 socket.onmessage=e=>{try{const v=JSON.parse(e.data),p=pending.get(v.id);if(!p)return;pending.delete(v.id);clearTimeout(p.timer);v.error?p.reject(Error(v.error)):p.resolve(v.result);}catch{message('控制响应无效，请重新打开页面',true);}};
 socket.onclose=()=>{for(const p of pending.values()){clearTimeout(p.timer);p.reject(Error('控制连接中断。操作不会自动重发，请核对实际状态。'));}pending.clear();$('badge').textContent='已断开';$('badge').className='badge';if(!reconnecting){reconnecting=true;setTimeout(openSocket,3000);}};
}
function previewActive(){return state && (['queued','connecting','starting','ready','stopping'].includes(state.preview.state)||state.preview.cleanup_pending);}
function render(){if(!state)return;const c=state.state||{},commands=state.commands||[],recording=c.recording===true,unknown=c.stale;
 $('cameraName').textContent=state.device.name;$('connection').textContent=(state.device.transport==='wifi'?'Wi-Fi':state.device.transport==='usb'?'USB':'等待连接')+' · '+(state.sync_enabled?'同步已开启':'同步已关闭');$('badge').textContent=state.device.online?'已连接':'等待相机';$('badge').className='badge'+(state.device.online?' online':'');
 $('shootState').classList.toggle('recording',recording);$('shootState').replaceChildren(Object.assign(document.createElement('span'),{className:'dot'}),document.createTextNode(unknown?'状态待刷新':recording?'拍摄中':c.busy?'相机忙碌':'待机'));
 const metrics=[];if(c.battery_percent!=null)metrics.push('电量 '+c.battery_percent+'%');if(c.storage_remaining_bytes!=null)metrics.push('存储卡剩余 '+size(c.storage_remaining_bytes));if(recording && c.recording_seconds!=null)metrics.push('已录制 '+c.recording_seconds+' 秒');if(c.overheating)metrics.push('过热告警');$('metrics').textContent=metrics.join(' · ');$('specification').textContent=c.specification||'';
 const action=c.mode==='photo'?'photo':c.mode==='timelapse'?'start_timelapse':'start_recording';$('shutter').textContent=c.mode==='photo'?'拍照':c.mode==='timelapse'?'开始延时摄影':'开始录像';if(previewActive())$('shutter').textContent='结束预览并'+$('shutter').textContent;
 $('shutter').disabled=busy||recording||!commands.includes(action);$('shutter').hidden=recording;$('stopRecord').hidden=!recording&&!unknown&&!stopping;$('stopRecord').disabled=stopping||!commands.includes('stop_recording');$('stopRecord').textContent=c.mode==='timelapse'?'停止拍摄':'停止录像';$('hilight').hidden=!recording||c.mode!=='video';$('hilight').disabled=busy||!commands.includes('hilight');
 $('previewStart').hidden=previewActive();$('previewStop').hidden=!previewActive();$('previewStart').disabled=busy||recording||previewActive()||state.device.transport!=='wifi';$('previewStop').disabled=busy||!previewActive();$('readSettings').disabled=busy||recording||previewActive()||!commands.includes('catalog');$('applyPreset').disabled=busy||recording||previewActive();
 $('previewHint').hidden=!(previewActive()||recording);$('previewHint').textContent=recording?'关闭页面不会停止录像':'关闭页面不会结束预览';
 const path=state.preview.path||'';if(path && $('message').textContent==='正在开启预览，画面准备好后自动显示')message('');if(path!==iframePath || !path){iframePath=path;$('screen').replaceChildren();if(path){const f=document.createElement('iframe');f.title='相机实时画面';f.allow='autoplay; fullscreen';f.src=path+'#embedded';$('screen').append(f);}else{const e=document.createElement('div');e.className='empty';const strong=document.createElement('strong');strong.textContent=previewActive()?'正在准备画面':'预览未开启';e.append(strong);if(previewActive())e.append(document.createTextNode(state.preview.reason||'等待相机连接'));$('screen').append(e);}}
 const catalog=state.catalog,key=JSON.stringify(catalog);if(key!==catalogKey){catalogKey=key;renderSettings(catalog);}for(const el of $('parameters').querySelectorAll('button,select'))el.disabled=busy||recording||previewActive();
 if(mediaSession && mediaSession!==state.session_id){mediaSession=null;files=[];closeReplay();renderFiles();message('相机连接已变化，请重新刷新素材',true);}
}
function renderSettings(catalog){$('settings').hidden=!catalog;$('settingsEmpty').hidden=!!catalog;if(!catalog)return;$('preset').replaceChildren(...catalog.presets.map(p=>new Option(({'video':'视频','photo':'照片','timelapse':'延时'}[p.mode]||'')+' · '+p.label,p.id)));$('preset').value=catalog.preset;$('parameters').replaceChildren();for(const p of catalog.parameters){const field=document.createElement('div');field.className='field';const label=document.createElement('label');label.textContent=p.label;const line=document.createElement('div');line.className='fieldline';const select=document.createElement('select');select.setAttribute('aria-label',p.label);select.append(...p.options.map(o=>new Option(o.label,o.value)));select.value=p.value;const button=document.createElement('button');button.textContent='应用';button.onclick=()=>execute(()=>capture('set_parameter',{key:p.key,value:Number(select.value),expected_preset:catalog.preset},true));line.append(select,button);field.append(label,line);$('parameters').append(field);}}
async function execute(fn,priority=false){if(busy&&!priority)return;operations++;busy=true;render();message('正在处理');try{await fn();await refresh();}catch(e){message(e.message,true);}finally{operations--;busy=operations>0;render();}}
async function capture(command,parameters={},reload=false){await releaseLibrary();const op=await call({action:'capture',command,parameters,session_id:state.session_id,nonce:nonce()});let done=false;for(let i=0;i<60;i++){await wait(1000);const r=await call({action:'result',id:op.id});if(r.state==='done'){if(r.result.error||r.result.uncertain)throw Error(r.result.error||r.result.reason||'结果待核验，请刷新状态');done=true;break;}}if(!done)throw Error('操作仍在执行，请稍后刷新状态；不会重复发送');if(reload)await capture('catalog');else message(command==='catalog'?'拍摄参数已更新':'操作已完成');}
async function releaseLibrary(){if(mediaSession){try{await call({action:'library_release',session_id:mediaSession});}catch{}mediaSession=null;}}
async function stopPreview(){await call({action:'preview_stop',id:state.preview.id});for(let i=0;i<45;i++){await wait(1000);await refresh();if(!previewActive())return;}throw Error('预览停止结果待核验，请稍后刷新');}
async function switchTab(next){closeReplay();if(next==='shoot'){await releaseLibrary();message('');}tab=next;$('shoot').hidden=next!=='shoot';$('library').hidden=next!=='library';$('shootTab').setAttribute('aria-selected',next==='shoot');$('mediaTab').setAttribute('aria-selected',next==='library');if(next==='library'){if(previewActive()){message('浏览相机素材需要先结束实时预览。');$('refreshLibrary').textContent='结束预览并浏览';}else await loadLibrary();}}
async function loadLibrary(){if(previewActive())await stopPreview();$('refreshLibrary').textContent='刷新素材';mediaSession=state.session_id;message('正在读取素材');const list=await call({action:'library_list',session_id:mediaSession});files=list.files;page=0;renderFiles();message('');}
function renderFiles(){$('files').replaceChildren();$('libraryCount').textContent=files.length+' 个文件';$('libraryEmpty').hidden=files.length>0;$('libraryEmpty').textContent=mediaSession?'相机中没有可浏览的视频或照片':'请刷新素材';$('pager').hidden=files.length<=12;$('pageNumber').textContent=(page+1)+' / '+Math.max(1,Math.ceil(files.length/12));$('previous').disabled=page===0;$('next').disabled=(page+1)*12>=files.length;for(const f of files.slice(page*12,(page+1)*12)){const b=document.createElement('button');b.className='file';const thumb=document.createElement('div');thumb.className='thumb';const img=document.createElement('img');img.alt=f.name+' 缩略图';img.loading='lazy';img.src='thumbnail/'+f.id;img.onerror=()=>{img.remove();thumb.textContent=f.kind==='video'?'视频 · 点击回放':'照片 · 点击查看';};thumb.append(img);const name=document.createElement('strong');name.className='filename';name.textContent=f.name;const info=document.createElement('span');info.className='muted';info.textContent=(f.kind==='video'?'视频':'照片')+' · '+size(f.size);b.append(thumb,name,info);b.onclick=()=>openReplay(f);$('files').append(b);}}
function closeReplay(){const video=$('replayContent').querySelector('video');if(video){video.pause();video.removeAttribute('src');video.load();}$('replayContent').replaceChildren();if($('replay').open)$('replay').close();}
function openReplay(f){closeReplay();$('replayName').textContent=f.name;const media=document.createElement(f.kind==='video'?'video':'img');if(f.kind==='video'){media.controls=true;media.playsInline=true;media.preload='metadata';media.src='media/'+f.id;media.onerror=()=>{$('replayHint').textContent='此文件暂无法播放。请刷新相机连接；若原文件使用此浏览器不支持的编码，请使用已归档文件播放。';};media.onplaying=()=>{$('replayHint').textContent=f.quality};}else{media.src='thumbnail/'+f.id;media.alt=f.name;}$('replayContent').append(media);$('replayHint').textContent=f.kind==='video'?f.quality+' · 正在读取画面':'照片缩略图';$('replay').showModal();if(f.kind==='video')media.play().catch(()=>{$('replayHint').textContent='请点击播放按钮 · '+f.quality;});}
$('shootTab').onclick=()=>execute(()=>switchTab('shoot'));$('mediaTab').onclick=()=>execute(()=>switchTab('library'));
$('refreshState').onclick=()=>execute(()=>capture('state'));$('readSettings').onclick=()=>execute(()=>capture('catalog'));$('applyPreset').onclick=()=>execute(()=>capture('load_preset',{preset:Number($('preset').value)},true));
$('shutter').onclick=()=>execute(()=>{let command=state.state.mode==='photo'?'photo':state.state.mode==='timelapse'?'start_timelapse':'start_recording';if(previewActive())command={photo:'preview_photo',start_timelapse:'preview_timelapse',start_recording:'preview_record'}[command];return capture(command);});$('stopRecord').onclick=()=>execute(async()=>{stopping=true;render();try{await capture('stop_recording');}finally{stopping=false;}},true);$('hilight').onclick=()=>execute(()=>capture('hilight'));
$('previewStart').onclick=()=>execute(async()=>{await releaseLibrary();await call({action:'preview_start',nonce:nonce()});message('正在开启预览，画面准备好后自动显示');});$('previewStop').onclick=()=>execute(async()=>{await stopPreview();message('预览已结束');});
$('refreshLibrary').onclick=()=>execute(loadLibrary);$('previous').onclick=()=>{page--;renderFiles();};$('next').onclick=()=>{page++;renderFiles();};$('closeReplay').onclick=closeReplay;$('replay').addEventListener('cancel',closeReplay);
document.addEventListener('visibilitychange',()=>{if(document.hidden){closeReplay();releaseLibrary();}else if(tab==='library')execute(loadLibrary);});
setInterval(()=>{if(!document.hidden)refresh().catch(e=>message(e.message,true));},2500);
setInterval(()=>{if(tab==='library'&&mediaSession&&!document.hidden)call({action:'library_touch',session_id:mediaSession}).catch(e=>message(e.message,true));},8000);
window.addEventListener('pagehide',()=>{releaseLibrary();closeReplay();socket?.close();});openSocket();
</script></html>'''
