# Easier bot management, deployment and reporting

Plan: 6 October 2026. The implementation now includes host setup/commands, release
manifests, durable management jobs, website controls and readable period reports.
Follow [bot_operations.md](bot_operations.md) for the implemented commands and
prerequisites. Linux/Docker deployment, reboot, private-network access and visual
layout verification still require an environment with those capabilities.

## Outcome and scope

Make it straightforward to set up one Linux server, start several independently
configured bots, upgrade selected bots to a new Docker release, and understand
their health and research results from the website.

Keep the existing Streamlit dashboard, Docker Compose deployment, SQLite collector,
and persistent run files. Both strategies remain paper-trading/research tools.
Use the existing shared application image initially: different bot instances can
run different immutable image versions without requiring an image per bot.

Planning assumption: provide website management controls as well as server commands.
The command interface is delivered first and remains usable if the website is down.
If command-only management is preferred, omit the website controller and expose
deployment previews and copyable commands in its place.

## What exists and what needs to change

| Area | Existing implementation | Proposed improvement |
|---|---|---|
| Website | Instance table, nested selectors, raw summary JSON | Fleet overview, readable bot summaries, obvious navigation and actions |
| Reports | Daily JSON/CSV; HTML wraps JSON in a preformatted block | Readable daily and weekly reports, comparisons and useful exports |
| Deployment | Digest-pinned images, per-instance locks, config snapshots, startup checks and rollback | Named releases, deployment previews, durable job progress and batch updates |
| Server setup | Manual directories, ownership, config registry and environment files | Repeatable bootstrap and one management command |
| Image publishing | GitHub Actions builds images and prints a digest | Machine-readable release manifest with commit, digests and compatibility metadata |

The dashboard currently returns early when there is no index or run history. Setup
and deployment navigation must remain available on an empty installation.

## Website experience

### Overview

- Show total bots, healthy bots, bots needing attention, and intentionally stopped
  bots. Keep collector freshness and server/storage health visible separately.
- Show one readable row per configured bot: friendly name, strategy, selected
  markets, status, release, last heartbeat and a strategy-appropriate result.
- Translate internal states into Running, Starting, Warming up, Reconnecting,
  Degraded, Stopped, Failed, Never started, or Unknown, with a short explanation.
- Put failures and actionable alerts first. A quiet market is not a broken bot;
  missing collector data is not proof that a worker stopped.
- Provide clear entry points to Add bot, Deploy release, and View reports, including
  on a fresh server before any data exists.

### Bots

Selecting a bot opens Summary, Activity, Configuration, and Run history. Default to
the current deployment and recent results; move run IDs, hashes and raw JSON into
technical details. Separate the desired state, observed container state, worker
heartbeat, and collector freshness instead of deriving everything from the latest run.

Lead/follower summaries show closed paper P&L, closed trades, wins/losses/breakevens,
open or unresolved positions, and common rejection reasons. Time-arbitrage summaries
show distinct opportunity episodes, active opportunities, and the best quoted estimate.
Count opportunity lifecycle events correctly rather than counting each update as a
new opportunity. Show unavailable values explicitly when older runs lack evidence.

Add bot is a short flow: name -> strategy -> configuration template or existing
config -> market/config validation -> release -> deployment preview. Show friendly
field descriptions and validation errors; retain an advanced TOML editor. Existing
example markets must be reviewed and validated before a bot starts.

Actions: deploy/update, stop, start, restart and rollback. Explain that a new process
starts a new research run; interrupted positions are not resumed. Before a change,
show affected bots, current/target releases and configuration differences.

### Deployments

- Choose a readable release label with commit, build date and full digest available
  in details. Resolve labels to immutable digests before enqueueing any work.
- Select one bot or a set, preview the changes, then follow queued, validating,
  pulling, starting, checking health, succeeded, failed or rolled-back stages.
- Upgrade a batch sequentially, verify each bot, and stop the batch on failure.
  Keep successful earlier upgrades and list untouched bots explicitly; do not imply
  a fleet-wide atomic transaction. Offer individual rollback.
- Record previous/target image and config, timestamps, requesting identity/source,
  logs and final outcome. Show whether rollback itself recovered successfully.
- Keep platform updates separate from worker updates so a bot release does not
  unnecessarily restart the dashboard or collector.

### Reports

- Add Today, Yesterday, Last 7 days and custom date ranges; use Europe/London
  calendar boundaries consistently and label them. Offer UTC in technical details.
- Lead with a short factual summary of activity, results, interruptions and changes
  since the prior equivalent period. Generate this from recorded data and templates.
- Provide fleet and per-bot views, separate strategy sections, trend charts, and
  side-by-side comparisons within the same strategy. Split results by config/version
  by default; label any explicitly combined history.
- Include healthy-but-inactive bots and data gaps. Distinguish zero activity from
  missing observations, and show report coverage and generation time.
- Preserve exact decimal values in exports. Label lead/follower P&L as hypothetical
  and fee-excluded; never sum arbitrage estimates into earned or fleet P&L.
- Keep current open-position snapshots distinct from historical period results.
  Weekly reports must not add repeated daily position snapshots together.
- Produce formatted HTML, CSV and JSON using the same reporting model as the UI.
  Reports continue to generate without an open browser. Downloads respect selected
  bots and dates; fleet-wide downloads are an explicit separate option.

## Deployment and server design

### One management interface

Add `polytraderctl` as a thin interface over the existing deployment module, extending
it instead of maintaining separate deployment logic for the shell and website.
Support `doctor`, `init`, `bot add`, `config validate`, `platform up/update`,
`deploy`, `status`, `logs`, `start`, `stop`, `restart`, and `rollback`.

Make bootstrap repeatable: validate the supported Linux distribution and prerequisite
versions, create missing directories and service users, install the host helper in
an isolated environment, set ownership for new data directories, and configure boot
startup. Re-running must preserve existing configuration, secrets, releases and data.
Remove the shell wrapper's hard-coded checkout path and support an explicit root.
Provide actionable prerequisite installation instructions and an optional explicit
dependency-install mode for the initially supported distribution.

Use a release manifest containing a human label, commit, immutable worker/platform
image digests, supported architecture and config/data compatibility. It can reference
the same digest for all roles. Publish it from CI, keep a local release catalog, and
support importing a manifest for command-only installations. Keep the allowlisted
repository validation. Registry login is only required for private images.

Deploying an unchanged image/config pair is a no-op unless restart is explicitly
requested. Preflight the entire selected set before changes; use existing instance
locks and validation before replacement. Publish durable operation state and check
recovery after failed upgrades, including a failed first deployment with no rollback.
On controller restart, reconcile interrupted jobs with actual containers and saved
release state before accepting another change to the same bot.

### Website controls

Use a small host management service that calls the deployment module. The dashboard
submits allowlisted requests through a restricted local Unix socket; it receives job
IDs immediately and polls for progress. Keep privileged Docker access in this host
service. Do not mount the Docker socket in the dashboard or expose arbitrary shell
commands, host paths or unrestricted image names through the interface.

Retain private Tailscale access as the single-user website boundary, with management
available only behind that boundary. Restrict the socket to the dashboard service
identity; keep management credentials out of browser state. Preserve CSRF protection
and explicitly authorize mutation requests. Public or multi-user access would require
a separate authentication/roles design.

Persist management jobs in a separate control store owned by the host service;
the collector remains the only writer of the research index. Use immutable config
revisions and atomic registry changes. Both CLI and UI use the same validation,
locking and audit path. Keep a documented host recovery path if the service is down.

### Target new-server commands

Target experience, on the documented Linux image with Git,
Python, Docker Engine/Compose and private access installed. Replace placeholders and
provide reviewed bot configs. Publishing the first image is a separate CI step.

```sh
git clone <repository-url> polytrader
cd polytrader
sudo sh deploy/bootstrap.sh --root /srv/polytrader

# For a private image only: authenticate the deployment account to GHCR first.
sudo polytraderctl init --release-file ./release.json
sudo polytraderctl bot add lead-main --strategy lead_follower --config ./lead.toml
sudo polytraderctl bot add arb-main --strategy time_arbitrage --config ./arb.toml
sudo polytraderctl doctor
sudo polytraderctl platform up
sudo polytraderctl deploy --all --preview
sudo polytraderctl deploy --all
sudo polytraderctl status
sudo tailscale serve --bg http://127.0.0.1:8501
```

The bootstrap prints the concrete config paths, next steps and eventual dashboard
address. `doctor` checks permissions, disk capacity, registry access, configuration,
port availability and service health where applicable. Setup fails clearly if a
prerequisite or release is missing. The quickstart must also include prerequisite
installation and first-release publishing commands, not assume an existing server.

Routine changes should be similarly short:

```sh
sudo polytraderctl deploy lead-main --release <release-label> --preview
sudo polytraderctl deploy lead-main --release <release-label>
sudo polytraderctl deploy --all --release <release-label>
sudo polytraderctl rollback lead-main
sudo polytraderctl logs lead-main --tail 100
```

## Implementation order and acceptance checks

1. **Bootstrap and command interface.** Extend `ops/deployment.py`, `deploy/`, and
   image publishing; add release manifests, repeatable setup and batch deployment.
   Verify on a clean Linux VM that the documented commands start both strategies,
   survive reboot, preserve intentionally stopped bots, and can be rerun safely.
   Verify independent upgrades, failed validation, startup failure and rollback.
2. **Dashboard and shared read models.** Split `ops/dashboard.py` into small views
   and add query/presentation helpers. Build Overview and bot details using existing
   runs before adding mutation controls. Check empty, running, stopped, stale,
   degraded, mixed-version and legacy datasets; visually review desktop and narrow
   layouts. All configured bots must remain visible, including never-started bots.
3. **Readable reports.** Extend `ops/reports.py` and strategy adapters with a shared
   period summary model and formatted HTML. Verify daily/weekly totals, exact money,
   DST boundaries, zero activity, missing observations, snapshot semantics and correct
   instance/config filtering. Compare exported and displayed figures.
4. **Website management.** Add the host service, durable jobs, release picker,
   configuration flow and deployment history. Check repeated clicks, concurrent
   requests, browser disconnection, service restart mid-deploy, invalid requests,
   unavailable controller and failed rollback. CLI remains available throughout.
5. **End-to-end handover.** Rewrite `bot_operations.md` around a short quickstart,
   routine tasks and troubleshooting. Run the full journey from a clean VM: add two
   bots, deploy, view reports, upgrade one, roll back, reboot and restore a backup.
   Verify older data remains readable after platform updates and document supported
   rollback compatibility. Exercise the actual private access and registry setup.

Deliver each stage as a reviewable change. Preserve existing uncommitted repository
work, research logs and configs. Existing operations tests are a starting point;
real Linux/Docker checks are required for the deployment acceptance criteria.
