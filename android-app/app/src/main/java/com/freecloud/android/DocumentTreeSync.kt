package com.freecloud.android

import android.content.Context
import android.net.Uri
import android.webkit.MimeTypeMap
import androidx.documentfile.provider.DocumentFile
import java.io.ByteArrayOutputStream
import java.io.File
import java.io.FileOutputStream
import java.util.zip.ZipEntry
import java.util.zip.ZipOutputStream

data class LocalNode(
    val entry: ManifestEntry,
    val document: DocumentFile,
)

class DocumentTreeSync(private val context: Context) {
    fun scanTree(treeUri: String): Map<String, LocalNode> {
        val root = root(treeUri)
        val out = linkedMapOf<String, LocalNode>()
        walk(root, "", out)
        return out
    }

    fun ensureDirectory(treeUri: String, path: String) {
        val root = root(treeUri)
        ensureDirectory(root, UrlTools.remotePath(path))
    }

    fun downloadToTree(treeUri: String, path: String, bytes: ByteArray) {
        val root = root(treeUri)
        val cleanPath = UrlTools.remotePath(path)
        val parentPath = cleanPath.substringBeforeLast('/', "")
        val name = cleanPath.substringAfterLast('/')
        val parent = ensureDirectory(root, parentPath)
        var file = parent.findFile(name)
        if (file == null || file.isDirectory) {
            file?.delete()
            file = parent.createFile(mimeTypeForName(name), name)
        }
        requireNotNull(file) { "Could not create local file $path" }
        context.contentResolver.openOutputStream(file.uri, "wt")?.use { output ->
            output.write(bytes)
        } ?: error("Could not open local file for writing: $path")
    }

    fun uploadBytes(node: LocalNode): ByteArray {
        return context.contentResolver.openInputStream(node.document.uri)?.use { input ->
            val out = ByteArrayOutputStream()
            val chunk = ByteArray(1024 * 1024)
            while (true) {
                val read = input.read(chunk)
                if (read <= 0) break
                out.write(chunk, 0, read)
            }
            out.toByteArray()
        } ?: error("Could not read local file ${node.entry.path}")
    }

    fun deletePath(treeUri: String, path: String): Boolean {
        val document = findPath(root(treeUri), UrlTools.remotePath(path)) ?: return false
        return deleteRecursive(document)
    }

    fun findDocument(treeUri: String, path: String): DocumentFile? {
        return findPath(root(treeUri), UrlTools.remotePath(path))
    }

    fun readTextFile(treeUri: String, path: String): String {
        val document = findDocument(treeUri, path) ?: error("Could not find local file $path")
        require(!document.isDirectory) { "Folders cannot be opened as text files." }
        return context.contentResolver.openInputStream(document.uri)?.use { input ->
            input.bufferedReader(Charsets.UTF_8).readText()
        } ?: error("Could not open local file $path")
    }

    fun writeTextFile(treeUri: String, path: String, text: String) {
        val document = findDocument(treeUri, path) ?: error("Could not find local file $path")
        require(!document.isDirectory) { "Folders cannot be saved as text files." }
        context.contentResolver.openOutputStream(document.uri, "wt")?.use { output ->
            output.writer(Charsets.UTF_8).use { it.write(text) }
        } ?: error("Could not write local file $path")
    }

    fun exportFolderZip(treeUri: String, path: String, targetZip: File) {
        val document = findDocument(treeUri, path) ?: error("Could not find local folder $path")
        require(document.isDirectory) { "Only folders can be exported as zip files." }
        ZipOutputStream(FileOutputStream(targetZip)).use { zip ->
            zipDirectory(document, "", zip)
        }
    }

    fun contentTypeForName(name: String): String = mimeTypeForName(name)

    fun contentTypeForPath(path: String): String = mimeTypeForName(path.substringAfterLast('/'))

    fun isTextFile(path: String): Boolean {
        return path.substringAfterLast('.', "").lowercase() in setOf(
            "txt", "md", "json", "csv", "log", "xml", "yaml", "yml", "ini", "conf",
            "php", "html", "htm", "css", "js", "kt", "java", "py", "sh",
        )
    }

    private fun walk(current: DocumentFile, prefix: String, out: MutableMap<String, LocalNode>) {
        for (child in current.listFiles()) {
            val name = child.name ?: continue
            val path = if (prefix.isBlank()) name else "$prefix/$name"
            val entry = ManifestEntry(
                path = path,
                name = name,
                type = if (child.isDirectory) "dir" else "file",
                size = if (child.isDirectory) 0L else child.length(),
                mtime = child.lastModified(),
            )
            out[path] = LocalNode(entry, child)
            if (child.isDirectory) {
                walk(child, path, out)
            }
        }
    }

    private fun root(treeUri: String): DocumentFile {
        return requireNotNull(DocumentFile.fromTreeUri(context, Uri.parse(treeUri))) {
            "Could not open the chosen local folder."
        }
    }

    private fun findPath(root: DocumentFile, path: String): DocumentFile? {
        if (path.isBlank()) return root
        var current = root
        for (part in path.split("/").filter { it.isNotBlank() }) {
            current = current.findFile(part) ?: return null
        }
        return current
    }

    private fun ensureDirectory(root: DocumentFile, path: String): DocumentFile {
        if (path.isBlank()) return root
        var current = root
        for (part in path.split("/").filter { it.isNotBlank() }) {
            val existing = current.findFile(part)
            current = when {
                existing == null -> requireNotNull(current.createDirectory(part)) { "Could not create local folder $path" }
                existing.isDirectory -> existing
                else -> error("Local path conflict at $part")
            }
        }
        return current
    }

    private fun deleteRecursive(document: DocumentFile): Boolean {
        if (document.isDirectory) {
            for (child in document.listFiles()) {
                if (!deleteRecursive(child)) return false
            }
        }
        return document.delete()
    }

    private fun mimeTypeForName(name: String): String {
        val ext = name.substringAfterLast('.', "").lowercase()
        return MimeTypeMap.getSingleton().getMimeTypeFromExtension(ext) ?: "application/octet-stream"
    }

    private fun zipDirectory(document: DocumentFile, prefix: String, zip: ZipOutputStream) {
        val baseName = document.name ?: "folder"
        val folderPrefix = if (prefix.isBlank()) "$baseName/" else "$prefix$baseName/"
        zip.putNextEntry(ZipEntry(folderPrefix))
        zip.closeEntry()

        for (child in document.listFiles()) {
            val childName = child.name ?: continue
            if (child.isDirectory) {
                zipDirectory(child, folderPrefix, zip)
            } else {
                val entryName = "$folderPrefix$childName"
                zip.putNextEntry(ZipEntry(entryName))
                context.contentResolver.openInputStream(child.uri)?.use { input ->
                    val buffer = ByteArray(1024 * 1024)
                    while (true) {
                        val read = input.read(buffer)
                        if (read <= 0) break
                        zip.write(buffer, 0, read)
                    }
                } ?: error("Could not read local file $entryName")
                zip.closeEntry()
            }
        }
    }
}
