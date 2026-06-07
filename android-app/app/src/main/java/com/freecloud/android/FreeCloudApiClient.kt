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

    private fun openConnection(url: String, method: String, contentType: String?): HttpURLConnection {
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
            )
        }
        return out
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
