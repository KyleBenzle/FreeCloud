package com.freecloud.android

import android.content.Context
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import android.util.Base64
import java.security.KeyStore
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec

class ConfigStore(context: Context) {
    private val prefs = context.getSharedPreferences("freecloud_config", Context.MODE_PRIVATE)
    private val keyStore = KeyStore.getInstance("AndroidKeyStore").apply { load(null) }

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
            password = loadPassword(),
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
            .putString("tree_uri", config.treeUri)
            .putInt("interval_seconds", config.intervalSeconds)
            .putBoolean("delete_remote", config.deleteRemote)
            .remove(LEGACY_PASSWORD_KEY)
            .apply()
        savePassword(config.password)
    }

    private fun loadPassword(): String {
        val encrypted = prefs.getString(PASSWORD_KEY, null)
        val iv = prefs.getString(PASSWORD_IV_KEY, null)
        if (!encrypted.isNullOrEmpty() && !iv.isNullOrEmpty()) {
            return try {
                val cipher = Cipher.getInstance(TRANSFORMATION)
                cipher.init(
                    Cipher.DECRYPT_MODE,
                    secretKey(),
                    GCMParameterSpec(128, Base64.decode(iv, Base64.NO_WRAP)),
                )
                String(cipher.doFinal(Base64.decode(encrypted, Base64.NO_WRAP)), Charsets.UTF_8)
            } catch (_: Exception) {
                ""
            }
        }

        val legacyPassword = prefs.getString(LEGACY_PASSWORD_KEY, "")?.orEmpty().orEmpty()
        if (legacyPassword.isNotEmpty()) {
            savePassword(legacyPassword)
        }
        return legacyPassword
    }

    private fun savePassword(password: String) {
        if (password.isEmpty()) {
            prefs.edit()
                .remove(PASSWORD_KEY)
                .remove(PASSWORD_IV_KEY)
                .remove(LEGACY_PASSWORD_KEY)
                .apply()
            return
        }

        val cipher = Cipher.getInstance(TRANSFORMATION)
        cipher.init(Cipher.ENCRYPT_MODE, secretKey())
        val encrypted = cipher.doFinal(password.toByteArray(Charsets.UTF_8))
        prefs.edit()
            .putString(PASSWORD_KEY, Base64.encodeToString(encrypted, Base64.NO_WRAP))
            .putString(PASSWORD_IV_KEY, Base64.encodeToString(cipher.iv, Base64.NO_WRAP))
            .remove(LEGACY_PASSWORD_KEY)
            .apply()
    }

    private fun secretKey(): SecretKey {
        val existing = keyStore.getKey(KEY_ALIAS, null) as? SecretKey
        if (existing != null) {
            return existing
        }

        val generator = KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, "AndroidKeyStore")
        generator.init(
            KeyGenParameterSpec.Builder(
                KEY_ALIAS,
                KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT,
            )
                .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
                .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
                .build(),
        )
        return generator.generateKey()
    }

    private companion object {
        const val KEY_ALIAS = "freecloud_config_key"
        const val LEGACY_PASSWORD_KEY = "password"
        const val PASSWORD_KEY = "password_encrypted"
        const val PASSWORD_IV_KEY = "password_iv"
        const val TRANSFORMATION = "AES/GCM/NoPadding"
    }
}
