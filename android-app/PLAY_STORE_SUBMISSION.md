# FreeCloud Google Play Submission

This file tracks the Android app submission work for Google Play.

## Current Rejection

Google Play rejected the app because reviewers were not given working app access details.

FreeCloud does not use a normal username login. The app needs:

- a FreeCloud server URL
- the FreeCloud server password
- an Android phone folder selected through the system folder picker

Google needs those details in **Policy and programs > App content > App access**.

## Required Demo Access

Create and maintain one demo FreeCloud install for Google review.

Requirements:

- The demo server must be online at all times while the app is in review.
- The password must not expire.
- The server must work from any reviewer location.
- The demo should contain harmless sample files only.
- Do not store the demo password in this git repository.

Suggested demo contents:

- `Welcome.txt`
- `Documents/Example note.txt`
- `Pictures/sample.png`

## Play Console App Access Text

Paste this into the Play Console App access instructions after replacing the placeholders.

```text
FreeCloud does not use usernames or third-party sign-in. The app connects to a user-provided FreeCloud server.

Use this reusable demo server for review:

Website Address: <DEMO_BASE_URL>
Remote Folder: FreeCloud
Password: <DEMO_PASSWORD>

Steps:
1. Open the app.
2. In Website Address, enter <DEMO_DOMAIN_OR_BASE_URL>.
3. In Remote Folder, enter FreeCloud if the app shows that field.
4. In Password, enter <DEMO_PASSWORD>.
5. Tap Choose Phone Folder and select any test folder on the device. If needed, create an empty folder named FreeCloudReview.
6. Tap Start Syncing.
7. The app will connect to the demo server and show the synced files screen.

No username, email address, one-time password, QR code, payment, subscription, or Google/Facebook login is required.
```

Use one of these input formats, depending on how the demo server is hosted:

- If the server is at `https://example.com/FreeCloud`, enter:
  - Website Address: `https://example.com`
  - Remote Folder: `FreeCloud`
- If the server is at a custom base URL already ending in `/FreeCloud`, enter:
  - Website Address: `https://example.com/FreeCloud`
  - Remote Folder: `FreeCloud`

The app code handles both forms.

## Release Checklist

Before sending the next review:

- Confirm the demo server URL opens in a browser.
- Confirm `freecloud_api.php?action=ping` returns a FreeCloud JSON response.
- Install the release build on a clean Android device or emulator.
- Verify the App access instructions exactly match the app fields.
- Verify the app reaches the synced files screen using only the Play Console credentials.
- Keep the demo server online until review is complete.
- Complete the Data safety form consistently with the app behavior.
- Confirm the privacy policy URL is reachable.
- Upload the new `.aab` if the app version changed.
- Go to **Publishing overview** and click **Send for review**.

## Data Safety Notes

FreeCloud syncs files chosen by the user to the user's configured FreeCloud server.

The app currently:

- uses network access to connect to the user-provided server
- asks the user to choose a local folder through Android's folder picker
- stores the server URL and selected folder URI locally
- stores the FreeCloud password locally using Android Keystore encryption
- runs a foreground data sync service
- does not include ads
- does not include analytics SDKs
- does not use third-party sign-in

Data safety answers should describe the app honestly. If the user syncs personal files to their own server, that is user-directed transfer to the server they configured.

## Build Commands

From `android-app/`:

```bash
./gradlew test
./gradlew bundleRelease
```

The release artifact is normally written under:

```text
app/build/outputs/bundle/release/
```

The repository currently also contains:

```text
app/release/app-release.aab
```
