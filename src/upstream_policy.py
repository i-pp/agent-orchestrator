"""Read pinned common policy directly from an existing local Git checkout."""
import hashlib
import json
from pathlib import Path
import re
import subprocess
import time

from common_policy import PolicyError,SourcePolicyBundle,_json,_path,_utf8,_sha256

REPOSITORY='i-pp/ai-orchestration'
GIT_URL='git@github.com:i-pp/ai-orchestration.git'
HTTPS_URL='https://github.com/i-pp/ai-orchestration.git'
FILES=('docs/ai/orchestration.md','docs/ai/policies.md','agents/README.md')
MAX_DOCUMENT=65536


def pin_for(project):
    pin=_json(_path(project,directory=True)/'config/upstream-policy.json',
        {'schema_version','repository','git_url','commit','version','sha256','files','checkout'})
    if pin['repository']!=REPOSITORY or pin['git_url']!=GIT_URL:
        raise PolicyError('Unsupported upstream policy source')
    if not isinstance(pin['commit'],str) or not re.fullmatch(r'[0-9a-f]{40}',pin['commit']):
        raise PolicyError('Upstream policy commit is invalid')
    if pin['version']!='git:'+pin['commit']:raise PolicyError('Upstream version/commit mismatch')
    _sha256(pin['sha256'])
    if not isinstance(pin['files'],dict) or set(pin['files'])!=set(FILES):
        raise PolicyError('Upstream document set differs from the pin')
    for entry in pin['files'].values():
        if (not isinstance(entry,dict) or set(entry)!={'sha256','git_blob_sha'}
                or not isinstance(entry['git_blob_sha'],str) or not re.fullmatch(r'[0-9a-f]{40}',entry['git_blob_sha'])):
            raise PolicyError('Invalid upstream document pin')
        _sha256(entry['sha256'])
    if not isinstance(pin['checkout'],str) or not pin['checkout'].strip():raise PolicyError('Missing checkout path')
    return pin


def resolve_checkout(project,value):
    if not isinstance(value,str) or not value.strip():raise PolicyError('Missing checkout path')
    try:
        path=Path(value).expanduser()
        if not path.is_absolute():path=Path(project)/path
        return _path(path,directory=True)
    except (ValueError,RuntimeError,TypeError):raise PolicyError('Checkout path is unavailable') from None


def assemble(documents):
    return ''.join('--- UPSTREAM '+name+' ---\n'+documents[name]+('' if documents[name].endswith('\n') else '\n') for name in FILES)


def checked_document(raw,entry):
    if len(raw)>MAX_DOCUMENT:raise PolicyError('Upstream document exceeds its size bound')
    if (hashlib.sha256(raw).hexdigest()!=entry['sha256']
            or hashlib.sha1(b'blob '+str(len(raw)).encode()+b'\0'+raw).hexdigest()!=entry['git_blob_sha']):
        raise PolicyError('Upstream document hash mismatch')
    text=_utf8(raw,'pinned local Git object')
    if not text.strip():raise PolicyError('Empty upstream document')
    return text


def load_upstream(project,legacy):
    project=_path(project,directory=True);pin=pin_for(project)
    override=project/'config/policy-source.local.json'
    local_commit=None;location=pin['checkout']
    if override.exists() or override.is_symlink():
        settings=_json(override,{'schema_version','checkout','checkout_commit'})
        location=settings['checkout'];local_commit=settings['checkout_commit']
        if not isinstance(local_commit,str) or not re.fullmatch(r'[0-9a-f]{40}',local_commit):
            raise PolicyError('Invalid operator-local checkout commit')
    checkout=resolve_checkout(project,location)
    commit=local_commit or pin['commit']
    deadline=time.monotonic()+10
    def git(arguments,*,optional=False):
        remaining=deadline-time.monotonic()
        if remaining<=0:raise PolicyError('Local policy read deadline exceeded')
        try:
            result=subprocess.run(['git','--no-optional-locks','-c','core.fsmonitor=false',
                '-c','core.untrackedCache=false','-C',str(checkout),*arguments],
                capture_output=True,timeout=min(5,remaining),check=False)
        except (OSError,subprocess.SubprocessError):raise PolicyError('Pinned local policy source is unavailable; no fallback') from None
        if result.returncode:
            if optional:return None
            raise PolicyError('Pinned local policy source is unavailable; no fallback')
        return result.stdout
    top=_utf8(git(['rev-parse','--show-toplevel']).strip(),'local Git metadata')
    if Path(top).resolve()!=checkout:raise PolicyError('Policy path must identify the checkout root')
    origin=git(['remote','get-url','origin'],optional=True)
    origin_matches=origin is not None and _utf8(origin.strip(),'local Git metadata') in [GIT_URL,HTTPS_URL,HTTPS_URL.removesuffix('.git')]
    # A deliberate local-commit override may be an originless parallel checkout.
    # It is an untrusted carrier: every document must still equal the canonical GitHub hash pin.
    if not origin_matches and local_commit is None:
        raise PolicyError('Checkout origin is unverified; an explicit content-equivalent local commit is required')
    if git(['rev-parse','--verify',commit+'^{commit}']).strip().decode()!=commit:
        raise PolicyError('Local policy commit differs from the selected revision')
    head_before=git(['rev-parse','HEAD']).strip().decode()
    documents={}
    for name in FILES:
        try:size=int(git(['cat-file','-s',commit+':'+name]).strip())
        except ValueError:raise PolicyError('Invalid Git object size') from None
        if not 0<size<=MAX_DOCUMENT:raise PolicyError('Upstream document exceeds its size bound')
        documents[name]=checked_document(git(['show',commit+':'+name]),pin['files'][name])
    status=git(['status','--porcelain','-z','--untracked-files=normal'])
    if len(status)>65536:raise PolicyError('Checkout status exceeds its bound')
    head_after=git(['rev-parse','HEAD']).strip().decode()
    text=assemble(documents)
    if hashlib.sha256(text.encode()).hexdigest()!=pin['sha256']:
        raise PolicyError('Upstream aggregate hash mismatch')
    provenance={'repository':pin['repository'],'commit':pin['commit'],'sha256':pin['sha256'],'files':pin['files'],
        'document_bytes':{name:len(documents[name].encode()) for name in FILES},
        'checkout':str(checkout),'loaded_checkout_commit':commit,'checkout_head_before':head_before,
        'checkout_head_after':head_after,'checkout_dirty':bool(status),
        'checkout_origin_verified':origin_matches,'local_commit_override':local_commit is not None,
        'checkout_origin_state':'canonical' if origin_matches else 'missing' if origin is None else 'different',
        'source_proof':'pinned_document_hashes','working_tree_body_used':False,
        'legacy_reference':{'file':'policies.md','version':legacy.version,'sha256':legacy.sha256},
        'legacy_body_injected':False,'automatic_network':False}
    return SourcePolicyBundle(text,pin['version'],pin['sha256'],
        'https://github.com/'+REPOSITORY+'/tree/'+pin['commit'],json.dumps(provenance,sort_keys=True))


def check_bundle(bundle):
    if not isinstance(bundle.text,str):raise PolicyError('Upstream bundle text is invalid')
    try:meta=json.loads(bundle.provenance_json)
    except (TypeError,ValueError):raise PolicyError('Invalid upstream provenance') from None
    if (not isinstance(meta,dict) or meta.get('repository')!=REPOSITORY
            or not isinstance(meta.get('commit'),str) or not re.fullmatch(r'[0-9a-f]{40}',meta['commit'])
            or bundle.version!='git:'+meta['commit'] or bundle.sha256!=meta.get('sha256')
            or bundle.source!='https://github.com/'+REPOSITORY+'/tree/'+meta['commit']
            or not isinstance(meta.get('files'),dict) or set(meta['files'])!=set(FILES)
            or meta.get('legacy_body_injected') is not False or meta.get('automatic_network') is not False
            or meta.get('working_tree_body_used') is not False
            or type(meta.get('checkout_dirty')) is not bool
            or type(meta.get('checkout_origin_verified')) is not bool
            or type(meta.get('local_commit_override')) is not bool
            or not (meta['checkout_origin_verified'] or meta['local_commit_override'])
            or meta.get('checkout_origin_state') not in (['canonical'] if meta['checkout_origin_verified'] else ['missing','different'])
            or meta.get('source_proof')!='pinned_document_hashes'
            or not isinstance(meta.get('loaded_checkout_commit'),str)
            or not re.fullmatch(r'[0-9a-f]{40}',meta['loaded_checkout_commit'])):
        raise PolicyError('Upstream provenance/version is inconsistent')
    for field in ['checkout_head_before','checkout_head_after']:
        if not isinstance(meta.get(field),str) or not re.fullmatch(r'[0-9a-f]{40}',meta[field]):
            raise PolicyError('Invalid checkout provenance')
    if not meta['local_commit_override'] and meta['loaded_checkout_commit']!=meta['commit']:
        raise PolicyError('Checkout revision differs from the canonical pin')
    _sha256(bundle.sha256)
    try:actual=hashlib.sha256(bundle.text.encode()).hexdigest()
    except UnicodeError:raise PolicyError('Upstream text is invalid UTF-8') from None
    if actual!=bundle.sha256:raise PolicyError('Upstream bundle text hash mismatch')
    sizes=meta.get('document_bytes')
    if not isinstance(sizes,dict) or set(sizes)!=set(FILES):raise PolicyError('Invalid document lengths')
    raw=bundle.text.encode();position=0
    for name in FILES:
        entry=meta['files'][name];length=sizes[name]
        if (not isinstance(entry,dict) or set(entry)!={'sha256','git_blob_sha'}
                or not isinstance(entry['git_blob_sha'],str) or not re.fullmatch(r'[0-9a-f]{40}',entry['git_blob_sha'])
                or type(length) is not int or not 0<length<=MAX_DOCUMENT):
            raise PolicyError('Invalid source document provenance')
        _sha256(entry['sha256'])
        marker=('--- UPSTREAM '+name+' ---\n').encode()
        if raw[position:position+len(marker)]!=marker:raise PolicyError('Invalid source section boundary')
        position+=len(marker);part=raw[position:position+length]
        checked_document(part,entry);position+=length
        if not part.endswith(b'\n'):
            if raw[position:position+1]!=b'\n':raise PolicyError('Invalid source section boundary')
            position+=1
    if position!=len(raw):raise PolicyError('Unexpected source section content')
