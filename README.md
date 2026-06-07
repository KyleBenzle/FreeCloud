<table width="100%">
  <tr>
    <td align="left" valign="middle">
      <img src="website/logo_small.png" width="120" alt="FreeCloud logo">
    </td>
    <td align="right" valign="middle">
      <h1>FreeCloud</h1>
    </td>
  </tr>
</table>

## Stop paying twice for storage

FreeCloud turns the web hosting space you already pay for into a simple personal cloud drive.

It currently includes:
- a desktop sync app for Linux/macOS, with a Windows launcher path
- a working Android app
- a PHP web file manager and sync API you upload to your host
- a public website/download page

If you already pay for:
- web hosting
- cloud storage
- maybe email storage

then you may already be paying for the same kind of disk space more than once.

FreeCloud is the simple version of using that hosting space instead.

## Public Links

- Website: `https://freecloud.wiki`
- Source: `https://github.com/KyleBenzle/FreeCloud`

## What FreeCloud Is

FreeCloud is a hosted file sync and browser drive built around your own web host.

You upload the server files to a folder like:

```text
public_html/FreeCloud/
```

Then your desktop app, Android app, and browser all talk to that same hosted FreeCloud install.

## Current Platforms

- Linux desktop: primary tested desktop path
- macOS desktop: same Python/Tk desktop app, but still needs real-world Mac testing
- Windows desktop: launcher and compatibility path exist, but it has not been fully tested
- Android: working app
- iPhone/iPad: not implemented

## Setup

### 1. Upload the server files

Upload the contents of [`server/`](server) to your web host so it looks like this:

```text
public_html/FreeCloud/index.php
public_html/FreeCloud/freecloud_api.php
public_html/FreeCloud/freecloud.php
public_html/FreeCloud/freecloud_download.php
public_html/FreeCloud/freecloud_preview.php
```

It should **not** look like this:

```text
public_html/FreeCloud/server/freecloud_api.php
```

Then visit:

```text
https://yourdomain.com/FreeCloud
```

### 2. Run the desktop app

Linux / macOS:

```bash
./Run_Mac_Linux.sh
```

Windows:

```bat
Run_Windows.bat
```

In the desktop app, enter:
- `Domain`: `https://yourdomain.com`
- `Cloud drive folder`: `FreeCloud`
- `Local folder`: your normal local sync folder
- `FreeCloud password`: the password you want this hosted drive to use

On first save:
- if the hosted drive is not initialized yet, the app creates it
- if it is already initialized, the app uses the existing password

### 3. Use the web view

Once uploaded, your hosted browser drive is available at:

```text
https://yourdomain.com/FreeCloud
```

You can:
- upload files
- browse folders
- preview supported media
- edit supported text files
- download files or ZIP folders

### 4. Use the Android app

The Android app connects to the same hosted FreeCloud install.

Use:
- `Domain`: `https://yourdomain.com`
- `Drive name`: `FreeCloud`
- `Password`: the same FreeCloud password
- `Phone folder`: the local Android folder you want to use

## Project Structure

### `server/`

The actual hosted FreeCloud web app and API:

- [`server/index.php`](server/index.php)
- [`server/freecloud.php`](server/freecloud.php)
- [`server/freecloud_api.php`](server/freecloud_api.php)
- [`server/freecloud_download.php`](server/freecloud_download.php)
- [`server/freecloud_preview.php`](server/freecloud_preview.php)

### Desktop app

The current desktop app is built from:

- [`freecloud_ui.py`](freecloud_ui.py): desktop GUI
- [`freecloud_cli.py`](freecloud_cli.py): sync engine / CLI
- [`Run_Mac_Linux.sh`](Run_Mac_Linux.sh): Linux/macOS launcher
- [`Run_Windows.bat`](Run_Windows.bat): Windows launcher

### `android-app/`

Android app source project.

### `website/`

Public landing/download site assets, including:

- [`website/index.html`](website/index.html)
- [`website/privacy.html`](website/privacy.html)
- [`website/FreeCloud.zip`](website/FreeCloud.zip)

This folder is for the public website, not the hosted FreeCloud backend.

## How Sync Works

The current sync model is simple:

- local changes upload to the hosted FreeCloud server
- remote changes download back to the local folder
- remote deletions remove unchanged local files
- local deletions do **not** delete remote files unless the CLI is started with `--delete-remote`

This behavior is intentional, but it is not fully symmetrical.

## Configuration Files

Desktop/local config currently uses simple JSON files:

- last-used config:
  - `.freecloud_last_config.json`
- per-local-folder config:
  - `.freecloud_client.json`
- sync state:
  - `.freecloud_state.json`

These are convenient, but they are not hardened secrets storage.

## Current Architecture

FreeCloud currently has three main user-facing layers:

1. Desktop sync client
2. Android client
3. Hosted PHP web app and API

The hosted server stores synced files in:

```text
freecloud_files/
```

The main server pieces are:

- `freecloud_api.php` for sync actions
- `freecloud.php` for browser file management
- `freecloud_preview.php` for inline previews
- `freecloud_download.php` for downloads and ZIP export

## Security Notes

FreeCloud is practical, but not fully hardened.

Important current realities:

- storage protection currently depends heavily on host configuration
- the app stores local credentials in plaintext JSON
- the web UI does not currently have CSRF protection
- some risky file types are still allowed through the browser file manager
- large uploads are still relatively simple and not highly robust

So:
- this is not meant to be a hardened enterprise storage platform
- it is meant to be a small, useful personal hosted drive

## Known Limitations

- macOS path still needs broader real-device testing
- Windows path is best-effort and not deeply tested
- no iPhone/iPad app
- no multi-sync manager yet
- no self-host mode right now
- no GitHub publishing integration right now

## Development Notes

If you are working on the project locally:

- the desktop app is the main path for Linux/macOS
- the Android app lives in [`android-app/`](android-app)
- the public website lives in [`website/`](website)
- the hosted backend lives in [`server/`](server)
- the current architecture and sync flow are summarized in [`PROJECT_NOTES.md`](PROJECT_NOTES.md)

## Why This Exists

Because paying for hosting and then paying again for storage often makes no sense.

You already have the space.

Use it.
