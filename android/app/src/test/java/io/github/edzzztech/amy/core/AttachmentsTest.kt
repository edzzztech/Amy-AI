package io.github.edzzztech.amy.core

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.ByteArrayInputStream
import java.io.ByteArrayOutputStream
import java.util.zip.ZipEntry
import java.util.zip.ZipOutputStream

class AttachmentsTest {

    private fun docx(body: String, extraEntryFirst: Boolean = true): ByteArray {
        val bytes = ByteArrayOutputStream()
        ZipOutputStream(bytes).use { zip ->
            if (extraEntryFirst) {
                zip.putNextEntry(ZipEntry("[Content_Types].xml"))
                zip.write("<Types/>".toByteArray())
                zip.closeEntry()
            }
            zip.putNextEntry(ZipEntry("word/document.xml"))
            zip.write(
                ("<?xml version=\"1.0\"?><w:document><w:body>$body</w:body></w:document>")
                    .toByteArray(Charsets.UTF_8)
            )
            zip.closeEntry()
        }
        return bytes.toByteArray()
    }

    private fun para(vararg runs: String) =
        "<w:p><w:pPr><w:tabs><w:tab w:val=\"left\" w:pos=\"720\"/></w:tabs></w:pPr>" +
            runs.joinToString("") { "<w:r><w:t xml:space=\"preserve\">$it</w:t></w:r>" } +
            "</w:p>"

    @Test
    fun paragraphsBecomeLinesAndEntitiesAreDecoded() {
        val text = Docx.text(
            ByteArrayInputStream(docx(para("Fish &amp; chips ", "for two") + para("Total: &#163;12"))),
            10_000,
        )
        // The tab stop definition in the paragraph properties is not a tab.
        assertEquals("Fish & chips for two\nTotal: £12", text)
    }

    @Test
    fun tablesKeepEachRowOnOneLine() {
        val row = { a: String, b: String ->
            "<w:tr><w:tc>${para(a)}</w:tc><w:tc>${para(b)}</w:tc></w:tr>"
        }
        val text = Docx.text(
            ByteArrayInputStream(docx("<w:tbl>" + row("Name", "Qty") + row("Pens", "3") + "</w:tbl>")),
            10_000,
        )
        assertEquals("Name | Qty\nPens | 3", text)
    }

    @Test
    fun tabsBreaksAndDeletedTextAndFieldCodes() {
        val body = "<w:p><w:r><w:t>A</w:t><w:tab/><w:t>B</w:t><w:br/><w:t>C</w:t></w:r>" +
            "<w:del><w:r><w:delText>gone</w:delText></w:r></w:del>" +
            "<w:r><w:instrText> PAGE </w:instrText></w:r></w:p>"
        assertEquals("A\tB\nC", Docx.text(ByteArrayInputStream(docx(body)), 10_000))
    }

    @Test
    fun stopsAtTheLimitAndRejectsNonDocuments() {
        val long = (1..500).joinToString("") { para("Line number $it") }
        val text = Docx.text(ByteArrayInputStream(docx(long)), 300)!!
        assertTrue(text.length < 400)
        assertTrue(text.startsWith("Line number 1\n"))
        assertNull(Docx.text(ByteArrayInputStream("not a zip at all".toByteArray()), 1000))
    }

    @Test
    fun badEntitiesAreLeftAloneNotThrown() {
        assertEquals("&#99999999; &bogus;", Docx.fromXml("<w:t>&#99999999; &bogus;</w:t>", 100))
    }

    // --- sizing the prompt to the model -----------------------------------------

    private fun attached(text: String) =
        Attachments.Attached("notes.txt", text.length.toLong(), text, truncated = false, readable = true)

    @Test
    fun aShortFileGoesInWhole() {
        val prompt = Attachments.prompt(attached("Buy milk."), "", room = 2000)
        assertTrue(prompt.startsWith("Here is a file called notes.txt:"))
        assertTrue(prompt.contains("Buy milk."))
        assertFalse(prompt.contains("first part only"))
    }

    @Test
    fun aLongFileKeepsItsBeginningAndFitsTheRoom() {
        val text = "START " + "x".repeat(10_000) + " END"
        val room = 2650
        val prompt = Attachments.prompt(attached(text), "What is this?", room)
        assertTrue("prompt must fit: ${prompt.length}", prompt.length <= room)
        assertTrue(prompt.contains("START"))
        assertFalse(prompt.contains("END"))
        assertTrue(prompt.contains("(first part only)"))
        assertTrue(prompt.endsWith("What is this?"))
    }
}
