"""The REST publisher preserves branch rules, unrelated commits and scope."""
import base64
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import httpx

from publish_public_data import (APIError, Change, PublicationError, Publisher,
    allowed_path, blob_sha, inspect_changes, main)

PATH='data/live/existing_tracking.json'
OLD=b'{"old":true}\n';NEW=b'{"new":true}\n'
BASE=('100644','blob',blob_sha(OLD))
TOKEN='unit-test-secret-never-log'


class GitAPI:
    def __init__(self):
        self.head='a'*40;self.first=self.head;self.calls=[];self.ref_reads=0
        self.trees={'b'*40:{PATH:BASE,'app.py':('100644','blob',blob_sha(b'unrelated code'))}}
        self.commits={self.head:{'tree':{'sha':'b'*40},'parents':[]}}
        self.denied=None;self.truncated=False;self.wrong_blob=False;self.race=False;self.raced=False
        self.transient=[]

    def advance(self,*,same_file=False):
        current=dict(self.trees[self.commits[self.head]['tree']['sha']])
        current[PATH if same_file else 'notes.txt']=('100644','blob',blob_sha(b'new remote source' if same_file else b'new unrelated note'))
        tree=hashlib.sha1(json.dumps(current,sort_keys=True).encode()).hexdigest();self.trees[tree]=current
        head=hashlib.sha1((self.head+tree).encode()).hexdigest()
        self.commits[head]={'tree':{'sha':tree},'parents':[{'sha':self.head}]};self.head=head

    def handle(self,request):
        self.calls.append(request)
        assert request.url.host=='api.github.com'
        assert request.headers['authorization']=='Bearer '+TOKEN
        path=request.url.path.removeprefix('/repos/owner/repo')
        body=json.loads(request.content) if request.content else None
        if self.transient:
            return httpx.Response(self.transient.pop(0),headers={'retry-after':'1'},json={'message':TOKEN})
        if request.method=='GET' and path=='/git/ref/heads/main':
            self.ref_reads+=1;return httpx.Response(200,json={'object':{'sha':self.head}})
        if request.method=='GET' and path.startswith('/git/commits/'):
            return httpx.Response(200,json=self.commits[path.split('/')[-1]])
        if request.method=='GET' and path.startswith('/git/trees/'):
            sha=path.split('/')[-1]
            return httpx.Response(200,json={'sha':sha,'truncated':self.truncated,'tree':[
                {'path':name,'mode':item[0],'type':item[1],'sha':item[2]} for name,item in self.trees[sha].items()]})
        if request.method=='POST' and path=='/git/blobs':
            content=base64.b64decode(body['content']);assert body['encoding']=='base64'
            return httpx.Response(201,json={'sha':'0'*40 if self.wrong_blob else blob_sha(content)})
        if request.method=='POST' and path=='/git/trees':
            current=dict(self.trees[body['base_tree']])
            for entry in body['tree']:
                if entry['sha'] is None:current.pop(entry['path'],None)
                else:current[entry['path']]=(entry['mode'],entry['type'],entry['sha'])
            sha=hashlib.sha1(json.dumps(current,sort_keys=True).encode()).hexdigest();self.trees[sha]=current
            return httpx.Response(201,json={'sha':sha})
        if request.method=='POST' and path=='/git/commits':
            sha=hashlib.sha1(json.dumps(body,sort_keys=True).encode()).hexdigest()
            self.commits[sha]={'tree':{'sha':body['tree']},'parents':[{'sha':x} for x in body['parents']]}
            return httpx.Response(201,json={'sha':sha,**self.commits[sha]})
        if request.method=='PATCH' and path=='/git/refs/heads/main':
            assert body['force'] is False
            if self.denied:return httpx.Response(self.denied,json={'message':TOKEN+' protected branch'})
            if self.race and not self.raced:self.raced=True;self.advance();return httpx.Response(422,json={'message':'non-fast-forward'})
            parent=self.commits[body['sha']]['parents'][0]['sha']
            if parent!=self.head:return httpx.Response(422,json={'message':'non-fast-forward'})
            self.head=body['sha'];return httpx.Response(200,json={'object':{'sha':self.head}})
        raise AssertionError('Unexpected test API route '+request.method+' '+path)


class PublicDataAPITests(unittest.TestCase):
    def setUp(self):
        self.api=GitAPI();self.client=httpx.Client(transport=httpx.MockTransport(self.api.handle))
        self.addCleanup(self.client.close);self.sleeps=[]
        self.publisher=Publisher('owner/repo',TOKEN,client=self.client,sleep=self.sleeps.append)

    def test_creates_verified_blob_tree_parent_and_nonforced_main_update(self):
        result=self.publisher.publish([Change(PATH,NEW,BASE)])
        self.assertEqual(result['status'],'published')
        final=self.api.trees[self.api.commits[self.api.head]['tree']['sha']]
        self.assertEqual(final[PATH],('100644','blob',blob_sha(NEW)))
        self.assertEqual(final['app.py'],('100644','blob',blob_sha(b'unrelated code')))
        tree_body=json.loads(next(r.content for r in self.api.calls if r.method=='POST' and r.url.path.endswith('/trees')))
        self.assertEqual(tree_body['base_tree'],'b'*40)
        self.assertEqual([x['path'] for x in tree_body['tree']],[PATH])
        self.assertEqual(self.api.commits[self.api.head]['parents'],[{'sha':self.api.first}])

    def test_concurrent_unrelated_commit_survives_nonfastforward_retry(self):
        self.api.race=True
        result=self.publisher.publish([Change(PATH,NEW,BASE)])
        self.assertEqual(result['status'],'published')
        final=self.api.trees[self.api.commits[self.api.head]['tree']['sha']]
        self.assertIn('notes.txt',final)
        self.assertEqual(final[PATH][2],blob_sha(NEW))
        self.assertEqual(sum(r.method=='PATCH' for r in self.api.calls),2)

    def test_same_file_remote_change_aborts_before_any_mutation(self):
        self.api.advance(same_file=True);head=self.api.head
        with self.assertRaisesRegex(PublicationError,'changed remotely'):
            self.publisher.publish([Change('data/live/existing_dataset_scope.json',b'new',None),Change(PATH,NEW,BASE)])
        self.assertEqual(self.api.head,head)
        self.assertFalse(any(r.method in {'POST','PATCH'} for r in self.api.calls))

    def test_already_published_exact_state_is_noop(self):
        self.api.trees['b'*40][PATH]=('100644','blob',blob_sha(NEW))
        self.assertEqual(self.publisher.publish([Change(PATH,NEW,BASE)])['status'],'unchanged')
        self.assertFalse(any(r.method!='GET' for r in self.api.calls))

    def test_remote_file_mode_or_parent_symlink_change_is_conflict(self):
        for value in [('100755','blob',BASE[2]),('120000','blob',BASE[2])]:
            self.api.trees['b'*40][PATH]=value
            with self.assertRaisesRegex(PublicationError,'changed remotely'):
                self.publisher.publish([Change(PATH,NEW,BASE)])
        self.api.trees['b'*40][PATH]=BASE
        self.api.trees['b'*40]['data/live']=('120000','blob',BASE[2])
        with self.assertRaisesRegex(PublicationError,'parent is no longer'):
            self.publisher.publish([Change(PATH,NEW,BASE)])

    def test_protected_branch_403_and_422_honored_and_token_redacted(self):
        for status in [403,422]:
            self.api.denied=status;head=self.api.head
            with self.assertRaises(APIError) as caught:self.publisher.publish([Change(PATH,NEW,BASE)])
            self.assertNotIn(TOKEN,str(caught.exception));self.assertEqual(caught.exception.status,status)
            self.assertEqual(self.api.head,head)
        self.assertTrue(all(json.loads(r.content)['force'] is False for r in self.api.calls if r.method=='PATCH'))

    def test_wrong_blob_sha_and_truncated_tree_never_update_ref(self):
        self.api.wrong_blob=True
        with self.assertRaisesRegex(PublicationError,'computed Git SHA'):
            self.publisher.publish([Change(PATH,NEW,BASE)])
        self.assertFalse(any(r.method=='PATCH' for r in self.api.calls))
        self.api.truncated=True
        with self.assertRaisesRegex(PublicationError,'truncated'):
            self.publisher.publish([Change(PATH,NEW,BASE)])

    def test_tracked_deletion_uses_null_sha(self):
        self.publisher.publish([Change(PATH,None,BASE)])
        final=self.api.trees[self.api.commits[self.api.head]['tree']['sha']]
        self.assertNotIn(PATH,final)
        tree=json.loads(next(r.content for r in self.api.calls if r.method=='POST' and r.url.path.endswith('/trees')))
        self.assertIsNone(tree['tree'][0]['sha'])

    def test_transient_5xx_and_429_bounded_then_success(self):
        self.api.transient=[503,429]
        self.assertEqual(self.publisher.publish([Change(PATH,NEW,BASE)])['status'],'published')
        self.assertEqual(self.sleeps,[1,1])

    def test_unsupported_path_repository_and_oversized_blob_fail_before_requests(self):
        for path in ['../private','/data/live/schedule_today.json','data/live/../private.json',
                     'data/live/secrets.json','data/live/.cache/schedule_today.json']:
            with self.assertRaises(PublicationError):self.publisher.publish([Change(path,b'private',None)])
        self.assertEqual(self.api.calls,[])
        with self.assertRaises(PublicationError):Publisher('owner/repo/../../secrets',TOKEN,client=self.client)
        with patch('publish_public_data.MAX_FILE_BYTES',1):
            with self.assertRaises(PublicationError):self.publisher.publish([Change(PATH,b'xx',None)])


class PublicDataCheckoutTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.git('init','-q');self.git('config','user.name','Fixture')
        self.git('config','user.email','fixture@example.test')
        (self.root/'data/live').mkdir(parents=True)
        (self.root/PATH).write_bytes(OLD);(self.root/'app.py').write_text('original')
        self.git('add','.');self.git('commit','-qm','Fixture baseline')

    def git(self,*args):return subprocess.run(['git','-C',str(self.root),*args],check=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE).stdout

    def test_scope_includes_tracked_untracked_deletion_but_excludes_cache_and_code(self):
        (self.root/PATH).unlink();(self.root/'data/live/2026-11-05.json').write_text('{}')
        (self.root/'data/live/schedule_today.json').write_text('{"current":"fixture"}')
        (self.root/'data/live/schedule_status.json').write_text('{"health":"fixture"}')
        (self.root/'data/live/.cache').mkdir();(self.root/'data/live/.cache/private.json').write_text('secret')
        (self.root/'data/live/private.json').write_text('secret');(self.root/'app.py').write_text('unrelated change')
        _,changes=inspect_changes(self.root,'daily')
        self.assertEqual({c.path for c in changes},{PATH,'data/live/2026-11-05.json'})
        self.assertIsNone(next(c.content for c in changes if c.path==PATH))
        _,schedule=inspect_changes(self.root,'schedule')
        self.assertEqual({c.path for c in schedule},{'data/live/schedule_today.json','data/live/schedule_status.json'})
        self.assertFalse({c.path for c in changes}&{c.path for c in schedule})

    def test_selected_symlink_and_size_bound_rejected(self):
        (self.root/PATH).unlink();(self.root/PATH).symlink_to(self.root/'app.py')
        with self.assertRaisesRegex(PublicationError,'symlink'):inspect_changes(self.root,'daily')
        (self.root/PATH).unlink();(self.root/PATH).write_bytes(NEW)
        with patch('publish_public_data.MAX_FILE_BYTES',1):
            with self.assertRaisesRegex(PublicationError,'size bound'):inspect_changes(self.root,'daily')

    def test_cli_does_not_echo_credentials_or_unexpected_errors(self):
        (self.root/PATH).write_bytes(NEW);output=io.StringIO()
        with patch.dict(os.environ,{'GITHUB_TOKEN':TOKEN,'GITHUB_REPOSITORY':'owner/repo'}), \
                patch('publish_public_data.Publisher',side_effect=RuntimeError(TOKEN)),contextlib.redirect_stderr(output):
            self.assertEqual(main(['--root',str(self.root),'--mode','daily']),1)
        self.assertNotIn(TOKEN,output.getvalue())

    def test_filename_allowlist_rejects_traversal_and_invalid_dates(self):
        self.assertFalse(allowed_path('data/live/2026-02-30.json','daily'))
        self.assertFalse(allowed_path('data/live//schedule_today.json','daily'))
        self.assertTrue(allowed_path('data/live/historical_matchups/tracking_pair_history_abcdef012345_001.jsonl.gz','daily'))
        self.assertFalse(allowed_path('data/live/career_history/career-001.jsonl.gz','schedule'))

    def test_schedule_paths_are_exclusively_owned_and_workflows_run_independently(self):
        for name in ['schedule_today.json','schedule_status.json']:
            path='data/live/'+name
            self.assertTrue(allowed_path(path,'schedule'))
            self.assertFalse(allowed_path(path,'daily'))
        source=Path(__file__).parent
        daily=(source/'nba-daily-data.yml').read_text()
        schedule=(source/'nba-schedule-data.yml').read_text()
        self.assertIn('group: nba-public-data-daily',daily)
        self.assertIn('group: nba-public-data-schedule',schedule)
        self.assertNotIn('group: nba-public-data\n',daily+schedule)
        self.assertIn('publish_public_data.py --mode daily',daily)
        self.assertIn('publish_public_data.py --mode schedule',schedule)


if __name__=='__main__':unittest.main()
