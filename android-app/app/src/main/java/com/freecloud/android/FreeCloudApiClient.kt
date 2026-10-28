package com.freecloud.android

import org.json.JSONArray
import org.json.JSONObject
import java.io.ByteArrayOutputStream
import java.io.InputStream
import java.net.HttpURLConnection
import java.net.URL
import java.net.URLEncoder

class FreeCloudApiException(message: String, val code: Int = 0, val body: String = "") : RuntimeException(message)

class FreeCloudApiClient(baseUrl: String, private val password: String) {
    private var baseUrl: String = baseUrl.removeSuffix("/")

    fun ping(): JSONObject = requestJson("ping")

    fun setup(name: String, password: String): JSONObject {
        val form = "name=${urlEncode(name)}&password=${urlEncode(password)}"
        return requestJson(
            action = "setup",
            method = "POST",
            contentType = "application/x-www-form-urlencoded",
            body = form.toByteArray(Charsets.UTF_8),
        )
    }

    fun manifest(): List<ManifestEntry> {
        val data = requestJson("manifest")
        val entries = data.optJSONArray("entries") ?: JSONArray()
        return parseEntries(entries)
    }

    fun metadataList(path: String): List<ManifestEntry> {
        val data = requestJson("metadata_list", mapOf("path" to UrlTools.remotePath(path)))
        val entries = data.optJSONArray("entries") ?: JSONArray()
        return parseEntries(entries)
    }

    fun metadataManifest(): List<ManifestEntry> {
        val data = requestJson("metadata_manifest")
        return parseEntries(data.optJSONArray("entries") ?: JSONArray())
    }

    fun placementRules(): List<PlacementRule> {
        val data = requestJson("placement_rules")
        val out = mutableListOf<PlacementRule>()
        fun collect(groups: JSONObject?, desiredRules: Boolean) {
            groups ?: return
            for (deviceId in groups.keys()) {
                val rules = groups.optJSONArray(deviceId) ?: continue
                for (index in 0 until rules.length()) {
                    val rule = rules.optJSONObject(index) ?: continue
                    // Any explicit placement rule, including OFF, assigns this path
                    // to reconciliation and prevents the legacy engine racing it.
                    val enabled = if (desiredRules) true else rule.optString("mode") in setOf("storage_only", "sync", "keep_offline", "upload_only", "download_only")
                    out += PlacementRule(deviceId, UrlTools.remotePath(rule.optString("path")), enabled, rule.optBoolean("recursive", true))
                }
            }
        }
        collect(data.optJSONObject("desired_rules"), true)
        collect(data.optJSONObject("sync_rules"), false)
        return out
    }

    fun placementManagedPaths(paths: Collection<String>): Set<String> {
        val normalized = paths.map { UrlTools.remotePath(it) }.filter { it.isNotEmpty() }.distinct().sorted()
        if (normalized.isEmpty()) return emptySet()
        require(normalized.size <= 20_000) { "Too many changed paths to check placement ownership safely." }
        val pathArray = JSONArray()
        normalized.forEach(pathArray::put)
        val data = postJson("placement_ownership", JSONObject().put("paths", pathArray))
        val managed = data.optJSONArray("managed_paths") ?: JSONArray()
        return (0 until managed.length())
            .mapNotNull { index -> managed.optString(index).takeIf { it.isNotEmpty() }?.let { UrlTools.remotePath(it) } }
            .toSet()
    }

    fun pollReconciliation(deviceId: String): List<ReconciliationJob> {
        val data = postJson("poll_reconciliation", JSONObject().put("device_id", deviceId))
        val jobs = data.optJSONArray("jobs") ?: JSONArray()
        return (0 until jobs.length()).mapNotNull { index -> parseJob(jobs.optJSONObject(index)) }
    }

    fun downloadReconciliation(deviceId: String, jobId: String): ByteArray {
        val connection = openConnection(
            apiUrl("download_reconciliation", mapOf("device_id" to deviceId, "job_id" to jobId)), "GET", null,
            mapOf("X-FreeCloud-Device" to deviceId),
        )
        return executeBytes(connection)
    }

    fun uploadReconciliation(deviceId: String, jobId: String, bytes: ByteArray, hash: String): JSONObject {
        val connection = openConnection(
            apiUrl("upload_reconciliation", mapOf("device_id" to deviceId, "job_id" to jobId)),
            "POST", "application/octet-stream",
            mapOf("X-FreeCloud-Device" to deviceId, "X-FreeCloud-SHA256" to hash),
        )
        return JSONObject(String(executeBytes(connection, bytes), Charsets.UTF_8))
    }

    fun commitReconciliation(deviceId: String, jobId: String, sourceUnchanged: Boolean): JSONObject =
        postJson("commit_reconciliation", JSONObject().put("device_id", deviceId).put("job_id", jobId).put("source_unchanged", sourceUnchanged))

    fun acknowledgeReconciliation(deviceId: String, jobId: String, status: String, error: String = ""): JSONObject =
        postJson("ack_reconciliation", JSONObject().put("device_id", deviceId).put("job_id", jobId).put("status", status).put("error", error.take(500)))

    fun reportLocationHash(deviceId: String, jobId: String, hash: String = "", error: String = ""): JSONObject =
        postJson("report_location_hash", JSONObject().put("device_id", deviceId).put("job_id", jobId).put("hash", hash).put("error", error.take(500)))

    fun registerDevice(identity: DeviceIdentity): JSONObject {
        return requestJson(
            action = "register_device",
            method = "POST",
            contentType = "application/json",
            body = JSONObject()
                .put("device_id", identity.id)
                .put("name", identity.name)
                .put("type", identity.type)
                .put("capabilities", JSONArray(listOf("metadata", "local_storage", "background_sync", "keep_offline")))
                .toString().toByteArray(Charsets.UTF_8),
        )
    }

    fun heartbeat(deviceId: String): JSONObject {
        return postJson("heartbeat", JSONObject().put("device_id", deviceId))
    }

    fun publishManifest(deviceId: String, entries: List<ManifestEntry>, complete: Boolean): JSONObject {
        val payloadEntries = JSONArray()
        for (entry in entries) {
            payloadEntries.put(
                JSONObject()
                    .put("path", entry.path)
                    .put("name", entry.name)
                    .put("type", entry.type)
                    .put("size", entry.size)
                    .put("mtime", entry.mtime)
                    .put("hash", entry.hash),
            )
        }
        return postJson(
            "publish_manifest",
            JSONObject().put("device_id", deviceId).put("entries", payloadEntries).put("complete", complete),
        )
    }

    fun postJson(action: String, payload: JSONObject): JSONObject {
        return requestJson(
            action = action,
            method = "POST",
            contentType = "application/json",
            body = payload.toString().toByteArray(Charsets.UTF_8),
        )
    }

    fun mkdir(path: String) {
        requestJson("mkdir", mapOf("path" to UrlTools.remotePath(path)), method = "POST", body = ByteArray(0))
    }

    fun upload(path: String, bytes: ByteArray, mtime: Long, contentType: String) {
        requestJson(
            action = "upload",
            params = mapOf("path" to UrlTools.remotePath(path), "mtime" to mtime.toString()),
            method = "POST",
            contentType = contentType,
            body = bytes,
        )
    }

    fun downloadBytes(path: String): ByteArray {
        val connection = openConnection(
            apiUrl("download", mapOf("path" to UrlTools.remotePath(path))),
            "GET",
            null,
        )
        return executeBytes(connection)
    }

    fun downloadUrl(path: String): String = apiUrl("download", mapOf("path" to UrlTools.remotePath(path)))

    fun requestHeaders(): Map<String, String> {
        return if (password.isEmpty()) emptyMap() else mapOf("X-FreeCloud-Password" to password)
    }

    fun delete(path: String) {
        requestJson("delete", mapOf("path" to UrlTools.remotePath(path)), method = "POST", body = ByteArray(0))
    }

    private fun requestJson(
        action: String,
        params: Map<String, String> = emptyMap(),
        method: String = "GET",
        contentType: String? = null,
        body: ByteArray? = null,
    ): JSONObject {
        val connection = openConnection(apiUrl(action, params), method, contentType)
        val responseBytes = executeBytes(connection, body)
        return try {
            JSONObject(String(responseBytes, Charsets.UTF_8))
        } catch (exc: Exception) {
            throw FreeCloudApiException("The server answered, but not with FreeCloud JSON.")
        }
    }

    private fun executeBytes(connection: HttpURLConnection, body: ByteArray? = null): ByteArray {
        try {
            if (body != null) {
                connection.doOutput = true
                connection.outputStream.use { it.write(body) }
            }

            val code = connection.responseCode
            val stream = if (code in 200..299) connection.inputStream else connection.errorStream
            val response = stream?.readFully() ?: ByteArray(0)
            rememberFinalUrl(connection.url.toString())
            if (code !in 200..299) {
                val bodyText = String(response, Charsets.UTF_8)
                val detail = parseErrorMessage(bodyText)
                val message = if (detail != null) {
                    "FreeCloud server error ($code): $detail"
                } else {
                    "HTTP $code from FreeCloud server."
                }
                throw FreeCloudApiException(message, code, bodyText)
            }
            return response
        } finally {
            connection.disconnect()
        }
    }

    private fun openConnection(url: String, method: String, contentType: String?, extraHeaders: Map<String, String> = emptyMap()): HttpURLConnection {
        val connection = URL(url).openConnection() as HttpURLConnection
        connection.requestMethod = method
        connection.connectTimeout = 120_000
        connection.readTimeout = 120_000
        connection.instanceFollowRedirects = true
        connection.setRequestProperty("User-Agent", "FreeCloudAndroid/1")
        if (password.isNotEmpty()) {
            connection.setRequestProperty("X-FreeCloud-Password", password)
        }
        if (contentType != null) {
            connection.setRequestProperty("Content-Type", contentType)
        }
        for ((key, value) in extraHeaders) connection.setRequestProperty(key, value)
        return connection
    }

    private fun apiUrl(action: String, params: Map<String, String>): String {
        val query = buildString {
            append("action=").append(urlEncode(action))
            for ((key, value) in params) {
                append("&").append(urlEncode(key)).append("=").append(urlEncode(value))
            }
        }
        return "${baseUrl.removeSuffix("/")}/freecloud_api.php?$query"
    }
    private fun rememberFinalUrl(finalUrl: String) {
        if (!finalUrl.endsWith("/freecloud_api.php") && !finalUrl.contains("/freecloud_api.php?")) return
        baseUrl = finalUrl.substringBefore("/freecloud_api.php").removeSuffix("/")
    }

    private fun parseEntries(entries: JSONArray): List<ManifestEntry> {
        val out = mutableListOf<ManifestEntry>()
        for (index in 0 until entries.length()) {
            val item = entries.optJSONObject(index) ?: continue
            val path = item.optString("path")
            if (path.isBlank()) continue
            out += ManifestEntry(
                path = path,
                name = item.optString("name").ifBlank { path.substringAfterLast('/') },
                type = item.optString("type"),
                size = item.optLong("size"),
                mtime = item.optLong("mtime"),
                hash = item.optString("hash"),
                locations = parseLocations(item.optJSONArray("locations")),
                desiredLocations = parseLocations(item.optJSONArray("desired_locations")),
                staleLocations = item.optJSONArray("stale_locations")?.let { values ->
                    (0 until values.length()).map { values.optString(it) }
                } ?: emptyList(),
                removingLocations = item.optJSONArray("removing_locations")?.let { values ->
                    (0 until values.length()).map { values.optString(it) }
                } ?: emptyList(),
            )
        }
        return out
    }

    private fun parseLocations(locations: JSONArray?): List<StorageLocation> {
        if (locations == null) return emptyList()
        val out = mutableListOf<StorageLocation>()
        for (index in 0 until locations.length()) {
            val item = locations.optJSONObject(index) ?: continue
            out += StorageLocation(
                kind = item.optString("kind"),
                deviceId = item.optString("device_id"),
                name = item.optString("name"),
                available = item.optBoolean("available", true),
                size = item.optLong("size"),
                mtime = item.optLong("mtime"),
                hash = item.optString("hash"),
            )
        }
        return out
    }

    private fun parseJob(item: JSONObject?): ReconciliationJob? {
        item ?: return null
        val id = item.optString("job_id")
        if (id.isBlank()) return null
        val facts = item.optJSONObject("destination_facts")?.let {
            StorageLocation("", "", "", true, it.optLong("size"), it.optLong("mtime"), it.optString("hash"))
        }
        val removalFacts = mutableMapOf<String, StorageLocation>()
        item.optJSONObject("removal_facts")?.let { values ->
            for (path in values.keys()) {
                val value = values.optJSONObject(path) ?: continue
                removalFacts[path] = StorageLocation("", "", "", true, value.optLong("size"), value.optLong("mtime"), value.optString("hash"))
            }
        }
        return ReconciliationJob(
            id, item.optString("path"), item.optString("status"), item.optString("source_device_id"),
            item.optString("destination_device_id"), item.optLong("expected_size"), item.optLong("expected_mtime"),
            item.optString("hash"), facts, item.optBoolean("recursive"), removalFacts,
        )
    }

    private fun parseErrorMessage(body: String): String? {
        return runCatching {
            JSONObject(body).optString("error").takeIf { it.isNotBlank() }
        }.getOrNull()
    }

    private fun urlEncode(value: String): String = URLEncoder.encode(value, "UTF-8")
}

private fun InputStream.readFully(): ByteArray {
    val buffer = ByteArrayOutputStream()
    val chunk = ByteArray(1024 * 1024)
    while (true) {
        val read = read(chunk)
        if (read <= 0) break
        buffer.write(chunk, 0, read)
    }
    return buffer.toByteArray()
}
