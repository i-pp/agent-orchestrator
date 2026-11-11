import http from 'node:http';
const server=http.createServer(async(req,res)=>{
 if(req.method==='GET'){
  res.writeHead(200,{'Content-Type':'text/html','Referrer-Policy':req.url.includes('same-origin')?'same-origin':'no-referrer','Cache-Control':'no-store'});
  return res.end('<!doctype html><meta charset="utf-8"><title>Fixture form origin</title><form method="post"><input name="csrf" type="hidden" value="'+ 'a'.repeat(64) +'"><input name="request_sha256" type="hidden" value="'+'b'.repeat(64)+'"><button name="decision" value="accept">Submit fixture</button></form>');
 }
 let body='';for await(const c of req)body+=c;
 const f=new URLSearchParams(body);const result={origin_present:req.headers.origin!==undefined,origin_is_null:req.headers.origin==='null',origin_is_expected:req.headers.origin==='http://127.0.0.1:5418',content_type:req.headers['content-type'],fields:[...f.keys()].sort(),csrf_length:f.get('csrf')?.length,hash_length:f.get('request_sha256')?.length,decision:f.get('decision')};
 console.log(JSON.stringify(result));res.writeHead(200,{'Content-Type':'application/json'});res.end(JSON.stringify(result));
});server.listen(5418,'127.0.0.1',()=>console.log('Fixture ready 127.0.0.1:5418'));
