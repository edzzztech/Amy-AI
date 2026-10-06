package io.github.edzzztech.amy

import android.Manifest
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
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
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.compose.ui.viewinterop.AndroidView
import androidx.core.content.ContextCompat
import androidx.lifecycle.lifecycleScope
import io.github.edzzztech.amy.core.Amy
import io.github.edzzztech.amy.core.Pictures
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import io.github.edzzztech.amy.ui.Icon
import io.github.edzzztech.amy.ui.Sym
import java.io.File
import java.time.LocalDateTime
import java.time.format.DateTimeFormatter

/**
 * Camera mode: the phone's version of the desktop's desk view.
 *
 * Captures to the app's own storage and records the shot in the action log.
 * The photo then appears in the conversation. With a model that can see
 * (Gemma 3n) she says what is in it; with any other she says why she can't.
 */
class CameraActivity : ComponentActivity() {

    private var capture: ImageCapture? = null

    /**
     * Whether the camera may be used. State, so that granting permission on
     * first use binds the camera: before, the preview had already tried and
     * failed while the prompt was up, and stayed black after "Allow".
     */
    private var allowed by mutableStateOf(false)

    /** Whether the model on the phone can describe a photo. Read off the main thread. */
    private var canSee by mutableStateOf(false)

    private val requestCamera = registerForActivityResult(
        ActivityResultContracts.RequestPermission()
    ) { granted -> if (granted) allowed = true else finish() }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        Amy.start(this)
        enableEdgeToEdge()

        allowed = ContextCompat.checkSelfPermission(this, Manifest.permission.CAMERA) ==
            PackageManager.PERMISSION_GRANTED
        if (!allowed) requestCamera.launch(Manifest.permission.CAMERA)
        lifecycleScope.launch {
            canSee = withContext(Dispatchers.IO) { Amy.llm.canSee() }
        }

        setContent {
            AmyTheme {
                CameraScreen(
                    allowed = allowed,
                    canSee = canSee,
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
        val name = LocalDateTime.now().format(PHOTO_NAME)
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

    /**
     * Put the photo in the conversation and have her describe it aloud: the
     * camera was opened to ask about something. The photo and her answer are
     * there as the camera closes.
     */
    private fun onCaptured(file: File) {
        Amy.actions.record("device", "Photo taken: ${file.name}")
        lifecycleScope.launch {
            val picture = withContext(Dispatchers.IO) { Pictures.fromFile(file) }
            if (picture == null) {
                Amy.tts?.speak("Saved, but I couldn't read the photo back.")
            } else {
                Amy.askAboutImage(
                    picture,
                    question = "Describe what you see in a sentence or two.",
                    spoken = true,
                )
            }
            finish()
        }
    }

    companion object {
        private val PHOTO_NAME = DateTimeFormatter.ofPattern("yyyyMMdd-HHmmss")

        fun open(context: Context) {
            context.startActivity(Intent(context, CameraActivity::class.java))
        }
    }
}

@Composable
private fun CameraScreen(
    allowed: Boolean,
    canSee: Boolean,
    onBind: (PreviewView, Int) -> Unit,
    onShutter: () -> Unit,
    onClose: () -> Unit,
) {
    var front by remember { mutableStateOf(false) }
    val lens = if (front) CameraSelector.LENS_FACING_FRONT else CameraSelector.LENS_FACING_BACK

    Box(Modifier.fillMaxSize().background(Color.Black)) {
        // Bind once per lens. Binding from both factory and update ran it on
        // creation and again on every recomposition, so the camera was torn
        // down and rebuilt repeatedly — a flicker each time.
        var preview by remember { mutableStateOf<PreviewView?>(null) }
        AndroidView(
            modifier = Modifier.fillMaxSize(),
            factory = { ctx ->
                PreviewView(ctx).also { view ->
                    view.scaleType = PreviewView.ScaleType.FILL_CENTER
                    preview = view
                }
            },
        )
        LaunchedEffect(preview, lens, allowed) {
            if (allowed) preview?.let { onBind(it, lens) }
        }

        // Top bar: close, and the name of the active lens. Clear of the
        // status bar: from Android 15 every screen runs edge to edge.
        Row(
            Modifier.fillMaxWidth().statusBarsPadding().padding(16.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Box(
                Modifier
                    .clip(CircleShape)
                    .clickable(role = Role.Button, onClick = onClose)
                    .padding(10.dp),
            ) { Icon(Sym.Close, Color.White, size = 20.dp, label = "Close camera") }
            Spacer(Modifier.weight(1f))
            Text(
                if (front) "FRONT" else "REAR",
                fontSize = 11.sp,
                letterSpacing = 2.sp,
                color = Color(0xFF5EEAD4),
                modifier = Modifier
                    .clip(RoundedCornerShape(8.dp))
                    .clickable(onClickLabel = "Switch camera", role = Role.Button) {
                        front = !front
                    }
                    .padding(horizontal = 10.dp, vertical = 6.dp),
            )
        }

        Column(
            Modifier
                .align(Alignment.BottomCenter)
                .navigationBarsPadding()
                .padding(bottom = 40.dp),
            horizontalAlignment = Alignment.CenterHorizontally,
        ) {
            Text(
                if (canSee) "Take a photo and I'll tell you what's in it"
                else "Photos are saved. To describe them I need a Gemma 3n model",
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
                    .clickable(onClickLabel = "Take a photo", role = Role.Button,
                        onClick = onShutter)
                    .semantics { contentDescription = "Shutter" }
            )
        }
    }
}
