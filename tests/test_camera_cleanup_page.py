"""Execute the shipped cleanup UI, including asynchronous empty results."""
import json
import shutil
import subprocess
import unittest

from media_importer.camera_page import PAGE


HARNESS = r'''
const vm=require('node:vm'),assert=require('node:assert/strict');
const input=JSON.parse(require('node:fs').readFileSync(0,'utf8'));
class Element {
 constructor(){this.children=[];this.hidden=false;this.disabled=false;this.open=false;this.textContent='';this.classList={toggle(){}};}
 append(...children){for(const child of children){if(child.parentElement)child.parentElement.children=child.parentElement.children.filter(x=>x!==child);child.parentElement=this;this.children.push(child);}}
 replaceChildren(...children){for(const c of this.children)c.parentElement=null;this.children=[];this.append(...children);}
 addEventListener(){} showModal(){this.open=true;} close(){this.open=false;}
}
const elements=new Map(),document={hidden:false,addEventListener(){},createElement(){return new Element();},
 getElementById(id){if(!elements.has(id))elements.set(id,new Element());return elements.get(id);},
 querySelector(selector){return this.getElementById(selector);}};
class Socket {constructor(){this.readyState=1;} send(){throw Error('Unexpected control request');}}
const context=vm.createContext({document,WebSocket:Socket,URL,assert,
 location:{href:'https://preview.example/camera/fixture/'},window:{addEventListener(){}},
 setInterval(){},setTimeout(){return 1;},clearTimeout(){},fixture:input.fixture});
vm.runInContext(input.script,context);
vm.runInContext(input.test,context);
'''


@unittest.skipUnless(shutil.which('node'), 'Node.js is required for browser-side cleanup tests')
class CleanupPageTests(unittest.TestCase):
    def run_js(self, test, *, eligible=0):
        fixture = {'id': 'fixture', 'state': 'ready', 'mode': 'archived', 'eligible_files': eligible,
                   'eligible_bytes': eligible * 32, 'primary_files': eligible, 'auxiliary_files': 0,
                   'pending_archive': 0, 'unknown': 0, 'details': [], 'cancelled': False,
                   'files': [{'name': 'GOPR0001.JPG', 'size': 32}] if eligible else [],
                   'preserved': [] if eligible else [{'paths': ['100GOPRO/GOPR0001.GPR'],
                                                     'reason': '此类型尚未验证无线清理，保留原片'}]}
        result = subprocess.run([shutil.which('node'), '-e', HARNESS], input=json.dumps({
            'script': PAGE.split('<script>', 1)[1].split('</script>', 1)[0],
            'test': test, 'fixture': fixture}), text=True, capture_output=True, timeout=10)
        self.assertEqual(0, result.returncode, result.stderr)

    def test_empty_result_has_no_delete_confirmation_or_false_proof_and_clears_pending_banner(self):
        self.run_js('''
          cleanup=fixture;cleanupReviewRequested=true;message('正在核验清理范围');renderCleanup();
          assert.equal($('message').hidden,true);
          assert.equal($('cleanupTitle').textContent,'暂无可清理文件');
          assert.equal($('cleanupReviewTitle').textContent,'暂无可清理文件');
          assert.equal($('cleanupReview').open,true);
          assert.equal($('confirmCleanup').hidden,true);assert.equal($('confirmCleanup').disabled,true);
          assert.equal($('cleanupTotal').hidden,true);assert.equal($('cleanupPolicy').hidden,true);
          assert.equal($('cancelCleanupReview').textContent,'关闭');
          assert.equal($('cleanupRetained').hidden,false);assert.equal($('cleanupRetained').open,true);
          assert.match($('cleanupRetainedFiles').children[0].textContent,/GOPR0001.GPR.*保留原片/);
        ''')

    def test_scan_errors_are_visible_without_disabling_the_readable_files(self):
        self.run_js('''
          scanErrors=[{path:'100GOPRO/GH010002.MP4',reason:'MP4 元数据边界无效'}];
          renderScanErrors();
          assert.equal($('scanErrors').hidden,false);
          assert.match($('scanErrorSummary').textContent,/1 个文件无法核验/);
          assert.match($('scanErrorFiles').children[0].textContent,/GH010002.MP4/);
          scanErrors=[];renderScanErrors();
          assert.equal($('scanErrors').hidden,true);
        ''')

    def test_confirmable_result_keeps_explicit_delete_and_actual_file_scope(self):
        self.run_js('''
          cleanup=fixture;cleanupReviewRequested=true;message('正在核验清理范围');renderCleanup();
          assert.equal($('message').hidden,true);
          assert.equal($('cleanupReviewTitle').textContent,'确认清理原片');
          assert.equal($('confirmCleanup').hidden,false);assert.equal($('confirmCleanup').disabled,false);
          assert.equal($('cleanupTotal').hidden,false);assert.equal($('cleanupPolicy').hidden,false);
          assert.equal($('cleanupFiles').children.length,1);
          assert.match($('cleanupFiles').children[0].textContent,/GOPR0001.JPG/);
          assert.equal($('cancelCleanupReview').textContent,'取消清理');
        ''', eligible=1)

    def test_async_running_banner_ends_only_when_operation_is_ready(self):
        self.run_js('''
          cleanup={...fixture,state:'running'};cleanupReviewRequested=true;
          message('正在核验清理范围');renderCleanup();
          assert.equal($('message').hidden,false);assert.equal($('cleanupReview').open,false);
          cleanup=fixture;renderCleanup();
          assert.equal($('message').hidden,true);assert.equal($('cleanupReview').open,true);
        ''')

    def test_cleanup_result_does_not_erase_an_unrelated_error(self):
        self.run_js('''
          cleanup=fixture;message('归档连接中断',true);renderCleanup();
          assert.equal($('message').hidden,false);assert.equal($('message').textContent,'归档连接中断');
        ''')
