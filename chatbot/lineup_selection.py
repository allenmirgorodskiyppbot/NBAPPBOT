"""Select already-qualified research candidates with explicit lineup coverage.

This layer never relaxes eligibility, invents an offer, or estimates combined
win odds. It only chooses among the ranked candidates supplied by an engine.
"""
from __future__ import annotations

from collections import Counter

from feeds import TEAM_WORDS, canonical_player, canonical_team


def _subject(item):
    return item.get('leg', item)


def _team(item):
    team = canonical_team(_subject(item).get('team'))
    return team if team in TEAM_WORDS else None


def select_lineup(candidates, count, *, required_teams=(), constraints=None):
    """Return ranked distinct-player candidates plus truthful completion metadata.

``candidates`` are in best-first order and have already passed their engine's
evidence gates. For a single game, ``required_teams`` identifies its two teams.
Across games the default is at least two known teams, not every uploaded team.
Incomplete candidates remain useful research, but cannot be called a completed
lineup by consumers checking ``lineup_complete``.
    """
    if type(count) is not int or not 1 <= count <= 6:
        raise ValueError('Request between one and six distinct players.')
    constraints = dict(constraints or {})
    both = constraints.get('both_teams', count >= 2)
    if type(both) is not bool:
        raise ValueError('The both-teams constraint must be true or false.')
    required = {canonical_team(team) for team in required_teams}
    required = {team for team in required if team in TEAM_WORDS}
    unique, seen = [], set()
    for item in candidates:
        name = str(_subject(item).get('player') or '').strip()
        key = canonical_player(name)
        if not name or key in seen:
            continue
        if required and _team(item) not in required:
            continue
        seen.add(key)
        unique.append(item)
    chosen = unique[:count]
    if both and count >= 2:
        selected_teams = {_team(item) for item in chosen} - {None}
        # For one game cover its named teams. Otherwise retain the strongest
        # known team and find the strongest eligible player from another team.
        targets = sorted(required - selected_teams)
        if not required and len(selected_teams) < 2:
            target = next((_team(item) for item in unique
                           if _team(item) and _team(item) not in selected_teams), None)
            targets = [target] if target else []
        for target in targets:
            selected_players = {canonical_player(_subject(item)['player']) for item in chosen}
            replacement = next((item for item in unique if _team(item) == target and
                                canonical_player(_subject(item)['player']) not in selected_players), None)
            if replacement is None:
                continue
            if len(chosen) < count:
                chosen.append(replacement)
                continue
            team_counts = Counter(_team(item) for item in chosen)
            # Never remove the only representative of another required team.
            replace_at = next((i for i in range(len(chosen) - 1, -1, -1)
                               if not _team(chosen[i]) or team_counts[_team(chosen[i])] > 1), None)
            if replace_at is not None:
                chosen[replace_at] = replacement
        # Restore engine ranking for presentation after the coverage swap.
        order = {id(item): index for index, item in enumerate(unique)}
        chosen.sort(key=lambda item: order[id(item)])
    selected_teams = {_team(item) for item in chosen} - {None}
    missing = sorted(required - selected_teams) if both else []
    coverage = len(selected_teams) >= 2 and not missing
    satisfied = not both or coverage
    complete = len(chosen) == count and satisfied
    if both and not satisfied:
        if count < 2:
            note = 'At least two selections are needed to include both teams.'
        elif missing:
            note = ('Two-team coverage is unavailable: need an eligible selection from '
                    + ', '.join(missing) + '. These are partial research candidates, not a complete lineup.')
        else:
            note = ('Two-team coverage is unavailable. Need eligible selections from at least two known teams; '
                    'these are partial research candidates, not a complete lineup.')
    elif len(chosen) < count:
        note = f'Only {len(chosen)} of {count} requested players have eligible evidence; the lineup is incomplete.'
    else:
        note = ('Includes at least one supported player from each team.' if both and required else
                'Includes supported players from at least two teams.' if both else
                'Requested number of distinct players is supported.')
    return chosen, {
        'requested_count': count,
        'lineup_constraints': {'both_teams': both},
        'required_teams': sorted(required),
        'selected_teams': sorted(selected_teams),
        'missing_teams': missing,
        'two_team_coverage': coverage,
        'constraints_satisfied': satisfied,
        'lineup_complete': complete,
        'constraint_note': note,
    }
