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

- The container imports once when it starts. The page then checks the import
  folder when it opens, when you return to the tab, and every 5 minutes while
  it's open. **Import JSON** checks right away.
- The line under the sidebar text shows how many export files were found, or
  in red, why none could be read. The same message is in the container log.
- Each export is a full snapshot of the app. A file is imported again
  whenever it changes, which matters because the app keeps rewriting the
  current day's file. Files are applied oldest to newest, so the newest
  snapshot wins.
- Only files named `po-tracker-*.json` are read. Syncthing's temporary
  files are ignored.

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
**Deploy** in Dockge. Everything is re-imported when the container starts.

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
