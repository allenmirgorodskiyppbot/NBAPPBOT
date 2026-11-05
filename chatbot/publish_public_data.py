"""Publish only collected public NBA data through GitHub's ordinary Git API.

The workflow token and branch permissions are unchanged. Ref updates are never
forced. Same-file changes since checkout require a fresh collection; unrelated
remote commits are preserved by building on their actual current tree/parent.
"""
from __future__ import annotations

import argparse
import base64
from dataclasses import dataclass
import datetime as dt
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import time

import httpx

API = 'https://api.github.com'
MAX_FILES = 512
MAX_FILE_BYTES = 32_000_000
MAX_TOTAL_BYTES = 128_000_000
MAX_API_BYTES = 16_000_000
SHA = re.compile(r'[0-9a-f]{40}')
FIXED = {'existing_dataset_scope.json', 'season_schedule.json',
         'existing_tracking.json','structured_availability.json.gz',
         'official_injury_pdf_status.json','availability_collection_status.json',
         'career_history_status.json'}


class PublicationError(Exception):
    """A deliberately redacted, reviewable publication failure."""


class APIError(PublicationError):
    def __init__(self, status):
        self.status = status
        super().__init__(f'GitHub public-data API returned HTTP {status}; branch permissions and rules were honored.')


@dataclass(frozen=True)
class Change:
    path: str
    content: bytes | None
    baseline: tuple[str,str,str] | None # mode, type, SHA at checkout

    @property
    def desired(self):
        return ('100644','blob',blob_sha(self.content)) if self.content is not None else None


def blob_sha(content):
    return hashlib.sha1(b'blob '+str(len(content)).encode()+b'\0'+content).hexdigest()


def allowed_path(path, mode):
    if mode not in {'daily','schedule'} or not isinstance(path,str) or '\\' in path:
        return False
    parts=PurePosixPath(path).parts
    if path != '/'.join(parts) or not parts or any(part in {'','..','.'} for part in parts):
        return False
    if not path.startswith('data/live/'):
        return False
    relative=path[len('data/live/'):]
    if mode=='schedule':
        return relative in {'schedule_today.json','schedule_status.json'}
    if relative in FIXED:
        return True
    if re.fullmatch(r'\d{4}-\d{2}-\d{2}\.json',relative):
        try:dt.date.fromisoformat(relative[:-5]);return True
        except ValueError:return False
    return bool(re.fullmatch(r'career_history/(?:history_manifest\.json|career-\d{3}\.jsonl\.gz)',relative)
        or re.fullmatch(r'historical_matchups/(?:historical_matchups_manifest\.json|tracking_pair_history_[0-9a-f]{12}_\d{3}\.jsonl\.gz)',relative))


def _git(root,*args):
    try:
        return subprocess.run(['git','-C',str(root),*args],check=True,stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE).stdout
    except (OSError,subprocess.CalledProcessError):
        raise PublicationError('The checked-out Git state could not be inspected.') from None


def _safe_file(root,path):
    target=root
    for part in PurePosixPath(path).parts:
        target=target/part
        if target.is_symlink():
            raise PublicationError('A public-data path or parent is a symlink; nothing was published.')
    if target.exists() and not target.is_file():
        raise PublicationError('A public-data path is not a regular file.')
    return target


def inspect_changes(root,mode):
    root=Path(root).resolve()
    top=Path(_git(root,'rev-parse','--show-toplevel').decode().strip()).resolve()
    if top!=root:raise PublicationError('Publication root must be the repository checkout root.')
    checkout=_git(root,'rev-parse','HEAD').decode().strip()
    if not SHA.fullmatch(checkout):raise PublicationError('Checkout commit identity is invalid.')
    changed=_git(root,'diff','--name-only','--no-renames','-z','HEAD','--','data/live')
    untracked=_git(root,'ls-files','--others','--exclude-standard','-z','--','data/live')
    try:names=sorted({name.decode('utf-8') for name in (changed+untracked).split(b'\0') if name})
    except UnicodeError:raise PublicationError('A changed path has an unsupported encoding.') from None
    selected=[name for name in names if allowed_path(name,mode)]
    if len(selected)>MAX_FILES:raise PublicationError('Public-data file count exceeds its bound.')
    baseline={}
    for entry in _git(root,'ls-tree','-rz','HEAD','--','data/live').split(b'\0'):
        if not entry:continue
        fields,name=entry.split(b'\t',1)
        path=name.decode('utf-8')
        if path not in selected:continue
        file_mode,kind,sha=fields.decode().split()
        baseline[path]=(file_mode,kind,sha)
    changes=[];total=0
    for name in selected:
        target=_safe_file(root,name)
        old=baseline.get(name)
        if old and old[:2] != ('100644','blob'):
            raise PublicationError('A selected baseline path is not a regular public-data blob.')
        if target.exists():
            if target.stat().st_size>MAX_FILE_BYTES:raise PublicationError('A public-data file exceeds its size bound.')
            content=target.read_bytes();total+=len(content)
            if len(content)>MAX_FILE_BYTES or total>MAX_TOTAL_BYTES:raise PublicationError('Public-data bytes exceed their bound.')
        elif old:content=None
        else:raise PublicationError('An untracked public-data path disappeared during collection.')
        change=Change(name,content,old)
        if change.desired!=old:changes.append(change)
    return checkout,changes


class Publisher:
    def __init__(self,repository,token,*,mode='daily',client=None,sleep=time.sleep):
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]{1,100}',repository or ''):
            raise PublicationError('GITHUB_REPOSITORY must identify one ordinary owner/repository.')
        if repository.split('/')[1] in {'.','..'} or not token or '\n' in token or '\r' in token:
            raise PublicationError('A valid workflow repository and token are required.')
        if mode not in {'daily','schedule'}:raise PublicationError('Unsupported publication mode.')
        self.mode=mode;self.base=f'/repos/{repository}';self.token=token;self.sleep=sleep
        self.owned=client is None
        self.client=client or httpx.Client(timeout=httpx.Timeout(45,connect=15),follow_redirects=False)

    def close(self):
        if self.owned:self.client.close()

    def request(self,method,path,body=None):
        for attempt in range(3):
            try:
                with self.client.stream(method,API+self.base+path,json=body,follow_redirects=False,
                        headers={'Authorization':'Bearer '+self.token,'Accept':'application/vnd.github+json',
                                 'X-GitHub-Api-Version':'2022-11-28','User-Agent':'NBAPPBOT public-data publisher'}) as response:
                    status=response.status_code
                    if status==429 or 500<=status<600:
                        if attempt<2:
                            wait=response.headers.get('retry-after','')
                            self.sleep(min(10,max(1,int(wait))) if wait.isdigit() else 2**attempt)
                            continue
                    if not 200<=status<300:raise APIError(status)
                    data=bytearray()
                    for chunk in response.iter_bytes():
                        data.extend(chunk)
                        if len(data)>MAX_API_BYTES:raise PublicationError('GitHub API response exceeds its bound.')
                    value=json.loads(data)
                    if not isinstance(value,dict):raise PublicationError('GitHub API returned an unexpected document.')
                    return value
            except (httpx.TimeoutException,httpx.NetworkError):
                if attempt<2:self.sleep(2**attempt);continue
                raise PublicationError('GitHub API connection failed after bounded retries.') from None
            except (ValueError,UnicodeError):
                raise PublicationError('GitHub API returned invalid JSON.') from None
        raise APIError(status)

    def head(self):
        result=self.request('GET','/git/ref/heads/main')
        sha=(result.get('object') or {}).get('sha','')
        if not SHA.fullmatch(sha):raise PublicationError('Remote main ref is invalid.')
        return sha

    def tree(self,commit):
        result=self.request('GET','/git/commits/'+commit)
        tree=(result.get('tree') or {}).get('sha','')
        if not SHA.fullmatch(tree):raise PublicationError('Remote commit tree is invalid.')
        document=self.request('GET','/git/trees/'+tree+'?recursive=1')
        if document.get('truncated') is not False or document.get('sha')!=tree or not isinstance(document.get('tree'),list):
            raise PublicationError('Remote tree is truncated or unverified; publication stopped.')
        entries={}
        for item in document['tree']:
            path=item.get('path');mode=item.get('mode');kind=item.get('type');sha=item.get('sha','')
            if not isinstance(path,str) or path in entries or not SHA.fullmatch(sha):
                raise PublicationError('Remote tree contains an invalid or duplicated entry.')
            entries[path]=(mode,kind,sha)
        return tree,entries

    def publish(self,changes):
        if not changes:return {'status':'unchanged','files':0}
        if len(changes)>MAX_FILES or len({c.path for c in changes})!=len(changes):
            raise PublicationError('Publication has an invalid file count or duplicate path.')
        if any(not allowed_path(c.path,self.mode) or c.content is not None and
               (not isinstance(c.content,bytes) or len(c.content)>MAX_FILE_BYTES) for c in changes):
            raise PublicationError('Publication contains an unsupported path or oversized public blob.')
        if sum(len(c.content or b'') for c in changes)>MAX_TOTAL_BYTES:
            raise PublicationError('Public-data bytes exceed their bound.')
        blobs={};last_commit=None
        for lease in range(3):
            head=self.head()
            if last_commit and head==last_commit:
                return {'status':'published','files':len(changes),'commit':head}
            base_tree,remote=self.tree(head)
            pending=[]
            for change in changes:
                if any(remote.get(str(parent),('040000','tree',None))[:2]!=('040000','tree')
                       for parent in PurePosixPath(change.path).parents if str(parent)!='.'):
                    raise PublicationError('A remote public-data parent is no longer a directory; recollect before publishing.')
                current=remote.get(change.path)
                if current==change.desired:continue
                if current!=change.baseline:
                    raise PublicationError('A selected public-data file changed remotely after checkout; recollect before publishing.')
                pending.append(change)
            if not pending:return {'status':'unchanged','files':0,'commit':head}
            entries=[]
            for change in pending:
                desired=change.desired
                if desired and desired[2] not in blobs:
                    result=self.request('POST','/git/blobs',{'content':base64.b64encode(change.content).decode(),'encoding':'base64'})
                    if result.get('sha')!=desired[2]:raise PublicationError('Created public blob differs from its independently computed Git SHA.')
                    blobs[desired[2]]=True
                entries.append({'path':change.path,'mode':'100644','type':'blob','sha':desired[2] if desired else None})
            created=self.request('POST','/git/trees',{'base_tree':base_tree,'tree':entries})
            tree_sha=created.get('sha','')
            if not SHA.fullmatch(tree_sha):raise PublicationError('Created Git tree identity is invalid.')
            result=self.request('POST','/git/commits',{'message':'Refresh collected public NBA data',
                'tree':tree_sha,'parents':[head]})
            commit=result.get('sha','')
            if (not SHA.fullmatch(commit) or (result.get('tree') or {}).get('sha')!=tree_sha
                    or [parent.get('sha') for parent in result.get('parents',[])]!=[head]):
                raise PublicationError('Created commit does not retain the leased main parent/tree.')
            if self.head()!=head:continue
            try:
                # force:false is the actual lease enforcement if main moves
                # after the preceding check: our commit cannot fast-forward it.
                self.request('PATCH','/git/refs/heads/main',{'sha':commit,'force':False})
                return {'status':'published','files':len(pending),'commit':commit}
            except APIError as error:
                if error.status!=422:raise
                latest=self.head()
                if latest==head:
                    raise # Branch protection/validation failure, never bypass.
                if latest==commit:return {'status':'published','files':len(pending),'commit':commit}
                last_commit=commit
        raise PublicationError('Remote main kept changing; a fresh collection is required.')


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode',required=True,choices=['daily','schedule'])
    parser.add_argument('--root',default='.')
    args=parser.parse_args(argv)
    publisher=None
    try:
        _,changes=inspect_changes(args.root,args.mode)
        if not changes:print('Public data unchanged.');return 0
        publisher=Publisher(os.getenv('GITHUB_REPOSITORY',''),os.getenv('GITHUB_TOKEN',''),mode=args.mode)
        result=publisher.publish(changes)
        print('Public data '+result['status']+': '+str(result['files'])+' files.'+
              (' Commit '+result['commit']+'.' if result.get('commit') else ''))
        return 0
    except PublicationError as error:
        print('Public data publication failed: '+str(error),file=sys.stderr);return 1
    except Exception:
        # Never dump arbitrary request bodies, credentials, headers or errors.
        print('Public data publication failed unexpectedly; no forced ref update was attempted.',file=sys.stderr);return 1
    finally:
        if publisher:publisher.close()


if __name__=='__main__':raise SystemExit(main())
