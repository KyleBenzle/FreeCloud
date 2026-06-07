# FreeCloud Android App

This folder contains the Android app for FreeCloud.

## What It Does Right Now

- first screen lets the user enter:
  - domain
  - remote folder name
  - password
  - local phone folder through the Android folder picker
- saves that setup locally
- starts a foreground sync service
- syncs the chosen phone folder with the existing FreeCloud PHP API
- shows a running screen with:
  - running / stopped status
  - saved server URL
  - saved local folder
  - local synced file browser
  - activity log
  - start / stop / view on web / edit setup buttons
- can open and edit local text files from the synced folder
- can share local files and zip local folders
- restarts sync after boot or package replacement when setup exists

## Important Notes

- this uses the same server API already in this repo
- the chosen local folder uses Android's Storage Access Framework
- sync is intentionally simple in this first version
- the mobile sync behavior is meant to be full local storage sync, not remote-only browsing
- local deletions do not delete hosted files by default; the hosted copy is restored on the next sync

## Current Limits

- not build-tested in this environment because this workspace does not have Java / Android SDK installed
- no conflict UI yet
- no iPhone app yet
- no push or live presence system for showing other connected users

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
