# VideoEngine
**RedLantern Studios — Internal AI Video Editing Engine**

Drop a video. Type what you want. Get it back edited.

Local Python server with a mobile-first web UI. Claude interprets plain English editing prompts into structured FFmpeg/Whisper pipelines. Jobs are tracked in Supabase and local JSON.

---

## Quick Start

```bash
bash start.sh
```

Then open `http://localhost:8765` in a browser, or `http://<your-mac-ip>:8765` from your phone (same WiFi).

---

## Requirements

| Tool | Install |
|---|---|
| Python 3.9+ | [python.org](https://python.org) |
| ffmpeg | `brew install ffmpeg` |
| whisper | `pip install openai-whisper` |
| anthropic | `pip install anthropic` |

Whisper and Anthropic are optional. Without them, the system falls back to silence removal only.

---

## How It Works

```
Phone uploads video
       ↓
POST /upload → multipart parse → save to inputs/
       ↓
create_job() → local JSON + Supabase sync
       ↓
enqueue_job() → FIFO queue (max 2 concurrent)
       ↓
process_job():
  1. PLANNING   → Claude interprets prompt → plan_raw
  2. VALIDATING → validate_plan() → rejects unknown ops / missing params
  3. PROCESSING → executes each step sequentially → temp/{job_id}_step_n.mp4
  4. COMPLETED  → copy to outputs/{job_id}_final.mp4 → cleanup temp
       ↓
UI polls /jobs every 2s → renders live state
       ↓
GET /download/{filename} → serve final file
```

---

## Operations

| Operation | What it does | Requires |
|---|---|---|
| `remove_silence` | Cuts audio pauses > threshold | ffmpeg |
| `remove_fillers` | Removes um/uh/like/you know | whisper |
| `burn_captions` | Auto-generates + burns subtitles | whisper |
| `trim` | Cuts video to start/end times | ffmpeg |
| `speed` | Changes playback speed | ffmpeg |
| `add_intro_text` | Overlays text at start | ffmpeg |
| `add_outro_text` | Overlays text at end | ffmpeg |
| `export_format` | Sets output format | ffmpeg |

---

## Job Lifecycle

```
queued → planning → validating → processing → completed
                                            ↘ failed
```

Every job has: `id, status, progress, current_step, logs[], plan_raw, plan_validated, error, input_path, output_path`

---

## API

| Route | Method | Description |
|---|---|---|
| `/` | GET | Web UI |
| `/status` | GET | Tool availability check |
| `/jobs` | GET | All jobs (last 10 log entries each) |
| `/job/<id>` | GET | Full single job record |
| `/upload` | POST | multipart/form-data: video, prompt, api_key |
| `/download/<filename>` | GET | Serve completed output file |

---

## File Structure

```
VideoEngine/
├── server.py       # Core server — job engine, operations, HTTP handler
├── ui.html         # Mobile-first single-file UI
├── start.sh        # Pre-flight check + launcher
├── tests.py        # Unit tests
├── inputs/         # Uploaded source videos (gitignored)
├── outputs/        # Final processed videos (gitignored)
├── temp/           # Intermediate step files, auto-cleaned (gitignored)
├── jobs/           # Per-job JSON records (gitignored)
└── logs/           # Reserved for future log files (gitignored)
```

---

## Supabase

Jobs sync to the `videojobs` table on `endovljmaudnxdzdapmf.supabase.co` (RedLantern Studios project). Local JSON in `jobs/` is the primary truth. Supabase is best-effort — fails silently if unreachable.

---

## Security

- Max file size: 500MB
- Accepted types: `.mp4`, `.mov`
- Filenames sanitized (path traversal stripped)
- API keys never stored on disk or returned via API
- Server binds `0.0.0.0` — intentional for local network phone access. Do not run on a public network.

---

## Known Limitations (Current Alpha)

| Issue | Impact | Priority |
|---|---|---|
| `remove_silence` uses audio-only filter — video track not cut | Sync broken on silent-cut videos | P0 |
| No aspect ratio conversion (9:16) | Not truly Shorts/Reels/TikTok ready | P1 |
| Whisper steps show 0% progress during transcription | UI appears stuck on long videos | P1 |
| No retry on failed jobs | Manual re-upload required | P1 |
| No volume normalization | Audio levels inconsistent | P1 |
| `export_format` is a passthrough copy | No actual format enforcement | P1 |
| No client-side file size check | Large files fail silently until server rejects | P1 |
| No preview before download | Can't verify output without downloading | P2 |
| No pagination on /jobs | Slows after 100+ jobs | P2 |

---

## Tests

```bash
python3 tests.py
```

Covers: validation (unknown ops, missing params, bad JSON), job manager (create/load/save/log), filename sanitization, failure injection.

---

## Status: Alpha

Core pipeline is functional. Claude planning, validation, and FFmpeg execution are all wired. The P0 silence removal sync bug must be fixed before this is reliable for real content. End-to-end testing with actual video files required before calling this production.
