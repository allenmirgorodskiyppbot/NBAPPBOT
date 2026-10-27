"""Explicit workbook player scope; estimated assignments never certify rosters."""
from __future__ import annotations

import json
import re
import unicodedata
from collections import defaultdict

from .core import digest


def normalized_name(value):
    value=unicodedata.normalize("NFKD",str(value))
    value="".join(c for c in value if not unicodedata.combining(c))
    return " ".join(re.sub(r"[^a-z0-9]+"," ",value.casefold()).split())


def matchup_scope(store, selected=None):
    from .organized import resolve_players
    if selected is not None and all(isinstance(p,dict) for p in selected):
        selected=[p["id"] for p in selected]
    selected=resolve_players(store,selected)
    selected_names={normalized_name(p["name"]):p for p in selected}
    selected_ids={p["id"] for p in selected}
    by_name=defaultdict(list)
    for player in store.rows("SELECT id,name FROM players ORDER BY id"):
        by_name[normalized_name(player["name"])].append(player)
    pairs={}; unresolved={}; by_opponent=defaultdict(set)
    for raw in store.rows("SELECT payload FROM raw_rows WHERE sheet='Matchups' ORDER BY file_hash,row_number"):
        row=json.loads(raw["payload"])
        knicks=selected_names.get(normalized_name(row.get("Knicks player","")))
        team=str(row.get("Opponent") or "")
        if not knicks or not team or team=="NYK":
            continue
        for field,assignment in (("Suspected defender","primary"),("Alternate defender","alternate")):
            name=row.get(field)
            if name in (None,""):
                continue
            matches=by_name.get(normalized_name(name),[])
            if len(matches)!=1:
                record={"knicks_player":knicks["name"],"opponent_team":team,"name":name,
                    "assignment":assignment,"reason":"unknown_name" if not matches else "ambiguous_name"}
                unresolved[digest(record)]=record
                continue
            opponent=matches[0]
            if opponent["id"] in selected_ids:
                record={"knicks_player":knicks["name"],"opponent_team":team,"name":name,
                    "assignment":assignment,"reason":"selected_knicks_identity_in_opponent_assignment"}
                unresolved[digest(record)]=record
                continue
            by_opponent[team].add(opponent["id"])
            record={"knicks_id":knicks["id"],"knicks_name":knicks["name"],
                "opponent_id":opponent["id"],"opponent_name":opponent["name"],
                "opponent_team":team,"assignment":assignment,
                "assignment_status":row.get("Assignment status"),
                "scope_note":"Uploaded estimated matchup; not a measured assignment or verified current roster"}
            pairs[digest(record)]=record
    opponent_ids=set().union(*by_opponent.values()) if by_opponent else set()
    return {"selected":selected,"selected_ids":sorted(selected_ids),
        "by_opponent":{team:sorted(ids) for team,ids in sorted(by_opponent.items())},
        "opponent_ids":sorted(opponent_ids),"all_ids":sorted(selected_ids|opponent_ids),
        "pairs":list(pairs.values()),"unresolved":list(unresolved.values()),
        "meaning":"Selected Knicks and explicitly named primary/alternate matchups; full rosters excluded"}


def matchup_games(store, seasons=None, career_rows=None):
    """Historical fixtures come from actual game affiliations, not roster snapshots."""
    seasons=set(seasons or ["2024-25","2025-26",store.config["season"]])
    games={}
    for row in store.rows("""SELECT DISTINCT pg.game_id,pg.date_et,pg.season,pg.team,pg.opponent,g.tipoff
        FROM player_games pg LEFT JOIN games g ON g.id=pg.game_id
        WHERE pg.team='NYK' OR pg.opponent='NYK' ORDER BY pg.date_et,pg.game_id"""):
        if row["season"] not in seasons:
            continue
        game_id=str(row["game_id"])
        if not game_id.startswith(("002","004")):
            continue
        opponent=row["opponent"] if row["team"]=="NYK" else row["team"]
        if not opponent or opponent=="NYK":
            continue
        existing=games.get(game_id)
        if existing and (existing["opponent"]!=opponent or existing["date_et"]!=row["date_et"]):
            raise ValueError("Conflicting actual historical Knicks fixture: "+game_id)
        games[game_id]={"game_id":game_id,"date_et":row["date_et"],"season":row["season"],
            "season_type":"Playoffs" if game_id.startswith("004") else "Regular Season",
            "opponent":opponent,"tipoff":row.get("tipoff")}
    for row in career_rows or []:
        if row.get("team")!="NYK" or row.get("season") not in seasons:
            continue
        game_id=str(row.get("game_id") or "")
        if not game_id.startswith(("002","004")) or not row.get("opponent"):
            continue
        date=str(row.get("date_et") or "")[:10]
        candidate={"game_id":game_id,"date_et":date,"season":row["season"],
            "season_type":row.get("season_type") or ("Playoffs" if game_id.startswith("004") else "Regular Season"),
            "opponent":row["opponent"],"tipoff":None}
        existing=games.get(game_id)
        if existing and (existing["opponent"]!=candidate["opponent"] or existing["date_et"]!=date):
            raise ValueError("Career/canonical Knicks fixture conflict: "+game_id)
        games.setdefault(game_id,candidate)
    return sorted(games.values(),key=lambda r:(r["date_et"],r["game_id"]))


def game_player_ids(scope, opponent):
    return set(scope["selected_ids"])|set(scope["by_opponent"].get(opponent,[]))
