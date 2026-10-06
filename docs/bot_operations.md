# Polytrader bot operations

The website has four views: **Overview**, **Bots**, **Deployments**, and **Reports**.
Use it to inspect health, read research summaries, add configurations, and deploy
selected bots. `polytraderctl` provides the same deployment and recovery operations
from the server. Both strategies remain public-data research tools: lead/follower
paper trading and time-arbitrage opportunity detection, with no live order execution.

## New server: quickstart

The bootstrap targets **Ubuntu 24.04 on x86-64**, with systemd and a local persistent
disk. The published release currently targets `amd64`. Use a reviewed checkout and
reviewed market configurations. Server provisioning and private-network enrollment
are outside this repository.

### 1. Publish an image and download its release manifest

Push the reviewed code to GitHub so **Test and publish** can build the image. From
an authenticated workstation with GitHub CLI installed:

```sh
gh workflow run test-build.yaml --repo paulbeka/polytrader --ref main
gh run list --repo paulbeka/polytrader --workflow test-build.yaml --limit 5
# Set RUN_ID to the successful run for the commit you intend to deploy.
RUN_ID=REPLACE_WITH_RUN_ID
gh run watch "$RUN_ID" --repo paulbeka/polytrader
gh run download "$RUN_ID" --repo paulbeka/polytrader -n polytrader-release -D ./release
scp ./release/release.json YOUR_SERVER:~/release.json
```

The artifact contains a readable `build-<commit>` label, exact image digests, build
metadata and schema compatibility. Importing the same manifest is safe; replacing
an existing label with a different image is rejected. No mutable `latest` tag is used.
Use your actual branch instead of `main` if different.

### 2. Bootstrap the server

On the server:

```sh
sudo apt-get update
sudo apt-get install -y git
sudo git clone https://github.com/paulbeka/polytrader.git /opt/polytrader
cd /opt/polytrader
# Use the same reviewed commit that produced release.json.
sudo git checkout REPLACE_WITH_REVIEWED_COMMIT
sudo sh deploy/bootstrap.sh --install-dependencies --root /srv/polytrader
```

`--install-dependencies` installs Python/venv and Docker from Docker's official
Ubuntu repository when Docker is absent. An existing Docker installation must
already provide a working Compose plugin (2.24+). Omitting the flag checks existing
prerequisites. See [Docker's Ubuntu installation instructions](https://docs.docker.com/engine/install/ubuntu/)
for existing/conflicting Docker packages.

Bootstrap creates persistent directories, installs an isolated host helper and
starts a systemd controller. Re-running preserves configs, secrets, releases and
data; it waits for the current controller operation before updating host code.
Existing data ownership is preserved and checked by `doctor`. If migrating older
root-owned data, review permissions explicitly instead of recursively changing
unrelated files.

For private GHCR images, authenticate **the root deployment account**, because the
controller runs as root:

```sh
sudo docker login ghcr.io
```

Use a read-only package token as the password. Keep it out of Git and command history.
Skip login for public images.

### 3. Add configurations and start the platform and bots

Copy templates, review their market references and research settings, then run:

```sh
cd /opt/polytrader
cp src/polytrader/bot/config/lead_follower.example.toml ~/lead.toml
cp src/polytrader/bot/config/example.time_arbitrage.toml ~/arb.toml
# Edit ~/lead.toml and ~/arb.toml to select the markets you intend to observe.
nano ~/lead.toml
nano ~/arb.toml

sudo polytraderctl init --release-file ~/release.json
sudo polytraderctl bot add lead-main --strategy lead_follower --config ~/lead.toml --display-name "Lead research"
sudo polytraderctl bot add arb-main --strategy time_arbitrage --config ~/arb.toml --display-name "Arbitrage research"
sudo polytraderctl doctor
sudo polytraderctl platform up
sudo polytraderctl deploy --all --preview
sudo polytraderctl deploy --all
sudo polytraderctl status
```

`init` imports a release and sets the initial default on the first run; later imports
do not silently change it. `bot add` rejects duplicate IDs. To change a saved config,
use `bot update` with the same arguments, then deploy again. Config revisions are
immutable; historical releases retain their saved configurations.

`doctor` checks Docker/Compose, storage, permissions, image access/architecture,
config parsing, the dashboard port, controller access and (after platform startup)
collector freshness. Configuration checks validate settings;
deployment also validates current public market discovery **before replacing any
selected worker**. An unresolved market or external API outage can block deployment.

Alternatively start the platform after `init`, then use **Bots ? Add a bot** and
**Deployments** to configure and launch bots through the website.

### 4. Reach the website privately

The dashboard listens on the server's `127.0.0.1:8501`. Install/enroll Tailscale using
[its Linux installation guide](https://tailscale.com/docs/install/linux), then:

```sh
curl -fsSL https://tailscale.com/install.sh -o /tmp/polytrader-tailscale-install.sh
sudo sh /tmp/polytrader-tailscale-install.sh
sudo tailscale up
sudo tailscale serve --bg http://127.0.0.1:8501
```

Tailscale prints the private URL. Restrict access to your intended devices through
your tailnet policy. Use private Serve, not public Funnel. The website has management
privileges through a restricted Unix socket, so keep it behind this private boundary.
No Docker socket is mounted in the dashboard. Public or multi-user hosting would
need a separate authentication and authorization design.

## Routine commands

```sh
sudo polytraderctl status
sudo polytraderctl status lead-main --json
sudo polytraderctl logs lead-main --tail 100
sudo polytraderctl stop lead-main
sudo polytraderctl start lead-main
sudo polytraderctl restart lead-main
sudo polytraderctl rollback lead-main --preview
sudo polytraderctl rollback lead-main

# Import a newly published release, then upgrade one bot or the fleet.
sudo polytraderctl init --release-file ~/new-release.json
sudo polytraderctl deploy lead-main --release build-REPLACE --preview
sudo polytraderctl deploy lead-main --release build-REPLACE
sudo polytraderctl deploy --all --release build-REPLACE

# Platform changes are separate from bot updates.
sudo polytraderctl platform update --release build-REPLACE
sudo polytraderctl platform rollback
```

A batch preflights all selected bots, then changes them sequentially. Failure stops
the batch: earlier successful upgrades stay deployed and later bots are untouched.
Each failed worker upgrade attempts to restore its previous image **and saved
configuration**, then checks recovery health. Inspect the recorded result if recovery
fails. Deploying an identical image/config does nothing, including for a stopped bot;
use `start` or `restart` explicitly.

Stop is intentional: `unless-stopped` preserves it across Docker restarts. Running
containers restart on crashes and server boot. A new process creates a fresh research
run, including after rollback; it does not resume open positions or order books.
Process health uses fresh heartbeat, connected transport and working logging, not
market activity. An upgrade can therefore succeed during a quiet market.

`platform up` retains the currently selected platform release. An explicit update
changes it; failed startup attempts to restore the previous platform image. Current
manifests use data/config schema 1. An incompatible schema is rejected. Future schema
changes will require an explicit migration/backup procedure before platform rollback.
The host helper is updated from a reviewed checkout by re-running bootstrap, separately
from container image changes.

## Website workflow and reports

- **Overview:** all configured bots, actionable health states, deployment labels and
  recent activity. Collector freshness is separate from worker health. Quiet markets
  do not make a bot unhealthy. A never-started bot remains visible.
- **Bots:** readable latest-run metrics, activity, config editing and downloadable
  historical artifacts. Settings are validated on save; deploy explicitly to apply
  them. Technical JSON is tucked into expandable details.
- **Deployments:** import `release.json`, choose an action and bots, preview image and
  config changes, then apply. Job progress and logs survive a browser disconnect.
  Repeated submission of the same reviewed request returns the same job.
- **Reports:** Today, Yesterday, Last 7 days or custom dates, with selected-bot HTML,
  CSV and JSON downloads. Versions/configs stay separate. Charts cover ranges up to
  31 days; reports/downloads cover the complete selected range.

Reports use Europe/London calendar boundaries, including DST. Lead/follower results
are hypothetical and fee-excluded. Arbitrage reports count episode openings, not each
update, and never add quoted estimates into earned P&L. Missing observations are
shown as unavailable; observed quiet bots have zero recorded activity. Current
position snapshots are not historical end-of-day reconstructions. Weekly reports
query the period directly rather than summing repeated daily snapshots.

## Troubleshooting and recovery

```sh
sudo polytraderctl doctor
sudo systemctl status polytrader-controller
sudo journalctl -u polytrader-controller -n 100
sudo polytraderctl logs lead-main --tail 100
sudo polytraderctl recover
```

The controller reconciles interrupted deployments at startup. An interrupted
operation is labelled for review rather than blindly replayed; failed recovery blocks
further changes to that bot until repaired. CLI deployment commands use the same
locks and durable job store and remain available when the controller is stopped.
Use `recover` before retrying an interrupted upgrade.

If website management is unavailable, research pages remain accessible. Check the
controller service and `/srv/polytrader/run/control.sock` (group 10001, mode 660).
Do not expose this socket over TCP. If collection is stale, inspect platform logs:

```sh
sudo docker logs polytrader-platform-collector-1 --tail 100
```

The usual host layout is:

```text
/srv/polytrader/
  host-venv/              # installed command/controller package
  instances.toml         # configured bot registry
  configs/               # immutable reviewed configuration revisions
  catalog/               # immutable release manifests
  releases/<bot>/        # saved image/config pairs, current/previous/pending state
  control/               # private job database and initial default release
  locks/                 # shared host locks
  run/control.sock       # restricted management socket
  platform/              # Compose file, private .env, current/previous platform release
  data/<bot>/<run>/       # independent persistent worker output
  data/ops/              # research index, reports, status, backups
```

Put optional backup/alert settings in `/srv/polytrader/platform/.env`; keep it mode
600. Re-run `polytraderctl platform up` after changing settings. Preserve this file,
`instances.toml`, `configs`, `catalog`, `control` and `releases` through your protected
host backup process; application research backups below do not include host secrets.

## Local development and legacy commands

On Windows PowerShell, the dashboard works without a host controller:

```powershell
.venv\Scripts\python.exe -m pip install -e ".[live,ops]"
.venv\Scripts\python.exe -m polytrader.ops collect --root data --once
.venv\Scripts\python.exe -m streamlit run src/polytrader/ops/dashboard.py --server.address=127.0.0.1
```

Open `http://localhost:8501`. Run continuous collection in another terminal by omitting
`--once`. `POLYTRADER_DATA` changes the data root; `POLYTRADER_DATABASE` overrides the
index. `POLYTRADER_CONTROL_SOCKET` enables host management only when a controller is
available. Local previews do not start bots or survive Windows reboot. Existing local
process records live in `.tools/ops-local-processes.json`.

`deploy/deploy.sh` and the restricted SSH GitHub workflow remain available for legacy
single-bot deployments. They retain per-instance locking and digest restrictions;
new deployments should use `polytraderctl` for durable batch/job history. The legacy
SSH entry still targets `/srv/polytrader`; install the reviewed checkout there if you
use the old forced-command setup. Do not grant that automation key an unrestricted
shell. `polytraderctl --root /custom/root ...` supports an explicit installation root.

## Data, reports and recovery

Managed workers enable 16 MiB / one-hour JSONL segments. Rotation happens on the next
write; quiet event streams need no empty segments. Closed segments are gzipped and
checksummed; `segments.json` orders them. Existing single-file sessions still work.
Input replay requires the entire input chain, and the replay reader verifies hashes.
Do not externally copy/truncate active log files. Console logs rotate separately.

Workers write `runtime.json`, atomic `status.json` and `checkpoint.json` every 10
seconds, `current_summary.json` every minute, and a final summary at shutdown.
SIGTERM cancels the root task so writers close within the 45-second grace period.
Open positions from interrupted runs remain unresolved/censored, and a restarted
worker starts fresh with new feed warm-up. It does not resume positions or books.
Quotes and trades can be hours old without failing process/transport health checks.

SQLite is a single-writer, local-disk WAL index; never put it on a network share.
Collector offsets and event keys make ingestion restartable and idempotent. It defers
partial active lines and records malformed complete lines in `issues`. Closed segments
are verified on first ingestion and skipped after indexing. Reports split by instance,
strategy version and config hash. UTC evidence is grouped into Europe/London calendar
days, including DST. Health hours count consecutive observed heartbeats only; gaps are
unknown. Run summaries inside old reports are explicitly snapshots as of generation,
not reconstructed historical position states.

Daily CSV/JSON/HTML reports and `last-7-days` exports appear under `data/ops/reports`. First collection builds
all observed dates; subsequent minute refreshes cover today/yesterday. For importing
older sessions, restart the collector to rebuild historical reports. `--once` builds
reports and exits without triggering automated backups or retention.

Backups use SQLite's backup API and checksummed tar archives. They include closed
segments, manifests, configs in session metadata, reports and checkpoints. Active tails
are excluded; `backup_coverage.json` records whether a run is fully replayable. A local
backup can be tested without cloud credentials:

```powershell
.venv\Scripts\python.exe -m polytrader.ops backup --root data --destination data/ops/backups
.venv\Scripts\python.exe -m polytrader.ops restore data/ops/backups/ARCHIVE.tar.gz .tools/restore-test
```

Restore requires an empty directory, rejects path traversal and checks every file.
Test the restored dashboard/index and replay a completed retained lead/follower run.
When restoring for continued collection, move the restored directory into the data
root while workers are stopped; run the collector to update stored paths. Old active
runs are historical/censored, never automatically resumed. Save host registry/configs
and `.env` separately using your administrator's protected backup process; secrets
are deliberately outside research archives.

Set `POLYTRADER_BACKUP_BUCKET`, standard AWS credentials, and optionally
`S3_ENDPOINT_URL` for nightly uploads. The collector attempts once per UTC day and
retries failures hourly. Only successful offsite backups permit retention: seven days
of complete input chains and 90 days of events, calculated from completed-run time.
Create an empty `PIN` file in a run to retain it. Active/crashed-unfinalized runs are
never automatically pruned. Long-running sessions therefore retain their input chain
until stopped; plan periodic orderly restarts if disk use requires it. Reports and
the historical event index remain long-term. Set bucket lifecycle rules separately.
The collector retains the latest two automatic local backup archives; manual backups
are not automatically bounded. Expired input chains explicitly refuse replay.

Disk usage above 85% is alerted; managed workers stop when free space falls below
1 GiB. Existing evidence and the last checkpoint remain for diagnosis. Capacity must
include gzip rotation scratch space and backup staging (potentially several copies of
retained evidence). Inspect storage before long unattended runs; the initial server
size is a starting assumption, not a storage guarantee.

`data/ops/alerts.json`, dashboard notices and console logs show worker/backup failures.
An optional `POLYTRADER_ALERT_WEBHOOK` receives JSON `{"text":"..."}` on alert changes.
Configure an external VPS uptime check separately: a dead server or dead collector
cannot deliver its own alerts. Use the dashboard's stale collector warning and Docker
health checks to diagnose missing collection.

## Verification and boundaries

```powershell
.venv\Scripts\python.exe -m unittest discover -s tests -q
```

The operations suite covers idempotent/partial ingestion, exact Decimal totals,
strategy separation, DST, heartbeat gaps, disk failure, rotated replay, restore and
retention safeguards, durable management jobs, stale previews, batch failure,
controller request validation, exact period summaries, and Streamlit navigation and
deployment forms. Deployment tests use a fake Docker runner. Linux image smoke tests
and a Linux-only SIGTERM test run in CI. Local Windows checks cannot prove
VPS permissions, Docker restart-on-reboot, Tailscale access or S3 delivery; exercise
those on the selected host before relying on unattended operation. Pinning Python
dependencies makes releases repeatable; the base image follows `python:3.14-slim`,
so use the resulting immutable image digest for deployments and rollback.

The local implementation was checked through Streamlit's application test harness;
an interactive browser was unavailable for desktop/mobile screenshot review. On the
target server also verify two real bots, an individual upgrade/rollback, a reboot
(including an intentionally stopped bot), private website access and backup restore.

No server, bucket, secrets or external notification destination has been provisioned
by this implementation. CI/workflows are prepared but must be pushed to GitHub and
configured before they can publish or deploy.
