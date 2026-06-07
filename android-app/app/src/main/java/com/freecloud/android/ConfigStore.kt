package com.freecloud.android

import android.content.Context

class ConfigStore(context: Context) {
    private val prefs = context.getSharedPreferences("freecloud_config", Context.MODE_PRIVATE)

    fun load(): FreeCloudConfig? {
        val baseUrl = prefs.getString("base_url", "")?.trim().orEmpty()
        val treeUri = prefs.getString("tree_uri", "")?.trim().orEmpty()
        if (baseUrl.isEmpty() || treeUri.isEmpty()) {
            return null
        }

        return FreeCloudConfig(
            domain = prefs.getString("domain", "")?.orEmpty().orEmpty(),
            driveName = prefs.getString("drive_name", "FreeCloud")?.orEmpty().orEmpty(),
            baseUrl = baseUrl,
            password = prefs.getString("password", "")?.orEmpty().orEmpty(),
            treeUri = treeUri,
            intervalSeconds = prefs.getInt("interval_seconds", 30),
            deleteRemote = prefs.getBoolean("delete_remote", false),
        )
    }

    fun save(config: FreeCloudConfig) {
        prefs.edit()
            .putString("domain", config.domain)
            .putString("drive_name", config.driveName)
            .putString("base_url", config.baseUrl)
            .putString("password", config.password)
            .putString("tree_uri", config.treeUri)
            .putInt("interval_seconds", config.intervalSeconds)
            .putBoolean("delete_remote", config.deleteRemote)
            .apply()
    }
}
