# FreeCloud Manager Overview

FreeCloud is a GPL-licensed personal cloud system consisting of a hosted PHP
web application/API, a Python Linux/macOS desktop app and CLI, a Raspberry Pi
storage-node mode, a native Kotlin Android app, and a public website. Windows
has a launcher but is not yet a mature, separately tested desktop target.

## Where the project is now

The project is transitioning from copying every file to every device toward a
metadata-driven, selective-sync architecture. The hosted PHP server maintains
the complete virtual file tree and records which devices hold each file's
bytes. A Raspberry Pi or Linux node can publish a local folder into that tree
without uploading its contents to the web host. The web, desktop, and Android
browsers can display these metadata-only entries and show their storage
location.

The older full-folder sync engine still exists for compatibility, but the
desktop and Android apps no longer start it automatically. Android also has an
initial media player with saved playback position and next-file playback.

The current Python test suite has 26 passing tests, the Python entry points
compile, and all PHP files pass syntax checks. Android is documented as not
fully build-tested in this development environment. There is also a large set
of existing uncommitted changes implementing the metadata/node work, so those
changes should be reviewed and preserved before unrelated refactoring.

## Major unfinished work

- One-time node-to-web downloads now work through protected temporary staging;
  streaming, chunking, resume, and node-to-node delivery are not implemented.
- Selective-sync rules are not fully enforced by every client.
- Android and desktop lack complete download-on-demand and keep-offline flows.
- Conflict/version handling and distributed deletion/tombstones are incomplete.
- JSON metadata persistence needs safer locking/atomic-write handling for
  concurrent requests.
- Windows support, integration tests, and deployment/security hardening remain
  incomplete.
- `PROJECT_NOTES.md` describes much of the older architecture and should not be
  treated as the current source of truth; `README.md` is newer.

## Suggested next plan

1. Stabilize the current metadata branch: review the uncommitted diff, build
   Android with Java 17/Android SDK, add PHP API behavior tests, and commit a
   known-good checkpoint.
2. Define the node-transfer protocol before adding UI: node presence,
   authentication, transfer requests, range/resume support, timeouts, and
   behavior when a node is offline.
3. Exercise the node-to-web vertical slice on the real PHP host and Pi,
   including offline, oversize, hash-mismatch, expiry, and successful cleanup.
4. Build `browse_only`, `download_on_demand`, and `pinned` behavior around that
   transfer path, using "most specific path wins" for nested rules.
5. Add explicit version/conflict and deletion semantics before enabling more
   aggressive two-way selective sync.
6. Harden metadata storage, authentication, secret handling, upload limits,
   and deployment behavior before presenting FreeCloud as production-ready.

The current milestone is the generic queued node-to-web transfer foundation.
The next milestone should reuse it for persistent desktop sync/keep-offline.
