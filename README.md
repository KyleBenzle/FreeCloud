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

# FreeCloud

FreeCloud is a personal cloud drive that uses ordinary PHP web hosting as the
central coordinator. The long-term goal is to let one FreeCloud file tree span
multiple storage locations without requiring every file to exist on every
device.

The current project includes:

- a hosted PHP web app and API that can be uploaded to `public_html/FreeCloud`
- a Linux/macOS Python desktop app
- a Python command-line client
- a Raspberry Pi / Linux storage-node mode
- an Android app
- public website/download assets

FreeCloud is free software licensed under the
[GNU General Public License version 3 or later](LICENSE).

Public links:

- Website: `https://freecloud.wiki`
- Source: `https://github.com/KyleBenzle/FreeCloud`

## Current Architecture Direction

FreeCloud is moving from a simple "sync every file everywhere" model to a
metadata-driven storage model.

The important idea is:

```text
The server knows the complete FreeCloud file tree.
The server also knows where each file physically exists.
Clients browse metadata first.
Actual file bytes are downloaded only on demand or because the user selected sync/keep-offline.
```

This allows a large folder, such as an audiobook library on a Raspberry Pi hard
drive, to appear in the FreeCloud web interface without uploading that whole
folder to the web server and without pushing it to every client device.

## Core Components

### Hosted PHP Server

The hosted PHP server is still the center of the system. It lives in
[`server/`](server) and is uploaded directly into a web hosting folder such as:

```text
public_html/FreeCloud/
```

It should look like this on the host:

```text
public_html/FreeCloud/index.php
public_html/FreeCloud/freecloud.php
public_html/FreeCloud/freecloud_api.php
public_html/FreeCloud/freecloud_download.php
public_html/FreeCloud/freecloud_preview.php
public_html/FreeCloud/freecloud_transfer.php
```

It should not look like this:

```text
public_html/FreeCloud/server/freecloud.php
```

The main hosted files are:

- [`server/index.php`](server/index.php): entry point
- [`server/freecloud.php`](server/freecloud.php): browser file manager UI
- [`server/freecloud_api.php`](server/freecloud_api.php): sync/API endpoint
- [`server/freecloud_download.php`](server/freecloud_download.php): direct downloads and ZIP export
- [`server/freecloud_preview.php`](server/freecloud_preview.php): inline previews
- [`server/freecloud_transfer.php`](server/freecloud_transfer.php): temporary node-transfer downloads

### Desktop App

The desktop app is built from:

- [`freecloud_ui.py`](freecloud_ui.py): Tk desktop UI
- [`freecloud_cli.py`](freecloud_cli.py): sync engine, CLI, and storage-node commands
- [`Run_Mac_Linux.sh`](Run_Mac_Linux.sh): Linux/macOS launcher
- [`Run_Windows.bat`](Run_Windows.bat): Windows launcher

The desktop app still contains the original full-sync behavior, but it no longer
starts that full sync automatically after setup or app launch. The cloud browser
portion has been updated to use metadata-backed listings, show where files are
available from, and keep Pi-only files visible without trying to download them
automatically.

### CLI / Raspberry Pi Storage Node

The CLI now has a `node` command group for Linux machines such as a Raspberry Pi.
This mode lets a Linux box publish metadata for local folders without uploading
all file contents to the web server.

Example:

```bash
python3 freecloud_cli.py node setup --name "Raspberry Pi" --type raspberry_pi
python3 freecloud_cli.py node rules add /mnt/freecloud-drive/FreeCloud/Audiobooks --remote /Audiobooks --mode storage_only
python3 freecloud_cli.py node publish
```

That maps this real Pi path:

```text
/mnt/freecloud-drive/FreeCloud/Audiobooks
```

to this FreeCloud virtual path:

```text
/Audiobooks
```

The website can then show `/Audiobooks` even though the actual files remain on
the Pi drive.

### Android App

The Android app lives in [`android-app/`](android-app).

Android now starts moving toward the metadata model. The main file browser uses
`metadata_list` so it can show server metadata, including folders published by a
Raspberry Pi / local node. The older full-sync service still exists, but it is
not started automatically by default.

Android also has a first built-in media player pass:

- audio/music playback uses Android `MediaPlayer`
- video playback uses Android `VideoView`
- playback position is remembered locally
- the next playable file in the current folder starts automatically when one
  file finishes

Pi-only streaming is not complete yet because the server-to-node byte relay is
not implemented. Android can show Pi-only files as metadata, but cannot stream
their bytes until that relay exists.

See [`android-app/README.md`](android-app/README.md) for Android-specific notes.

### Website Assets

The public website/download page lives in [`website/`](website). This is not the
hosted FreeCloud backend. It is the marketing/download site content.

## Hosted Server Storage Layout

Once installed on a web host, FreeCloud uses two main storage areas.

### `freecloud_files/`

This folder contains files physically stored on the web server.

Example:

```text
public_html/FreeCloud/freecloud_files/example.pdf
```

If a file is in `freecloud_files/`, the PHP web server can download, preview,
edit, or zip it directly because the bytes are local to the host.

### `freecloud_meta/`

This folder contains metadata JSON used by the new storage model.

Example files:

```text
public_html/FreeCloud/freecloud_meta/devices.json
public_html/FreeCloud/freecloud_meta/files.json
public_html/FreeCloud/freecloud_meta/sync_rules.json
```

The metadata folder stores information about files and devices. It does not
necessarily store the file contents.

For example, the server can know this:

```text
Path: Audiobooks/Book/chapter01.mp3
Type: file
Size: 12345678
Available from: Raspberry Pi
Storage kind: local_node
```

while the actual file bytes still live here:

```text
/mnt/freecloud-drive/FreeCloud/Audiobooks/Book/chapter01.mp3
```

on the Raspberry Pi.

## Metadata Model

The metadata model separates "what exists" from "where the bytes exist".

Each file or folder metadata record can include:

- `path`: FreeCloud virtual path, such as `Audiobooks/Book/chapter01.mp3`
- `name`: display name
- `type`: `file` or `dir`
- `size`: file size in bytes
- `mtime`: modified time
- `hash`: content hash when available
- `version`: simple version counter
- `deleted`: deletion flag
- `locations`: list of places that currently have the entry

A location can include:

- `kind`: storage type, such as `web_server` or `local_node`
- `device_id`: stable device identifier
- `name`: human-readable device/storage name
- `available`: whether that location is currently considered available
- `updated_at`: last metadata update time

Example conceptual record:

```json
{
  "path": "Audiobooks",
  "name": "Audiobooks",
  "type": "dir",
  "size": 0,
  "mtime": 1787367810,
  "hash": "",
  "version": 1,
  "deleted": false,
  "locations": [
    {
      "kind": "local_node",
      "device_id": "fed9f89cee4e4604af4cb1978ba98111",
      "name": "Raspberry Pi",
      "available": true,
      "updated_at": 1788039790
    }
  ]
}
```

## Device Registration

Every storage node/client can register with the server.

Example:

```bash
python3 freecloud_cli.py node setup --name "Raspberry Pi" --type raspberry_pi
```

The client saves device config locally at:

```text
~/.config/freecloud/device.json
```

That file identifies:

- the FreeCloud server URL
- the device name
- the device type
- the stable device id

The server stores registered devices in:

```text
freecloud_meta/devices.json
```

The device name is used in the web UI so entries can say things like:

```text
Available from Raspberry Pi
```

instead of exposing only a raw device id.

## Sync Rules And Storage Rules

The node mode uses rules to decide which local folders map into the FreeCloud
tree.

A rule contains:

- local source path
- remote FreeCloud path
- mode

Example:

```text
/mnt/freecloud-drive/FreeCloud/Audiobooks -> /Audiobooks [storage_only]
```

The most important current mode is:

```text
storage_only
```

`storage_only` means:

- scan this local folder
- publish metadata to the server
- tell the server this node has the files
- do not upload all file bytes to the web server
- do not force every other device to download the files

This is the intended mode for very large libraries that should be browsable but
not automatically copied everywhere.

## Raspberry Pi Storage Node Workflow

Typical setup on a Pi:

```bash
cd ~/FreeCloud
python3 freecloud_cli.py node setup --name "Raspberry Pi" --type raspberry_pi
```

Add a folder rule:

```bash
python3 freecloud_cli.py node rules add /mnt/freecloud-drive/FreeCloud/Audiobooks --remote /Audiobooks --mode storage_only
```

Check status:

```bash
python3 freecloud_cli.py node status
```

Expected status shape:

```text
Server: https://yourdomain.com/FreeCloud
Device: Raspberry Pi (...)
Rules: 1
  1. /mnt/freecloud-drive/FreeCloud/Audiobooks -> /Audiobooks [storage_only]
```

Publish metadata:

```bash
python3 freecloud_cli.py node publish
```

Expected result:

```text
Published N metadata entries.
```

This does not mean `N` files were uploaded. It means `N` file/folder metadata
records were sent to the server.

## Mounting External Drives On A Pi

For storage-node mode, the external drive should be mounted at a stable path.

Example mount point:

```text
/mnt/freecloud-drive
```

Example audiobook folder:

```text
/mnt/freecloud-drive/FreeCloud/Audiobooks
```

Useful commands:

```bash
lsblk -o NAME,SIZE,FSTYPE,LABEL,MOUNTPOINTS
sudo blkid /dev/sda1
ls /mnt/freecloud-drive
ls /mnt/freecloud-drive/FreeCloud
ls /mnt/freecloud-drive/FreeCloud/Audiobooks
```

The node publisher checks that configured source folders exist before publishing.
This prevents a boot-time problem where the Pi starts before the USB drive is
mounted and accidentally publishes an empty/missing library.

## Autostart On Raspberry Pi / Linux

Node mode supports a user systemd service.

Enable autostart:

```bash
cd ~/FreeCloud
python3 freecloud_cli.py node autostart enable
```

Check status:

```bash
systemctl --user status freecloud-node
```

Restart manually:

```bash
systemctl --user restart freecloud-node
```

View logs:

```bash
journalctl --user -u freecloud-node -f
```

The service runs:

```text
python3 freecloud_cli.py node daemon
```

The daemon is responsible for keeping the node active and publishing/refreshing
metadata over time.

## Web UI Behavior

The web file manager now merges two sources:

1. physical web-server files from `freecloud_files/`
2. metadata-backed files from `freecloud_meta/files.json`

This means the web UI can show:

- normal files stored on the web server
- node-only files stored on a Pi or other local node
- combined folders where some entries are on the server and some are remote

The UI exposes simple location information:

```text
Available from Web Server
Available from Raspberry Pi
Available from Raspberry Pi, +1 more
```

For metadata-only files, the web UI can queue a temporary transfer from an
available local node. The node daemon uploads the requested bytes into protected
temporary server storage, the browser downloads them, and the staged copy is
deleted. This is a one-time web download, not a permanent web-server location.

## API Actions

The hosted API is [`server/freecloud_api.php`](server/freecloud_api.php).

Original sync/file actions still exist for compatibility. Newer metadata/device
actions include:

- `register_device`: register a client or storage node
- `heartbeat`: update device availability/last-seen information
- `list_devices`: list registered devices
- `metadata_manifest`: return metadata-backed manifest
- `poll_transfers`: let an assigned source claim queued transfer work
- `upload_transfer`: stage requested bytes with size/hash verification
- `fail_transfer`: report a source-side transfer failure
- `metadata_list`: list one metadata-backed folder
- `get_sync_rules`: read server-side sync rules
- `set_sync_rules`: update server-side sync rules
- `publish_manifest`: publish metadata from a storage node

The API authenticates using the same FreeCloud password mechanism as the rest of
the app.

Example metadata list request:

```bash
curl -H "X-FreeCloud-Password: YOUR_PASSWORD" \
  "https://yourdomain.com/FreeCloud/freecloud_api.php?action=metadata_list&path="
```

The root response should include entries known through physical storage and
metadata.

## Current Implemented State

The following behavior is implemented:

- hosted PHP server still works as normal FreeCloud web file manager
- old API compatibility remains
- server can store metadata in `freecloud_meta/`
- clients/nodes can register as devices
- Pi/Linux CLI can define storage rules
- Pi/Linux CLI can publish metadata for local folders
- web UI can show metadata-only folders/files
- web UI can show "Available from" location labels
- desktop cloud browser can show metadata-backed entries and availability labels
- desktop setup/app launch opens the cloud browser without automatically starting legacy full sync
- metadata publishing does not upload the actual file bytes
- publisher refuses to publish when configured source roots are missing
- user systemd autostart can run node daemon on Linux/Pi
- web users can request a one-time download of a node-only file through protected temporary staging

Known working example:

```text
Local Pi path:
  /mnt/freecloud-drive/FreeCloud/Audiobooks

FreeCloud virtual path:
  /Audiobooks

Mode:
  storage_only

Result:
  The website shows Audiobooks as available from Raspberry Pi without uploading
  the audiobook files to the web server.
```

## Not Finished Yet

The following pieces are not finished and should not be assumed to work:

- streaming/range playback of node-only files
- full Android selective-sync behavior beyond metadata browsing
- Android on-demand download from a storage node
- Android keep-offline/sync selection
- desktop full selective-sync conversion
- complete conflict-resolution UI
- complete hash/version conflict workflow
- Backblaze B2 provider
- chunked/resumable and node-to-node transfer
- true remote deletion/version tombstone workflow across all clients

The web UI now supports one-time node-only file downloads when the source daemon
is running. Persistent sync and media streaming still require later milestones.

## Target User Behaviors

The intended final behavior is:

### Browse Everything

A client should be able to browse the entire FreeCloud tree from metadata even
when most files are not stored locally.

### One-Time Download

The user can choose Download for a remote file.

Expected final flow:

```text
User requests file.
Server checks metadata.
Server finds a storage location that has the file.
If that node/provider is available, FreeCloud transfers the bytes.
The requesting device receives a normal downloaded copy.
The file is not automatically added to permanent sync.
```

### Keep Offline / Sync On This Device

The user can choose Keep Offline for a file or folder.

Expected final flow:

```text
The item is added to that device's managed sync set.
The file bytes are downloaded.
Future edits upload newer versions.
Other devices that also keep the file offline receive updates.
Folder selection applies recursively.
```

### Remove Local Copy

The user can remove a local cached/offline copy without deleting the canonical
FreeCloud metadata entry or copies on other storage locations.

### Upload

Uploading should create or update file metadata and add/update a storage
location for the uploaded bytes.

### Delete

Delete needs careful semantics. A complete design should distinguish:

- removing only one local copy
- deleting a file from the FreeCloud tree
- deleting one storage provider's copy
- preserving tombstones/conflict history

## Storage Provider Direction

The sync/browse logic should be designed around storage providers rather than
hard-coding every storage type.

Current conceptual provider kinds:

- `web_server`: bytes are in hosted `freecloud_files/`
- `local_node`: bytes are on a registered local device, such as a Raspberry Pi

Future provider kinds:

- `backblaze_b2`: bytes are in Backblaze B2
- other object storage or local node providers

The desired abstraction:

```text
Storage provider can list metadata.
Storage provider can report whether it has a file.
Storage provider can read/stream file bytes.
Storage provider can write file bytes.
Storage provider can delete or mark unavailable.
```

The browser, Android app, and desktop UI should not need to know whether bytes
live on the web host, a Pi, or B2. They should ask the metadata/storage layer.

## Versioning And Conflict Direction

FreeCloud should not rely only on timestamps.

The direction is:

- each file has a current version
- hashes identify content where practical
- clients upload the base version they edited from
- server accepts normal updates when the base version matches current version
- server detects conflict when two clients edited the same older version
- conflicts should be surfaced instead of silently overwriting data

Example:

```text
File is version 3.
Desktop edits version 3.
Android also edits version 3.
Desktop uploads first, creating version 4.
Android uploads later while still based on version 3.
Server detects conflict instead of overwriting version 4.
```

Some hash/version fields exist now, but full cross-client conflict handling is
not complete.

## Install / Setup Summary

### Web Server

Upload the contents of [`server/`](server) directly into:

```text
public_html/FreeCloud/
```

Then open:

```text
https://yourdomain.com/FreeCloud
```

### Desktop App

Linux/macOS:

```bash
./Run_Mac_Linux.sh
```

Windows:

```bat
Run_Windows.bat
```

Desktop setup fields:

- Website Address: `https://yourdomain.com/FreeCloud`
- Local Folder: local sync folder
- Password: FreeCloud password

Important: the legacy desktop sync path still behaves like a full sync client.
It is manual now and labeled as full sync. Do not assume it is fully
selective-sync yet.

### Raspberry Pi / Linux Storage Node

```bash
cd ~/FreeCloud
python3 freecloud_cli.py node setup --name "Raspberry Pi" --type raspberry_pi
python3 freecloud_cli.py node rules add /mnt/freecloud-drive/FreeCloud/Audiobooks --remote /Audiobooks --mode storage_only
python3 freecloud_cli.py node publish
python3 freecloud_cli.py node autostart enable
```

### Android

Open [`android-app/`](android-app) in Android Studio.

The Android app currently has metadata-backed browsing and a first media player
pass. It still needs server-to-node streaming and keep-offline support for the
full metadata/selective-sync model.

## Development Commands

Run Python tests:

```bash
python3 -m unittest discover -s tests
```

Compile-check Python files:

```bash
python3 -m py_compile freecloud_cli.py freecloud_ui.py
```

Lint PHP syntax:

```bash
php -l server/freecloud_api.php
php -l server/freecloud.php
php -l server/freecloud_download.php
php -l server/freecloud_preview.php
php -l server/freecloud_transfer.php
```

Android tests/build, when Java and Android SDK are installed:

```bash
cd android-app
./gradlew test
./gradlew assembleDebug
```

## Important Safety And Security Notes

FreeCloud is a practical personal project, not a hardened enterprise storage
system.

Important current realities:

- desktop/CLI credentials are stored in local JSON config files
- Android stores the saved password through Android Keystore-backed encryption
- password authentication is simple
- the browser UI uses CSRF tokens for mutating form/API-style actions
- storage protection depends partly on web-host configuration
- large uploads are still handled simply
- node-only web download uses temporary staging and is limited by PHP host limits
- conflict handling is not complete
- deletion semantics need more design before aggressive multi-device deletion

Do not paste real production passwords into logs, issue trackers, README files,
or AI chats. If a real password is accidentally exposed, change it.

## Guidance For Future AI / Developer Work

When continuing development, preserve the incremental architecture.

Prefer this path:

1. Keep the hosted PHP server as the coordinator.
2. Keep old file sync working while adding metadata-backed behavior.
3. Keep `freecloud_files/` for web-server-owned bytes.
4. Keep `freecloud_meta/` for metadata, devices, rules, and locations.
5. Treat Raspberry Pi / Linux node storage as `local_node`.
6. Add on-demand download before adding complex provider features.
7. Add keep-offline state after one-time download is reliable.
8. Convert Android and desktop sync to metadata/selective-sync carefully.
9. Add a clean storage provider interface before Backblaze B2.
10. Add Backblaze B2 only after the core model works with web server and local node storage.

Avoid a large rewrite unless the current structure truly blocks the next feature.
The clean transition is to separate metadata from contents, then gradually teach
each client to use metadata first and fetch bytes only when needed.
