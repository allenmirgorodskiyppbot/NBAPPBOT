"""Durable NBA observations and an immutable PrizePicks entry ledger.

Live statistics produce provisional grades. Only explicit reconciliation records
what PrizePicks actually returned; the model never decides settlement.
"""
from __future__ import annotations

import datetime as dt
from contextlib import contextmanager, nullcontext
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
import time
import uuid
import unicodedata


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str, allow_nan=False)


def finite_number(value, label, minimum=None):
    if isinstance(value, bool):
        raise ValueError(f'{label} must be a number.')
    try:
        number = float(value)
    except (ValueError, TypeError):
        raise ValueError(f'{label} must be a number.') from None
    if not math.isfinite(number) or (minimum is not None and number < minimum):
        raise ValueError(f'{label} must be finite and at least {minimum}.')
    return number


def normalized_name(value):
    text = unicodedata.normalize('NFKD', str(value)).encode('ascii', 'ignore').decode().casefold()
    return re.sub(r'[^a-z0-9]', '', text)


STAT_COMPONENTS = {
    'pts': ('pts',), 'reb': ('reb',), 'ast': ('ast',), 'tpm': ('tpm',),
    'fga': ('fga',), 'tpa': ('tpa',), 'minutes': ('minutes',),
    'stl': ('stl',), 'blk': ('blk',), 'tov': ('tov',),
    'pra': ('pts', 'reb', 'ast'), 'pr': ('pts', 'reb'),
    'pa': ('pts', 'ast'), 'ra': ('reb', 'ast'), 'stocks': ('stl', 'blk'),
}
STAT_ALIASES = {
    'points': 'pts', 'rebounds': 'reb', 'assists': 'ast', '3pm': 'tpm',
    '3-pt made': 'tpm', '3 pointers made': 'tpm', 'three pointers made': 'tpm',
    'pts+rebs+asts': 'pra', 'points+rebounds+assists': 'pra',
    'points+rebounds': 'pr', 'points+assists': 'pa', 'rebounds+assists': 'ra',
    'steals': 'stl', 'blocks': 'blk', 'turnovers': 'tov', 'steals+blocks': 'stocks',
}
PLAYER_FIELDS = ['game_id', 'player_id', 'player', 'team', 'opponent', 'date',
                 'season', 'season_type', 'appearance_state', 'pts', 'reb', 'ast',
                 'tpm', 'fga', 'tpa', 'minutes', 'stl', 'blk', 'tov', 'source']
GAME_FIELDS = ['id', 'date', 'status', 'home_team', 'away_team', 'home_score',
               'away_score', 'source', 'start_time', 'provider_game_id']


class MasterStore:
    def __init__(self, path):
        self.path = self.database_path = str(Path(path).resolve())
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.executescript('''
                CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                INSERT OR IGNORE INTO metadata VALUES ('schema_version','1');
                CREATE TABLE IF NOT EXISTS games (
                  id TEXT PRIMARY KEY, date TEXT, status TEXT, home_team TEXT, away_team TEXT,
                  home_score REAL, away_score REAL, source TEXT, start_time TEXT,
                  provider_game_id TEXT, updated_at TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS games_date ON games(date);
                CREATE TABLE IF NOT EXISTS player_games (
                  game_id TEXT NOT NULL, player_id TEXT NOT NULL, player TEXT, team TEXT,
                  opponent TEXT, date TEXT, season TEXT, season_type TEXT, appearance_state TEXT,
                  pts REAL, reb REAL, ast REAL, tpm REAL, fga REAL, tpa REAL, minutes REAL,
                  stl REAL, blk REAL, tov REAL, source TEXT, updated_at TEXT NOT NULL,
                  PRIMARY KEY(game_id,player_id));
                CREATE INDEX IF NOT EXISTS player_games_date ON player_games(date);
                CREATE INDEX IF NOT EXISTS player_games_name ON player_games(player);
                CREATE TABLE IF NOT EXISTS news (
                  id TEXT PRIMARY KEY, title TEXT NOT NULL, url TEXT, published_at TEXT,
                  source TEXT, body TEXT, team TEXT, metadata_json TEXT, updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS feed_status (
                  name TEXT PRIMARY KEY, last_success TEXT, last_error TEXT, last_attempt TEXT,
                  detail TEXT, error TEXT);
                CREATE TABLE IF NOT EXISTS checkpoints (name TEXT PRIMARY KEY, value_json TEXT);
                CREATE TABLE IF NOT EXISTS raw_snapshots (
                  id TEXT PRIMARY KEY, kind TEXT, entity_key TEXT, source TEXT,
                  payload_json TEXT NOT NULL, observed_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS boards (
                  id TEXT PRIMARY KEY, created_at TEXT NOT NULL, source TEXT, legs_json TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS entries (
                  id TEXT PRIMARY KEY, created_at TEXT NOT NULL, source TEXT, stake REAL NOT NULL,
                  entry_type TEXT NOT NULL, payout_multiplier REAL, legs_json TEXT NOT NULL,
                  provisional_status TEXT NOT NULL DEFAULT 'pending',
                  grades_json TEXT NOT NULL DEFAULT '[]', expected_payout REAL,
                  graded_at TEXT, actual_payout REAL, settled_at TEXT, settlement_note TEXT);
                CREATE TABLE IF NOT EXISTS grade_events (
                  id INTEGER PRIMARY KEY AUTOINCREMENT, entry_id TEXT NOT NULL,
                  created_at TEXT NOT NULL, kind TEXT NOT NULL, payload_json TEXT NOT NULL);
            ''')
            # Existing deployments already have schema 1. Preserve their rows
            # while adding the tip-off fields needed by the worker scheduler.
            game_columns = {row['name'] for row in db.execute('PRAGMA table_info(games)')}
            for field in ['start_time', 'provider_game_id']:
                if field not in game_columns:
                    db.execute(f'ALTER TABLE games ADD COLUMN {field} TEXT')
            db.execute("UPDATE metadata SET value='2' WHERE key='schema_version'")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA busy_timeout=30000')
        try:
            with db:
                yield db
        finally:
            db.close()

    def _snapshot(self, db, kind, key, payload):
        # Exclude retrieval timestamps: repeated unchanged observations share one snapshot.
        body = {k: v for k, v in payload.items() if k not in {'updated_at', 'observed_at', 'retrieved_at'}}
        text = encoded(body)
        snapshot_id = hashlib.sha256((kind + ':' + str(key) + ':' + text).encode()).hexdigest()
        db.execute('INSERT OR IGNORE INTO raw_snapshots VALUES (?,?,?,?,?,?)',
                   (snapshot_id, kind, str(key), str(payload.get('source', '')), text, now()))

    def _upsert(self, table, fields, keys, payload, db=None):
        if not all(payload.get(k) is not None and str(payload[k]) for k in keys):
            raise ValueError('Missing required identifier: ' + ', '.join(keys))
        where = ' AND '.join(k + '=?' for k in keys)
        # A bundle supplies one connection so no per-row context can commit a
        # partial box score. Standalone calls retain their existing transaction.
        with (nullcontext(db) if db is not None else self.connect()) as db:
            old = db.execute(f'SELECT * FROM {table} WHERE {where}',
                             tuple(str(payload[k]) for k in keys)).fetchone()
            values = {name: payload[name] if name in payload else (old[name] if old else None)
                      for name in fields}
            for key in keys:
                values[key] = str(values[key])
            if values.get('date'):
                values['date'] = str(values['date'])[:10]
            self._snapshot(db, table, ':'.join(str(values[k]) for k in keys), payload)
            if old and all(old[k] == values[k] for k in fields):
                return False
            names = fields + ['updated_at']
            db.execute(f'INSERT INTO {table} ({",".join(names)}) VALUES '
                       f'({",".join("?" for _ in names)}) ON CONFLICT '
                       f'({",".join(keys)}) DO UPDATE SET '
                       + ','.join(f'{k}=excluded.{k}' for k in names if k not in keys),
                       [values[k] for k in fields] + [str(payload.get('updated_at') or now())])
            return True

    def upsert_game(self, game, *, db=None):
        return self._upsert('games', GAME_FIELDS, ['id'], dict(game), db=db)

    def upsert_player_game(self, stat, *, db=None):
        stat = dict(stat)
        if not stat.get('player_id') and stat.get('player'):
            stat['player_id'] = 'name:' + normalized_name(stat['player'])
        for field in set(PLAYER_FIELDS) & set(STAT_COMPONENTS):
            if field in stat and stat[field] is not None:
                stat[field] = finite_number(stat[field], field, 0)
        return self._upsert('player_games', PLAYER_FIELDS, ['game_id', 'player_id'], stat, db=db)

    def upsert_game_bundle(self, game, rows):
        """Atomically publish one game and its player observations/raw snapshots.

        Readers see either the previous complete version or the committed new
        version, including when a final box score is corrected. Any invalid row
        rolls back every update; the game status is written after its players.
        """
        game = dict(game)
        if game.get('id') is None or not str(game['id']):
            raise ValueError('Game bundle needs a game id.')
        game_id = str(game['id'])
        player_rows = list(rows)
        changed_rows = 0
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            for row in player_rows:
                row = dict(row)
                if str(row.get('game_id', '')) != game_id:
                    raise ValueError('Every player row in a bundle must match its game id.')
                changed_rows += self.upsert_player_game(row, db=db)
            game_changed = self.upsert_game(game, db=db)
        return {'game_changed': game_changed, 'player_rows': len(player_rows),
                'player_rows_changed': changed_rows}

    def record_news(self, item):
        item = dict(item)
        if not str(item.get('title', '')).strip():
            raise ValueError('News title is required.')
        identity = str(item.get('id') or item.get('url') or encoded(item))
        item_id = str(item.get('id') or hashlib.sha256(identity.encode()).hexdigest())
        fields = ['title', 'url', 'published_at', 'source', 'body', 'team']
        values = [item.get(k) for k in fields]
        metadata = encoded({k: v for k, v in item.items()
                            if k not in {'updated_at', 'observed_at', 'retrieved_at'}})
        with self.connect() as db:
            old = db.execute('SELECT metadata_json FROM news WHERE id=?', (item_id,)).fetchone()
            self._snapshot(db, 'news', item_id, item)
            if old and old['metadata_json'] == metadata:
                return False
            db.execute('INSERT INTO news VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET '
                       'title=excluded.title,url=excluded.url,published_at=excluded.published_at,'
                       'source=excluded.source,body=excluded.body,team=excluded.team,'
                       'metadata_json=excluded.metadata_json,updated_at=excluded.updated_at',
                       [item_id] + values + [metadata, now()])
            return True

    def feed_success(self, name, detail=''):
        stamp = now()
        with self.connect() as db:
            db.execute('INSERT INTO feed_status(name,last_success,last_attempt,detail,error) VALUES (?,?,?,?,NULL) '
                       'ON CONFLICT(name) DO UPDATE SET last_success=excluded.last_success,'
                       'last_attempt=excluded.last_attempt,detail=excluded.detail,error=NULL',
                       (name, stamp, stamp, str(detail)[:1000]))

    def feed_failure(self, name, error):
        stamp = now()
        with self.connect() as db:
            db.execute('INSERT INTO feed_status(name,last_error,last_attempt,error) VALUES (?,?,?,?) '
                       'ON CONFLICT(name) DO UPDATE SET last_error=excluded.last_error,'
                       'last_attempt=excluded.last_attempt,error=excluded.error',
                       (name, stamp, stamp, str(error)[:1000]))

    def get_checkpoint(self, name, default=None):
        with self.connect() as db:
            row = db.execute('SELECT value_json FROM checkpoints WHERE name=?', (name,)).fetchone()
            return json.loads(row[0]) if row else default

    def set_checkpoint(self, name, value):
        with self.connect() as db:
            db.execute('INSERT INTO checkpoints VALUES (?,?) ON CONFLICT(name) '
                       'DO UPDATE SET value_json=excluded.value_json', (name, encoded(value)))

    def status(self):
        with self.connect() as db:
            return {'schema_version': 2,
                    'knicks_digest': self.get_checkpoint('knicks_digest_status', {}),
                    'release_checks': self.get_checkpoint('release_checks', {}),
                    'counts': {table: db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
                               for table in ['games', 'player_games', 'news', 'boards', 'entries']},
                    'feeds': [dict(row) for row in db.execute('SELECT * FROM feed_status ORDER BY name')]}

    def games(self, date=None):
        with self.connect() as db:
            if date:
                rows = db.execute('SELECT * FROM games WHERE date=? ORDER BY id', (str(date)[:10],))
            else:
                rows = db.execute('SELECT * FROM games ORDER BY date DESC,id LIMIT 100')
            return [dict(row) for row in rows]

    def recent_news(self, limit=10):
        with self.connect() as db:
            return [dict(row) for row in db.execute('SELECT * FROM news '
                    'ORDER BY COALESCE(published_at,updated_at) DESC LIMIT ?', (max(1, min(int(limit), 100)),))]

    def query(self, sql):
        if not isinstance(sql, str) or not 1 <= len(sql) <= 5000:
            raise ValueError('Use a read-only SELECT of at most 5000 characters.')
        db = sqlite3.connect(Path(self.path).as_uri() + '?mode=ro', uri=True, timeout=15)
        allowed_actions = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ,
                           sqlite3.SQLITE_FUNCTION, sqlite3.SQLITE_RECURSIVE}
        allowed_tables = {'games', 'player_games', 'news', 'feed_status', 'boards', 'entries', 'grade_events'}
        blocked = {'load_extension', 'readfile', 'writefile', 'randomblob', 'zeroblob', 'printf', 'format'}
        deadline = time.monotonic() + 2

        def authorize(action, arg1, arg2, database, trigger):
            if action not in allowed_actions:
                return sqlite3.SQLITE_DENY
            if action == sqlite3.SQLITE_READ and arg1 not in allowed_tables:
                return sqlite3.SQLITE_DENY
            if action == sqlite3.SQLITE_FUNCTION and str(arg2).lower() in blocked:
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        db.set_authorizer(authorize)
        db.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
        db.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 100_000)
        try:
            cursor = db.execute(sql)
            if not cursor.description:
                raise ValueError('Only read-only queries are supported.')
            rows = cursor.fetchmany(101)
            return {'columns': [c[0] for c in cursor.description], 'rows': rows[:100],
                    'truncated': len(rows) > 100}
        finally:
            db.close()

    def _legs(self, legs):
        if not isinstance(legs, list) or not 1 <= len(legs) <= 100:
            raise ValueError('Supply between 1 and 100 selections.')
        cleaned = []
        for leg in legs:
            if not isinstance(leg, dict):
                raise ValueError('Each selection must be an object.')
            item = dict(leg)
            if not str(item.get('player', '')).strip():
                raise ValueError('Each selection needs a player name.')
            stat = str(item.get('stat', '')).strip().lower()
            stat = STAT_ALIASES.get(stat, stat)
            if stat not in STAT_COMPONENTS:
                raise ValueError('Unsupported statistic: ' + stat)
            side = str(item.get('side', '')).strip().lower()
            side = {'over': 'more', 'under': 'less'}.get(side, side)
            if side not in {'more', 'less'}:
                raise ValueError('Each selection needs side more or less.')
            if not item.get('game_id') and not item.get('date'):
                raise ValueError('Each selection needs an exact game_id or game date.')
            if item.get('date'):
                item['date'] = dt.date.fromisoformat(str(item['date'])[:10]).isoformat()
            item.update(player=str(item['player']).strip(), stat=stat, side=side,
                        line=finite_number(item.get('line'), 'line', 0))
            cleaned.append(item)
        return cleaned

    def create_board(self, legs, source='manual'):
        clean = self._legs(legs)
        board_id = uuid.uuid4().hex
        with self.connect() as db:
            db.execute('INSERT INTO boards VALUES (?,?,?,?)', (board_id, now(), str(source), encoded(clean)))
        return self.get_board(board_id)

    def get_board(self, board_id):
        with self.connect() as db:
            row = db.execute('SELECT * FROM boards WHERE id=?', (str(board_id),)).fetchone()
        if not row:
            raise ValueError('Board not found.')
        result = dict(row)
        result['legs'] = json.loads(result.pop('legs_json'))
        return result

    def boards(self, limit=20):
        with self.connect() as db:
            ids = [r[0] for r in db.execute('SELECT id FROM boards ORDER BY created_at DESC LIMIT ?',
                                          (max(1, min(int(limit), 100)),))]
        return [self.get_board(i) for i in ids]

    list_boards = boards

    def create_entry(self, legs, stake, entry_type, payout_multiplier=None, source='manual'):
        clean = self._legs(legs)
        if not 2 <= len(clean) <= 6:
            raise ValueError('A tracked PrizePicks entry must have 2–6 selections.')
        stake = finite_number(stake, 'stake', 0.01)
        entry_type = str(entry_type).lower().replace(' play', '').strip()
        if entry_type not in {'power', 'flex'}:
            raise ValueError('Entry type must be power or flex.')
        if payout_multiplier is not None:
            payout_multiplier = finite_number(payout_multiplier, 'payout_multiplier', 0)
        entry_id = uuid.uuid4().hex
        with self.connect() as db:
            db.execute('INSERT INTO entries(id,created_at,source,stake,entry_type,payout_multiplier,legs_json) '
                       'VALUES (?,?,?,?,?,?,?)',
                       (entry_id, now(), str(source), stake, entry_type, payout_multiplier, encoded(clean)))
            db.execute('INSERT INTO grade_events(entry_id,created_at,kind,payload_json) VALUES (?,?,?,?)',
                       (entry_id, now(), 'created', encoded({'legs': clean, 'stake': stake,
                        'entry_type': entry_type, 'payout_multiplier': payout_multiplier, 'source': source})))
        return self.get_entry(entry_id)

    def get_entry(self, entry_id):
        with self.connect() as db:
            row = db.execute('SELECT * FROM entries WHERE id=?', (str(entry_id),)).fetchone()
        if not row:
            raise ValueError('Entry not found.')
        result = dict(row)
        result['legs'] = json.loads(result.pop('legs_json'))
        result['grades'] = json.loads(result.pop('grades_json'))
        result['settled'] = result['settled_at'] is not None
        payout = result['actual_payout']
        result['net_profit'] = round(payout - result['stake'], 2) if payout is not None else None
        result['financial_result'] = (None if payout is None else
            'won' if payout > result['stake'] else 'lost' if payout < result['stake'] else 'break_even')
        return result

    def list_entries(self, limit=50):
        with self.connect() as db:
            ids = [r[0] for r in db.execute('SELECT id FROM entries ORDER BY created_at DESC LIMIT ?',
                                          (max(1, min(int(limit), 500)),))]
        return [self.get_entry(i) for i in ids]

    def _grade_leg(self, db, leg):
        period = str(leg.get('period') or leg.get('market_period') or 'full_game').lower()
        period = period.replace('-', '_').replace(' ', '_')
        if period not in {'full', 'game', 'full_game'}:
            return {'status': 'review', 'reason': 'Partial-game lines require period-specific statistics.'}
        # A postponed/cancelled game usually has no player box score. Inspect the
        # known event before requiring a player observation, so it cannot stay
        # pending forever merely because the cancellation prevented statistics.
        known_game = None
        if leg.get('game_id'):
            known_game = db.execute('SELECT * FROM games WHERE id=?',
                                    (str(leg['game_id']),)).fetchone()
        elif leg.get('date') and leg.get('team'):
            candidates = db.execute('SELECT * FROM games WHERE date=? AND '
                                    '(UPPER(home_team)=? OR UPPER(away_team)=?)',
                                    (str(leg['date'])[:10], str(leg['team']).upper(),
                                     str(leg['team']).upper())).fetchall()
            if len(candidates) == 1:
                known_game = candidates[0]
        if known_game and any(word in str(known_game['status'] or '').lower()
                              for word in ['postpon', 'cancel', 'suspend']):
            return {'status': 'review', 'game_id': known_game['id'],
                    'source': known_game['source'], 'observed_at': known_game['updated_at'],
                    'reason': 'Postponed/cancelled/suspended game needs platform reconciliation.'}
        selector, values = [], []
        if leg.get('game_id'):
            selector.append('game_id=?'); values.append(str(leg['game_id']))
        if leg.get('date'):
            selector.append('date=?'); values.append(str(leg['date'])[:10])
        if leg.get('team'):
            selector.append('UPPER(team)=?'); values.append(str(leg['team']).upper())
        if leg.get('player_id'):
            selector.append('player_id=?'); values.append(str(leg['player_id']))
        matches = [dict(r) for r in db.execute('SELECT * FROM player_games WHERE ' + ' AND '.join(selector), values)]
        if not leg.get('player_id'):
            matches = [r for r in matches if normalized_name(r['player']) == normalized_name(leg['player'])]
        if not matches:
            return {'status': 'pending', 'reason': 'No matching player-game observation yet.'}
        if len(matches) != 1:
            return {'status': 'review', 'reason': 'Ambiguous player/game match; specify player_id and game_id.'}
        record = matches[0]
        game = db.execute('SELECT * FROM games WHERE id=?', (record['game_id'],)).fetchone()
        if not game:
            return {'status': 'pending', 'reason': 'Game status has not been collected.'}
        status = str(game['status'] or '').lower()
        if any(word in status for word in ['postpon', 'cancel', 'suspend']):
            return {'status': 'review', 'reason': 'Postponed/cancelled/suspended game needs platform reconciliation.'}
        if not (status in {'final', 'post', 'completed', 'closed'} or 'final' in status):
            return {'status': 'pending', 'reason': 'Game is not final.'}
        evidence = {'game_id': record['game_id'], 'player_id': record['player_id'],
                    'source': record['source'], 'observed_at': record['updated_at']}
        appearance = str(record['appearance_state'] or '').lower()
        if appearance in {'dnp', 'did not play', 'inactive', 'out', 'not played'}:
            return {**evidence, 'status': 'dnp', 'reason': 'Explicit DNP; PrizePicks entry reversion needs reconciliation.'}
        if leg['side'] == 'more' and (leg.get('reboot') is True or
                str(leg.get('reboot_status', '')).lower() in {'eligible', 'rebooted'}):
            return {**evidence, 'status': 'review', 'reason': 'Possible Reboot; reconcile the platform settlement.'}
        components = STAT_COMPONENTS[leg['stat']]
        if any(record.get(c) is None for c in components):
            return {**evidence, 'status': 'review', 'reason': 'Required statistic is missing; not assumed zero.'}
        value = sum(record[c] for c in components)
        if math.isclose(value, leg['line'], rel_tol=0, abs_tol=1e-9):
            return {**evidence, 'status': 'tie', 'actual': value,
                    'reason': 'Exact-line tie; PrizePicks reversion rules require reconciliation.'}
        won = value > leg['line'] if leg['side'] == 'more' else value < leg['line']
        return {**evidence, 'status': 'won' if won else 'lost', 'actual': value,
                'reason': 'Provisional statistical result; platform exclusions, Reboot eligibility '
                          'and actual payout are not automatically confirmed.'}

    def grade_entries(self):
        changed = []
        with self.connect() as db:
            # This method reads several rows and then writes grades. Reserve the
            # short write transaction first so a concurrent final publication
            # cannot land between its player-stat and game-status observations.
            db.execute('BEGIN IMMEDIATE')
            entries = [dict(row) for row in db.execute('SELECT * FROM entries')]
            for entry in entries:
                legs = json.loads(entry['legs_json'])
                grades = [self._grade_leg(db, leg) for leg in legs]
                states = [g['status'] for g in grades]
                if any(s in {'review', 'tie', 'dnp'} for s in states):
                    result = 'review'
                elif 'pending' in states:
                    result = 'pending'
                elif all(s == 'won' for s in states):
                    result = 'won'
                elif entry['entry_type'] == 'power' or all(s == 'lost' for s in states):
                    result = 'lost'
                else:
                    result = 'partial'
                payout = (round(entry['stake'] * entry['payout_multiplier'], 2)
                          if result == 'won' and entry['payout_multiplier'] is not None else
                          0.0 if result == 'lost' else None)
                payload = {'provisional_status': result, 'grades': grades, 'expected_payout': payout}
                grade_json = encoded(grades)
                if (entry['grades_json'] == grade_json and entry['provisional_status'] == result
                        and entry['expected_payout'] == payout):
                    continue
                db.execute('UPDATE entries SET provisional_status=?,grades_json=?,expected_payout=?,graded_at=? WHERE id=?',
                           (result, grade_json, payout, now(), entry['id']))
                db.execute('INSERT INTO grade_events(entry_id,created_at,kind,payload_json) VALUES (?,?,?,?)',
                           (entry['id'], now(), 'provisional_grade', encoded(payload)))
                changed.append(entry['id'])
        return [self.get_entry(i) for i in changed]

    def mark_settlement(self, entry_id, payout, note=''):
        payout = finite_number(payout, 'payout (total returned, including stake)', 0)
        with self.connect() as db:
            old = db.execute('SELECT actual_payout,settled_at,settlement_note FROM entries WHERE id=?',
                             (str(entry_id),)).fetchone()
            if old is None:
                raise ValueError('Entry not found.')
            if old['actual_payout'] == payout and old['settlement_note'] == str(note):
                return self.get_entry(entry_id)
            payload = {'actual_payout': payout, 'note': str(note), 'previous': dict(old)}
            db.execute('UPDATE entries SET actual_payout=?,settled_at=?,settlement_note=? WHERE id=?',
                       (payout, now(), str(note)[:2000], str(entry_id)))
            db.execute('INSERT INTO grade_events(entry_id,created_at,kind,payload_json) VALUES (?,?,?,?)',
                       (str(entry_id), now(), 'actual_settlement', encoded(payload)))
        return self.get_entry(entry_id)

    def ledger_summary(self):
        with self.connect() as db:
            entries = [dict(row) for row in db.execute('SELECT * FROM entries')]
        settled = [e for e in entries if e['actual_payout'] is not None]
        stake = sum(e['stake'] for e in settled)
        returned = sum(e['actual_payout'] for e in settled)
        legs = {}
        provisional = {}
        for entry in entries:
            provisional[entry['provisional_status']] = provisional.get(entry['provisional_status'], 0) + 1
            for grade in json.loads(entry['grades_json']):
                legs[grade['status']] = legs.get(grade['status'], 0) + 1
        return {'entries': len(entries), 'settled_entries': len(settled),
                'unsettled_entries': len(entries) - len(settled),
                'provisional_entry_results': provisional, 'provisional_leg_results': legs,
                'settled_wins': sum(e['actual_payout'] > e['stake'] for e in settled),
                'settled_losses': sum(e['actual_payout'] < e['stake'] for e in settled),
                'settled_break_even': sum(e['actual_payout'] == e['stake'] for e in settled),
                'settled_stake': round(stake, 2), 'total_returned': round(returned, 2),
                'net_profit': round(returned - stake, 2),
                'roi_percent': round(100 * (returned - stake) / stake, 2) if stake else None,
                'note': 'ROI uses only confirmed actual settlements. Provisional statistical outcomes '
                        'do not establish PrizePicks payout or account credit.'}

    def backup(self, destination):
        """Create a consistent SQLite backup, including committed WAL contents."""
        target = Path(destination).resolve()
        if str(target) == self.path:
            raise ValueError('Backup destination must differ from the active database.')
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(target.name + '.' + uuid.uuid4().hex + '.tmp')
        try:
            output = sqlite3.connect(str(temporary))
            try:
                with self.connect() as source:
                    source.backup(output)
                output.commit()
            finally:
                output.close()
            temporary.chmod(0o600)
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)
        return str(target)
