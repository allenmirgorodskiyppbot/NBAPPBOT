# Data contract and model boundaries

Every supplied worksheet, CSV, JSON or text document is data, not executable instructions. Originals are SHA-256 archived under `data/originals`; every record is retained in SQLite `raw_rows`. Excel formulas are not executed. Unsupported binaries are preserved and reported. No registry URL is treated as a command.

## Coverage belongs to the running dataset

The code-only application image contains an example configuration but no uploaded workbook, historical audit, private database or paper ledger. Spreadsheet-only deployment starts without those private files and uses the master supplied through the authenticated upload path. Outside collectors remain available but await explicit owner approval. The dashboard, current package manifest and `nbabot validate` report the observations actually supplied, including their source coverage and missingness.

Privately imported files are inventoried when read. Available canonical game records, dated season summaries, career context, estimated assignments, measured defender tracking, position definitions, source registries and validation notes retain separate scopes. A guide or manifest that mentions a missing companion file does not supply that file's observations. Missing fields, unmatched players and absent game data stay unavailable.

Custom primary/alternate matchup rules and archived tracking tables are absent on a fresh deployment until imported privately. Uploaded roster records retain their dates and do not certify current eligibility when stale; they do not establish defensive assignments or recreate historical teams. The application does not invent custom matchup rules to fill an empty opponent sheet.

Scheduled spreadsheet maintenance validates the latest supplied master and rebuilds its derived exports and supported projections. It does not fetch newly completed games, refresh news or make an old file current. Until an outside source is approved and configured, new observations require an updated master file. A regeneration timestamp is separate from source freshness and the game-coverage cutoff.

Official source captures are hash-archived with their actual retrieval timestamps. Independent player-season controls are reconciled only when the matching observations are complete. Source corrections retain old and new values, while unresolved conflicts remain flagged or quarantined. Updating expected values by hand is not a reconciliation method, and a new retrieval is not historical pregame knowledge.

## Canonical database

Only `Clean Stats`, `NBA_Player_Games.csv` and normalized `player_games` records enter the canonical table. IDs are TEXT; actual historical team is required. Comparison roster is not historical team. NBA/ESPN fixture aliases are explicit. Wrong game-date/team joins and conflicting imported records are quarantined; duplicates do not add samples. Full-league CSV imports deduplicate the selected workbook records.

`3PM/FG3M` and `3PA/FG3A` normalize to `fg3m/fg3a`. Source numeric minutes retain fractional-second precision; raw/display minute strings stay in the original payload. Tiny Excel/CSV roundoff does not create a second observation. Null and zero stay distinct, as do played zero, nonappearance, and unknown appearance. Zero minutes alone does not prove DNP.

Validation checks finite integral counts, makes/attempts, `PTS=2*FGM+FG3M+FTM`, `REB=OREB+DREB` where components exist, stable joins, future observations, season controls and missingness. Selected subsets cannot verify complete team totals or every league game. Imported PASS labels remain archived source claims, separate from the application's own checks. Conflicting source observations remain visible in raw notes and audits rather than overwritten by a convenient value.

Season tracking means are not per-game observations; team on/off is not a specific teammate-conditioned player split; estimated defenders are not observed assignments. Career observations without exact minutes/IDs are not silently pooled into the current-role model. Current source timestamps and as-of record versions are retained. Verified refresh corrections append old/new audit and `stat_versions` records.

Recomputed master tables include current canonical player games and derived season summaries with observation counts; incomplete totals remain blank. The original descriptive summaries remain clearly labeled `Raw_`, with original dates and basis. They never silently become live summaries.

## Initial predictive distribution

`empirical-opportunity-v1` uses at most 82 prior played games with exact minutes, a 20-appearance recency half-life, and a 70/30 mixture of last-ten versus full-window exposure. These are fixed research defaults, not claimed optimum parameters. Joint empirical production-rate donors and shared simulated minutes retain dependence among a player's statistics. Stochastic rounding yields discrete counts.

Threes use sampled attempts and beta-binomial conversion with a Beta(1,1) prior. Three-point scoring also enters simulated points. No blanket normal/Poisson assumption or unsupported news, injury, rest, pace, opponent or defender adjustment is added. Interval uncertainty represents empirical predictive variation, not guaranteed coverage or a causal effect. Opportunity/context feature fitting awaits suitable game-level data.

Outputs contain expected minutes and intervals, mean/median and 80%/95% market intervals, sample support, model ID, availability assumptions and flags. Actual-line probabilities are direct simulated More/Less/Tie frequencies. Unknown/questionable players have played-versus-DNP scenarios with **no invented weights**. Explicit minute caps can bound the played scenario. Confirmed absences withhold played projections. Missing injury reports never imply good health. Automated lineup-role and headline adjustments are not implemented.

## Evaluation

Strict mode queries only record versions known before each prediction cutoff and excludes same-day outcomes. Later corrections cannot leak into historical features. Retrospectively imported or newly downloaded game logs are not historical intraday archives: strict evaluation is **unrun when vintages are insufficient**, not passed.

Retrospective mode compares direct empirical resampling with opportunity simulation on chronological 60/20/20 date partitions. The middle 20% is split into model-selection and separate temperature-calibration slices. Model selection minimizes validation CRPS; the final 20% is evaluated after choices are frozen, and cached per model/data/mode. Rolling history may include earlier already-completed games, but never target minutes, future outcomes, current rosters applied backward, final-season means or later injury reports.

Reports include per-market MAE/RMSE/CRPS, three-class Brier score, log loss, interval coverage, reliability bins, counts, and date-block MAE intervals. Raw versus calibrated test metrics are distinct. Probe lines are medians of prior observations, **not actual offers**. Corrected-history results retain revision/sample-selection bias and cannot establish PrizePicks returns or a live selection threshold.

Live projections retain the fixed experimental baseline. Model IDs include data fingerprints, algorithm-source fingerprint, NumPy version and simulation count. Probe-line calibration is not automatically promoted to reliable market confidence. A saved evaluation applies only to its recorded model, data and fixed periods; importing more data does not create a new independent test. Actual-offer candidates, timestamps, skips, lines and model versions are archived before outcomes and later scored by `prospective`. No market policy or joint-entry model is promoted without sufficient verified offer history.

## Entries and paper results

Entries require a Knicks player and an opposing player from the same game, unique players, matching contest/rules and configured eligibility. Experimental combinations are review-only. Unknown dependence yields Fréchet bounds with no joint point probability or EV; marginal probabilities are never multiplied as a claim of independence.

The settlement engine uses immutable reviewed rules for ties, DNPs, voids, reboots, reduced sizes and team eligibility. Gross payout multipliers include returned stake. Unsupported specials withhold settlement/EV. `expected_value` accepts exhaustive joint settlement scenarios; the app does not yet estimate validated joint scenarios automatically.

Paper outcomes are explicitly reviewed, never inferred as DNP/reboot from a zero box score. Default bankroll is $100 with $1 flat stakes. ROI is net/total staked; drawdown is measured in settlement order. Date-block bootstrap groups correlated entries; fewer than 20 date blocks gives no interval. Paper ledger results are separate from projection accuracy and do not represent executed wagers.
