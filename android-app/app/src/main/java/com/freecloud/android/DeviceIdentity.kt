package com.freecloud.android

import java.util.UUID

data class DeviceIdentity(
    val id: String,
    val name: String,
    val type: String = "android",
)

object DeviceIdentityDefaults {
    fun stableId(existing: String?, generate: () -> String = { UUID.randomUUID().toString().replace("-", "") }): String {
        return existing?.trim().takeUnless { it.isNullOrEmpty() } ?: generate()
    }

    fun defaultName(manufacturer: String, model: String): String {
        val cleanManufacturer = manufacturer.trim()
        val cleanModel = model.trim()
        if (cleanModel.isEmpty()) return "Android Device"
        if (cleanManufacturer.isEmpty() || cleanModel.startsWith(cleanManufacturer, ignoreCase = true)) return cleanModel
        return "$cleanManufacturer $cleanModel"
    }
}
