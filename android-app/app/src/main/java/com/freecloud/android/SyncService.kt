package com.freecloud.android

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.Service
import android.content.Context
import android.content.Intent
import android.os.Build
import android.os.IBinder
import androidx.core.app.NotificationCompat

class SyncService : Service() {
    private val configStore by lazy { ConfigStore(this) }
    private val engine by lazy { SyncEngine(this) }

    @Volatile
    private var stopRequested = false

    @Volatile
    private var workerGeneration = 0

    private var workerThread: Thread? = null

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (intent?.action == ACTION_STOP) {
            stopSyncLoop("Stopped")
            return START_NOT_STICKY
        }

        val config = configStore.load() ?: run {
            stopSelf()
            return START_NOT_STICKY
        }

        if (workerThread?.isAlive == true) {
            return START_STICKY
        }

        startForeground(NOTIFICATION_ID, buildNotification("Starting sync..."))
        stopRequested = false
        val generation = ++workerGeneration
        SyncRuntime.running = true
        publishStatus(true, "Starting sync...")

        workerThread = Thread {
            while (!stopRequested && generation == workerGeneration) {
                try {
                    val counts = engine.syncOnce(config) { progress ->
                        publishStatus(true, progress)
                        updateNotification(progress)
                    }
                    val message = counts.summary()
                    publishStatus(true, message)
                    updateNotification(message)
                } catch (_: InterruptedException) {
                    break
                } catch (exc: Exception) {
                    val message = exc.message ?: "Sync failed."
                    publishStatus(true, "Sync error: $message")
                    updateNotification("Sync error")
                }

                var interrupted = false
                repeat(config.intervalSeconds) {
                    if (stopRequested || generation != workerGeneration) return@repeat
                    try {
                        Thread.sleep(1_000)
                    } catch (_: InterruptedException) {
                        interrupted = true
                        return@repeat
                    }
                }
                if (interrupted) break
            }

            SyncRuntime.running = false
            workerThread = null
            publishStatus(false, "Stopped")
            stopForeground(STOP_FOREGROUND_REMOVE)
            stopSelf()
        }.apply { start() }

        return START_STICKY
    }

    override fun onDestroy() {
        stopRequested = true
        workerGeneration += 1
        workerThread?.interrupt()
        workerThread = null
        SyncRuntime.running = false
        super.onDestroy()
    }

    private fun stopSyncLoop(message: String) {
        stopRequested = true
        workerGeneration += 1
        workerThread?.interrupt()
        workerThread = null
        SyncRuntime.running = false
        publishStatus(false, message)
        stopForeground(STOP_FOREGROUND_REMOVE)
        stopSelf()
    }

    private fun publishStatus(running: Boolean, message: String) {
        SyncRuntime.running = running
        SyncRuntime.lastMessage = message
        sendBroadcast(Intent(SyncRuntime.ACTION_STATUS).apply {
            setPackage(packageName)
            putExtra(SyncRuntime.EXTRA_RUNNING, running)
            putExtra(SyncRuntime.EXTRA_MESSAGE, message)
        })
    }

    private fun buildNotification(message: String): Notification {
        val manager = getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            manager.createNotificationChannel(
                NotificationChannel(
                    CHANNEL_ID,
                    "FreeCloud Sync",
                    NotificationManager.IMPORTANCE_LOW,
                ),
            )
        }

        return NotificationCompat.Builder(this, CHANNEL_ID)
            .setContentTitle("FreeCloud Sync")
            .setContentText(message)
            .setSmallIcon(android.R.drawable.stat_notify_sync)
            .setOngoing(true)
            .build()
    }

    private fun updateNotification(message: String) {
        val manager = getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        manager.notify(NOTIFICATION_ID, buildNotification(message))
    }

    companion object {
        private const val CHANNEL_ID = "freecloud_sync"
        private const val NOTIFICATION_ID = 1001
        const val ACTION_START = "com.freecloud.android.action.START"
        const val ACTION_STOP = "com.freecloud.android.action.STOP"

        fun start(context: Context) {
            val intent = Intent(context, SyncService::class.java).apply { action = ACTION_START }
            androidx.core.content.ContextCompat.startForegroundService(context, intent)
        }

        fun stop(context: Context) {
            val intent = Intent(context, SyncService::class.java).apply { action = ACTION_STOP }
            context.startService(intent)
        }
    }
}
