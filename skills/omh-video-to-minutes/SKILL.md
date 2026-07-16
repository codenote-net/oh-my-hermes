---
name: omh-video-to-minutes
description: "Convert meeting videos into structured Markdown minutes."
version: 1.0.0
author: Tadashi Shigeoka, Hermes Agent
license: MIT
platforms: [linux, macos]
allowed-tools: [terminal, read_file, write_file, search_files, vision_analyze]
prerequisites:
  commands: [claude, codex, ffmpeg, uv]
metadata:
  hermes:
    tags: [video, transcription, meetings, minutes, whisper]
    requires_toolsets: [terminal]
---

# Video to Minutes Skill

Convert a local meeting recording into grounded Markdown minutes using extracted audio,
timestamped speech transcription, and periodic video captures. This skill does not infer
decisions, owners, or due dates that are absent from the recording.

## Model Routing

- Run the main workflow in Codex CLI with the exact model ID `gpt-5.6-luna`. It owns input
  collection, file handling, media extraction, transcription orchestration, verification, and
  the final result returned to Hermes.
- Run only the transcript-to-minutes drafting step with `claude-haiku-4-5` through the
  Claude CLI. The main agent must review the draft against the source transcript before accepting
  it.
- Do not use `delegate_task` or a nested Hermes process to select the drafting model. Start a
  separate non-interactive Claude CLI process with an explicit model instead.

Use the dedicated Codex CLI model option; do not rewrite the user's global Codex configuration
and do not describe the model as the ambiguous phrase "gpt-5.6 in Luna mode":

```bash
codex exec --model gpt-5.6-luna \
  --config model_reasoning_effort='"low"' \
  --ephemeral --skip-git-repo-check \
  -C "<ABSOLUTE_OUTPUT_DIR>" \
  "Run the video-to-minutes workflow described in <ABSOLUTE_SKILL_MD_PATH>. Process \
<ABSOLUTE_VIDEO_PATH>, use language <LANGUAGE> and a <INTERVAL_SECONDS>-second capture interval. \
Use <ABSOLUTE_TRANSCRIBE_SCRIPT_PATH> for transcription. Do not draft the final minutes yet. \
Return the absolute transcript path and capture directory."
```

## When to Use

- The user asks to turn an MP4, MOV, MKV, or WebM recording into meeting notes or minutes.
- The user wants a transcript plus decisions, action items, highlights, or visual references.
- The source is a local video file; for a YouTube URL with captions, prefer a transcript-fetching skill.

## Prerequisites

Use the `terminal` tool to verify the runtime before processing:

```bash
command -v ffmpeg
command -v uv
command -v codex
command -v claude
uv run --with faster-whisper python3 -c "from faster_whisper import WhisperModel; print('faster-whisper: ok')"
```

If `claude`, `codex`, `ffmpeg`, or `uv` is missing, explain what will be installed and obtain
the user's approval before changing their environment. `uv` resolves `faster-whisper` in an
isolated environment; do not install it into Hermes Agent's own virtual environment or the
system Python.

```bash
uv run --with faster-whisper python3 -c "from faster_whisper import WhisperModel"
```

The helper uses `fcntl` for its optional cross-process concurrency guard, so this skill is
limited to Linux and macOS.

## How to Run

Use the `terminal` tool from the directory where output files should be created. Resolve
`SKILL_DIR` to the directory containing this `SKILL.md`; do not assume the current working
directory is the skill directory.

```bash
ffmpeg -i "/absolute/path/to/meeting.mp4" -vn -acodec pcm_s16le -ar 16000 -ac 1 meeting_audio.wav
mkdir -p captures
ffmpeg -i "/absolute/path/to/meeting.mp4" -vf fps=1/60 captures/capture_%03d.png
uv run --with faster-whisper python3 "SKILL_DIR/scripts/transcribe.py" meeting_audio.wav \
  --language ja --model large-v3 --vad-filter \
  --initial-prompt "project names, people names, domain terms" \
  --output meeting_audio.txt
```

Then use `read_file` for the transcript, `search_files` to enumerate captures, and
`vision_analyze` only for captures whose visual content can materially improve the minutes.
Save the result with `write_file`.

## Quick Reference

| Setting | Default | Guidance |
| --- | --- | --- |
| Capture interval | 60 seconds | Shorten for slide-heavy sessions; lengthen for talking heads. |
| Language | `ja` | Use the recording's BCP-47-style language code, such as `en`. |
| Model | `large-v3` | Use a smaller model only when speed or memory matters more than accuracy. |
| Device | `cpu` | Pass `--device cuda --compute-type float16` on a compatible GPU. |
| VAD | enabled by workflow | Keep `--vad-filter` for meetings to reduce silence hallucinations. |
| Timestamps | enabled | Pass `--no-timestamps` only when plain text is explicitly required. |
| Previous text | disabled | Add `--condition-on-previous-text` only for clean, continuous audio. |
| CPU threads | auto | Pass `--cpu-threads N` to cap or tune CTranslate2 threads. |
| Batch size | disabled | Pass `--batch-size N` to use batched inference when supported. |
| Parallel jobs | auto by RAM | Pass `--max-concurrent N` for an explicit shared limit. |

The same `--coordination-id` makes concurrent helper processes share a slot limit. Keep the
default for normal use; choose a distinct ID only for an intentionally independent workload.

## Procedure

1. **Confirm inputs.** Obtain or infer the absolute video path, output path, language,
   screenshot interval, and important proper nouns. Use defaults of `meeting_minutes.md`,
   `ja`, and 60 seconds when the user has no preference. Completion: the source file exists
   and every chosen parameter is known.

2. **Prepare an isolated output directory.** Avoid overwriting unrelated `meeting_audio.wav`,
   `meeting_audio.txt`, or `captures/` files. If they already exist, choose a job-specific
   directory or ask before replacing them. Completion: output paths are safe to write.

3. **Extract audio.** Invoke through `terminal`:

   ```bash
   ffmpeg -i "<VIDEO>" -vn -acodec pcm_s16le -ar 16000 -ac 1 meeting_audio.wav
   ```

   Completion: the command succeeds and `meeting_audio.wav` is non-empty.

4. **Extract reference captures.** Invoke through `terminal`:

   ```bash
   mkdir -p captures
   ffmpeg -i "<VIDEO>" -vf fps=1/<INTERVAL_SECONDS> captures/capture_%03d.png
   ```

   Completion: the command succeeds; an empty capture set is reported rather than hidden.

5. **Transcribe.** Build `--initial-prompt` from supplied names and domain vocabulary. Run:

   ```bash
   uv run --with faster-whisper python3 "SKILL_DIR/scripts/transcribe.py" meeting_audio.wav \
     --language "<LANGUAGE>" \
     --model large-v3 \
     --vad-filter \
     --initial-prompt "<TERMS>" \
     --cpu-threads 0 \
     --batch-size 0 \
     --max-concurrent 0 \
     --coordination-id omh-video-to-minutes-transcribe \
     --output meeting_audio.txt
   ```

   For recordings of 10 minutes or longer, **do not run this command as a blocking terminal
   call**. The terminal's execution timeout can kill a healthy transcription and force it to
   restart from the beginning. Start it with Hermes's background process support, retain the
   returned process/session ID, and poll that process plus `transcribe.log` until it exits. If
   background process support is unavailable, use a detached shell process and record its PID.
   Do not launch a second transcription while the first PID/session is alive. Do not declare
   completion until the process exits successfully, the log contains `Transcription saved to`,
   and the transcript file exists and is non-empty.

6. **Draft with Claude Haiku.** Resolve absolute paths for the transcript, capture directory,
   and draft output. Start a non-interactive Claude CLI process through `terminal`:

   ```bash
   claude --print --model claude-haiku-4-5 --no-session-persistence \
     --permission-mode acceptEdits \
     "Read the complete transcript at <ABSOLUTE_TRANSCRIPT_PATH> and the capture files under \
   <ABSOLUTE_CAPTURE_DIR>. Draft grounded Japanese meeting minutes at <ABSOLUTE_DRAFT_PATH> \
   using Highlights, Decisions, Action Items, Detailed Minutes, and Reference: Capture Images. \
   Use TBD for missing owners or dates. Do not invent facts. Return the draft path."
   ```

   Run this command with the output directory as its working directory. Keep the drafting prompt
   self-contained and require an absolute output path. If Claude CLI authentication is
   unavailable, stop and report that model-routing requirement; do not silently draft with the
   main model. Completion: the child command exits successfully and the draft file exists.

7. **Verify with the main model.** Start a second Codex CLI pass with the exact same model ID:

   ```bash
   codex exec --model gpt-5.6-luna \
     --config model_reasoning_effort='"low"' \
     --ephemeral --skip-git-repo-check \
     -C "<ABSOLUTE_OUTPUT_DIR>" \
     "Read <ABSOLUTE_TRANSCRIPT_PATH> and <ABSOLUTE_DRAFT_PATH>. Inspect relevant captures in \
   <ABSOLUTE_CAPTURE_DIR>. Verify every decision and action item against source evidence, remove \
   unsupported claims, and write the corrected minutes to <ABSOLUTE_OUTPUT_PATH>."
   ```

   The Luna verification pass must read the complete transcript and the Haiku draft. Correct
   likely transcription errors only when supported by supplied proper nouns or visible evidence.
   Completion: every retained decision and action item maps to source evidence.

8. **Write the deliverable.** Use this structure, omitting no section silently:

   ```markdown
   # Meeting Summary

   ## Highlights
   - ...

   ## Decisions
   - ...

   ## Action Items
   | Assignee | Task | Due |
   | --- | --- | --- |
   | TBD | ... | TBD |

   ## Detailed Minutes
   - ...

   ## Reference: Capture Images
   - captures/capture_001.png
   ```

   Use `TBD` for missing owners or dates and say `No explicit decisions identified` when
   appropriate. Completion: the requested Markdown path exists and contains grounded content.

9. **Report results.** Return the minutes path, transcript path, capture count, and the exact
   model IDs used for orchestration and drafting. Mention any skipped visual review or uncertain
   names.

## Pitfalls

1. **Running the helper from the output directory by relative path.** Resolve it from the
   installed skill directory first.
2. **Inventing structure from weak evidence.** An idea discussed is not a decision; a possible
   task is not assigned work unless the recording says so.
3. **Overwriting previous output.** Use a per-recording directory when standard filenames exist.
4. **Reading only the end of a long transcript.** Minutes require coverage of the full recording.
5. **Analyzing every capture.** Inspect selectively; periodic frames are references, not a second
   transcript.
6. **Treating model download time as a hang.** The first faster-whisper run can download model
   weights. Monitor logs and disk/network activity before interrupting it.
7. **Disabling the concurrency guard casually.** Use `--disable-concurrency-guard` only when the
   user accepts the memory risk.
8. **Starting a nested Hermes process for Haiku.** Hermes has one configured default model and
   its global delegation override affects unrelated tasks. Use Claude CLI with `--model` instead.
9. **Silently falling back from Haiku.** Treat unavailable Claude CLI authentication as a blocked
   routing requirement and tell the user to run `claude auth`.
10. **Passing `gpt-5.6` plus a prose "Luna mode" instruction.** Codex CLI exposes Luna as the
    model ID `gpt-5.6-luna`; pass it with `--model` or `-m` for every Luna run.
11. **Running Codex in a non-Git output directory without an override.** Meeting artifacts often
    live outside a repository. Pass `--skip-git-repo-check` so the one-shot run can start there.
12. **Relying on the user's default reasoning level.** Pass
    `--config model_reasoning_effort='"low"'` on every Luna invocation. This keeps the workflow
    lightweight while retaining a small reasoning budget, without modifying global configuration.
13. **Blocking on long transcription in one terminal call.** For recordings of 10 minutes or
    longer, start a background process and poll it. A terminal timeout can discard a partial
    transcript because the helper opens its output file from the beginning on every run.

## Verification

Verify the helper CLI without loading a model:

```bash
uv run --with faster-whisper python3 "SKILL_DIR/scripts/transcribe.py" --help
```

For a real recording, verification is complete only when all checks pass:

- Audio extraction exits successfully and produces a non-empty WAV file.
- Transcription logs `Transcription saved to` and produces a non-empty transcript.
- Capture count is recorded, including zero.
- The Markdown deliverable exists and does not contain unsupported claims.
- The run reports `gpt-5.6-luna` for orchestration and verification, and
  `claude-haiku-4-5` for drafting.
- Both Luna execution logs report `reasoning effort: low`.
