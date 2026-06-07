package com.freecloud.android

import android.app.Activity
import android.os.Bundle
import android.widget.Button
import android.widget.EditText
import android.widget.TextView
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity

class FileEditorActivity : AppCompatActivity() {
    private val documents by lazy { DocumentTreeSync(this) }

    private lateinit var titleText: TextView
    private lateinit var editorText: EditText
    private lateinit var saveButton: Button

    private lateinit var treeUri: String
    private lateinit var path: String

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_file_editor)

        treeUri = intent.getStringExtra(EXTRA_TREE_URI).orEmpty()
        path = intent.getStringExtra(EXTRA_PATH).orEmpty()
        if (treeUri.isBlank() || path.isBlank()) {
            finish()
            return
        }

        titleText = findViewById(R.id.editorTitleText)
        editorText = findViewById(R.id.editorText)
        saveButton = findViewById(R.id.saveButton)

        titleText.text = path
        findViewById<Button>(R.id.backButton).setOnClickListener { finish() }
        saveButton.setOnClickListener { saveFile() }

        loadFile()
    }

    private fun loadFile() {
        saveButton.isEnabled = false
        Thread {
            runCatching { documents.readTextFile(treeUri, path) }
                .onSuccess { text ->
                    runOnUiThread {
                        editorText.setText(text)
                        saveButton.isEnabled = true
                    }
                }
                .onFailure { exc ->
                    runOnUiThread {
                        Toast.makeText(this, exc.message ?: "Could not open file.", Toast.LENGTH_LONG).show()
                        finish()
                    }
                }
        }.start()
    }

    private fun saveFile() {
        val text = editorText.text?.toString().orEmpty()
        saveButton.isEnabled = false
        Thread {
            runCatching { documents.writeTextFile(treeUri, path, text) }
                .onSuccess {
                    runOnUiThread {
                        setResult(Activity.RESULT_OK)
                        saveButton.isEnabled = true
                        Toast.makeText(this, "Saved.", Toast.LENGTH_SHORT).show()
                    }
                }
                .onFailure { exc ->
                    runOnUiThread {
                        saveButton.isEnabled = true
                        Toast.makeText(this, exc.message ?: "Could not save file.", Toast.LENGTH_LONG).show()
                    }
                }
        }.start()
    }

    companion object {
        const val EXTRA_TREE_URI = "tree_uri"
        const val EXTRA_PATH = "path"
    }
}
