# FreeCloud Android App

This folder contains the Android app for FreeCloud.

## What It Does Right Now

- first screen lets the user enter:
  - domain
  - remote folder name
  - password
  - local phone folder through the Android folder picker
- saves that setup locally
- connects to the hosted FreeCloud install without automatically downloading every file
- uses the chosen phone folder for local files, edited files, shares, and the legacy manual sync service
- shows a running screen with:
  - running / stopped status
  - saved server URL
  - saved local folder
  - metadata-backed FreeCloud file browser
  - activity log
  - start / stop / view on web / edit setup buttons
- shows files/folders published through server metadata, including node-only folders such as Raspberry Pi storage
- shows where entries are available from, such as Web Server or Raspberry Pi
- marks files that are only on another node as node-only
- includes a simple built-in media player panel for audio/video files that are either local on the phone or available directly from the web server
- remembers the last played media file and playback position
- resumes the saved media position when that file is opened again
- automatically starts the next playable media file in the same folder when playback completes
- can open and edit local text files from the synced folder
- can share local files and zip local folders
- only starts the old full-sync service manually unless auto-sync is explicitly enabled in config

## Important Notes

- this uses the same server API already in this repo
- the chosen local folder uses Android's Storage Access Framework
- metadata browsing uses `freecloud_api.php?action=metadata_list`
- the old full-sync service still exists, but the app no longer starts it automatically after setup
- sync is intentionally simple and should be converted carefully to the new selective-sync model
- local deletions do not delete hosted files by default; the hosted copy is restored on the next sync
- Raspberry Pi / local-node streaming is not finished; the temporary web-transfer mechanism is not yet wired into Android
- a Pi-only audiobook can appear in the Android browser before it is playable
- Android does not yet have keep-offline selection or true node-to-phone on-demand download

## Current Limits

- not build-tested in this environment because this workspace does not have Java / Android SDK installed
- no conflict UI yet
- no iPhone app yet
- no push or live presence system for showing other connected users
- no Android integration with the node-transfer queue yet
- no keep-offline UI yet
- no folder-level audiobook library UI beyond sorted folder playback

## Open In Android Studio

Open the `android-app/` folder in Android Studio.

If Android Studio asks to update the Gradle plugin or Kotlin version, review that before accepting changes.

## Command-Line Run And Test

Once Java 17 and the Android SDK are installed locally:

```bash
cd android-app
./gradlew test
./gradlew assembleDebug
```

## Main Files

- `app/src/main/java/com/freecloud/android/MainActivity.kt`
- `app/src/main/java/com/freecloud/android/SyncService.kt`
- `app/src/main/java/com/freecloud/android/SyncEngine.kt`
- `app/src/main/java/com/freecloud/android/FreeCloudApiClient.kt`
- `app/src/main/java/com/freecloud/android/DocumentTreeSync.kt`
