import argparse
import contextlib
import json
import os
import re
import sys
import tempfile
import time


def format_time(seconds):
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    return f"{h:02d}:{m:02d}:{s:05.2f}"


def sanitize_coordination_id(value):
    safe = re.sub(r"[^a-zA-Z0-9_.-]+", "_", value).strip("._")
    return safe or "video-to-minutes-transcribe"


def get_total_memory_mb():
    try:
        page_size = os.sysconf("SC_PAGE_SIZE")
        phys_pages = os.sysconf("SC_PHYS_PAGES")
    except (AttributeError, OSError, ValueError):
        return 0
    return int((page_size * phys_pages) / (1024 * 1024))


def estimate_worker_memory_mb(model_name, compute_type, batch_size):
    normalized_model = model_name.lower()
    normalized_type = compute_type.lower()

    model_memory_mb = 3200
    if "tiny" in normalized_model:
        model_memory_mb = 900
    elif "base" in normalized_model:
        model_memory_mb = 1400
    elif "small" in normalized_model:
        model_memory_mb = 2200
    elif "medium" in normalized_model:
        model_memory_mb = 3800
    elif "large" in normalized_model:
        model_memory_mb = 7000
    elif "turbo" in normalized_model:
        model_memory_mb = 4500

    if "float32" in normalized_type:
        compute_multiplier = 1.7
    elif "float16" in normalized_type or "bfloat16" in normalized_type:
        compute_multiplier = 1.0
    elif "int8" in normalized_type:
        compute_multiplier = 0.7
    else:
        compute_multiplier = 1.0

    batch_overhead_mb = max(0, batch_size) * 350
    runtime_overhead_mb = 512
    return int(model_memory_mb * compute_multiplier + batch_overhead_mb + runtime_overhead_mb)


def choose_auto_max_concurrent(model_name, compute_type, batch_size, headroom_ratio):
    total_memory_mb = get_total_memory_mb()
    if total_memory_mb <= 0:
        return 1, "total RAM is unavailable"

    headroom_ratio = min(max(headroom_ratio, 0.0), 0.9)
    usable_memory_mb = int(total_memory_mb * (1.0 - headroom_ratio))
    estimated_worker_mb = estimate_worker_memory_mb(model_name, compute_type, batch_size)
    memory_bound = max(1, usable_memory_mb // max(1, estimated_worker_mb))

    cpu_count = os.cpu_count() or 1
    cpu_bound = max(1, cpu_count // 2)

    auto_limit = max(1, min(memory_bound, cpu_bound))
    reason = (
        f"auto={auto_limit} (RAM {total_memory_mb}MB, "
        f"usable {usable_memory_mb}MB, worker~{estimated_worker_mb}MB, CPUs {cpu_count})"
    )
    return auto_limit, reason


def is_process_alive(pid):
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def read_pid_state(path):
    try:
        with open(path, "r", encoding="utf-8") as file:
            raw = json.load(file)
    except (FileNotFoundError, json.JSONDecodeError):
        return []
    if not isinstance(raw, list):
        return []
    pids = []
    for value in raw:
        if isinstance(value, int):
            pids.append(value)
    return pids


def write_pid_state(path, pids):
    temp_path = f"{path}.{os.getpid()}.tmp"
    with open(temp_path, "w", encoding="utf-8") as file:
        json.dump(pids, file)
    os.replace(temp_path, path)


@contextlib.contextmanager
def acquire_transcription_slot(
    coordination_id, max_concurrent, poll_seconds, timeout_seconds
):
    if max_concurrent <= 0:
        yield
        return

    try:
        import fcntl
    except ImportError:
        print(
            "Warning: file locking is unavailable; skipping concurrency guard.",
            flush=True,
        )
        yield
        return

    slot_id = sanitize_coordination_id(coordination_id)
    lock_path = os.path.join(tempfile.gettempdir(), f"{slot_id}.lock")
    state_path = os.path.join(tempfile.gettempdir(), f"{slot_id}.pids.json")
    pid = os.getpid()
    deadline = 0.0
    if timeout_seconds > 0:
        deadline = time.monotonic() + timeout_seconds

    while True:
        active_count = 0
        with open(lock_path, "a+", encoding="utf-8") as lock_file:
            fcntl.flock(lock_file, fcntl.LOCK_EX)
            active = [p for p in read_pid_state(state_path) if p != pid and is_process_alive(p)]
            active_count = len(active)
            if active_count < max_concurrent:
                active.append(pid)
                write_pid_state(state_path, active)
                fcntl.flock(lock_file, fcntl.LOCK_UN)
                break
            fcntl.flock(lock_file, fcntl.LOCK_UN)

        print(
            f"Waiting for transcription slot ({active_count}/{max_concurrent} active)...",
            flush=True,
        )
        if deadline and time.monotonic() >= deadline:
            raise TimeoutError(
                f"Timed out waiting for a transcription slot (limit={max_concurrent})."
            )
        time.sleep(max(0.1, poll_seconds))

    try:
        yield
    finally:
        with open(lock_path, "a+", encoding="utf-8") as lock_file:
            fcntl.flock(lock_file, fcntl.LOCK_EX)
            active = [p for p in read_pid_state(state_path) if p != pid and is_process_alive(p)]
            write_pid_state(state_path, active)
            fcntl.flock(lock_file, fcntl.LOCK_UN)


def main():
    parser = argparse.ArgumentParser(
        description="Transcribe audio using faster-whisper."
    )
    parser.add_argument("audio_path", help="Path to the audio file to transcribe.")
    parser.add_argument(
        "--language", default="ja", help="Language code (default: ja)"
    )
    parser.add_argument(
        "--model", default="large-v3", help="Whisper model name (default: large-v3)"
    )
    parser.add_argument(
        "--output",
        default="meeting_audio.txt",
        help="Output text file path (default: meeting_audio.txt)",
    )
    parser.add_argument(
        "--device", default="cpu", help="Device to use (default: cpu)"
    )
    parser.add_argument(
        "--compute-type",
        default="int8",
        help="Compute type for quantization (default: int8)",
    )
    parser.add_argument(
        "--beam-size", type=int, default=5, help="Beam size (default: 5)"
    )
    parser.add_argument(
        "--vad-filter",
        action="store_true",
        help="Enable voice activity detection to suppress non-speech hallucinations",
    )
    parser.add_argument(
        "--condition-on-previous-text",
        action="store_true",
        help="Condition each segment on the previously decoded text (default: disabled)",
    )
    parser.add_argument(
        "--initial-prompt",
        default="",
        help="Optional initial prompt with domain terms or proper nouns",
    )
    parser.add_argument(
        "--no-timestamps",
        action="store_true",
        help="Save plain text without timestamps to the output file",
    )
    parser.add_argument(
        "--cpu-threads",
        type=int,
        default=0,
        help="Number of CPU threads for CTranslate2 (default: auto)",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=0,
        help="Enable batched inference when > 0 (default: disabled)",
    )
    parser.add_argument(
        "--max-concurrent",
        type=int,
        default=0,
        help=(
            "Maximum parallel transcription processes sharing the same coordination id "
            "(default: auto)"
        ),
    )
    parser.add_argument(
        "--coordination-id",
        default="video-to-minutes-transcribe",
        help="Coordination id used for cross-process concurrency control",
    )
    parser.add_argument(
        "--memory-headroom-ratio",
        type=float,
        default=0.25,
        help="Fraction of RAM to keep free when auto-sizing max concurrency (default: 0.25)",
    )
    parser.add_argument(
        "--slot-wait-seconds",
        type=float,
        default=2.0,
        help="Wait interval while queued for a transcription slot (default: 2.0)",
    )
    parser.add_argument(
        "--slot-timeout-seconds",
        type=float,
        default=0.0,
        help="Timeout while waiting for a transcription slot in seconds (default: 0 = no timeout)",
    )
    parser.add_argument(
        "--disable-concurrency-guard",
        action="store_true",
        help="Disable cross-process concurrency guard",
    )
    args = parser.parse_args()

    if args.max_concurrent < 0:
        print("Error: --max-concurrent must be >= 0", file=sys.stderr)
        sys.exit(1)
    if args.batch_size < 0:
        print("Error: --batch-size must be >= 0", file=sys.stderr)
        sys.exit(1)
    if not (0.0 <= args.memory_headroom_ratio < 1.0):
        print("Error: --memory-headroom-ratio must be in [0.0, 1.0)", file=sys.stderr)
        sys.exit(1)
    if args.slot_wait_seconds <= 0:
        print("Error: --slot-wait-seconds must be > 0", file=sys.stderr)
        sys.exit(1)
    if args.slot_timeout_seconds < 0:
        print("Error: --slot-timeout-seconds must be >= 0", file=sys.stderr)
        sys.exit(1)

    try:
        from faster_whisper import WhisperModel
    except ImportError:
        print(
            "Error: faster-whisper is not installed.\n"
            "Install it with: pip install faster-whisper",
            file=sys.stderr,
        )
        sys.exit(1)

    max_concurrent = 0
    if args.disable_concurrency_guard:
        print("Concurrency guard: disabled", flush=True)
    elif args.max_concurrent > 0:
        max_concurrent = args.max_concurrent
        print(f"Concurrency guard: max_concurrent={max_concurrent} (manual)", flush=True)
    else:
        max_concurrent, reason = choose_auto_max_concurrent(
            args.model, args.compute_type, args.batch_size, args.memory_headroom_ratio
        )
        print(f"Concurrency guard: {reason}", flush=True)

    try:
        with acquire_transcription_slot(
            coordination_id=args.coordination_id,
            max_concurrent=max_concurrent,
            poll_seconds=args.slot_wait_seconds,
            timeout_seconds=args.slot_timeout_seconds,
        ):
            print(
                f"Loading model '{args.model}' on {args.device} ({args.compute_type})...",
                flush=True,
            )
            model_kwargs = {"device": args.device, "compute_type": args.compute_type}
            if args.cpu_threads > 0:
                model_kwargs["cpu_threads"] = args.cpu_threads
            model = WhisperModel(args.model, **model_kwargs)

            print(
                f"Transcribing '{args.audio_path}' (language={args.language}, beam_size={args.beam_size})...",
                flush=True,
            )
            transcribe_kwargs = {
                "language": args.language,
                "beam_size": args.beam_size,
                "vad_filter": args.vad_filter,
                "condition_on_previous_text": args.condition_on_previous_text,
            }
            if args.initial_prompt:
                transcribe_kwargs["initial_prompt"] = args.initial_prompt
            if args.batch_size > 0:
                try:
                    from faster_whisper import BatchedInferencePipeline
                except ImportError:
                    print(
                        "Warning: BatchedInferencePipeline is unavailable, falling back to non-batched mode.",
                        flush=True,
                    )
                    segments, info = model.transcribe(args.audio_path, **transcribe_kwargs)
                else:
                    print(
                        f"Using batched inference (batch_size={args.batch_size})...",
                        flush=True,
                    )
                    batched_model = BatchedInferencePipeline(model=model)
                    segments, info = batched_model.transcribe(
                        args.audio_path, batch_size=args.batch_size, **transcribe_kwargs
                    )
            else:
                segments, info = model.transcribe(args.audio_path, **transcribe_kwargs)

            print(
                f"Detected language: {info.language} (probability {info.language_probability:.2f})",
                flush=True,
            )

            with open(args.output, "w", encoding="utf-8") as output_file:
                for segment in segments:
                    line = segment.text.strip()
                    timestamped_line = (
                        f"[{format_time(segment.start)} -> {format_time(segment.end)}] {line}"
                    )
                    print(
                        timestamped_line,
                        flush=True,
                    )
                    if args.no_timestamps:
                        output_file.write(line + "\n")
                    else:
                        output_file.write(timestamped_line + "\n")
    except TimeoutError as error:
        print(f"Error: {error}", file=sys.stderr)
        sys.exit(1)

    print(f"\nTranscription saved to '{args.output}'", flush=True)


if __name__ == "__main__":
    main()

