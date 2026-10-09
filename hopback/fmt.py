"""Display helpers shared by the picker and the adapters."""
import re
import shlex
import time

# Untrusted text (a session's title, reply or directory, a store's error) must
# not reach a terminal as an escape sequence, nor show as something it is not.
# Each of these becomes a space (written as escapes: the characters are invisible):
#   C0 controls, DEL, C1 controls          \x00-\x08 \x0b-\x1f \x7f-\x9f
#   soft hyphen, Arabic letter mark        \xad \u061c
#   Mongolian vowel separator              \u180e
#   zero-width space, LRM, RLM             \u200b \u200e \u200f
#   line and paragraph separators          \u2028 \u2029
#   bidi embeddings and overrides          \u202a-\u202e
#   word joiner, invisible operators,
#   bidi isolates, deprecated controls     \u2060-\u206f
#   byte order mark                        \ufeff
#   interlinear annotation                 \ufff9-\ufffb
#   tag characters                         \U000e0001-\U000e007f
#   surrogates (a lone one cannot be encoded, so print() would raise)
#                                          \ud800-\udfff
# ZWJ (\u200d), ZWNJ (\u200c) and variation selectors stay: emoji and several
# scripts need them. Multi-line text keeps its newlines and tabs.
_BAD = ("\x00-\x08\x0b-\x1f\x7f-\x9f\xad\u061c\u180e\u200b\u200e\u200f\u2028\u2029"
        "\u202a-\u202e\u2060-\u206f\ufeff\ufff9-\ufffb\U000e0001-\U000e007f\ud800-\udfff")
CONTROL = re.compile(f"[{_BAD}]")
CONTROL_LINE = re.compile(f"[\x09\x0a{_BAD}]")   # every C0 control, tab and newline too
_SHELL_ESCAPE = re.compile(f"[\\\\'\x09\x0a{_BAD}]")


def safe_text(text, line=False):
    """`text` with control characters replaced by a space (a line of the list
    also loses its newlines and tabs)."""
    return (CONTROL_LINE if line else CONTROL).sub(" ", text)


def shq(text):
    """`text` as one shell word that bash and zsh read back as exactly `text`,
    with no control, invisible or bidi character in the printed form: those
    become $'...' byte escapes (\\xNN of the UTF-8 bytes, so the path is unchanged)."""
    if not CONTROL_LINE.search(text):
        return shlex.quote(text)

    def esc(m):
        c = m.group()
        if c in "\\'":
            return "\\" + c
        try:
            raw = c.encode("utf-8", "surrogateescape")   # a file name's undecodable byte
        except UnicodeEncodeError:
            raw = c.encode("utf-8", "surrogatepass")
        return "".join(f"\\x{b:02x}" for b in raw)
    return "$'" + _SHELL_ESCAPE.sub(esc, text) + "'"


def size_str(nbytes):
    """KB up to a megabyte, then MB. Sizes here span 20 KB to 570 MB, so one
    fixed unit makes either the small or the large end unreadable."""
    kb = nbytes / 1024
    if kb < 1000:
        return f"{kb:.0f} KB"
    mb = kb / 1024
    return f"{mb:.1f} MB" if mb < 100 else f"{mb:.0f} MB"


def when(mtime, now):
    """Relative for the last week, absolute after that.

    Inside a week the useful question is "how long ago, and at what time of
    day", which a bare date cannot answer. Past a week the day itself is the
    identifier and the clock time is noise.
    """
    age = now - mtime
    clock = time.strftime("%-I:%M %p", time.localtime(mtime))
    if age < 3600:
        return f"{int(age // 60)}m ago"
    if age < 86400:
        return f"{int(age // 3600)}h ago, {clock}"
    days = int(age // 86400)
    if days == 1:
        return f"a day ago, {clock}"
    if days < 7:
        return f"{days} days ago, {clock}"
    if days < 14:
        return f"a week ago, {clock}"
    return time.strftime("%b %-d", time.localtime(mtime))
