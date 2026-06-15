package com.freecloud.android

import org.junit.Assert.assertEquals
import org.junit.Test

class UrlToolsTest {
    @Test
    fun buildSetupUrlsUsesProvidedFolderForBareDomain() {
        val result = UrlTools.buildSetupUrls("example.com", "FreeCloud")

        assertEquals("https://example.com", result.first)
        assertEquals("FreeCloud", result.second)
        assertEquals("https://example.com/FreeCloud", result.third)
    }

    @Test
    fun buildSetupUrlsReusesPathWhenAppAlreadyLivesInSubfolder() {
        val result = UrlTools.buildSetupUrls("https://example.com/freecloud", "FreeCloud")

        assertEquals("https://example.com/freecloud", result.first)
        assertEquals("freecloud", result.second)
        assertEquals("https://example.com/freecloud", result.third)
    }

    @Test
    fun remotePathStripsTraversalAndNormalizesSlashes() {
        assertEquals("photos/2026/cat.jpg", UrlTools.remotePath("./photos\\2026/../2026/cat.jpg"))
    }

    @Test(expected = IllegalArgumentException::class)
    fun normalizeDriveNameRejectsNestedPaths() {
        UrlTools.normalizeDriveName("parent/child")
    }

    @Test(expected = IllegalArgumentException::class)
    fun normalizeDomainRejectsRemotePlainHttp() {
        UrlTools.normalizeDomain("http://example.com")
    }
}
