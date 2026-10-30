# NBAPPBOT — always-on Knicks research and PrizePicks tracking

This version runs NBA collectors, a durable master dataset, manual PrizePicks line
analysis, entry grading, private Telegram chat and a local Qwen2.5 3B model on Akash.
News/statistics services do not depend on the AI model finishing an answer. No OpenAI key is used.

## Consolidated conversational research release

One `SOURCE_REF` update installs the entire current application: the earlier model,
news, statistics, entry-ledger, conversation-memory and game-day-digest changes are
included. The bootstrap fetches every required module, preserves `/data`, reuses the
approved workbook seed and applies additive database migrations. Existing private
Telegram credentials, chat password and allowlist remain deployment settings.

Shared basketball entity resolution now recognizes stored player names, unique first
names/surnames, selected common nicknames (OG, KAT and Deuce), team cities/names/codes,
and strongly matched surname typos. For example, “who is OG and Abby’s matchup against
the Hornets?” resolves OG Anunoby/Charlotte. Ambiguous names are not silently guessed.
Short follow-ups can retain the player and change the opponent. This layer is shared
by matchup, statistical, news and general research paths rather than an OG-only command.

Ordinary assignment questions retrieve the dated `Matchup_Assignments` record directly,
with the primary/alternate pairing, original Excel row and projection date. The workbook
does not specify assignment direction, so the answer does not claim who actually guards
whom. A dated pairing is not a verified current lineup. “Why”/explanation questions use
the general local model with bounded preloaded assignment, player-profile and relevant
news evidence, retaining read-only tools for other research. Player-specific news questions
filter unrelated reports before summarization. Common direct lookups work while the model
is loading; broader AI answers still depend on CPU inference and source coverage.

After startup the application automatically runs temporary-fixture business checks and
read-only live database, workbook, backup, RSS, schedule and connection checks. It records
the results under `release_checks` in `/status` and logs `RELEASE CHECKS`; no manual
`/selftest` invocation or extra Telegram notification is required. A blocked schedule is a
visible warning, not a pass. Model readiness is checked, but generated-answer speed/quality
and actual PrizePicks lines are not claimed tested by these checks.

This release does not restore blocked automatic game statistics, establish complete
official injury coverage, connect a PrizePicks account, or provide off-provider backups.
The selected free/manual workflow remains: upload game statistics and PrizePicks lines,
record entries actually placed, and reconcile the final credited payout.

## Knicks game-day injury digest

Automatic news delivery now defaults to `NEWS_ALERT_MODE=knicks_gameday`. The bot
continues collecting the configured feeds silently every few minutes, including on
off days. It queues **one Telegram injury/availability/roster digest per Eastern
Knicks game day, 60 minutes before the scheduled tipoff**, covering the Knicks and
their opponent. A 7pm Eastern tipoff means a 6pm digest; other start times and DST
are handled from the schedule. `KNICKS_DIGEST_MINUTES_BEFORE` changes that lead time
(15–180 minutes). `/summary` remains available on request at any time.

The independent scheduler checks the official current-season NBA JSON schedule
every 15 minutes even when `NBA_DATA_SOURCE=manual`. It stores calendar data outside
the game/settlement ledger. The default `KNICKS_SCHEDULE_URL` is
`https://cdn.nba.com/static/json/staticData/scheduleLeagueV2.json`; a separately
configured source must return that same complete current-season JSON schema.
This is **not yet verified reachable from the user's Akash provider**: the earlier
NBA scoreboard check timed out, and this environment's schedule request returned403.
If schedule access fails or becomes stale, the bot records `knicks_schedule` in
`/status` and pauses automatic timing. It does not infer an off day or invent a
tipoff from historical workbook rows or news. `/diagnose` includes the actual
schedule endpoint; `/reminder` displays the next game/send time or the blocker.

At send time the bot refreshes its configured news feeds and renders source titles,
publication dates/timezone uncertainty, and links without waiting for the AI model.
Only recent matching Knicks/opponent reports are selected; explicit promotional
offers are excluded. Name matching also uses each player's latest stored team, which
may be old in manual mode. This remains partial publisher coverage, not a current
official roster or complete NBA injury report. Missing reports never establish health.
Source failures appear in the digest and `/status`, rather than separate push alerts.

Scheduled messages use a persistent per-day outbox key, so restarts do not enqueue
another digest. A restart after the target time catches up only before tipoff.
Pending digests expire at tipoff instead of arriving after a delivery outage.
Unknown-time, canceled, postponed, live, or final games do not trigger a digest;
schedule changes discovered before queuing update its timing. Pending old individual
headline and feed-health alerts are canceled on upgrade. Entry-result notices and
replies to your questions continue independently. Telegram's send API itself has no
exactly-once receipt guarantee if a delivery succeeds but its acknowledgement is lost.

To activate this revision on the existing lease, update `SOURCE_REF` to the commit
pinned in [deploy.yaml](deploy.yaml), retain private credentials and the `/data`
volume, and apply the deployment update. The new defaults need no extra variable
entries. After startup, send `/reminder` and verify a real next game/send time;
`schedule_unavailable` means automatic timing is still blocked. `NEWS_ALERT_SCOPE`
is only used with the optional `NEWS_ALERT_MODE=instant`; `off` silences automatic
news delivery altogether. These modes do not stop news collection.

## CPU chat and fast news summaries

The default `OLLAMA_MODEL=qwen2.5:3b` is a small instruction model with tool support,
using the existing four-CPU/eight-GiB model service. The previous Qwen3 configuration
was observed generating reasoning until its output limit even on a tiny `/no_think`
test. A successful HTTP response alone does not establish a complete AI answer.
The [model registry](https://ollama.com/library/qwen2.5:3b) documents the model and its license.

- `/summary` returns a fast, deterministic digest of collected injury, availability and
  roster reports without model inference. `/summary trades` narrows it to transactions;
  `/summary news` includes all material report categories. Publisher wording, dates and
  source links remain explicit; unknown timezones are not guessed. These are collected
  reports, not a complete official participation list.
- Natural-language news summaries use one short, structured AI request to select
  relevant saved reports. The app renders their original wording, dates, source links,
  uncertainty and stale-report warnings, and enforces the requested bullet limit
  (default three, maximum five). Model-written medical claims are not accepted.
  This avoids dropped dates, invented statuses and five-bullet replies to a three-bullet
  request. Reports with unknown timezones are ordered within each publisher; their
  collection order does not establish when an injury update was published.
  If inference times out, returns invalid references/JSON, or exhausts its output limit,
  the reply reports the failure and shows a cited deterministic digest. Ollama's
  [structured-output API](https://docs.ollama.com/capabilities/structured-outputs) supplies
  the schema; the app also validates the result before displaying it.
- `/ai_status` reports the configured model, startup phase, whether a request is active,
  and the last sanitized failure category/time. It makes no model request, and is separate
  from `/status` data-feed health. Failure categories are logged without question text,
  response bodies, credentials or private URLs. The diagnostic state resets on restart.
- Explicit promo-code/affiliate-offer articles stay in the raw collected news archive but
  are excluded from digests and automatic injury/trade alerts. This is a targeted noise
  filter, not a guarantee that every irrelevant article is identified.

## Test the bot without a live PrizePicks board

Normal sentences are the primary chat interface; numeric command arguments are optional
shortcuts. Telegram retains the last three natural-language turns per private chat and
uses context from the last twelve hours, including across restarts. Commands and uploads
are not added to that conversation history. `/clear` clears that chat's context without
changing its dataset or picks.
For example, “What is Brunson averaging?” needs no game-count argument: the bot uses
all stored completed games played in the current regular season and states that scope.
“What is his PPG?” can use the recent player topic. An explicit season is honored;
career, opponent and other unsupported filters go to the general query planner.

For measured matchups, ask “What FG% does Tyrese Maxey shoot when Jalen Brunson guards
him?” The bot reads the dated measured-pair section of `Pair_Tracking`, calculates
FGM/FGA, and returns the season, phase, game/date range and Excel/source citations.
It never adds overlapping historical aggregate phases or substitutes whole-game
coappearances for measured defending. A follow-up using “his” can use the previous
player topic. “Defensive percentage” alone prompts a clarification of the metric;
the user supplies names/intent, not shot counts. Missing, zero-attempt or conflicting
samples are reported rather than replaced with a guessed percentage. The current
quick matchup path handles overall FG% allowed; other metrics remain separate requests.

Player game logs have a direct read-only path that does not wait for model inference:

```text
/playerlog Jalen Brunson | points | 5
/playerlog Jalen Brunson | pra | 10 | 2026-27
```

Ordinary questions asking for a player's last 1–20 games and averages use the same
path when their filters are supported, including the five-game Brunson question.
It includes only final games marked Played, defaults to Regular Season, honors an
explicit season, and excludes future dates. Missing values remain missing and the
average uses only observed values. Output includes each game's date, sample size,
missing count, source, latest stored game date and collection mode. Player-name
ambiguity is not silently merged. Opponent/date-range/team/comparison filters stay
with the general query planner rather than being ignored. Calculations run directly
against the master, not through model-written arithmetic, even while AI is busy.
General questions now use a bounded workbook catalog instead of the full overview
and multi-sheet schemas, shorter history/output, and at most three tool rounds.
This reduces CPU work; complex AI research can still be slow or fail and must be
verified on the deployed provider. `/playerlog` is available independently.

After applying the latest `SOURCE_REF` in your existing deployment, send `/selftest`
in your private Telegram chat. It checks imports, invalid-input rejection, final-stat
corrections, provisional grades, confirmed win/loss accounting, duplicate handling,
Telegram authorization/outbox recovery, Excel export, database reopen and backup restore.
All sample games and financial entries are confined to temporary databases and removed
afterward. It also checks the live master and latest local backup read-only and reports
RSS freshness. Warnings for a feed or missing backup are not counted as successful checks.
It does not call the model, contact a sportsbook or inject test news into your live dataset.

Then send `/ai_status` and a normal request such as “Summarize the latest collected
injury news in three short bullets.” Each displayed report should retain its source,
publication time, link and any stale-report warning. Tomorrow's actual lines, an actual
PrizePicks settlement, and provider-level recovery after a real service restart remain
separate acceptance checks. Local tests mock model/network transports; they do not
establish new provider-side inference or feed connectivity by themselves.

For an existing deployment, update both `SOURCE_REF` from the current `deploy.yaml` and
`OLLAMA_MODEL=qwen2.5:3b` under **chatbot → Vars & secrets**, preserving private credentials
and the existing `/data` volume. Applying the update downloads the new model if needed;
watch the chatbot logs for `KNICKS CHAT READY`. Then test `/summary`, `/ai_status`, and
a normal news-summary question. The new model's end-to-end behavior must be verified
from the actual provider; offline tests use mocked model responses.

**Implemented does not mean deployed:** importing this repository alone does not start
Akash services. Telegram stays disabled until its token and numeric user ID are supplied
privately. The public template contains no credentials.

## Current free setup: automatic news, manually supplied statistics

The new SDL defaults to `NBA_DATA_SOURCE=manual`, `NEWS_SOURCE=rss`, and NBA-wide
material-news alerts. The blocked ESPN game collectors stop; your historical data,
entries, Telegram configuration and persistent volume remain. News collection continues
independently from these publisher-listed feeds:

- RotoWire NBA player news: `https://www.rotowire.com/rss/news.php?sport=NBA`
- SNY Knicks: `https://sny.tv/knicks-feed`
- Elite Sports NY Knicks: `https://elitesportsny.com/category/nyc-teams/e-new-york-knicks/feed`

These feeds are published for RSS readers; they need no paid API key. They were checked
against publisher directories, but their reachability **must still be verified from your
Akash deployment** using `/diagnose` and `/status`. The ESPN403, NBA timeout and CBS429
checks do not tell us whether these other publishers are accessible. A feed that refuses
access stays failed/stale and receives a cooldown; no proxy or access workaround is used.

The first successful fetch of each feed establishes a baseline visible in `/news`, so old
articles do not flood Telegram. New or changed injury/trade/lineup reports are saved
quietly for the game-day digest; immediate alerts require `NEWS_ALERT_MODE=instant`.
Publication time is retained; unknown times stay unknown. Rumor
language is labeled unconfirmed; other articles remain publisher reports, not independent
verification. This is a watchlist of configured public sources, not an exhaustive crawler
of the entire internet or a promise of instantaneous breaking news.

For game-statistics updates, attach a CSV/JSON file in Telegram with caption `/importstats`,
or use **Import game statistics** in the web research desk. Required columns are:

```text
date,player,team,opponent,game_status,appearance_state,minutes,pts,reb,ast,tpm,season_type
```

Use an exact `YYYY-MM-DD` date, recognized team abbreviations, `game_status` final/live,
and `appearance_state` Played/DNP. Played rows require positive actual minutes; final
Played rows require points. Other missing statistics stay missing, never zero. Provide
`season_type` explicitly when known (Regular Season/Playoffs/etc.). Unknown phase is not
guessed. Use actual box-score values, not a PrizePicks projection or prediction.

A template is at [`examples/player_stats_template.csv`](examples/player_stats_template.csv).
Save an Excel table as CSV if needed. Imports validate the document, keep an audit record,
and publish game/player changes atomically. First-time final uploads can contain a partial
roster; they do not establish complete league coverage. A live-to-final update must include
previously observed live players so omitted preliminary values cannot be graded as final.
Placed entries grade against the uploaded finals; credited payouts still use `/settle`.

A personal note can be added with `/newsnote HEADLINE` followed by the detail on the next
line. It is explicitly unverified and is not broadcast as a confirmed breaking report.

## What this version does

- Seeds the master database once from the embedded Excel history: 6,350 player-game
  observations. The larger companion ZIP has not been imported.
- Can collect available NBA schedules and box scores for **all teams** in explicitly selected
  automatic-source mode. The current free SDL uses manual statistics because tested endpoints failed.
- Supports NBA CDN or ESPN game collection and published RSS or ESPN news; the default
  sends one Knicks/opponent digest before a Knicks game, when schedule access works.
- Waits normally 5 minutes between completed cycles, or 1 minute during live games/pre-Knicks-tip
  window. Source freshness and collection time are different: polling cannot make an
  upstream source instantaneous. Periodic API quotas/availability still apply.
- Initially backfills 7 days, revisits 3 recent days for corrections and resumes missed
  dates from durable checkpoints after downtime.
- Keeps source snapshots, update timestamps and feed errors. `/status` shows details;
  game-day digests include coverage failures. Separate health pushes require instant mode.
- Accepts manually uploaded PrizePicks lines, CSV/JSON files and screenshot OCR drafts.
  Available boards and entries actually placed are separate records.
- Produces versioned historical-support rankings and up to three two-leg **research
  alternatives**, using exact uploaded lines and distinct players from at least two teams.
- Grades saved full-game statistics provisionally, then records actual credited returns
  when you reconcile the PrizePicks settlement. Keeps original selections and grade revisions.
- Stores the master, entries, Telegram offsets/outbox and backups on a persistent volume.
  Creates an Excel export and backup every six hours, retaining the latest seven daily
  backup snapshots. Authenticated downloads provide a current export and backup.

## What remains limited

The free NBA CDN, ESPN endpoints and publisher RSS feeds are best-effort public sources without a service guarantee.
NBA CDN box scores and publisher headlines **do not provide a complete official NBA injury report**; ESPN availability snapshots are also incomplete. Trades
may appear as news, but a complete structured transaction/lineup feed is not implemented.
No direct PrizePicks account/board connection exists; you supply available lines and slips.
Additional licensed or published feeds can supplement the baseline, as explained below.

The analysis is descriptive research, not a validated betting prediction model. It reports
sample size, missing statistics, dates and historical line-crossing frequencies, not
calibrated future probabilities or expected returns. Injury/opponent/minutes adjustments
have not been validated. Suggested research combinations remain `actionable=false` until
current lines, participation and platform rules are checked. Correlated leg frequencies
are never multiplied into a claimed parlay win probability. Small samples and expired,
started or unsupported partial-game markets cannot enter the ranking.

Grading uses final box scores, not the language model. DNPs, ties, partial-game markets,
postponements and flagged Reboots require review. NBA Reboot eligibility is **not**
automatically established from a final box score. Flex partial payouts, boosts and special
contest rules require your actual settlement. Confirmed financial results and ROI use only
reconciled payouts; a calculated result does not prove that PrizePicks credited your account.

Player matching currently normalizes exact names, accents and punctuation. Cross-provider
nicknames, name collisions and changed names may need explicit IDs or manual correction.
The historical workbook is an embedded subset; some players have incomplete histories.

## Deploy the updated SDL

1. Open [`deploy.yaml`](deploy.yaml), select **Raw**, and save it as `deploy.yaml`.
2. In your private copy or Akash editor, set a unique `CHAT_PASSWORD` of at least 16
   characters. Leave `SOURCE_REF` unchanged; it pins this tested application revision.
3. Import this SDL into a **new Akash deployment** so the persistent volume is allocated
   at creation. Choose a provider offering the requested `beta3` persistent storage.
4. Review the displayed provider price before confirming. Requests total **6 vCPUs,
   11 GiB RAM, 24 GiB ephemeral root storage and 20 GiB persistent data storage**.
   `pricing.amount` is a bid ceiling, not a monthly price estimate.
5. Inspect `chatbot` logs. The bootstrap downloads pinned code, verifies/caches the seed
   workbook and installs dependencies. It attempts to install Tesseract for screenshots;
   if unavailable, text/CSV/JSON uploads still work. Initial model download can take minutes.
6. The collectors and Telegram commands start before the model finishes downloading.
   `KNICKS CHAT READY` means natural-language AI chat is also available. Model startup
   failures retry every 30 seconds; data collection continues independently.
7. Open the chatbot's public **HTTPS** URI. Log in to inspect feed status, news, results,
   or download the master Excel/backup. If the provider only exposes HTTP, configure an
   HTTPS proxy/custom domain before submitting your password.
8. After the new deployment is verified, close an obsolete starter deployment if you no
   longer need it. Do not close the persistent deployment as a way to restart its app.

The `chatbot` service uses SQLite WAL at `/data/master.sqlite`, one application replica and
one web worker. This suits a single owner's research bot; do not increase replicas against
this database volume. A larger multi-user service should migrate to a database server.
Only the chat app is public; Ollama is internal. The model cache is ephemeral and may be
re-downloaded after replacement. Data remains on the persistent `/data` volume.

Persistent volumes and local backups remain with the selected provider/deployment.
They are not independent disaster recovery. Download backups to another location regularly;
no off-provider backup target is configured. Closing a lease or losing the provider may
lose its volumes. Restore a downloaded backup before starting the app by placing it at
`/data/master.sqlite` while the application is stopped; never overwrite a running WAL database.

## Create and connect your Telegram bot

1. In Telegram, open the official [@BotFather](https://t.me/BotFather), send `/newbot`,
   and choose the bot's display name and unique username. Save the token privately.
2. Find your **numeric Telegram user ID**. A helper is included; run locally:

   ```sh
   python chatbot/telegram_setup.py
   ```

   It prompts for the token with hidden input, tells you to send `/start` to your bot,
   and shows the private-message sender IDs. Select your own ID. The token is neither
   printed nor saved. Use the helper before the bot's long polling starts; if another
   instance/webhook already consumes updates, stop that instance first.
3. In the Akash deployment editor set these environment variables privately:

   ```text
   TELEGRAM_BOT_TOKEN=your token from BotFather
   TELEGRAM_ALLOWED_USER_ID=your numeric user ID
   ```

   Do not paste the token into this repository, a public issue or a chat message.
   The Akash provider runs the workload and can access its runtime environment.
4. Apply the deployment configuration and send `/start` to the bot. Send `/status` to
   verify connection and source timestamps. Only allowlisted private chats are accepted;
   groups and other users cannot access your data.

Telegram uses long polling, so it needs outbound HTTPS to `api.telegram.org` but no public
webhook. Multiple pollers with the same token are unsupported. Offsets, received-command
records and queued notifications survive restarts. Failed sends retry; rare duplicates
remain possible if a network acknowledgment is lost. The model and notification sender run
independently so a slow answer does not block important alerts.

## Your PrizePicks workflow

**On a Knicks game day:** send screenshots of the available lines to the Telegram bot.
OCR returns a draft, not a verified board or placed entry. Correct names, game date,
statistic, line and direction using a message like this (replace the placeholders):

```text
/board YYYY-MM-DD
Jalen Brunson | points | more | ACTUAL_LINE | NYK
Opponent Player | rebounds | less | ACTUAL_LINE | OPPONENT_TEAM
```

The optional fifth field is the actual team abbreviation. Omit it only for Knicks players:
its default is NYK. Common supported statistics include points, rebounds, assists, threes,
PRA, points+rebounds, points+assists and rebounds+assists. Full-game markets are the automatic
starter; partial periods need separate data and reconciliation.

The bot returns a board ID. Then:

```text
/analyze BOARD_ID
```

It returns historical support and research alternatives. An all-Knicks board needs a line
from another team before a two-team research entry can be suggested. These commands do not
place bets or access your PrizePicks account.

After you actually place an entry, record the **exact 2–6 selections** in its own small
board and track it. Example syntax only—use your slip's real stake, type and multiplier:

```text
/track BOARD_ID 10 power 3
/entries
/results
```

For an entry with some lines selected from a larger board, create a separate board containing
only those selected lines before `/track`. JSON `/entry` is also available for advanced use.
Games grade automatically after the collected status becomes final. For account reconciliation:

```text
/settle ENTRY_ID 30 Confirmed total return shown on my PrizePicks slip
```

`30` means **total returned including stake**, not profit. A $10 stake with $30 returned
has $20 net profit; zero return records a financial loss. Use `/settle` again with a note
if PrizePicks corrects the payout; the earlier settlement stays in the audit trail.

`/today`, `/news`, `/summary`, `/newsnote`, `/status`, `/ai_status`, `/diagnose`, `/importstats`, `/boards`, `/entries`, `/results` and ordinary questions are
also supported. CSV/JSON board documents use fields `date,player,team,stat,line,side`.
No unplaced suggestion is counted as one of your actual bets.

## Repairing HTTP 403 feed failures on an existing deployment

HTTP403 means a responding server/proxy refused the request. It does not by itself
prove the cause or that moving providers will help. Passwords and Telegram tokens do
not authenticate requests to these public sports feeds.

This patch adds an independent NBA CDN adapter, RSS news sources and an owner-only
`/diagnose` command. It does not claim that another free source is guaranteed accessible.
Source fixtures, parsing and selection are tested; actual reachability must be checked
from the deployed container.

1. Open your **existing deployment → Edit/Update SDL**. Preserve your private password,
   Telegram token, allowed user ID and the existing `/data` persistent-volume settings.
2. Copy the new `SOURCE_REF` value from this repository's raw `deploy.yaml`. Replace
   that source reference and use the current free setup below. The independent news feeds
   must still succeed on your provider before they can supply updates:

   ```text
   NBA_DATA_SOURCE=manual
   NEWS_SOURCE=rss
   NEWS_FEED_URLS=https://www.rotowire.com/rss/news.php?sport=NBA,https://sny.tv/knicks-feed,https://elitesportsny.com/category/nyc-teams/e-new-york-knicks/feed
   NEWS_ALERT_SCOPE=league
   OLLAMA_MODEL=qwen2.5:3b
   ```

3. Apply the update. After bootstrap completes, send `/diagnose` to your Telegram bot.
   It checks fixed public URLs and reports status, content type and selected safe
   response headers. It never prints tokens, cookies, environment variables or private
   subscriber URLs. You can share this report for diagnosis.
4. Send `/status` and look for successful `rss_1`, `rss_2` and `rss_3` updates and a rising
   news count. Statistics remain manually supplied; disabled game collectors are expected.
   Superseded ESPN entries are explicitly marked disabled. The optional NBA CDN adapter
   can be selected only if its provider-side connectivity check succeeds.
   A successful schedule alone does not prove final player statistics are available.
5. If NBA CDN also returns403, it remains unavailable—do not count old imported data
   as refreshed. Use the diagnostic report with provider support to determine whether
   an egress policy or upstream refusal applies. A source-authorized API plan may be
   needed if free sources remain inaccessible.

Repeated401/403/429 responses put that hostname into a persistent 15-minute cooldown
with capped backoff (up to 1 hour; a longer numeric Retry-After is honored up to 24 hours).
Other publishers can continue independently. A manual `/diagnose` is a one-shot check
for the owner, not a proxy, rotating identity or access-control bypass.

The NBA CDN adapter uses native NBA game IDs only for its own box-score requests, keeps
existing canonical database IDs, preserves valid fields absent from another source and
rejects incomplete/mismatched final box scores. It cannot provide comprehensive injuries.

## News and availability feeds

The SDL uses manual game statistics and automatic published RSS news.
Optional ESPN/NBA CDN game adapters can be selected explicitly after access is verified. They require
no API key, but are not verified working replacements: a check from the deployed Akash
container returned ESPN HTTP403, NBA CDN ReadTimeout, and CBS HTTP429. Local CDN checks
also returned403. Publisher directories validate the RSS URLs, not Akash access.

Use `/diagnose` after the patch to check fixed public URLs, including the ESNY Knicks
feed. Do not assume an accessible headline feed restores player statistics or injuries.

You can explicitly choose `NBA_DATA_SOURCE=espn` or `NEWS_SOURCE=espn` if those endpoints
are accessible. No automatic IP/hostname/header workarounds are attempted after a denial.

Add optional
published/licensed RSS or Atom URLs in private configuration as a comma-separated value:

```text
NEWS_FEED_URLS=https://your-published-feed.example/nba.xml,https://another-feed.example/knicks.xml
```

Those example domains are placeholders, not working subscriptions. Follow each publisher's usage terms.
The bot deduplicates and timestamps articles; text is treated as evidence, not instructions.

`INJURY_FEED_URL` is optional. It must return this **normalized JSON schema**, not an arbitrary
Sportradar/RotoWire endpoint; a provider-specific adapter is needed for different schemas:

```json
{"injuries":[{"id":"provider-record-id","player":"Player Name","team":"NYK","status":"Out","published_at":"2026-10-29T18:00:00Z","source":"Your provider","url":"https://public-source.example/article","detail":"Provider's published update"}]}
```

Keep private feed keys out of GitHub. Without this setting `/status` explicitly marks it
disabled. The NBA CDN selection does not produce a dedicated injury-status feed. ESPN summary availability, when explicitly selected, remains partial. For low-latency professional coverage,
select and license a provider before adding its adapter; no paid subscription is configured.

## Maintenance and development

- Do not use GitHub commits as the live database. The persistent database is the master;
  the browser offers current Excel and consistent SQLite backup downloads.
- Seed imports are checksum tracked and insert missing history only, preserving live
  corrections even during first-start collection. Schema upgrades preserve existing data.
- `/api/master/status` and `/status` expose freshness/errors. News and research output
  cannot guarantee every crucial event has been observed.
- Environment names are shown in [`chatbot/.env.example`](chatbot/.env.example).
  The example is not automatically loaded: export the variables or configure Akash env.

Local development with Python3.12, Ollama running and the repository workbook:

```sh
python -m venv .venv
. .venv/bin/activate
pip install -r chatbot/requirements.txt
export DATA_DIR="$PWD/runtime"
export OLLAMA_URL=http://127.0.0.1:11434
export WORKBOOK_PATH="$PWD/data/Knicks_2026_27_Completed.xlsx"
read -rsp 'Chat password (16+ characters): ' CHAT_PASSWORD
export CHAT_PASSWORD
python -m uvicorn app:app --app-dir chatbot --host 127.0.0.1 --port 8000 --workers 1 --no-access-log
```

Optional screenshot OCR needs the system `tesseract` executable. Run checks:

```sh
cd chatbot
python -m unittest discover -v
```

Tests cover database persistence/migration, canonical identifiers, feed parsing/corrections,
source failures, timeline leakage, read-only SQL, immutable entries, provisional grading,
confirmed payouts, Telegram allowlisting, upload drafts, retries and alert responsiveness.
Actual Telegram delivery and Akash volume/provider operation require private setup/deployment;
mocked Telegram tests are not evidence of a connected production bot.

## Primary references

- [Akash persistent storage](https://akash.network/docs/developers/deployment/akash-sdl/advanced-features/)
- [Telegram bot creation](https://core.telegram.org/bots/tutorial#obtain-your-bot-token)
- [Telegram long polling](https://core.telegram.org/bots/api#getupdates)
- [Official NBA injury reports](https://official.nba.com/nba-injury-report-2026-27-season/)
- [PrizePicks DNPs, Reboots and ties](https://www.prizepicks.com/help-center/dnps-reboots-and-ties)

- [CBS published RSS directory](https://www.cbssports.com/xml/rss)
- [Elite Sports NY published RSS directory](https://elitesportsny.com/esny-rss-feeds/)

- [RotoWire published RSS directory](https://www.rotowire.com/rss/)
- [SNY published RSS directory](https://sny.tv/rss)
