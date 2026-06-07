package com.freecloud.android

import java.net.URI

object UrlTools {
    fun normalizeDomain(value: String): String {
        var domain = value.trim().removeSuffix("/")
        require(domain.isNotEmpty()) { "Domain is required." }
        if (!domain.startsWith("http://") && !domain.startsWith("https://")) {
            domain = "https://$domain"
        }
        return domain.removeSuffix("/")
    }

    fun normalizeDriveName(value: String): String {
        val parts = value.trim().trim('/').replace("\\", "/")
            .split("/")
            .filter { it.isNotBlank() && it != "." && it != ".." }
        require(parts.size == 1) { "Use one public_html folder name, like FreeCloud." }
        return parts.first()
    }

    fun remotePath(value: String): String {
        // Keep client paths deterministic before the PHP endpoint applies its
        // own path guard. Parent segments back up one accepted segment.
        val parts = mutableListOf<String>()
        for (part in value.replace("\\", "/").split("/").map { it.trim() }) {
            when {
                part.isEmpty() || part == "." -> Unit
                part == ".." && parts.isNotEmpty() -> parts.removeAt(parts.lastIndex)
                part != ".." -> parts += part
            }
        }
        return parts.joinToString("/")
    }

    fun buildSetupUrls(domainText: String, driveText: String): Triple<String, String, String> {
        val domain = normalizeDomain(domainText)
        var driveName = normalizeDriveName(driveText)
        val uri = URI(domain)
        val domainPath = uri.path?.trim('/') ?: ""

        if (domainPath.isNotEmpty()) {
            if (driveText.trim().isEmpty() || driveText.trim() == "FreeCloud") {
                driveName = domainPath.substringAfterLast('/')
            }
            return Triple(domain, driveName, domain)
        }

        return Triple(domain, driveName, "$domain/$driveName")
    }
}
