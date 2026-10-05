package io.github.edzzztech.amy.core

import android.content.Context
import android.net.Uri
import android.provider.OpenableColumns
import java.io.BufferedReader
import java.io.InputStreamReader

/**
 * Reading files the user attaches, so she can answer questions about them.
 *
 * Text is extracted here and handed to the model as context. The phone model
 * has a small context window, so the text is truncated rather than silently
 * overflowing it — a visible "first N characters" is easier to reason about
 * than a reply that quietly ignored the end of the document.
 */
class Attachments(private val context: Context) {

    data class Attached(
        val name: String,
        val bytes: Long,
        val text: String,
        val truncated: Boolean,
        val readable: Boolean,
    )

    fun read(uri: Uri): Attached {
        val name = displayName(uri)
        val size = sizeOf(uri)
        val type = context.contentResolver.getType(uri).orEmpty()

        if (!looksTextual(name, type)) {
            return Attached(name, size, "", truncated = false, readable = false)
        }

        val raw = try {
            context.contentResolver.openInputStream(uri)?.use { stream ->
                BufferedReader(InputStreamReader(stream)).use { reader ->
                    val buffer = StringBuilder()
                    val chunk = CharArray(8192)
                    while (buffer.length < MAX_CHARS) {
                        val read = reader.read(chunk)
                        if (read <= 0) break
                        buffer.appendRange(chunk, 0, read)
                    }
                    buffer.toString()
                }
            }.orEmpty()
        } catch (e: Exception) {
            return Attached(name, size, "", truncated = false, readable = false)
        }

        val truncated = raw.length >= MAX_CHARS
        return Attached(name, size, raw.take(MAX_CHARS), truncated, readable = raw.isNotBlank())
    }

    /** The prompt she actually sees, with the file inlined. */
    fun asPrompt(file: Attached, question: String): String = buildString {
        append("Here is a file called ")
        append(file.name)
        if (file.truncated) append(" (first part only)")
        append(":\n\n")
        append(file.text)
        append("\n\n")
        append(question.ifBlank { "Summarise this and tell me anything that needs action." })
    }

    private fun displayName(uri: Uri): String =
        runCatching {
            context.contentResolver.query(uri, null, null, null, null)?.use { c ->
                val i = c.getColumnIndex(OpenableColumns.DISPLAY_NAME)
                if (i >= 0 && c.moveToFirst()) c.getString(i) else null
            }
        }.getOrNull() ?: uri.lastPathSegment ?: "file"

    private fun sizeOf(uri: Uri): Long =
        runCatching {
            context.contentResolver.query(uri, null, null, null, null)?.use { c ->
                val i = c.getColumnIndex(OpenableColumns.SIZE)
                if (i >= 0 && c.moveToFirst()) c.getLong(i) else 0L
            }
        }.getOrNull() ?: 0L

    /**
     * Whether we can get text out of it. PDF and Word are deliberately excluded:
     * extracting those needs a parser we have not shipped, and claiming to read
     * one and returning mojibake is worse than saying no.
     */
    private fun looksTextual(name: String, mime: String): Boolean {
        if (mime.startsWith("text/")) return true
        if (mime in TEXTUAL_MIMES) return true
        return name.substringAfterLast('.', "").lowercase() in TEXTUAL_EXTENSIONS
    }

    private companion object {
        const val MAX_CHARS = 12_000

        val TEXTUAL_MIMES = setOf(
            "application/json", "application/xml", "application/csv",
            "application/javascript", "application/x-yaml",
        )
        val TEXTUAL_EXTENSIONS = setOf(
            "txt", "md", "markdown", "csv", "tsv", "json", "xml", "yaml", "yml",
            "log", "ini", "cfg", "conf", "py", "kt", "java", "js", "ts", "html",
            "css", "sh", "bat", "sql", "gradle", "properties",
        )
    }
}
