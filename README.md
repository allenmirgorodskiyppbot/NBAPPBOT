# NBAPPBOT — always-on Knicks research and PrizePicks tracking

This version runs NBA collectors, a durable master dataset, manual PrizePicks line
analysis, entry grading, private Telegram chat and a local Qwen3 model on Akash.
The collectors do not depend on the AI model finishing an answer. No OpenAI key is used.

**Implemented does not mean deployed:** importing this repository alone does not start
Akash services. Telegram stays disabled until its token and numeric user ID are supplied
privately. The public template contains no credentials.

## What this version does

- Seeds the master database once from the embedded Excel history: 6,350 player-game
  observations. The larger companion ZIP has not been imported.
- Collects available NBA schedules and box scores for **all teams**, including live and
  completed games, rather than only Knicks games.
- Checks ESPN NBA news and partial game-summary availability snapshots; sends relevant
  Knicks, opponent and tracked-player updates to your private Telegram chat.
- Runs normally every 5 minutes and every minute during live games or the pre-Knicks-tip
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

The free ESPN endpoints are best-effort public sources without a service guarantee.
Their availability information is **not a complete official NBA injury report**. Trades
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

`/today`, `/news`, `/status`, `/boards`, `/entries`, `/results` and ordinary questions are
also supported. CSV/JSON board documents use fields `date,player,team,stat,line,side`.
No unplaced suggestion is counted as one of your actual bets.

## News and availability feeds

ESPN games/news start automatically: no key is needed for the baseline. Add optional
published/licensed RSS or Atom URLs in private configuration as a comma-separated value:

```text
NEWS_FEED_URLS=https://your-published-feed.example/nba.xml,https://another-feed.example/knicks.xml
```

Those are placeholders, not working subscriptions. Follow the publisher's usage terms.
The bot deduplicates and timestamps articles; text is treated as evidence, not instructions.

`INJURY_FEED_URL` is optional. It must return this **normalized JSON schema**, not an arbitrary
Sportradar/RotoWire endpoint; a provider-specific adapter is needed for different schemas:

```json
{"injuries":[{"id":"provider-record-id","player":"Player Name","team":"NYK","status":"Out","published_at":"2026-10-29T18:00:00Z","source":"Your provider","url":"https://public-source.example/article","detail":"Provider's published update"}]}
```

Keep private feed keys out of GitHub. Without this setting `/status` explicitly marks it
disabled. ESPN summary availability remains partial. For low-latency professional coverage,
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
