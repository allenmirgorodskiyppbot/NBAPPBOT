# Sources, daily maintenance, deployment and recovery

Daily refresh defaults to **09:00 America/New_York**, handling daylight-saving changes. Pregame offsets default to 120, 60 and 15 minutes. Both are configurable. Internally timestamps have explicit UTC offsets; the dashboard displays Eastern time.

The foreground scheduler has durable SQLite jobs, heartbeat, cross-process refresh locking and bounded retries after 1/5/15 minutes (four attempts). Starting after today's schedule triggers catch-up, not replay of every missed day. Completed jobs are idempotent. Postponed or rescheduled fixtures obsolete old pregame jobs. Live processes do not survive stopped cloud tasks.

## Connections

Official NBA adapters implement schedule retrieval at `https://cdn.nba.com/static/json/staticData/scheduleLeagueV2_1.json` and player-game logs at `https://stats.nba.com/stats/playergamelogs`. Logs overlap the latest seven days to capture recent corrections. They preserve source minutes and supported counting fields. They do not claim complete DNP/intraday injury coverage or discovery of all older corrections. An authorized vendor can replace these sections and provide periodic full-season reconciliation.

With `public_context_enabled: true` (the default), the app requests current rosters from NBA `commonteamroster`, then public ESPN publisher JSON for injuries, team rosters, news, dated scoreboards and game summaries. No token or private API is used. ESPN endpoints are best-effort public publisher responses, not a licensed subscription or guaranteed stable contract. A complete current-instance refresh now passes against actual responses. Required domains are saved for future environments; a draft save alone does not establish runtime access. NBA requests use the publisher's standard origin/referrer headers and explicit period/filter parameters. Current season controls are independently fetched and reconciled, with prior versions preserved.

NBA IDs remain canonical. ESPN identity crosswalks require a unique exact full name and current NBA team, and preserve both evidence URLs; ambiguous names stay unresolved. Source roster membership does not establish health. Injury listings retain publication/retrieval times, expected-return fields and raw details; they become `reported_out/reported_inactive`, not a confirmed absence for a later game. Unlisted or listed-active players remain unknown without explicit game-specific clearance. Current complete rosters deactivate departed members without overwriting historical teams. Publisher articles remain unconfirmed until reviewed, with unresolved affected IDs visible and no headline-based adjustment. General summaries do not certify pregame lineups; realized box-score starters are never backfilled as known before the game.

Sources are `site.api.espn.com/apis/site/v2/sports/basketball/nba/injuries`, `/news`, `/teams/{id}/roster`, `/scoreboard` and `/summary`, and the official NBA hosts above. Repeated jobs use only the few teams in upcoming Knicks games, with at most four concurrent requests. Response-contract errors, wrong game joins and required-source failures preserve the prior package. All raw responses and identity gaps are archived in `data/publisher_raw` for review. These public feeds may not provide confirmed lineups, minutes restrictions or complete intraday injury history. A custom authorized feed can supply those missing fields through the contract below. Optional tracking/team-defense files are archived but are not yet model features.

For a custom feed, configure `provider_url`, the explicit `provider_allowed_hosts`, and the variable named by `provider_token_env` if authentication is needed. Inject credentials securely; never store them in source, JSON bundles, logs, URLs or data packages. Proxy secret placeholders work only at their scoped HTTPS destinations. Check bindings and a scoped read before asking for keys. TLS/package verification is never disabled, and authorization-bearing redirects are not followed. Cloud environment network settings must permit `cdn.nba.com`, `stats.nba.com`, `site.api.espn.com` and any chosen custom host. Custom feeds take precedence over public context.

Alternatively, set `provider_file` to a locally delivered normalized JSON bundle from an authorized vendor/export process. Re-reading an old file does not refresh its verification timestamp. `nbabot.providers.OfferProvider` and `AuthorizedJSONOffers` are extension points for authorized market-board providers using the same importer and review checks; no private API is assumed.

Bearer authentication is the default. For an authorized provider using a key header, configure `provider_auth_header` (for example `X-API-Key`) and an empty `provider_auth_scheme`, while keeping the actual value in the named secret variable. `nbabot source-check` makes a read-only scoped request and reports contract/section status and binding presence without logging token values or publishing data. An endpoint or response requiring a custom mapping must be identified before connection can be claimed.

## Normalized bundle

All six sections—`stats`, `schedule`, `rosters`, `injuries`, `lineups`, `news`—need an envelope:

```json
{
  "sections": {
    "stats": {
      "complete": true,
      "verified_at": "2026-10-22T18:00:00Z",
      "coverage_cutoff": "2026-10-21",
      "source_url": "https://YOUR_AUTHORIZED_PROVIDER/evidence",
      "records": []
    }
  }
}
```

This deliberately incomplete example fails validation. Completeness is an attestation, not proof: evidence and records must support it. Empty mandatory stats, schedules or rosters fail. Empty injury/news/lineup arrays mean no observations and **do not certify health or a confirmed lineup**. Future or stale verification times fail.

| Section | Record shape |
|---|---|
| stats | `GAME_ID`, `PLAYER_ID`, `PLAYER_NAME`, `TEAM_ABBREVIATION`, `Opponent`, `Season`, `GAME_DATE`, `Appeared`, source-precision `MIN`; supported `PTS/REB/AST/FG3M/FG3A/FGM/FGA/FTM/FTA` and other counts. Optional `MIN_SECONDS`, `known_at`, `Source ID`. Same-day records require `game_status: completed`. |
| schedule | `id`, `date_et`, `tipoff` with offset, `home`, `away`, `status` (`scheduled/completed/postponed/live/unknown`), `season`; optional metadata. Stable identity persists through postponement. |
| rosters | `player_id`, `name`, `team`, `observed_at`; optional `active` boolean. Include both current teams, trades and signings. Latest per-player snapshot wins without rewriting historical teams. |
| injuries | `player_id`, `game_id`, `status`, `observed_at`; optional `minutes_cap`, `expected_return`, `rest_announcement`, notes/evidence. Explicit `available/confirmed_active` is required to clear uncertainty. `out/inactive/confirmed_absent` withhold played forecasts. Unknown/questionable/doubtful remain uncertain. |
| lineups | `game_id`, `team`, `observed_at`, `confirmation`; include starters, rotation details and evidence. Expected and confirmed must stay distinct. |
| news | `id`, `headline`, `source_url`, `published_at`, `retrieved_at`, affected `players` ID array, `confirmation` (`confirmed/unconfirmed`); optional teams/coaches/context. No headline-based statistical adjustment. |

Publication, retrieval and observation times are distinct. Snapshots append rather than overwrite. Every fetched bundle, including failed runs, is retained in `data/refresh_raw`; old/new statistical changes remain in audit and version tables. Missing records never become zero. A source URL alone does not verify correctness.

## Publication and failure handling

The refresh validates canonical data in a transaction, recomputes projections and candidates, then builds a dated directory containing CSVs, `Master_Data.xlsx`, portable SQLite and a SHA-256 manifest. The stable `data/packages/latest` pointer changes atomically after a complete version is built. Runtime faults roll back data and restore the prior pointer. Startup/`recover` reconciles the small filesystem/SQLite crash window against committed update records. Unreferenced partial directories are not valid recovery versions.

Canonical/recomputed tables include `NBA_Player_Games`, `Player_Season_Derived`, `Upcoming_Projections`, fixtures, identities, freshness, snapshot histories, audit and update logs. Original summaries remain dated `Raw_` tables. Incomplete derived totals remain blank and carry observation counts. The manifest identifies coverage, last successful refresh, stale/missing sources and validation results.

Failed required sources retain the prior verified database and package, record failure logs and withhold candidates. Later injury/lineup/news changes, new projections and new offer boards supersede prior decisions, preserving the archive. An incomplete source is not published just because another source succeeded.

For recovery, stop scheduler/web processes, preserve all of `data`, run `recover` and inspect `validate`. Repair source failures before publishing. Dated versions hold their own portable `database.sqlite3`; verify manifest hashes before selecting a recovery version. With all writers stopped, preserve the old database/WAL/SHM, copy the chosen portable database into `data/nba.sqlite3`, remove only old database WAL/SHM files, and point latest at that same dated directory. Restart, revalidate and refresh live information. Never replace a database while processes hold it open.

## Persistent deployment

Development commands:

```bash
python -m nbabot serve
python -m nbabot scheduler run
python -m nbabot scheduler status
```

These are temporary processes. A chat-session heartbeat does not establish durable automation. On an **always-on host**:

```bash
cp config.example.json config.local.json  # only when absent
# Configure authorized sources and secure secret injection.
docker compose up --build -d
docker compose ps
docker compose logs --tail=100 scheduler
docker compose exec scheduler python -m nbabot scheduler status
```

The stack persists `data`, restarts services, and binds the dashboard to host loopback. Keep container `data_dir` as `data`. Put remote access behind your own authenticated reverse proxy. Inject tokens through your deployment secret manager; they are not baked into the image. Use one scheduler per data directory.

If Docker's build network has no DNS access while native package downloads work, the tested Linux/Python 3.12 alternative is `scripts/build-image.sh`, then `docker compose up -d --no-build`. It downloads verified distribution wheels through the native environment, records hashes, and builds `nbappbot:local` offline. The context includes only application code, pins and wheels, never uploaded data or credentials. Docker's public base image pull must still work. The native workflow and offline image are tested; persistent hosting remains an operator prerequisite.

Alternatively, from the configured virtualenv on a persistent Linux host with an available user systemd manager:

```bash
python -m nbabot scheduler install
systemctl --user status nbabot-scheduler.service
```

Installation verifies the manager exists and preserves differing existing units. User lingering may be required by host policy. Configure credentials through secure service environment management. This cloud machine has no user systemd bus; no persistent systemd service is claimed.

`/health` proves reachability/database access, not freshness or profitable picks. `/api/overview` exposes separate source and scheduler states. Logs go to stderr or your manager. Environment draft saves do not execute scripts or publish a snapshot; review/save and publish through environment settings. No live process is assumed to survive publication.
