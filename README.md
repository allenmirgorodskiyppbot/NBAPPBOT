# NBAPPBOT — Knicks research and PrizePicks tracking

NBAPPBOT keeps a persistent research dataset, collects relevant NBA news, answers
basketball questions, generates its own projections before any sportsbook upload,
and can compare those with user-supplied PrizePicks lines. The chat model
runs on Akash through Ollama (`qwen3:4b-instruct-2507-q4_K_M`); no OpenAI API key is required. Data
refreshes, reminders, calculations and entry grading run independently of AI replies.

## What refreshes

The refresh scope is the existing workbook's **269 players, 30 teams and 928 named
measured-pair definitions**. The bot does not automatically add players, metric
types or unrelated matchup populations.

- A tracked player's broad season statistics update from games against **any**
  opponent. For example, a Maxey appearance against Cleveland can update his
  overall PPG, minutes and shooting totals.
- A whole-game opponent split changes only when the player faces that opponent.
- Named defender measurements require that actual measured player pairing. An
  unrelated game cannot change Brunson–Maxey defensive FG%.
- Historical seasons, current seasons, whole-game results and tracking samples stay
  separate. Missing values remain missing; DNPs do not become zero-stat appearances.
- Imports validate scope, identities, dates and numeric values before publication.
  Corrected observations replace the corresponding record rather than counting the
  game twice. Season aggregates are not averaged together or added twice.

The original **53-sheet Excel workbook remains a dated reference snapshot**. The
persistent master database and regenerated `NBA_Master.xlsx` export contain the
refreshable observations, current summaries and season calendar. The export also
recalculates the existing split, correlation and coappearance definitions and
builds readable views for the original player/opponent pages. `Tracking_Metrics`
exposes source values and dates in ordinary columns; `Refresh_Coverage` identifies
what could be recomputed and what still requires its own source. The original
formatted workbook is preserved separately; a download does not overwrite a copy
already saved on your computer.

## Morning statistics refresh

The application checks at **11:30 AM America/New_York** for the previous day's
published data, including games unrelated to the Knicks when they update existing
players or teams. It catches up after a restart and retries pending publication
**hourly**. Daylight saving time follows the New York timezone.

After several missed daily runs, every skipped date is queued and retained across
restarts. Catch-up processes at most three game dates per hourly batch, with the
latest required date first. Older gaps remain explicit in `pending_dates`.

Tracking imports have their own hourly retry even after game observations finish.
Only dates actually imported advance game coverage; tracking publication dates
cannot make a missing game appear covered. Older source publications cannot undo
newer game corrections. Each completed collection attempt rebuilds the export,
including source failures and unchanged values, with coverage recorded first.
Failed Excel builds retry independently; a successful rebuild clears its export
error without claiming that unavailable source statistics became current.

The working free source is the public
[SportsDataverse data repository](https://github.com/sportsdataverse/sportsdataverse-data),
including its
[ESPN NBA player observations](https://github.com/sportsdataverse/sportsdataverse-data/releases/tag/espn_nba_player_boxscores)
and [NBA Stats extracts](https://github.com/sportsdataverse/sportsdataverse-data/releases/tag/nba_stats_leaguedash).
The October 30 release check found game observations through **October 29, 2026**.
The separate tracking extracts had October 29 publication timestamps but did not
declare their covered game date. That coverage stays **unknown**; legacy dates
inferred from the game feed are removed without deleting statistical values.
An observation file's newer date cannot advance a tracking file's coverage.

The repository's collection workflow converts the published extracts and retains
only the existing dataset scope. The Akash bot imports the validated files and
regenerates its master export when data changes. Source publication time, last
retrieval and latest covered game date are recorded separately. Neither a newly
started job nor a newly downloaded older file is labeled as today's completed data.
GitHub scheduling and upstream publication can be delayed; this is a morning update
with catch-up, not a guaranteed live feed.

Available extracts support player production, shooting, team totals/opponent
statistics, passing, rebounding, touches/possession and rim-related measurements.
The current publication does **not** provide current overall player defended-FG or
named measured-pair extracts. Those records retain their original dates and show
unavailable/stale coverage. Rim defense, scoring totals and whole-game coappearance
never substitute for those missing populations. Other unsupported tracking fields
also keep their source dates. Inspect `/status` before interpreting a value as current.

The relay also makes a bounded direct NBA Stats attempt for missing individual
defense, rim, named-pair and existing hustle fields (contested shots/deflections),
with a separate six-hour cooldown and a stop after host refusal/timeout. A working
mirror category is preserved if that attempt fails. Source availability is not
guaranteed. Sparse updates keep omitted old fields separately with their original
dates and denominators; they do not mix old counts into a newer total.

Existing calculated splits and correlations are regenerated from final played
observations with season, phase, opponent and same-game populations kept explicit.
AI retrieval can read these same regenerated tables through a bounded, read-only
tool with exact player/season/metric filters. Explicit original-sheet or cell
requests still read the original dated workbook.
Published calendar coverage can update Knicks rest context for matching completed
games; a player's appearance gap cannot establish team rest. Missing shot-location,
rotation, on/off, play-type or measured-defender inputs remain explicit gaps.
Team totals, rim defense and lineup positions are not substitutes for those inputs.

Tentative pairings now share a recheck across direct answers, AI evidence,
projections and Excel. Startup, collection cycles and pregame research check the
existing names against recent team observations and game-specific injury reports.
Conflicts are flagged. The original assignment date remains unchanged unless a
new assignment source exists; rechecking the October 22 estimate is not a new
defender prediction. Available directional measured history is shown separately
from an upcoming coaching assignment. No complete verified pregame-defender feed
has been established.

Manual statistics uploads remain available as an explicit fallback. The bot does
not send box-score recaps or create a new unrelated matchup archive.

## Automatic recovery

Unexpected failures in the news collector, reminder, daily refresh, maintenance
or Telegram service restart that worker with exponential backoff capped at one
minute. `/status` includes `service_health` with retry state and restart counts.
This recovers exited workers; it cannot repair unavailable publishers, exhausted
storage or a stopped Akash deployment.

Telegram delivery errors remain visible separately from polling health. Rate-limit
cooldowns persist across restarts, outgoing delivery is serialized, and `/help`,
`/status` and `/ai_status` stay available when slow-command capacity is full.
Routine retries do not add off-day messages or invoke the AI model.

Database backups continue even if grading notifications or Excel generation fail.
Excel builds run one at a time across scheduled jobs and downloads; the browser
disables repeat download clicks while a file is being prepared. Failed or abandoned
downloads clean up their temporary files. These controls reduce duplicate work
without changing the deployed model or hardware allocation.

## Season calendar and game-day message

The master includes **80 published Knicks regular-season games for 2026–27** from
[the NBA's schedule](https://www.nba.com/team/1610612752/schedule), plus an explicit
notice that two additional games depend on NBA Cup results. Unknown opponents,
dates and tipoffs are not invented. Playoff matchups are not prefilled.

The saved calendar supplies reminder timing even when a live NBA schedule recheck
fails. Such use is labeled as a saved publication, since later changes could be
missed. Known uncertainty windows are preserved; incomplete coverage cannot prove
an off day or a player's rest/workload. Calendar records do not establish final
scores or settle an entry.

The default policy sends **one combined injury/availability/roster digest and
PrizePicks upload reminder, 60 minutes before a Knicks tipoff**. A 7:30 PM Eastern
game produces a 6:30 PM reminder. If that day's lines are already saved, the message
identifies the board and asks for a replacement only if the lines changed.

News collection continues quietly on off days. Routine scoring recaps, promo
articles and individual headline pushes are excluded from the default delivery.
The configured sources are RotoWire NBA RSS, SNY Knicks and Elite Sports NY Knicks.
Reports retain dates, links and publisher/rumor labels. This is partial publisher
coverage, not a complete official injury report; missing news does not prove health.

The durable outbox prevents duplicate digest queuing across restarts. A missed
send time catches up only before tipoff, and an undelivered digest expires at
tipoff. Telegram replies and tracked-entry result notices remain separate from the
one-per-game injury digest.

## Research and projections

Generated explanations receive explicitly labeled evidence: all-opponent season
averages, player-versus-team samples, team production, team defense and tentative
assignments. Coverage dates, source publication dates and assignment dates remain
separate. A newer season average never substitutes for an older measured matchup.

The model receives concise descriptions of the available evidence and its limits,
selects relevant source records, and writes its own qualitative interpretation.
Supported comparisons are calculated only within matching player/season scopes;
a team total does not imply an individual defender's ability. The application
renders numerical facts and their scope directly
from those records, and rejects unverified numerical prose or invalid citations.
This is a general evidence format, not a table of answers for individual players
or questions. Qualitative interpretation can still be wrong and requires review.
Model-written query filters/calculations are explicitly marked for review.

When the needed evidence is available, one compact model request supplies the
explanation. Simple conversation is also model-generated, with a small prompt;
there are no canned greeting or thank-you responses. Questions needing additional
data retain the retrieval tools and use the same final source contract. Invalid
explanations get one bounded repair attempt, then return labeled saved evidence
if still invalid. Rejected replies are never reused from the cache.
Timeouts and model-server failures state the actual failure before showing saved
evidence. Compact prompts carry each selected fact once, with full source metadata
retained for rendering. Short explanations have a smaller output budget and a
180-second total request limit including any repair, with no automatic retry
after a timeout. Telegram displays a typing indicator while generating a reply.
Clearing chat history during a request cannot restore the deleted conversation
when that request completes.
`/ai_status` includes active-job elapsed time and timings for the latest compact
reply phase, including model loading, prompt evaluation and generation when
provided. Those phase counters do not include earlier retrieval calls; `/usage`
counts model work across the paths. Deeper lookups use a small relevant catalog,
the same model context, and at most two tool rounds inside a bounded deadline.
Explicit row/cell requests read the source instead of guessing from season totals.
Provider response time still needs a live Akash check.

Ordinary questions can use player names, unique surnames, supported nicknames,
team cities/names/codes and common name variations. Recent private conversation
context supports follow-ups such as “what about Boston?” Direct averages, supported
measured percentages and dated assignment lookups can answer without AI inference.
A tentative workbook pairing includes its date and source; it is not a confirmed
lineup or proof of who will guard whom.

Uploaded lines receive experimental context-aware research using comparable
regular-season observations, recent workload, production per minute, supported
opponent/rest cohorts, shot volume, source freshness and availability uncertainty.
The report shows the reference projection, workload scenarios, sample scope,
supporting evidence and counterarguments. Missing inputs are disclosed.

Better-supported and higher-variance alternatives use the actual uploaded lines.
An aggressive candidate needs supporting evidence; the engine can abstain rather
than manufacture a risky pick from a simple average. These are experimental
research scenarios, **not calibrated win probabilities or guaranteed advantages**.
Leg frequencies are not multiplied into a claimed parlay win probability.

## Independent projections before PrizePicks

Ask for picks or use `/picks` to generate research from the existing master without
uploading any lines. The default is up to four distinct players for the next
unstarted saved Knicks game. `/picks YYYY-MM-DD 4` requests an explicit game date;
an already-started date is held instead of being silently treated as pregame.
The normal game-day digest includes a compact version of this independent research.

Each item gives the bot's own statistical projection, workload/context evidence,
sample date, and conditional thresholds: **More only at or below** one target, or
**Less only at or above** another. These numbers are research targets, not claimed
PrizePicks offers. The target buffer uses at least 0.5, 15% of the projection, or
25% of the observed p10–p90 scenario spread, rounded outward to half points. This
is an explicit heuristic, not a calibrated likelihood or a proven betting edge.

The research uses saved same-team regular-season workload and production per
minute, observed opponent/rest cohorts, freshness and collected availability
reports. It chooses supported markets and distinct players, rather than adding a
fourth name simply to reach the requested count. Missing opposing-team data is
disclosed; a list supported by only one team is not a complete ordinary multi-team
entry. No board, wager or master-stat record is created by independent research.

Check the targets against the current PrizePicks card, active status and offer terms
when available. Screenshots and `/board` remain optional ways to compare exact
offers and record the selections actually placed. Independent research uses no
additional language-model calls, paid provider, or larger server allocation.

## PrizePicks and the ledger

PrizePicks lines and account information come from the user; there is no direct
sportsbook account connection. Send readable text, CSV/JSON, or a screenshot.
Screenshot intake now reads individual cards with local image preprocessing and
two corroborating OCR passes. Complete, confidently read full-game cards become a
research board automatically; unclear cards stay unresolved. The result is a short
list of up to three distinct players, each with a direction, actual uploaded line,
reason and relevant availability caution. Both visible More/Less buttons are
research alternatives, not an assertion that the user selected either one.

Photos in a Telegram album are combined into one response when received together.
Separate uploads from the same chat/game date are merged for 30 minutes; overlapping
cards are deduplicated. `/newboard` starts a fresh screenshot set without deleting
saved boards or entries. Image documents (PNG/JPEG/WebP) preserve original quality.
Put `YYYY-MM-DD` in a caption for a different game date; otherwise the upload's
Eastern day is explicitly disclosed and must match a saved game for the player's
stored team. Player/market/number confidence, unsupported periods, conflicting
alternate thresholds and unverified offer terms can prevent a recommendation.
Verify every displayed image-read line: OCR agreement is not visual verification.
Goblin and Demon icons are retained by side when readable; their actual payout
terms still require checking. A special quarters-with-points market is not treated
as ordinary full-game points. Unclear special cards do not invalidate unrelated
readable full-game cards.

The shortlist checks saved workload, opponent and schedule evidence plus recent
collected player news. It reuses a recent news refresh rather than making another
request for each picture. It can identify up to two other existing players in the
same scheduled game as **check these lines** ideas, using observed workload/volume.
It does not invent their PrizePicks thresholds, available sides or payout terms.
Insufficient samples and reported absences can produce no pick. The reader and
shortlist use no additional model inference or paid data provider.

Saving available lines does not record a placed entry. An automatic screenshot
board cannot be tracked wholesale: first save only the exact selections actually
placed via `/board`, then `/track` with the actual stake and displayed payout.
Offensive/defensive rebounds use fields already present in the master; unsupported
two-point-attempt and partial-game cards require separate manual analysis.

The bot analyzes saved boards, records entries actually placed, and calculates
provisional outcomes when suitable final statistics become available. DNP/reboot,
tie, partial-market and other settlement exceptions can require review. Confirm the
actual credited **total return including stake** before recording account profit or
loss. Explicit settlement is idempotent; estimated outcomes do not become credited
payouts merely because the model says they won.

## Efficiency, storage and cost

News checks, reminders, imports, averages and grading do not call the chat model.
Unchanged analysis can be reused; identical AI requests use a bounded short-lived
cache that invalidates when their relevant data changes. Model prompts, output and
tool rounds are bounded. `/usage` reports inference requests, cache hits, elapsed
work and available token counters without exposing credentials.
Startup loads the model with the same context size used for compact answers before
reporting ready. An already downloaded model is reused without a registry pull.
Requests keep it resident between conversations while the model
service remains running, using already-reserved RAM without periodic AI calls.

**Akash credits fund the reserved running lease, not individual Telegram messages
or local-model tokens.** Reducing unnecessary inference improves response time and
resource use, but does not itself change the provider's lease price. Changing
reserved resources/pricing is a separate deployment decision. See
[Akash's deployment lifecycle](https://akash.network/docs/learn/core-concepts/deployments/).

Keep `/data` on the existing persistent volume. It contains the master, entries,
conversation context, outbox and local backups. The bot exports Excel/database
files and makes six-hourly local backups, retaining seven daily snapshots. These
share the same provider volume; keep an additional copy outside that provider when
independent backup protection is needed.

## Apply the release and inspect it

1. Open the running Akash deployment, then **Update → chatbot → Vars & secrets**.
2. Set `SOURCE_REF` to the full application commit pinned in [deploy.yaml](deploy.yaml).
3. Set `OLLAMA_MODEL` to `qwen3:4b-instruct-2507-q4_K_M`. This is the explicit
   non-thinking instruction release; the older `qwen3:4b` tag is a different model.
4. Retain the existing Telegram token, allowed user ID, chat password and `/data`
   storage settings, then apply the deployment update.

The bootstrap installs the pinned application modules and dependencies, reuses the
approved workbook, imports the season calendar and applies additive database
changes. Startup checks run automatically and are saved in `/status`; a source
warning is not reported as a successful refresh. The model can take several
minutes to download while the other services continue working. Data updates happen
automatically; future software releases use a new source reference.

Useful Telegram commands:

| Command | Purpose |
| --- | --- |
| `/status` | Dataset scope, refresh results, covered dates, source errors and Telegram health |
| `/reminder` | Next Knicks game/send time, saved-calendar use or timing uncertainty |
| `/usage` | AI work counters, cache reuse and the hosting-billing distinction |
| `/ai_status` | Model readiness, active request and last sanitized failure |
| `/selftest` | Temporary-fixture business checks and read-only live integrity checks |
| `/summary [injuries\|news\|trades]` | Saved sourced reports without waiting for AI |
| `/playerlog NAME \| points \| 5` | Stored game observations and averages |
| `/boards`, `/analyze BOARD_ID` | Saved available lines and their research report |
| `/picks [YYYY-MM-DD] [COUNT]` | Own projections and conditional targets, without an uploaded board |
| `/newboard` | Start a fresh screenshot set; leave saved boards/entries intact |
| `/entries`, `/results` | Recorded entries, provisional outcomes and confirmed account results |
| `/settle ENTRY_ID TOTAL_RETURN [note]` | Reconcile the actual credited return |
| `/importstats` | Caption an explicit CSV/JSON statistics upload |
| `/clear` | Clear recent chat context without deleting the dataset or entries |
| `/help` | Upload formats and the remaining commands |

Self-tests do not place entries, call a sportsbook, inject sample records into the
live master, or establish the quality/speed of a generated AI answer. Checks with
mocked transports are distinct from verified publisher access and actual Akash
message delivery.

## October 30 reliability validation

The preceding chat-reliability release passed **368 automated tests**, **14 isolated PrizePicks workflow
checks**, and selected semantic checks for **10 real-model conversation/retrieval
scenarios**, with targeted reruns after fixes. The scenarios include social replies,
follow-up comparisons, scoped matchups, unavailable official injury coverage,
potential versus actual assists, and an original workbook row checked independently.
See the [validation record](docs/validation-2026-10-30.json) for answers, timings,
revision hashes and limits.

Real inference used official Ollama 0.40.13 in a CPU container capped at four CPUs
and 8 GiB RAM. Successful tested replies took 5.7–37.9 seconds locally, with no OOM
kills. The Instruct-2507 Q4_K_M GGUF came from the public DockerHub model artifact;
byte identity with Ollama's registry artifact was not established. Provider latency,
live Telegram delivery and today's actual PrizePicks lines still need deployment
checks. These examples do not establish universal factual accuracy or betting performance.

The later screenshot/shortlist release has a separate
[validation record](docs/validation-screenshots-2026-10-30.json). It passed **417 automated tests** and **12 isolated saved-master smoke checks**.
Its tests cover synthetic two-column images, spatial pairing, unknown players, ambiguous numbers,
market periods, album/replay handling, alternate offers, event/availability checks,
and isolated saved-master research. Original user screenshots were not available
in the development workspace, so these checks do not establish extraction accuracy
on those actual images or deployment performance.


The subsequent independent-projections release adds `/picks`, the pregame targets
and actual-image reader corrections. **460 automated tests passed.** Its separate validation record is
[here](docs/validation-independent-picks-2026-10-30.json). All four original
PrizePicks screenshots were then available privately for validation: the reader
matched all 19 supported card occurrences (17 distinct markets and 29 visible
sides), including side-specific Standard/Goblin/Demon markers, against independent
visual inspection. Unsupported quarters-with-points and two-point-attempt markets,
and cut-off cards, stayed excluded. No original screenshots were published.


## October 31 existing-statistic refresh validation

The expanded refresh release passed **525 automated tests**. A temporary master
imported actual published October 29–30 game observations and 1,281 current
tracking records, then rebuilt the Excel export without modifying the original
master or creating entries. The export includes 4,953 existing split definitions,
4,137 correlation definitions and 232 rechecked tentative pairings. Unavailable
inputs remain explicitly unavailable. See the [validation record](docs/validation-refresh-2026-10-31.json)
for sample coverage, source limitations and checks. Live Akash activation remains
a separate deployment step.


The November 1 recovery update passed **553 automated tests**, including simulated
multi-day outages, worker crashes, Telegram rate limits, command-capacity pressure,
failed backup/export steps and disconnected downloads. These tests use temporary
fixtures and mocked services; they do not establish live publisher availability or
future pick accuracy. GitHub now runs the regression suite on application changes;
data-only publications do not start redundant test runs.
Run `python chatbot/run_tests.py` from the repository root to use the same test
runner as CI. Async tests have a 30-second deadline; stalled runs print diagnostic
stacks. The test-only event-loop tick also supports idle mocked services in managed
execution environments and is not installed in the deployed chatbot.

Runtime Python packages, including web-framework transitive dependencies, are pinned
to the same tested versions. Download cleanup tests cover both ASGI disconnect
events and send failures using valid protocol messages.
