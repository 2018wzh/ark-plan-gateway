import {readFile} from 'node:fs/promises';
import assert from 'node:assert/strict';
import test from 'node:test';
import ts from 'typescript';
const source=await readFile(new URL('../src/quotaSummary.ts',import.meta.url),'utf8');
const {outputText}=ts.transpileModule(source,{compilerOptions:{target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.ES2022}});
const {quotaSummary}=await import(`data:text/javascript;base64,${Buffer.from(outputText).toString('base64')}`);
const account=(group,plan,usage,extra={})=>({quota_group:group,plan,usage,enabled:1,expired:0,quota_checked_at:100,...extra});
const window=(quota,used)=>({quota,used,reset_time:200});
test('cross-plan ratios use equal subject weights and deduplicate keys by latest window',()=>{
 const rows=quotaSummary([account('a','agent',{AFPFiveHour:window(1000,100)}),account('a','agent',{AFPFiveHour:window(1000,300)},{quota_checked_at:110}),account('b','coding',{session:window(100,50)})]);
 assert.equal(rows[0].percent,60);assert.equal(rows[0].known,2);assert.equal(rows[0].afpRemaining,700);assert.equal(rows[0].afpTotal,1000);
 assert.equal(rows[1].unknown,1);assert.equal(rows[1].percent,null);
});
test('unknown, stale and exhausted quotas remain distinct; disabled and expired excluded',()=>{
 const rows=quotaSummary([account('a','agent',{AFPDaily:window(10,12)},{quota_error:'failed'}),account('b','agent',{}),account('c','agent',{AFPDaily:window(10,0)},{enabled:0}),account('d','agent',{AFPDaily:window(10,0)},{expired:1})]);
 assert.equal(rows[1].percent,0);assert.equal(rows[1].unknown,1);assert.equal(rows[1].exhausted,1);assert.equal(rows[1].stale,1);assert.equal(rows[1].afpRemaining,0);
});
test('empty and invalid windows cannot become full quota',()=>{
 assert.equal(quotaSummary([]).length,4);
 const rows=quotaSummary([account('a','coding',{session:window(0,0),weekly:window(100,NaN)})]);
 assert.equal(rows[0].percent,null);assert.equal(rows[0].unknown,1);assert.equal(rows[2].percent,null);assert.equal(rows[1].unknown,0);
});
