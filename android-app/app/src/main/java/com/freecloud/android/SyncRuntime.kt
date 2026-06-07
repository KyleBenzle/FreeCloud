package com.freecloud.android

object SyncRuntime {
    const val ACTION_STATUS = "com.freecloud.android.SYNC_STATUS"
    const val EXTRA_RUNNING = "running"
    const val EXTRA_MESSAGE = "message"

    @Volatile
    var running: Boolean = false

    @Volatile
    var lastMessage: String = "Stopped"
}
