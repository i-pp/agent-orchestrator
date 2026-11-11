/** Inert Sites/Worker-compatible approval service. No listener or provisioning.
 * Only an approved server host may configure identity, pairing, DB and API key.
 */
const HEX = /^[0-9a-f]{64}$/;
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const COMMAND = 'item/commandExecution/requestApproval';
const FILE = 'item/fileChange/requestApproval';
const FIELDS = ['id','task_id','thread_id','native_thread_id','native_turn_id','request_sha256','method','action','target','command_preview','reason_preview','risk','accept_supported','response_supported','offered_decisions'];
const encoder = new TextEncoder();
export const canonical = value => JSON.stringify(sort(value));
function sort(value) {
  if (Array.isArray(value)) return value.map(sort);
  if (value && typeof value === 'object') return Object.fromEntries(Object.keys(value).sort().map(k => [k, sort(value[k])]));
  return value;
}
export async function sha256(value) {
  return Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256', encoder.encode(value))), b => b.toString(16).padStart(2,'0')).join('');
}
function equal(a,b) {
  if (typeof a !== 'string' || typeof b !== 'string' || a.length !== b.length) return false;
  let result = 0; for (let i=0;i<a.length;i++) result |= a.charCodeAt(i)^b.charCodeAt(i); return result === 0;
}
function random() { return Array.from(crypto.getRandomValues(new Uint8Array(32)), b => b.toString(16).padStart(2,'0')).join(''); }
class ApprovalServiceError extends Error { constructor(status,code){super(code);this.status=status;} }
function fail(status, code) { throw new ApprovalServiceError(status,code); }
function exactKeys(value, keys) { return value && !Array.isArray(value) && typeof value === 'object' && canonical(Object.keys(value).sort()) === canonical([...keys].sort()); }
const allowed = r => ['accept','decline'].filter(d => Array.isArray(r.offered_decisions) && r.offered_decisions.includes(d) && (d !== 'accept' || r.accept_supported === true && r.method === COMMAND));
function escape(value) { return String(value).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
function safeOrigin(origin) { try { const u=new URL(origin);return origin === u.origin && u.protocol === 'https:' && !u.username && !u.password; } catch { return false; } }

/** Host-only opt-in; not an HTTP flag. Never use behind another/untrusted ingress.
 * Dispatch must strip caller-supplied identity headers and inject verified ones.
 * It supplies identity, NOT auth_time, fresh step-up or human action evidence.
 */
export function sitesIdentityAdapter({ trustedSitesDispatch = false } = {}) {
  return request => {
    if (!trustedSitesDispatch) return null;
    const subject = request.headers.get('oai-authenticated-user-id');
    return typeof subject === 'string' && subject.length > 0 && subject.length <= 256 ? subject : null;
  };
}

export class D1ApprovalStore {
  constructor(db) { this.db=db; }
  async get(id) { return this.db.prepare('SELECT * FROM approval_interactions WHERE challenge_id=?').bind(id).first(); }
  async create(p) {
    await this.db.prepare(`INSERT OR IGNORE INTO approval_interactions
      (challenge_id,approval_id,owner_binding,binding_sha256,request_sha256,expires_at,record_json,status)
      SELECT ?,?,?,?,?,?,?,'PENDING' WHERE ?>unixepoch() AND NOT EXISTS
      (SELECT 1 FROM approval_interactions WHERE owner_binding=? AND approval_id=? AND (status='CONSUMED' OR expires_at>unixepoch()))`)
      .bind(p.challenge_id,p.approval_id,p.owner_binding,p.binding_sha256,p.request_sha256,p.expires_at,canonical(p.record),p.expires_at,p.owner_binding,p.approval_id).run();
    const row=await this.get(p.challenge_id);
    if (!row) fail(409,'APPROVAL_ALREADY_CONSUMED');
    if (row.approval_id!==p.approval_id || row.owner_binding!==p.owner_binding || row.binding_sha256!==p.binding_sha256 || row.request_sha256!==p.request_sha256 || row.expires_at!==p.expires_at || row.record_json!==canonical(p.record)) fail(409,'APPROVAL_CHANGED');
    return row;
  }
  async form(row,subject,session,csrf,now) {
    const r=await this.db.prepare("UPDATE approval_interactions SET subject_sha256=?,session_sha256=?,csrf_sha256=? WHERE challenge_id=? AND binding_sha256=? AND status='PENDING' AND expires_at>? AND expires_at>unixepoch()")
      .bind(subject,session,csrf,row.challenge_id,row.binding_sha256,now).run();
    if (r.meta.changes!==1) fail(409,'INTERACTION_NOT_PENDING');
  }
  async decide(row,subject,session,csrf,decision,now) {
    const r=await this.db.prepare("UPDATE approval_interactions SET status='DECIDED',decision=?,decided_at=?,csrf_sha256=NULL WHERE challenge_id=? AND binding_sha256=? AND status='PENDING' AND expires_at>? AND expires_at>unixepoch() AND subject_sha256=? AND session_sha256=? AND csrf_sha256=?")
      .bind(decision,now,row.challenge_id,row.binding_sha256,now,subject,session,csrf).run();
    if (r.meta.changes!==1) fail(409,'DECISION_BINDING_FAILED');
  }
  async consume(row,subject,decision,receiptHash,now) {
    const r=await this.db.prepare("UPDATE approval_interactions SET status='CONSUMED',consumed_at=?,receipt_sha256=? WHERE challenge_id=? AND binding_sha256=? AND status='DECIDED' AND expires_at>? AND expires_at>unixepoch() AND subject_sha256=? AND decision=? AND NOT EXISTS (SELECT 1 FROM approval_interactions previous WHERE previous.owner_binding=? AND previous.approval_id=? AND previous.status='CONSUMED')")
      .bind(now,receiptHash,row.challenge_id,row.binding_sha256,now,subject,decision,row.owner_binding,row.approval_id).run();
    if (r.meta.changes!==1) fail(409,'NO_UNAMBIGUOUS_HUMAN_DECISION');
  }
}

export function createApprovalService({store,origin,ownerBinding,approverSubjectSha256,machineKeySha256,authenticateBrowser,clock=()=>Math.floor(Date.now()/1000)} = {}) {
  const enabled=store && safeOrigin(origin) && /^[a-zA-Z0-9_-]{16,128}$/.test(ownerBinding??'') && HEX.test(approverSubjectSha256??'') && HEX.test(machineKeySha256??'') && typeof authenticateBrowser==='function';
  const baseHeaders={'Cache-Control':'private, no-store','Referrer-Policy':'same-origin','X-Content-Type-Options':'nosniff','Content-Security-Policy':"default-src 'none'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"};
  function json(value,status=200) { return new Response(JSON.stringify(value),{status,headers:{...baseHeaders,'Content-Type':'application/json'}}); }
  const envelope=row=>Object.fromEntries(['challenge_id','approval_id','owner_binding','binding_sha256','request_sha256','expires_at'].map(k=>[k,row[k]]));
  function state(row,now) {
    if (row.status==='CONSUMED') return 'CONSUMED';
    if (row.expires_at<=now) return 'EXPIRED';
    if (row.status==='DECIDED' && !equal(row.subject_sha256,approverSubjectSha256)) return 'REVOKED';
    return row.status;
  }
  function result(row,now) { const status=state(row,now);return {...envelope(row),status,decision:status==='DECIDED'?row.decision:null,execution_authorized:false}; }
  async function human(request) {
    if (request.headers.has('authorization') || request.headers.has('oai-sites-authorization')) fail(403,'HUMAN_BROWSER_CHANNEL_REQUIRED');
    const subject=await authenticateBrowser(request);
    if (typeof subject!=='string' || !subject) fail(401,'HUMAN_AUTHENTICATION_REQUIRED');
    const digest=await sha256(subject);
    if (!equal(digest,approverSubjectSha256)) fail(403,'APPROVAL_NOT_OWNED');
    return digest;
  }
  async function machine(request) {
    // A browser identity is never a machine credential or vice versa.
    if (await authenticateBrowser(request)) fail(403,'MACHINE_CHANNEL_REQUIRED');
    const auth=request.headers.get('authorization')??'';
    if (!auth.startsWith('Bearer ') || !equal(await sha256(auth.slice(7)),machineKeySha256)) fail(401,'MACHINE_AUTHENTICATION_REQUIRED');
  }
  async function boundedText(request,max) {
    const reader=request.body?.getReader();if (!reader) return '';
    const chunks=[];let size=0;
    for (;;) {
      const {value,done}=await reader.read();if(done)break;
      size+=value.byteLength;
      if(size>max){await reader.cancel();fail(413,'REQUEST_TOO_LARGE');}
      chunks.push(value);
    }
    const all=new Uint8Array(size);let offset=0;for(const chunk of chunks){all.set(chunk,offset);offset+=chunk.byteLength;}
    try{return new TextDecoder('utf-8',{fatal:true}).decode(all);}catch{fail(400,'INVALID_REQUEST_ENCODING');}
  }
  async function bodyJson(request,max=16384) {
    if (request.headers.get('content-type')!=='application/json') fail(400,'INVALID_MACHINE_REQUEST');
    const raw=await boundedText(request,max);
    try { return JSON.parse(raw); } catch { fail(400,'INVALID_MACHINE_REQUEST'); }
  }
  function cookie(request) {
    const parts=(request.headers.get('cookie')??'').split(';').map(x=>x.trim()).filter(x=>x.startsWith('__Host-laf-browser='));
    if (parts.length!==1 || !HEX.test(parts[0].slice(19))) return null;
    return parts[0].slice(19);
  }
  async function handle(request) {
    try {
      if (!enabled) return json({error:'APPROVAL_PROVIDER_UNCONFIGURED'},503);
      const url=new URL(request.url);let now=clock();
      if (url.origin!==origin || url.search || url.hash) fail(400,'INVALID_APPROVAL_ORIGIN');
      const api=/^\/api\/laf\/approvals\/([0-9a-f-]{36})(\/consume)?$/.exec(url.pathname);
      if (api) {
        await machine(request);now=clock();
        const id=api[1];if (!UUID.test(id)) fail(400,'INVALID_CHALLENGE');
        if (!api[2] && request.method==='PUT') {
          const p=await bodyJson(request);now=clock();
          if (!exactKeys(p,['challenge_id','approval_id','owner_binding','binding_sha256','request_sha256','expires_at','record']) || p.challenge_id!==id || !UUID.test(p.approval_id??'') || p.owner_binding!==ownerBinding || !HEX.test(p.binding_sha256??'') || !HEX.test(p.request_sha256??'') || !Number.isSafeInteger(p.expires_at) || p.expires_at<=now || p.expires_at>now+300 || !exactKeys(p.record,FIELDS)) fail(400,'INVALID_APPROVAL_BINDING');
          const r=p.record;
          if (r.id!==p.approval_id || r.request_sha256!==p.request_sha256 || ![COMMAND,FILE].includes(r.method) || r.response_supported!==true || !allowed(r).length || canonical(r).length>12000 || await sha256(canonical(r))!==p.binding_sha256) fail(400,'INVALID_APPROVAL_RECORD');
          for (const k of ['task_id','thread_id','native_thread_id','native_turn_id']) if (typeof r[k]!=='string' || !r[k] || encoder.encode(r[k]).length>160) fail(400,'INVALID_APPROVAL_RECORD');
          const row=await store.create(p);return json(result(row,clock()));
        }
        const row=await store.get(id);if (!row || row.owner_binding!==ownerBinding) fail(404,'UNKNOWN_INTERACTION');
        if (!api[2] && request.method==='GET') return json(result(row,clock()));
        if (api[2] && request.method==='POST') {
          const p=await bodyJson(request,4096);now=clock();
          if (!exactKeys(p,[...Object.keys(envelope(row)),'decision']) || Object.keys(envelope(row)).some(k=>p[k]!==row[k]) || !allowed(JSON.parse(row.record_json)).includes(p.decision) || state(row,now)!=='DECIDED' || row.decision!==p.decision) fail(409,'NO_UNAMBIGUOUS_HUMAN_DECISION');
          const receipt=random();await store.consume(row,approverSubjectSha256,p.decision,await sha256(receipt),clock());
          if(clock()>=row.expires_at)fail(409,'DECISION_EXPIRED_AFTER_CONSUMPTION');
          return json({...envelope(row),status:'CONSUMED',decision:p.decision,receipt,execution_authorized:false});
        }
        return json({error:'METHOD_NOT_ALLOWED'},405);
      }
      const page=/^\/approvals\/([0-9a-f-]{36})$/.exec(url.pathname);
      if (!page || !UUID.test(page[1])) return json({error:'NOT_FOUND'},404);
      const subject=await human(request);now=clock();
      const row=await store.get(page[1]);if (!row || row.owner_binding!==ownerBinding) fail(404,'UNKNOWN_INTERACTION');
      now=clock();if (state(row,now)!=='PENDING') return json(result(row,now),409);
      const record=JSON.parse(row.record_json);
      if (request.method==='GET') {
        const session=cookie(request)??random(), csrf=random();
        await store.form(row,subject,await sha256(session),await sha256(csrf),clock());
        if(clock()>=row.expires_at)fail(409,'INTERACTION_EXPIRED');
        const buttons=allowed(record).map(d=>`<button name="decision" value="${d}">${d==='accept'?'Approve once':'Decline once'}</button>`).join('');
        const html=`<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Review one LAF action</title></head><body><h1>Review one pending action</h1><p>Untrusted request content follows. This records a decision; it does not execute the action.</p><pre>${escape(canonical(record))}</pre><form method="post"><input type="hidden" name="csrf" value="${csrf}"><input type="hidden" name="request_sha256" value="${row.request_sha256}">${buttons}</form></body></html>`;
        return new Response(html,{headers:{...baseHeaders,'Content-Type':'text/html; charset=utf-8','Set-Cookie':`__Host-laf-browser=${session}; Path=/; Secure; HttpOnly; SameSite=Strict; Max-Age=600`}});
      }
      if (request.method==='POST') {
        if (request.headers.get('origin')!==origin || request.headers.get('content-type')!=='application/x-www-form-urlencoded') fail(403,'INVALID_BROWSER_DECISION');
        const text=await boundedText(request,4096);
        const fields=new URLSearchParams(text), keys=[...fields.keys()];
        if (keys.length!==3 || new Set(keys).size!==3 || !['csrf','decision','request_sha256'].every(k=>fields.has(k)) || fields.get('request_sha256')!==row.request_sha256 || !allowed(record).includes(fields.get('decision'))) fail(400,'INVALID_BROWSER_DECISION');
        const session=cookie(request);if (!session || !HEX.test(fields.get('csrf')??'')) fail(403,'DECISION_BINDING_FAILED');
        await store.decide(row,subject,await sha256(session),await sha256(fields.get('csrf')),fields.get('decision'),clock());
        if(clock()>=row.expires_at)fail(409,'INTERACTION_EXPIRED');
        return json({status:'DECISION_RECORDED',approval_id:row.approval_id,request_sha256:row.request_sha256,decision:fields.get('decision'),execution_authorized:false});
      }
      return json({error:'METHOD_NOT_ALLOWED'},405);
    } catch (error) { return error instanceof ApprovalServiceError
      ?json({error:error.message},error.status):json({error:'APPROVAL_SERVICE_UNAVAILABLE'},503); }
  }
  return {handle,enabled:!!enabled};
}
