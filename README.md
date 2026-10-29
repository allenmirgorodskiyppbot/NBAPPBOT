# NBAPPBOT — always-on Knicks research and PrizePicks tracking

This version runs NBA collectors, a durable master dataset, manual PrizePicks line
analysis, entry grading, private Telegram chat and a local Qwen3 model on Akash.
News/statistics services do not depend on the AI model finishing an answer. No OpenAI key is used.

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
articles do not flood Telegram. New or changed injury/trade/lineup reports then generate
source-linked alerts. Publication time is retained; unknown times stay unknown. Rumor
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
- Supports NBA CDN or ESPN game collection and published RSS or ESPN news; sends relevant
  Knicks, opponent and tracked-player updates to your private Telegram chat.
- Waits normally 5 minutes between completed cycles, or 1 minute during live games/pre-Knicks-tip
  window. Source freshness and collection time are different: polling cannot make an
  upstream source instantaneous. Periodic API quotas/availability still apply.
- Initially backfills 7 days, revisits 3 recent days for corrections and resumes missed
  dates from durable checkpoints after downtime.
- Keeps source snapshots, update timestamps and feed errors. Alerts on transitions into
  feed failure/staleness and recovery; `/status` shows details.
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

`/today`, `/news`, `/newsnote`, `/status`, `/diagnose`, `/importstats`, `/boards`, `/entries`, `/results` and ordinary questions are
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
