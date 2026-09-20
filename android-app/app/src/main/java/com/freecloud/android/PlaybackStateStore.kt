package com.freecloud.android

import android.content.Context

data class PlaybackProgress(
    val path: String,
    val positionMs: Int,
)

class PlaybackStateStore(context: Context) {
    private val prefs = context.getSharedPreferences("freecloud_playback", Context.MODE_PRIVATE)

    fun load(): PlaybackProgress? {
        val path = prefs.getString(KEY_PATH, "")?.trim().orEmpty()
        if (path.isEmpty()) return null
        return PlaybackProgress(
            path = path,
            positionMs = prefs.getInt(KEY_POSITION_MS, 0),
        )
    }

    fun save(path: String, positionMs: Int) {
        prefs.edit()
            .putString(KEY_PATH, path)
            .putInt(KEY_POSITION_MS, positionMs.coerceAtLeast(0))
            .apply()
    }

    fun clear() {
        prefs.edit().clear().apply()
    }

    private companion object {
        const val KEY_PATH = "path"
        const val KEY_POSITION_MS = "position_ms"
    }
}
