import {readFile} from 'node:fs/promises';
import assert from 'node:assert/strict';
import test from 'node:test';
import ts from 'typescript';

const source=await readFile(new URL('../src/cooldown.ts',import.meta.url),'utf8');
const {outputText}=ts.transpileModule(source,{compilerOptions:{target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.ES2022}});
const {poolCooldown,formatWait}=await import(`data:text/javascript;base64,${Buffer.from(outputText).toString('base64')}`);
const account=(until,overrides={})=>({enabled:1,auth_failed:0,expired:0,cooldown_kind:'quota',cooldown_until:until,...overrides});

test('earliest eligible recovery excludes disabled, invalid, expired and elapsed accounts',()=>{
  assert.deepEqual(poolCooldown([account(180),account(150),account(101,{enabled:0}),account(102,{auth_failed:1}),account(103,{expired:1}),account(90)],100),{count:2,until:150,exact:true});
});
test('unknown recovery never invents a pool minimum',()=>{
  assert.deepEqual(poolCooldown([account(null),account(150)],100),{count:2,until:150,exact:false});
  assert.deepEqual(poolCooldown([account(null)],100),{count:1,until:null,exact:false});
  assert.deepEqual(poolCooldown([],100),{count:0,until:null,exact:true});
});
test('countdown crosses minute and day boundaries without becoming negative',()=>{
  assert.equal(formatWait(161,100),'1分 1秒');
  assert.equal(formatWait(161,102),'59秒');
  assert.equal(formatWait(161,162),'0秒');
  assert.equal(formatWait(90161,100),'1天 1小时 1分 1秒');
});

test('model holds use earliest recoverable route and combine account cooldown',()=>{
  const models={models:['a','b'],model_mapping:{a:'real-a'},model_blocks:[{model:'real-a',retry_at:130},{model:'b',retry_at:150}]};
  assert.deepEqual(poolCooldown([account(null,{...models,cooldown_kind:null})],100),{count:1,until:130,exact:true});
  assert.deepEqual(poolCooldown([account(140,models)],100),{count:1,until:140,exact:true});
  assert.deepEqual(poolCooldown([account(null,{...models,cooldown_kind:null,models:['a','b','available']})],100),{count:0,until:null,exact:true});
});

test('manual account holds are not invented quota cooldowns',()=>{
  assert.deepEqual(poolCooldown([account(null,{cooldown_kind:'account'})],100),{count:0,until:null,exact:true});
  const models={models:['a','b'],model_blocks:[{model:'a',retry_at:null},{model:'b',retry_at:150}]};
  assert.deepEqual(poolCooldown([account(null,{...models,cooldown_kind:null})],100),{count:1,until:150,exact:false});
});
