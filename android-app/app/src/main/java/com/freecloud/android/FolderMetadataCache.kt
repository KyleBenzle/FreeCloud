package com.freecloud.android

import android.content.Context
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.security.MessageDigest

/** Small per-folder cache so browsing never waits for a device scan or network. */
class FolderMetadataCache(private val directory: File) {
    constructor(context: Context) : this(File(context.filesDir, "freecloud_folder_cache"))

    @Synchronized
    fun load(folder: String): List<ManifestEntry>? {
        val file = fileFor(folder)
        if (!file.isFile()) return null
        val root = runCatching { JSONObject(file.readText(Charsets.UTF_8)) }.getOrNull() ?: return null
        return decodeEntries(root.optJSONArray("entries"))
    }

    @Synchronized
    fun save(folder: String, entries: List<ManifestEntry>) {
        directory.mkdirs()
        val file = fileFor(folder)
        val root = JSONObject().put("version", 1).put("path", UrlTools.remotePath(folder))
            .put("updated_at", System.currentTimeMillis()).put("entries", encodeEntries(entries))
        val temp = File(directory, "${file.name}.tmp")
        temp.writeText(root.toString(), Charsets.UTF_8)
        if (!temp.renameTo(file)) {
            file.writeText(temp.readText(Charsets.UTF_8), Charsets.UTF_8)
            temp.delete()
        }
    }

    private fun fileFor(folder: String): File {
        val key = MessageDigest.getInstance("SHA-256").digest(UrlTools.remotePath(folder).toByteArray())
            .joinToString("") { "%02x".format(it) }
        return File(directory, "$key.json")
    }

    internal fun encodeEntries(entries: List<ManifestEntry>): JSONArray = JSONArray().also { array ->
        entries.forEach { entry ->
            array.put(
                JSONObject()
                    .put("path", entry.path).put("name", entry.name).put("type", entry.type)
                    .put("size", entry.size).put("mtime", entry.mtime).put("hash", entry.hash)
                    .put("locations", encodeLocations(entry.locations))
                    .put("desired_locations", encodeLocations(entry.desiredLocations))
                    .put("stale_locations", JSONArray(entry.staleLocations))
                    .put("removing_locations", JSONArray(entry.removingLocations)),
            )
        }
    }

    internal fun decodeEntries(array: JSONArray?): List<ManifestEntry> {
        if (array == null) return emptyList()
        return (0 until array.length()).mapNotNull { index ->
            val item = array.optJSONObject(index) ?: return@mapNotNull null
            val path = item.optString("path")
            if (path.isBlank()) return@mapNotNull null
            ManifestEntry(
                path, item.optString("name").ifBlank { path.substringAfterLast('/') }, item.optString("type"),
                item.optLong("size"), item.optLong("mtime"), item.optString("hash"),
                decodeLocations(item.optJSONArray("locations")), decodeLocations(item.optJSONArray("desired_locations")),
                decodeStrings(item.optJSONArray("stale_locations")), decodeStrings(item.optJSONArray("removing_locations")),
            )
        }
    }

    private fun encodeLocations(locations: List<StorageLocation>): JSONArray = JSONArray().also { array ->
        locations.forEach { location ->
            array.put(
                JSONObject().put("kind", location.kind).put("device_id", location.deviceId)
                    .put("name", location.name).put("available", location.available)
                    .put("size", location.size).put("mtime", location.mtime).put("hash", location.hash),
            )
        }
    }

    private fun decodeLocations(array: JSONArray?): List<StorageLocation> {
        if (array == null) return emptyList()
        return (0 until array.length()).mapNotNull { index ->
            val item = array.optJSONObject(index) ?: return@mapNotNull null
            StorageLocation(
                item.optString("kind"), item.optString("device_id"), item.optString("name"),
                item.optBoolean("available", true), item.optLong("size"), item.optLong("mtime"), item.optString("hash"),
            )
        }
    }

    private fun decodeStrings(array: JSONArray?): List<String> =
        if (array == null) emptyList() else (0 until array.length()).map { array.optString(it) }
}
