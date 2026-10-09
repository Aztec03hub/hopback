"""Display helpers shared by the picker and the adapters."""
import re
import time

# Untrusted text (a session's title, reply or directory, a store's error) must
# not reach a terminal as an escape sequence. C0 and C1 controls, DEL, the line
# and paragraph separators and the bidi controls (a U+202E title can display as
# something else than it is) become a space. ZWJ and variation selectors stay:
# emoji need them. Multi-line text keeps its newlines and tabs.
_BAD = ("\x00-\x08\x0b-\x1f\x7f-\x9f  ‎‏؜"
        "‪-‮⁦-⁩")
CONTROL = re.compile(f"[{_BAD}]")
CONTROL_LINE = re.compile(f"[\x09\x0a{_BAD}]")
HARD_CONTROL = re.compile("[\x00-\x1f\x7f-\x9f]")   # what no real path contains


def safe_text(text, line=False):
    """`text` with control characters replaced by a space (a line of the list
    also loses its newlines and tabs)."""
    return (CONTROL_LINE if line else CONTROL).sub(" ", text)


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
