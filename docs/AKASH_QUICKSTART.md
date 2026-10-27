# Deploy the master-first spreadsheet bot on Akash

This deployment starts with **your uploaded master spreadsheet as the primary source**. It starts empty, and the workbook stays outside the public repository and container image. Outside integrations remain available but pending your explicit approval. The initial configuration sets `external_sources_approved: false`, so NBA statistics, injuries, rosters, lineups, ESPN news and other online data are not requested before approval.

The default **9:00 a.m. America/New_York** job rescans the stored master and regenerates the local outputs. While outside integrations await approval, **new games enter when you upload an updated master**. A local daily rescan does not make yesterday's spreadsheet current. Later approval is an explicit owner configuration step for the source and scope you choose; the existing integrations and data model remain available.

## 1. Get the published container image

Open the repository's [Actions page](https://github.com/allenmirgorodskiyppbot/NBAPPBOT/actions). The `Build Akash image` workflow runs when application changes are pushed to `codex/akash-ready`. It runs the tests, builds for `linux/amd64`, and publishes a code-only image using GitHub's built-in `GITHUB_TOKEN`.

Wait for a green run. Open its summary and copy the complete pinned image:

```text
ghcr.io/allenmirgorodskiyppbot/nbappbot@sha256:THE_ACTUAL_REGISTRY_DIGEST
```

Under your GitHub account's **Packages**, open the `nbappbot` container package and its settings. Make the **code-only package public** so the provider can pull it without a registry password. This does not publish the workbook; no workbook or database is included in the image.

If no run exists, the source or workflow has not yet been published successfully. If GitHub blocks workflow creation or Actions is disabled, finish that repository/account step before deploying. The local Docker image ID is not a registry manifest digest, and a placeholder image cannot start the app.

## 2. Prepare your private Akash deployment form

Open [deploy/akash/deploy-spreadsheet.yaml](../deploy/akash/deploy-spreadsheet.yaml) and change:

- `REPLACE_IMAGE_DIGEST`: the digest from the successful Actions run.
- `REPLACE_WITH_A_PRIVATE_PASSWORD`: a unique password, entered in your private Console form or secure deployment environment settings.

Keep `NBABOT_INIT_MODE=spreadsheet`, `NBABOT_CONFIG=/data/state/config.json` and both dashboard authentication variables. The runtime rejects a missing or placeholder password. Do not commit the filled password to GitHub or share your filled SDL publicly. The chosen provider can inspect workload environment variables; use a unique password.

The template requests **2 vCPU, 6 GiB RAM and 64 GiB persistent storage**, mounted at `/data`. The dashboard and scheduler run in one container and share the database. Select a provider supporting the requested persistent storage class.

The sample `uact`/`10000` pricing is a configurable maximum bid **per block**, not a monthly price or spending recommendation. Use the current Console's accepted denomination and review the actual provider quote and required deposit.

## 3. Create the lease and enable HTTPS

1. Open [Akash Console](https://console.akash.network), sign in and use its current funding flow.
2. Create a deployment using your private, edited SDL. Let the Console validate it.
3. Review provider bids, persistent storage, resources and estimated ongoing cost, then authorize the selected lease.
4. In the logs, look for `Created empty spreadsheet-only state; waiting for private master upload` and `Dashboard and daily scheduler started under one supervisor`.
5. Configure HTTPS using the Console/provider's supported gateway or a trusted TLS reverse proxy/custom domain. Use the HTTPS address before logging in or uploading the master. The first plain HTTP hostname is not sufficient for private password/file transfer.

The empty deployment waits for your file. It does not need a seed ZIP, object-storage URL or external data-provider connection. The `/health` probe remains a minimal public process check; a healthy process does not establish that a master was uploaded or that its data is current.

## 4. Upload the master privately

Open the HTTPS dashboard and log in with `owner` and the password from your private deployment settings. Use the dashboard's master upload control to choose your local Excel workbook.

The upload API accepts `.xlsx` files up to **32 MiB (33,554,432 bytes)**. The SDL raises the provider ingress body limit to **40 MiB** and sets read/send timeouts to **180,000 milliseconds (180 seconds)**. This avoids the official ingress default of 1 MiB rejecting a normal master before it reaches the app. Upload processing returns a job response and the dashboard polls its status; the workbook does not need to finish processing inside one proxy request. If the provider returns `413 Request Entity Too Large` for a supported file, verify that it applied the SDL's `http_options`. The app's own 32 MiB limit still applies.

The field names, byte limit and timeout units were checked against the official [SDL HTTP-options implementation](https://github.com/akash-network/chain-sdk/blob/141c80c320cb0ce0977583d1eeda45aedba40196/go/sdl/v2.go#L123) and [provider ingress mapping](https://github.com/akash-network/provider/blob/11c36f2a53f0d80ed551a4ed61b8b261bdc282e0/cluster/kube/client_ingress.go#L37). Enter the dashboard credentials only in the private Console form or secure deployment environment settings, and use HTTPS for the upload.

The app preserves the uploaded original, validates its supported data and regenerates the package from that master. Check the upload result and coverage cutoff before using the outputs. Source timestamps, missing observations, unavailable fields and uncertainty remain visible. An unsupported or failed import must not replace the last verified master.

The organized Excel download is available from this deployed dashboard after a successful import. For now, upload a newer master to add completed games or corrected statistics. Outside sources stay inactive until your explicit approval; connecting an account or restarting the deployment does not grant that approval.

## 5. Verify daily operation and persistence

In the Console's container shell, run:

```bash
python -m nbabot --config /data/state/config.json scheduler status
python /app/akash-runtime.py health
```

Confirm a recent scheduler heartbeat, the uploaded master identity, coverage cutoff and successful local regeneration in the dashboard. The dashboard should identify the master as the active source and external integrations as **awaiting owner approval**. Keep `external_sources_approved` false until you approve the intended source and scope. Existing public-source bootstrap and feed adapters remain available for that later approval.

Restart the container within the same lease and verify that the uploaded master and latest package survive. After the next **actual scheduled 9 a.m. Eastern** run, inspect its outcome before relying on daily operation. A recent heartbeat is runtime evidence; the app cannot verify that the external Akash lease stays funded.

## Recovery

Persistent storage is tied to the lease and does not automatically move to another provider. Keep your master and a verified backup outside the provider before closing or migrating the lease. Versioned local packages use atomic publication and configurable retention, but a backup only on `/data` is not off-provider recovery.

The [full operations guide](AKASH.md) describes existing seed and outside-source integrations. The current deployment remains focused on the master; an owner can approve specific outside sources later without replacing the application or its data model. No outside data request should run before that approval.
