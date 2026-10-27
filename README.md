# Knicks Projection Desk

A runnable Python application for importing NBA history, maintaining a verified master package, projecting Knicks-game players, and reviewing actual offers. It includes a SQLite audit database, CLI, dashboard, daily/pregame scheduler, chronological evaluation and a paper ledger. It never submits entries.

**Deployment mode:** spreadsheet-only. The code-only image starts without private data; upload the master workbook through an authenticated HTTPS connection to supply its observations. Online data collection is disabled in this mode. The daily job can validate, rebuild exports and regenerate supported projections from the latest supplied master, but it cannot add games or updates absent from that file. Source coverage and missingness remain visible. Projections remain experimental, and persistent automation requires a funded, operating deployment.

For the spreadsheet-only Akash deployment and private workbook upload, follow the [quickstart](docs/AKASH_QUICKSTART.md). The [private-seed guide](docs/AKASH.md) describes the existing verified-backup route when a secure dataset transfer is ready.

## Run

Python 3.12+ is required. From this checkout:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.lock
python -m pip install --no-deps -e .
cp config.example.json config.local.json  # only if no local config exists
nbabot init
nbabot import /path/to/Knicks_2026_27_Matchup_Workbook.xlsx
# Also import the companion CSV directory when supplied.
nbabot validate
nbabot project --games 3
nbabot export
nbabot report
nbabot serve
```

The dashboard listens on port 8000 locally and displays Eastern game times, distributions, actual offers, More/Less/Tie probabilities, injury assumptions, skip reasons, freshness, scheduler state and paper results. Reports are `reports/upcoming.md`, `reports/upcoming.json` and `reports/projections.csv`. Originals and generated data are kept under ignored `data/`.

In the prepared cloud checkout, use `.venv/bin/nbabot`. Each cloud task is isolated; use the existing checkout rather than creating a worktree unless explicitly requested.

## Commands

| Command | Behavior |
|---|---|
| `nbabot import PATH...` | Read supplied workbook/CSV/JSON/text files; preserve originals, raw records and provenance; normalize without double counting. |
| `nbabot bootstrap --seasons 2024-25 2025-26` | Supplement supplied history with official league-wide logs and independently fetched season controls, with reconciliation and an audit archive. |
| `nbabot reference Historical_Data_Audit.xlsx` | Preserve the audited historical reference unchanged in durable storage for organized exports. |
| `nbabot context --seasons 2024-25 2026-27` | Fetch official team-game pace/defense and season opportunity references, separately from game observations and model features. |
| `nbabot backfill-matchups` | Backfill the eight selected careers and their explicitly named matchup games: opportunities, starts, rotations, shots and measured pairs; cache verified sources and retain conflicts. |
| `nbabot organize --output reports/organized --csv` | Build `Knicks_Historical_Organized.xlsx`, checks, missing-data list and change log using the configured seven- or eight-player `organized_players` cohort. |
| `nbabot validate` | Check IDs, conflicts, shooting/components, minutes, joins, future dates, season controls, missingness and coverage. |
| `nbabot refresh` | Collect NBA stats/schedule plus authorized provider sections, validate, audit, publish and regenerate projections. Failure exits 2 and retains the last package. |
| `nbabot source-check` | Check authorized HTTPS feed access and record contract without publishing or exposing credentials. |
| `nbabot export` | Generate versioned Excel/CSV/SQLite package with checksums and stable `data/packages/latest`; never marks old data fresh. |
| `nbabot train` | Register the reproducible initial model specification and data hash. |
| `nbabot evaluate --mode strict` | Chronological checks using historical knowledge timestamps; insufficient vintages are unrun. |
| `nbabot evaluate --mode retrospective` | Corrected-history research comparison, separate selection/calibration slices and frozen test; no real-market return claim. |
| `nbabot project --games 3` | Conditional distributions for tracked Knicks and supported opposing candidates. |
| `nbabot rules rules.json` | Import immutable reviewed contest rules; no assumed current payout table. |
| `nbabot offers board.json` | Import actual CSV/JSON offers and regenerate analysis; screenshot extraction requires review. |
| `nbabot rank` | Review only actually offered sides; archive candidates, explanations and skip reasons. |
| `nbabot entries --size 2` | Construct eligible qualified combinations containing Knicks and opponent players. |
| `nbabot entries --experimental` | Archive review-only combinations; joint probability and EV remain withheld. |
| `nbabot settle ENTRY_ID win void ...` | Record reviewed paper outcomes under the entry's versioned rules. |
| `nbabot prospective` | Score archived actual-offer forecasts once outcomes arrive. |
| `nbabot bankroll` | Report paper stakes, payouts, net, balance, ROI, drawdown and date-block uncertainty. |
| `nbabot report --output reports` | Write Markdown/JSON/CSV reports. |
| `nbabot scheduler status` | Inspect actual heartbeat, jobs and persistent-service evidence. |
| `nbabot scheduler run` | Daily 09:00 Eastern and pregame 120/60/15-minute jobs with retries. |
| `nbabot scheduler retry` | Requeue failed/exhausted jobs after fixing their source problem. |
| `nbabot scheduler install` | Install/start a user systemd service on an available persistent host. |
| `nbabot recover` | Reconcile publication after a crash. |

Use `nbabot --config /absolute/config.local.json COMMAND` for a separate configuration. No credentials go in tracked files. Paper defaults are $100 and $1 flat stakes.

## Inputs still required

1. Additional per-game opportunities and reconstructed rotation stints remain partial, explicitly labeled references. The configured Knicks cohort now includes all eight selected players, including Andre Drummond. The organized workbook excludes game-by-game injury-history tables.
2. Current actual offers with contest, capture evidence and verified eligibility/settlement/payout rules. The `examples/` templates contain placeholders, not invented lines or current-platform assertions.
3. Maintain access to the configured NBA/public ESPN sources. Current-instance refresh has succeeded; future environments must retain the saved network settings. Game-specific clearance and confirmed pregame lineups may remain unavailable and stay unknown. A custom authorized feed is optional using the [provider contract](docs/PROVIDERS_AND_OPERATIONS.md).
4. An always-on host for persistent automation. A running cloud chat process is temporary. Saved environment changes must be reviewed/saved and published separately.

Standard settlement is implemented. Special-offer settlement, promoted market thresholds, production calibration and validated joint dependence require further verified data and adapters; they remain withheld. There is no betting, deposit, or entry-submission integration.

## Test

```bash
python -m unittest discover -s tests -v
```

The suite covers full imports/exports, dashboard requests, reruns, failed updates and publication faults, IDs, minutes, missingness, joins, leakage, trades, postponements, stale/withdrawn offers, shared simulation, reduced-entry settlement, correlation withholding and paper accounting. Synthetic fixtures exist only in temporary test databases.

See [data/model boundaries](docs/DATA_AND_MODELS.md) and [sources, deployment and recovery](docs/PROVIDERS_AND_OPERATIONS.md). A healthy process or retrospective hypothetical-line calibration does not establish supported live picks.

## Akash and daily organized Excel

Follow the [Akash deployment guide](docs/AKASH.md) for the locally tested image, verified data seed, persistent storage, lease creation and operating checks. No paid lease has been created by preparing these files.

Set `organized_players` to the explicit selected cohort of seven or eight stable NBA player IDs or exact names in your local configuration. The current selection includes Brunson, Towns, Anunoby, Hart, Bridges, McBride, Alvarado and Drummond. Run `nbabot reference reports/historical_audit/Historical_Data_Audit.xlsx`, then `nbabot export`. Successful daily and pregame refreshes publish both the canonical `Master_Data.xlsx` and the requested `Knicks_Historical_Organized.xlsx` atomically, with CSVs, checksums and recovery versions. The dashboard provides download links. `nbabot organize --players "Full Name" ...` is a one-off override; edit the configuration to persist the selection for scheduling.

The first eight sheets are the currently selected player pages, the next 29 cover each Knicks opponent from both sides, followed by canonical data/reference sheets. The exporter also supports a separately configured seven-player cohort. Opponent player detail is restricted to the primary/alternate matchup names and actual Knicks fixtures; each player gets their own exact-pair view. Broader named-player season baselines remain separate from individual matchup records. Selected Knicks career rows retain actual historical teams and separate regular seasons from playoffs. The underlying league training database and original source archives are preserved.

Successful daily refreshes also update these scoped historical references when `scoped_history_enabled` is true. Completed captures are cached; failed historical rotation sources have a configurable 24-hour retry cooldown. New prospective feature-cutoff snapshots preserve the actual retrieval time and immutable reference pointers. They do not reconstruct historical pregame knowledge. Newly collected opportunities and rotations are reference data until chronological model validation supports their use; no automatic injury, lineup or headline adjustment is implied.

Package retention defaults to 14 days and at least seven recovery versions. It never deletes canonical records or uploaded originals and never removes the latest verified package. Keep an external backup: provider-local persistent volumes do not migrate automatically to another Akash provider. `package_retention_days: 0` disables pruning.

Optional dashboard authentication uses `NBABOT_DASHBOARD_USER` and `NBABOT_DASHBOARD_PASSWORD` together, through HTTPS ingress. Keep credentials out of Git, image layers and public SDL files. `/health` stays available for process probes; a healthy process does not establish source freshness or a funded Akash lease.
