package com.freecloud.android

data class FreeCloudConfig(
    val domain: String,
    val driveName: String,
    val baseUrl: String,
    val password: String,
    val treeUri: String,
    val intervalSeconds: Int = 30,
    val deleteRemote: Boolean = false,
)

data class ManifestEntry(
    val path: String,
    val name: String,
    val type: String,
    val size: Long,
    val mtime: Long,
) {
    val isDirectory: Boolean
        get() = type == "dir"
}

data class SyncState(
    val local: Map<String, ManifestEntry> = emptyMap(),
    val remote: Map<String, ManifestEntry> = emptyMap(),
)

data class SyncCounts(
    val uploaded: Int = 0,
    val downloaded: Int = 0,
    val deletedLocal: Int = 0,
    val deletedRemote: Int = 0,
    val conflicts: Int = 0,
) {
    fun summary(): String {
        val parts = mutableListOf<String>()
        if (uploaded > 0) parts += "uploaded $uploaded"
        if (downloaded > 0) parts += "downloaded $downloaded"
        if (deletedLocal > 0) parts += "deleted local $deletedLocal"
        if (deletedRemote > 0) parts += "deleted remote $deletedRemote"
        if (conflicts > 0) parts += "conflicts $conflicts"
        return if (parts.isEmpty()) "No changes." else parts.joinToString(", ")
    }
}
