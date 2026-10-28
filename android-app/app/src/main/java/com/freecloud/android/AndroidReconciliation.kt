package com.freecloud.android

data class ReconciliationCounts(val uploaded: Int = 0, val downloaded: Int = 0, val conflicts: Int = 0)

class AndroidReconciliation(
    private val client: FreeCloudApiClient,
    private val documents: DocumentTreeSync,
) {
    fun process(config: FreeCloudConfig, identity: DeviceIdentity, onProgress: ((String) -> Unit)? = null): ReconciliationCounts {
        var uploaded = 0
        var downloaded = 0
        var conflicts = 0
        for (job in client.pollReconciliation(identity.id)) {
            try {
                when {
                    job.status == "queued_delete" && job.destinationDeviceId == identity.id -> {
                        val local = documents.scanTree(config.treeUri)
                        if (local.containsKey(job.path)) {
                            require(documents.deletePath(config.treeUri, job.path)) { "Could not delete globally deleted Android copy" }
                        }
                        client.acknowledgeReconciliation(identity.id, job.jobId, "deleted")
                        downloaded++
                    }
                    job.status == "queued_hash" && job.sourceDeviceId == identity.id -> {
                        try {
                            val node = documents.scanTree(config.treeUri)[job.path]
                                ?: error("Android copy is unavailable for hash verification")
                            require(!node.entry.isDirectory && ReconciliationSafety.sameFacts(
                                node.entry.size, documents.normalizedMtime(node), job.expectedSize, job.expectedMtime,
                            )) { "Android copy changed; hash proof was refused" }
                            client.reportLocationHash(identity.id, job.jobId, documents.sha256(node))
                        } catch (error: Throwable) {
                            client.reportLocationHash(identity.id, job.jobId, error = error.message ?: "Android hash verification failed")
                            conflicts++
                        }
                    }
                    job.status == "queued_remove" && job.destinationDeviceId == identity.id -> {
                        val local = documents.scanTree(config.treeUri)
                        require(job.removalFacts.isNotEmpty()) { "Removal job has no verified facts" }
                        for ((path, facts) in job.removalFacts) {
                            val node = local[path] ?: continue
                            if (!node.entry.isDirectory) require(
                                ReconciliationSafety.sameFacts(node.entry.size, documents.normalizedMtime(node), facts.size, facts.mtime)
                            ) { "Android copy changed; removal was refused" }
                            if (!node.entry.isDirectory) require(
                                facts.hash.matches(Regex("[a-fA-F0-9]{64}")) && documents.sha256(node).equals(facts.hash, ignoreCase = true)
                            ) { "Android copy could not be verified by SHA-256; removal was refused" }
                        }
                        val prefix = "${job.path}/"
                        val actualSubtree = local.keys.filter { it == job.path || it.startsWith(prefix) }.toSet()
                        require(actualSubtree.all { it in job.removalFacts }) { "Android folder changed; removal was refused" }
                        if (local.containsKey(job.path)) {
                            require(documents.deletePath(config.treeUri, job.path)) { "Could not remove Android copy" }
                        }
                        client.acknowledgeReconciliation(identity.id, job.jobId, "removed")
                        downloaded++
                    }
                    job.status == "queued_source" && job.sourceDeviceId == identity.id -> {
                        onProgress?.invoke("Preparing ${job.path}...")
                        val before = documents.scanTree(config.treeUri)[job.path]
                            ?: error("Requested source file is missing")
                        require(!before.entry.isDirectory && sameFacts(before, job.expectedSize, job.expectedMtime, documents)) {
                            "Source changed before transfer"
                        }
                        val bytes = documents.uploadBytes(before)
                        val hash = ReconciliationSafety.sha256(bytes)
                        client.uploadReconciliation(identity.id, job.jobId, bytes, hash)
                        val after = documents.scanTree(config.treeUri)[job.path]
                        val unchanged = after != null && sameFacts(after, job.expectedSize, job.expectedMtime, documents)
                            && documents.sha256(after) == hash
                        val committed = client.commitReconciliation(identity.id, job.jobId, unchanged)
                        if (committed.optJSONObject("job")?.optString("status") == "conflict") conflicts++ else uploaded++
                    }
                    job.status == "ready" && job.destinationDeviceId == identity.id -> {
                        onProgress?.invoke("Receiving ${job.path}...")
                        val existing = documents.scanTree(config.treeUri)[job.path]
                        if (!destinationUnchanged(existing, job, documents)) {
                            client.acknowledgeReconciliation(identity.id, job.jobId, "conflict", "Android destination changed while transfer was pending.")
                            conflicts++
                            continue
                        }
                        val bytes = client.downloadReconciliation(identity.id, job.jobId)
                        require(bytes.size.toLong() == job.expectedSize) { "Downloaded size does not match" }
                        require(job.hash.isNotBlank() && ReconciliationSafety.sha256(bytes) == job.hash) { "Downloaded SHA-256 does not match" }
                        documents.installReconciliationFile(config.treeUri, job.path, bytes, job.expectedMtime)
                        client.acknowledgeReconciliation(identity.id, job.jobId, "installed")
                        downloaded++
                    }
                    job.status == "conflict" -> conflicts++
                }
            } catch (error: Throwable) {
                runCatching { client.acknowledgeReconciliation(identity.id, job.jobId, "failed", error.message ?: "Android reconciliation failed") }
                conflicts++
            }
        }
        return ReconciliationCounts(uploaded, downloaded, conflicts)
    }

    private fun destinationUnchanged(existing: LocalNode?, job: ReconciliationJob, documents: DocumentTreeSync): Boolean {
        if (existing?.entry?.isDirectory == true) return false
        return ReconciliationSafety.destinationMayInstall(
            existing != null, existing?.entry?.size ?: 0, existing?.let { documents.normalizedMtime(it) } ?: 0,
            job.destinationFacts,
        )
    }

    private fun sameFacts(node: LocalNode, size: Long, mtime: Long, documents: DocumentTreeSync): Boolean =
        ReconciliationSafety.sameFacts(node.entry.size, documents.normalizedMtime(node), size, mtime)
}
