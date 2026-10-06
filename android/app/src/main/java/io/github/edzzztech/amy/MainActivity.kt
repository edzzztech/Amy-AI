package io.github.edzzztech.amy

import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.animation.*
import androidx.compose.animation.core.*
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.KeyboardActions
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.alpha
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.graphicsLayer
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.core.content.ContextCompat
import androidx.lifecycle.lifecycleScope
import io.github.edzzztech.amy.core.*
import io.github.edzzztech.amy.ui.DriftingBackground
import io.github.edzzztech.amy.ui.Icon
import io.github.edzzztech.amy.ui.Orb
import io.github.edzzztech.amy.ui.Sym
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.util.Calendar

class MainActivity : ComponentActivity() {

    private val requestMic = registerForActivityResult(
        ActivityResultContracts.RequestPermission()
    ) { granted ->
        if (granted) {
            startAmy()
            askForNotifications()
        } else {
            AmyState.setProblem("Amy needs the microphone to listen.")
        }
    }

    // Optional: she works without it, but on Android 13+ the "listening"
    // notification is hidden and the after-restart reminder never appears.
    private val requestNotifications = registerForActivityResult(
        ActivityResultContracts.RequestPermission()
    ) { }

    private val pickFile = registerForActivityResult(
        ActivityResultContracts.OpenDocument()
    ) { uri -> uri?.let { attach(it) } }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        // imePadding only reports real insets edge to edge; without this the
        // input bar sits behind the keyboard instead of riding above it.
        enableEdgeToEdge()
        Amy.start(this)

        setContent {
            AmyTheme {
                Screen(
                    onSend = { Amy.submit(it, spoken = false) },
                    onMic = ::toggleListening,
                    onNewConversation = Amy::newConversation,
                    onOpenConversation = Amy::openConversation,
                    onAttach = { pickFile.launch(arrayOf("*/*")) },
                    onCamera = { CameraActivity.open(this) },
                    onOverlay = ::toggleOverlay,
                    onPair = { PairActivity.open(this) },
                )
            }
        }

        if (ContextCompat.checkSelfPermission(this, Manifest.permission.RECORD_AUDIO)
            == PackageManager.PERMISSION_GRANTED
        ) {
            startAmy()
            askForNotifications()
        } else {
            requestMic.launch(Manifest.permission.RECORD_AUDIO)
        }
    }

    /** After the microphone, never alongside it: two prompts at once lose one. */
    private fun askForNotifications() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU &&
            ContextCompat.checkSelfPermission(this, Manifest.permission.POST_NOTIFICATIONS)
            != PackageManager.PERMISSION_GRANTED
        ) {
            requestNotifications.launch(Manifest.permission.POST_NOTIFICATIONS)
        }
    }

    /**
     * Read a file and ask her about it in one step. The reading happens off
     * the main thread: the file may be coming from a cloud drive, and waiting
     * for it there froze the whole screen.
     */
    private fun attach(uri: Uri) {
        val reader = Attachments(this)
        lifecycleScope.launch {
            // A picture goes to the model as a picture, for one that can see.
            if (contentResolver.getType(uri)?.startsWith("image/") == true) {
                val picture = withContext(Dispatchers.IO) { Pictures.fromUri(this@MainActivity, uri) }
                if (picture == null) {
                    AmyState.setProblem("I couldn't open that picture.")
                    return@launch
                }
                AmyState.setProblem(null)
                Amy.actions.record("file", "Attached a picture")
                Amy.askAboutImage(
                    picture,
                    shown = "Attached a picture",
                    question = "What's in this picture? Mention anything that stands out.",
                    spoken = false,
                )
                return@launch
            }
            val file = withContext(Dispatchers.IO) { reader.read(uri) }
            if (!file.readable) {
                AmyState.setProblem(
                    "I can't read " + file.name + ". Text, Markdown, CSV, JSON, code, " +
                        "Word documents and pictures are fine; PDFs need a parser I don't have yet."
                )
                return@launch
            }
            AmyState.setProblem(null)
            Amy.actions.record("file", "Attached " + file.name)
            Amy.askAboutFile(file)
        }
    }

    /** The floating orb. Needs a permission only Settings can grant. */
    private fun toggleOverlay() {
        if (!OverlayService.isAllowed(this)) {
            AmyState.setProblem("Allow Amy to draw over other apps, then tap again.")
            startActivity(OverlayService.permissionIntent(this))
            return
        }
        AmyState.setProblem(null)
        // A toggle, not a one-way switch: tapping again takes it off screen.
        if (AmyState.overlayOn.value) {
            stopService(Intent(this, OverlayService::class.java))
        } else {
            startService(Intent(this, OverlayService::class.java))
        }
    }

    /**
     * The mic button. Talking: interrupt her. Asleep: wake her. Otherwise: put
     * her to sleep.
     * Asleep is read from its own flag, not the status, which passes through
     * Speaking and Thinking while she is asleep.
     */
    private fun toggleListening() {
        when {
            AmyState.state.value == Listening.Speaking ||
                AmyState.state.value == Listening.Thinking -> Amy.stopSpeaking()
            AmyState.asleep.value -> send(AmyService.ACTION_LISTEN)
            else -> send(AmyService.ACTION_SLEEP)
        }
    }

    private fun send(action: String) =
        startForegroundService(Intent(this, AmyService::class.java).setAction(action))

    private fun startAmy() = startForegroundService(Intent(this, AmyService::class.java))
}

// Same palette as the desktop and the site.
private val AmyColors = darkColorScheme(
    primary = Color(0xFF5EEAD4),
    secondary = Color(0xFFA78BFA),
    background = Color(0xFF07080C),
    surface = Color(0xFF13161F),
    onBackground = Color(0xFFE8EAF2),
    onSurface = Color(0xFFE8EAF2),
)

/** How close to the bottom counts as "following" a streaming reply. */
private const val FOLLOW_THRESHOLD_PX = 400

private val Line = Color(0x1AFFFFFF)
private val Muted = Color(0xFF8C92A6)

@Composable
fun AmyTheme(content: @Composable () -> Unit) =
    MaterialTheme(colorScheme = AmyColors, content = content)

@Composable
fun Screen(
    onSend: (String) -> Unit,
    onMic: () -> Unit,
    onNewConversation: () -> Unit,
    onOpenConversation: (String) -> Unit,
    onAttach: () -> Unit,
    onCamera: () -> Unit,
    onOverlay: () -> Unit,
    onPair: () -> Unit,
) {
    val drawerState = rememberDrawerState(DrawerValue.Closed)
    val scope = rememberCoroutineScope()
    val history by AmyState.history.collectAsState()

    ModalNavigationDrawer(
        drawerState = drawerState,
        drawerContent = {
            HistoryDrawer(
                history = history,
                onNew = { onNewConversation(); scope.launch { drawerState.close() } },
                onOpen = { onOpenConversation(it); scope.launch { drawerState.close() } },
            )
        },
    ) {
        Home(
            onSend = onSend,
            onMic = onMic,
            onAttach = onAttach,
            onCamera = onCamera,
            onOverlay = onOverlay,
            onPair = onPair,
            onOpenDrawer = { scope.launch { drawerState.open() } },
        )
    }
}

@Composable
private fun HistoryDrawer(
    history: List<Conversation>,
    onNew: () -> Unit,
    onOpen: (String) -> Unit,
) {
    ModalDrawerSheet(drawerContainerColor = MaterialTheme.colorScheme.background) {
        Column(Modifier.padding(horizontal = 18.dp, vertical = 22.dp)) {
            Text("CONVERSATIONS", fontSize = 11.sp, letterSpacing = 2.sp, color = Muted)
            Spacer(Modifier.height(16.dp))
            Text(
                "New conversation",
                fontSize = 14.sp,
                color = MaterialTheme.colorScheme.primary,
                modifier = Modifier
                    .fillMaxWidth()
                    .clip(RoundedCornerShape(12.dp))
                    .clickable(onClick = onNew)
                    .padding(horizontal = 12.dp, vertical = 12.dp),
            )
        }
        HorizontalDivider(color = Line)
        if (history.isEmpty()) {
            Text(
                "Nothing saved yet.",
                fontSize = 13.sp,
                color = Muted,
                modifier = Modifier.padding(18.dp),
            )
        } else {
            LazyColumn {
                items(history, key = { it.id }) { conversation ->
                    Column(
                        Modifier
                            .fillMaxWidth()
                            .clickable { onOpen(conversation.id) }
                            .padding(horizontal = 18.dp, vertical = 12.dp)
                    ) {
                        Text(
                            conversation.title,
                            fontSize = 14.sp,
                            color = MaterialTheme.colorScheme.onSurface,
                            maxLines = 1,
                        )
                        Spacer(Modifier.height(3.dp))
                        Text("${conversation.turns.size} messages", fontSize = 11.sp, color = Muted)
                    }
                }
            }
        }
    }
}

@Composable
private fun Home(
    onSend: (String) -> Unit,
    onMic: () -> Unit,
    onAttach: () -> Unit,
    onCamera: () -> Unit,
    onOverlay: () -> Unit,
    onPair: () -> Unit,
    onOpenDrawer: () -> Unit,
) {
    val state by AmyState.state.collectAsState()
    val heard by AmyState.heard.collectAsState()
    val reply by AmyState.reply.collectAsState()
    val turns by AmyState.turns.collectAsState()
    val problem by AmyState.problem.collectAsState()
    var draft by remember { mutableStateOf("") }

    // The orb shrinks out of the way once there is a conversation to read,
    // rather than the screen jumping between two layouts.
    val hasTurns = turns.isNotEmpty()
    val orbSize by animateDpAsState(
        targetValue = if (hasTurns) 88.dp else 180.dp,
        animationSpec = spring(
            dampingRatio = Spring.DampingRatioLowBouncy,
            stiffness = Spring.StiffnessLow,
        ),
        label = "orbSize",
    )

    val level by AmyState.level.collectAsState()
    val overlayOn by AmyState.overlayOn.collectAsState()
    val asleep by AmyState.asleep.collectAsState()

    Box(Modifier.fillMaxSize()) {
        DriftingBackground(Modifier.fillMaxSize())
        Column(Modifier.fillMaxSize().statusBarsPadding()) {

            Row(
                Modifier.fillMaxWidth().padding(horizontal = 10.dp, vertical = 8.dp),
                verticalAlignment = Alignment.CenterVertically,
            ) {
                Box(
                    Modifier
                        .clip(CircleShape)
                        .clickable(role = Role.Button, onClick = onOpenDrawer)
                        .padding(10.dp),
                ) { Icon(Sym.Menu, Muted, label = "Conversations") }
                Spacer(Modifier.width(6.dp))
                Text("Amy", fontSize = 17.sp, color = MaterialTheme.colorScheme.onBackground)
                Spacer(Modifier.weight(1f))
                StatusPill(state)
                Spacer(Modifier.width(10.dp))
            }

            // Follow the conversation as it grows, the way a chat should. New
            // turns always scroll into view; a streaming reply only pulls you
            // down if you were already near the bottom, so scrolling up to
            // reread something is not fought by every new word.
            val scroll = rememberScrollState()
            LaunchedEffect(turns.size) {
                scroll.animateScrollTo(scroll.maxValue)
            }
            LaunchedEffect(reply.length) {
                if (scroll.maxValue - scroll.value < FOLLOW_THRESHOLD_PX) {
                    scroll.scrollTo(scroll.maxValue)
                }
            }

            Column(
                modifier = Modifier
                    .weight(1f)
                    .fillMaxWidth()
                    .verticalScroll(scroll)
                    .padding(horizontal = 22.dp),
                horizontalAlignment = Alignment.CenterHorizontally,
            ) {
                Spacer(Modifier.height(14.dp))
                Orb(state = state, level = level, modifier = Modifier.size(orbSize))

                AnimatedVisibility(visible = !hasTurns, enter = fadeIn(), exit = fadeOut()) {
                    Column(horizontalAlignment = Alignment.CenterHorizontally) {
                        Spacer(Modifier.height(22.dp))
                        Text(
                            greeting(),
                            fontSize = 27.sp,
                            color = MaterialTheme.colorScheme.onBackground,
                        )
                        Spacer(Modifier.height(6.dp))
                        Text("Say \"Amy\", or ask me something", fontSize = 14.sp, color = Muted)
                        Spacer(Modifier.height(20.dp))
                        Row(Modifier.horizontalScroll(rememberScrollState())) {
                            SUGGESTIONS.forEach { text ->
                                Chip(text) { onSend(text) }
                                Spacer(Modifier.width(8.dp))
                            }
                        }
                    }
                }

                AnimatedVisibility(
                    visible = heard.isNotEmpty(),
                    enter = fadeIn() + expandVertically(),
                    exit = fadeOut() + shrinkVertically(),
                ) {
                    Text(
                        heard,
                        fontSize = 16.sp,
                        textAlign = TextAlign.Center,
                        color = Muted,
                        modifier = Modifier.padding(top = 14.dp),
                    )
                }

                problem?.let {
                    Spacer(Modifier.height(12.dp))
                    Text(
                        it,
                        fontSize = 13.sp,
                        textAlign = TextAlign.Center,
                        color = Color(0xFFE57373),
                    )
                }

                Spacer(Modifier.height(22.dp))

                turns.forEach { turn -> Bubble(turn) }
                if (reply.isNotEmpty()) Bubble(Turn("amy", reply, ""), streaming = true)

                Spacer(Modifier.height(16.dp))
            }

            Row(
                Modifier
                    .fillMaxWidth()
                    // Lifts the bar above the keyboard instead of it covering
                    // whatever you are typing into.
                    .imePadding()
                    .navigationBarsPadding()
                    .padding(horizontal = 10.dp, vertical = 10.dp),
                verticalAlignment = Alignment.CenterVertically,
            ) {
                ToolButton(Sym.Attach, "Attach a file", onAttach)
                Spacer(Modifier.width(4.dp))
                ToolButton(Sym.Camera, "Camera", onCamera)
                Spacer(Modifier.width(4.dp))
                ToolButton(
                    Sym.Orb,
                    if (overlayOn) "Hide the floating orb" else "Show the floating orb",
                    onOverlay,
                    active = overlayOn,
                )
                Spacer(Modifier.width(4.dp))
                ToolButton(Sym.Desktop, "Pair with your computer", onPair)
                Spacer(Modifier.width(6.dp))
                OutlinedTextField(
                    value = draft,
                    onValueChange = { draft = it },
                    placeholder = { Text("Ask Amy", color = Muted, fontSize = 14.sp) },
                    singleLine = true,
                    shape = RoundedCornerShape(26.dp),
                    modifier = Modifier.weight(1f),
                    keyboardOptions = KeyboardOptions(imeAction = ImeAction.Send),
                    keyboardActions = KeyboardActions(onSend = {
                        onSend(draft); draft = ""
                    }),
                    colors = OutlinedTextFieldDefaults.colors(
                        focusedBorderColor = MaterialTheme.colorScheme.primary,
                        unfocusedBorderColor = Line,
                        focusedTextColor = MaterialTheme.colorScheme.onBackground,
                        unfocusedTextColor = MaterialTheme.colorScheme.onBackground,
                    ),
                )
                Spacer(Modifier.width(6.dp))
                MicOrSend(hasDraft = draft.isNotBlank(), state = state, asleep = asleep) {
                    if (draft.isNotBlank()) {
                        onSend(draft); draft = ""
                    } else onMic()
                }
            }
        }
    }
}

@Composable
private fun StatusPill(state: Listening) {
    val label = when (state) {
        Listening.Idle -> "ready"
        Listening.Listening -> "listening"
        Listening.Thinking -> "thinking"
        Listening.Speaking -> "speaking"
        Listening.Muted -> "asleep"
    }
    val tint = if (state == Listening.Muted) Muted else MaterialTheme.colorScheme.primary
    val working = state == Listening.Thinking || state == Listening.Listening
    // Only animate while she is working: an infinite transition left running
    // at rest redraws every frame for nothing, which is battery for no reason.
    val alpha = if (working) {
        val pulse = rememberInfiniteTransition(label = "pill")
        pulse.animateFloat(
            initialValue = 0.45f,
            targetValue = 1f,
            animationSpec = infiniteRepeatable(tween(900), RepeatMode.Reverse),
            label = "pulseAlpha",
        ).value
    } else 1f
    Text(
        label,
        fontSize = 11.sp,
        letterSpacing = 1.5.sp,
        color = tint,
        modifier = Modifier.graphicsLayer { this.alpha = alpha },
    )
}

/**
 * One message.
 *
 * Only your messages fade in. Hers stream in word by word, which is its own
 * entrance; fading them as well was what broke the streaming reply. Its fade
 * state was keyed on the text length, so it reset to invisible on every token,
 * and the effect that makes it visible only ever fires once — the reply faded
 * out as it arrived and stayed gone.
 */
@Composable
private fun Bubble(turn: Turn, streaming: Boolean = false) {
    val mine = turn.role == "you"
    var shown by remember(turn.at) { mutableStateOf(!mine) }
    LaunchedEffect(turn.at) { shown = true }
    val alpha by animateFloatAsState(
        targetValue = if (shown) 1f else 0f,
        animationSpec = tween(260),
        label = "bubble",
    )

    Column(
        Modifier
            .fillMaxWidth()
            .padding(bottom = 16.dp)
            .graphicsLayer { this.alpha = if (streaming) 1f else alpha },
        horizontalAlignment = if (mine) Alignment.End else Alignment.Start,
    ) {
        if (!mine) {
            Text(
                "Amy",
                fontSize = 11.sp,
                letterSpacing = 1.sp,
                color = MaterialTheme.colorScheme.primary,
            )
            Spacer(Modifier.height(6.dp))
        }
        Surface(
            color = if (mine) MaterialTheme.colorScheme.surface else Color.Transparent,
            shape = RoundedCornerShape(
                topStart = 20.dp,
                topEnd = 20.dp,
                bottomStart = if (mine) 20.dp else 4.dp,
                bottomEnd = if (mine) 4.dp else 20.dp,
            ),
        ) {
            Text(
                turn.text + if (streaming) "..." else "",
                fontSize = 15.sp,
                lineHeight = 23.sp,
                color = MaterialTheme.colorScheme.onBackground,
                modifier = Modifier.padding(
                    horizontal = if (mine) 16.dp else 0.dp,
                    vertical = if (mine) 11.dp else 0.dp,
                ),
            )
        }
    }
}

/**
 * The send-or-listen button.
 *
 * It breathes while listening by scaling its *drawing*, not its size. Animating
 * the size changed the layout every frame, which made the whole input row
 * re-measure sixty times a second — the opposite of smooth. A graphics-layer
 * scale is applied on the GPU after layout and costs nothing.
 */
@Composable
private fun MicOrSend(hasDraft: Boolean, state: Listening, asleep: Boolean, onClick: () -> Unit) {
    val listening = state == Listening.Listening
    val busy = state == Listening.Speaking || state == Listening.Thinking
    val scale = if (listening && !hasDraft) {
        val breath = rememberInfiniteTransition(label = "mic")
        breath.animateFloat(
            initialValue = 1f,
            targetValue = 1.07f,
            animationSpec = infiniteRepeatable(tween(700), RepeatMode.Reverse),
            label = "micScale",
        ).value
    } else 1f
    // What a tap does decides what it shows, so the button never says one
    // thing and does another: the mic used to read "listen" while a tap put
    // her to sleep.
    val (sym, label) = when {
        hasDraft -> Sym.Send to "Send"
        busy -> Sym.Stop to "Stop"
        asleep -> Sym.Sleep to "Wake Amy"
        else -> Sym.Mic to "Put Amy to sleep"
    }
    val dim = sym == Sym.Sleep
    Box(
        Modifier
            .size(48.dp)
            .graphicsLayer {
                scaleX = scale
                scaleY = scale
            }
            .clip(CircleShape)
            .background(
                if (dim) MaterialTheme.colorScheme.surface
                else MaterialTheme.colorScheme.primary
            )
            .clickable(role = Role.Button, onClick = onClick),
        contentAlignment = Alignment.Center,
    ) {
        Icon(sym, if (dim) Muted else Color(0xFF04221D), size = 20.dp, label = label)
    }
}

@Composable
private fun ToolButton(sym: Sym, label: String, onClick: () -> Unit, active: Boolean = false) {
    Box(
        Modifier
            .size(42.dp)
            .clip(CircleShape)
            .background(
                if (active) MaterialTheme.colorScheme.primary.copy(alpha = 0.18f)
                else MaterialTheme.colorScheme.surface
            )
            .clickable(role = Role.Button, onClick = onClick),
        contentAlignment = Alignment.Center,
    ) {
        Icon(
            sym,
            if (active) MaterialTheme.colorScheme.primary else Muted,
            size = 19.dp,
            label = label,
        )
    }
}

/** A tappable opener, the way Gemini offers starting points. */
@Composable
private fun Chip(text: String, onClick: () -> Unit) {
    Text(
        text,
        fontSize = 13.sp,
        color = MaterialTheme.colorScheme.onBackground,
        modifier = Modifier
            .clip(RoundedCornerShape(20.dp))
            .background(MaterialTheme.colorScheme.surface)
            .clickable(onClick = onClick)
            .padding(horizontal = 15.dp, vertical = 11.dp),
    )
}

private val SUGGESTIONS = listOf(
    "What's on my screen?",
    "Open Spotify",
    "What have you done today?",
    "How many apps can you see?",
)

/** Time of day greeting, which is most of what makes an assistant feel present. */
private fun greeting(): String =
    when (Calendar.getInstance().get(Calendar.HOUR_OF_DAY)) {
        in 0..11 -> "Good morning"
        in 12..17 -> "Good afternoon"
        else -> "Good evening"
    }
