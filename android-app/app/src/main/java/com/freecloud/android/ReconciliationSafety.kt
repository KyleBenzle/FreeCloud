package com.freecloud.android

import java.security.MessageDigest
import kotlin.math.abs

object ReconciliationSafety {
    fun sameFacts(size: Long, mtime: Long, expectedSize: Long, expectedMtime: Long): Boolean =
        size == expectedSize && abs(mtime - expectedMtime) <= 2

    fun destinationMayInstall(exists: Boolean, size: Long, mtime: Long, planned: StorageLocation?): Boolean =
        if (planned == null) !exists else exists && sameFacts(size, mtime, planned.size, planned.mtime)

    fun sha256(bytes: ByteArray): String = MessageDigest.getInstance("SHA-256")
        .digest(bytes).joinToString("") { "%02x".format(it) }

    fun isManaged(
        path: String,
        rules: List<PlacementRule>,
        catalogManagedPaths: Set<String> = emptySet(),
    ): Boolean = path in catalogManagedPaths || rules.any { rule ->
        rule.enabled && (path == rule.path || (rule.recursive && (rule.path.isBlank() || path.startsWith("${rule.path}/"))))
    }
}
