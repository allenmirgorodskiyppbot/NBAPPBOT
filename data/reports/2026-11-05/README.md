# Exact remaining NBA master gaps — November 5, 2026

The exported master contains 171,889 player observations and 21,198 games.
All 16 basic counting/minutes fields are populated in its 153,677 saved final
Played observations, including the saved current-season Played sample.
A populated saved sample does not prove every expected career game was recovered.

- **226 historical starter flags**, across 123 players, January 29, 2013–March 5, 2026.
- **3 LeBron James plus/minus values:** November 19 and 21, and December 2, 2003.
  The source minutes conflict, so the unknown values were held rather than guessed.
- **2 final-score fields** for NYK–ORL, December 13, 2025. Source venue headers conflict;
  known game identities and home/away fields were preserved.

[Every missing player field, game and reason](missing_player_statistics.csv) ·
[Missing game-score fields](missing_game_scores.csv) · [Machine-readable summary](summary.json)

For starter flags, 100 lack exactly five explicit named starters on both rosters;
117 have unresolved source athlete identities; 6 conflict with known statistics;
2 lack qualifying flag evidence and 1 lacks a verified Played source observation.

Additional gaps are distinct from player box-score blanks:

- Overall closest-defender FG made, FG attempted and FG% allowed.
- Under-six-foot closest-defender FG made, FG attempted and FG% allowed.
- Eight aggregate tracking categories have no verified covered-game date.
- Eight configured player pairings have no observed measured matchup in the available
  archive; see the summary for their exact names and directions. The new archive begins
  in 2017–18, so earlier lifetime guarding measurements are not claimed.
- 104 overlapping legacy/archive matchup observations disagree and remain held.
- Six additional measured fields (potential assists, blocks, help blocks, help FG made,
  help FG attempted and switches) are unknown in 446 retained legacy observations.
- Confirmed future starters, expected minutes and restrictions are unknown unless an
  explicit trusted report supplies them. Missing injury rows do not imply healthy.
- A complete historical PrizePicks offer, available-side and payout archive is absent.

The three remaining uncertain rejected-source appearances are Mike Conley on
November 24, 2007 (MEM/WAS) and January 26, 2008 (MEM/LAC), and Al Horford on
January 16, 2009 (ATL/GSW). Those sources show -- minutes without an explicit DNP.
These are participation uncertainties, not confirmed Played appearances with zero stats.
Other saved Unknown observations remain separately labeled; the full master has 806.

In Excel, open Missing_Stats_By_Game, Game_Stat_Gaps, Tracking_Field_Coverage and
Unknown_Participation. Publication time, report time and covered-game dates remain
separate. The automatic Akash upgrade merges these verified sources and rebuilds the
export without transferring the large workbook through chat.
