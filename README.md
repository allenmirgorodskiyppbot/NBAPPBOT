# NBAPPBOT — Knicks research and PrizePicks tracking

NBAPPBOT keeps a persistent research dataset, collects relevant NBA news, answers
basketball questions, generates its own projections before any sportsbook upload,
and can compare those with user-supplied PrizePicks lines. The chat model
runs on Akash through Ollama (`qwen3:4b-instruct-2507-q4_K_M`); no OpenAI API key is required. Data
refreshes, reminders, calculations and entry grading run independently of AI replies.

## November 8 team lookup and inventory repair

Tested application `SOURCE_REF`: `bc8a593317512379648f01c36e0620858c390729`.
The [release regression run](https://github.com/allenmirgorodskiyppbot/NBAPPBOT/actions/runs/42504231477)
passed all **1,719 tests**. Update the existing Akash deployment to activate this version; keep its persistent volume and private settings.
The [conversation and calendar validation report](data/reports/2026-11-05/natural_conversation_validation.json) records the actual model trials, workbook and source limits.
The [live-test repair report](data/reports/2026-11-05/selftest_timeout_validation.json) records the self-test timeout, game-date context and reply-validation fixes.
The [prop-explanation validation report](data/reports/2026-11-05/prop_explanation_validation.json) records the final source, five actual-model explanation trials, rejected drafts and remaining wording limits.
The [master/schedule/reminder validation report](data/reports/2026-11-07/master_schedule_reminder_validation.json) records the Nov7 live schedule recovery, four-comparison master-data controls and ninety-minute reminder integration tests.

The [team lookup and inventory validation report](data/reports/2026-11-08/team_inventory_validation.json) records the actual Nov. 7 source conflict, a complete lookup/import/projection check on a disposable copy of the real master, and the cached request timing.

`/picks` automatically selects the next saved Knicks matchup and loads those two teams from the master. It reads recent roster records and returns saved research without waiting for a network refresh. Regular workers and `/refresh` own updates. The owner's standing review preferences are saved in [AGENTS.md](AGENTS.md).

Historical game coverage is now verified separately for each team. An incorrect Indiana–Denver home/away header on Nov. 7 no longer blocks valid Knicks coverage or Milwaukee's verified rest day. The original conflicting row and whole-day unverified status remain intact. Team certificates retain exact game IDs, three public source records, checksums and publication clocks. The refresh queue includes uncovered intervening dates for both matchup teams while retaining its bounded batches and retry intervals.

When current completeness is still unverified, supported saved-master targets remain visible with their sample dates and an explicit coverage warning. These historical comparisons do not become qualified current picks. Known-out players, missing current player metrics, invalid events and unsupported samples remain excluded.

The [uploaded research validation report](data/reports/2026-11-08/uploaded_research_validation.json) records the timing-only regression, research exclusions and local projection benchmark. Controlled replay observations are synthetic and do not assert current NBA data or betting performance.

Natural follow-ups such as “why the fourth from my screenshots?” preserve the uploaded research side and recompute the same 20-game rule, including recent-form cautions. Changing the player, line, side, statistical phase or game date does not reuse the old selection as proof. `/freshness` distinguishes verified completed-game inputs from an unverified live clock.

Every uploaded-board reply now contains **From your uploaded lines** and **My own calculated targets from your master**. The second section calculates independent targets for that exact matchup without copying uploaded thresholds or quoting a bookmaker. Both groups remain available for follow-ups; ambiguous numbering asks which group, and calculated targets never become confirmed offers.

Screenshot analysis uses the saved matchup and each player's same-team master records. It requests at least four distinct players and both teams when supported; larger saved requests remain supported. With verified completed-game inputs, a failed or expired live timing check no longer hides all research choices or independent targets. Uploaded choices display explicit More/Less, historical support, recent form and source dates. Narrow support and recent contradictions are labeled, and timing remains unverified. Missing statistical coverage, unreadable sides, known-out players and started games remain excluded; unsupported slots are never padded. Strict shortlist eligibility remains separate from these research choices.

An upload receives a durable reading acknowledgment; `/status` reports its stage and elapsed time. Projection reads reuse request-local source data without carrying a cache across requests. The 90-minute reminder can include labeled research from verified completed-game statistics while live timing remains unverified.

Failed/partial schedule checks retry after one minute, while source HTTP cooldowns remain enforced. A missing unrelated game no longer blocks a fresh independently matched Knicks game. Fallbacks retain their real source timestamps; GitHub scheduling does not guarantee relay freshness.

Compact prop replies keep the calculated decision, exact quantities and citations in the application. The model writes only the explanation. Historical averages and hit counts remain context; they cannot replace the actual Hold reason or establish consistent performance. Future-game timing and current data verification remain separate from any prediction.

The current research benchmark is the **arithmetic mean of the latest 20 usable,
completed regular-season games for the player's current team**, strictly before
the game being assessed. Earlier seasons on that team can contribute; their dates
and seasons remain visible. A ranked pick requires all 20 observations, positive
played minutes and the requested statistic. DNPs, incomplete records, conflicting
duplicates, other teams and other statistical phases are excluded. Shorter samples
remain available for discussion without becoming ranked picks.

The mean and median must support the same side of an exact offered line, with the
disclosed research buffer. Small differences, opposing evidence, unavailable
current data or inadequate samples produce **Pass**. Recent playing time,
opponent history, rest and tentative defenders provide context; they do not add
unsupported numerical bonuses to the mean. `/strategy` explains this policy in
the bot. Separately labeled uploaded research leans can use the observed 20-game side counts when the strict buffer is not met; ties use the last five observed games, then the mean and median. Recent contradictions are disclosed. The bot can return fewer than four supported choices and still enforces the required team/player constraints.

The earlier simulated 20-game rule scored **53.6%**, compared with **53.7% for
Always Less**. Those tests used invented reference lines and allowed shorter
samples. They did not establish a profitable betting edge. The deployed variant
requires 20 comparable games and additional current-data checks; those stricter
filters have not established an edge either. Actual historical PrizePicks offers,
available sides and payouts were absent, so the simulation is not a sportsbook
ROI backtest. New pregame archives retain forecasts, supplied offers and passes
for later evaluation; unavailable payouts remain unknown.

The release supplies available careers for current roster identities and every
named matchup participant, including veterans absent from the current-season
feed. Reviewed aliases share one provider identity. The earliest
included career begins with LeBron James in **2003–04**. Historical teams,
regular seasons, playoffs and play-in games stay separately labeled. ESPN player
IDs prevent a current player from inheriting a retired namesake's records.
The latest repair pass recovered **89 of 92 previously unverified source games**.
Across the recorded source checks, 104 of 107 rejected events now have independently
validated repairs. Three old participation records remain uncertain: Mike Conley
on November 24, 2007 and January 26, 2008, and Al Horford on January 16, 2009.
They remain unknown rather than being silently classified as DNP or Played.
Published inventory has not been reconciled independently against every career game.

After the deployment loads this release's `SOURCE_REF`, a background worker
downloads its pinned, checksummed career-history bundle, backs up the master and
adds missing observations. Existing records, entries, settlements, chat history
and outbox state remain in the persistent database. The install is idempotent and
retries failures hourly; `/status` shows its result. Daily collection continues
afterward. Existing chat, screenshot intake, commands, browser/API routes,
reminders, exports, backups and grading remain available.

## Verified gap completion and reminder reliability

The current November 5 master export has **171,889 player observations, 21,198 games
and 96 worksheets**. The calendar update adds three worksheets to the previous
93-sheet export without changing its saved player-observation count. Its 153,677
saved final Played observations contain all 16 basic counting/minutes fields.
This describes the saved population, not a proof of a
complete career inventory. The upgrade adds 679 observations and completes 653
plus/minus values, 2,954 historical starter flags and 7,338 game-score fields from
verified public source evidence. Known non-null facts, entries, grading, chat and
current-game readiness checks are preserved; each completion has an immutable audit.

Remaining player blanks are **226 historical starter flags and three LeBron James
plus/minus values**. Two final-score fields for NYK–ORL on December 13, 2025 remain
held because source venue headers conflict. Exact names, games, fields and reasons
are in [the gap report](data/reports/2026-11-05/README.md), and in Excel's
`Missing_Stats_By_Game`, `Game_Stat_Gaps` and `Tracking_Field_Coverage` sheets.
No saved 2026–27 Played record has a missing basic counting/minutes field.

The upgrade adds **9,388 actual measured pair-game observations**, dated October
19, 2017 through November 2, 2026, covering 920 of 928 configured pair definitions.
Unobserved pairs do not become zeroes. Conflicting overlapping legacy measurements
remain held, and pre-2017 guarding coverage is not invented. Hustle tracking now
includes 16 published fields. Overall and under-six-foot closest-defender FG made,
FG attempted and FG% allowed remain unavailable; eight aggregate tracking categories
still lack verified covered-game dates.

Structured official injury reports retain exact report clocks when known; date-only
and Not Yet Submitted records remain uncertain. Fresh game-specific official
Out/Questionable/Doubtful designations hold recommendations. An absent injury row
never means healthy, and no starters, expected minutes or restrictions are inferred
from missing reports. The collector also tries the original official PDF publication;
a refusal or parsing failure preserves prior evidence and its original timestamps.

The nightly importer still runs at **11:30 AM Eastern**, with hourly retries,
restart catch-up and explicit pending dates. It now lowers covered-through when a
publisher retracts complete coverage. Daily and current-event publishers use
separate concurrency groups and disjoint file scopes, so timing checks cannot
cancel a pending daily refresh. Git API publication preserves unrelated commits,
checks for conflicting source changes and honors the same token/branch rules.
This remains a daily refresh of available published NBA statistics across all
30 teams. A game ending does not trigger an immediate completed-game import;
the result must first be published and pass the independent coverage checks.

The saved **2026–27 season calendar is currently Knicks-only**. It retains **80 published
assigned games and two unassigned games toward an 82-game season**. Unknown Cup
opponents and missing tipoff times remain unresolved. The three added Excel sheets
are `Knicks_Season_Schedule`, `Schedule_Coverage` and `Schedule_TBD`: they show saved
fixtures, actual coverage and unresolved slots. Calendar rows do not create player
results or establish current game timing. NBA-wide game-stat collection continues
independently of this narrower future-calendar scope.

If direct historical inventory endpoints refuse requests, a completed nonempty
regular-season date can be corroborated by an exact match across the published NBA
calendar, two-sided NBA final team results and independently validated ESPN full
boxes. All source hashes/publication times remain visible. This fallback does not
verify live timing, empty off days or unannounced Cup/TBD events. A missing ESPN ID
can be completed only from an existing unique registered NBA/ESPN identity and
the same validated current-season source; existing provider IDs cannot be replaced.

A trusted saved game calendar can now produce a single **timing-unverified reminder
90 minutes before its scheduled tipoff** when live verification fails. The message
explains the hold; it contains no unverified picks. Verified current timing retains
the normal research reminder. Restarts, late catch-up, cancellations, tipoff expiry
and Eastern daylight-saving changes keep explicit behavior and deduplication.

Natural follow-up questions retain player, market, offered threshold and pending
clarification. Why/blowout/confidence/motivation questions receive a focused checked
explanation or a concise computed fallback. Lineup preferences for both teams,
market and pick count persist privately per Telegram chat beyond recent history;
`/clear` removes that memory. Old player/line context never becomes a permanent
preference or a verified offer.

## Manual refresh and daily Telegram receipts

Send **`/refresh`** (or **`/update`**) to start an on-demand dataset check. The bot
acknowledges immediately and posts the result in the same private chat when the
background job finishes. **`/refresh status`** (or **`/update status`**) shows the
current stage, elapsed time and last result without starting another job. One job
runs at a time. A Telegram polling restart preserves an active refresh; application
shutdown records interruption without silently replaying completed imports. User-requested checks bypass the
normal service polling timers while respecting source/host refusal cooldowns.

Results distinguish changed values, unchanged verified saved sources, and partial
or failed verification. Count-stable stat corrections count as updates; check
and publication timestamps alone do not. An unchanged dataset reuses a known-good
Excel. Changed data, a missing file or a recorded export failure triggers one final
rebuild after imports. Failed history exports also retry automatically.

The normal **11:30 AM Eastern** refresh queues a Telegram completion notice with
the covered game date, tracking coverage, Excel status and remaining blockers.
Receipts are deduplicated by Eastern calendar day; late startup catches up and
failed queueing retries. The notification is marked queued only after the durable
outbox confirms it. Hourly source retries continue without repeating the daily
receipt. A completed check does not imply every external source is available.

## Activate on the existing Akash deployment

Update the existing deployment's `SOURCE_REF` to the full tested application SHA
listed above, keeping the persistent volume and private environment settings.
This single redeploy includes both bot changes and the automatic dataset upgrade;
**no manual transfer of the 78 MB Excel is required**. The background worker checks
trusted public source manifests, makes backups, merges missing records and completes
compatible NULL values, then regenerates the Excel export. Startup and the first
large export can take several minutes.

Use `/status` for source-specific upgrade phases and `/freshness` for current-game
readiness. Readiness can still be held by an external schedule refusal, unverified
game inventory or unavailable current data; an Excel rebuild does not remove those
holds. The bot's web export provides the rebuilt master after the upgrade.

## What refreshes

Opponent career coverage includes every named participant in the matchup records,
including veterans absent from the current-season publication. The collector
publishes a validated career-history relay during daily refreshes; the bot checks
that relay hourly and adds missing records. All 269 named matchup participants
(eight selected players and 261 opponents) have available published history in
the checked November 5 build. `Matchup_Career_Coverage` in Excel lists actual
saved game counts, seasons and dates for each name; this is not a claim that every
career game or measured defender possession has been recovered.

Explicit “career” and “lifetime” questions now read available observations across
seasons and teams. An explicit opponent filter, including NYK, is retained.
For example, “Josh Giddey lifetime assists against the Knicks” requests his saved
career games against NYK. Current-season questions and the same-team 20-game
selection benchmark keep their own populations. Documented player aliases are
resolved before counting games, so name variants cannot create two lineup slots.

Historical source checks can complete compatible unknown game metadata while
preserving player statistics and recording the source evidence. They do not
satisfy current-game freshness checks. An unavailable career source retains
existing history and reports missing participants for retry; no unknown player
identity or absent game is filled by a guess.

Game observations cover the current published NBA player population across
**30 teams**, plus previously authorized workbook entities. A validated
current-season publication can add a newly observed player with an unambiguous
provider identity. Conflicting identities are held for review, and a failed scope
refresh retains the previous scope. The existing **928 named measured-pair
definitions** remain a separate population; discovering a new player does not
create a measured defender pairing or invent an unavailable statistic.

- A tracked player's broad season statistics update from games against **any**
  opponent. For example, a Maxey appearance against Cleveland can update his
  overall PPG, minutes and shooting totals.
- A whole-game opponent split changes only when the player faces that opponent.
- Named defender measurements require that actual measured player pairing. An
  unrelated game cannot change Brunson–Maxey defensive FG%.
- Historical seasons, current seasons, whole-game results and tracking samples stay
  separate. Missing values remain missing; DNPs do not become zero-stat appearances.
- Daily source imports validate scope, identities, dates and numeric values before
  publication. Verified publisher corrections update the corresponding record
  without counting the game twice. Additive history/workbook imports preserve the
  existing record. Season aggregates are not averaged together or added twice.

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
published data across the NBA, including existing and newly validated
current-season players and teams. It catches up after a restart and retries
pending publication **hourly**. Daylight saving time follows the New York timezone.

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
Source publication timestamps and covered game dates are retained independently.
The seven collected tracking categories do not declare their covered game dates;
that coverage stays **unknown**. Legacy dates inferred from the game feed are
removed without deleting statistical values. An observation file's newer date
cannot advance a tracking file's coverage.

The repository's collection workflow converts the published extracts and refreshes
the validated current-season identity scope. The Akash bot imports the validated
files and regenerates its master export when data changes. Source publication time,
last retrieval and latest covered game date are recorded separately. Neither a newly
started job nor a newly downloaded older file is labeled as today's completed data.
GitHub scheduling and upstream publication can be delayed; this is a morning update
with catch-up, not a guaranteed live feed.

Completed-day publications are also rechecked hourly for corrections. The relay
checks the bounded recent-day window against an independent ESPN NBA inventory;
an empty publication is created only when its league calendar establishes an off
day. Original statistic publication dates remain separate from verification and
download times. Evening relay checks continue through 10/11 PM Eastern across
daylight saving time.

Use `/freshness` for a concise game-day readiness report. Current picks require a
successful current-day publication check, coverage of the latest completed team
game, and a complete played/DNP observation for the selected player. A played
observation must include the requested statistic. Intervening game dates require
verified inventories or complete published team-calendar coverage; unknown gaps
hold the affected pick. Rest days do not become zero-stat games. Future-date
targets remain historical research until that game's daily checks are current.
Tracking fields, injuries and tentative defenders keep their own coverage limits;
a fresh Excel export or game-stat check does not establish that every field is current.

Current selections also require an independent event-status check within fifteen
minutes, matching the saved game and confirming a future tipoff. Bounded ESPN and
official NBA schedule checks run independently of the morning statistics import
and before pick research. A separate GitHub schedule relay is scheduled every
**10 minutes**; only a genuinely recent, matching source check can satisfy the
same freshness requirement. A relay download does not reset its source-check
time. GitHub jobs can be delayed, and a stale relay leaves the pick held.
A scoreboard final without imported statistics cannot grade an entry. Missing, failed or conflicting
event checks hold current recommendations and prevent an unverified reminder.

Available extracts support player production, shooting, team totals/opponent
statistics, passing, rebounding, touches/possession and rim-related measurements.
Four advanced categories remain unavailable in the current publication:
**overall player defended-FG, named measured pairs, less-than-six-foot defense and
hustle**. Those records retain their original dates and show unavailable/stale
coverage. Rim defense, scoring totals and whole-game coappearance
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

The saved calendar supplies the planned game and reminder time. Current picks
and reminder queuing still require a successful recent independent timing check;
a failed check leaves the saved calendar visible with its uncertainty. Known
uncertainty windows are preserved; incomplete coverage cannot prove an off day or
a player's rest/workload. Calendar records do not establish final scores or
settle an entry.

The default policy sends **one combined injury/availability/roster digest and
PrizePicks upload reminder, 90 minutes before a Knicks tipoff**. A 7:30 PM Eastern
game produces a 6:00 PM reminder. A recent same-matchup screenshot board is re-analyzed
for a target of four distinct players, alongside independent projections. Without a
current board, the bot asks for full current PrizePicks screenshots. Existing deployments
must set `KNICKS_DIGEST_MINUTES_BEFORE=90`; an explicit old value of 60 still overrides
the new default. Late startup recomputes fresh research before tipoff when current
checks pass, while a previously queued daily notice remains deduplicated.

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

The model interprets the question and reads scoped evidence before writing a
short explanation. For a stated prop, the application calculates the observed
distribution, mean, median, workload and source limits. Routine prop and next-game
answers display their exact decision, line, dates and numerical facts through
application rendering; the model supplies a short interpretation of those facts.
The model cannot change a calculated More, Less or Pass decision. Deeper generated
numeric claims retain their source checks, which reject unsupported quantities,
conflicting metrics, opponent-scope changes and invented availability or defender assignments. A team
total does not establish an individual defender's ability. These checks are bounded,
not a guarantee that every generated interpretation is correct. Deeper model-written
query filters/calculations remain explicitly marked for review.

Private conversation retains up to twelve question/answer pairs for twelve hours,
including recent pick reports and uploaded-board replies. Follow-ups keep the
player, market and line: “Josh Hart 22.5” prompts for the missing statistic;
“I mean PRA,” “up or down?” and “why?” continue the same discussion. Changing the
market clears the old threshold. A genuinely ambiguous player, statistic or line
gets a short clarification. The bot also distinguishes preferences and lineup
instructions from requests for team statistics. `/clear` removes this context.
Ordinal questions such as “why the second one?” select that player and market.
Corrections such as “18.5, not 19.5” update the discussion, and switching game dates
clears the earlier offer. A saved preview supplies the related prop’s game date after
the application rechecks its calendar row. Explicit user dates override that context;
players outside the discussed matchup require clarification. Publication dates never
become the game date. Explanations keep held and unavailable legs visible,
including mixed lists of hypothetical targets and user-provided lines. Larger
lineups receive a complete computed summary when they exceed the AI's bounded
evidence budget.

Both-team coverage is enforced when building complete multi-player lineups; missing
qualified opponents produce a clearly labeled partial draft. Recent explicit team
constraints and market preferences carry into subsequent Telegram research.
Hypothetical `/picks` targets remain distinct from uploaded sportsbook lines.
Conversational answers do not create entries or modify the financial ledger.

Ordinary conversation uses the local AI to interpret meaning and the relevant
discussion, including unfamiliar phrasing and indirect follow-ups. Rephrased
questions use the conversation's subject without requiring a special command.
Factual basketball
questions read scoped saved observations, schedule records or collected news
before answering; a remembered model fact does not establish a current roster,
injury, statistic or matchup. Prop reasoning, risks and game previews receive a
short model-written explanation alongside the application-rendered facts.

When the model is starting or busy, or its answer times out or fails validation,
the bot returns a checked computed fallback or asks for an essential missing
detail. This fallback also preserves unavailable legs and freshness holds.
Questions needing additional data retain the retrieval tools and use the same
final source contract. Invalid
explanations get one bounded repair attempt, then return a concise computed
assessment or labeled saved facts if still invalid. `/ai_status` retains the actual
failure and timing information. Rejected replies are never reused from the cache.
Compact prompts carry each selected fact once, with full source metadata
retained for rendering. Compact research requests have a smaller output budget and a
180-second total request limit including any repair, with no automatic retry
after a timeout. Telegram displays a typing indicator while generating a reply.
Follow-up messages in the same chat wait for the preceding answer so they receive
completed context; health commands remain available while it works.
Clearing chat history during a request cannot restore the deleted conversation
when that request completes.

Ask about upcoming basketball in your own words, then continue discussing the
game or a proposed player statistic. The AI uses the conversation to resolve the
subject; it asks a short clarification when several picks or meanings remain
possible. A next-game question reads the saved Knicks calendar and dated evidence,
then gives the Eastern date/time, relevant team results and a short outlook.
Explicit subject changes override an older prop discussion. Saved timing,
incomplete calendars and unverified game-specific availability remain qualified;
a preview does not certify live timing or authorize a pick.

`/ai_status` includes active-job elapsed time and timings for the latest compact
reply phase, including model loading, prompt evaluation and generation when
provided. Those phase counters do not include earlier retrieval calls; `/usage`
counts model work across the paths. Deeper lookups use a small relevant catalog,
the same model context, and at most two tool rounds inside a bounded deadline.
Explicit row/cell requests read the source instead of guessing from season totals.
Provider response time still needs a live Akash check.

Five actual local component tests took **9.5–21.8 seconds**. Those measurements
describe the tested local model and data setup, not a live Akash response-time
guarantee or proof that every phrasing will be interpreted correctly.

Ordinary questions can use player names, unique surnames, supported nicknames,
team cities/names/codes and common name variations. Recent private conversation
context supports follow-ups such as “what about Boston?” Direct averages, supported
measured percentages and dated assignment lookups can answer without AI inference.
A tentative workbook pairing includes its date and source; it is not a confirmed
lineup or proof of who will guard whom.

Uploaded lines use the same 20-game mean and median rule described above. The
report shows the reference mean, observed variation, sample scope, supporting
context and counterarguments. Workload scenarios are illustrations and do not
replace the reference mean. Missing inputs and current coverage holds are disclosed.

Better-supported and higher-variance alternatives use the actual uploaded lines.
An aggressive candidate needs supporting evidence; the engine can abstain rather
than manufacture a risky pick from a simple average. These are experimental
research scenarios, **not calibrated win probabilities or guaranteed advantages**.
Leg frequencies are not multiplied into a claimed parlay win probability.

## Independent projections before PrizePicks

Ask for picks or use `/picks` to generate research from the existing master without
uploading any lines. The default is up to four distinct players for the next
unstarted saved Knicks game. Send just `Nov 2`, `November 2`, or `11/2` to request
that day, or use `/picks Nov 2` / `picks for November 2`. Numeric dates are
month/day. Omitted years use the current July–June NBA season, so `Jan 5` in
November 2026 means January 2027. A past date remains past; it is never silently
moved into next year. `/picks today`, `/picks tomorrow`, and full YYYY-MM-DD dates
still work. Add an optional count from 2–6; otherwise the default is four.
An already-started date is held instead of being silently treated as pregame.
The normal game-day digest includes a compact version of this independent research.

Each item gives the bot's own statistical projection, workload/context evidence,
sample date, and conditional thresholds: **More only at or below** one target, or
**Less only at or above** another. These numbers are research targets, not claimed
PrizePicks offers. The target buffer uses at least 0.5, 15% of the projection, or
25% of the observed p10–p90 scenario spread, rounded outward to half points. This
is an explicit heuristic, not a calibrated likelihood or a proven betting edge.

The research uses the latest 20 valid same-team regular-season observations for
each market, with freshness and collected availability checks. Opponent/rest
cohorts and workload remain labeled context. It chooses supported markets and
distinct players and can return fewer than the requested count. Missing
opposing-team data is disclosed; a list supported by only one team is not a complete
ordinary multi-team entry. No board, wager or master-stat record is created by independent research.

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

## Import an updated master workbook

Career history installs automatically with the software release. To add an
additional enriched master `.xlsx`, use **Import master Excel** on the bot website
and wait for its import report. The browser accepts files up to **128 MB**. For a
file up to **20 MB**, attach it to Telegram with `/importmaster` as the caption.
Sending `/importmaster` alone displays these instructions.

The importer validates the workbook, makes a consistent database backup, and
merges accepted additions in one transaction. Existing game/player observations
win conflicts. Entries, financial settlements, chat context, the outbox and live
freshness checks are preserved. Re-importing the same workbook does not duplicate
its observations. Original source sheets remain available as dated reference
material alongside the regenerated export.

An uploaded workbook establishes its saved observations and provenance; it does
not verify today's schedule, current injuries, a tentative defender assignment or
the latest completed game. `/freshness` continues to apply the independent checks.
Daily source collection remains enabled after the merge and can apply verified
publisher corrections through its normal refresh path.

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
2. Set `SOURCE_REF` to the full tested application commit supplied with the release,
   also pinned in [deploy.yaml](deploy.yaml) after publication. Enter only the
   40-character commit value in the value field.
3. Set `OLLAMA_MODEL` to `qwen3:4b-instruct-2507-q4_K_M`. This is the explicit
   non-thinking instruction release; the older `qwen3:4b` tag is a different model.
4. Retain the existing Telegram token, allowed user ID, chat password and `/data`
   storage settings, then apply the deployment update.
5. Open `/status` to check career-history installation and worker health, then
   `/freshness` for the actual game-data and schedule checks. `/strategy` shows
   the current research rule; `/help` lists the commands.

The bootstrap installs the pinned application modules and dependencies, reuses the
approved workbook, imports the season calendar and applies additive database
changes. The separate history worker checks the pinned bundle once per manifest,
backs up the database before merging, and retains existing records. Restarting
does not repeat a completed merge. Startup checks run automatically and are saved
in `/status`; a source warning is not reported as a successful refresh. The model
can take several minutes to download while the other services continue working. Data updates happen
automatically; future software releases use a new source reference.

Useful Telegram commands:

| Command | Purpose |
| --- | --- |
| `/status` | Dataset scope, refresh results, covered dates, source errors and Telegram health |
| `/freshness` | Current publication coverage, schedule verification and reasons a pick is held |
| `/strategy` | Current 20-game benchmark, minimum sample, pass rules and testing limits |
| `/reminder` | Next Knicks game/send time, saved-calendar use or timing uncertainty |
| `/usage` | AI work counters, cache reuse and the hosting-billing distinction |
| `/ai_status` | Model readiness, active request and last sanitized failure |
| `/selftest` | Start background fixture/read-only checks and receive the finished report |
| `/selftest status` | Check stage, elapsed time and the latest saved report |
| `/summary [injuries\|news\|trades]` | Saved sourced reports without waiting for AI |
| `/playerlog NAME \| points \| 5` | Stored game observations and averages |
| `/boards`, `/analyze BOARD_ID` | Saved available lines and their research report |
| `/picks [DATE] [COUNT]` | Own projections; DATE can be Nov 2 or 11/2, and a date-only message also works |
| `/newboard` | Start a fresh screenshot set; leave saved boards/entries intact |
| `/entries`, `/results` | Recorded entries, provisional outcomes and confirmed account results |
| `/settle ENTRY_ID TOTAL_RETURN [note]` | Reconcile the actual credited return |
| `/importstats` | Caption an explicit CSV/JSON statistics upload |
| `/importmaster` | Instructions, or caption an enriched `.xlsx` for an additive merge with backup |
| `/clear` | Clear recent chat context without deleting the dataset or entries |
| `/help` | Upload formats and the remaining commands |

`/selftest` runs as a background owner job: it confirms the start, then sends the
finished report through the durable Telegram outbox. `/selftest status` shows its
stage, elapsed time or last report without starting another check. A busy server
can take longer than one minute; the reply deadline no longer discards a completed
report. Repeating the command while it is active does not launch another worker.
See the [background self-test validation](data/reports/2026-11-05/selftest_background_validation.json).

Self-test fixture exports run separately from the full-master Excel queue. Normal
full-master exports remain serialized. Live integrity checks have an eight-second SQLite execution
budget each, and the read-only counts/feed snapshot has a three-second budget.
Wall time also includes storage waits and scheduling under server load;
unavailable or unfinished reads produce warnings, while confirmed corruption
produces failure. Replies and logs include safe error categories and check times.
A requester timing out does not release the worker guard before its native thread
finishes. See the [self-test timeout validation](data/reports/2026-11-05/selftest_timeout_validation.json).

Self-tests do not place entries, call a sportsbook, inject sample records into the
live master, or establish the quality/speed of a generated AI answer. Checks with
mocked transports are distinct from verified publisher access and actual Akash
message delivery.

## November 4 conversation validation

The startup follow-up passed **860 automated tests** on its
[tested application commit](https://github.com/allenmirgorodskiyppbot/NBAPPBOT/actions/runs/41932919415).
Missing per-date checks now run once immediately after the daily deadline when
upgrading from an older worker; actual failures retain the hourly cooldown.
`/freshness` also shows the safe current schedule error category. These checks do
not establish successful schedule access from the actual deployment.

The subsequent pregame/freshness update passed **856 automated tests** on its
[final application commit](https://github.com/allenmirgorodskiyppbot/NBAPPBOT/actions/runs/41930748027),
including a clean downloaded-package import and checks for all local runtime
dependencies. Real local-model checks covered threshold corrections, ordinal
follow-ups and mixed priced/unpriced explanations; accepted replies took roughly
22–64 seconds. The final delivery replay also exercised the post-generation
readiness check, which can hold a pick if tipoff or source readiness changes while
the model answers. See [pregame validation](docs/validation-pregame-2026-11-04.json).

The data workflow updated source publications, but its independent ESPN inventory
request was unavailable on the GitHub runner. The verified November 3 off-day
document was fetched separately from ESPN and published with its real check time.
Provider access remains environment-dependent; `/freshness` reports the actual
deployment's readiness instead of interpreting a successful workflow or AI startup
as proof that every source is current.

The conversational update passed **703 automated tests** in
[GitHub Actions](https://github.com/allenmirgorodskiyppbot/NBAPPBOT/actions/runs/41911945699).
Actual local-model tests covered the Hart market clarification and follow-ups,
Randle three-point explanations, both-team instructions, ordinary conversation,
and a sourced Brunson matchup question. Corrected outputs from earlier iterations
were also replayed through the final checks, alongside the inaccurate drafts that
must be rejected. The final two Telegram-answer-path smoke tests used the finished
code and actual model responses.

See the [validation record](docs/validation-conversation-2026-11-04.json) for the
tested cases, timing, revision and limits. Testing used temporary database copies
and the local Qwen 4B model on four CPUs and 8 GiB RAM. It did not send Telegram
messages or contact the user's Akash deployment. Generated research replies took
roughly one to two minutes in the slower tested cases, including a repair when
needed; simple chat and missing-market clarification were much quicker. These
checks establish the tested conversational behavior, not universal accuracy or
betting performance. Deployment still requires updating `SOURCE_REF` and checking
the actual provider.

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


