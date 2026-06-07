package com.freecloud.android

import android.content.Context
import org.json.JSONObject
import java.io.File

class SyncStateStore(private val context: Context) {
    private val file = File(context.filesDir, "freecloud_sync_state.json")

    fun load(): SyncState {
        if (!file.isFile()) return SyncState()
        val raw = runCatching { file.readText(Charsets.UTF_8) }.getOrDefault("")
        if (raw.isBlank()) return SyncState()
        val root = runCatching { JSONObject(raw) }.getOrNull() ?: return SyncState()
        return SyncState(
            local = parseMap(root.optJSONObject("local")),
            remote = parseMap(root.optJSONObject("remote")),
        )
    }

    fun save(state: SyncState) {
        val root = JSONObject()
        root.put("local", mapToJson(state.local))
        root.put("remote", mapToJson(state.remote))
        file.writeText(root.toString(), Charsets.UTF_8)
    }

    private fun parseMap(source: JSONObject?): Map<String, ManifestEntry> {
        if (source == null) return emptyMap()
        val out = linkedMapOf<String, ManifestEntry>()
        for (key in source.keys()) {
            val item = source.optJSONObject(key) ?: continue
            out[key] = ManifestEntry(
                path = item.optString("path"),
                name = item.optString("name"),
                type = item.optString("type"),
                size = item.optLong("size"),
                mtime = item.optLong("mtime"),
            )
        }
        return out
    }

    private fun mapToJson(source: Map<String, ManifestEntry>): JSONObject {
        val out = JSONObject()
        for ((path, entry) in source) {
            out.put(
                path,
                JSONObject()
                    .put("path", entry.path)
                    .put("name", entry.name)
                    .put("type", entry.type)
                    .put("size", entry.size)
                    .put("mtime", entry.mtime),
            )
        }
        return out
    }
}
