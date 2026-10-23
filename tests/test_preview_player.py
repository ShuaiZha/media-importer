"""Execute the shipped player, including negotiation, fallback and revocation."""
import json
import re
import shutil
import subprocess
import unittest

from media_importer.preview_player import PAGE

HARNESS = r'''
const vm=require('node:vm'),assert=require('node:assert/strict');
const input=JSON.parse(require('node:fs').readFileSync(0,'utf8'));
let clock=10000;
class Element {
 constructor(){this.paused=true;this.readyState=0;this.currentTime=0;this.videoWidth=0;this.buffered={length:0};this.textContent='';}
 canPlayType(){return input.native?'maybe':'';}
 play(){this.paused=false;return Promise.resolve();} pause(){this.paused=true;}
 load(){} remove(){this.removed=true;} removeAttribute(){} replaceChildren(child){this.child=child;}
 getVideoPlaybackQuality(){return {totalVideoFrames:30};} send(value){this.sent=value;}
}
const elements=new Map(),document={hidden:false,events:{},
 createElement(name){const element=new Element();if(name==='viewer-video')element.video=new Element();return element;},
 querySelector(name){if(!elements.has(name))elements.set(name,new Element());return elements.get(name);},
 addEventListener(name,fn){this.events[name]=fn;}};
class Socket {static OPEN=1;constructor(){this.readyState=1;this.messages=[];}close(){this.readyState=3;this.closed=true;}send(value){this.messages.push(JSON.parse(value));}}
class Peer {
 constructor(config){this.config=config;this.connectionState='new';this.candidates=[];this.transceivers=[];}
 addTransceiver(kind,options){this.transceivers.push({kind,...options});}
 async createOffer(){return {sdp:'fixture-offer',type:'offer'};}
 async setLocalDescription(value){this.localDescription=value;}
 async setRemoteDescription(value){this.remoteDescription=value;}
 async addIceCandidate(value){this.candidates.push(value);}
 async getStats(){return new Map([['video',{type:'inbound-rtp',kind:'video',framesDecoded:60,jitterBufferDelay:.6,jitterBufferEmittedCount:60}]]);}
 close(){this.closed=true;}
}
const intervals=[],timers=[];
const context=vm.createContext({document,WebSocket:Socket,RTCPeerConnection:Peer,MediaStream:class{},MediaSource:class{},
 VideoRTC:class{},customElements:{define(){}},URL,location:{href:'https://preview.example/view/session/'},
 Date:{now:()=>clock},setInterval(fn,delay){intervals.push({fn,delay});},setTimeout(fn,delay){timers.push({fn,delay});},
 fetch:async()=>({ok:true,status:200,json:async()=>({webrtc:input.rtc,audio:input.audio})}),assert,
 advance(ms){clock+=ms;},intervals,timers});
vm.runInContext(input.script,context);
(async()=>{for(let i=0;i<8;i++)await Promise.resolve();await vm.runInContext(input.test,context);})()
.catch(error=>{console.error(error);process.exitCode=1;});
'''


@unittest.skipUnless(shutil.which('node'), 'Node.js is required for browser-side playback tests')
class PlaybackTests(unittest.TestCase):
    def run_js(self, code, native=True, rtc=True, audio=False):
        script=PAGE.split('<script type="module">',1)[1].split('</script>',1)[0]
        script=re.sub(r'^import .*;$','',script,flags=re.M)
        result=subprocess.run([shutil.which('node'),'-e',HARNESS],
                              input=json.dumps({'script':script,'test':code,'native':native,'rtc':rtc,'audio':audio}),
                              text=True,capture_output=True,timeout=10)
        self.assertEqual(0,result.returncode,result.stderr)

    def test_live_audio_is_receive_only_and_requires_explicit_unmute(self):
        self.run_js('''(async()=>{
          assert.equal(pc.transceivers.length,2);
          assert.equal(pc.transceivers[1].kind,'audio');
          assert.equal(pc.transceivers[1].direction,'recvonly');
          assert.equal(player.muted,true);
          assert.equal(document.querySelector('#sound').hidden,false);
          document.querySelector('#sound').onclick();
          assert.equal(player.muted,false);assert.equal(player.volume,1);
          document.querySelector('#sound').onclick();assert.equal(player.muted,true);
        })()''', audio=True)

    def test_webrtc_is_receive_only_without_public_ice_services(self):
        self.run_js('''(async()=>{
          assert.equal(mode,'webrtc');assert.equal(pc.config.iceServers.length,0);
          assert.equal(pc.transceivers.length,1);assert.equal(pc.transceivers[0].kind,'video');
          assert.equal(pc.transceivers[0].direction,'recvonly');
          await socket.onopen();assert.equal(socket.messages[0].type,'webrtc/offer');
          assert.match(modeLabel.textContent,/低延迟/);
        })()''')

    def test_candidates_wait_for_answer_and_track_is_played(self):
        self.run_js('''(async()=>{
          await socket.onmessage({data:JSON.stringify({type:'webrtc/candidate',value:'fixture'})});
          assert.equal(pc.candidates.length,0);
          await socket.onmessage({data:JSON.stringify({type:'webrtc/answer',value:'answer'})});
          assert.equal(pc.candidates.length,1);
          const stream={fixture:true};pc.ontrack({streams:[stream]});assert.equal(video().srcObject,stream);
          assert.equal(video().paused,false);
        })()''')

    def test_timeout_falls_back_once_and_releases_peer(self):
        self.run_js(r'''const old=pc;timers[0].fn();assert.equal(old.closed,true);
        assert.equal(mode,'hls');assert.match(player.src,/hls\/master.m3u8$/);
        const current=player;timers[0].fn();assert.equal(player,current);
        assert.match(modeLabel.textContent,/已自动切换/);''')

    def test_playing_peer_is_not_replaced_by_startup_timer(self):
        self.run_js('''player.videoWidth=848;player.readyState=4;
        timers[0].fn();assert.equal(mode,'webrtc');assert.equal(pc.closed,undefined);''')

    def test_failed_connection_and_socket_close_fall_back(self):
        for action in ("pc.connectionState='failed';pc.onconnectionstatechange()",'socket.onclose()'):
            self.run_js(action+";assert.equal(mode,'hls');assert.equal(pc,null);")

    def test_manual_fallback_and_retry_are_effective(self):
        self.run_js('''(async()=>{
        const previous=pc;document.querySelector('#compat').onclick();assert.equal(mode,'hls');assert.equal(previous.closed,true);
        await start();assert.equal(mode,'webrtc');
        })()''')

    def test_advertised_hls_support_with_decoder_failure_falls_back_to_mse(self):
        self.run_js('''document.querySelector('#compat').onclick();const previous=player;
        const failure=previous.onerror;failure();assert.equal(mode,'mse');assert.equal(previous.removed,true);
        const current=player;failure();assert.equal(player,current);assert.equal(player.mode,'mse');''')

    def test_hls_failure_without_mse_is_explicit_and_retry_reconnects(self):
        self.run_js('''(async()=>{
        MediaSource=undefined;document.querySelector('#compat').onclick();
        player.onerror();assert.equal(label.textContent,'兼容播放失败，请重新连接');
        player.error={code:3};player.paused=true;player.readyState=2;
        document.querySelector('#retry').onclick();for(let i=0;i<8;i++)await Promise.resolve();
        assert.equal(mode,'webrtc');
        })()''')

    def test_old_async_negotiation_does_not_replace_new_player(self):
        self.run_js('''(async()=>{
        const old=socket;document.querySelector('#compat').onclick();const current=player;
        await old.onmessage({data:JSON.stringify({type:'error',value:'old failure'})});
        assert.equal(player,current);assert.equal(mode,'hls');
        })()''')

    def test_server_without_rtc_and_browser_without_rtc_use_compatibility(self):
        self.run_js("assert.equal(mode,'hls');assert.equal(pc,undefined);",rtc=False)
        self.run_js("assert.equal(mode,'mse');assert.equal(player.mode,'mse');",native=False,rtc=False)
        self.run_js("RTCPeerConnection=undefined;connect();assert.equal(mode,'hls');")

    def test_revocation_closes_peer_and_cannot_be_reopened(self):
        self.run_js('''(async()=>{
        const old=pc;fetch=async()=>({status:410,ok:false});await intervals[0].fn();
        assert.equal(ended,true);assert.equal(old.closed,true);assert.equal(label.textContent,'预览已结束');
        const previous=player;document.querySelector('#compat').onclick();document.querySelector('#retry').onclick();
        assert.equal(player,previous);
        })()''')

    def test_report_uses_decoded_frames_not_estimated_end_to_end_delay(self):
        self.run_js('''(async()=>{
        player.readyState=4;player.currentTime=2;player.paused=false;await intervals[0].fn();
        const stats=socket.messages.at(-1);assert.equal(stats.frames,60);assert.equal(stats.mode,'webrtc');
        assert.equal(stats.jitter_buffer_ms,10);assert.equal(stats.latency,undefined);
        assert.equal(label.textContent,'画面播放中');
        })()''')

    def test_returning_from_background_reconnects_but_does_not_end_preview(self):
        self.run_js('''(async()=>{
        const old=pc;document.hidden=true;document.events.visibilitychange();advance(5000);
        document.hidden=false;document.events.visibilitychange();for(let i=0;i<8;i++)await Promise.resolve();
        assert.notEqual(pc,old);assert.equal(old.closed,true);assert.equal(ended,false);
        })()''')
