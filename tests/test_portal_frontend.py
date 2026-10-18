"""真实前端脚本的隔离行为检查；无需浏览器、第三方依赖或网络。"""
from pathlib import Path
import shutil
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which('node')
CHECK = r'''
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const root = process.argv[1];
const group = process.argv[2];
const keys = ['authentications','id','token','nickname','is_supper','permissions','login_type'];
const tick = () => new Promise(resolve => setTimeout(resolve, 0));
function element(value='') {
  return {value,hidden:true,textContent:'',disabled:false,listeners:{},attributes:{},
    addEventListener(event, fn){this.listeners[event]=fn},
    setAttribute(key,value){this.attributes[key]=value},removeAttribute(key){delete this.attributes[key]},
    reportValidity(){return true},focus(){this.focused=true}};
}
function fixture(file, ids, responses, search='') {
  const nodes = Object.fromEntries(ids.map(id=>[id,element()]));
  const values = new Map(keys.map(key=>[key,'old-'+key])); values.set('unrelated','keep');
  const navigation=[], calls=[], writes=[];
  const context = {window:{},document:{getElementById:id=>nodes[id]},URL,URLSearchParams,Headers,
    location:{origin:'https://ops.example',pathname:'/auth/open',search,hash:'#continue',
      assign:path=>navigation.push(path),replace:path=>navigation.push(path)},
    localStorage:{removeItem:key=>{writes.push(['remove',key]);values.delete(key)},
      setItem:(key,value)=>{writes.push(['set',key,value]);values.set(key,value)}},
    fetch:async(...args)=>{calls.push(args);const response=responses.shift();
      assert.ok(response,'unexpected extra request');return {ok:response.status===200,status:response.status,json:async()=>response.data}}};
  vm.createContext(context);
  return {context,nodes,values,navigation,calls,writes,
    run(){vm.runInContext(fs.readFileSync(root+'/management/portal/'+file,'utf8'),context)}};
}
const bridgeIDs=['component-panel','component-title','component-status','component-error','component-retry','component-native-actions','component-native'];
const spug={id:'1',token:'opaque-spug-token',nickname:'管理员',is_supper:'true',permissions:'[]',login_type:'default'};
(async()=>{
  if(group==='bridge') {
    for(const [component,storage,redirect] of [['spug',spug,'/spug/home'],['npm',{authentications:'opaque-npm-session'},'/npm/'],['dbx',{},'/dbx/']]) {
      const f=fixture('component.js',bridgeIDs,[{status:200,data:{storage,redirect}}],'?component='+component);
      f.run();await tick();
      assert.equal(f.navigation[0],redirect);
      assert.deepEqual(JSON.parse(f.calls[0][1].body),{component,next:redirect});
      assert.equal(f.calls[0][1].credentials,'same-origin');
      assert.equal(f.calls[0][1].headers['Content-Type'],'application/json');
      for(const [key,value] of Object.entries(storage))assert.equal(f.values.get(key),value);
      assert.equal(f.values.get('unrelated'),'keep');
      if(component==='dbx')assert.deepEqual(f.writes,[]);
      assert.equal(f.nodes['component-native-actions'].hidden,true);
    }
    const responses=[{status:503,data:{error:'组件暂不可用'}},{status:200,data:{storage:spug,redirect:'/spug/home'}}];
    const retry=fixture('component.js',bridgeIDs,responses,'?component=spug');retry.run();await tick();
    assert.equal(retry.nodes['component-error'].textContent,'组件暂不可用');
    assert.equal(retry.nodes['component-retry'].hidden,false);
    assert.equal(retry.nodes['component-native-actions'].hidden,false);
    await retry.nodes['component-retry'].listeners.click();
    assert.equal(retry.navigation[0],'/spug/home');
    const expired=fixture('component.js',bridgeIDs,[{status:401,data:{}}],'?component=npm&next=%2Fnpm%2F');
    expired.run();await tick();
    assert.equal(expired.navigation[0],'/auth/login?next='+encodeURIComponent('/auth/open?component=npm&next=%2Fnpm%2F#continue'));
    assert.deepEqual(expired.writes,[]);
    const deep=fixture('component.js',bridgeIDs,[{status:200,data:{storage:spug,redirect:'/spug/home'}}],
      '?component=spug&next=%2Fspug%2Fschedule%3Fview%3Dhistory');
    deep.run();await tick();
    assert.equal(JSON.parse(deep.calls[0][1].body).next,'/spug/schedule?view=history');
    assert.equal(deep.navigation[0],'/spug/home');
  }
  if(group==='native') {
    for(const [component,redirect,removed] of [['spug','/spug/login',Object.keys(spug)],['npm','/npm/login',['authentications']],['dbx','/dbx/',[]]]) {
      const f=fixture('component.js',bridgeIDs,[{status:503,data:{error:'原生账号需要重新认证'}},
        {status:200,data:{redirect,storage:{token:'must-not-be-used'}}}],
        '?component='+component+'&next='+encodeURIComponent(redirect));
      f.run();await tick();
      assert.equal(f.nodes['component-native-actions'].hidden,false);
      await f.nodes['component-native'].listeners.click();
      assert.equal(f.calls[1][0],'/auth/api/component-native');
      assert.equal(f.calls[1][1].method,'POST');
      assert.equal(f.calls[1][1].credentials,'same-origin');
      assert.equal(f.calls[1][1].headers['Content-Type'],'application/json');
      assert.deepEqual(JSON.parse(f.calls[1][1].body),{component,next:redirect});
      assert.equal(f.navigation[0],redirect);
      assert.equal(f.writes.some(write=>write[0]==='set'),false);
      for(const key of keys)assert.equal(f.values.has(key),!removed.includes(key));
      assert.equal(f.values.get('unrelated'),'keep');
    }
    const expired=fixture('component.js',bridgeIDs,[{status:503,data:{error:'需要原生认证'}},{status:401,data:{}}],
      '?component=npm&next=%2Fnpm%2Flogin');
    expired.run();await tick();await expired.nodes['component-native'].listeners.click();
    assert.equal(expired.navigation[0],'/auth/login?next='+encodeURIComponent('/auth/open?component=npm&next=%2Fnpm%2Flogin#continue'));
    assert.deepEqual(expired.writes,[]);
  }
  if(group==='boundaries') {
    for(const [component,storage] of [['spug',{...spug,authentications:'foreign'}],['spug',{...spug,token:{bad:true}}],
      ['npm',{}],['dbx',{token:'forbidden'}],['spug',null],['spug',[]]]) {
      const f=fixture('component.js',bridgeIDs,[{status:200,data:{storage,redirect:'/spug/home'}}],'?component='+component);
      f.run();await tick();assert.deepEqual(f.writes,[]);assert.deepEqual(f.navigation,[]);
      assert.equal(f.nodes['component-error'].hidden,false);
    }
    for(const redirect of ['https://other.example','//other.example','//[bad','/npm/','/spug/login']) {
      const f=fixture('component.js',bridgeIDs,[{status:200,data:{storage:spug,redirect}}],'?component=spug');
      f.run();await tick();assert.equal(f.navigation[0],'/spug/home');
    }
    const unknown=fixture('component.js',bridgeIDs,[],'?component=__proto__');unknown.run();await tick();
    assert.deepEqual(unknown.calls,[]);assert.equal(unknown.nodes['component-error'].hidden,false);
    const blocked=fixture('component.js',bridgeIDs,[{status:200,data:{storage:spug,redirect:'/spug/home'}}],'?component=spug');
    blocked.context.localStorage.setItem=()=>{throw new Error('storage blocked')};blocked.run();await tick();
    assert.deepEqual(blocked.navigation,[]);assert.equal(blocked.nodes['component-retry'].hidden,false);
    for(const key of Object.keys(spug))assert.equal(blocked.values.has(key),false);
  }
  if(group==='operator') {
    const ids=['login-form','login-username','login-password','login-submit','login-error'];
    for(const status of [200,401]) {
      const f=fixture('login.js',ids,[{status,data:{redirect:'//[bad',error:'账号或密码不正确'}}],'?next=%2F%2F%5Bbad');
      f.nodes['login-username'].value='admin';f.nodes['login-password'].value='fixture-password';
      f.run();await f.nodes['login-form'].listeners.submit({preventDefault(){}});
      assert.equal(JSON.parse(f.calls[0][1].body).next,'/');
      assert.equal(f.nodes['login-password'].value,'');
      assert.equal(f.values.get('unrelated'),'keep');
      for(const key of keys)assert.equal(f.values.has(key),status!==200);
      assert.deepEqual(f.navigation,status===200?['/']:[]);
    }
    const f=fixture('auth.js',['session-controls','session-user','session-logout','session-error'],
      [{status:200,data:{authenticated:true,user:'admin'}},{status:200,data:{redirect:'/auth/login'}}]);
    f.run();await tick();await f.nodes['session-logout'].listeners.click();
    for(const key of keys)assert.equal(f.values.has(key),false);
    assert.equal(f.values.get('unrelated'),'keep');assert.equal(f.navigation[0],'/auth/login');
  }
  console.log('PASS '+group);
})().catch(error=>{console.error(error);process.exit(1)});
'''


@unittest.skipUnless(NODE, 'requires Node.js for frontend script execution')
class PortalFrontendTests(unittest.TestCase):
    def run_check(self, group):
        result = subprocess.run([NODE, '-e', CHECK, str(ROOT), group], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_component_bridge_real_success_retry_and_expiry_behavior(self):
        self.run_check('bridge')

    def test_storage_and_redirect_trust_boundaries(self):
        self.run_check('boundaries')

    def test_native_login_fallback_uses_grant_and_clears_only_target_component_storage(self):
        self.run_check('native')

    def test_operator_login_and_logout_clear_only_known_component_identities(self):
        self.run_check('operator')


if __name__ == '__main__':
    unittest.main()
