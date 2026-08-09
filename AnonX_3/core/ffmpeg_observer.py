"""Transparent FFmpeg stdout relay for cold direct-playback telemetry.

This file is executed as a standalone helper process.  It deliberately does
not import :mod:`AnonX_3`, because importing the application package would
bootstrap services in the media subprocess.  The helper writes only sanitized
timing events to a private per-start JSONL file and relays decoded bytes to
ntgcalls unchanged.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path


def _timestamp() -> dict[str, int | str]:
    return {
        "wall": datetime.now(timezone.utc).isoformat(timespec="microseconds"),
        "wall_time_ns": time.time_ns(),
        "monotonic_ns": time.perf_counter_ns(),
    }


class EventWriter:
    def __init__(self, path: str, chat_id: int, media_id: str, kind: str) -> None:
        self._path = Path(path)
        self._chat_id = int(chat_id)
        self._media_id = str(media_id)
        self._kind = str(kind)
        self._lock = threading.Lock()
        self._stream = None

    def emit(self, event: str, **fields) -> None:
        payload = {
            "event": str(event),
            "chat_id": self._chat_id,
            "media_id": self._media_id,
            "kind": self._kind,
            **_timestamp(),
            **fields,
        }
        with self._lock:
            if self._stream is None:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                self._stream = self._path.open(
                    "a", encoding="utf-8", buffering=1
                )
            self._stream.write(
                json.dumps(payload, ensure_ascii=True, separators=(",", ":"))
                + "\n"
            )
            self._stream.flush()

    def close(self) -> None:
        with self._lock:
            if self._stream is not None:
                self._stream.close()
                self._stream = None


def _parse_args(argv: list[str]) -> tuple[argparse.Namespace, list[str]]:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--event-file", required=True)
    parser.add_argument("--chat-id", required=True, type=int)
    parser.add_argument("--media-id", required=True)
    parser.add_argument("--kind", default="audio")
    parser.add_argument("--frame-bytes", type=int, default=3840)
    parser.add_argument("--keep-event-file", action="store_true")
    if "--" not in argv:
        parser.error("missing command separator")
    split_at = argv.index("--")
    options = parser.parse_args(argv[:split_at])
    command = argv[split_at + 1 :]
    if not command:
        parser.error("missing ffmpeg command")
    return options, command


def _input_url(command: list[str]) -> str:
    try:
        return command[command.index("-i") + 1]
    except (ValueError, IndexError):
        return ""


def _safe_detail(line: str, input_url: str) -> str:
    clean = str(line or "").replace("\r", " ").replace("\n", " ").strip()
    if input_url:
        clean = clean.replace(input_url, "[direct_url]")
    return clean[:180]


def main(argv: list[str] | None = None) -> int:
    options, command = _parse_args(list(sys.argv[1:] if argv is None else argv))
    writer = EventWriter(
        options.event_file,
        options.chat_id,
        options.media_id,
        options.kind,
    )
    input_url = _input_url(command)
    process: subprocess.Popen | None = None
    raw_seen = threading.Event()
    decoded_emitted = False
    last_error = {"line": ""}

    def _terminate_child(*_args) -> None:
        if process is not None and process.poll() is None:
            try:
                process.terminate()
            except Exception:
                pass

    try:
        for sig_name in ("SIGTERM", "SIGINT"):
            sig = getattr(signal, sig_name, None)
            if sig is not None:
                signal.signal(sig, _terminate_child)

        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
        writer.emit("ffmpeg_spawned", pid=int(process.pid))

        def _read_stderr() -> None:
            assert process is not None and process.stderr is not None
            while True:
                raw_line = process.stderr.readline()
                if not raw_line:
                    break
                line = raw_line.decode("utf-8", "replace")
                safe = _safe_detail(line, input_url)
                if safe:
                    last_error["line"] = safe
                # FFmpeg's HTTP response header is the earliest observable
                # read from the remote connection.  The format-probe/Input
                # markers cover protocols/builds that omit HTTP debug lines.
                if not raw_seen.is_set() and (
                    "header='HTTP/" in line
                    or "probed with size=" in line
                    or line.lstrip().startswith("Input #0,")
                ):
                    raw_seen.set()
                    writer.emit("raw_url_first_bytes", evidence="ffmpeg_input")

        stderr_thread = threading.Thread(
            target=_read_stderr,
            name="ffmpeg-observer-stderr",
            daemon=True,
        )
        stderr_thread.start()

        assert process.stdout is not None
        output = sys.stdout.buffer
        first_decoded = True
        frame_bytes = max(1, int(options.frame_bytes or 1))
        while True:
            chunk = process.stdout.read(frame_bytes)
            if not chunk:
                break
            now = _timestamp()
            if not raw_seen.is_set():
                raw_seen.set()
                writer.emit(
                    "raw_url_first_bytes",
                    evidence="implied_by_decoded_output",
                    **now,
                )
            if first_decoded:
                first_decoded = False
                decoded_emitted = True
                writer.emit(
                    "first_decoded_audio_frame",
                    bytes=len(chunk),
                    **now,
                )
            output.write(chunk)
            output.flush()

        return_code = process.wait()
        stderr_thread.join(timeout=0.25)
        writer.emit(
            "ffmpeg_exited",
            return_code=int(return_code),
            detail=last_error["line"] if return_code else "",
        )
        return int(return_code)
    except BrokenPipeError:
        _terminate_child()
        return 0
    except BaseException as ex:
        writer.emit("ffmpeg_observer_failed", error=type(ex).__name__)
        _terminate_child()
        return 1
    finally:
        if process is not None:
            for pipe in (process.stdout, process.stderr):
                if pipe is not None:
                    try:
                        pipe.close()
                    except Exception:
                        pass
        writer.close()
        # Successful long-lived streams are normally unlinked by the parent
        # as soon as startup observation finishes.  Windows cannot unlink an
        # open file, so remove it here when the media process eventually exits.
        # Keep no-frame failures long enough for the parent to consume them.
        if decoded_emitted and not options.keep_event_file:
            try:
                Path(options.event_file).unlink(missing_ok=True)
            except OSError:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
