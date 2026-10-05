package io.github.edzzztech.amy

import android.Manifest
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.camera.core.CameraSelector
import androidx.camera.core.ImageCapture
import androidx.camera.core.ImageCaptureException
import androidx.camera.core.Preview
import androidx.camera.lifecycle.ProcessCameraProvider
import androidx.camera.view.PreviewView
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.compose.ui.viewinterop.AndroidView
import androidx.core.content.ContextCompat
import io.github.edzzztech.amy.core.Amy
import java.io.File
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/**
 * Camera mode: the phone's version of the desktop's desk view.
 *
 * Captures to the app's own storage and records the shot in the action log.
 * What it does *not* yet do is describe what it sees — that needs a
 * vision-capable model, and the text-only Gemma build loaded here cannot do
 * it. The capture path is built so that when a vision model is dropped in,
 * only [onCaptured] changes.
 */
class CameraActivity : ComponentActivity() {

    private var capture: ImageCapture? = null

    private val requestCamera = registerForActivityResult(
        ActivityResultContracts.RequestPermission()
    ) { granted -> if (!granted) finish() }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        Amy.start(this)

        if (ContextCompat.checkSelfPermission(this, Manifest.permission.CAMERA)
            != PackageManager.PERMISSION_GRANTED
        ) {
            requestCamera.launch(Manifest.permission.CAMERA)
        }

        setContent {
            AmyTheme {
                CameraScreen(
                    onBind = { view, lensFacing -> bind(view, lensFacing) },
                    onShutter = ::takePhoto,
                    onClose = { finish() },
                )
            }
        }
    }

    private fun bind(view: PreviewView, lensFacing: Int) {
        val future = ProcessCameraProvider.getInstance(this)
        future.addListener({
            val provider = future.get()
            val preview = Preview.Builder().build()
                .also { it.surfaceProvider = view.surfaceProvider }
            val imageCapture = ImageCapture.Builder()
                .setCaptureMode(ImageCapture.CAPTURE_MODE_MINIMIZE_LATENCY)
                .build()
            try {
                provider.unbindAll()
                provider.bindToLifecycle(
                    this,
                    CameraSelector.Builder().requireLensFacing(lensFacing).build(),
                    preview,
                    imageCapture,
                )
                capture = imageCapture
            } catch (e: Exception) {
                Amy.actions.record("device", "Camera failed to bind", outcome = "failed",
                    detail = e.message)
            }
        }, ContextCompat.getMainExecutor(this))
    }

    private fun takePhoto() {
        val imageCapture = capture ?: return
        val dir = File(getExternalFilesDir(null), "photos").apply { mkdirs() }
        val name = SimpleDateFormat("yyyyMMdd-HHmmss", Locale.UK).format(Date())
        val file = File(dir, "$name.jpg")

        imageCapture.takePicture(
            ImageCapture.OutputFileOptions.Builder(file).build(),
            ContextCompat.getMainExecutor(this),
            object : ImageCapture.OnImageSavedCallback {
                override fun onImageSaved(output: ImageCapture.OutputFileResults) {
                    onCaptured(file)
                }

                override fun onError(exception: ImageCaptureException) {
                    Amy.actions.record("device", "Photo failed", outcome = "failed",
                        detail = exception.message)
                }
            },
        )
    }

    /** The seam a vision model drops into later. */
    private fun onCaptured(file: File) {
        Amy.actions.record("device", "Photo taken: ${file.name}")
        Amy.tts?.speak("Saved.")
        finish()
    }

    companion object {
        fun open(context: Context) {
            context.startActivity(Intent(context, CameraActivity::class.java))
        }
    }
}

@Composable
private fun CameraScreen(
    onBind: (PreviewView, Int) -> Unit,
    onShutter: () -> Unit,
    onClose: () -> Unit,
) {
    val context = LocalContext.current
    var front by remember { mutableStateOf(false) }
    val lens = if (front) CameraSelector.LENS_FACING_FRONT else CameraSelector.LENS_FACING_BACK

    Box(Modifier.fillMaxSize().background(Color.Black)) {
        AndroidView(
            modifier = Modifier.fillMaxSize(),
            factory = { ctx ->
                PreviewView(ctx).also { view ->
                    view.scaleType = PreviewView.ScaleType.FILL_CENTER
                    onBind(view, lens)
                }
            },
            update = { view -> onBind(view, lens) },
        )

        // Top bar: close, and the name of the active lens.
        Row(
            Modifier.fillMaxWidth().padding(16.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Text(
                "✕",
                fontSize = 20.sp,
                color = Color.White,
                modifier = Modifier
                    .clip(RoundedCornerShape(8.dp))
                    .clickable(onClick = onClose)
                    .padding(horizontal = 10.dp, vertical = 4.dp),
            )
            Spacer(Modifier.weight(1f))
            Text(
                if (front) "FRONT" else "REAR",
                fontSize = 11.sp,
                letterSpacing = 2.sp,
                color = Color(0xFF5EEAD4),
                modifier = Modifier
                    .clip(RoundedCornerShape(8.dp))
                    .clickable { front = !front }
                    .padding(horizontal = 10.dp, vertical = 6.dp),
            )
        }

        Column(
            Modifier.align(Alignment.BottomCenter).padding(bottom = 40.dp),
            horizontalAlignment = Alignment.CenterHorizontally,
        ) {
            Text(
                "Describing what she sees needs a vision model",
                fontSize = 11.sp,
                color = Color(0x99FFFFFF),
                textAlign = TextAlign.Center,
                modifier = Modifier.padding(bottom = 14.dp),
            )
            Box(
                Modifier
                    .size(72.dp)
                    .clip(CircleShape)
                    .background(Color.White)
                    .clickable(onClick = onShutter)
            )
        }
    }
}
