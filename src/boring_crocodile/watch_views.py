"""Three small presentations of the same bounded, authenticated watch events."""
from __future__ import annotations

import codecs
from collections import OrderedDict
from datetime import datetime
import json
import re
import unicodedata

_SGR = re.compile(r"\x1b\[(?:(?:0|1|22|39|3[0-7]|9[0-7])(?:;(?:0|1|22|39|3[0-7]|9[0-7]))*)?m\Z")
_COLOR = {"queued": "36", "running": "36", "started": "36", "stdout": "32",
          "stderr": "33", "failed": "31", "cancelled": "33", "stopping": "33",
          "completed": "32", "exit error": "31", "cleanup failed": "31", "gap": "33", "unknown": "33", "disconnected": "33", "stopped": "33", "not_started": "33", "succeeded": "32", "refused": "33"}


def safe_text(value: str) -> str:
    """Leave printable text alone; display terminal controls and bidi marks literally."""
    result = []
    for char in value:
        if char == "\n":
            result.append(char)
        elif char == "\t" or unicodedata.category(char).startswith("C"):
            result.append(json.dumps(char, ensure_ascii=True)[1:-1])
        else:
            result.append(char)
    return "".join(result)


class OutputDecoder:
    """Decode split UTF-8/ANSI chunks; pass only basic SGR colors to a TTY."""

    def __init__(self, color: bool):
        self.decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self.color = color
        self.escape = ""
        self.active_color = ""
        self.bold = False
        self.foreground = None

    def _style(self, sequence: str) -> None:
        for code in map(int, (sequence[2:-1] or "0").split(";")):
            if code == 0:
                self.bold, self.foreground = False, None
            elif code == 1:
                self.bold = True
            elif code == 22:
                self.bold = False
            elif code == 39:
                self.foreground = None
            else:
                self.foreground = code
        codes = ([1] if self.bold else []) + ([self.foreground] if self.foreground else [])
        self.active_color = "\x1b[" + ";".join(map(str, codes)) + "m" if codes else ""

    def feed(self, data: bytes) -> list[tuple[str, bool]]:
        rendered = []
        line = self.active_color if self.color else ""
        visible = False
        for char in self.decoder.decode(data):
            if self.escape:
                self.escape += char
                if char.isalpha() or len(self.escape) >= 32:
                    value = self.escape
                    self.escape = ""
                    if _SGR.fullmatch(value):
                        if self.color:
                            line += value
                            self._style(value)
                    elif re.fullmatch(r"\x1b\[[0-9;]*m", value):
                        # Drop unsupported color styles rather than replaying controls.
                        pass
                    else:
                        line += safe_text(value)
                continue
            if char == "\x1b":
                self.escape = char
            elif char == "\n":
                rendered.append((line + ("\x1b[0m" if self.color and self.active_color else ""), False))
                line = self.active_color if self.color else ""
                visible = False
            else:
                line += safe_text(char)
                visible = True
        if visible:
            rendered.append((line + ("\x1b[0m" if self.color and self.active_color else ""), True))
        return rendered


class WatchView:
    """Shared event mapping; subclasses own only line arrangement."""

    def __init__(self, color: bool = False) -> None:
        self.color = color
        self.streams = OrderedDict()
        self.drops = OrderedDict()

    def line(self, identity: str, kind: str, body: str) -> str:
        raise NotImplementedError

    def _identity(self, event: dict) -> str:
        if event.get("job_id"):
            return "job " + safe_text(str(event["job_id"]))[:8]
        return safe_text(str(event.get("task", "?"))).rsplit("/", 1)[-1]

    @staticmethod
    def _refusal(refusal: object) -> str:
        if not isinstance(refusal, dict):
            return ""
        text = safe_text(str(refusal.get("code", "?")))[:32]
        if refusal.get("message"):
            text += ": " + safe_text(str(refusal["message"]))[:200]
        return text

    def _line(self, identity: str, kind: str, body: str) -> str:
        return self.line(identity, kind, body)

    def styled(self, kind: str, width: int = 0, align: str = "<") -> str:
        label = f"{kind:{align}{width}}" if width else kind
        return f"\x1b[{_COLOR[kind]}m{label}\x1b[0m" if self.color and kind in _COLOR else label

    def _block(self, identity: str, kind: str, value: str) -> list[str]:
        return [self._line(identity, kind, part) for part in safe_text(value).split("\n")]

    def render(self, event: dict, output_bytes: bytes | None = None) -> list[str]:
        identity = self._identity(event)
        action = event.get("event")
        rows = []
        sender = event.get("sender")
        dropped = event.get("dropped", 0)
        if event.get("observation_gap") or (isinstance(dropped, int) and dropped > self.drops.get(sender, 0)):
            rows.append(self._line(identity, "gap", "observation may be incomplete"))
        if isinstance(dropped, int):
            self.drops[sender] = dropped
            self.drops.move_to_end(sender)
            if len(self.drops) > 128:
                self.drops.popitem(last=False)
        if action == "output":
            stream = str(event.get("stream", "output"))
            key = (event.get("sender"), stream)
            decoder = self.streams.get(key)
            if decoder is None:
                decoder = OutputDecoder(self.color)
                self.streams[key] = decoder
                if len(self.streams) > 256:
                    self.streams.popitem(last=False)
            self.streams.move_to_end(key)
            for part, partial in decoder.feed(output_bytes or b""):
                rows.append(self._line(identity, stream, part + (" [chunk]" if partial else "")))
            return rows
        if action in ("file_state", "batch_state", "batch_step"):
            state = safe_text(str(event.get("state", "unknown")))
            if action == "batch_state":
                message = "batch=" + safe_text(str(event.get("batch_id", "?")))[:16]
                if state == "started":
                    message += " count=" + safe_text(str(event.get("count", "?")))
                else:
                    message += " " + safe_text(str(event.get("attempted", 0))) + "/" + safe_text(str(event.get("count", "?")))
                if event.get("reason"):
                    message += " reason=" + safe_text(str(event["reason"]))[:64]
            elif action == "batch_step":
                message = (safe_text(str(event.get("index", "?"))) + "/" + safe_text(str(event.get("count", "?"))) +
                           " " + safe_text(str(event.get("method", "?")))[:32] +
                           " id=" + safe_text(str(event.get("step_id", "?")))[:32])
                if event.get("duration_ms") is not None:
                    message += " " + safe_text(str(event["duration_ms"])) + "ms"
            else:
                message = (safe_text(str(event.get("method", "?")))[:32] +
                           " " + safe_text(str(event.get("root", "?")))[:63] + "/" +
                           safe_text(str(event.get("path", "?")))[:256])
                if event.get("duration_ms") is not None:
                    message += " " + safe_text(str(event["duration_ms"])) + "ms"
                if event.get("batch_id"):
                    message += " batch=" + safe_text(str(event["batch_id"]))[:16]
            rows.append(self._line(identity, state, message))
            return rows
        if action == "job_state":
            state = safe_text(str(event.get("state", "unknown")))
            message = ""
            if state == "completed" and "exit_code" in event:
                # Only command jobs have an exit code; Pirate and batch jobs do not.
                message = "exit=" + safe_text(str(event.get("exit_code")))
            elif state == "failed":
                message = safe_text(str(event.get("error", "unknown error")))
            elif state == "refused":
                message = self._refusal(event.get("refusal"))
            label = "exit error" if state == "completed" and event.get("exit_code") not in (0, None) else state
            if label == "exit error":
                message = "completed " + message
            if state == "queued":
                message = "id=" + safe_text(str(event.get("job_id", "?")))
                if event.get("kind") == "pirate":
                    message += (" pirate=" + safe_text(str(event.get("pirate", "?")))[:64] +
                                "/" + safe_text(str(event.get("flavour", "?")))[:64])
                elif event.get("kind") == "batch":
                    message += " steps=" + safe_text(str(event.get("step_count", "?")))
            rows.append(self._line(identity, label, message))
            if state == "queued" and "command" in event:
                rows.extend(self._block(identity, "command", str(event["command"])))
                if event.get("command_truncated"):
                    rows.append(self._line(identity, "command", "[preview truncated]"))
            return rows
        if action == "call_state":
            # Foreground calls owned by another process: services and Pirate describe.
            state = safe_text(str(event.get("state", "unknown")))
            message = safe_text(str(event.get("method", "?")))[:32]
            for key in ("tool", "flavour"):
                if event.get(key):
                    message += " " + safe_text(str(event[key]))[:64]
            if event.get("duration_ms") is not None:
                message += " " + safe_text(str(event["duration_ms"])) + "ms"
            if event.get("code"):
                message += " code=" + safe_text(str(event["code"]))[:32]
            rows.append(self._line(identity, state, message))
            return rows
        if action == "dispatch":
            rows.append(self._line(identity, "dispatch", "to task runner"))
            if not event.get("job_id") and "command" in event:
                rows.extend(self._block(identity, "command", str(event["command"])))
            return rows
        if action == "accepted":
            rows.append(self._line(identity, "accepted", ""))
        elif action == "started":
            rows.append(self._line(identity, "started", "pid=" + safe_text(str(event.get("pid")))))
        elif action == "process_result":
            status = "exit=" + safe_text(str(event.get("exit_code")))
            if event.get("timed_out"):
                status += " timed out"
            if event.get("output_incomplete"):
                status += " output incomplete"
            kind = "exit error" if event.get("exit_code") not in (0, None) or event.get("timed_out") else "result"
            rows.append(self._line(identity, kind, status))
        elif action == "output_truncated":
            rows.append(self._line(identity, "gap", safe_text(str(event.get("stream"))) + " truncated"))
        elif action == "cleanup":
            good = event.get("complete") is True
            rows.append(self._line(identity, "cleanup" if good else "cleanup failed",
                                   "confirmed" if good else "not confirmed; outcome unknown"))
        else:
            rows.append(self._line(identity, "event", safe_text(str(action))))
        return rows


class TimelineView(WatchView):
    def line(self, identity: str, kind: str, body: str) -> str:
        return f"{datetime.now().astimezone():%H:%M:%S}  {identity}  {self.styled(kind, 12)} {body}".rstrip()


class CompactView(WatchView):
    def line(self, identity: str, kind: str, body: str) -> str:
        return f"[{identity}] {self.styled(kind, 9, '>')} | {body}".rstrip()


class CardsView(WatchView):
    def line(self, identity: str, kind: str, body: str) -> str:
        return f"{identity}  {self.styled(kind)}: {body}".rstrip() if kind in ("queued", "dispatch") else f"  [{identity}] {self.styled(kind)}: {body}".rstrip()


VIEWS = {"timeline": TimelineView, "compact": CompactView, "cards": CardsView}
