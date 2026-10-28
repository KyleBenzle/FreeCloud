package com.freecloud.android

import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Assert.assertEquals
import org.junit.Test

class ReconciliationSafetyTest {
    @Test fun verifiesSha256() {
        assertEquals("ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad", ReconciliationSafety.sha256("abc".toByteArray()))
    }

    @Test fun absentDestinationRequiresAbsentPlan() {
        assertTrue(ReconciliationSafety.destinationMayInstall(false, 0, 0, null))
        assertFalse(ReconciliationSafety.destinationMayInstall(true, 3, 10, null))
    }

    @Test fun changedDestinationIsRejected() {
        val planned = StorageLocation("android", "phone", "Phone", true, 3, 100)
        assertTrue(ReconciliationSafety.destinationMayInstall(true, 3, 101, planned))
        assertFalse(ReconciliationSafety.destinationMayInstall(true, 4, 101, planned))
        assertFalse(ReconciliationSafety.destinationMayInstall(true, 3, 110, planned))
    }

    @Test fun desiredManagedRulesGateLegacySyncWritesAndDeletesRecursively() {
        val rules = listOf(PlacementRule("phone", "Documents", true, true), PlacementRule("phone", "one.txt", true, false))
        assertTrue(ReconciliationSafety.isManaged("Documents/report.pdf", rules))
        assertTrue(ReconciliationSafety.isManaged("Documents/sub/deep.pdf", rules))
        assertTrue(ReconciliationSafety.isManaged("one.txt", rules))
        assertFalse(ReconciliationSafety.isManaged("other.txt", rules))
    }

    @Test fun rootPlacementRuleOwnsAllDescendantsEvenWithNarrowerLegacyRule() {
        val rules = listOf(PlacementRule("phone", "", true, true), PlacementRule("phone", "Documents", false, true))
        assertTrue(ReconciliationSafety.isManaged("Documents/report.pdf", rules))
        assertTrue(ReconciliationSafety.isManaged("other.txt", rules))
    }

    @Test fun catalogDefaultPlacementAlsoExcludesPathFromLegacySync() {
        assertTrue(ReconciliationSafety.isManaged("new/file.txt", emptyList(), setOf("new/file.txt")))
        assertFalse(ReconciliationSafety.isManaged("legacy.txt", emptyList(), setOf("new/file.txt")))
    }
}
