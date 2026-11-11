import test from 'node:test';
import assert from 'node:assert/strict';
import { DatabaseSync } from 'node:sqlite';
import { readFileSync } from 'node:fs';
import { createApprovalService, D1ApprovalStore, sha256, canonical, sitesIdentityAdapter } from './service.mjs';

const ORIGIN='https://synthetic-approval.example.test';
const KEY='synthetic-machine-key-00000000000000000000';
const OWNER='synthetic-site-owner', OWNER_BINDING='synthetic-local-owner-binding';
const ID='11111111-1111-4111-8111-111111111111', APPROVAL='22222222-2222-4222-8222-222222222222';
class D1Fixture {
  constructor(){this.db=new DatabaseSync(':memory:');this.db.exec(readFileSync(new URL('./schema.sql',import.meta.url),'utf8'));}
  prepare(sql){const db=this.db;return {bind(...values){return {async first(){return db.prepare(sql).get(...values)??null;},async run(){return {meta:{changes:Number(db.prepare(sql).run(...values).changes)}};}}}};}
}
async function fixture(extra={}) {
  const d1=new D1Fixture(), now={value:1000};d1.db.function('unixepoch',()=>now.value);
  const cfg={store:new D1ApprovalStore(d1),origin:ORIGIN,ownerBinding:OWNER_BINDING,approverSubjectSha256:await sha256(OWNER),machineKeySha256:await sha256(KEY),authenticateBrowser:sitesIdentityAdapter({trustedSitesDispatch:true}),clock:()=>now.value,...extra};
  const service=createApprovalService(cfg);
  const record={id:APPROVAL,task_id:'synthetic-task',thread_id:'synthetic-thread',native_thread_id:'synthetic-native-thread',native_turn_id:'synthetic-native-turn',request_sha256:'a'.repeat(64),method:'item/commandExecution/requestApproval',action:'command_execution',target:{cwd:'/synthetic'},command_preview:'cat demo.txt',reason_preview:null,risk:'HUMAN_REVIEW_REQUIRED',accept_supported:true,response_supported:true,offered_decisions:['accept','decline']};
  const payload=()=>({challenge_id:ID,approval_id:APPROVAL,owner_binding:OWNER_BINDING,binding_sha256:null,request_sha256:record.request_sha256,expires_at:1120,record});
  async function request(path,{method='GET',body,headers={}}={}) {return service.handle(new Request(ORIGIN+path,{method,headers,body}));}
  const machine=(method,path,body,headers={})=>request(path,{method,body:body===undefined?undefined:JSON.stringify(body),headers:{authorization:'Bearer '+KEY,'content-type':'application/json',...headers}});
  async function stage(challenge=ID,expires=1120){const p={...payload(),challenge_id:challenge,expires_at:expires,binding_sha256:await sha256(canonical(record))};const response=await machine('PUT','/api/laf/approvals/'+challenge,p);return {response,p};}
  async function form(challenge=ID,subject=OWNER,cookie) {const response=await request('/approvals/'+challenge,{headers:{'oai-authenticated-user-id':subject,...(cookie?{cookie}:{})}});const html=await response.text();return {response,html,csrf:/name="csrf" value="([^"]+)"/.exec(html)?.[1],cookie:response.headers.get('set-cookie')?.split(';')[0]};}
  async function decide(form,{challenge=ID,subject=OWNER,decision='accept',csrf=form.csrf,cookie=form.cookie,origin=ORIGIN,extraFields={},headers={}}={}) {return request('/approvals/'+challenge,{method:'POST',headers:{'oai-authenticated-user-id':subject,cookie:cookie??'',origin,'content-type':'application/x-www-form-urlencoded',...headers},body:new URLSearchParams({csrf,decision,request_sha256:record.request_sha256,...extraFields}).toString()});}
  async function consume(p,decision='accept'){const {record:_,...env}=p;return machine('POST','/api/laf/approvals/'+p.challenge_id+'/consume',{...env,decision});}
  return {d1,now,cfg,service,record,request,machine,stage,form,decide,consume};
}

test('unconfigured service and identity adapter fail closed',async()=>{
  assert.equal((await createApprovalService().handle(new Request(ORIGIN))).status,503);
  assert.equal(sitesIdentityAdapter()(new Request(ORIGIN,{headers:{'oai-authenticated-user-id':OWNER}})),null);
});
test('only exact machine API credentials can stage',async()=>{
  const f=await fixture();const {p}=await f.stage();
  assert.equal((await f.request('/api/laf/approvals/'+ID,{method:'PUT',body:JSON.stringify(p),headers:{'content-type':'application/json'}})).status,401);
  assert.equal((await f.machine('GET','/api/laf/approvals/'+ID,undefined,{'oai-authenticated-user-id':OWNER})).status,403);
  const bad={...p,owner_binding:'unpaired-other-owner'};
  assert.equal((await f.machine('PUT','/api/laf/approvals/'+ID,bad)).status,400);
});
test('public link, missing identity, and wrong authenticated subject grant no access',async()=>{
  const f=await fixture();await f.stage();
  assert.equal((await f.request('/approvals/'+ID)).status,401);
  const wrong=await f.form(ID,'another-synthetic-subject');assert.equal(wrong.response.status,403);assert.ok(!wrong.html.includes('cat demo.txt'));
  assert.equal((await f.machine('GET','/approvals/'+ID)).status,403);
});
test('owner form escapes data and supplies secure cookie without raw identity',async()=>{
  const f=await fixture();f.record.command_preview='<script>synthetic</script>';await f.stage();const form=await f.form();
  assert.equal(form.response.status,200);assert.ok(form.html.includes('&lt;script&gt;'));assert.ok(!form.html.includes('<script>'));assert.ok(!form.html.includes(OWNER));
  const cookie=form.response.headers.get('set-cookie');for(const token of ['Secure','HttpOnly','SameSite=Strict','Path=/'])assert.ok(cookie.includes(token));
  assert.ok(form.response.headers.get('content-security-policy').includes("form-action 'self'"));
});
test('machine credential cannot submit a browser decision, even with fixture identity',async()=>{
  const f=await fixture();await f.stage();const form=await f.form();assert.equal((await f.decide(form,{headers:{authorization:'Bearer '+KEY}})).status,403);
  assert.equal((await f.d1.prepare('SELECT status FROM approval_interactions WHERE challenge_id=?').bind(ID).first()).status,'PENDING');
});
test('exact explicit click records only; machine status exposes no receipt',async()=>{
  const f=await fixture();const {p}=await f.stage();const form=await f.form();assert.equal((await f.decide(form)).status,200);
  const status=await (await f.machine('GET','/api/laf/approvals/'+ID)).json();assert.equal(status.status,'DECIDED');assert.equal(status.execution_authorized,false);assert.ok(!('receipt' in status));
  const response=await f.consume(p);assert.equal(response.status,200);const data=await response.json();assert.equal(data.status,'CONSUMED');assert.match(data.receipt,/^[0-9a-f]{64}$/);
  const row=await f.cfg.store.get(ID);assert.notEqual(row.receipt_sha256,data.receipt);assert.equal(row.receipt_sha256,await sha256(data.receipt));assert.equal(row.csrf_sha256,null);
});
test('CSRF, session, origin, duplicates, choice and hash cannot be substituted',async()=>{
  const f=await fixture();await f.stage();const form=await f.form();
  for(const options of [{csrf:'b'.repeat(64)},{cookie:'__Host-laf-browser='+'c'.repeat(64)},{origin:'https://evil.example.test'},{extraFields:{human_approved:'true'}},{decision:'acceptForSession'},{extraFields:{request_sha256:'d'.repeat(64)}}])assert.notEqual((await f.decide(form,options)).status,200);
  assert.equal((await f.decide(form)).status,200);assert.notEqual((await f.decide(form)).status,200);
});
test('decline-only native request never offers or records accept',async()=>{
  const f=await fixture();f.record.offered_decisions=['decline'];await f.stage();const form=await f.form();assert.ok(!form.html.includes('value="accept"'));assert.notEqual((await f.decide(form)).status,200);assert.equal((await f.decide(form,{decision:'decline'})).status,200);
});
test('six concurrent explicit clicks and consumes have one winner each',async()=>{
  const f=await fixture();const {p}=await f.stage();const form=await f.form();const clicks=await Promise.all(Array.from({length:6},()=>f.decide(form)));assert.equal(clicks.filter(r=>r.status===200).length,1);
  const consumes=await Promise.all(Array.from({length:6},()=>f.consume(p)));assert.equal(consumes.filter(r=>r.status===200).length,1);
});
test('unused expired decision needs new challenge and click; consumed cannot revive',async()=>{
  const f=await fixture();const {p}=await f.stage();const form=await f.form();await f.decide(form);f.now.value=1120;
  assert.equal((await (await f.machine('GET','/api/laf/approvals/'+ID)).json()).status,'EXPIRED');assert.notEqual((await f.consume(p)).status,200);
  const fresh='33333333-3333-4333-8333-333333333333';const next=await f.stage(fresh,1240);assert.equal(next.response.status,200);assert.notEqual((await f.consume(next.p)).status,200);
  const newForm=await f.form(fresh);assert.equal((await f.decide(newForm,{challenge:fresh})).status,200);assert.equal((await f.consume(next.p)).status,200);
  f.now.value=1240;const again=await f.stage('44444444-4444-4444-8444-444444444444',1360);assert.notEqual(again.response.status,200);
});
test('parallel stage cannot create multiple active challenges for same approval',async()=>{
  const f=await fixture();const replies=await Promise.all([f.stage(),f.stage('33333333-3333-4333-8333-333333333333')]);assert.equal(replies.filter(x=>x.response.status===200).length,1);
});
test('subject revocation invalidates recorded decision at status and consume',async()=>{
  const f=await fixture();const {p}=await f.stage();const form=await f.form();await f.decide(form);
  const revoked=createApprovalService({...f.cfg,approverSubjectSha256:await sha256('replacement-owner')});
  const status=await revoked.handle(new Request(ORIGIN+'/api/laf/approvals/'+ID,{headers:{authorization:'Bearer '+KEY}}));assert.equal((await status.json()).status,'REVOKED');
  const {record:_,...env}=p;const response=await revoked.handle(new Request(ORIGIN+'/api/laf/approvals/'+ID+'/consume',{method:'POST',headers:{authorization:'Bearer '+KEY,'content-type':'application/json'},body:JSON.stringify({...env,decision:'accept'})}));assert.equal(response.status,409);
});
test('changed payload, native permissions and wrong binding cannot stage',async()=>{
  const f=await fixture();const {p}=await f.stage();for(const bad of [{...p,binding_sha256:'b'.repeat(64)},{...p,record:{...p.record,command_preview:'different'}},{...p,record:{...p.record,method:'item/permissions/requestApproval'}}])assert.notEqual((await f.machine('PUT','/api/laf/approvals/'+ID,bad)).status,200);
});
test('idempotent stage preserves existing decision and no proof is in the URL',async()=>{
  const f=await fixture();await f.stage();const form=await f.form();await f.decide(form);const second=await f.stage();assert.equal(second.response.status,200);assert.equal((await second.response.json()).status,'DECIDED');
});
test('entry point without approved bindings is inert',async()=>{
  const worker=(await import('./worker.mjs')).default;assert.equal((await worker.fetch(new Request(ORIGIN))).status,503);
});
test('request body limit is enforced before parsing and missing trust opt-in stays blocked',async()=>{
  const f=await fixture();const response=await f.request('/api/laf/approvals/'+ID,{method:'PUT',headers:{authorization:'Bearer '+KEY,'content-type':'application/json'},body:'x'.repeat(16385)});assert.equal(response.status,413);
  const worker=(await import('./worker.mjs')).default;const env={APPROVAL_DB:f.d1,APPROVAL_ORIGIN:ORIGIN,APPROVAL_OWNER_BINDING:OWNER_BINDING,APPROVAL_OWNER_SUBJECT_SHA256:await sha256(OWNER),APPROVAL_MACHINE_KEY_SHA256:await sha256(KEY)};
  assert.equal((await worker.fetch(new Request(ORIGIN+'/api/laf/approvals/'+ID,{headers:{authorization:'Bearer '+KEY}}),env)).status,503);
});
test('slow body crossing expiry cannot record or consume a decision',async()=>{
  for (const operation of ['decide','consume']) {
    const f=await fixture();const {p}=await f.stage();const form=await f.form();
    if(operation==='consume')assert.equal((await f.decide(form)).status,200);
    const body=operation==='decide'?new URLSearchParams({csrf:form.csrf,decision:'accept',request_sha256:f.record.request_sha256}).toString():JSON.stringify({...Object.fromEntries(Object.entries(p).filter(([k])=>k!=='record')),decision:'accept'});
    const stream=new ReadableStream({pull(controller){f.now.value=1120;controller.enqueue(new TextEncoder().encode(body));controller.close();}});
    // A deferred async authentication hop makes body consumption occur after entry.
    const original=f.cfg.authenticateBrowser;
    const service=createApprovalService({...f.cfg,authenticateBrowser:async request=>{await Promise.resolve();return original(request);}});
    const headers=operation==='decide'?{'oai-authenticated-user-id':OWNER,cookie:form.cookie,origin:ORIGIN,'content-type':'application/x-www-form-urlencoded'}:{authorization:'Bearer '+KEY,'content-type':'application/json'};
    const path=operation==='decide'?'/approvals/'+ID:'/api/laf/approvals/'+ID+'/consume';
    const response=await service.handle(new Request(ORIGIN+path,{method:'POST',body:stream,duplex:'half',headers}));assert.notEqual(response.status,200);
    assert.equal((await f.cfg.store.get(ID)).status,operation==='decide'?'PENDING':'DECIDED');assert.ok(!(await response.text()).includes('receipt'));
  }
});
test('database execution time fences expiry after handler clocks were checked',async()=>{
  const f=await fixture();const {p}=await f.stage();const form=await f.form();
  const original=f.d1.prepare.bind(f.d1);
  f.d1.prepare=sql=>{
    const statement=original(sql);
    return {bind(...args){const bound=statement.bind(...args);return {first:()=>bound.first(),run:async()=>{if(sql.includes("SET status='DECIDED'"))f.now.value=1120;return bound.run();}};}};
  };
  assert.notEqual((await f.decide(form)).status,200);assert.equal((await f.cfg.store.get(ID)).status,'PENDING');
});
test('database time prevents delayed consume from exporting a receipt',async()=>{
  const f=await fixture();const {p}=await f.stage();const form=await f.form();await f.decide(form);
  const original=f.d1.prepare.bind(f.d1);
  f.d1.prepare=sql=>{const statement=original(sql);return {bind(...args){const bound=statement.bind(...args);return {first:()=>bound.first(),run:async()=>{if(sql.includes("SET status='CONSUMED'"))f.now.value=1120;return bound.run();}};}};};
  const response=await f.consume(p);assert.notEqual(response.status,200);assert.ok(!(await response.text()).includes('receipt'));assert.equal((await f.cfg.store.get(ID)).status,'DECIDED');
});
test('post-commit delayed consume reply contains no proof and retains consumed fence',async()=>{
  const f=await fixture();const {p}=await f.stage();const form=await f.form();await f.decide(form);
  const original=f.d1.prepare.bind(f.d1);
  f.d1.prepare=sql=>{const statement=original(sql);return {bind(...args){const bound=statement.bind(...args);return {first:()=>bound.first(),run:async()=>{const result=await bound.run();if(sql.includes("SET status='CONSUMED'"))f.now.value=1120;return result;}};}};};
  const response=await f.consume(p);assert.equal(response.status,409);assert.ok(!(await response.text()).includes('"receipt"'));assert.equal((await f.cfg.store.get(ID)).status,'CONSUMED');
});
test('provider exceptions never echo raw identity, secrets or internal errors',async()=>{
  const f=await fixture();const service=createApprovalService({...f.cfg,authenticateBrowser:()=>{throw Object.assign(new Error('synthetic-private-error-content'),{status:403});}});
  const response=await service.handle(new Request(ORIGIN+'/approvals/'+ID));assert.equal(response.status,503);const body=await response.text();assert.ok(!body.includes('synthetic-private-error-content'));assert.ok(body.includes('APPROVAL_SERVICE_UNAVAILABLE'));
});
