package io.github.edzzztech.amy

import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
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
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.core.content.ContextCompat
import io.github.edzzztech.amy.core.*
import io.github.edzzztech.amy.ui.DriftingBackground
import io.github.edzzztech.amy.ui.Icon
import io.github.edzzztech.amy.ui.Orb
import io.github.edzzztech.amy.ui.Sym
import kotlinx.coroutines.launch
import java.util.Calendar

class MainActivity : ComponentActivity() {

    private val requestMic = registerForActivityResult(
        ActivityResultContracts.RequestPermission()
    ) { granted ->
        if (granted) startAmy() else AmyState.setProblem("Amy needs the microphone to listen.")
    }

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
                )
            }
        }

        if (ContextCompat.checkSelfPermission(this, Manifest.permission.RECORD_AUDIO)
            == PackageManager.PERMISSION_GRANTED
        ) {
            startAmy()
        } else {
            requestMic.launch(Manifest.permission.RECORD_AUDIO)
        }
    }

    /** Read a file and ask her about it in one step. */
    private fun attach(uri: Uri) {
        val reader = Attachments(this)
        val file = reader.read(uri)
        if (!file.readable) {
            AmyState.setProblem(
                "I can't read " + file.name + ". Plain text, Markdown, CSV, JSON and " +
                    "code are fine; PDFs and Word files need a parser I don't have yet."
            )
            return
        }
        AmyState.setProblem(null)
        Amy.actions.record("file", "Attached " + file.name)
        Amy.submitRaw(shown = "Attached " + file.name, prompt = reader.asPrompt(file, ""))
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

    /** The mic button wakes or sleeps the always-on listener. */
    private fun toggleListening() {
        when (AmyState.state.value) {
            Listening.Speaking -> Amy.stopSpeaking()
            Listening.Muted -> send(AmyService.ACTION_LISTEN)
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
                        .clickable(onClick = onOpenDrawer)
                        .padding(10.dp),
                ) { Icon(Sym.Menu, Muted) }
                Spacer(Modifier.width(6.dp))
                Text("Amy", fontSize = 17.sp, color = MaterialTheme.colorScheme.onBackground)
                Spacer(Modifier.weight(1f))
                StatusPill(state)
                Spacer(Modifier.width(10.dp))
            }

            Column(
                modifier = Modifier
                    .weight(1f)
                    .fillMaxWidth()
                    .verticalScroll(rememberScrollState())
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
                ToolButton(Sym.Attach, onAttach)
                Spacer(Modifier.width(4.dp))
                ToolButton(Sym.Camera, onCamera)
                Spacer(Modifier.width(4.dp))
                ToolButton(Sym.Orb, onOverlay, active = overlayOn)
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
                MicOrSend(hasDraft = draft.isNotBlank(), state = state) {
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
    val pulse = rememberInfiniteTransition(label = "pill")
    val alpha by pulse.animateFloat(
        initialValue = if (working) 0.45f else 1f,
        targetValue = 1f,
        animationSpec = infiniteRepeatable(tween(900), RepeatMode.Reverse),
        label = "pulseAlpha",
    )
    Text(
        label,
        fontSize = 11.sp,
        letterSpacing = 1.5.sp,
        color = tint,
        modifier = Modifier.alpha(alpha),
    )
}

@Composable
private fun Bubble(turn: Turn, streaming: Boolean = false) {
    val mine = turn.role == "you"
    var shown by remember(turn.at, turn.text.length) { mutableStateOf(false) }
    LaunchedEffect(Unit) { shown = true }
    val alpha by animateFloatAsState(
        targetValue = if (shown) 1f else 0f,
        animationSpec = tween(260),
        label = "bubble",
    )

    Column(
        Modifier.fillMaxWidth().padding(bottom = 16.dp).alpha(alpha),
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

@Composable
private fun MicOrSend(hasDraft: Boolean, state: Listening, onClick: () -> Unit) {
    val listening = state == Listening.Listening
    val scale by animateFloatAsState(
        targetValue = if (listening) 1.06f else 1f,
        animationSpec = if (listening) {
            infiniteRepeatable(tween(700), RepeatMode.Reverse)
        } else tween(200),
        label = "micScale",
    )
    val sleeping = state == Listening.Muted && !hasDraft
    Box(
        Modifier
            .size((48 * scale).dp)
            .clip(CircleShape)
            .background(
                if (sleeping) MaterialTheme.colorScheme.surface
                else MaterialTheme.colorScheme.primary
            )
            .clickable(onClick = onClick),
        contentAlignment = Alignment.Center,
    ) {
        Icon(
            when {
                hasDraft -> Sym.Send
                state == Listening.Muted -> Sym.Sleep
                listening -> Sym.Stop
                else -> Sym.Mic
            },
            if (sleeping) Muted else Color(0xFF04221D),
            20.dp,
        )
    }
}

@Composable
private fun ToolButton(sym: Sym, onClick: () -> Unit, active: Boolean = false) {
    Box(
        Modifier
            .size(42.dp)
            .clip(CircleShape)
            .background(
                if (active) MaterialTheme.colorScheme.primary.copy(alpha = 0.18f)
                else MaterialTheme.colorScheme.surface
            )
            .clickable(onClick = onClick),
        contentAlignment = Alignment.Center,
    ) { Icon(sym, if (active) MaterialTheme.colorScheme.primary else Muted, 19.dp) }
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
