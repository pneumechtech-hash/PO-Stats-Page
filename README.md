# PO Tracker Stats

Dashboard for the PO Tracker phone app. It reads the app's JSON exports
(`po-tracker-YYYY-MM-DD.json`) from a folder, keeps them in a small SQLite
database, and shows time, gaps and order stats.

## Run on a server with Dockge

The phone app's auto-export writes one file per day into a folder, and
Syncthing carries that folder to the server. The container reads the
synced folder and serves the dashboard.

The server doesn't need this code. On every push to `main`, GitHub builds
the Docker image and publishes it as
`ghcr.io/pneumechtech-hash/po-stats-page` (see
`.github/workflows/docker-image.yml`), and Dockge pulls it from there.

### One-time setup

1. **Let the server pull the image.** The repo is private, so the image is
   too. Choose one:
   - **Log in, and keep the image private:**
     1. On GitHub, go to Settings → Developer settings → Personal access
        tokens → Tokens (classic), and create a token with only the
        `read:packages` scope.
     2. On the server, run `sudo -H docker login ghcr.io -u pneumechtech-hash`
        and paste the token as the password.
     3. Dockge runs `docker` inside its own container, so it needs that
        login too. In Dockge's own compose file (usually
        `/opt/dockge/compose.yaml`), add this line under the `dockge`
        service's `volumes:`, then run `docker compose up -d` in that folder:
        ```yaml
        - /root/.docker/:/root/.docker
        ```
   - **Or make the image public:** on GitHub, open your profile →
     Packages → `po-stats-page` → Package settings → Change visibility →
     Public. The image holds only the app code, none of your data, and the
     repo stays private. The package appears after the first build finishes.

2. **Create the stack.** In Dockge, add a stack named `po-stats` and paste
   in `docker-compose.yml` from this repo. Then:
   - Set the left side of the `/import` volume to the folder Syncthing
     syncs the exports into.
   - Set `TZ` to your timezone (e.g. `America/Toronto`, `Europe/London`).
     Days and the 07:30–16:00 work window are worked out in this zone.
   - To use another port, change the left side of `5000:5000`.

3. Click **Deploy**, then open `http://<server-ip>:5000`.

## How imports work

- The container checks the import folder when it starts and then every
  minute, whether or not the page is open. **Import JSON** checks right away.
- The line under the sidebar text shows how many export files were found, or
  in red, why none could be read. The same message is in the container log.
- Each export is a full snapshot of the app. A file is imported again
  whenever it changes, which matters because the app keeps rewriting the
  current day's file. Files are applied oldest to newest, so the newest
  snapshot wins.
- Only files named `po-tracker-*.json` are read. Syncthing's temporary
  files are ignored.

## Settings

The gear icon at the top right opens Settings:

- **Work day:** start and end times, lunch, and which days are work days.
  Inactive gaps are measured inside these hours.
- **Targets:** the daily target (7.5 h by default) used by the Avg Hours /
  Day gauge and the gaps summary, the shortest break that counts as a gap,
  and after how many days an untouched order is flagged as idle.
- **Monthly goal:** by default the month's goal is the daily target × the
  month's work days. Set a usual monthly goal to replace that, and add
  specific months (holidays, shutdowns) that need their own goal. The goal
  card's **Edit goal** button sets the current month's goal directly.
- **Dashboard sections:** turn the optional sections on or off: month
  goal, Not in SAP Yet, idle orders, period comparison, typical time per
  tag, heatmap, inactive gaps, patterns and the timesheet export.

Settings are saved on the server, in the database, so every browser and the
Homepage widget use the same ones.

## What's on the dashboard

- **Month goal:** hours this month against the goal, where a steady pace
  would be by today, and the hours per day still needed.
- **Not in SAP Yet:** unfinished orders with time that has no CNF yet,
  oldest first. Finished orders count as done.
- **Compare with previous period:** totals for the selected period, with the
  change since the same days of the previous week or month (or the previous
  work day for Today).
- **Typical time per tag:** median and average total time of finished
  orders with each tag, which helps when estimating a job.
- **Orders table:** search by PO, notes or tag, filter by status (including
  "Not in SAP yet"), click a tag to show only its orders, and download a
  **Timesheet CSV** (time per day and order) for the selected period.
- Today's inactive time only counts up to now; the rest of the day shows
  as "Rest of today" on the timeline.

## Deleting an order

To remove an order from the stats, such as a test PO, click it to open its
details, then click **Delete order** and confirm. It disappears from every
total, chart and list, and from the Homepage widget.

- It stays deleted even while the app's exports still include it, because
  imports skip deleted orders. You don't have to delete it in the app first.
- Right after deleting, the message at the bottom right has an **Undo**
  button. Later, open Settings: **Deleted orders** lists every deleted order,
  with a **Restore** button.
- This only changes the stats database. The order stays in the phone app
  and in the export files.

## Homepage widget

[Homepage](https://gethomepage.dev) can show a tile with today's and this
week's tracked time, using its Custom API widget and this app's
`/api/homepage` endpoint. Add this to Homepage's `services.yaml`, replacing
`<server-ip>` with the server's address (Homepage fetches the URL itself,
so it has to be reachable from the Homepage container):

```yaml
- Work:
    - PO Tracker:
        icon: mdi-timer-outline
        href: http://<server-ip>:5000
        description: PO time tracking
        widget:
          type: customapi
          url: http://<server-ip>:5000/api/homepage
          refreshInterval: 60000
          mappings:
            - field: today
              label: Today
            - field: week
              label: This week
            - field: active_orders
              label: Active orders
              format: number
            - field: last_sync_ago
              label: Last export
```

All the fields `/api/homepage` returns, if you want to show others:

| Field           | Example        | Meaning                                         |
|-----------------|----------------|-------------------------------------------------|
| `today`         | `5h 12m`       | Time tracked today                              |
| `week`          | `32h 10m`      | Time tracked this week (Monday to today)        |
| `week_pct`      | `80`           | This week as a % of the daily target × work days (`format: percent`) |
| `month`         | `62h 10m`      | Time tracked this month                         |
| `month_goal`    | `140h`         | This month's goal (see Settings)                |
| `month_pct`     | `44`           | Month as a % of the goal (`format: percent`)    |
| `month_left`    | `77h 50m`      | Hours still needed for the goal                 |
| `month_pace`    | `3h 20m behind` | Ahead of or behind a steady pace               |
| `month_per_day` | `7h 05m`       | Hours per remaining work day to reach the goal  |
| `today_seconds`, `week_seconds`, `month_seconds` | `18720` | The same in seconds (`format: duration`) |
| `orders_today`  | `3`            | Orders worked on today                          |
| `active_orders` | `12`           | Unfinished orders                               |
| `idle_orders`   | `2`            | Unfinished orders untouched for the idle period in Settings |
| `not_in_sap`    | `4h 30m`       | Worked time without a CNF yet, on unfinished orders |
| `last_sync_ago` | `5 min ago`    | When the newest export was written              |
| `last_sync`     | ISO date/time  | The same as a date (`format: relativeDate`)     |

`last_sync_ago` is a handy health check: if it says days ago on a work
day, the phone's auto-export or Syncthing has stopped.

## Updating

1. Commit and push to `main`.
2. Wait for the build: the repo's **Actions** tab shows a green check when
   it's done (a few minutes). Changes to `.md` files don't trigger a build.
3. In Dockge, open the `po-stats` stack and click **Update**. It pulls the
   new image and restarts the container.

The database lives in the `po-stats-data` volume, so it survives updates.

## Backups

The export files in the Syncthing folder are the real data. The database
can always be rebuilt from them. To start fresh, run
`docker compose down -v` in the stack's folder (usually
`/opt/stacks/po-stats`; this deletes the database volume), then click
**Deploy** in Dockge. Everything is re-imported when the container starts,
but Settings go back to the defaults, and orders you deleted on the
dashboard come back if the exports still include them, since both live in
the database.

## Permissions

The container runs as user ID 1000 and only needs to **read** the import
folder. If the dashboard shows no data and the logs mention permission
errors, either make the folder readable (`chmod -R a+rX <folder>`), or add
`user: "<uid>:<gid>"` under `po-stats:` in `docker-compose.yml`, using the
owner of that folder (`stat -c '%u:%g' <folder>` prints it).

On Fedora, RHEL and other SELinux systems, add `,z` to the volume
(`/srv/syncthing/po-tracker:/import:ro,z`) or the container can't read it.

## Troubleshooting: the dashboard is empty

Run these on the server:

```sh
docker logs po-stats                    # look for the "Import" lines
docker exec po-stats ls -la /import     # what the container actually sees
docker inspect po-stats --format '{{ range .Mounts }}{{ .Source }} -> {{ .Destination }}{{ println }}{{ end }}'
```

- **"The import folder /import is empty"** or **"No po-tracker-\*.json
  files"**: the volume points at the wrong folder. The `inspect` line shows
  which host folder is mounted. A relative path like `./import` is relative
  to the stack's folder. After changing it, click **Deploy** in Dockge (a
  restart alone keeps the old volume).
- **"No permission"** or **"Permission denied"**: see Permissions above.

## Troubleshooting: Update or Deploy fails

- **`denied` or `unauthorized`**: the server isn't allowed to pull the
  private image. See step 1 of the setup.
- **`manifest unknown` or `not found`**: no image has been published yet.
  Check the repo's Actions tab for a failed or unfinished build.

## Security

There is no login. Keep it on your home network or behind a VPN. Don't
forward the port to the internet.

## Run on Windows without Docker

Double-click `run.bat`. It reads exports from this folder and opens on
http://localhost:5000.

Optional environment variables, for either setup:

| Variable              | Default         | Meaning                        |
|-----------------------|-----------------|--------------------------------|
| `PO_STATS_IMPORT_DIR` | this folder     | Where to look for exports      |
| `PO_STATS_DB`         | `po_stats.db` here | Path of the SQLite database |
| `PO_STATS_IMPORT_EVERY` | `60`          | Seconds between background import checks (Docker only; `0` turns them off) |
