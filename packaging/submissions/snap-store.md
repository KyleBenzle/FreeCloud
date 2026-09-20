# Snap Store Submission

This tracks the Snap Store path for FreeCloud.

The Debian email for bug `#1139338` means FreeCloud was accepted into Debian
unstable as `freecloud 2.1.1+dfsg2-1`. That is separate from the Snap Store.

## Current Local Status

- `snap/snapcraft.yaml` exists.
- The snap is named `freecloud`.
- The snap uses strict confinement.
- `snap` is installed locally.
- `snapd` is active on the host system.
- `snapcraft 9.0.1` is installed locally.
- Default local builds need LXD.
- `snapcraft pack --destructive-mode` is not usable on the current host because
  this machine is Ubuntu 25.10 and the snap is `base: core24`.
- Local LXD build produced `freecloud_2.1.1_amd64.snap`.
- Local install test passed for `freecloud.cli --version`.

## Local Build Environment

Use one of these paths.

### Option A: LXD local build

Install and initialize LXD:

```sh
sudo snap install lxd
sudo lxd init --auto
sudo usermod -aG lxd "$USER"
```

Log out and back in after adding the user to the `lxd` group. Then build:

```sh
cd /home/lll/MEGA/FreeCloud
snapcraft pack
```

### Option B: Remote Launchpad build

Remote build avoids local LXD setup, but it uploads the project source to
Launchpad. The project must be committed to git first.

```sh
cd /home/lll/MEGA/FreeCloud
snapcraft login
snapcraft remote-build --build-for=amd64 --launchpad-accept-public-upload
```

Use this only after committing the snap packaging files.

## Build And Publish Flow

From the repository root:

```sh
snapcraft pack
```

That should create a file like:

```text
freecloud_2.1.1_amd64.snap
```

Install-test it locally before uploading:

```sh
sudo snap install --dangerous ./freecloud_2.1.1_amd64.snap
freecloud
freecloud.cli --version
sudo snap remove freecloud
```

Log in to the Snap Store:

```sh
snapcraft login
```

Register the name once:

```sh
snapcraft register freecloud
```

Upload to edge first:

```sh
snapcraft upload --release=edge freecloud_2.1.1_amd64.snap
```

Test from the store:

```sh
sudo snap install freecloud --edge
freecloud
freecloud.cli --version
```

After testing, promote to stable:

```sh
snapcraft status freecloud
snapcraft release freecloud <revision> stable
```

Or upload directly to stable once the package has been tested:

```sh
snapcraft upload --release=stable freecloud_2.1.1_amd64.snap
```

## Store Listing Text

Short description:

```text
Personal cloud drive using existing web hosting
```

Long description:

```text
FreeCloud turns storage space from an existing PHP web-hosting account into a
personal cloud drive. The desktop client provides a file browser and keeps a
selected local folder synchronized with the user's hosted FreeCloud drive.

FreeCloud includes a Python/Tk desktop app, a command-line sync client, and PHP
server files that users can upload to their own web host.
```

Links:

```text
Website: https://freecloud.wiki
Source code: https://github.com/KyleBenzle/FreeCloud
Issues: https://github.com/KyleBenzle/FreeCloud/issues
License: GPL-3.0-or-later
```

## Notes

The snap uses strict confinement and the `home` plug. Users should choose a
normal folder under their home directory, such as `~/FreeCloud`, for syncing.
