package io.github.edzzztech.amy.automation

import kotlin.math.roundToInt

/**
 * What was asked, worked out with no Android in sight, so that every pattern
 * [Commands] relies on is covered by plain JVM tests.
 *
 * The patterns are anchored on purpose. "What apps should I use for
 * budgeting?" has to reach the model, not be answered with an app count, and
 * "write me a poem" is not a request to type into a text field. A loose match
 * is how an assistant ends up confidently doing the wrong thing.
 */
object Phrases {

    // --- tidying ---------------------------------------------------------

    /**
     * "Could you please open Spotify for me?" -> "open Spotify". Case is kept.
     * Matching only the bare form missed every polite one, which then went to
     * the model — and a model saying "Opening Spotify" opens nothing.
     */
    fun bare(input: String): String =
        POLITE_END.replace(politeStartRemoved(input).trimEnd(*TRAILING), "").trimEnd(*TRAILING).trim()

    /** Only the polite opening removed: text to type is typed exactly as said. */
    fun politeStartRemoved(input: String): String = POLITE_START.replace(input.trim(), "")

    // --- questions answered from the phone itself ------------------------

    fun asksTime(lower: String) = TIME.matches(lower)
    fun asksDate(lower: String) = DATE.matches(lower)
    fun asksBattery(lower: String) = BATTERY.matches(lower)
    fun asksAppCount(lower: String) = APP_COUNT.matches(lower)
    fun asksScreen(lower: String) = SCREEN.containsMatchIn(lower)

    /** Hours of the action log to read back, or null if that is not the question. */
    fun actionLogHours(lower: String): Int? {
        val m = ACTION_LOG.matchEntire(lower) ?: return null
        return m.groupValues[1].toIntOrNull()?.coerceIn(1, 24 * 7) ?: 24
    }

    // --- things to do ----------------------------------------------------

    data class Open(val verb: String, val name: String) {
        /** "Open" and "launch" nearly always mean an app; "start" and "run" often do not. */
        val definite: Boolean get() = verb == "open" || verb == "launch"
    }

    fun open(lower: String): Open? {
        val m = OPEN.matchEntire(lower) ?: return null
        val name = m.groupValues[2].trim()
            .removeSuffix(" application").removeSuffix(" app").trim()
        return if (name.isEmpty()) null else Open(m.groupValues[1], name)
    }

    fun isBack(lower: String) = BACK.matches(lower)
    fun isHome(lower: String) = HOME.matches(lower)

    /** What to tap, or null. */
    fun tapTarget(lower: String): String? =
        TAP.matchEntire(lower)?.groupValues?.get(1)?.trim()?.takeIf { it.isNotEmpty() }

    /** What to type, case kept, or null. Takes the text with only the polite opening removed. */
    fun typed(text: String): String? =
        TYPE.matchEntire(text.trim())?.groupValues?.get(1)?.trim()?.takeIf { it.isNotEmpty() }

    /** True for on, false for off, null when this is not about the torch. */
    fun torch(lower: String): Boolean? {
        val m = TORCH_VERB_FIRST.matchEntire(lower) ?: TORCH_STATE_LAST.matchEntire(lower)
            ?: return null
        return m.groupValues[1] == "on"
    }

    sealed interface Volume {
        data class Step(val up: Boolean) : Volume
        data class Percent(val value: Int) : Volume
    }

    fun volume(lower: String): Volume? {
        if (VOLUME_MAX.matches(lower)) return Volume.Percent(100)
        VOLUME_SET.matchEntire(lower)?.let { m ->
            return Volume.Percent(m.groupValues[1].toInt().coerceIn(0, 100))
        }
        val m = VOLUME_STEP.matchEntire(lower) ?: return null
        val word = m.groupValues.drop(1).firstOrNull { it.isNotEmpty() } ?: return null
        return Volume.Step(up = word == "up" || word == "louder")
    }

    /** Seconds for "set a timer for 5 minutes" or "a ten minute timer", or null. */
    fun timer(lower: String): Int? {
        val m = TIMER_FOR.matchEntire(lower) ?: TIMER_AFTER.matchEntire(lower) ?: return null
        return durationSeconds(m.groupValues[1])
    }

    /**
     * Hour (0-23) and minute for "set an alarm for 7:30", or null. "An alarm
     * in 20 minutes" counts forward from now, rather than reading the 20 as a
     * time of day.
     */
    fun alarm(lower: String, nowHour: Int, nowMinute: Int): Pair<Int, Int>? {
        val m = ALARM.matchEntire(lower) ?: return null
        val rest = m.groupValues[1].trim()
        RELATIVE.matchEntire(rest)
            ?.let { r -> durationSeconds(r.groupValues[1].ifEmpty { r.groupValues[2] }) }
            ?.let { seconds ->
                val at = (nowHour * 60 + nowMinute + (seconds + 59) / 60) % (24 * 60)
                return at / 60 to at % 60
            }
        // Not a duration after all ("in the morning at 7"): read it as a time.
        return clockTime(rest, nowHour, nowMinute)
    }

    // --- durations and clock times ---------------------------------------

    /**
     * "5 minutes", "an hour and a half", "twenty five seconds", "1 hour 20
     * minutes" -> seconds. A bare number is taken as minutes, which is what
     * people mean by "a timer for 5". Null past a day, the most a clock app's
     * timer accepts.
     */
    fun durationSeconds(text: String): Int? {
        val s = " " + numbersAsDigits(text) + " "
        val spoken = s
            .replace(" half an hour ", " 30 minutes ")
            .replace(" half a minute ", " 30 seconds ")
            .replace(" a quarter of an hour ", " 15 minutes ")
            .replace(" quarter of an hour ", " 15 minutes ")
            .replace(" a quarter hour ", " 15 minutes ")
        var total = 0.0
        var found = false
        for (m in DURATION_PART.findAll(spoken)) {
            found = true
            val n = m.groupValues[1].let { if (it == "a" || it == "an") 1.0 else it.toDouble() }
            val half = if (m.groupValues[2].isNotEmpty() || m.groupValues[4].isNotEmpty()) 0.5 else 0.0
            total += (n + half) * unitSeconds(m.groupValues[3])
        }
        if (!found) {
            val bare = spoken.trim().toDoubleOrNull() ?: return null
            total = bare * 60
        }
        val seconds = total.roundToInt()
        return if (seconds in 1..MAX_TIMER_SECONDS) seconds else null
    }

    /**
     * "7", "7am", "7:30 pm", "19:45", "half past six", "noon" -> hour and
     * minute. Without am or pm the next one is meant: "7" said in the
     * afternoon is 7pm, said late at night is 7am.
     */
    fun clockTime(text: String, nowHour: Int, nowMinute: Int): Pair<Int, Int>? {
        // "7.30" is how much of the world writes 7:30, and "p.m." is pm.
        var s = " " + numbersAsDigits(text)
            .replace(DOTTED_TIME, "$1:$2")
            .replace(DOTTED_HALF, "$1m")
            .replace("o'clock", "")
            .replace("oclock", "")
            .replace(MORNING, "am")
            .replace(EVENING, "pm") + " "
        if (" noon " in s || " midday " in s) return 12 to 0
        if (" midnight " in s) return 0 to 0
        s = HALF_PAST.replace(s) { "${it.groupValues[1]}:30" }
        s = QUARTER_PAST.replace(s) { "${it.groupValues[1]}:15" }
        s = QUARTER_TO.replace(s) { m ->
            val h = m.groupValues[1].toInt()
            "${if (h <= 1) 12 else h - 1}:45"
        }
        val m = CLOCK.find(s) ?: return null
        var hour = m.groupValues[1].toInt()
        val minute = m.groupValues[2].toIntOrNull() ?: 0
        if (minute > 59) return null
        // "In the morning at 7" puts the am before the number.
        val half = m.groupValues[3].ifEmpty {
            when {
                " am " in s -> "am"
                " pm " in s -> "pm"
                else -> ""
            }
        }
        when (half) {
            "am" -> {
                if (hour !in 1..12) return null
                if (hour == 12) hour = 0
            }
            "pm" -> {
                if (hour !in 1..12) return null
                if (hour != 12) hour += 12
            }
            else -> {
                if (hour > 23) return null
                if (hour in 1..12) {
                    val morning = hour                       // 12 alone is noon
                    val evening = if (hour == 12) 0 else hour + 12
                    hour = if (minutesUntil(nowHour, nowMinute, morning, minute) <=
                        minutesUntil(nowHour, nowMinute, evening, minute)
                    ) morning else evening
                }
            }
        }
        return hour to minute
    }

    /** 5400 -> "1 hour and 30 minutes", for saying back what was set. */
    fun describe(seconds: Int): String {
        val parts = listOfNotNull(
            (seconds / 3600).takeIf { it > 0 }?.let { plural(it, "hour") },
            (seconds % 3600 / 60).takeIf { it > 0 }?.let { plural(it, "minute") },
            (seconds % 60).takeIf { it > 0 }?.let { plural(it, "second") },
        )
        return when (parts.size) {
            0 -> "0 seconds"
            1 -> parts[0]
            else -> parts.dropLast(1).joinToString(", ") + " and " + parts.last()
        }
    }

    private fun plural(n: Int, unit: String) = if (n == 1) "1 $unit" else "$n ${unit}s"

    /** Minutes from now until the clock next reads [hour]:[minute]; never zero. */
    private fun minutesUntil(nowHour: Int, nowMinute: Int, hour: Int, minute: Int): Int {
        val diff = (hour * 60 + minute) - (nowHour * 60 + nowMinute)
        return if (diff <= 0) diff + 24 * 60 else diff
    }

    private fun unitSeconds(unit: String): Int = when {
        unit.startsWith("h") -> 3600
        unit.startsWith("m") -> 60
        else -> 1
    }

    /**
     * Speech recognition writes "five" as often as "5". Lower-cases, turns
     * number words into digits, and joins "twenty five" into 25.
     */
    internal fun numbersAsDigits(text: String): String {
        var s = text.lowercase().replace('-', ' ').replace(Regex("\\s+"), " ").trim()
        for ((word, n) in NUMBER_WORDS) s = s.replace(Regex("\\b$word\\b"), n.toString())
        return TENS_AND_UNITS.replace(s) { m ->
            (m.groupValues[1].toInt() + m.groupValues[2].toInt()).toString()
        }
    }

    // --- patterns ----------------------------------------------------------

    private const val MAX_TIMER_SECONDS = 24 * 60 * 60
    private val TRAILING = charArrayOf('.', '!', '?', ',', ' ', '\u2026')

    private val NUMBER_WORDS = listOf(
        "zero" to 0, "one" to 1, "two" to 2, "three" to 3, "four" to 4, "five" to 5,
        "six" to 6, "seven" to 7, "eight" to 8, "nine" to 9, "ten" to 10,
        "eleven" to 11, "twelve" to 12, "thirteen" to 13, "fourteen" to 14,
        "fifteen" to 15, "sixteen" to 16, "seventeen" to 17, "eighteen" to 18,
        "nineteen" to 19, "twenty" to 20, "thirty" to 30, "forty" to 40,
        "fifty" to 50, "sixty" to 60, "seventy" to 70, "eighty" to 80, "ninety" to 90,
    )
    private val TENS_AND_UNITS = Regex("\\b([2-9]0) ([1-9])\\b")

    private val POLITE_START = Regex(
        "^(?:(?:(?:can|could|would|will) you |please |just |go ahead and |" +
            "i want you to |i'd like you to |i need you to ))+",
        RegexOption.IGNORE_CASE,
    )
    // "Now" is politeness in "open it now" but meaning in "20 minutes from
    // now", so it is left alone after "from".
    private val POLITE_END = Regex(
        "(?:,? (?:please|for me|thanks|thank you)|(?<!from),?(?: right)? now)+$",
        RegexOption.IGNORE_CASE,
    )

    private val TIME = Regex(
        "(?:what(?:'s| is) the time|what time is it|tell me the time|the time|time)" +
            "(?: now| right now)?"
    )
    private val DATE = Regex(
        "(?:what(?:'s| is) (?:the |today's )?date|what(?:'s| is) today|" +
            "what day is (?:it|today)|what(?:'s| is) the day|today's date|the date|date)" +
            "(?: today)?"
    )
    private val BATTERY = Regex(
        "(?:what(?:'s| is) )?(?:my |the )?(?:phone's )?battery" +
            "(?: level| percentage| life| status)?(?: at)?|" +
            "how much battery(?: do i have| have i got| is left| have i left| left)?|" +
            "how(?:'s| is) (?:my |the )?battery(?: doing)?"
    )
    private val APP_COUNT = Regex(
        "(?:how many apps|what apps)(?: do i have| have i got| can you see| are there| " +
            "are installed| are on (?:this|my|the) phone)?(?: installed)?"
    )
    private val SCREEN = Regex(
        "what(?:'s| is| can you see)? on (?:the |my )?screen|read (?:me )?(?:the |my )?screen"
    )
    private val ACTION_LOG = Regex(
        "(?:what (?:have you|did you) (?:done|do)|what have you been (?:doing|up to)|" +
            "(?:show me |read me |read )?(?:your |the )?action log)" +
            "(?: today| recently| lately| in the last (\\d{1,3}) hours?)?"
    )

    private val OPEN = Regex("(open|launch|start|run|load) (?:up )?(?:the |my )?(.+)")
    private val BACK = Regex("(?:go )?back")
    private val HOME = Regex("(?:go )?home|(?:go to )?(?:the )?home ?screen")
    private val TAP = Regex("(?:tap|press|click|select|hit) (?:on )?(?:the )?(.+?)(?: button)?")
    private val TYPE = Regex("(?:type|enter)(?: in| out)? (?!of )(.+)", RegexOption.IGNORE_CASE)

    private const val TORCH = "(?:torch|flashlight|flash light|flash)"
    private val TORCH_VERB_FIRST = Regex("(?:turn|switch|put) (on|off) (?:the |my )?$TORCH")
    private val TORCH_STATE_LAST = Regex("(?:(?:turn|switch|put) )?(?:the |my )?$TORCH (on|off)")

    private val VOLUME_STEP = Regex(
        "(?:turn )?(?:the )?(?:volume|sound|music) (up|down)|turn it (up|down)|" +
            "turn (up|down) (?:the )?(?:volume|sound|music)|(louder|quieter|softer)"
    )
    private val VOLUME_SET = Regex(
        "(?:set |turn )?(?:the )?(?:media )?volume (?:to |at )?(\\d{1,3})(?: ?%| percent)?"
    )
    private val VOLUME_MAX = Regex(
        "(?:max|maximum|full) volume|(?:set |turn )?(?:the )?volume (?:to |all the way )?" +
            "(?:max|maximum|full|up all the way)"
    )

    private val TIMER_FOR = Regex("(?:set |start |make )?(?:me )?(?:a |an )?timer (?:for )?(.+)")
    private val TIMER_AFTER = Regex("(?:set |start |make )?(?:me )?(?:a |an )?(.+?) timer")
    private val ALARM = Regex(
        "(?:set (?:an |a |my )?alarm|wake me(?: up)?|alarm)(?: for| at)? (.+)"
    )
    private val RELATIVE = Regex("in (.+?)(?: from now)?|(.+?) from now")
    private val DOTTED_TIME = Regex("\\b(\\d{1,2})\\.(\\d{2})\\b")
    private val DOTTED_HALF = Regex("(?<=[\\d ])([ap])\\.m\\.?")
    private val MORNING = Regex("\\b(?:in the morning|this morning|tomorrow morning)\\b")
    private val EVENING = Regex(
        "\\b(?:in the afternoon|this afternoon|in the evening|this evening|at night|tonight)\\b"
    )

    private val DURATION_PART = Regex(
        "\\b(\\d+(?:\\.\\d+)?|an?) (and a half )?(hours?|hrs?|minutes?|mins?|seconds?|secs?)\\b" +
            "( and a half)?"
    )
    private val HALF_PAST = Regex("half past (\\d{1,2})")
    private val QUARTER_PAST = Regex("(?:a )?quarter past (\\d{1,2})")
    private val QUARTER_TO = Regex("(?:a )?quarter to (\\d{1,2})")
    private val CLOCK = Regex("\\b(\\d{1,2})(?:[: ](\\d{2}))?\\s*(am|pm)?\\b")
}
