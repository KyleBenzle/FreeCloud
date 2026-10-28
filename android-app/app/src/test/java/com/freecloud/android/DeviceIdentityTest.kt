package com.freecloud.android

import org.junit.Assert.assertEquals
import org.junit.Test

class DeviceIdentityTest {
    @Test
    fun stableIdReusesPersistedValue() {
        assertEquals("saved-id", DeviceIdentityDefaults.stableId("saved-id") { "new-id" })
    }

    @Test
    fun stableIdGeneratesValueOnlyWhenMissing() {
        assertEquals("new-id", DeviceIdentityDefaults.stableId("") { "new-id" })
    }

    @Test
    fun defaultNameUsesManufacturerAndModel() {
        assertEquals("Google Pixel 7a", DeviceIdentityDefaults.defaultName("Google", "Pixel 7a"))
        assertEquals("Samsung Galaxy", DeviceIdentityDefaults.defaultName("Samsung", "Samsung Galaxy"))
    }
}
