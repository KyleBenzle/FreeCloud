# FreeCloud — Code Review, Reliability Audit, and Android Redesign Plan

Date: 2026-10-23. Source reviewed: `~/MEGA/FreeCloud` working tree, including uncommitted and untracked files.

**This review was read-only.**
- No source files were modified and nothing was deployed.
- No services were touched and no production data was read or written beyond the targeted checks listed below.
- The Audiobooks collection was not accessed.

**Diagnostics actually run:**
- `php -l` on every server file.
- The Python unit suite (129 tests, run with HOME pointed at a temp dir) and the server tests (51 tests). All passed.
- SHA-256 comparison of deploy-script hashes against the working tree.
- A public DNS lookup for kylebenzle.com: it has no AAAA record.
- Read-only hash and log checks on the Pi, done in the earlier session.
- Probe scripts that ran copies of the PHP API against temporary directories in `/tmp/fcrev/`. Findings marked **[PROBED]** were reproduced this way, not just inferred from the code.

**Line references** are to the current working tree.
- `ui` = `freecloud_ui.py`, `cli` = `freecloud_cli.py`, `api` = `server/freecloud_api.php`, `lifecycle` = `server/freecloud_lifecycle.php`.
- Android paths are relative to `android-app/app/src/main/java/com/freecloud/android/`.

---

## A. Executive summary

**What FreeCloud is today.** FreeCloud has two sync systems living side by side.

1. **Legacy "full sync" engine.**
   - Desktop: `cli.sync_once`. Android: `SyncEngine`.
   - It does a 3-way compare between one device and the web server's file tree, using its own local state file.
2. **Newer Catalog/reconciliation engine.**
   - The server holds a catalog (`files.json`), desired-placement rules, tombstones, generations, and per-job reconciliation records.
   - Each device publishes a manifest and polls for jobs.

The newer engine is the right design, and its safety core is good:
- Desired and actual placement are stored separately.
- Removal requires a SHA-256-proven alternate copy.
- Locations are dropped only on a strict `complete:true` manifest.
- Global delete persists its intent first and collects per-device acknowledgements.
- Transfers are staged with length and hash checks.

That foundation is worth keeping. **A rewrite is not justified.**

**Why it isn't reliable yet.** The newer engine is not actually the one in charge, and it has gaps in its planner.

1. **Both engines run at the same time on the same files.**
   - The desktop GUI starts the legacy loop one second after launch (`ui:817`).
   - The legacy engine on both desktop and Android treats every default-ON item as "unmanaged" and syncs it as well.
   - Result: double transfers, racing writers, local deletes being undone, and a "Synced" status that comes from the legacy loop's console output.
2. **The Catalog engine only runs while a UI is open.**
   - Desktop: closing the window, or starting at login with `--background`, runs only the legacy loop (`ui:359`, `cli:1849`).
   - Android: `autoSync` can never become true, so reconciliation only happens while the app is open.
3. **The server planner has holes that stop convergence.**
   - Once two copies agree, the planner never creates jobs for other desired devices that are still missing the file (C1, [PROBED]).
   - Any job that ended `failed`, `canceled` or `complete` is permanent, so a transient error or an OFF→ON toggle desynchronizes that device for good (C2, [PROBED]).
   - These two bugs alone explain most "it's in the catalog but never arrived" and "stuck Pending" reports.
4. **Several paths can lose data.**
   - A filename with invalid UTF-8 empties `files.json` (C6, [PROBED]).
   - Turning OFF two copies in quick succession can delete the last copy (C4, [PROBED]).
   - A non-recursive OFF on a folder deletes the web subtree (C5, [PROBED]).
   - New content created under a previously deleted name is silently `rmtree`'d (C3, [PROBED]).
   - API uploads truncate the live file before the new bytes arrive (C9).

**Verdict: targeted repairs, not a rewrite.**
- Remove the legacy engine.
- Run the Catalog engine headless on every device.
- Fix roughly ten specific server functions.
- Make the server the only authority for toggle state.
- Rebuild the Android presentation and background layers on top of its existing protocol code.

Everything after that is performance and polish.

---

## B. Architecture map

```
 Laptop (~/FreeCloud)              Raspberry Pi (one machine, two providers)          Android phone (SAF tree)
 ┌───────────────────────┐        ┌─────────────────────────────────────────┐        ┌───────────────────────┐
 │ freecloud_ui.py (Tk)  │        │ nginx + PHP-FPM  /var/www/.../FreeCloud │        │ MainActivity (UI +    │
 │  - 20 s os.walk scan  │ HTTPS  │  freecloud_api.php   (JSON API, 36 act.)│ HTTPS  │   reconciliation run) │
 │  - publish_manifest   │◄──────►│  freecloud_lifecycle.php (tombstones,   │◄──────►│ SyncService (legacy   │
 │  - poll/ack jobs      │        │     generations, atomic JSON)           │        │   SyncEngine, FGS)    │
 │  - legacy sync_once   │        │  freecloud.php (web UI), download,      │        │ AndroidReconciliation │
 │    child process (!)  │        │     preview, transfer endpoints         │        │ DocumentTreeSync(SAF) │
 │ freecloud_cli.py      │        │  freecloud_meta/*.json  ← AUTHORITATIVE │        └───────────────────────┘
 │  - legacy loop (bg)   │        │  freecloud_files/      = "Web Server"   │
 └───────────────────────┘        │                          provider       │
                                  │ freecloud_cli.py node daemon (systemd)  │
                                  │  - external drive = "Pi" provider       │
                                  │  - complete manifest every cycle        │
                                  └─────────────────────────────────────────┘
```

Per `tools/deploy_pi_update.sh` and `README_FreeCloud_Pi_Recovery.md`, the "Web Server" and the "Raspberry Pi" node appear to be the same physical host. They are two logical providers: web storage, and the node daemon's external drive.

**Authoritative state.** Everything is flat JSON under `freecloud_meta/`, guarded by one global `flock` on `catalog.lock`:

| File | Holds |
|---|---|
| `files.json` | The catalog. Per path: `type, size, mtime, hash, version, generation_id, placement_default, placement_source_device_id`, plus `locations[]` (actual copies, each with client-reported facts). |
| `desired_locations.json` | Explicit per-device ON/OFF rules (`path, desired, recursive`). |
| `sync_rules.json` | Legacy mode rules. Only `storage_only`, `sync` and `keep_offline` count as desired (`api:622`). |
| `devices.json` | Client-chosen `device_id`, type, `last_seen_at`. "Online" means seen within the last 180 s. |
| `tombstones.json`, `generations.json` | Global-delete state, and recreation scopes. |
| `accepted_versions.json` | Per-path baseline used for 3-way comparison. |
| `reconciliation/*.json` (+ `.bin`) | One record per job, plus staged bytes. |
| `transfers/*.json` | Browser→node downloads (these have leases). |

**Lifecycle stage by stage.**

| Stage | Owner | Implementation | Trigger | On failure |
|---|---|---|---|---|
| Detect | Desktop GUI only | `ui.run_local_manifest_scan` (2917) → `cli.local_manifest` (1199): a full `os.walk`, no inotify | Every 20 s, plus a 5 s "two identical signatures" debounce | One unreadable folder blocks the whole publish. The error goes to a hidden console. |
| | Android | `DocumentTreeSync.walk` (177): full SAF walk | App open, or every legacy-service cycle | Errors swallowed |
| | Pi node | `cli` node daemon (1673): full walk, plus hashes with `--hashes` | Every 10 s (setup saves 10) | The first failure re-raises and skips the cycle |
| Manifest | Each device | `publish_manifest` (`api:3158`): upsert locations; absence counts only if `complete===true` | After a detected change | Retried on the next scan |
| Default placement | Server | `newManifestPlacementDefault` (`api:734`), `recordNewWebPlacementDefaults` (`api:766`) | First catalog insert | Missing for web-UI uploads, moves and legacy rows (C7) |
| Desired calc | Server | `desiredLocationsForPath` (`api:664`): longest-prefix explicit rule > sync rule > `placement_default` | Every list or poll | — |
| Plan | Server | `planMultiProviderReconciliation` (`api:1649`) inside `poll_reconciliation` (`api:2260`), only when `check_only` is absent | A device polls | C1 and C2 holes. No leases. Runs entirely under the global lock. |
| Transfer | Devices + server | web→device: staged `.bin` at plan time. device→web/device: `upload_reconciliation` (length + SHA header) → `commit_reconciliation` | Job delivered on poll | Client acks `failed`, which is permanent (C2) |
| Install/verify | Device | Desktop: temp file, SHA check, `os.replace`, `utime`, ack. Android: temp → backup-rename → rename | — | Orphaned `.part` files are published as real files |
| Confirm actual | Server | The device's next manifest reports the path (`api:3436`) | — | — |
| UI status | Each client | Desktop: `folder_sync_state` (direct children only) **plus** legacy-loop stdout. Android: `roughlyEqual` (never true) | — | Misreports (Section C) |

---

## C. Issues

Severity: **Critical** means data loss or the core requirement is broken. **High** means frequent wrong behavior. **Medium** means real but bounded. **Low** means cosmetic or rare. Complexity: S < 1 day, M = 1–3 days, L > 3 days.

### C.1 Confirmed defects — Critical

| # | Finding | Evidence | Consequence | Fix | Cx |
|---|---|---|---|---|---|
| 1 | **Two sync engines on the same files.** Desktop GUI starts the legacy loop at every launch. The legacy engine (desktop and Android) skips only paths covered by explicit rules, and the default-ON `placement_default` is not a rule. | `ui:817` → `start_sync_if_needed` (4248). `cli:1275-1310`. Android `SyncEngine.kt:43` + `FreeCloudApiClient.kt:45-64` vs `api:689-703`. | Double uploads and downloads, non-atomic legacy writes racing atomic ones, laptop disk filling, local deletes undone (`cli:1348-1356`), legacy `.conflict-local` copies replicated everywhere. | Delete the legacy engine as a sync mechanism on both clients (keep the CLI subcommand only as a manual tool, if at all). | M |
| 2 | **Catalog sync never runs headless on the desktop.** `--background`, autostart and "Run in background" all launch `freecloud_cli.py` with no arguments, which runs `legacy_sync_main`. | `ui:359, 4192, 4347`, `ui:1203-1213`, `cli:1849`. | With the window closed, the laptop never publishes, never installs and never processes deletes. It drops "Offline" after 180 s. | Add a `freecloud_cli.py desktop run` daemon that contains the scan→publish→poll loop now living in the Tk worker threads. GUI, tray and autostart all start or attach to that one process. The GUI becomes a viewer. | M |
| 3 | **Android sync never runs in the background.** `autoSync` defaults to false and nothing ever sets it true. `saveSetup` resets it. | `Models.kt:11`, `ConfigStore.kt:34`, `MainActivity.kt:290-299`. | The phone neither sends nor receives unless the app is open. | WorkManager periodic plus expedited workers (Section F). | M |
| 4 | **C1 — Planner never fills in missing desired devices once two or more copies agree. [PROBED]** | `api:1672-1701`: the all-agree branch only closes jobs for devices that already have the file, then returns `[]`. | New devices never get existing content. A device toggled back ON, or one that missed the first wave, never gets the file. "Catalog visible but bytes never arrive." | In that branch, plan a job for every desired device not in `$actual`, preferring `web_server` as source. | S |
| 5 | **C2 — Terminal job states are permanent. [PROBED]** Job IDs are deterministic, and `plan*Job` returns any existing record regardless of status. | `api:1438-1441, 1503-1506, 1630-1633`. A `staged` job whose commit is lost is never re-offered (2408). | One "No space left" or network blip, or an OFF→ON toggle, and that device never receives the file. | Make `failed/canceled/complete/conflict` re-plannable when facts still require a transfer. Add `attempts` and `next_attempt_at` with exponential backoff, and lease expiry for `staged/ready/queued_source`. | M |
| 6 | **C6 — Invalid-UTF-8 filename empties `files.json`. [PROBED]** `json_encode` returning `false` is written as an empty string and renamed over the catalog. | `api:76` (`saveJsonFile`), `lifecycle:258`. Combined with `loadJsonMap` returning `[]` on decode failure (`api:56`). | Every placement and location record is lost. The same path can wipe `tombstones.json`, which brings deleted items back. Likely trigger: a Latin-1 filename on the Pi drive. | `JSON_THROW_ON_ERROR`, refuse to write, keep a `.bak`. Have `loadJsonMap` fail closed (503) on corrupt files. Reject non-UTF-8 paths in `normalizeRelativePath`. | S |
| 7 | **C4 — Toggle OFF can delete the last copy. [PROBED]** Copies that are themselves pending removal still count as the "alternate". | `safeRemovalFacts`, `api:1045-1060`. | Desktop OFF then Web OFF in quick succession leaves zero copies. | Only count alternates that are desired ON, not stale, and not being removed. Re-check under the lock at execution time. | M |
| 8 | **C3 / new content under a deleted name is deleted. [PROBED]** Tombstones never expire and are matched by path only. Clients delete with no age or hash check. | `api:3213-3226, 3330-3337`. `lifecycle:17-31`. `cli:783-791` (`shutil.rmtree`). `AndroidReconciliation.kt:16-21`. | Reusing a deleted folder name destroys new work within about 30 s. The recreate dialog is desktop-only and appears too late. | Server: content whose mtime is later than `deleted_at`, or whose hash differs from the pre-delete facts, starts a new generation automatically. Clients: refuse a `queued_delete` for items newer than the tombstone's `deleted_at`. | M |
| 9 | **A local delete or rename is not a global delete.** The complete manifest just drops the location, and default-ON re-installs it. | `api:3297-3315` ("not a global delete"). Android has no `global_delete` call at all. | The user cannot delete from the file manager or from Android. Renames produce duplicates. | Decision needed (see D.4). Recommended: when a device that previously reported a path stops reporting it, and its last-reported facts equal the catalog facts, treat it as a global delete. Rename = delete + add, and the server can pair them by hash. | M |

### C.2 Confirmed defects — High

| # | Finding | Evidence | Fix | Cx |
|---|---|---|---|---|
| 10 | **C5 — Non-recursive OFF on a folder deletes the whole web subtree unchecked. [PROBED]** | `api:1053-1056, 1086-1097` | Force `recursive=true` for directories, or delete only empty folders | S |
| 11 | **C9 — API `upload`/`save` truncate the live file first.** No length or hash check, and mtime is set even on a short write. | `api:3530-3537, 3570` | Temp file + length/hash check + rename. Return 507 on a short write. | S |
| 12 | **C10 — `upload/save/mkdir/move/delete` write `files.json` outside the lock.** | `api:1932-1936` | Take the lock inside the save helper itself | S |
| 13 | **C7 — Web-UI uploads, API moves and legacy rows never get the "all" default.** They show OFF everywhere and stay on the server only. [PROBED] | `freecloud.php:1367`, `movePath api:1816` | Stamp defaults there. Add lazy inheritance and a one-time migration (Section E). | S |
| 14 | **C8 — `recreate_path` writes explicit OFF for every non-selected device.** | `api:3093-3099` | Stamp `placement_default=all` instead | S |
| 15 | **C11 — Concurrent offline edits create a permanent `provider_conflict`** with no resolution path, instead of "newest wins". | `api:1713-1757` | Section D.3 | M |
| 16 | **Upload body cap.** The documented nginx config sets `client_max_body_size 10M`. Files over 10 MB then 413 in `upload_reconciliation`, the job stays `queued_source`, and the client re-uploads on every poll. | `README_FreeCloud_Pi_Recovery.md:103,135` (production value not re-checked) | Chunked or resumable upload, or raise the limit. Return a distinct "too large" status. | M |
| 17 | **Temp/part files are published as real content.** Applies to `.freecloud-part-*`, `.crdownload`, `.part`, `.swp`, `~$*`, and Android `.part`/`.backup`. | `cli:1184-1196`, `DocumentTreeSync.kt:177-201` | Shared exclusion list in the manifest builders. Startup cleanup of orphaned parts. | S |
| 18 | **Whole-file-in-RAM transfers** on the Android upload/download/reconcile paths and the desktop legacy upload and UI download. | `DocumentTreeSync.kt:49-60`, `FreeCloudApiClient.kt:195-198, 325-334`, `cli:250`, `ui:3900` | Streaming with `setFixedLengthStreamingMode` / OkHttp, incremental hashing | M |
| 19 | **Android ms vs s mtimes.** `walk` stores milliseconds. Legacy upload sends ms to `touch()`. `roughlyEqual` compares local ms with remote seconds, so "Synced" is unreachable. | `DocumentTreeSync.kt:194`, `SyncEngine.kt:95,123,152`, `MainActivity.kt:788,802` | Normalize to seconds at `walk` | S |
| 20 | **Status lies.** "Synced" is set from legacy-loop stdout (`ui:4238`). `folder_sync_state` checks only direct children (`ui:2861`). The idle 30 s poll is `check_only` (`ui:3041`), so nothing is planned while idle. | as cited | Status comes from a server summary (Section D.5) | M |
| 21 | **Toggles render OFF for items the server says ON.** Local-only rows lack `desired_locations` and show "Removal blocked". There is also a placeholder row after navigation, a fallback to legacy `list`, and stale cache rendered as live. | `ui:2511-2526, 2429-2435, 2815, 2155` | Section E | M |
| 22 | **Desktop drag-move renames only the web copy.** Catalog and devices are not updated. | `ui:3616-3643` → `api:1816` | Catalog-level move (Section D) | M |
| 23 | **Android 15 FGS violations.** The endless `dataSync` loop doesn't implement `onTimeout`, so it crashes at 6 h. BOOT_COMPLETED→FGS start is disallowed at targetSdk 35. | `SyncService.kt:49-77`, `BootReceiver.kt:18` | Replace with WorkManager | M |
| 24 | **Planning cost under the global lock.** Each full poll walks the web tree, globs every job record per entry (O(entries × records)), and copies plus hashes staged bytes. Records are never garbage-collected. Heartbeats wait on the same lock. | `api:1686, 1777, 1460-1468` | Section D.6 | L |

### C.3 Confirmed defects — Medium

- **One unreadable subfolder blocks all desktop publishing** (`cli:1204-1205`).
- **No disk-space check and no backoff.**
  - Desktop: on ENOSPC the same file is re-downloaded on every poll.
  - Server: no `disk_free_space` check. Each destination gets its own `.bin` staging copy, and failed jobs keep theirs forever (C13).
- **`move` and `delete` API actions bypass the catalog, tombstones and last-copy checks** (`api:3466, 3601`, `freecloud.php:1290`).
- **Corrupt metadata is read as empty** (`loadJsonMap`, `api:56`), so a corrupt tombstones file means resurrection.
- **Authorization.** One shared password; any client can act as any `device_id` or register `web_server` (`api:1946`). The API is fully open if `password_hash` is empty. No API rate limit.
- **Stored XSS via preview.** HTML and SVG are served inline on the same origin (`freecloud_preview.php:158-170`). A synced `.html` file could read the CSRF token and POST `global_delete`.
- **Desktop concurrency bugs.**
  - `save_json` uses a fixed `.tmp` name from several threads (`cli:513`).
  - Network calls run on the Tk main thread (`ui:4124, 3716, 1045…`), freezing the UI for up to 120–300 s.
- **Android reliability.**
  - Concurrent reconciliation (activity and service, rotation, every status broadcast). Stop/start race in `SyncService`.
  - Main-thread SAF and Keystore I/O.
  - Full SAF walk per reconciliation job.
  - A failed refresh hides its error and leaves "Refreshing…" on screen forever.
  - Local files can't be opened when the server is unreachable (`MainActivity.kt:401, 435-453, 484`).
- **Swallowed errors.**
  - Eight `except Exception: pass` blocks. The `queued_source` failure is discarded silently (`cli:872`).
  - Errors go to a hidden console. Logs grow without limit.

### C.4 Confirmed — Low

- The second toggle click sends the value computed from the server state, not the displayed state, and optimistic overrides can stick (`ui:2448, 2326`).
- Edits that keep the same size within 2 s go undetected.
- Tombstones targeting removed devices never complete.
- `devices.online` is never set false.
- Android: hardcoded `https://kylebenzle.com` default, no insets handling on Android 15, the Back handler is disabled permanently after the first root exit, unbounded log TextView.
- No Range support in API `download`, so seeking deep into a remote audiobook streams from byte 0.

### C.5 Potential risks (plausible, not reproduced)

- **The legacy loop deletes local files** when the web copy disappears without a rule (`cli:1336-1342`). This goes away once the legacy loop is removed.
- **GUI and desktop `node daemon` sharing one `device_id`** would publish conflicting complete manifests, so location claims flip back and forth.
- **Password leak through redirects.**
  - Desktop urllib forwards the header on a redirect, and `remember_final_url` then saves the new URL permanently.
  - On Android, POST redirects become GET.
- **Pi clock before NTP sync** (no RTC) produces wrong mtimes that win or lose the newest-wins comparison incorrectly.
- **Python surrogate-escaped filenames make PHP reject the whole manifest JSON**, so the Pi silently publishes 0 entries.
- **Case-insensitive client filesystems** (SAF, future Windows) collide on `Foo`/`foo`.
- **`browse_only`/`download_only` modes are ignored by the `all` default.**
- **No fsync on the Pi's SD card**, combined with corrupt-reads-as-empty.
- **Android Gradle wrapper 9.0.0 with AGP 8.5.2 / Kotlin 1.9.24** may not build.

### C.6 Unverified

- Production PHP `max_execution_time` and FPM `request_terminate_timeout` versus the long planning polls.
- The actual live nginx body limit.
- How the phone's Private DNS is set, and how the ISP router versus the TP-Link handle hairpin NAT.
- Whether Android builds with the current wrapper.
- How each client renders the C1 state and terminal `failed` jobs. The reports suggest they render as "up to date", but this was not observed on a device.

### C.7 Status of the historical problems (section 4 of your brief)

| Problem | Current status |
|---|---|
| A. Files not reaching other devices | **Still present, with new causes:** no headless catalog sync (Critical 2), Android foreground-only (Critical 3), C1, C2, the 10 MB body cap, web-UI uploads with no default (C7). The original "local display without publication" cause is improved: the stability gate exists and publication is complete, but one unreadable subfolder still blocks it. |
| B. API/helper mismatch | **The same failure is possible again.** `freecloud_lifecycle.php` is untracked in git. The README and the restore list omit it. The deploy check covers 3 of 11 helpers. There is no version handshake. All 14 helper functions currently exist. |
| C. Toggles showing OFF | **Still present** through local-only rows, the placeholder row, the legacy-list fallback, stale cache, and C7 for web uploads and legacy items. |
| D. Catalog vs bytes | **Partly addressed.** The desktop shows Pending, Synced and Removing states. But status checks only direct children, and the C1 state (desired, missing, no job) carries no marker at all. |
| E. Stuck Pending | **Explained by** C1, C2, staged jobs with no lease, the idle `check_only` poll, and the 10 MB cap. |
| F. "Up to date" while unfinished | **Still present** (legacy-stdout status, direct-children-only check). |
| G. Offline catch-up | **Not met** for the closed desktop GUI or for Android. On the server, C2 makes failed jobs permanent. |
| H. Android on home Wi-Fi | **Primarily external.** `README_FreeCloud_Pi_Recovery.md:163-176` documents hairpin-NAT failure, worked around with `/etc/hosts` on the laptop, which a phone cannot do. There is no AAAA record, so IPv6 is ruled out. The app makes it worse: 120 s connect timeout, no LAN fallback, refresh errors hidden, local files unopenable offline. |
| I. Delete / resurrection | **Resurrection protection works**, bluntly. **Recreation is broken** (C3, C8). Local deletes are not honored (Critical 9). |
| J. Storage limits | **A full device fails only its own job**, which is good isolation, but per C2 it never retries. **The Pi/web disk is a single point of failure**: staging, storage and metadata share one disk, and a failed metadata write can then wipe metadata via C6. |

---

## D. Sync reliability improvements (prioritized)

### D.1 One engine, always running

1. **Remove the legacy engine from automatic paths.**
   - Desktop: drop `start_sync_if_needed`, the `sync.pid` background loop and "Full Sync Now".
   - Android: drop `SyncEngine` and `SyncStateStore` from `SyncService` and `MainActivity`.
   - The server's legacy `upload/manifest/list` actions can remain for the web UI.
2. **Headless desktop daemon.** Move the scan → publish → poll → process loop out of `FreeCloudUi` into `cli`.
   - Run it as a systemd user service, with the same shape as the existing node autostart (`cli:1712`).
   - Guard it with a PID lock that uses `fcntl.flock`, not check-then-write.
   - The GUI reads status from it, through a small local status JSON file or a socket, and sends "refresh now" and toggle actions.
   - This also removes the GUI-vs-node-daemon `device_id` collision.
3. **Detection.**
   - Use inotify via `watchdog` or `inotify_simple` for immediate change events, with a debounced per-path publish.
   - Keep a fallback full scan every 5–10 minutes instead of every 20 s.
   - Per-file stability rule: skip files whose mtime is less than 10 s old, and files matching the temp-name list. Use incremental manifests (`complete:false`) for event batches, and a `complete:true` manifest only for the periodic full scan.
   - Make an unreadable subfolder a per-path warning, not a whole-tree failure. For that subtree, send a non-complete manifest.

### D.2 Convergent planner (server)

1. **C1:** plan every desired-but-missing device.
2. **C2:** re-plannable terminal states, with attempts, backoff (1 m → 5 m → 30 m → 2 h, capped) and a `last_error` field.
3. **Leases:** `ready`, `staged` and `queued_source` jobs expire after a lease, e.g. 15 min without progress, and go back to planning.
4. **Idle planning:**
   - Every device's normal poll should plan for that device only (cheap), rather than either planning nothing (`check_only`) or planning the entire catalog.
   - Implement `planForDevice($deviceId)`: iterate only over entries that are desired for that device and not confirmed in its actual locations.
5. **Byte limits:** chunked upload (e.g. 8 MB parts with per-part offsets) for `upload_reconciliation`, or raise the nginx and PHP limits to cover the largest expected file.
6. **Capacity:** a `disk_free_space` check before staging. One shared staged blob per `(path, size, mtime, hash)` instead of one per destination. Devices report free space in their heartbeat, and the planner skips (with a visible "Not enough space on X") rather than failing.

### D.3 Version rule: "most recently modified wins"

The current state:
- Comparisons use client mtime ±2 s, plus the hash when present.
- If two providers changed since the baseline, the result is a permanent conflict (C11).
- If there is no baseline, the newer mtime wins when it is more than 2 s newer, otherwise conflict.

**Smallest deterministic implementation of your rule (no conflict copies):**
1. When several providers changed since the baseline, pick the one with the **largest mtime**.
   - Exact tie (within 2 s): prefer the copy with a hash, then the earliest server receipt time (`updated_at` of the location), then the lexically smallest `device_id`.
   - The winner becomes the new baseline and is planned to every desired device. Losing copies are overwritten by normal jobs.
2. **Clock skew is the unavoidable limitation.** A device with a wrong clock can win or lose incorrectly. Smallest mitigations:
   - Clients send their wall-clock time in every heartbeat. The server records `clock_offset = client_now − server_now`, normalizes reported mtimes by subtracting it, and flags devices with more than 60 s of skew in the Devices view.
   - Clamp mtimes in the future to receipt time.
   - Start the Pi node only after time sync (`After=time-sync.target`, `systemd-time-wait-sync`).
3. **Two devices edit the same file offline:** the later edit wins and the other edit is lost. That is what your rule asks for. It is a real trade-off and worth stating once in the UI or docs.
   - The existing desktop `.freecloud-part`/backup handling stays.
   - The legacy `.conflict-local` copies disappear once the legacy loop is removed. Those are conflict copies, not backups, so this is consistent with your instruction.
4. **Android SAF cannot set mtime.**
   - The logical-mtime map is the right workaround. Prune it on delete and rename, and reset it when the sync folder changes.
   - Android must publish the *logical* mtime of installed files, or it will look newer than everyone else.

### D.4 Deletion

1. **C3 fix (server + client):**
   - The tombstone keeps the pre-delete facts. A later manifest entry under a tombstone is re-targeted for deletion **only** if its facts match those pre-delete facts, or its mtime is ≤ `deleted_at`.
   - Anything newer automatically opens a new generation, with default placement `all`.
   - Clients independently refuse a `queued_delete` when the local item's mtime is later than the job's `deleted_at`. This is defense in depth.
2. **Local deletes. Decision for you.** A normal cloud drive (Dropbox, Drive) treats deleting a file in the synced folder as a global delete. I recommend the same, with these safeguards:
   - Only when the device previously reported the item.
   - Only when its last-reported facts match the catalog's current facts, so a stale device cannot delete newer content elsewhere.
   - Rename detection by hash (delete + add with the same hash within one manifest becomes a server-side move).
   - The existing tombstone and acknowledgement machinery does the rest.

   Turning a device OFF remains different and is never a delete.
3. **C4/C5 last-copy fixes:** see Section C. Also re-validate under the lock when the device acks `removed`.
4. **Route API `delete` and web-UI delete through `global_delete`.** Implement `move` as a catalog rename: move the catalog row, desired rules, accepted version and tombstone check, then emit move jobs or let devices re-pair by hash.

### D.5 Truthful status

- **New server endpoint `sync_status?device_id=`.** It returns:
  - counts of pending installs and uploads for this device, failures with reasons, conflicts,
  - whether this device's last manifest was accepted, plus its `last_publish_at`,
  - the server's view of every device's online state and backlog.
- Desktop and Android show "Up to date" only when this device has no local unpublished changes **and** the server reports zero pending or failed items for it.
- Pending items always carry a reason: "Waiting for Pi (offline since 14:02)", "Retrying in 5 min: No space left on device", "File larger than server limit".

### D.6 Performance

- Index reconciliation records by path, in one file or later SQLite, instead of globbing.
- Garbage-collect terminal records older than N days.
- Stage bytes lazily when the destination first requests them, not at planning time.
- Take `heartbeat` out of the catalog lock.
- Pi node: interval 60 s, no `--hashes` on every cycle (hash only files whose size or mtime changed, with a cache).
- **Consider moving `freecloud_meta` from JSON files to SQLite (WAL mode).** This is a real improvement for atomicity, fsync and lock granularity. It is Phase 4 because the JSON fixes (C6, C15, backups) make the current store safe enough.

---

## E. Default toggle and placement model

**One authoritative interpretation, computed only on the server.** For entry *P* and device *D*, effective desired placement is the first match below:

1. An explicit rule for *D* on *P* (`desired_locations.json`, `recursive:false` or `true`).
2. The longest ancestor of *P* with a `recursive:true` explicit rule for *D*. Explicit OFF and ON both count, and the most specific wins (already implemented in `api:664`).
3. A legacy `sync_rules` entry for *D* (`storage_only`, `sync`, `keep_offline` = ON; `browse_only` = OFF; P1 risk).
4. *P*'s own `placement_default`. If absent, inherit the nearest ancestor's `placement_default`.
5. If no row anywhere up the chain has one: **`all`**. This is the global default for ordinary FreeCloud content.

`source_only` is used only for content a device publishes **from outside its FreeCloud folder** (node rule roots). That is how external selective storage keeps its behavior. Reconciliation-incapable device types stay excluded, as now.

**Changes needed to make this true everywhere:**
- Step 5 as a lazy fallback, plus a one-time migration that sets `placement_default=all` on ordinary legacy rows. Keep the existing `storage_only`/`adopt` rules: explicit rules still win.
- Stamp defaults in the web-UI upload, `save_file`, API `move` and `recreate_path` (C7, C8).
- Newly registered devices automatically become desired for `all` content (already true by policy). C1 makes it actually happen.
- Fix the test harness so it stops stripping `placement_default` after every publish (`tests/test_freecloud_server_api.py:72-87`). Most tests currently check a state production never produces.

**What clients receive.** Every listing returns, per entry and per provider, one object:

```json
{"device_id":"pi-…","desired":true,"desired_source":"inherited:/Photos" | "explicit" | "default",
 "actual":"stored" | "missing" | "stale" | "removing" | "copying",
 "pending":{"status":"retrying","reason":"No space left on device","next_attempt_at":…} | null}
```

**Client rules:**
- Render the toggle from `desired` only, and the status text from `actual` plus `pending`. Never compute either locally.
- A local item not yet in the catalog shows **"Not uploaded yet"**, with toggles disabled (not OFF).
- While a toggle request is in flight, show a spinner on that switch only. Afterwards, render the server's returned state, which `set_desired_location` should return in full. No lingering optimistic overrides.
- If the cache is older than one successful refresh, show the toggles visibly disabled with "Last updated 3 min ago".

---

## F. Android redesign proposal

### F.1 Answers to your twelve questions

1. **Unnecessarily complicated?** Yes, in the wrong places. A 981-line god Activity runs setup, browser, player, log, share and its own reconciliation. Two sync engines, raw threads, no lifecycle components.
2. **Redundant screens or controls?** Start/Stop Sync, the always-visible Server/Local rows, the hidden Activity log, the setup form reused as "Edit Setup", the inline player panel, and a fixed-height RecyclerView nested in a ScrollView.
3. **Simplify without losing function?** Yes. Removing the legacy engine deletes about 400 lines and most of the races.
4. **Networking reliable?** No.
   - 120 s connect and read timeouts, no retry, no LAN fallback.
   - A fresh client per call, whole-body buffering.
   - Refresh errors hidden.
5. **Background sync correct for current Android?** No. It never runs by default. When it does run, it violates the Android 15 `dataSync` 6-hour limit and the boot-start restriction.
6. **Detect local changes?** Partly. SAF has no reliable recursive file observer.
   - Best effort: a periodic WorkManager scan (15 min minimum), a scan on app open, and a `ContentObserver` on the tree URI (provider-dependent).
   - A cheap pre-check compares the count and max mtime of top-level children before doing a full walk.
   - Uploads made *through the app* publish immediately.
7. **Permissions and scoped storage?** Correct. SAF tree with a persisted grant, no `MANAGE_EXTERNAL_STORAGE`. Changing folder should reset state and release the old grant.
8. **Availability vs downloading?** No. The status pill mixes actions ("Play", "Edit") with states, and "Synced" is unreachable because of the ms/s bug.
9. **Toggles intuitive and reliable?** They don't exist. Placement data is parsed and cached but never shown.
10. **Keep the UI or redesign?** **Redesign the presentation layer and the background layer.** There is almost no current UI matching your spec to preserve.
11. **Compose + Material 3?** Yes, worth it here.
    - The UI is being rebuilt anyway.
    - ViewModel + StateFlow fixes the rotation and threading bugs structurally.
    - M3 gives the bottom sheets, switches and navigation bar you want for free.
12. **Incremental, no protocol change?** Yes. Every step below uses the existing API, plus the new `sync_status` endpoint and the richer placement objects from Section E.

### F.2 What to keep, simplify, rebuild

- **Keep and refactor:**
  - `FreeCloudApiClient` (move to OkHttp, streaming bodies).
  - `AndroidReconciliation` and `ReconciliationSafety` (fact and SHA checks before delete or overwrite are good).
  - `DocumentTreeSync`'s install path and logical-mtime map.
  - `ConfigStore` (Keystore AES-GCM password storage).
  - `DeviceIdentity`, `FolderMetadataCache` (as an offline cache).
- **Delete:** `SyncEngine`, `SyncStateStore`, `SyncService`, `BootReceiver`, `SyncRuntime`, the legacy `upload/delete/manifest` client calls, and `MainActivity` once the new UI replaces it.
- **Rebuild:** UI (Compose/M3), background work (WorkManager), media (Media3 `MediaSessionService` + ExoPlayer, giving background audiobook playback and lock-screen controls).

### F.3 Navigation

A bottom navigation bar with three tabs, plus Settings behind a gear in the top bar. Four tabs would give Settings equal weight with daily-use screens.

```
┌─────────────────────────────────────┐
│ FreeCloud            🔍   ⚙         │  ← search, settings
│ Home › Photos › 2026                │  ← breadcrumbs (tap to jump)
│ ● Up to date                        │  ← sync-health chip (tap → Activity)
├─────────────────────────────────────┤
│ 📁 Trips                     12 items│
│ 🖼 beach.jpg      2.1 MB · Oct 3  ✓ │  ✓ on this phone
│ 🎬 clip.mp4       840 MB · Oct 2  ☁ │  ☁ in cloud only
│ 📄 notes.txt      3 KB · today   ↑ │  ↑ uploading
│ 📄 draft.docx     40 KB · today  ⏱ │  ⏱ queued for this phone
│ 📄 big.iso        4 GB           ⚠ │  ⚠ failed – tap for reason
│                                 [+] │  ← FAB: Upload · New folder
├─────────────────────────────────────┤
│   Files      Activity     Devices   │
└─────────────────────────────────────┘
```

**Files screen:**
- Breadcrumbs, search (filters the cached folder instantly, with an optional server-wide search), sort (name/date/size), pull-to-refresh.
- One status icon per row, computed from the server's per-device state for *this phone*.
- Opens instantly from cache. When the server is unreachable, a banner reads "Can't reach server on this network · Retry", and local copies stay openable.
- Audio and video play in a mini-player bar above the navigation bar, backed by MediaSession.

**Item action sheet** (long-press or ⋮):
- Open / Play · Keep on this phone (desired ON for this device) · Share · Rename · Move · Delete everywhere (confirmation lists which devices will remove it) · Info & storage.

**Info & storage sheet (placement toggles):**

```
beach.jpg · 2.1 MB · modified Oct 3, 14:02
Stored on
  This phone   [●  ON]  ✓ Stored
  Laptop       [●  ON]  ✓ Stored
  Raspberry Pi [●  ON]  ⏱ Waiting – Pi offline since 09:12
  Web server   [○ OFF]  Removing… (verified copy on Laptop)
  ON because: default for new files       (or "inherited from /Photos")
```

- For a folder: "Applies to everything inside", with a count of items that have their own setting.
- Switching OFF never says "delete". It says "Remove from Raspberry Pi (other copies stay)". If OFF would leave no copies, the switch is disabled and explained.

**Activity tab:**
- Groups: In progress (progress bars), Waiting (reason + device), Failed (reason + Retry), Recently completed (last 24 h).
- Replaces the raw log. A "Copy diagnostics" button exports the technical log.

**Devices tab:**
- Each device: name, type, online or last seen, free space, backlog count, clock-skew warning. Rename, remove.
- "This phone" also shows its folder, last sync and battery-optimization status.

**Settings:**
- *Server:* URL, password, optional "Home network address" (LAN fallback, see F.5).
- *This phone:* device name, sync folder (changing it resets state cleanly), background sync (**default ON**), Wi-Fi only, charging only.
- *Diagnostics:* connection test showing DNS result, connect time and TLS status; export logs.
- *About:* version, server protocol version.

**Remove or combine:** Start/Stop Sync, the Server/Local rows, the setup form as an edit screen, the inline player, the nested scroll, the hidden log. The text editor becomes "Open with FreeCloud editor" in the action sheet. Drop the hardcoded domain default.

### F.4 Technical architecture

- Single Activity, Compose Navigation, `ViewModel` + `StateFlow` per screen. A `Repository` sits over `ApiClient` + `FolderMetadataCache`.
- Coroutines, with one `Mutex` (or a WorkManager unique work name) around reconciliation, so it can never run twice.
- `SyncWorker` (`CoroutineWorker`):
  - Periodic every 15 min with a network constraint.
  - Expedited one-off on app open, on pull-to-refresh and after an in-app upload.
  - Uses `setForeground(dataSync)` only while a transfer is actually running.
  - WorkManager survives reboot, so `BootReceiver` is unnecessary.
- **Streaming everywhere:** OkHttp `RequestBody` from the SAF `InputStream` with a known length, and downloads to a temp document while hashing incrementally.
- **Timeouts:** connect 10 s, read 60 s. Retry idempotent GETs once.
- Exclude `.part`/`.backup`/temp names from `walk`. Normalize mtimes to seconds.
- Upgrade the toolchain to a matching AGP / Kotlin 2.x / Gradle combination before starting.

### F.5 Home Wi-Fi (H)

1. **Network fix (best, also fixes the browser):** split-horizon DNS.
   - Run dnsmasq or Pi-hole on the Pi, resolving `kylebenzle.com → 192.168.1.114`, and hand it out via TP-Link DHCP.
   - Set the phone's Private DNS to *Automatic* or *Off*. A named Private DNS provider bypasses LAN DNS.
2. **App fix:** OkHttp custom `Dns`.
   - Try the public resolution with a short connect timeout. On failure, use the configured home address **while keeping SNI/Host = kylebenzle.com**, so the Let's Encrypt certificate still validates.
   - Never fall back to http and never disable hostname verification.
   - ExoPlayer with `OkHttpDataSource` inherits the same fallback.
3. **UX:** an explicit "unreachable on this network" banner instead of an endless "Refreshing…".

### F.6 Rewrite or not

**Not a full rewrite.** It is a new presentation layer and background layer over the existing protocol and safety code, delivered screen by screen behind a flag. Estimated 3–5 weeks of focused work for the full Phase 3.

---

## G. Testing strategy

**Why tests passed while sync was broken:**
- Server tests run PHP via `php -r` with `$_POST` filled in by hand, so the JSON body path real clients use never runs.
- The harness rewrites the catalog after each publish, stripping `placement_default`.
- Byte-transfer endpoints are untested.
- Client tests use hand-written fakes that can drift from the server.
- One test asserts the legacy loop auto-starts. Another asserts the unconditional `rmtree` is correct.

**1. E2E harness** (`tests/e2e/test_sync_e2e.py`, opt-in via `FREECLOUD_E2E=1`, about 2 days to build). Your 12-step scenario becomes executable:

- **Setup:**
  - Copy the whole `server/` into a temp dir, start `php -S 127.0.0.1:<port>`, run `setup` over HTTP.
  - Fail on any non-200 response or any PHP warning on stderr.
- **Devices:**
  - `desktop`, `node` and `android-sim` temp roots, each its own HOME and config, each running the *real* CLI as a subprocess (`desktop run --once`, `node run --once`), so the JSON-over-HTTP path is real.
  - Android is simulated with raw HTTP calls that copy `FreeCloudApiClient` request shapes. Better: a JVM test that points the real `FreeCloudApiClient` at the same server.
- **Steps, each asserted:**
  1. Create a file on the desktop.
  2. `metadata_manifest` shows a desktop location.
  3. Desired placement is ON for every device and the web server. The test must **not** rewrite the catalog.
  4. Cycle until quiescent. Bytes exist on node and android-sim.
  5. SHA-256 is equal everywhere.
  6. Modify the file. The new SHA appears everywhere and the mtime is preserved.
  7. Stop cycling the node, or point it at a closed port.
  8. Modify or create another file. The jobs are pending with a reason, not permanently failed.
  9. Resume the node.
  10. Everything converges with no manual action.
  11. Set the node OFF for one file. The node copy is removed only after hash proof; the others are untouched.
  12. Global delete. Removed everywhere, tombstone retained, a stale node manifest does not resurrect it, and **a new file created at the same name later survives** (the C3 regression).
- **Additional cases:**
  - Truncated or corrupted transfer is never installed.
  - Injected ENOSPC on one device leaves the others syncing and retries later.
  - A 50 MB file (catches body limits).
  - A non-UTF-8 filename does not empty the catalog.
  - Two offline edits: the newer mtime wins.
  - OFF→ON re-delivers the file.
  - A new device receives all existing content.

**2. Regression unit tests** for each confirmed defect in Section C: C1–C11, ms/s mtimes, temp-file exclusion, toggle rendering of local-only rows, status with nested pending items. Each fix PR adds its test first.

**3. Fix the existing harness:**
- Send real JSON (via `php -S`).
- Delete the post-publish rewrite (lines 72-87).
- Load every entry file in each test.
- Delete the tests that assert legacy auto-start and unconditional `rmtree`.

**4. Contract tests:** record real job JSON from the E2E run and validate the CLI and Android fake fixtures against it.

**5. Android:**
- JVM tests for `AndroidReconciliation` behind interfaces (fake API, fake document tree).
- One instrumented test for SAF install and rollback.
- WorkManager `TestDriver` tests for scheduling.

**6. Production canary (post-deploy):** a throwaway `/.freecloud-canary/<timestamp>.txt` round trip. Upload, observe it on the Pi node, compare SHA, global delete, confirm the tombstone. Kept clear of user folders.

---

## H. Deployment strategy

1. **Commit everything load-bearing now.**
   - `server/freecloud_lifecycle.php`, `tools/deploy_pi_update.sh`, both server test files, the four untracked Android sources and two tests.
   - Add `freecloud_lifecycle.php` to the README file list and to `restoration/web-server-restore-files.txt`.
   - **Before committing `images/`, move out `Gmail - RE_ Motion for Continuance.pdf`.** Both deb and snap currently package that whole directory.
   - Keep `README_FreeCloud_Pi_Recovery.md` and `restoration/` out of git, since they contain your public IP and personal file names. Add them to `.gitignore`.
2. **Protocol versioning:**
   - Define `FC_PROTOCOL_VERSION` and `FC_LIFECYCLE_VERSION` in `freecloud_lifecycle.php`.
   - `freecloud_api.php` checks `FC_LIFECYCLE_VERSION` at the top and returns a clear 503 ("server files out of sync: lifecycle vN, need vM") instead of a fatal 500.
   - `ping` returns `protocol_version`, `min_client_protocol`, `bundle_version` and `git_commit`.
   - Clients send `X-FreeCloud-Protocol`. The server returns 426 to clients that are too old, and clients refuse to sync (with a visible message) against a server that is too old.
3. **Build a bundle from a clean commit** (`tools/build_bundle.sh`):
   - Refuse a dirty `server/` or CLI.
   - Emit `manifest.json` = `{bundle_version, git_commit, protocol_version, files:{name: sha256}}`. The deploy script reads its hashes from this file instead of hand-edited constants.
4. **Preflight on the Pi in a staging directory:**
   - `php -l` all files.
   - Include each entry file in an output buffer with stubbed request variables to prove they load together.
   - Collect every `fc_*(` call by grep and assert `function_exists` for each, instead of the current hand-listed 3.
   - Run the E2E suite against the staged copy.
5. **Atomic switch:**
   - Releases in `FreeCloud-releases/<stamp>/` containing code only. Data (`freecloud_meta`, `freecloud_files`, `freecloud_sessions`, `config.json`) lives in a shared data directory referenced by config.
   - Switch with `ln -sfn` + `mv -T`, then reload php-fpm to clear opcache. This removes the current file-by-file mixed-version window.
6. **Post-deploy smoke test** (automatic, rolls back on failure):
   - `ping` version matches the manifest.
   - `metadata_list`, `list_devices` and `freecloud.php` return 200.
   - The canary round trip passes.
   - No new fatals in the php-fpm log.
   - The node service restarts and its heartbeat appears.
7. **Rollback:**
   - Point the symlink back and reload FPM.
   - Metadata migrations must be idempotent and recorded in the manifest, with `freecloud_meta` snapshotted before any migration and restored on rollback. The current script backs it up but never restores it.
8. **Coordinated releases:** desktop (deb/snap) and Android carry the protocol version they speak in their about screen and release notes. Ship the server first; it stays backward compatible for one protocol version.

---

## I. Prioritized roadmap

Effort: person-days for one developer familiar with the code. Risk = the chance the change itself causes a regression.

### Phase 1 — Stability (≈ 2–3 weeks)

| # | Task | Effort | Risk | Depends | Verify |
|---|---|---|---|---|---|
| 1.0 | Commit untracked load-bearing files. Remove the PDF from `images/`. Add the lifecycle file to the README and restore list. | 0.5 | Low | — | Clean checkout builds deb/snap. `git status` shows no untracked server files. |
| 1.1 | Build the E2E harness (Section G.1). Fix the unit harness. | 2–3 | Low | 1.0 | Harness runs. It should initially **fail** on the C1, C2 and C3 steps, which proves it detects them. |
| 1.2 | Server data safety: C6 (json_encode check, fail-closed reads, `.bak`), C15, C9 (atomic upload), C10 (lock in save helper) | 1.5 | Low | 1.1 | Non-UTF-8 and short-write tests. The catalog is never emptied. |
| 1.3 | Server last-copy safety: C4, C5, plus re-validation under the lock when a removal is acked | 1.5 | Med | 1.1 | Double-OFF test keeps one copy. Non-recursive folder OFF refused. |
| 1.4 | Planner convergence: C1, C2 (re-plannable + backoff), leases, per-device planning on every poll | 3 | Med | 1.1 | E2E steps 4, 10, OFF→ON and the new-device case pass. |
| 1.5 | Tombstone/recreate: C3 (auto new generation for newer content, client refusal check), C8 | 2 | Med | 1.1 | E2E step 12 regression passes. Old offline device still can't resurrect. |
| 1.6 | Desktop: remove the legacy loop from all automatic paths. Add the headless `desktop run` daemon (systemd user unit, flock). GUI attaches. | 3–4 | Med | 1.4 | GUI closed: a file added on the web arrives within a minute. `ps` shows exactly one sync process. |
| 1.7 | Android hotfixes in the existing code: disable `SyncEngine`, mtime → seconds, exclude temp names, streaming transfers, 10 s/60 s timeouts, show refresh errors, local files openable offline | 3 | Med | 1.4 | A 1 GB file transfers without OOM. "On this phone" state is correct. |
| 1.8 | Versioned deployment (Section H steps 2–6) | 2–3 | Low | 1.0 | Deploying an API without its matching lifecycle file is refused at preflight. |

### Phase 2 — Reliability & performance (≈ 2–3 weeks)

| # | Task | Effort | Risk | Verify |
|---|---|---|---|---|
| 2.1 | `sync_status` endpoint + truthful status in desktop. Remove legacy-stdout status and the direct-children-only check. | 2 | Low | Pending deep in the tree → not "Up to date". |
| 2.2 | Server-authoritative placement objects (Section E). Lazy default inheritance + migration. Stamp web-UI/move/recreate (C7). Desktop renders only server state. | 3 | Med | No OFF toggles for server-ON items in any of the 5 listed scenarios. |
| 2.3 | Newest-wins resolution (D.3), clock-offset normalization, Pi waits for time sync | 2 | Med | Two-offline-edits E2E. Skewed-clock unit test. |
| 2.4 | Local delete → global delete with safeguards. Rename pairing. Catalog-level `move`. Route API/web delete through tombstones (C14). Pending your decision on D.4. | 3–4 | High | Delete in the file manager disappears everywhere. A stale device can't delete newer content. |
| 2.5 | inotify detection + incremental manifests + 5–10 min fallback scan. Per-subtree scan errors. Pi interval 60 s, cached hashing. | 3 | Med | New file published in under 15 s. Idle CPU near 0. |
| 2.6 | Capacity: free-space heartbeat, staging dedupe, `.bin` GC, `disk_free_space` checks, chunked upload or raised body limit | 3 | Med | 50 MB and ENOSPC E2E cases. |
| 2.7 | Planner performance: job index, record GC, lazy staging, heartbeat outside the lock | 3–4 | Med | Poll time stays flat as record count grows. |
| 2.8 | Desktop hygiene: move network calls off the Tk thread, unique temp names, structured rotating log, no swallowed errors | 2 | Low | No UI freeze with the server offline. |

### Phase 3 — Android simplification (≈ 3–5 weeks)

| # | Task | Effort | Risk | Verify |
|---|---|---|---|---|
| 3.1 | Toolchain upgrade. Add ViewModel, coroutines, WorkManager, OkHttp, Compose/M3, Media3. | 1 | Med | Clean build. Existing tests pass. |
| 3.2 | Repository + `SyncWorker` (periodic + expedited, unique work). Delete SyncService/BootReceiver/SyncEngine. Background sync default ON. | 3 | Med | Phone asleep overnight: files from the laptop present in the morning. |
| 3.3 | OkHttp LAN-fallback DNS + connection diagnostics. Plus the split-horizon DNS on the Pi (network side). | 2 | Low | Works on home Wi-Fi with Private DNS = Automatic. |
| 3.4 | Compose Files screen (cache-first, status icons, search/sort, FAB upload/new folder) | 4 | Med | Your Section 8 checklist. |
| 3.5 | Action sheet + Info & storage sheet (toggles from server placement objects) + global delete | 3 | Med | Toggle round trip reflects server state. Delete confirmation lists devices. |
| 3.6 | Activity and Devices tabs, Settings | 3 | Low | Failed item shows its reason and Retry works. |
| 3.7 | Media3 MediaSessionService + mini-player. API `download` Range support (small server change). | 3 | Med | Audiobook keeps playing with the screen off. Seek near the end is instant. |
| 3.8 | Remove MainActivity and the old layouts | 0.5 | Low | — |

### Phase 4 — Optional

- **Per-device tokens** issued at registration, bound to `device_id`. Reserved-ID guard. IP-based login backoff (C16).
- **Preview hardening:** serve HTML/SVG as attachment or with `CSP: sandbox` (C17). This is security, so consider it Phase 2 if the web UI is exposed publicly.
- **SQLite (WAL) for `freecloud_meta`.**
- **Case-collision detection** for SAF/Windows.
- **Desktop UI simplification:** matching the Android information architecture, so the Tk app becomes a thin viewer over the daemon.

---

## J. Recommendation: the first five changes

Ranked by impact on reliability. **Precondition for all five:** commit the untracked load-bearing files and build the E2E harness (1.0–1.1, about 3 days). Without it you cannot tell whether any of these worked, which is how the project got here.

1. **Make the Catalog engine the only sync engine, and run it headless on every device.**
   - Remove the legacy loop from the desktop GUI, background mode, autostart, and Android.
   - Add the systemd-managed `desktop run` daemon and the Android WorkManager worker.
   - *Why first:* today the newer engine only works while a window is open, and the legacy engine is actively fighting it. That causes duplicate transfers, undone deletes and false "Synced". Every other fix is undermined while two engines race.
2. **Close the server's data-loss holes:** C6 (catalog emptied by a bad filename), C15 (corrupt file read as empty), C4 (OFF deletes the last copy), C5 (non-recursive folder OFF), C9 (truncating uploads), C10 (unlocked writers).
   - *Why:* each one can destroy real data or the catalog in a single event. All are small, local fixes, about 3 days together.
3. **Make the planner converge:** C1 (plan missing desired devices), C2 (retry terminal jobs with backoff), job leases, per-device planning on every poll, and a fix for the 10 MB upload cap.
   - *Why:* these are the direct causes of "in the catalog but never arrived", "stuck Pending", "offline device never catches up", and "new device gets nothing". This change does the most for your #1 requirement.
4. **Fix deletion semantics:** C3 (new content under a deleted name is auto-deleted), C8, and (with your approval) local-delete-means-global-delete with stale-device safeguards.
   - *Why:* C3 silently destroys new work, and the lack of local deletes makes FreeCloud behave unlike any cloud drive. The existing tombstone design stays and becomes trustworthy.
5. **One server-authoritative placement and status model:** lazy `all` default + migration + stamping on every entry point (C7), per-provider `{desired, source, actual, pending+reason}` objects, the `sync_status` endpoint, and clients that only render.
   - *Why:* this removes, structurally, the whole family of "toggle shows OFF", "Up to date while pending" and "Pending with no reason" bugs. It is also the foundation the Android redesign needs.

Versioned deployment (1.8) should land with change #2, before any of these reach the Pi.

---

### Questions that genuinely need your decision

1. **Local delete:** should deleting a file in `~/FreeCloud` (or in the Android folder) be a global delete, as in Dropbox/Drive (recommended), or should only the explicit "Delete everywhere" action delete?
2. **Upload size:** chunked uploads (more code, works with any proxy limit), or raise the nginx/PHP body limit on the Pi (one config change)?
3. **Android navigation:** three bottom tabs (Files / Activity / Devices) + Settings gear (recommended), or a single Files screen with Activity reached from the status chip?
