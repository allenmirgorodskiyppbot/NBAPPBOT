"""Known statistical-phase exclusions, separate from team coappearance history.

ESPN season_type=2 includes All-Star events and the NBA Cup championship.
These confirmed championship identities do not contribute to regular-season
statistics. Future championships need their own published phase evidence.
"""
from __future__ import annotations

from feeds import TEAM_WORDS, canonical_team

CUP_FINALS = {
    '401607495': {'date': '2023-12-09', 'teams': {'LAL', 'IND'},
        'source': 'https://www.nba.com/pacers/news/rewind-pacers-lakers-231209'},
    '401734908': {'date': '2024-12-17', 'teams': {'MIL', 'OKC'},
        'source': 'https://www.nba.com/game/mil-vs-undefined-0062400001'},
    '401809839': {'date': '2025-12-16', 'teams': {'NYK', 'SAS'},
        'source': 'https://www.nba.com/game/sas-vs-nyk-0062500001'},
}


def regular_phase_exclusion(row):
    """Return a documented exclusion for raw publisher or normalized observations."""
    day = str(row.get('date') or row.get('game_date') or '')[:10]
    team = canonical_team(row.get('team') or row.get('home_team') or row.get('team_abbreviation'))
    other = canonical_team(row.get('opponent') or row.get('away_team') or row.get('opponent_team_abbreviation'))
    if team not in TEAM_WORDS or other not in TEAM_WORDS or team == other:
        return {'reason': 'Non-franchise exhibition/All-Star or unresolved team identity'}
    provider = str(row.get('provider_game_id') or row.get('game_id') or '')
    for event, known in CUP_FINALS.items():
        matches = day == known['date'] and {team, other} == known['teams']
        if provider == event and not matches:
            raise ValueError('Known championship provider identity conflicts with its date or franchises.')
        if matches:
            return {'reason': 'NBA Cup championship excluded from regular-season statistics',
                    'provider_game_id': event, 'source': known['source']}
    phase = row.get('nba_season_type') or row.get('season_type')
    if phase is not None and str(phase) not in {'2', 'Regular Season'}:
        return {'reason': 'Source labels a different statistical phase'}
    return None


def published_event_exclusion(event, game):
    """Use explicit event metadata for later championships; never infer a date."""
    import re
    known=regular_phase_exclusion(game)
    if known:return known
    season=event.get('season') or {}
    if season.get('type') not in (None,2,'2'):
        return {'reason':'Independent event labels a different statistical phase','source':game.get('source')}
    competition=(event.get('competitions') or [{}])[0]
    notes=competition.get('notes') or []
    texts=[note.get('headline','') for note in notes if isinstance(note,dict)]
    texts.extend(str(value or '') for value in (event.get('name'),competition.get('name')))
    for text in texts:
        if re.search(r'\b(?:NBA\s+Cup|In[ -]Season\s+Tournament)\s*(?:[-–:]\s*)?(?:championship(?:\s+game)?|final)\s*$',text,re.I):
            return {'reason':'Independent event explicitly labels the NBA Cup championship',
                    'source':game.get('source'),'published_label':text}
    return None
