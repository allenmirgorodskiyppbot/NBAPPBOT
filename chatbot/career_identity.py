"""Resolve explicitly documented NBA player aliases without fuzzy name merges."""
from __future__ import annotations

from functools import lru_cache
import datetime as dt
import json
from pathlib import Path
from zoneinfo import ZoneInfo

from feeds import canonical_player
from master import normalized_name

TARGETS_PATH = Path(__file__).with_name('matchup_history_targets.json')


@lru_cache(maxsize=8)
def _bundled(path, modified):
    document = json.loads(Path(path).read_text())
    return document.get('players', [])


def _known_rows(store, path):
    rows = []
    target = Path(path)
    if target.exists():
        rows.extend(_bundled(str(target.resolve()), target.stat().st_mtime_ns))
    if callable(getattr(store, 'get_checkpoint', None)):
        saved = store.get_checkpoint('career_history_identity_scope', {})
        if isinstance(saved, dict):
            rows.extend(saved.get('players', []))
            for item in saved.get('target_resolution', []):
                if isinstance(item, dict):
                    rows.append(item)
    return [row for row in rows if isinstance(row, dict)]


def resolve_career_identity(store, player, *, targets_path=TARGETS_PATH):
    """Return only aliases explicitly tied to one provider identity.

    A current namesake or an ambiguous alias remains unresolved. The bundled
    crosswalk wins canonical spelling; rows are never renamed in the database.
    """
    player = str(player or '').strip()
    default = {'player': player, 'player_id': canonical_player(player),
               'aliases': [player] if player else [],
               'player_ids': [canonical_player(player)] if player else [],
               'espn_player_id': None, 'ambiguous': False, 'identity_source': None}
    wanted = normalized_name(player)
    if not wanted:
        return default
    rows = _known_rows(store, targets_path)

    def aliases(row):
        values = [row.get('player'), row.get('requested_player'),
                  row.get('canonical_player'), *row.get('aliases', [])]
        return {str(value).strip() for value in values if isinstance(value, str) and value.strip()}

    matching = [row for row in rows if wanted in {normalized_name(value) for value in aliases(row)}
                and str(row.get('espn_player_id') or '').isdigit()]
    providers = {str(row['espn_player_id']) for row in matching}
    if len(providers) != 1:
        return {**default, 'ambiguous': len(providers) > 1}
    provider = next(iter(providers))
    connected = [row for row in rows if str(row.get('espn_player_id') or '') == provider]
    approved = set().union(*(aliases(row) for row in connected))
    # Do not borrow an alias already bound to a different historical provider.
    for name in approved:
        bound = {str(row['espn_player_id']) for row in rows
                 if str(row.get('espn_player_id') or '').isdigit()
                 and normalized_name(name) in {normalized_name(value) for value in aliases(row)}}
        if len(bound) > 1:
            return {**default, 'ambiguous': True}
    canonical = str(matching[0].get('canonical_player') or matching[0].get('player') or player)
    approved.update((canonical, player))
    return {'player': canonical, 'player_id': canonical_player(canonical),
            'aliases': sorted(approved),
            'player_ids': sorted({canonical_player(name) for name in approved}),
            'espn_player_id': provider, 'ambiguous': False,
            'identity_source': 'Documented matchup/provider identity crosswalk'}


def career_player_key(player, store=None):
    return resolve_career_identity(store, player)['player_id']


def matchup_career_coverage(store, observations, games, *, now=None, targets_path=TARGETS_PATH):
    """Describe actual saved eligible history for every explicitly named participant."""
    now = now or dt.datetime.now(dt.timezone.utc)
    today = now.astimezone(ZoneInfo('America/New_York')).date().isoformat()
    game_map = {row['id']: row for row in games}
    indexed = {}
    for row in observations:
        game = game_map.get(row.get('game_id'), {})
        if (game.get('status') != 'final' or game.get('date') != row.get('date')
                or str(row.get('appearance_state') or '').lower() != 'played'
                or row.get('season_type') != 'Regular Season'
                or not row.get('date') or row['date'] > today):
            continue
        indexed.setdefault(normalized_name(row['player']), []).append(row)
    target = Path(targets_path)
    targets = _bundled(str(target.resolve()), target.stat().st_mtime_ns) if target.exists() else []
    results = []
    for item in targets:
        name = item.get('requested_player') or item.get('player')
        if not name:
            continue
        identity = resolve_career_identity(store, name, targets_path=targets_path)
        rows = [row for alias in {normalized_name(value) for value in identity['aliases']}
                for row in indexed.get(alias, []) if row.get('player_id') in identity['player_ids']]
        grouped = {}
        for row in rows:
            grouped.setdefault(row['game_id'], []).append(row)
        # Two aliases of one person never become two played games. Conflicting
        # statistics are visible rather than resolved by picking a favorable row.
        unique = [values[0] for values in grouped.values()]
        fields = ('pts', 'reb', 'ast', 'fga', 'tpa', 'tpm', 'minutes')
        conflicts = sum(any(len({row.get(field) for row in values if row.get(field) is not None}) > 1
                            for field in fields) for values in grouped.values())
        versus = [row for row in unique if row.get('opponent') == 'NYK']
        dates = [row['date'] for row in unique]
        results.append({'requested_player': name, 'canonical_player': identity['player'],
            'espn_player_id': identity['espn_player_id'], 'nba_player_id': item.get('nba_player_id'),
            'status': 'ambiguous_identity' if identity['ambiguous'] else
                      'available_published_history' if unique else 'missing_played_history',
            'played_regular_games': len(unique), 'duplicate_alias_rows': len(rows) - len(unique),
            'stat_conflicting_games': conflicts,
            'seasons': sorted({row.get('season') for row in unique if row.get('season')}),
            'first_game': min(dates) if dates else None, 'last_game': max(dates) if dates else None,
            'regular_games_against_NYK': len(versus),
            'history_completeness': 'Available saved history; complete career inventory is not independently verified.',
            'matchup_scope': 'Whole-game opponent history; these counts do not establish measured guarding.'})
    return results
