package com.freecloud.android

import android.Manifest
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.content.pm.PackageManager
import android.media.MediaPlayer
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.text.SpannableString
import android.text.SpannableStringBuilder
import android.text.method.ScrollingMovementMethod
import android.text.style.ForegroundColorSpan
import android.text.style.StyleSpan
import android.view.LayoutInflater
import android.view.View
import android.widget.ImageButton
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.PopupMenu
import android.widget.TextView
import android.widget.Toast
import android.widget.VideoView
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.activity.OnBackPressedCallback
import androidx.core.content.FileProvider
import androidx.core.content.ContextCompat
import java.io.File
import kotlin.math.abs

class MainActivity : AppCompatActivity() {
    private lateinit var configStore: ConfigStore
    private lateinit var documentTreeSync: DocumentTreeSync
    private lateinit var syncStateStore: SyncStateStore
    private lateinit var playbackStateStore: PlaybackStateStore

    private lateinit var setupCard: LinearLayout
    private lateinit var runningCard: LinearLayout
    private lateinit var domainInput: EditText
    private lateinit var driveInput: EditText
    private lateinit var passwordInput: EditText
    private lateinit var folderPathText: TextView
    private lateinit var setupMessage: TextView
    private lateinit var statusText: TextView
    private lateinit var serverValue: TextView
    private lateinit var localFolderValue: TextView
    private lateinit var currentFolderText: TextView
    private lateinit var fileCountText: TextView
    private lateinit var playerSection: LinearLayout
    private lateinit var nowPlayingText: TextView
    private lateinit var playerProgressText: TextView
    private lateinit var videoPlayerView: VideoView
    private lateinit var playPauseButton: Button
    private lateinit var restartButton: Button
    private lateinit var nextButton: Button
    private lateinit var emptyFilesText: TextView
    private lateinit var filesRecyclerView: LinearLayout
    private lateinit var activitySection: LinearLayout
    private lateinit var logText: TextView
    private lateinit var saveSetupButton: Button
    private lateinit var upFolderButton: Button
    private lateinit var menuButton: ImageButton

    private var selectedTreeUri: Uri? = null
    private var statusReceiverRegistered = false
    private var currentFolderPath: String = ""
    private var currentPlayablePath: String = ""
    private var currentPlayableFolder: String = ""
    private var currentIsVideo: Boolean = false
    private var audioPlayer: MediaPlayer? = null
    private val progressHandler = Handler(Looper.getMainLooper())
    private val progressTicker = object : Runnable {
        override fun run() {
            updatePlayerProgress()
            progressHandler.postDelayed(this, 1_000)
        }
    }
    @Volatile
    private var cachedLocalNodes: Map<String, LocalNode> = emptyMap()
    @Volatile
    private var cachedRemoteState: Map<String, ManifestEntry> = emptyMap()
    @Volatile
    private var cachedFolderEntries: List<ManifestEntry> = emptyList()
    @Volatile
    private var refreshInFlight = false

    private val folderPicker = registerForActivityResult(ActivityResultContracts.OpenDocumentTree()) { uri ->
        if (uri == null) return@registerForActivityResult
        contentResolver.takePersistableUriPermission(
            uri,
            Intent.FLAG_GRANT_READ_URI_PERMISSION or Intent.FLAG_GRANT_WRITE_URI_PERMISSION,
        )
        selectedTreeUri = uri
        folderPathText.text = "Phone folder: $uri"
        cachedLocalNodes = emptyMap()
        cachedRemoteState = emptyMap()
        refreshFileList(forceRescan = true)
    }

    private val notificationPermissionRequester =
        registerForActivityResult(ActivityResultContracts.RequestPermission()) { granted ->
            if (!granted) {
                appendLog("Notification permission denied. Sync notifications may be hidden.")
            }
        }

    private val editorLauncher = registerForActivityResult(ActivityResultContracts.StartActivityForResult()) {
        refreshFileList(forceRescan = true)
    }

    private val statusReceiver = object : BroadcastReceiver() {
        override fun onReceive(context: Context?, intent: Intent?) {
            if (intent?.action != SyncRuntime.ACTION_STATUS) return
            val running = intent.getBooleanExtra(SyncRuntime.EXTRA_RUNNING, false)
            val message = intent.getStringExtra(SyncRuntime.EXTRA_MESSAGE).orEmpty()
            applyStatus(running, message)
            if (shouldForceRefreshForStatus(message)) {
                refreshFileList(forceRescan = true)
            } else if (cachedLocalNodes.isNotEmpty()) {
                renderCurrentFolderFromCache()
            }
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)

        configStore = ConfigStore(this)
        documentTreeSync = DocumentTreeSync(this)
        syncStateStore = SyncStateStore(this)
        playbackStateStore = PlaybackStateStore(this)
        bindViews()
        wireActions()
        requestNotificationPermissionIfNeeded()
        registerBackHandler()

        val config = configStore.load()
        if (config != null) {
            selectedTreeUri = Uri.parse(config.treeUri)
            populateFromConfig(config)
            showRunningCard()
            refreshFileList(forceRescan = true)
        } else {
            showSetupCard()
        }

        applyStatus(SyncRuntime.running, SyncRuntime.lastMessage)
    }

    override fun onStart() {
        super.onStart()
        registerStatusReceiver()
    }

    override fun onStop() {
        saveCurrentPlaybackPosition()
        if (statusReceiverRegistered) {
            unregisterReceiver(statusReceiver)
            statusReceiverRegistered = false
        }
        super.onStop()
    }

    override fun onDestroy() {
        progressHandler.removeCallbacks(progressTicker)
        audioPlayer?.release()
        audioPlayer = null
        videoPlayerView.stopPlayback()
        super.onDestroy()
    }

    private fun bindViews() {
        setupCard = findViewById(R.id.setupCard)
        runningCard = findViewById(R.id.runningCard)
        domainInput = findViewById(R.id.domainInput)
        driveInput = findViewById(R.id.driveInput)
        passwordInput = findViewById(R.id.passwordInput)
        folderPathText = findViewById(R.id.folderPathText)
        setupMessage = findViewById(R.id.setupMessage)
        statusText = findViewById(R.id.statusText)
        serverValue = findViewById(R.id.serverValue)
        localFolderValue = findViewById(R.id.localFolderValue)
        currentFolderText = findViewById(R.id.currentFolderText)
        fileCountText = findViewById(R.id.fileCountText)
        playerSection = findViewById(R.id.playerSection)
        nowPlayingText = findViewById(R.id.nowPlayingText)
        playerProgressText = findViewById(R.id.playerProgressText)
        videoPlayerView = findViewById(R.id.videoPlayerView)
        playPauseButton = findViewById(R.id.playPauseButton)
        restartButton = findViewById(R.id.restartButton)
        nextButton = findViewById(R.id.nextButton)
        emptyFilesText = findViewById(R.id.emptyFilesText)
        filesRecyclerView = findViewById(R.id.filesRecyclerView)
        activitySection = findViewById(R.id.activitySection)
        logText = findViewById(R.id.logText)
        saveSetupButton = findViewById(R.id.saveSetupButton)
        upFolderButton = findViewById(R.id.upFolderButton)
        menuButton = findViewById(R.id.menuButton)

        logText.movementMethod = ScrollingMovementMethod()
    }

    private fun wireActions() {
        findViewById<Button>(R.id.pickFolderButton).setOnClickListener {
            folderPicker.launch(selectedTreeUri)
        }

        saveSetupButton.setOnClickListener { saveSetup() }
        upFolderButton.setOnClickListener {
            if (currentFolderPath.isBlank()) return@setOnClickListener
            currentFolderPath = currentFolderPath.substringBeforeLast('/', "")
            refreshFileList(forceRescan = true)
        }
        menuButton.setOnClickListener { showOverflowMenu() }
        playPauseButton.setOnClickListener { togglePlayback() }
        restartButton.setOnClickListener { seekToStart() }
        nextButton.setOnClickListener { playNextInFolder() }
    }

    private fun registerBackHandler() {
        onBackPressedDispatcher.addCallback(this, object : OnBackPressedCallback(true) {
            override fun handleOnBackPressed() {
                if (runningCard.visibility == View.VISIBLE && currentFolderPath.isNotBlank()) {
                    currentFolderPath = currentFolderPath.substringBeforeLast('/', "")
                    refreshFileList(forceRescan = true)
                    return
                }
                isEnabled = false
                onBackPressedDispatcher.onBackPressed()
            }
        })
    }

    private fun saveSetup() {
        val treeUri = selectedTreeUri
        if (treeUri == null) {
            setupMessage.setTextColor(getColorCompat(R.color.freecloud_danger))
            setupMessage.text = "Choose a phone folder first."
            return
        }

        val domainText = domainInput.text.toString()
        val driveText = driveInput.text.toString()
        val password = passwordInput.text.toString()

        saveSetupButton.isEnabled = false
        setupMessage.setTextColor(getColorCompat(R.color.freecloud_muted))
        setupMessage.text = "Connecting..."

        Thread {
            try {
                val (domain, driveName, baseUrl) = UrlTools.buildSetupUrls(domainText, driveText)
                val client = FreeCloudApiClient(baseUrl, password)
                val ping = try {
                    client.ping()
                } catch (exc: FreeCloudApiException) {
                    if (exc.code == 409) null else throw exc
                }

                if (ping == null || !ping.optBoolean("setup", false)) {
                    client.setup(driveName, password)
                }

                val config = FreeCloudConfig(
                    domain = domain,
                    driveName = driveName,
                    baseUrl = baseUrl,
                    password = password,
                    treeUri = treeUri.toString(),
                )

                configStore.save(config)

                runOnUiThread {
                    populateFromConfig(config)
                    showRunningCard()
                    setupMessage.text = ""
                    saveSetupButton.isEnabled = true
                    appendLog("Saved setup for ${config.baseUrl}")
                    refreshFileList(forceRescan = true)
                }
            } catch (exc: Exception) {
                runOnUiThread {
                    saveSetupButton.isEnabled = true
                    setupMessage.setTextColor(getColorCompat(R.color.freecloud_danger))
                    setupMessage.text = exc.message ?: "Could not save setup."
                }
            }
        }.start()
    }

    private fun populateFromConfig(config: FreeCloudConfig) {
        domainInput.setText(config.domain)
        driveInput.setText(config.driveName)
        passwordInput.setText(config.password)
        folderPathText.text = "Phone folder: ${config.treeUri}"
        serverValue.text = boldLabelValue("Server:", config.baseUrl)
        localFolderValue.text = boldLabelValue("Local folder:", summarizeTreeUri(config.treeUri))
        currentFolderText.text = "Folder: /"
    }

    private fun showSetupCard() {
        setupCard.visibility = View.VISIBLE
        runningCard.visibility = View.GONE
        menuButton.visibility = View.GONE
    }

    private fun showRunningCard() {
        setupCard.visibility = View.GONE
        runningCard.visibility = View.VISIBLE
        menuButton.visibility = View.VISIBLE
    }

    private fun applyStatus(running: Boolean, message: String) {
        statusText.text = if (running) "Running" else "Stopped"
        statusText.setTextColor(getColorCompat(if (running) R.color.freecloud_success else R.color.freecloud_blue_dark))
        if (message.isNotBlank()) {
            appendLog(message)
        }
    }

    private fun appendLog(message: String) {
        val current = logText.text?.toString().orEmpty()
        val updated = if (current.isBlank()) message else "$message\n$current"
        logText.text = updated
    }

    private fun refreshFileList(forceRescan: Boolean) {
        val config = configStore.load() ?: return
        if (!forceRescan && cachedLocalNodes.isNotEmpty() && cachedFolderEntries.isNotEmpty()) {
            renderCurrentFolderFromCache()
            return
        }
        if (refreshInFlight) return
        refreshInFlight = true
        Thread {
            val client = FreeCloudApiClient(config.baseUrl, config.password)
            val remoteState = runCatching {
                client.metadataList(currentFolderPath)
            }.recoverCatching {
                syncStateStore.load().remote.values
                    .filter { entry -> isDirectChild(currentFolderPath, entry.path) }
            }
            val fileMapResult = runCatching { documentTreeSync.scanTree(config.treeUri) }
            if (fileMapResult.isFailure || remoteState.isFailure) {
                val error = fileMapResult.exceptionOrNull()
                    ?: remoteState.exceptionOrNull()
                runOnUiThread {
                    refreshInFlight = false
                    fileCountText.text = "Could not load files"
                    emptyFilesText.visibility = View.VISIBLE
                    emptyFilesText.text = error?.message ?: "Could not load the FreeCloud file list."
                    renderFileItems(emptyList())
                }
                return@Thread
            }

            val folderEntries = remoteState.getOrThrow()
            cachedFolderEntries = folderEntries
            cachedRemoteState = folderEntries.associateBy { it.path }
            cachedLocalNodes = fileMapResult.getOrThrow()

            runOnUiThread {
                refreshInFlight = false
                renderCurrentFolderFromCache()
            }
        }.start()
    }

    private fun openItem(item: SyncedFileItem) {
        val config = configStore.load() ?: return
        if (item.isDirectory) {
            currentFolderPath = item.path
            refreshFileList(forceRescan = true)
            return
        }

        if (item.isPlayable) {
            playMediaItem(item)
            return
        }

        if (item.hasLocalCopy && documentTreeSync.isTextFile(item.path)) {
            val intent = Intent(this, FileEditorActivity::class.java)
                .putExtra(FileEditorActivity.EXTRA_TREE_URI, config.treeUri)
                .putExtra(FileEditorActivity.EXTRA_PATH, item.path)
            editorLauncher.launch(intent)
            return
        }

        if (!item.hasLocalCopy && !item.hasWebServerCopy) {
            Toast.makeText(this, "${item.name} is on another device. Pi streaming is the next server feature.", Toast.LENGTH_LONG).show()
            return
        }

        if (!item.hasLocalCopy) {
            Toast.makeText(this, "This file is available from the server but is not downloaded to this phone yet.", Toast.LENGTH_LONG).show()
            return
        }

        val document = documentTreeSync.findDocument(config.treeUri, item.path)
        if (document == null) {
            Toast.makeText(this, "Could not open ${item.name}.", Toast.LENGTH_LONG).show()
            return
        }

        val contentType = documentTreeSync.contentTypeForPath(item.path)
        val intent = Intent(Intent.ACTION_VIEW).apply {
            setDataAndType(document.uri, contentType)
            addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
        }
        try {
            startActivity(intent)
        } catch (_: Exception) {
            Toast.makeText(this, "No app found to open ${item.name}.", Toast.LENGTH_LONG).show()
        }
    }

    private fun registerStatusReceiver() {
        if (statusReceiverRegistered) return
        val filter = IntentFilter(SyncRuntime.ACTION_STATUS)
        ContextCompat.registerReceiver(
            this,
            statusReceiver,
            filter,
            ContextCompat.RECEIVER_NOT_EXPORTED,
        )
        statusReceiverRegistered = true
    }

    private fun requestNotificationPermissionIfNeeded() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.TIRAMISU) return
        if (checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) == PackageManager.PERMISSION_GRANTED) return
        notificationPermissionRequester.launch(Manifest.permission.POST_NOTIFICATIONS)
    }

    private fun roughlyEqual(current: ManifestEntry, previous: ManifestEntry?): Boolean {
        if (previous == null) return false
        if (current.type != previous.type) return false
        if (current.isDirectory) return true
        return current.size == previous.size && abs(current.mtime - previous.mtime) <= 2_000
    }

    private fun isDirectChild(parentPath: String, path: String): Boolean {
        if (parentPath.isBlank()) return !path.contains('/')
        if (!path.startsWith("$parentPath/")) return false
        val remainder = path.removePrefix("$parentPath/")
        return remainder.isNotBlank() && !remainder.contains('/')
    }

    private fun itemSubtitle(node: LocalNode): String {
        return if (node.entry.isDirectory) {
            val childCount = cachedLocalNodes.keys.count { isDirectChild(node.entry.path, it) }
            if (childCount == 1) "1 item" else "$childCount items"
        } else {
            val sizeKb = (node.entry.size + 1023) / 1024
            if (sizeKb <= 1) "File" else "${sizeKb} KB"
        }
    }

    private fun itemSubtitle(entry: ManifestEntry, localNode: LocalNode?, remoteEntry: ManifestEntry?): String {
        if (localNode != null && remoteEntry == null) return itemSubtitle(localNode)
        val prefix = if (entry.isDirectory) {
            "Folder"
        } else {
            val sizeMb = entry.size.toDouble() / (1024.0 * 1024.0)
            if (sizeMb >= 1.0) String.format("%.1f MB", sizeMb) else "${(entry.size + 1023) / 1024} KB"
        }
        val location = remoteEntry?.availableFromLabel()
        val local = if (localNode != null) "On phone" else null
        return listOfNotNull(prefix, location, local).joinToString(" • ")
    }

    private fun isMediaFile(path: String): Boolean {
        return path.substringAfterLast('.', "").lowercase() in setOf(
            "mp3", "m4a", "aac", "wav", "flac", "ogg", "opus",
            "mp4", "m4v", "mov", "webm", "3gp", "mkv",
        )
    }

    private fun isVideoFile(path: String): Boolean {
        return path.substringAfterLast('.', "").lowercase() in setOf(
            "mp4", "m4v", "mov", "webm", "3gp", "mkv",
        )
    }

    private fun playMediaItem(item: SyncedFileItem, resumeMs: Int? = null) {
        val config = configStore.load() ?: return
        saveCurrentPlaybackPosition()
        currentPlayablePath = item.path
        currentPlayableFolder = item.path.substringBeforeLast('/', "")
        currentIsVideo = isVideoFile(item.path)
        playerSection.visibility = View.VISIBLE
        videoPlayerView.visibility = if (currentIsVideo) View.VISIBLE else View.GONE
        nowPlayingText.text = item.name
        playerProgressText.text = "Loading..."
        playPauseButton.text = "Pause"

        val uri = if (item.hasLocalCopy) {
            val document = documentTreeSync.findDocument(config.treeUri, item.path)
            if (document == null) {
                Toast.makeText(this, "Could not open ${item.name}.", Toast.LENGTH_LONG).show()
                return
            }
            document.uri
        } else {
            if (!item.hasWebServerCopy) {
                Toast.makeText(this, "${item.name} is on another device. Pi streaming is the next server feature.", Toast.LENGTH_LONG).show()
                return
            }
            Uri.parse(FreeCloudApiClient(config.baseUrl, config.password).downloadUrl(item.path))
        }

        runCatching {
            val headers = FreeCloudApiClient(config.baseUrl, config.password).requestHeaders()
            if (currentIsVideo) {
                audioPlayer?.release()
                audioPlayer = null
                videoPlayerView.stopPlayback()
                videoPlayerView.setVideoURI(uri, headers)
                videoPlayerView.setOnPreparedListener { player ->
                    val saved = savedPositionFor(item.path, resumeMs)
                    if (saved > 0) {
                        videoPlayerView.seekTo(saved.coerceAtMost(player.duration.coerceAtLeast(0)))
                    }
                    videoPlayerView.start()
                    startProgressTicker()
                }
                videoPlayerView.setOnCompletionListener {
                    playbackStateStore.save(item.path, 0)
                    playNextInFolder()
                }
                videoPlayerView.setOnErrorListener { _, _, _ ->
                    Toast.makeText(this, "Could not play ${item.name}.", Toast.LENGTH_LONG).show()
                    playPauseButton.text = "Play"
                    true
                }
            } else {
                videoPlayerView.stopPlayback()
                audioPlayer?.release()
                audioPlayer = MediaPlayer().apply {
                    setDataSource(this@MainActivity, uri, headers)
                    setOnPreparedListener { player ->
                        val saved = savedPositionFor(item.path, resumeMs)
                        if (saved > 0) {
                            player.seekTo(saved.coerceAtMost(player.duration.coerceAtLeast(0)))
                        }
                        player.start()
                        startProgressTicker()
                    }
                    setOnCompletionListener {
                        playbackStateStore.save(item.path, 0)
                        playNextInFolder()
                    }
                    setOnErrorListener { _, _, _ ->
                        Toast.makeText(this@MainActivity, "Could not play ${item.name}.", Toast.LENGTH_LONG).show()
                        playPauseButton.text = "Play"
                        true
                    }
                    prepareAsync()
                }
            }
        }.onFailure { exc ->
            Toast.makeText(this, exc.message ?: "Could not play ${item.name}.", Toast.LENGTH_LONG).show()
        }
    }

    private fun savedPositionFor(path: String, resumeMs: Int?): Int {
        return resumeMs ?: playbackStateStore.load()
            ?.takeIf { it.path == path }
            ?.positionMs
            ?: 0
    }

    private fun startProgressTicker() {
        progressHandler.removeCallbacks(progressTicker)
        progressHandler.post(progressTicker)
    }

    private fun togglePlayback() {
        if (currentPlayablePath.isBlank()) {
            val resume = playbackStateStore.load()
            val item = resume?.let { progress ->
                currentItems().firstOrNull { it.path == progress.path && it.isPlayable }
            }
            if (item != null) {
                playMediaItem(item, resume.positionMs)
            }
            return
        }
        if (isCurrentlyPlaying()) {
            saveCurrentPlaybackPosition()
            if (currentIsVideo) videoPlayerView.pause() else audioPlayer?.pause()
            playPauseButton.text = "Play"
        } else {
            if (currentIsVideo) videoPlayerView.start() else audioPlayer?.start()
            playPauseButton.text = "Pause"
        }
    }

    private fun seekToStart() {
        if (currentPlayablePath.isBlank()) return
        if (currentIsVideo) videoPlayerView.seekTo(0) else audioPlayer?.seekTo(0)
        playbackStateStore.save(currentPlayablePath, 0)
        updatePlayerProgress()
    }

    private fun playNextInFolder() {
        val playlist = currentItems()
            .filter { it.isPlayable && it.path.substringBeforeLast('/', "") == currentPlayableFolder }
            .sortedBy { it.name.lowercase() }
        val currentIndex = playlist.indexOfFirst { it.path == currentPlayablePath }
        val next = when {
            playlist.isEmpty() -> null
            currentIndex < 0 -> playlist.first()
            currentIndex + 1 < playlist.size -> playlist[currentIndex + 1]
            else -> null
        }
        if (next == null) {
            playPauseButton.text = "Play"
            updatePlayerProgress()
            return
        }
        playMediaItem(next, 0)
    }

    private fun saveCurrentPlaybackPosition() {
        if (currentPlayablePath.isBlank()) return
        val position = currentPositionMs()
        playbackStateStore.save(currentPlayablePath, position)
    }

    private fun updatePlayerProgress() {
        if (currentPlayablePath.isBlank()) return
        val position = currentPositionMs()
        val duration = currentDurationMs()
        playbackStateStore.save(currentPlayablePath, position)
        playerProgressText.text = if (duration > 0) {
            "${formatDuration(position)} / ${formatDuration(duration)}"
        } else {
            formatDuration(position)
        }
        playPauseButton.text = if (isCurrentlyPlaying()) "Pause" else "Play"
    }

    private fun currentPositionMs(): Int {
        return if (currentIsVideo) {
            runCatching { videoPlayerView.currentPosition }.getOrDefault(0)
        } else {
            runCatching { audioPlayer?.currentPosition ?: 0 }.getOrDefault(0)
        }
    }

    private fun currentDurationMs(): Int {
        return if (currentIsVideo) {
            runCatching { videoPlayerView.duration }.getOrDefault(0)
        } else {
            runCatching { audioPlayer?.duration ?: 0 }.getOrDefault(0)
        }
    }

    private fun isCurrentlyPlaying(): Boolean {
        return if (currentIsVideo) {
            runCatching { videoPlayerView.isPlaying }.getOrDefault(false)
        } else {
            runCatching { audioPlayer?.isPlaying == true }.getOrDefault(false)
        }
    }

    private fun formatDuration(ms: Int): String {
        val totalSeconds = (ms / 1000).coerceAtLeast(0)
        val hours = totalSeconds / 3600
        val minutes = (totalSeconds % 3600) / 60
        val seconds = totalSeconds % 60
        return if (hours > 0) {
            "%d:%02d:%02d".format(hours, minutes, seconds)
        } else {
            "%02d:%02d".format(minutes, seconds)
        }
    }

    private fun currentItems(): List<SyncedFileItem> {
        val localDirect = cachedLocalNodes.values
            .filter { node -> isDirectChild(currentFolderPath, node.entry.path) }
            .associateBy { it.entry.path }
        val allPaths = (cachedFolderEntries.map { it.path } + localDirect.keys).toSortedSet()
        return allPaths.mapNotNull { path ->
            val remoteEntry = cachedRemoteState[path]
            val localNode = localDirect[path]
            val entry = remoteEntry ?: localNode?.entry ?: return@mapNotNull null
            val hasLocalCopy = localNode != null
            val hasWebServerCopy = remoteEntry?.hasWebServerCopy ?: true
            val playable = !entry.isDirectory && isMediaFile(entry.path) && (hasLocalCopy || hasWebServerCopy)
            SyncedFileItem(
                path = entry.path,
                name = entry.name,
                subtitle = itemSubtitle(entry, localNode, remoteEntry),
                isDirectory = entry.isDirectory,
                status = if (entry.isDirectory) "Open" else if (playable && hasLocalCopy) "Play" else if (playable) "Stream" else "Cloud",
                isSynced = localNode != null && remoteEntry != null && roughlyEqual(localNode.entry, remoteEntry),
                hasLocalCopy = hasLocalCopy,
                hasWebServerCopy = hasWebServerCopy,
                isPlayable = playable,
            )
        }
    }

    private fun renderCurrentFolderFromCache() {
        val items = currentItems()
            .map { item ->
                val localNode = cachedLocalNodes[item.path]
                val remoteEntry = cachedRemoteState[item.path]
                val entry = remoteEntry ?: localNode?.entry
                val isSynced = localNode != null && remoteEntry != null && roughlyEqual(localNode.entry, remoteEntry)
                item.copy(
                    status = when {
                        item.isDirectory -> "Open"
                        item.isPlayable && item.hasLocalCopy -> "Play"
                        item.isPlayable && item.hasWebServerCopy -> "Stream"
                        !item.hasLocalCopy && !item.hasWebServerCopy -> "Node only"
                        entry != null && documentTreeSync.isTextFile(entry.path) && item.hasLocalCopy -> "Edit"
                        isSynced -> "Synced"
                        item.hasLocalCopy -> "Local"
                        else -> "Cloud"
                    },
                    isSynced = isSynced,
                )
            }
            .sortedWith(compareBy<SyncedFileItem>({ !it.isDirectory }, { it.name.lowercase() }))

        currentFolderText.text = "Folder: /${currentFolderPath}".replace("//", "/")
        upFolderButton.isEnabled = currentFolderPath.isNotBlank()
        fileCountText.text = "${items.size} items in this folder"
        emptyFilesText.visibility = if (items.isEmpty()) View.VISIBLE else View.GONE
        emptyFilesText.text = "This folder is empty."
        renderFileItems(items)
        showSavedPlaybackHint(items)
    }

    private fun showSavedPlaybackHint(items: List<SyncedFileItem>) {
        if (currentPlayablePath.isNotBlank()) return
        val progress = playbackStateStore.load() ?: return
        val item = items.firstOrNull { it.path == progress.path && it.isPlayable } ?: return
        playerSection.visibility = View.VISIBLE
        videoPlayerView.visibility = if (isVideoFile(item.path)) View.VISIBLE else View.GONE
        nowPlayingText.text = "Resume ${item.name}"
        playerProgressText.text = formatDuration(progress.positionMs)
        playPauseButton.text = "Play"
    }

    private fun renderFileItems(items: List<SyncedFileItem>) {
        filesRecyclerView.removeAllViews()
        val inflater = LayoutInflater.from(this)
        for (item in items) {
            val itemView = inflater.inflate(R.layout.item_synced_file, filesRecyclerView, false)
            val glyphText = itemView.findViewById<TextView>(R.id.fileGlyphText)
            val nameText = itemView.findViewById<TextView>(R.id.fileNameText)
            val pathText = itemView.findViewById<TextView>(R.id.filePathText)
            val statusText = itemView.findViewById<TextView>(R.id.fileStatusText)

            glyphText.text = glyphForItem(item)
            nameText.text = item.name
            pathText.text = item.subtitle
            statusText.text = item.status
            statusText.setBackgroundResource(
                if (item.isDirectory) R.drawable.freecloud_pending_badge_bg
                else if (item.isSynced) R.drawable.freecloud_synced_badge_bg
                else R.drawable.freecloud_pending_badge_bg,
            )
            statusText.setTextColor(
                getColorCompat(
                    if (item.isDirectory) R.color.freecloud_blue_dark
                    else if (item.isSynced) R.color.freecloud_success
                    else R.color.freecloud_blue_dark,
                ),
            )
            itemView.setOnClickListener { openItem(item) }
            itemView.setOnLongClickListener {
                showItemMenu(it, item)
                true
            }
            filesRecyclerView.addView(itemView)
        }
    }

    private fun glyphForItem(item: SyncedFileItem): String {
        if (item.isDirectory) return "\uD83D\uDCC1"
        return when (item.name.substringAfterLast('.', "").lowercase()) {
            "jpg", "jpeg", "png", "gif", "webp", "bmp", "svg" -> "\uD83D\uDDBC"
            "mp4", "mov", "m4v", "webm", "ogv", "ogg" -> "\uD83C\uDFA5"
            "mp3", "wav", "m4a", "aac", "flac" -> "\uD83C\uDFB5"
            "txt", "md", "json", "csv", "log", "xml", "yaml", "yml", "ini", "conf" -> "\uD83D\uDCC4"
            "pdf" -> "\uD83D\uDCD5"
            "zip", "rar", "7z", "tar", "gz" -> "\uD83D\uDCE6"
            else -> "\uD83D\uDCC4"
        }
    }

    private fun getColorCompat(colorRes: Int): Int {
        return ContextCompat.getColor(this, colorRes)
    }

    private fun shouldForceRefreshForStatus(message: String): Boolean {
        val lower = message.lowercase()
        return lower == "no changes." ||
            lower.startsWith("uploaded:") ||
            lower.startsWith("downloaded:") ||
            lower.startsWith("deleted local item:") ||
            lower.startsWith("deleted remote item:") ||
            lower.startsWith("created local folder:") ||
            lower.startsWith("created remote folder:") ||
            lower.startsWith("sync error:") ||
            lower == "stopped"
    }

    private fun showOverflowMenu() {
        val popup = PopupMenu(this, menuButton)
        popup.menuInflater.inflate(R.menu.file_browser_menu, popup.menu)
        val running = SyncRuntime.running
        val startItem = popup.menu.findItem(R.id.menu_start_sync)
        val stopItem = popup.menu.findItem(R.id.menu_stop_sync)

        startItem.isEnabled = !running
        stopItem.isEnabled = running
        startItem.title = coloredMenuTitle(
            "Start Sync",
            if (running) R.color.freecloud_muted else R.color.freecloud_success,
        )
        stopItem.title = coloredMenuTitle(
            "Stop Sync",
            if (running) R.color.freecloud_danger else R.color.freecloud_muted,
        )

        popup.setOnMenuItemClickListener { item ->
            when (item.itemId) {
                R.id.menu_start_sync -> {
                    SyncService.start(this)
                    true
                }
                R.id.menu_stop_sync -> {
                    SyncService.stop(this)
                    true
                }
                R.id.menu_view_web -> {
                    val config = configStore.load() ?: return@setOnMenuItemClickListener true
                    startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(config.baseUrl)))
                    true
                }
                R.id.menu_toggle_activity -> {
                    activitySection.visibility = if (activitySection.visibility == View.VISIBLE) View.GONE else View.VISIBLE
                    true
                }
                R.id.menu_edit_setup -> {
                    showSetupCard()
                    true
                }
                else -> false
            }
        }
        popup.show()
    }

    private fun showItemMenu(anchor: View, item: SyncedFileItem) {
        val popup = PopupMenu(this, anchor)
        popup.menu.add(0, 1, 0, "Share")
        popup.setOnMenuItemClickListener {
            shareItem(item)
            true
        }
        popup.show()
    }

    private fun shareItem(item: SyncedFileItem) {
        val config = configStore.load() ?: return
        if (item.isDirectory) {
            Toast.makeText(this, "Preparing folder zip...", Toast.LENGTH_SHORT).show()
            Thread {
                runCatching {
                    val safeName = item.name.ifBlank { "folder" }
                    val zipFile = File(cacheDir, "share_${safeName}_${System.currentTimeMillis()}.zip")
                    documentTreeSync.exportFolderZip(config.treeUri, item.path, zipFile)
                    val uri = FileProvider.getUriForFile(this, "${packageName}.fileprovider", zipFile)
                    runOnUiThread {
                        launchShareSheet(uri, "application/zip", "$safeName.zip")
                    }
                }.onFailure { exc ->
                    runOnUiThread {
                        Toast.makeText(this, exc.message ?: "Could not share folder.", Toast.LENGTH_LONG).show()
                    }
                }
            }.start()
            return
        }

        val document = documentTreeSync.findDocument(config.treeUri, item.path)
        if (document == null) {
            Toast.makeText(this, "Could not share ${item.name}.", Toast.LENGTH_LONG).show()
            return
        }
        launchShareSheet(document.uri, documentTreeSync.contentTypeForPath(item.path), item.name)
    }

    private fun launchShareSheet(uri: Uri, type: String, title: String) {
        val intent = Intent(Intent.ACTION_SEND).apply {
            putExtra(Intent.EXTRA_STREAM, uri)
            addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
            this.type = type
        }
        startActivity(Intent.createChooser(intent, "Share $title"))
    }

    private fun coloredMenuTitle(text: String, colorRes: Int): SpannableString {
        return SpannableString(text).apply {
            setSpan(ForegroundColorSpan(getColorCompat(colorRes)), 0, length, 0)
        }
    }

    private fun boldLabelValue(label: String, value: String): SpannableStringBuilder {
        return SpannableStringBuilder().apply {
            append(label)
            setSpan(StyleSpan(android.graphics.Typeface.BOLD), 0, length, 0)
            append(" ")
            append(value)
        }
    }

    private fun summarizeTreeUri(treeUri: String): String {
        val decoded = Uri.decode(Uri.parse(treeUri).lastPathSegment.orEmpty())
        val afterColon = decoded.substringAfter(':', decoded)
        val parts = afterColon.split('/').filter { it.isNotBlank() }
        return when {
            parts.isEmpty() -> afterColon.ifBlank { treeUri }
            parts.size == 1 -> parts[0]
            else -> ".../${parts.takeLast(2).joinToString("/")}"
        }
    }
}
