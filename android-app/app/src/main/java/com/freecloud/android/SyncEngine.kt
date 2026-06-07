package com.freecloud.android

import android.content.Context
import kotlin.math.abs

class SyncEngine(private val context: Context) {
    private val stateStore = SyncStateStore(context)
    private val documents = DocumentTreeSync(context)

    fun syncOnce(config: FreeCloudConfig, onProgress: ((String) -> Unit)? = null): SyncCounts {
        onProgress?.invoke("Scanning local folder...")
        val previous = stateStore.load()
        val previousLocal = previous.local
        val previousRemote = previous.remote

        val client = FreeCloudApiClient(config.baseUrl, config.password)
        val local = documents.scanTree(config.treeUri)
        onProgress?.invoke("Loading remote file list...")
        val remote = client.manifest().associateBy { it.path }
        onProgress?.invoke("Syncing ${local.size} local and ${remote.size} remote items...")

        var uploaded = 0
        var downloaded = 0
        var deletedLocal = 0
        var deletedRemote = 0
        var conflicts = 0

        val allPaths = (local.keys + remote.keys + previousLocal.keys + previousRemote.keys).toSortedSet()

        for (path in allPaths) {
            val localNode = local[path]
            val localEntry = localNode?.entry
            val remoteEntry = remote[path]
            val previousLocalEntry = previousLocal[path]
            val previousRemoteEntry = previousRemote[path]

            if (localEntry != null &&
                remoteEntry != null &&
                previousLocalEntry == null &&
                previousRemoteEntry == null &&
                roughlyEqual(localEntry, remoteEntry)
            ) {
                continue
            }

            if (localEntry?.isDirectory == true && remoteEntry == null) {
                client.mkdir(path)
                uploaded += 1
                onProgress?.invoke("Created remote folder: $path")
                continue
            }

            if (remoteEntry?.isDirectory == true && localEntry == null) {
                documents.ensureDirectory(config.treeUri, path)
                downloaded += 1
                onProgress?.invoke("Created local folder: $path")
                continue
            }

            if ((localEntry?.isDirectory == true && remoteEntry?.isDirectory == false) ||
                (localEntry?.isDirectory == false && remoteEntry?.isDirectory == true)
            ) {
                conflicts += 1
                continue
            }

            val localChanged = localEntry != null && !roughlyEqual(localEntry, previousLocalEntry)
            val remoteChanged = remoteEntry != null && !roughlyEqual(remoteEntry, previousRemoteEntry)

            if (localEntry != null && remoteEntry != null && !localChanged && !remoteChanged) {
                continue
            }

            if (localEntry != null && remoteEntry == null) {
                if (previousRemoteEntry != null && !localChanged) {
                    if (documents.deletePath(config.treeUri, path)) {
                        deletedLocal += 1
                        onProgress?.invoke("Deleted local item: $path")
                    }
                } else if (!localEntry.isDirectory) {
                    val bytes = documents.uploadBytes(localNode!!)
                    client.upload(path, bytes, localEntry.mtime, documents.contentTypeForName(localEntry.name))
                    uploaded += 1
                    onProgress?.invoke("Uploaded: $path")
                }
                continue
            }

            if (remoteEntry != null && localEntry == null) {
                // Match the desktop default: a local deletion is not allowed to
                // erase the hosted copy unless that behavior is explicitly on.
                if (previousLocalEntry != null && !remoteChanged && config.deleteRemote) {
                    client.delete(path)
                    deletedRemote += 1
                    onProgress?.invoke("Deleted remote item: $path")
                } else if (!remoteEntry.isDirectory) {
                    documents.downloadToTree(config.treeUri, path, client.downloadBytes(path))
                    downloaded += 1
                    onProgress?.invoke("Downloaded: $path")
                }
                continue
            }

            if (localEntry != null && remoteEntry != null) {
                if (localChanged && remoteChanged) {
                    conflicts += 1
                    onProgress?.invoke("Conflict: $path")
                } else if (localChanged) {
                    val bytes = documents.uploadBytes(localNode!!)
                    client.upload(path, bytes, localEntry.mtime, documents.contentTypeForName(localEntry.name))
                    uploaded += 1
                    onProgress?.invoke("Uploaded: $path")
                } else if (!remoteEntry.isDirectory) {
                    documents.downloadToTree(config.treeUri, path, client.downloadBytes(path))
                    downloaded += 1
                    onProgress?.invoke("Downloaded: $path")
                }
            }
        }

        onProgress?.invoke("Refreshing sync state...")
        val freshLocal = documents.scanTree(config.treeUri).mapValues { it.value.entry }
        val freshRemote = client.manifest().associateBy { it.path }
        stateStore.save(SyncState(local = freshLocal, remote = freshRemote))

        return SyncCounts(
            uploaded = uploaded,
            downloaded = downloaded,
            deletedLocal = deletedLocal,
            deletedRemote = deletedRemote,
            conflicts = conflicts,
        )
    }

    private fun roughlyEqual(current: ManifestEntry, previous: ManifestEntry?): Boolean {
        if (previous == null) return false
        if (current.type != previous.type) return false
        if (current.isDirectory) return true
        return current.size == previous.size && abs(current.mtime - previous.mtime) <= 2_000
    }
}
