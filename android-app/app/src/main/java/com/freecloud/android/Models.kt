package com.freecloud.android

data class FreeCloudConfig(
    val domain: String,
    val driveName: String,
    val baseUrl: String,
    val password: String,
    val treeUri: String,
    val intervalSeconds: Int = 30,
    val deleteRemote: Boolean = false,
    val autoSync: Boolean = false,
    val deviceId: String = "",
    val deviceName: String = "",
    val deviceType: String = "android",
)

data class ManifestEntry(
    val path: String,
    val name: String,
    val type: String,
    val size: Long,
    val mtime: Long,
    val hash: String = "",
    val locations: List<StorageLocation> = emptyList(),
    val desiredLocations: List<StorageLocation> = emptyList(),
    val staleLocations: List<String> = emptyList(),
    val removingLocations: List<String> = emptyList(),
) {
    val isDirectory: Boolean
        get() = type == "dir"

    val hasWebServerCopy: Boolean
        get() = locations.isEmpty() || locations.any { it.kind == "web_server" && it.available }

    fun availableFromLabel(): String {
        if (locations.isEmpty()) return "Web Server"
        val labels = locations
            .filter { it.available }
            .map { it.displayName() }
            .distinct()
        if (labels.isEmpty()) return "Unavailable"
        return if (labels.size <= 2) labels.joinToString(", ") else "${labels.first()}, +${labels.size - 1} more"
    }
}

data class StorageLocation(
    val kind: String,
    val deviceId: String,
    val name: String,
    val available: Boolean,
    val size: Long = 0,
    val mtime: Long = 0,
    val hash: String = "",
) {
    fun displayName(): String {
        if (name.isNotBlank()) return name
        return when (kind) {
            "web_server" -> "Web Server"
            "local_node" -> "Storage Node"
            else -> kind.ifBlank { "Storage" }
        }
    }
}

data class ReconciliationJob(
    val jobId: String,
    val path: String,
    val status: String,
    val sourceDeviceId: String,
    val destinationDeviceId: String,
    val expectedSize: Long,
    val expectedMtime: Long,
    val hash: String,
    val destinationFacts: StorageLocation? = null,
    val recursive: Boolean = false,
    val removalFacts: Map<String, StorageLocation> = emptyMap(),
)

data class PlacementRule(val providerId: String, val path: String, val enabled: Boolean, val recursive: Boolean)

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
    val reconciledUploaded: Int = 0,
    val reconciledDownloaded: Int = 0,
) {
    fun summary(): String {
        val parts = mutableListOf<String>()
        if (uploaded > 0) parts += "uploaded $uploaded"
        if (downloaded > 0) parts += "downloaded $downloaded"
        if (deletedLocal > 0) parts += "deleted local $deletedLocal"
        if (deletedRemote > 0) parts += "deleted remote $deletedRemote"
        if (conflicts > 0) parts += "conflicts $conflicts"
        if (reconciledUploaded > 0) parts += "reconciled upload $reconciledUploaded"
        if (reconciledDownloaded > 0) parts += "reconciled download $reconciledDownloaded"
        return if (parts.isEmpty()) "No changes." else parts.joinToString(", ")
    }
}
