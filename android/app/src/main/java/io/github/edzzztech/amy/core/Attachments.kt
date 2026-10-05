package io.github.edzzztech.amy.core

import android.content.Context
import android.net.Uri
import android.provider.OpenableColumns
import java.io.BufferedInputStream
import java.io.InputStream
import java.io.InputStreamReader
import java.nio.charset.Charset
import java.util.zip.ZipInputStream

/**
 * Reading files the user attaches, so she can answer questions about them.
 *
 * Text is extracted here and handed to the model as context. The phone model
 * has a small context window, so [prompt] sizes the document to it rather than
 * letting it overflow — and keeps the beginning, which is where a document
 * says what it is. Overflowing the window cut from the front instead and lost
 * exactly that part.
 *
 * [read] does I/O through a content provider, which may be fetching the file
 * from the cloud, so it must not be called on the main thread.
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
        val (name, size) = describe(uri)
        val type = context.contentResolver.getType(uri).orEmpty()
        val extension = name.substringAfterLast('.', "").lowercase()
        val unreadable = Attached(name, size, "", truncated = false, readable = false)
        val word = extension == "docx" || type == DOCX_MIME
        // Decided before opening: on a cloud drive, opening a file starts
        // downloading it, which is a waste for one we cannot read.
        if (!word && !looksTextual(name, type)) return unreadable

        val raw = try {
            context.contentResolver.openInputStream(uri)?.use { stream ->
                if (word) Docx.text(stream, MAX_CHARS) else readText(stream)
            }
        } catch (e: Exception) {
            null
        } ?: return unreadable

        // A file called .txt that is really binary reads as noise; say so
        // instead of feeding the model garbage.
        if (raw.isBlank() || raw.take(2000).contains('\u0000')) return unreadable
        val truncated = raw.length >= MAX_CHARS
        return Attached(name, size, raw.take(MAX_CHARS), truncated, readable = true)
    }

    /**
     * Up to [MAX_CHARS] of text, in whatever encoding the file announces.
     * Windows Notepad saves "Unicode" as UTF-16 with a byte-order mark, which
     * read as UTF-8 comes out as every other character a blank.
     */
    private fun readText(stream: InputStream): String {
        val input = BufferedInputStream(stream)
        input.mark(3)
        val bom = ByteArray(3)
        val got = input.read(bom)
        input.reset()
        val charset: Charset = when {
            got >= 2 && bom[0] == 0xFF.toByte() && bom[1] == 0xFE.toByte() -> Charsets.UTF_16LE
            got >= 2 && bom[0] == 0xFE.toByte() && bom[1] == 0xFF.toByte() -> Charsets.UTF_16BE
            else -> Charsets.UTF_8
        }
        val reader = InputStreamReader(input, charset)
        val buffer = StringBuilder()
        val chunk = CharArray(8192)
        while (buffer.length < MAX_CHARS) {
            val read = reader.read(chunk)
            if (read <= 0) break
            buffer.appendRange(chunk, 0, read)
        }
        return buffer.toString().removePrefix("\uFEFF")
    }

    /** Name and size in one query. */
    private fun describe(uri: Uri): Pair<String, Long> {
        val fallback = uri.lastPathSegment ?: "file"
        return runCatching {
            context.contentResolver.query(
                uri, arrayOf(OpenableColumns.DISPLAY_NAME, OpenableColumns.SIZE),
                null, null, null,
            )?.use { c ->
                if (!c.moveToFirst()) return@use null
                val n = c.getColumnIndex(OpenableColumns.DISPLAY_NAME)
                val s = c.getColumnIndex(OpenableColumns.SIZE)
                val name = if (n >= 0) c.getString(n) else null
                val size = if (s >= 0 && !c.isNull(s)) c.getLong(s) else 0L
                (name ?: fallback) to size
            }
        }.getOrNull() ?: (fallback to 0L)
    }

    /**
     * Whether we can get text out of it. PDF is deliberately excluded:
     * extracting it needs a parser we have not shipped, and claiming to read
     * one and returning mojibake is worse than saying no.
     */
    private fun looksTextual(name: String, mime: String): Boolean {
        if (mime.startsWith("text/")) return true
        if (mime in TEXTUAL_MIMES) return true
        return name.substringAfterLast('.', "").lowercase() in TEXTUAL_EXTENSIONS
    }

    companion object {
        /** Read at most this much; [prompt] then fits it to the model. */
        private const val MAX_CHARS = 24_000

        private const val DOCX_MIME =
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

        private const val DEFAULT_QUESTION =
            "Summarise this and tell me anything that needs action."

        private val TEXTUAL_MIMES = setOf(
            "application/json", "application/xml", "application/csv",
            "application/javascript", "application/x-yaml", "application/rtf",
        )
        private val TEXTUAL_EXTENSIONS = setOf(
            "txt", "md", "markdown", "csv", "tsv", "json", "xml", "yaml", "yml",
            "log", "ini", "cfg", "conf", "py", "kt", "java", "js", "ts", "html",
            "css", "sh", "bat", "sql", "gradle", "properties", "srt", "vtt",
        )

        /**
         * The prompt she actually sees: the file, cut to fit [room] characters
         * together with the question, so nothing is lost off the front.
         */
        fun prompt(file: Attached, question: String, room: Int): String {
            val ask = question.ifBlank { DEFAULT_QUESTION }
            val header = "Here is a file called ${file.name}"
            val textRoom = (room - header.length - ask.length - PROMPT_OVERHEAD).coerceAtLeast(200)
            val cut = file.text.length > textRoom
            return buildString {
                append(header)
                if (cut || file.truncated) append(" (first part only)")
                append(":\n\n")
                append(if (cut) file.text.take(textRoom) else file.text)
                append("\n\n")
                append(ask)
            }
        }

        /** The labels and line breaks [prompt] adds around the text, with margin. */
        private const val PROMPT_OVERHEAD = 40
    }
}

/**
 * Text out of a Word document without a library. A .docx is a zip, and the
 * body is word/document.xml: the words are in <w:t> elements, paragraphs end
 * with </w:p>. Formatting is dropped; deleted text and field codes, which are
 * in other elements, are skipped rather than read out.
 */
internal object Docx {

    fun text(stream: InputStream, maxChars: Int): String? {
        ZipInputStream(stream).use { zip ->
            while (true) {
                val entry = zip.nextEntry ?: return null
                if (entry.name == "word/document.xml") {
                    return fromXml(readCapped(zip, MAX_XML_BYTES), maxChars)
                }
            }
        }
    }

    fun fromXml(xml: String, maxChars: Int): String {
        val out = StringBuilder()
        for (m in TOKEN.findAll(xml)) {
            if (out.length >= maxChars) break
            when (val tag = m.value) {
                // Before the text check: "<w:tab/>" also starts with "<w:t".
                "<w:tab/>" -> out.append('\t')
                // A table cell ends its paragraph and then itself; join the
                // cells of a row on one line rather than one per line.
                "</w:tc>" -> {
                    if (out.endsWith("\n")) out.setLength(out.length - 1)
                    out.append(CELL)
                }
                "</w:tr>" -> {
                    if (out.endsWith(CELL)) out.setLength(out.length - CELL.length)
                    out.append('\n')
                }
                else ->
                    if (tag.startsWith("<w:t")) {
                        out.append(unescape(m.groupValues[1]))
                    } else if (out.isNotEmpty() && out.last() != '\n') {
                        out.append('\n')          // paragraph end or line break
                    }
            }
        }
        return out.toString().trim()
    }

    private const val CELL = " | "

    /** A zip entry can claim any size; read no more than [limit] of it. */
    private fun readCapped(input: InputStream, limit: Int): String {
        val bytes = java.io.ByteArrayOutputStream()
        val chunk = ByteArray(16 * 1024)
        while (bytes.size() < limit) {
            val n = input.read(chunk, 0, minOf(chunk.size, limit - bytes.size()))
            if (n <= 0) break
            bytes.write(chunk, 0, n)
        }
        return bytes.toString("UTF-8")
    }

    private fun unescape(s: String): String {
        if ('&' !in s) return s
        return ENTITY.replace(s) { m ->
            val e = m.groupValues[1]
            when {
                e == "amp" -> "&"
                e == "lt" -> "<"
                e == "gt" -> ">"
                e == "quot" -> "\""
                e == "apos" -> "'"
                e.startsWith("#x") -> codePoint(e.drop(2).toIntOrNull(16)) ?: m.value
                e.startsWith("#") -> codePoint(e.drop(1).toIntOrNull()) ?: m.value
                else -> m.value
            }
        }
    }

    /** Null for anything that is not a real character, rather than throwing. */
    private fun codePoint(n: Int?): String? =
        n?.takeIf { Character.isValidCodePoint(it) }?.let { String(Character.toChars(it)) }

    private const val MAX_XML_BYTES = 8 * 1024 * 1024

    // Text runs, tab characters, line and page breaks, cell and paragraph
    // ends. <w:tab/> with no attributes only: <w:tab w:pos=.../> is a tab
    // stop definition, not a tab in the text.
    private val TOKEN = Regex(
        "<w:t(?:\\s[^>]*)?>([^<]*)</w:t>|<w:tab/>|<w:br(?:\\s[^>]*)?/>|<w:cr/>|</w:p>|</w:tc>|</w:tr>"
    )
    private val ENTITY = Regex("&(#x[0-9a-fA-F]+|#[0-9]+|[a-z]+);")
}
