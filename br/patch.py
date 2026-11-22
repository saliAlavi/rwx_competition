"""Parse and apply `git diff` style unified diffs (stdlib only)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


@dataclass
class Hunk:
    old_start: int
    old_len: int
    new_start: int
    new_len: int
    lines: list[str] = field(default_factory=list)  # each starts with ' ', '-', '+' or '\\'


@dataclass
class FilePatch:
    old_path: str | None  # None when the file is added
    new_path: str | None  # None when the file is deleted
    hunks: list[Hunk] = field(default_factory=list)
    binary: bool = False
    mode_change: bool = False

    @property
    def path(self) -> str:
        return self.new_path or self.old_path  # type: ignore[return-value]

    def old_changed_lines(self) -> set[int]:
        """Old-file line numbers that were removed or replaced."""
        out: set[int] = set()
        for hunk in self.hunks:
            line_no = hunk.old_start
            for line in hunk.lines:
                tag = line[:1]
                if tag == " ":
                    line_no += 1
                elif tag == "-":
                    out.add(line_no)
                    line_no += 1
        return out

    def old_insertion_points(self) -> set[int]:
        """Old-file line numbers that new lines were inserted directly *after* (0 = top)."""
        out: set[int] = set()
        for hunk in self.hunks:
            line_no = hunk.old_start - 1 if hunk.old_len else hunk.old_start
            prev_tag = None
            for line in hunk.lines:
                tag = line[:1]
                if tag in (" ", "-"):
                    line_no += 1
                elif tag == "+" and prev_tag != "-":
                    out.add(line_no)
                prev_tag = tag if tag != "\\" else prev_tag
        return out


def _strip_prefix(path: str) -> str | None:
    path = path.strip()
    if path == "/dev/null":
        return None
    if path.startswith('"') and path.endswith('"'):
        path = path[1:-1].encode("latin-1", "backslashreplace").decode("unicode_escape")
        path = path.encode("latin-1", "ignore").decode("utf-8", "replace")
    if path[:2] in ("a/", "b/"):
        path = path[2:]
    return path


def parse(text: str) -> list[FilePatch]:
    files: list[FilePatch] = []
    current: FilePatch | None = None
    hunk: Hunk | None = None
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("diff --git "):
            m = re.match(r'^diff --git (?:"?a/)(.+?)"? (?:"?b/)(.+?)"?$', line)
            old = new = None
            if m:
                old, new = m.group(1), m.group(2)
            current = FilePatch(old_path=old, new_path=new)
            files.append(current)
            hunk = None
        elif current is not None and hunk is None and line.startswith("--- "):
            current.old_path = _strip_prefix(line[4:].split("\t")[0])
        elif current is not None and hunk is None and line.startswith("+++ "):
            current.new_path = _strip_prefix(line[4:].split("\t")[0])
        elif current is not None and line.startswith("new file mode"):
            current.old_path = None
        elif current is not None and line.startswith("deleted file mode"):
            current.new_path = None
        elif current is not None and line.startswith("rename from "):
            current.old_path = line[len("rename from "):]
        elif current is not None and line.startswith("rename to "):
            current.new_path = line[len("rename to "):]
        elif current is not None and (line.startswith("old mode") or line.startswith("new mode")):
            current.mode_change = True
        elif current is not None and (line.startswith("Binary files") or line.startswith("GIT binary patch")):
            current.binary = True
        elif current is not None and line.startswith("@@"):
            m = HUNK_RE.match(line)
            if not m:
                raise ValueError(f"bad hunk header: {line!r}")
            hunk = Hunk(
                int(m.group(1)),
                int(m.group(2)) if m.group(2) is not None else 1,
                int(m.group(3)),
                int(m.group(4)) if m.group(4) is not None else 1,
            )
            current.hunks.append(hunk)
            # consume exactly the hunk body
            old_left, new_left = hunk.old_len, hunk.new_len
            i += 1
            while i < len(lines) and (old_left > 0 or new_left > 0 or (lines[i].startswith("\\"))):
                body = lines[i]
                tag = body[:1]
                if tag == "\\":
                    hunk.lines.append(body)
                elif tag == "-":
                    hunk.lines.append(body)
                    old_left -= 1
                elif tag == "+":
                    hunk.lines.append(body)
                    new_left -= 1
                elif tag == " " or body == "":
                    hunk.lines.append(" " + body[1:] if body else " ")
                    old_left -= 1
                    new_left -= 1
                else:
                    break
                i += 1
            # trailing "\ No newline" after the counted lines
            while i < len(lines) and lines[i].startswith("\\"):
                hunk.lines.append(lines[i])
                i += 1
            hunk = None
            continue
        i += 1
    return files


def apply(old_text: str | None, fp: FilePatch) -> str | None:
    """Return the new file text (None if deleted)."""
    if fp.new_path is None:
        return None
    old_lines = (old_text or "").split("\n")
    # keepends emulation: track whether the old file ended with a newline
    old_has_nl = old_text is not None and old_text.endswith("\n")
    if old_has_nl:
        old_lines.pop()
    elif old_text == "":
        old_lines = []
    out: list[str] = []
    pos = 0  # index into old_lines
    new_has_nl = old_has_nl if fp.hunks else old_has_nl
    for hunk in fp.hunks:
        start = hunk.old_start - 1 if hunk.old_len else hunk.old_start
        if start < pos:
            raise ValueError("overlapping hunks")
        out.extend(old_lines[pos:start])
        pos = start
        last_tag = None
        for line in hunk.lines:
            tag, body = line[:1], line[1:]
            if tag == " ":
                out.append(old_lines[pos] if pos < len(old_lines) else body)
                pos += 1
            elif tag == "-":
                pos += 1
            elif tag == "+":
                out.append(body)
            elif tag == "\\":
                if last_tag in ("+", " "):
                    new_has_nl = False
                if last_tag == "-":
                    pass
                continue
            last_tag = tag
        # if the hunk reached the old end and its last line was context/added, newline state
        if pos >= len(old_lines) and last_tag in ("+", " ") and not any(
            l.startswith("\\") for l in hunk.lines[-1:]
        ):
            new_has_nl = True
    out.extend(old_lines[pos:])
    text = "\n".join(out)
    if out and new_has_nl:
        text += "\n"
    return text
