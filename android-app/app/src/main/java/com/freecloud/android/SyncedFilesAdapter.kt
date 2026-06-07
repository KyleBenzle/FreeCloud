package com.freecloud.android

import android.view.LayoutInflater
import android.view.View
import android.view.ViewGroup
import android.widget.TextView
import androidx.core.content.ContextCompat
import androidx.recyclerview.widget.RecyclerView

data class SyncedFileItem(
    val path: String,
    val name: String,
    val isDirectory: Boolean,
    val subtitle: String,
    val status: String,
    val isSynced: Boolean,
)

class SyncedFilesAdapter(
    private val onClick: (SyncedFileItem) -> Unit,
) : RecyclerView.Adapter<SyncedFilesAdapter.FileViewHolder>() {
    private val items = mutableListOf<SyncedFileItem>()

    fun submitList(files: List<SyncedFileItem>) {
        items.clear()
        items.addAll(files)
        notifyDataSetChanged()
    }

    override fun onCreateViewHolder(parent: ViewGroup, viewType: Int): FileViewHolder {
        val view = LayoutInflater.from(parent.context).inflate(R.layout.item_synced_file, parent, false)
        return FileViewHolder(view, onClick)
    }

    override fun onBindViewHolder(holder: FileViewHolder, position: Int) {
        holder.bind(items[position])
    }

    override fun getItemCount(): Int = items.size

    class FileViewHolder(
        itemView: View,
        private val onClick: (SyncedFileItem) -> Unit,
    ) : RecyclerView.ViewHolder(itemView) {
        private val glyphText: TextView = itemView.findViewById(R.id.fileGlyphText)
        private val nameText: TextView = itemView.findViewById(R.id.fileNameText)
        private val pathText: TextView = itemView.findViewById(R.id.filePathText)
        private val statusText: TextView = itemView.findViewById(R.id.fileStatusText)

        fun bind(item: SyncedFileItem) {
            glyphText.text = glyphFor(item)
            nameText.text = item.name
            pathText.text = item.subtitle
            statusText.text = item.status
            statusText.setBackgroundResource(
                if (item.isDirectory) R.drawable.freecloud_pending_badge_bg
                else if (item.isSynced) R.drawable.freecloud_synced_badge_bg
                else R.drawable.freecloud_pending_badge_bg,
            )
            statusText.setTextColor(
                ContextCompat.getColor(
                    itemView.context,
                    if (item.isDirectory) R.color.freecloud_blue_dark
                    else if (item.isSynced) R.color.freecloud_success
                    else R.color.freecloud_blue_dark,
                ),
            )
            itemView.setOnClickListener { onClick(item) }
        }

        private fun glyphFor(item: SyncedFileItem): String {
            if (item.isDirectory) return "DIR"
            val ext = item.name.substringAfterLast('.', "").uppercase()
            return ext.take(3).ifBlank { "FILE" }
        }
    }
}
