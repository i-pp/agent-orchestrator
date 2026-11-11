/** Socketless synthetic test harness. No credentials or identity from the host. */
import { DatabaseSync } from 'node:sqlite';
import { readFileSync } from 'node:fs';
import readline from 'node:readline';
import { createApprovalService, D1ApprovalStore, sitesIdentityAdapter, sha256 } from '../../integrations/sites-approval/service.mjs';
const db=new DatabaseSync(':memory:');db.exec(readFileSync(new URL('../../integrations/sites-approval/schema.sql',import.meta.url),'utf8'));
const d1={prepare(sql){return {bind(...values){return {async first(){return db.prepare(sql).get(...values)??null;},async run(){return {meta:{changes:Number(db.prepare(sql).run(...values).changes)}};}}}};}};
let now=1000, subject='fixture-sites-subject';db.function('unixepoch',()=>now);
const config={store:new D1ApprovalStore(d1),origin:'https://approval.example.test',ownerBinding:'fixture-local-owner-binding',approverSubjectSha256:await sha256(subject),machineKeySha256:await sha256('fixture-machine-key-000000000000000000'),authenticateBrowser:sitesIdentityAdapter({trustedSitesDispatch:true}),clock:()=>now};
let service=createApprovalService(config);
for await(const line of readline.createInterface({input:process.stdin,crlfDelay:Infinity})) {
  try {
    const input=JSON.parse(line);
    if(input.op==='clock'){now=input.now;console.log('{}');continue;}
    if(input.op==='revoke'){service=createApprovalService({...config,approverSubjectSha256:await sha256('replacement-subject')});console.log('{}');continue;}
    const request=new Request(config.origin+input.path,{method:input.method,headers:input.headers??{},body:input.body});
    const response=await service.handle(request), body=await response.text();
    console.log(JSON.stringify({status:response.status,body,headers:{'set-cookie':response.headers.get('set-cookie'),'content-type':response.headers.get('content-type')}}));
  }catch{console.log(JSON.stringify({harness_error:true}));}
}
db.close();
