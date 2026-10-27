# Deploying the Knicks bot on Akash

For the current **master-primary** deployment, follow [the short setup guide](AKASH_QUICKSTART.md) and use `deploy/akash/deploy-spreadsheet.yaml`. It starts empty, accepts the private master through the authenticated dashboard, and regenerates local outputs at 9 a.m. Eastern. Outside integrations remain available but await explicit owner approval for their source and scope. Until then, new game data enters only when an updated master is uploaded.

The seed/provider workflows below describe the separate advanced full-data setup. They are optional future modes; the current spreadsheet-only setup uses the short guide above.

The prepared deployment runs one dashboard and one scheduler in a **single container**, with one shared persistent `/data` volume. Do not deploy these as separate services sharing the same claimed volume: Akash provisions distinct volumes per service. The supervisor stops the container if either child exits, so the provider can restart the complete service. A restart reuses `/data/state` and does not overwrite the database with the initial seed.

These files prepare a deployment. They do not fund an account, push an image, start a paid lease or establish that updates are operating on Akash. Check the live deployment after completing the steps below.

## 1. Finish the workbook configuration locally

For the prepared eight-player dataset, use the supplied `NBAPPBOT_Akash_Data.zip` and its `.sha256` file. The completed workbook has 53 sheets: eight player pages, 29 opponents, and 16 supporting sheets. Reusing this verified archive lets you proceed directly to the image and hosting steps below. The `NBAPPBOT_Akash_Deploy.zip` bundle contains the source code and deployment instructions; the dataset is supplied as a separate archive. Run the commands below when creating a new seed from your own local copy or a later data refresh.

Use the explicit selected NBA player IDs in `organized_players` in your local configuration. The current eight-player selection is Brunson, Towns, Anunoby, Hart, Bridges, McBride, Alvarado and Drummond; a separately configured seven-player cohort remains supported. The daily refresh exports `Knicks_Historical_Organized.xlsx` alongside the canonical `Master_Data.xlsx` when this selection is configured. The reference audit is persisted inside `data/reference`, so it travels with the backup. No game-by-game injury-history tables are added to the organized workbook; operational injury/news snapshots can remain in the underlying database for pregame use.

```bash
cd /workspace/NBAPPBOT
.venv/bin/nbabot --config config.local.json validate
.venv/bin/nbabot --config config.local.json export
.venv/bin/python scripts/akash-seed.py --config config.local.json --output data/backups/akash-seed-2026-10-26.zip
```

The seed script takes the refresh lock, uses SQLite's consistent backup operation, checks SQLite integrity and verifies the latest package hashes. It includes the canonical database with source/correction/history records, preserved originals, archived reference data, configuration and the latest verified data package. Old package versions, caches and wheel files are excluded to keep the transfer manageable; retain your older backups separately. Runtime locks and service heartbeats are cleared **in the backup only**.

The generated `.sha256` file is required for first-boot verification. Review the seed contents before uploading: the database and original files can contain private provider content, player tracking history and paper selections. Environment token values and dashboard credentials are not copied into the seed. A credential embedded directly in a provider URL is unsafe; remove it and use environment authentication instead.

## 2. Build and publish the application image

Akash providers must be able to pull your image from a registry. A local `nbappbot:akash` tag is insufficient. The image contains application code, pinned dependencies and startup logic; it contains no historical workbook or database.

```bash
bash scripts/akash-build-image.sh nbappbot:akash
```

On another machine with normal Docker build networking, the equivalent command is:

```bash
docker build --platform linux/amd64 -f deploy/akash/Dockerfile -t nbappbot:akash .
```

On a machine already authenticated to your chosen registry, tag and push the built image under your account, then obtain the registry's resulting immutable digest. Example names:

```text
ghcr.io/YOUR_ACCOUNT/nbappbot:2026-10-26
ghcr.io/YOUR_ACCOUNT/nbappbot@sha256:THE_REGISTRY_DIGEST
```

For example, after replacing `YOUR_ACCOUNT` and authenticating securely to GHCR:

```bash
docker tag nbappbot:akash ghcr.io/YOUR_ACCOUNT/nbappbot:2026-10-26
docker push ghcr.io/YOUR_ACCOUNT/nbappbot:2026-10-26
```

Use the digest reported by the registry push in the SDL, and make this code-only package publicly pullable if you are using the template without private-registry credentials.

Choose a publicly pullable code-only image or configure supported private-registry credentials in the Console. Build for `linux/amd64` unless your selected provider explicitly supports your image's architecture. The local helper requires a Linux Python 3.12 virtual environment and Docker; it prepares an offline wheel build context because some cloud Docker builders lack network DNS. A registry push requires your own registry authentication. Never send wallet seed phrases, private keys or registry tokens in chat.

## 3. Host the initial seed and prepare the SDL

Upload the ZIP to storage you control using a short-lived HTTPS download link. Keep private data private. Set the link only in the deployment configuration; it can be visible to the chosen provider. Its expiry should allow enough time for the first boot and retries. An existing verified `/data/state` does not need the URL on later restarts. Remove or rotate the seed link after successful initialization.

The startup process requires `NBABOT_SEED_URL` and `NBABOT_SEED_SHA256` when the persistent state does not exist. It verifies the whole archive and individual files, rejects path traversal and links, checks SQLite integrity, and installs the state with an atomic rename. A broken download, bad checksum or partial existing state stops initialization rather than starting an empty replacement dataset.

Use `deploy/akash/deploy.yaml` as the template. Replace the three `REPLACE_...` values, or render an ignored local file:

```bash
.venv/bin/python scripts/akash-configure.py \
  --image ghcr.io/YOUR_ACCOUNT/nbappbot@sha256:THE_REGISTRY_DIGEST \
  --seed-url https://YOUR_AUTHORIZED_STORAGE/seed.zip \
  --sha256 THE_64_CHARACTER_ARCHIVE_SHA256 \
  --denom uact --max-price 10000 \
  --output data/deploy/akash.yaml
```

`10000` is a configurable **maximum bid amount per block**, not a monthly price or spending recommendation. Review the current Console's accepted denomination and real cost estimate before creating a deployment. Current official repository examples use `uact`; older Akash docs use `uakt` or IBC denominations. Use the denomination supported by your chosen funding route and current Console. No price, provider quote or required deposit has been verified for your account.

The template requests 2 vCPU, 6 GiB RAM, 4 GiB ephemeral space and 64 GiB persistent `beta3` storage. The organized workbook and canonical export can temporarily require several GiB. Select a provider that supports this storage class and outbound access to `stats.nba.com`, `cdn.nba.com` and `site.api.espn.com`; verify NBA endpoints from the actual deployed host because reachability differs by provider. Provider endpoints and authoritative-source availability can change.

## 4. Create the lease in Akash Console

1. Open [Akash Console](https://console.akash.network), sign in and use its current account funding flow. Managed Console and self-custody Console Air have different account flows; use the option you intend. Only you should authorize payments and wallet transactions.
2. Create a deployment from your rendered SDL. Let the Console validate it. This repository has checked its YAML structure and persistent-volume wiring; a live Akash schema/provider validation requires the actual account and deployment.
3. Review bids, operator reputation, persistent storage, resources and estimated price. Select a provider and authorize the lease/deposit. Keep enough balance for renewal and enable the available balance/closure alerts.
4. Open its logs. Look for `Installed verified seed into persistent state`, followed by `Dashboard and daily scheduler started under one supervisor`.
5. Use the Console-provided public hostname and its HTTPS ingress/custom-domain configuration. Do not assume the provider's first HTTP link provides TLS. Before exposing private master files, configure HTTPS and dashboard authentication or restrict access at a trusted proxy.

The optional environment variables `NBABOT_DASHBOARD_USER` and `NBABOT_DASHBOARD_PASSWORD` enable HTTP Basic authentication in the app. Set both through secure deployment settings; do not commit their values into the SDL or use Basic authentication over plain HTTP. A provider may be able to inspect workload environment variables; use unique credentials. The `/health` endpoint remains a minimal reachability check for the provider. There are no public write or wager-submission endpoints.

## 5. Verify the schedule and downloads

In the Console's container shell, run:

```bash
python -m nbabot --config /data/state/config.json scheduler status
python -m nbabot --config /data/state/config.json source-check
python -m nbabot --config /data/state/config.json refresh
python /app/akash-runtime.py health
```

The scheduler uses `09:00` in `America/New_York` by default, including daylight saving changes, with Knicks pregame refreshes 120, 60 and 15 minutes before tipoff. Change these values in `/data/state/config.json` and restart the deployment to apply scheduler configuration. The database and file exports remain on the mounted volume.

Keep `scoped_history_enabled: true` to update the additional historical references during successful daily and pregame refreshes. These requests cover the eight selected Knicks players and the primary/alternate opposing players explicitly named in the workbook's matchup rules. They do not expand the historical enrichment into every opponent roster. The existing league-wide canonical game database remains the broader baseline.

The scoped maintenance collects selected-player career regular-season and playoff games, independently checked career controls, available game opportunities (including potential assists, rebound chances and touches), measured defender pairs, shot events, historical starts and reconstructed rotation stints. Completed captures are reused with integrity checks; current-season career tables refresh on each call, and newly completed Knicks fixtures enter the request scope. A changed source, failed join or unavailable historical endpoint remains a coverage warning. A successful core refresh can retain a partial optional-reference report, so check `scoped_history` in the refresh result and the workbook's `Missing_Data` sheet as well as the core update status.

`historical_retry_hours` defaults to `24`. This cooldown applies to failed historical rotation requests, avoiding the same unavailable endpoint at every pregame poll. The failure remains recorded and does not become a zero or a successful capture. To inspect or deliberately retry the historical scope independently, use the `backfill-matchups` command below; `--force` bypasses verified-cache reuse and the rotation failure cooldown, so use it only after a meaningful source change or when checking a correction.

Automatic refreshes limit new or retried rotation requests to `historical_rotation_batch_size`, which defaults to `8` per run. Unseen games take priority, and eligible historical failures rotate by oldest attempt time after the cooldown. This bounds repeated unavailable-source work during pregame maintenance; it does not mark the remaining games complete. The manual `backfill-matchups` command processes the full requested scope by default; add `--rotation-batch 8` to bound its rotation work too. Other reference types retain their own verified-cache behavior.

```bash
python -m nbabot --config /data/state/config.json backfill-matchups
```

New prospective feature-cutoff snapshots are archived under `/data/state/reference/pregame_snapshots`. Their timestamps describe what was retrieved and available **now**. They do not manufacture historical pregame injury records, reconstruct earlier knowledge or make old games eligible for a strict as-of backtest. Newly collected opportunities, defender pairs, shots and rotations remain reference data until chronological validation supports a model effect. Collection alone does not calibrate probabilities or authorize an automatic minutes/news adjustment.

Verify all of the following in the dashboard/API:

- A recent scheduler heartbeat and current `operating_in_this_instance` status.
- A **successful** refresh run with source coverage and failed/stale source information visible.
- `/api/export/Knicks_Historical_Organized.xlsx` downloads the current, valid file, with the configured eight player sheets first and 29 opponent sheets next.
- `/api/export/manifest.json` records the package creation time, coverage cutoff and matching file hashes. An old download is not evidence of today's successful refresh.
- Scoped reference status, failed sources and the prospective snapshot cutoff agree with the current refresh and workbook coverage notes. A source's historical coverage date and its current retrieval time remain separate.
- After a controlled container restart within the same lease, canonical game counts, stable IDs, source history and the latest package survive. After the next scheduled 9 a.m. run, the update log records that actual scheduled outcome.

The Docker image's health check combines the dashboard response with a scheduler heartbeat under 90 seconds old. Akash provider platforms may not honor Docker `HEALTHCHECK`; use the Console's live status, logs and an external uptime check. The app cannot independently attest that your paid lease will remain funded or available. A current heartbeat establishes operation in that instance, not permanent availability.

Recommendations remain withheld when required current information or actual offers are missing/stale. Completing this deployment does not validate experimental projection probabilities or make a profitable-selection claim.

## Daily files, backup and recovery

Successful updates publish a dated package and atomically change `/data/state/packages/latest`. Failed refreshes preserve the last verified package and record failures for retry. The organized workbook is regenerated from the same canonical data and archived references. The seed includes the entire `reference/` tree, including career, opportunities, matchups, rotations, historical context and prospective cutoff snapshots. Each reference retains its real source cutoff and retrieval time; a newly completed game does not silently make an unavailable tracking, shot or rotation observation current.

**Persistent storage lasts only for the lease.** Closing the lease, moving to another provider or losing the deployment can lose it; reopening a lease at the same provider does not restore the volume. Before closing, migrating, resizing or updating a deployment, download a verified backup to storage outside the provider. Keep the old lease until restoration on the new provider is verified.

For a fresh backup inside the container, copy `scripts/akash-seed.py` from the source repository or include it in an operations workspace, then run it against `/data/state/config.json`. The code-only runtime image intentionally contains the same helper at `/app/akash-seed.py`:

```bash
python /app/akash-seed.py --config /data/state/config.json --output /data/backup-2026-10-26.zip
```

Use an authenticated provider-shell file transfer or upload that backup to authorized object storage using your own secure operations workflow. Check its SHA256 after downloading. A backup only on `/data` is not disaster recovery. Configure a daily off-provider backup before relying on this deployment long term; no external backup destination or credentials have been supplied here, so off-provider backups are not automatically active.

Full immutable packages can consume roughly 0.8–1+ GiB per daily run with the current dataset, and pregame updates create more versions. **64 GiB is a starting allocation, not unlimited retention.** The app's configurable package retention defaults to 14 days while retaining at least seven recent packages; cleanup runs after a successful publish and never deletes the canonical database or its source/correction history. Size your storage for pregame versions too, monitor usage and copy longer-term recovery versions off-provider. Review the retention settings in your configuration before relying on them. A failed publish retains the last valid files, but a full disk also prevents future successful updates.

To restore, create a fresh lease/empty volume with a known-good ZIP and matching checksum. Startup installs that state and repairs the controlled `latest` link. Never unzip over a running database. If an existing state is incomplete, stop services and inspect/rename it before explicit restoration. Compare the restored canonical counts, newest game date, configured player selection and package hashes before closing the old lease.

## Sources checked for the deployment structure

The Akash website was inaccessible from this execution environment, so the authoritative public Akash GitHub repositories were used. The current Console README distinguishes managed Console and self-custody Console Air. Storage documentation describes volume mounting and lease-lifetime limitations; older documentation can contain historical tooling/payment examples, so use the current Console's validation and quote before signing.

- [Current managed Console README](https://github.com/akash-network/console/blob/main/README.md)
- [Current Console Air README](https://github.com/akash-network/console-air/blob/main/README.md)
- [Official WordPress SDL example](https://github.com/akash-network/awesome-akash/blob/master/wordpress/deploy.yaml)
- [Persistent storage limitations](https://github.com/akash-network/docs/blob/master/features/persistent-storage/persistent-storage-limitations.md)
- [Persistent storage SDL detail](https://github.com/akash-network/docs/blob/master/features/persistent-storage/persistent-storage-sdl-deepdive.md)

No Akash wallet binding, provider lease or registry push credential was established during preparation. Build/tests and local container checks are separate from real deployment operation.
