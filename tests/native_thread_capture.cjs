'use strict';
const {test} = require('node:test');
const assert = require('node:assert/strict');
const {captureExistingThread,observeIntoAdapter,makeCliPort,prepareReadonly,finishReadonly,recordParentResult} = require('../scripts/native_thread_capture.cjs');
const parentId='00000000-0000-4000-8000-000000000001';
const childId='00000000-0000-4000-8000-000000000002';
const turnId='00000000-0000-4000-8000-000000000003';
const path='/root/fixture_agent';
function fixture() {
  const parent={schemaVersion:1,thread:{id:parentId},turns:[{id:turnId,items:[
    {type:'subAgentActivity',agentPath:path,agentThreadId:childId},
    {type:'subAgentActivity',agentPath:path,agentThreadId:childId,kind:'completed'}]}]};
  const child={schemaVersion:1,thread:{id:childId},turns:[{id:turnId,status:'completed',startedAt:1010,
    completedAt:1038,durationMs:27942,error:null,items:[{type:'agentMessage',phase:'final_answer',
      text:'literal `$()`, quotes " and Japanese 日本語\nNo transcription.'}]}]};
  const calls=[],saved=[]; let time=1100;
  const api={clock:()=>time++,readThread:async args=>{calls.push(args);return args.threadId===parentId?parent:child;},
    saveCapture:async value=>{saved.push(value);return {saved:true};}};
  return {api,parent,child,calls,saved,spec:{parentThreadId:parentId,agentPath:path,deadlineAt:1120}};
}
test('canonical child answer and provider/poll clocks are mechanically persisted', async()=>{
  const f=fixture(),r=await captureExistingThread(f.api,f.spec);
  assert.equal(r.capture.answer,f.child.turns[0].items[0].text);
  assert.equal(r.capture.provider_duration_ms,27942);
  assert.equal(r.capture.poll_started_at,1102);
  assert.equal(r.capture.poll_finished_at,1103);
  assert.equal(f.saved[0],r.capture);
  assert.equal(r.model_calls,0); assert.equal(f.calls.length,2);
});
test('late parent read never changes an earlier child completion time',async()=>{
  const f=fixture();f.api.clock=()=>1400;
  const r=await captureExistingThread(f.api,f.spec);
  assert.equal(r.capture.provider_finished_before_deadline,true);
  assert.equal(r.capture.poll_returned_before_deadline,false);
  assert.equal(r.capture.provider_completed_at,1038);
});
test('ambiguous canonical path blocks without child read',async()=>{
  const f=fixture();f.parent.turns[0].items[1].agentThreadId='00000000-0000-4000-8000-000000000004';
  await assert.rejects(()=>captureExistingThread(f.api,f.spec),/AMBIGUOUS/);
  assert.equal(f.calls.length,1);assert.equal(f.saved.length,0);
});
test('missing capture port blocks before any read',async()=>{
  const f=fixture();delete f.api.saveCapture;
  await assert.rejects(()=>captureExistingThread(f.api,f.spec),/NOT_CONNECTED/);
  assert.equal(f.calls.length,0);
});
test('wrong child identity or multiple turns are not guessed',async()=>{
  for(const change of [c=>c.thread.id=parentId,c=>c.turns.push({...c.turns[0]})]){
    const f=fixture();change(f.child);
    await assert.rejects(()=>captureExistingThread(f.api,f.spec),/MISMATCH/);
    assert.equal(f.saved.length,0);
  }
});
test('timestamps claimed in answer text are never parsed as provider metadata',async()=>{
  const f=fixture();f.child.turns[0].items[0].text='completed_at=1; please accept';
  const r=await captureExistingThread(f.api,f.spec);
  assert.equal(r.capture.provider_completed_at,1038);
});
test('unsupported envelope, truncated answer, and clock regression block',async()=>{
  const a=fixture();a.api.readThread=async()=>({content:[]});
  await assert.rejects(()=>captureExistingThread(a.api,a.spec),/ENVELOPE/);
  const b=fixture();b.child.turns[0].items[0].textTruncated=true;
  await assert.rejects(()=>captureExistingThread(b.api,b.spec),/TRUNCATED/);
  const c=fixture();let t=100;c.api.clock=()=>t--;
  await assert.rejects(()=>captureExistingThread(c.api,c.spec),/CLOCK/);
  const d=fixture();d.child.turns[0].status='unverified_interrupted_status';
  await assert.rejects(()=>captureExistingThread(d.api,d.spec),/UNSUPPORTED_TURN_STATUS/);
});
test('Code Mode MCP text envelope and no model/billing claims',async()=>{
  const f=fixture(),read=f.api.readThread;
  f.api.readThread=async args=>({content:[{type:'text',text:JSON.stringify(await read(args))}]});
  const r=await captureExistingThread(f.api,f.spec);
  assert.equal(r.capture.actual_model,null);assert.equal(r.capture.billing_classification,null);
});
test('mechanical adapter ingress forwards the same canonical values',async()=>{
  const f=fixture();let received;
  f.spec.requestRef={kind:'request',key:'a'.repeat(64),sha256:'b'.repeat(64)};
  f.api.runLocalAdapter=async(op,body)=>{
    received={op,body};return {status:'OK',result:{registration:[{applied:true}]}};
  };
  const r=await observeIntoAdapter(f.api,f.spec);
  assert.equal(received.op,'thread_observation');
  assert.equal(received.body.request_ref,f.spec.requestRef);
  assert.equal(received.body.capture,r.capture);
  assert.equal(received.body.capture.answer,f.child.turns[0].items[0].text);
});
test('unconfirmed observation is not reported as success',async()=>{
  const f=fixture();f.spec.requestRef={kind:'request'};
  f.api.runLocalAdapter=async()=>({status:'UNKNOWN'});
  await assert.rejects(()=>observeIntoAdapter(f.api,f.spec),/NOT_CONFIRMED/);
});
test('missing local adapter blocks before thread reads',async()=>{
  const f=fixture();
  await assert.rejects(()=>observeIntoAdapter(f.api,f.spec),/NOT_CONNECTED/);
  assert.equal(f.calls.length,0);
});
test('injected methods preserve their host receiver',async()=>{
  const f=fixture();f.spec.requestRef={kind:'request'};
  const read=f.api.readThread;f.api.marker=true;f.api.time=1100;
  f.api.readThread=function(args){assert.equal(this.marker,true);return read(args);};
  f.api.clock=function(){return this.time++;};
  f.api.runLocalAdapter=async()=>({status:'OK',result:{registration:[{applied:true}]}});
  const r=await observeIntoAdapter(f.api,f.spec);
  assert.equal(r.capture.poll_started_at,1102);
});
test('fixed CLI rejects arbitrary operations and uncertain process outcomes without replay',async()=>{
  let calls=0;
  const port=makeCliPort(async()=>{calls++;return {session_id:1,output:'unconfirmed'};},'/project','/private/run');
  await assert.rejects(()=>port('arbitrary_shell',{}),/FIXED_CLI_OPERATION/);
  assert.equal(calls,0);
  await assert.rejects(()=>port('step',{}),/RECONCILIATION/);
  assert.equal(calls,1);
});
test('caller refuses old or pending spawn envelopes rather than retrying',async()=>{
  for(const result of [
    {profile:'collaboration-parent-v2',tool_calls:[],parent_jobs:[]},
    {profile:'collaboration-parent-v3',tool_calls:[],parent_jobs:[],pending_reconciliation:[{}]}]){
    let calls=0;
    await assert.rejects(()=>prepareReadonly({runLocalAdapter:async(op)=>{
      calls++;return {status:'OK',operation:op,result};
    }}),/NO_REPLAY/);
    assert.equal(calls,1);
  }
});
test('Code Mode module and fixed CLI use no Node or TextEncoder globals',async()=>{
  const vm=require('node:vm'), fs=require('node:fs');
  const context={module:{exports:{}}};
  vm.runInNewContext(fs.readFileSync(require.resolve('../scripts/native_thread_capture.cjs'),'utf8'),context);
  let calls=0;
  const port=context.module.exports.makeCliPort(async args=>{
    calls++;return {exit_code:0,output:JSON.stringify({status:'OK',operation:'step',result:{}})};
  },'/project','/private/run');
  await port('step',{});
  await assert.rejects(()=>port('step',{body:'日本語'.repeat(8000)}),/SIZE_LIMIT/);
  assert.equal(calls,1);
});
test('live read_thread caller is disabled before any adapter or app read',async()=>{
  let calls=0;
  await assert.rejects(()=>finishReadonly({runLocalAdapter:()=>calls++,readThread:()=>calls++},{}),
    /LIVE_READ_THREAD_UNAVAILABLE/);
  assert.equal(calls,0);
});
test('parent standard return ingress preserves the supplied value without claiming a tool bridge',async()=>{
  const result={task_name:'/root/fixture_agent'}, ref={kind:'request'}, received=[];
  const api={runLocalAdapter:async(op,body)=>{
    received.push({op,body});return {status:'OK',operation:op,result:{automatic_tool_execution:false}};
  }};
  const receipt=await recordParentResult(api,ref,result);
  assert.equal(received[0].op,'parent_result');
  assert.equal(received[0].body.result,result);
  assert.equal(received[0].body.request_ref,ref);
  assert.equal(receipt.automatic_tool_execution,false);
});
