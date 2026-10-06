package io.github.edzzztech.amy.core

import android.content.Context
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.graphics.Matrix
import android.net.Uri
import androidx.core.graphics.scale
import androidx.exifinterface.media.ExifInterface
import java.io.ByteArrayInputStream
import java.io.ByteArrayOutputStream
import java.io.File
import java.io.InputStream
import kotlin.math.roundToInt

/**
 * Pictures made ready for the model: decoded small, turned the right way up,
 * and re-encoded.
 *
 * Small, because a 12-megapixel photo decoded whole is around 50 MB of memory,
 * and Gemma 3n's vision encoder looks at 768 pixels at most anyway. The right
 * way up, because cameras save photos sideways with an EXIF note saying which
 * way is up - a note the model never reads, so a portrait would arrive lying
 * on its side.
 *
 * Both readers do disk (or cloud) I/O: call them off the main thread.
 */
object Pictures {

    /** The longest side kept. Gemma 3n's encoder works at 256, 512 or 768. */
    const val MAX_SIDE = 768

    fun fromUri(context: Context, uri: Uri): ByteArray? =
        prepare { context.contentResolver.openInputStream(uri) }

    fun fromFile(file: File): ByteArray? = prepare { file.inputStream() }

    /**
     * Read once into memory, then decode from there: size, pixels and
     * orientation are read separately, and for a picture on a cloud drive
     * each opening of the stream can mean another download.
     */
    private fun prepare(open: () -> InputStream?): ByteArray? {
        return try {
            val raw = open()?.use { readCapped(it) } ?: return null
            val bounds = BitmapFactory.Options().apply { inJustDecodeBounds = true }
            BitmapFactory.decodeByteArray(raw, 0, raw.size, bounds)
            if (bounds.outWidth <= 0 || bounds.outHeight <= 0) return null

            val options = BitmapFactory.Options().apply {
                inSampleSize = sampleSize(bounds.outWidth, bounds.outHeight)
            }
            val decoded = BitmapFactory.decodeByteArray(raw, 0, raw.size, options) ?: return null
            val orientation = try {
                ExifInterface(ByteArrayInputStream(raw)).getAttributeInt(
                    ExifInterface.TAG_ORIENTATION, ExifInterface.ORIENTATION_NORMAL,
                )
            } catch (e: Exception) {
                ExifInterface.ORIENTATION_NORMAL      // no EXIF (a PNG, a screenshot): already upright
            }

            val upright = orient(decoded, orientation)
            if (upright !== decoded) decoded.recycle()
            val (w, h) = fit(upright.width, upright.height)
            val scaled = if (w == upright.width && h == upright.height) upright
            else upright.scale(w, h)
            if (scaled !== upright) upright.recycle()

            ByteArrayOutputStream().use { out ->
                scaled.compress(Bitmap.CompressFormat.PNG, 100, out)
                scaled.recycle()
                out.toByteArray()
            }
        } catch (e: Exception) {
            null
        } catch (e: OutOfMemoryError) {
            null
        }
    }

    /** The whole file, unless it is implausibly large for a picture. */
    private fun readCapped(input: InputStream): ByteArray? {
        val out = ByteArrayOutputStream()
        val chunk = ByteArray(64 * 1024)
        while (true) {
            val n = input.read(chunk)
            if (n < 0) break
            out.write(chunk, 0, n)
            if (out.size() > MAX_BYTES) return null
        }
        return out.toByteArray()
    }

    private const val MAX_BYTES = 60 * 1024 * 1024

    private fun orient(bitmap: Bitmap, orientation: Int): Bitmap {
        val m = Matrix()
        when (orientation) {
            ExifInterface.ORIENTATION_ROTATE_90 -> m.postRotate(90f)
            ExifInterface.ORIENTATION_ROTATE_180 -> m.postRotate(180f)
            ExifInterface.ORIENTATION_ROTATE_270 -> m.postRotate(270f)
            ExifInterface.ORIENTATION_FLIP_HORIZONTAL -> m.postScale(-1f, 1f)
            ExifInterface.ORIENTATION_FLIP_VERTICAL -> m.postScale(1f, -1f)
            ExifInterface.ORIENTATION_TRANSPOSE -> {
                m.postRotate(90f)
                m.postScale(-1f, 1f)
            }
            ExifInterface.ORIENTATION_TRANSVERSE -> {
                m.postRotate(270f)
                m.postScale(-1f, 1f)
            }
            else -> return bitmap
        }
        return Bitmap.createBitmap(bitmap, 0, 0, bitmap.width, bitmap.height, m, true)
    }

    /**
     * The largest power of two to decode at that still leaves the long side
     * at least [maxSide] - decoding at a power of two is cheap; scaling the
     * rest of the way happens after.
     */
    internal fun sampleSize(width: Int, height: Int, maxSide: Int = MAX_SIDE): Int {
        var sample = 1
        while (maxOf(width, height) / (sample * 2) >= maxSide) sample *= 2
        return sample
    }

    /** The size with the long side at most [maxSide], keeping the shape. */
    internal fun fit(width: Int, height: Int, maxSide: Int = MAX_SIDE): Pair<Int, Int> {
        val long = maxOf(width, height)
        if (long <= maxSide) return width to height
        val scale = maxSide.toDouble() / long
        return maxOf(1, (width * scale).roundToInt()) to maxOf(1, (height * scale).roundToInt())
    }
}
