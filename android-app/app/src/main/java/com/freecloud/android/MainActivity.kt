package com.freecloud.android

import android.Manifest
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
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
    @Volatile
    private var cachedLocalNodes: Map<String, LocalNode> = emptyMap()
    @Volatile
    private var cachedRemoteState: Map<String, ManifestEntry> = emptyMap()
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
        SyncService.start(this)
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
            SyncService.start(this)
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
        if (statusReceiverRegistered) {
            unregisterReceiver(statusReceiver)
            statusReceiverRegistered = false
        }
        super.onStop()
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
            renderCurrentFolderFromCache()
        }
        menuButton.setOnClickListener { showOverflowMenu() }
    }

    private fun registerBackHandler() {
        onBackPressedDispatcher.addCallback(this, object : OnBackPressedCallback(true) {
            override fun handleOnBackPressed() {
                if (runningCard.visibility == View.VISIBLE && currentFolderPath.isNotBlank()) {
                    currentFolderPath = currentFolderPath.substringBeforeLast('/', "")
                    renderCurrentFolderFromCache()
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
                    SyncService.start(this)
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
        if (!forceRescan && cachedLocalNodes.isNotEmpty()) {
            renderCurrentFolderFromCache()
            return
        }
        if (refreshInFlight) return
        refreshInFlight = true
        Thread {
            val remoteState = syncStateStore.load().remote
            val fileMapResult = runCatching { documentTreeSync.scanTree(config.treeUri) }
            if (fileMapResult.isFailure) {
                val error = fileMapResult.exceptionOrNull()
                runOnUiThread {
                    refreshInFlight = false
                    fileCountText.text = "Could not load local folder"
                    emptyFilesText.visibility = View.VISIBLE
                    emptyFilesText.text = error?.message ?: "Could not read the chosen phone folder."
                    renderFileItems(emptyList())
                }
                return@Thread
            }

            cachedRemoteState = remoteState
            cachedLocalNodes = fileMapResult.getOrThrow()

            runOnUiThread {
                refreshInFlight = false
                renderCurrentFolderFromCache()
            }
        }.start()
    }

    private fun openLocalItem(item: SyncedFileItem) {
        val config = configStore.load() ?: return
        if (item.isDirectory) {
            currentFolderPath = item.path
            renderCurrentFolderFromCache()
            return
        }

        if (documentTreeSync.isTextFile(item.path)) {
            val intent = Intent(this, FileEditorActivity::class.java)
                .putExtra(FileEditorActivity.EXTRA_TREE_URI, config.treeUri)
                .putExtra(FileEditorActivity.EXTRA_PATH, item.path)
            editorLauncher.launch(intent)
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
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            registerReceiver(statusReceiver, filter, RECEIVER_NOT_EXPORTED)
        } else {
            @Suppress("DEPRECATION")
            registerReceiver(statusReceiver, filter)
        }
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

    private fun renderCurrentFolderFromCache() {
        val items = cachedLocalNodes.values
            .filter { node -> isDirectChild(currentFolderPath, node.entry.path) }
            .sortedWith(compareBy<LocalNode>({ !it.entry.isDirectory }, { it.entry.name.lowercase() }))
            .map { node ->
                val remoteEntry = cachedRemoteState[node.entry.path]
                val isSynced = remoteEntry != null && roughlyEqual(node.entry, remoteEntry)
                SyncedFileItem(
                    path = node.entry.path,
                    name = node.entry.name,
                    subtitle = itemSubtitle(node),
                    isDirectory = node.entry.isDirectory,
                    status = when {
                        node.entry.isDirectory -> "Open"
                        documentTreeSync.isTextFile(node.entry.path) -> "Edit"
                        isSynced -> "Synced"
                        else -> "Local"
                    },
                    isSynced = isSynced,
                )
            }

        currentFolderText.text = "Folder: /${currentFolderPath}".replace("//", "/")
        upFolderButton.isEnabled = currentFolderPath.isNotBlank()
        fileCountText.text = "${items.size} items in this folder"
        emptyFilesText.visibility = if (items.isEmpty()) View.VISIBLE else View.GONE
        emptyFilesText.text = "This folder is empty."
        renderFileItems(items)
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
            itemView.setOnClickListener { openLocalItem(item) }
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
