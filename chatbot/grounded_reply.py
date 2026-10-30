"""General evidence contracts for generated explanations, independent of player names.

The model selects evidence and writes an interpretation. Statistics, populations,
dates and citations are rendered from the selected records, not rewritten by it.
Source rendering protects numeric wording and population labels. Model-written
query filters/calculations and qualitative interpretation still need review.
"""
from __future__ import annotations

import json
import math
import re


def number(value):
    return f'{value:.2f}'.rstrip('0').rstrip('.') if type(value) in (int,float) and math.isfinite(value) else 'unavailable'


def date(value):
    return str(value)[:10] if value else 'unknown'


METRICS = {
    'pts': ('ppg', 'PPG', 'points_observed'),
    'reb': ('rpg', 'RPG', 'rebounds_observed'),
    'ast': ('apg', 'APG', 'assists_observed'),
    'minutes': ('minutes_per_game', 'minutes/game', None),
    'tpm': ('threes_per_game', 'made threes/game', None),
    'stl': ('steals_per_game', 'steals/game', None),
    'blk': ('blocks_per_game', 'blocks/game', None),
    'tov': ('turnovers_per_game', 'turnovers/game', None),
    'fg_pct': ('fg_pct', 'FG%', None),
    'three_pct': ('three_pct', '3P%', None),
    'ft_pct': ('ft_pct', 'FT%', None),
}


def requested_metrics(question):
    """Select statistic dimensions, never an entity-specific answer template."""
    patterns = {
        'pts': r'\b(?:points?|ppg|scor(?:e|es|ing))\b',
        'reb': r'\b(?:rebounds?|rpg)\b', 'ast': r'\b(?:assists?|apg)\b',
        'minutes': r'\b(?:minutes?|workload)\b',
        'tpm': r'\b(?:threes|three[- ]pointers?|3pm|made threes|made three[- ]point)\b',
        'stl': r'\bsteals?\b', 'blk': r'\bblocks?\b', 'tov': r'\bturnovers?\b',
        'fg_pct': r'\bfg\s*%|field[- ]goal.*?(?:percent|%)|shooting percentage',
        'three_pct': r'(?:three[- ]point|3p|3pt).*?(?:percent|%)',
        'ft_pct': r'\b(?:free[- ]throw|ft)\b.*?(?:percent|%)',
    }
    selected = [key for key, pattern in patterns.items() if re.search(pattern, question, re.I)]
    if 'pts' in selected:
        scoring_text = re.sub(r'\b(?:three|3)[- ]point(?:ers?)?\b', ' ', question, flags=re.I)
        if not re.search(patterns['pts'], scoring_text, re.I):
            selected.remove('pts')
    for compound, components in {'pra': ('pts', 'reb', 'ast'), 'pr': ('pts', 'reb'),
                                 'pa': ('pts', 'ast'), 'ra': ('reb', 'ast'),
                                 'stocks': ('stl', 'blk')}.items():
        if re.search(r'\b' + compound + r'\b', question, re.I):
            selected.extend(components)
    return list(dict.fromkeys(selected)) or ['pts', 'reb', 'ast']


def metric_text(row, metrics, opponent_sample=False):
    games = row.get('n') if opponent_sample else row.get('games', row.get('games_played'))
    values = row.get('averages', {}) if opponent_sample else row
    descriptions = []
    for metric in metrics:
        field, label, count_field = METRICS[metric]
        value = values.get(metric if opponent_sample else field)
        if metric.endswith('_pct'):
            descriptions.append(number(value*100 if type(value) in (int,float) else None) + ' ' + label + ' (shot ratio)')
            continue
        observed = row.get('observed_counts', {}).get(metric)
        if observed is None and count_field:
            observed = row.get(count_field)
        if observed is None and not opponent_sample and row.get('published_at') and value is not None:
            observed = games  # Published complete totals have the declared games denominator.
        text = number(value) + ' ' + label
        if observed is None:
            text += ' (observed count unavailable)'
        elif observed != games:
            text += f' ({observed}/{games} observed)'
        descriptions.append(text)
    return ', '.join(descriptions)


def evidence_cards(context, question=''):
    cards=[]
    def add(kind,text,record,source=None):
        cards.append({'id':f'E{len(cards)+1}','kind':kind,'text':text,
                      'source':source or record.get('source'),
                      'data_through_date':record.get('data_through_date') or record.get('through_date'),
                      'published_at':record.get('published_at')})
    metrics = requested_metrics(question)
    players = context.get('resolved_players', [])
    teams = context.get('resolved_teams', [])
    for row in context.get('assignment_projections',[])[:2]:
        text=(f"{row['selected_player']}–{row['opponent_team']} pairing: {row.get('primary') or 'unavailable'}"
              +(f"; alternate {row['alternate']}" if row.get('alternate') else '')
              +f". Tentative as of {date(row.get('assignment_asof'))}; guarding direction unconfirmed. "
              +f"Source: Matchup_Assignments row {row.get('_row','unknown')}.")
        add('tentative_assignment',text,row,'Matchup_Assignments')
        cards[-1].update(entity=row['selected_player'], opponent=row['opponent_team'],
                         matches_resolved_matchup=(len(players)==1 and len(teams)==1 and
                             players[0]==row['selected_player'] and teams[0]==row['opponent_team']))
    for row in context.get('opponent_player_statistics',[])[:2]:
        n=row.get('n',0)
        text=f"{row.get('player') or row.get('entity')} versus {row['opponent']}, {row['season']} {row.get('season_type','Regular Season')}: "
        if not n:
            text+='no matching completed appearances in the stored sample; coverage is incomplete.'
        else:
            text+=(f"{metric_text(row,metrics,opponent_sample=True)}; {n} stored {'appearance' if n==1 else 'appearances'}, "
                   f"{date(row.get('first_date'))}–{date(row.get('data_through_date'))}; incomplete sample. Source: master player games.")
        add('player_opponent_sample',text,row,'master player games')
    for row in context.get('current_player_statistics',[])[:2]:
        text=(f"{row.get('player') or row.get('entity')}: {metric_text(row,metrics)}; "
              f"{row.get('games',row.get('games_played','unknown'))} games, {row['season']} {row.get('season_type','Regular Season')}, all opponents. "
              f"Coverage through {date(row.get('data_through_date') or row.get('through_date'))}; published {date(row.get('published_at'))}. "
              'Source: '+('published NBA Stats extract.' if row.get('published_at') else 'stored player observations.'))
        add('player_all_opponent_averages',text,row)
    for row in context.get('current_tracking',[])[:8]:
        fields=row.get('fields',{});category=row.get('category');gp=row.get('gp')
        if category=='Player totals':
            continue  # Already represented by the population-labeled season averages.
        if category=='Team defense':
            parts=[]
            if fields.get('points_allowed') is not None and gp:
                parts.append(number(fields['points_allowed']/gp)+' points allowed/game')
            if fields.get('defended_fgm') is not None and fields.get('defended_fga'):
                parts.append(number(100*fields['defended_fgm']/fields['defended_fga'])+'% opponent FG')
            metric=', '.join(parts)
        elif category=='Team totals':
            metric=(number(fields['pts']/gp)+' points scored/game') if gp and fields.get('pts') is not None else ''
        else:
            labels={'potential_assists':'potential assists','passes':'passes','assists':'assists',
                    'rebound_chances':'rebound chances','rebounds':'rebounds','touches':'touches',
                    'possession_minutes':'possession minutes','rim_defended_fgm':'rim FGM allowed',
                    'rim_defended_fga':'rim FGA defended','defended_fgm':'FGM allowed','defended_fga':'FGA defended'}
            metric=', '.join(number(v)+' '+labels[k]+' total' for k,v in fields.items() if k in labels and v is not None)
        if not metric:continue
        text=(f"{row['entity']} {category.lower()}: {metric}; {gp} games, {row['season']}, all opponents. "
              f"Coverage through {date(row.get('data_through_date'))}; published {date(row.get('published_at'))}. Source: published NBA Stats extract.")
        add('tracking_'+str(category),text,row)
    for row in context.get('news',[])[:2]:
        add('publisher_report',f"Publisher report: {row.get('title','')} — {row.get('source','unknown')}, published {row.get('published_at') or 'time unknown'}; not independently verified.",row,row.get('url'))
    # Earlier cards carry assignment and population boundaries. Optional tracking
    # and news cannot crowd those out of the prompt.
    while len(json.dumps(cards,ensure_ascii=False))>6500 and len(cards)>1:
        cards.pop()
    return cards


def required_evidence(question,cards):
    if re.search(r'\b(matchups?|guard(?:s|ing)?|assign(?:ment|ed)?)\b',question,re.I):
        matches = [c['id'] for c in cards if c['kind']=='tentative_assignment' and c.get('matches_resolved_matchup')]
        return matches if len(matches)==1 else []
    return []


def prompt_evidence(cards):
    """Send factual text once; retain full records outside the model prompt.

    Population, source and timestamp labels are already in each card's text.
    Other metadata remains on the original card for validation and citations.
    """
    return [{key: card[key] for key in ('id', 'kind', 'text')} for card in cards]


def compact_evidence(cards, question, max_chars=2600):
    """Choose whole relevant cards for a bounded CPU inference prompt.

    This selects evidence, not an answer. IDs, labels and values are unchanged;
    callers disclose how many records were omitted and retain the full set for
    later retrieval. A record that cannot fit is omitted rather than shortened.
    """
    if max_chars < 2:
        return []
    metrics = set(requested_metrics(question))
    required = set(required_evidence(question, cards))
    shooting = bool(metrics & {'pts', 'tpm', 'fg_pct', 'three_pct', 'ft_pct'})
    defense = bool(re.search(r'\b(?:defen\w*|guard\w*|match\s*ups?)\b', question, re.I))

    def priority(card):
        kind = card.get('kind', '')
        if card.get('id') in required:
            return 0
        if kind in {'query_result', 'workbook_excerpt', 'experimental_analysis'}:
            return 1
        if kind in {'player_opponent_sample', 'player_all_opponent_averages'}:
            return 2
        if kind == 'tentative_assignment':
            return 3
        if kind == 'tracking_Team defense':
            return 4 if shooting or defense else None
        if kind == 'tracking_Passing':
            return 5 if 'ast' in metrics or re.search(r'\bpass(?:es|ing)?\b', question, re.I) else None
        if kind == 'tracking_Rebounding':
            return 5 if 'reb' in metrics else None
        if kind == 'tracking_Possessions':
            return 5 if metrics & {'pts', 'minutes', 'tov'} or re.search(r'\b(?:touches|possessions?)\b', question, re.I) else None
        if kind == 'tracking_Rim':
            # Rim-only defense must not stand in for overall defended shooting.
            return 5 if re.search(r'\b(?:rim|paint|interior)\b', question, re.I) else None
        if kind == 'tracking_Team totals':
            return 7 if re.search(r'\b(?:team|offen\w*|pace)\b', question, re.I) else None
        if kind == 'publisher_report':
            return 4 if re.search(r'\b(?:news|injur\w*|available|availability|lineup|trade\w*)\b', question, re.I) else 6
        if kind == 'collection_status':
            return 1 if re.search(r'\b(?:updat\w*|fresh\w*|dataset|status)\b', question, re.I) else 6
        return 7

    ranked = [(priority(card), index, card) for index, card in enumerate(cards)]
    ranked = sorted((item for item in ranked if item[0] is not None), key=lambda item: item[:2])
    selected = []
    for _, _, card in ranked:
        candidate = selected + [card]
        size = len(json.dumps(prompt_evidence(candidate), ensure_ascii=False, separators=(',', ':')))
        if size <= max_chars:
            selected.append(card)
    return selected


def reply_schema(cards):
    items={'type':'string'}
    if cards:items['enum']=[c['id'] for c in cards]
    return {'type':'object','additionalProperties':False,
            'required':['evidence_ids','analysis','needs_more_data'],
            'properties':{'evidence_ids':{'type':'array','maxItems':3 if cards else 0,'items':items},
                          'analysis':{'type':'string','maxLength':700},
                          'needs_more_data':{'type':'boolean'}}}


def render_reply(document,cards,required=()):
    if not isinstance(document,dict) or set(document)!={'evidence_ids','analysis','needs_more_data'}:
        raise ValueError('Invalid evidence response schema.')
    ids=document['evidence_ids'];analysis=document['analysis'];more=document['needs_more_data']
    by_id={c['id']:c for c in cards}
    if (not isinstance(ids,list) or len(ids)>3 or any(not isinstance(i,str) or i not in by_id for i in ids)
            or len(ids)!=len(set(ids)) or not isinstance(analysis,str) or len(analysis)>700 or type(more) is not bool):
        raise ValueError('Invalid evidence selection.')
    if more:
        if analysis.strip():raise ValueError('A retrieval request cannot assert an answer before evidence arrives.')
        return None
    if any(i not in ids for i in required):
        raise ValueError('The requested stored assignment was omitted.')
    if not analysis.strip() and not ids:
        raise ValueError('Empty evidence response.')
    # Numerical facts and their population/date labels must come from a source
    # card. This catches the demonstrated relabeling even when all digits exist
    # elsewhere in the prompt. It is not a claim to validate all qualitative prose.
    if re.search(r'\d|\b(?:zero|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty|thirty|forty|fifty|hundred|thousand)\b',analysis,re.I):
        raise ValueError('Numerical assertions must use the source evidence.')
    # A conversational reply may mention an entity without making a statistical
    # assertion. It remains generated by the model even when topic cards exist.
    selected=[by_id[i] for i in ids]
    answer='\n\n'.join([*(c['text'] for c in selected),analysis.strip()]).strip()
    return {'answer':answer,'sources':[{'tool':'scoped_evidence','evidence_id':c['id'],
             'population_kind':c['kind'],'source':c.get('source'),
             'data_through_date':c.get('data_through_date'),'published_at':c.get('published_at'),
             **({'query':c['query'],'excerpt':c.get('excerpt',False)} if c.get('query') else {})} for c in selected],
            'grounding':'Source-selected factual text; AI-generated qualitative interpretation.'}


def unverified_reply(cards,required=(),failure_message=None):
    selected=[c for c in cards if c['id'] in required]
    selected.extend(c for c in cards if c not in selected)
    selected=selected[:2]
    return {'answer':(failure_message or 'I could not verify the AI explanation.')+(' Here is the saved evidence:\n\n'+'\n\n'.join(c['text'] for c in selected) if selected else ''),
            'sources':[{'tool':'scoped_evidence','source':c.get('source')} for c in selected],
            'ai_used':False,'ai_failure_category':'grounding_error'}
