"""Automatic current NBA scope growth is additive, provider-bound and reversible."""
import asyncio
import copy
import datetime as dt
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from dataset_refresh import (expand_current_player_scope,scope_revision,validate_published_scope,
                             refresh_published_scope,load_scope,initialize_scope)
from feeds import TEAM_WORDS
from feeds import canonical_game
from master import MasterStore

NOW=dt.datetime(2026,11,5,3,tzinfo=dt.timezone.utc)


def scope():
    result={'schema_version':1,'population':'all_current_nba_players','teams':sorted(TEAM_WORDS),
            'players':[{'player':'Existing Player','player_id':'name:existingplayer','espn_player_id':'1234','aliases':[]}]}
    result['revision']=scope_revision(result);return result


def observation(player='New Rookie',identity='5678'):
    return {'player':player,'espn_player_id':identity,'date':'2026-11-03','team':'NYK','opponent':'CHI',
            'season':'2026-27','season_type':'Regular Season','appearance_state':'Played'}


def manifest(bundled,result):
    return {'schema_version':1,'base_revision':bundled['revision'],'season':'2026-27','scope':result,
            'upstream_full_game_validated':True,'source_url':'https://github.com/sportsdataverse/sportsdataverse-data/releases/download/espn_nba_player_boxscores/player_box_2027.parquet',
            'source_published_at':'2026-11-04T15:00:00+00:00','source_checked_at':NOW.isoformat(),
            'source_data_through_date':'2026-11-03','source_content_sha256':'a'*64}


class ScopeDiscoveryTests(unittest.TestCase):
    def missing_kon(self):
        old=scope()
        old['history_identity_source']='https://github.com/sportsdataverse/sportsdataverse-data/releases/download/espn_nba_player_boxscores/player_box_2027.parquet'
        old['players'].append({'player':'Kon Knueppel','nba_player_id':'1642851'})
        old['revision']=scope_revision(old)
        row={**observation('Kon Knueppel','5061575'),'source':old['history_identity_source'],
             'team':'CHA','opponent':'BKN','game_id':canonical_game('2026-11-03','CHA','BKN')}
        return old,row

    def completed_manifest(self,old,result,proofs):
        document=manifest(old,result)
        document['provider_completions']=[{**proof,'source_content_sha256':document['source_content_sha256'],
            'source_published_at':document['source_published_at']} for proof in proofs]
        return document

    def test_known_nba_provider_gap_completes_only_independently_bound_missing_field(self):
        old,row=self.missing_kon();before=copy.deepcopy(old);proofs=[]
        rows=[row,observation('Dalen Terry','4433159'),observation('Dylan Cardwell','4433174')]
        result=expand_current_player_scope(old,rows,provider_completions=proofs)
        self.assertEqual(old,before)
        prior=next(p for p in old['players'] if p['player']=='Kon Knueppel')
        updated=next(p for p in result['players'] if p['player']=='Kon Knueppel')
        self.assertEqual(updated,{**prior,'espn_player_id':'5061575'})
        self.assertEqual(len(result['players']),len(old['players'])+2)
        self.assertEqual(len(proofs),1)
        document=self.completed_manifest(old,result,proofs)
        self.assertEqual(validate_published_scope(document,old,old,NOW),result)
        self.assertEqual(validate_published_scope(document,old,result,NOW),result)
        again=[]
        self.assertEqual(expand_current_player_scope(result,rows,provider_completions=again,
            provider_completion_baseline=old),result)
        self.assertEqual(again,proofs)

    def test_completion_rejects_unknown_registered_id_alias_only_and_old_season(self):
        old,row=self.missing_kon()
        aliases=copy.deepcopy(old);aliases['players'][-1]['aliases']=['Other Full Name']
        variants=[(old,{**row,'espn_player_id':'932'}),(old,{**row,'nba_player_id':'999'}),
            (aliases,{**row,'player':'Other Full Name'}),(old,{**row,'season':'2025-26'}),
            (old,{**row,'source':row['source'].replace('2027','2005')}),
            (old,{**row,'game_id':'invented-event'})]
        for original,changed in variants:
            with self.subTest(changed=changed),self.assertRaises(ValueError):
                expand_current_player_scope(original,[changed])

    def test_ambiguous_source_bindings_do_not_mutate_scope_or_leak_proof(self):
        old,row=self.missing_kon();before=copy.deepcopy(old)
        for other in ({**row,'espn_player_id':'9999'},{**row,'player':'Other Player'}):
            proofs=[]
            with self.subTest(other=other),self.assertRaises(ValueError):
                expand_current_player_scope(old,[row,other],provider_completions=proofs)
            self.assertEqual(old,before);self.assertEqual(proofs,[])

    def test_provider_already_bound_elsewhere_and_known_provider_changes_still_reject(self):
        old,row=self.missing_kon()
        old['players'][0]['espn_player_id']='5061575'
        with self.assertRaises(ValueError):expand_current_player_scope(old,[row])
        old,row=self.missing_kon();old['players'][-1]['espn_player_id']='9999'
        with self.assertRaises(ValueError):expand_current_player_scope(old,[row])

    def test_retired_namesake_id_cannot_fill_a_current_nba_players_missing_provider(self):
        old,row=self.missing_kon()
        old['players'][-1]={'player':'Brandon Williams','nba_player_id':'1630314'}
        old['revision']=scope_revision(old)
        retired={**row,'player':'Brandon Williams','espn_player_id':'932'}
        with self.assertRaisesRegex(ValueError,'independent primary-name/NBA/ESPN binding'):
            expand_current_player_scope(old,[retired])

    def test_missing_provider_manifest_requires_exact_own_source_proof_and_preserves_all_other_fields(self):
        old,row=self.missing_kon();proofs=[]
        result=expand_current_player_scope(old,[row],provider_completions=proofs)
        good=self.completed_manifest(old,result,proofs)
        for change in ('absent','wrong_hash','wrong_pub','old_date','nba','aliases','canonical_id','known_id'):
            document=copy.deepcopy(good)
            if change=='absent':document.pop('provider_completions')
            elif change=='wrong_hash':document['provider_completions'][0]['source_content_sha256']='b'*64
            elif change=='wrong_pub':document['provider_completions'][0]['source_published_at']='2026-11-01T00:00:00Z'
            elif change=='old_date':document['provider_completions'][0]['source_dates']=['2025-11-03']
            else:
                player=next(p for p in document['scope']['players'] if p['player']=='Kon Knueppel')
                if change=='nba':player['nba_player_id']='9999'
                elif change=='aliases':player['aliases']=['New guessed alias']
                elif change=='canonical_id':player['player_id']='name:konknueppel'
                else:document['scope']['players'][0]['espn_player_id']='5061575'
                document['scope']['revision']=scope_revision(document['scope'])
            with self.subTest(change=change),self.assertRaises(ValueError):
                validate_published_scope(document,old,old,NOW)
        without=copy.deepcopy(good);without.pop('provider_completions')
        with self.assertRaises(ValueError):validate_published_scope(without,old,result,NOW)

    def test_new_unique_source_player_added_without_rewriting_prior(self):
        old=scope();before=copy.deepcopy(old)
        new=expand_current_player_scope(old,[observation()])
        self.assertEqual(old,before);self.assertEqual(len(new['players']),2)
        self.assertEqual(new['revision'],scope_revision(new))
        self.assertEqual(validate_published_scope(manifest(old,new),old,old,NOW),new)
        self.assertEqual(expand_current_player_scope(new,[observation()]),new)

    def test_same_name_new_id_and_same_id_new_name_fail(self):
        for row in (observation('Existing Player','5678'),observation('Different Player','1234')):
            with self.assertRaises(ValueError):expand_current_player_scope(scope(),[row])

    def test_old_opt_in_policy_does_not_expand(self):
        old=scope();old.pop('population')
        self.assertEqual(expand_current_player_scope(old,[observation()]),old)

    def test_unknown_id_non_nba_and_championship_are_rejected(self):
        for changes in ({'espn_player_id':''},{'team':'EAST'},
                        {'date':'2025-12-16','team':'NYK','opponent':'SAS'}):
            with self.assertRaises(ValueError):expand_current_player_scope(scope(),[{**observation(),**changes}])

    def test_manifest_cannot_change_prior_or_add_ambiguous_name_or_duplicate_id(self):
        old=scope();new=expand_current_player_scope(old,[observation()])
        for change in ('change_existing','duplicate_id','duplicate_name','remove','unbound'):
            altered=copy.deepcopy(new)
            if change=='change_existing':altered['players'][0]['nba_player_id']='456'
            elif change=='duplicate_id':altered['players'][1]['espn_player_id']='1234'
            elif change=='duplicate_name':altered['players'][1]['aliases']=['Existing Player']
            elif change=='remove':altered['players']=altered['players'][1:]
            else:altered['players'][1].pop('espn_player_id')
            altered['revision']=scope_revision(altered)
            with self.subTest(change=change),self.assertRaises(ValueError):
                validate_published_scope(manifest(old,altered),old,old,NOW)

    def test_manifest_wrong_source_date_revision_season_and_hash_fail(self):
        old=scope();new=expand_current_player_scope(old,[observation()])
        for changes in ({'source_url':'https://example.com/unverified'},
            {'source_data_through_date':'2026-11-06'},{'source_checked_at':'2026-11-06T00:00:00Z'},
            {'base_revision':'other'},{'season':'2025-26'},{'source_content_sha256':''}):
            with self.subTest(changes=changes),self.assertRaises(ValueError):
                validate_published_scope({**manifest(old,new),**changes},old,old,NOW)

    def test_runtime_failed_manifest_preserves_scope_and_hourly_cooldown(self):
        with tempfile.TemporaryDirectory() as directory:
            store=MasterStore(Path(directory)/'master.sqlite');old=scope();store.set_checkpoint('dataset_refresh_scope',old)
            class Worker:
                calls=0
                async def request(inner,url):inner.calls+=1;return {'schema_version':999}
            worker=Worker()
            with patch('dataset_refresh.load_scope',return_value=old):
                first=asyncio.run(refresh_published_scope(store,worker,NOW))
                second=asyncio.run(refresh_published_scope(store,worker,NOW+dt.timedelta(minutes=20)))
            self.assertEqual(first,old);self.assertEqual(second,old);self.assertEqual(worker.calls,1)
            self.assertEqual(store.get_checkpoint('dataset_scope_refresh')['phase'],'retained_prior_scope')

    def test_explicit_manual_force_checks_new_scope_inside_poll_hour_without_freshening_source_clock(self):
        with tempfile.TemporaryDirectory() as directory:
            store=MasterStore(Path(directory)/'master.sqlite');old=scope()
            store.set_checkpoint('dataset_refresh_scope',old)
            changed=expand_current_player_scope(old,[observation()])
            class Worker:
                calls=0
                document=manifest(old,old)
                async def request(inner,url):
                    inner.calls+=1
                    return inner.document
            worker=Worker();later=NOW+dt.timedelta(minutes=20)
            with patch('dataset_refresh.load_scope',return_value=old):
                self.assertEqual(asyncio.run(refresh_published_scope(store,worker,NOW)),old)
                worker.document=manifest(old,changed)
                self.assertEqual(asyncio.run(refresh_published_scope(store,worker,later)),old)
                self.assertEqual(worker.calls,1)
                self.assertEqual(asyncio.run(refresh_published_scope(store,worker,later,force=True)),changed)
            self.assertEqual(worker.calls,2)
            status=store.get_checkpoint('dataset_scope_refresh')
            self.assertEqual(status['last_attempt'],later.isoformat())
            self.assertEqual(status['source_published_at'],worker.document['source_published_at'])
            self.assertEqual(status['source_data_through_date'],'2026-11-03')

    def test_manual_force_preserves_worker_host_cooldown_and_identity_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            store=MasterStore(Path(directory)/'master.sqlite');old=scope()
            store.set_checkpoint('dataset_refresh_scope',old)
            store.set_checkpoint('dataset_scope_refresh',{'phase':'updated','last_attempt':NOW.isoformat()})
            class Worker:
                attempts=0;network_calls=0;cooldown=True
                async def request(inner,url):
                    inner.attempts+=1
                    if inner.cooldown:raise RuntimeError('Host cooldown remains active')
                    inner.network_calls+=1
                    invalid=copy.deepcopy(old);invalid['players'][0]['espn_player_id']='9999'
                    invalid['revision']=scope_revision(invalid)
                    return manifest(old,invalid)
            worker=Worker();later=NOW+dt.timedelta(minutes=20)
            with patch('dataset_refresh.load_scope',return_value=old):
                self.assertEqual(asyncio.run(refresh_published_scope(store,worker,later,force=True)),old)
                self.assertEqual(worker.network_calls,0)
                worker.cooldown=False
                self.assertEqual(asyncio.run(refresh_published_scope(store,worker,later,force=True)),old)
            self.assertEqual(worker.attempts,2);self.assertEqual(worker.network_calls,1)
            self.assertEqual(store.get_checkpoint('dataset_refresh_scope'),old)
            self.assertEqual(store.get_checkpoint('dataset_scope_refresh')['phase'],'retained_prior_scope')

    def test_bundled_real_scope_has_unique_identifiers(self):
        old=load_scope();old_before=copy.deepcopy(old)
        self.assertEqual(validate_published_scope(manifest(old,old),old,old,NOW),old_before)


class ScopeRefreshIntegrationTests(unittest.TestCase):
    def test_daily_new_player_revision_fetches_validated_manifest_before_import(self):
        from nightly_data import NightlyNBARefresh
        from test_nightly_data import snapshot, NOW as IMPORT_NOW, DAY
        from feeds import canonical_player
        with tempfile.TemporaryDirectory() as directory:
            store=MasterStore(Path(directory)/'master.sqlite');bundled=initialize_scope(store)
            updated=expand_current_player_scope(bundled,[observation()])
            document=manifest(bundled,updated)
            document.update(source_checked_at=IMPORT_NOW.isoformat(),source_published_at=IMPORT_NOW.isoformat(),
                            source_data_through_date=DAY)
            data=snapshot();data['scope_revision']=updated['revision']
            rookie={**data['player_games'][0],'player':'New Rookie','player_id':canonical_player('New Rookie'),
                    'espn_player_id':'5678','pts':3}
            data['player_games'].append(rookie);data['games'][0]['home_score']+=3
            calls=[]
            class Worker:
                async def request(inner,url):
                    calls.append(url)
                    if url.endswith('/existing_dataset_scope.json'):return document
                    if url.endswith('/'+DAY+'.json'):return data
                    if url.endswith('/existing_tracking.json'):
                        return {'schema_version':1,'scope_revision':updated['revision'],'season':'2026-27',
                                'records':[],'coverage':[]}
                    raise ValueError('Fixture has no other publication.')
            store.set_checkpoint('nightly_nba_refresh',{'initialized':True,'handled_target_date':'2026-10-28',
                'pending_dates':[],'last_tracking_attempt':IMPORT_NOW.isoformat()})
            asyncio.run(NightlyNBARefresh(store,Worker(),now=lambda:IMPORT_NOW).tick())
            self.assertTrue(calls[0].endswith('/'+DAY+'.json'))
            self.assertTrue(calls[1].endswith('/existing_dataset_scope.json'))
            self.assertEqual(store.get_checkpoint('dataset_refresh_scope')['revision'],updated['revision'])
            with store.connect() as db:
                row=db.execute("SELECT pts FROM player_games WHERE player='New Rookie'").fetchone()
            self.assertEqual(row[0],3)
