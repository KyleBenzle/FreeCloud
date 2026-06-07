# FreeCloud Project Notes

FreeCloud is one hosted PHP drive with two sync clients.

## Main Flow

1. Upload the contents of `server/` into a public web folder such as `public_html/FreeCloud/`.
2. Visit `https://yourdomain.com/FreeCloud/` or save setup from a client.
3. The server creates:
   - `config.json` for the drive name and password hash
   - `freecloud_files/` for user files
   - `freecloud_sessions/` for browser login sessions
4. Desktop and Android clients poll `freecloud_api.php`.
5. The clients compare local files, remote manifest entries, and the last saved sync state.
6. Changed local files upload; changed remote files download.

## Backend

`server/freecloud_api.php` is the sync API:

- `ping`: confirm setup/auth state
- `setup`: create `config.json`
- `manifest`: recursive remote file list
- `list`: one-folder remote file list
- `mkdir`: create a remote folder
- `upload`: write a file
- `download`: stream one file
- `save`: save text content through the API
- `delete`: remove a remote item

`server/freecloud.php` is the browser file manager. It uses the same storage folder but handles browser sessions, upload forms, previews, text editing, and web deletes.

`server/freecloud_preview.php` streams inline previews with range support for media. `server/freecloud_download.php` downloads files and zips folders.

## Desktop App

`freecloud_ui.py` is the Tk app. It saves setup, starts/stops the CLI sync process, shows remote files, and offers manual remote download/delete.

`freecloud_cli.py` owns the desktop sync logic. It stores:

- `~/.config/freecloud/last_config.json` for the last selected setup on Linux
- `~/.local/state/freecloud/` for app logs and the background sync PID on Linux
- `~/.local/state/freecloud/remote_tree_cache.json` for the last known hosted file tree
- `.freecloud_client.json` inside the local folder
- `.freecloud_state.json` inside the local folder

Older development builds saved `.freecloud_last_config.json` beside the app. Current code reads that file once as a fallback and then writes the newer per-user config location.

The desktop UI loads `remote_tree_cache.json` first so the file browser can show the last known cloud files immediately. It then refreshes the full hosted manifest in the background and saves the updated cache.

For Linux packaging:

- install `packaging/linux/freecloud` as `/usr/bin/freecloud`
- install `packaging/linux/freecloud.desktop` or `FreeCloud.desktop` as `/usr/share/applications/freecloud.desktop`
- install `icon.png` into the hicolor icon theme as `freecloud`
- install the Python files and `server/` into `/usr/share/freecloud`

Desktop local deletions are safe by default: they do not delete remote files unless the CLI is started with `--delete-remote`.

## Android App

The Android app uses Storage Access Framework folder permissions instead of raw filesystem paths.

Important files:

- `MainActivity.kt`: setup screen, file browser, menus, and service controls
- `SyncService.kt`: foreground polling loop
- `SyncEngine.kt`: local/remote/state comparison
- `FreeCloudApiClient.kt`: PHP API client
- `DocumentTreeSync.kt`: Storage Access Framework file operations
- `BootReceiver.kt`: restarts sync after boot or package replacement when setup exists

Android stores config in shared preferences and sync state in app-private `freecloud_sync_state.json`.

## Current Safety Model

The server password is stored as a PHP password hash, but clients store the password locally in plaintext config/preferences. This keeps setup simple, but it is not hardened secret storage.

Storage protection relies on the host honoring `.htaccess` inside `freecloud_files/`. On hosts that do not honor Apache `.htaccess`, the storage folder may need equivalent server-side deny rules.

The sync model intentionally favors preserving remote files. Local deletion should not remove remote files unless a client explicitly supports and enables that behavior.
