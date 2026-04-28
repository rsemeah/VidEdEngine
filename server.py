#!/usr/bin/env python3
"""
VideoEngine — AI-powered video editing server
RedLantern Studios
Run: python3 server.py
"""

import os, json, uuid, threading, subprocess, sys, re, shutil, queue, urllib.request, urllib.error
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse
import socketserver, socket

# ── Config ───────────────────────────────────────────────────────────────────
PORT           = 8765
BASE_DIR       = os.path.dirname(os.path.abspath(__file__))
INPUTS_DIR     = os.path.join(BASE_DIR, "inputs")
OUTPUTS_DIR    = os.path.join(BASE_DIR, "outputs")
TEMP_DIR       = os.path.join(BASE_DIR, "temp")
LOGS_DIR       = os.path.join(BASE_DIR, "logs")
JOBS_DIR       = os.path.join(BASE_DIR, "jobs")
MAX_FILE_SIZE  = 500 * 1024 * 1024
ALLOWED_EXTS   = {".mp4", ".mov"}
MAX_CONCURRENT = 2

SUPABASE_URL    = "https://endovljmaudnxdzdapmf.supabase.co"
SUPABASE_ANON   = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImVuZG92bGptYXVkbnhkemRhcG1mIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NzMwMjg3NTYsImV4cCI6MjA4ODYwNDc1Nn0.opc8usbAUsbWf6pX2Uqiwa5cftyTansA_1pkxUWIf4w"

for d in [INPUTS_DIR, OUTPUTS_DIR, TEMP_DIR, LOGS_DIR, JOBS_DIR]:
    os.makedirs(d, exist_ok=True)

# ── Status constants ──────────────────────────────────────────────────────────
QUEUED     = "queued"
PLANNING   = "planning"
VALIDATING = "validating"
PROCESSING = "processing"
COMPLETED  = "completed"
FAILED     = "failed"

# ── Job Store (Supabase primary, local JSON fallback) ─────────────────────────
_job_lock = threading.Lock()

def _sb_headers():
    return {
        "apikey": SUPABASE_ANON,
        "Authorization": f"Bearer {SUPABASE_ANON}",
        "Content-Type": "application/json",
        "Prefer": "return=minimal"
    }

def _sb_request(method, path, body=None):
    url = f"{SUPABASE_URL}{path}"
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, headers=_sb_headers(), method=method)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else []
    except Exception:
        return None

def _local_path(job_id):
    return os.path.join(JOBS_DIR, f"{job_id}.json")

def _save_local(job):
    with _job_lock:
        with open(_local_path(job["id"]), "w") as f:
            json.dump(job, f, indent=2)

def _load_local(job_id):
    p = _local_path(job_id)
    if not os.path.exists(p):
        return None
    with _job_lock:
        with open(p) as f:
            return json.load(f)

def create_job(video_name, prompt, input_path):
    job_id = str(uuid.uuid4())[:8]
    now = datetime.utcnow().isoformat()
    job = {
        "id": job_id, "video_name": video_name, "prompt": prompt,
        "status": QUEUED, "progress": 0, "current_step": None,
        "steps": [], "logs": [], "plan_raw": None, "plan_validated": None,
        "error": None, "input_path": input_path, "output_path": None,
        "output_filename": None, "created_at": now, "updated_at": now,
    }
    _save_local(job)
    _sb_request("POST", "/rest/v1/videojobs", job)
    return job_id, job

def load_job(job_id):
    local = _load_local(job_id)
    if local:
        return local
    result = _sb_request("GET", f"/rest/v1/videojobs?id=eq.{job_id}&limit=1")
    return result[0] if result else None

def save_job(job):
    job["updated_at"] = datetime.utcnow().isoformat()
    _save_local(job)
    payload = {k: v for k, v in job.items() if k != "id"}
    _sb_request("PATCH", f"/rest/v1/videojobs?id=eq.{job['id']}", payload)

def list_jobs():
    jobs = []
    for fname in sorted(os.listdir(JOBS_DIR), reverse=True):
        if fname.endswith(".json"):
            try:
                with open(os.path.join(JOBS_DIR, fname)) as f:
                    jobs.append(json.load(f))
            except Exception:
                pass
    return jobs

def append_log(job, level, step, message):
    entry = {"timestamp": datetime.utcnow().isoformat(), "level": level,
             "step": step, "message": message}
    job["logs"].append(entry)
    save_job(job)

# ── Tool checks ───────────────────────────────────────────────────────────────
def check_tool(name):
    return subprocess.run(["which", name], capture_output=True).returncode == 0

def check_pkg(pkg):
    return subprocess.run([sys.executable, "-c", f"import {pkg}"], capture_output=True).returncode == 0

def get_system_status():
    return {
        "ffmpeg":         check_tool("ffmpeg"),
        "whisper":        check_pkg("whisper"),
        "faster_whisper": check_pkg("faster_whisper"),
        "anthropic":      check_pkg("anthropic"),
        "moviepy":        check_pkg("moviepy"),
    }

# ── Claude Planning Layer ─────────────────────────────────────────────────────
PLANNING_SYSTEM = """You are a video editing pipeline planner.
Output ONLY valid JSON — no markdown, no explanation, no code fences.
Available operations: remove_fillers, remove_silence (params: threshold_seconds, min_duration),
burn_captions (params: style "youtube"|"reels"|"tiktok"), trim (params: start, end),
speed (params: factor), add_intro_text (params: text, duration_seconds),
add_outro_text (params: text, duration_seconds), export_format (params: format "mp4"|"mov")
Output: {"summary": "...", "operations": [{"op": "...", "params": {...}}]}"""

def get_plan_from_claude(prompt, api_key):
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        msg = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1000, system=PLANNING_SYSTEM,
            messages=[{"role": "user", "content": f"Edit request: {prompt}"}]
        )
        return json.loads(msg.content[0].text)
    except json.JSONDecodeError:
        raise ValueError("Invalid plan format: Claude returned non-JSON")
    except Exception:
        return {"summary": "Remove silence (Claude unavailable — fallback)",
                "operations": [{"op": "remove_silence",
                                 "params": {"threshold_seconds": 1.0, "min_duration": 0.3}}]}

# ── Validation Layer ──────────────────────────────────────────────────────────
OPERATION_REGISTRY = {
    "remove_fillers":  {"required": [], "defaults": {}},
    "remove_silence":  {"required": [], "defaults": {"threshold_seconds": 1.0, "min_duration": 0.3}},
    "burn_captions":   {"required": [], "defaults": {"style": "youtube"}},
    "trim":            {"required": ["start", "end"], "defaults": {}},
    "speed":           {"required": ["factor"], "defaults": {}},
    "add_intro_text":  {"required": ["text"], "defaults": {"duration_seconds": 3.0}},
    "add_outro_text":  {"required": ["text"], "defaults": {"duration_seconds": 3.0}},
    "export_format":   {"required": [], "defaults": {"format": "mp4"}},
}

def validate_plan(plan_raw):
    if not isinstance(plan_raw, dict):
        raise ValueError("Invalid plan format: expected dict")
    ops = plan_raw.get("operations")
    if not isinstance(ops, list) or not ops:
        raise ValueError("Invalid plan format: 'operations' must be a non-empty list")
    validated = []
    for op in ops:
        name = op.get("op")
        if name not in OPERATION_REGISTRY:
            raise ValueError(f"Unsupported operation: {name}")
        spec = OPERATION_REGISTRY[name]
        params = {**spec["defaults"], **(op.get("params") or {})}
        for req in spec["required"]:
            if req not in params:
                raise ValueError(f"Missing parameter '{req}' for operation '{name}'")
        validated.append({"op": name, "params": params})
    return {"summary": plan_raw.get("summary", ""), "operations": validated}

# ── Operation Handlers ────────────────────────────────────────────────────────
FFMPEG = "ffmpeg"

def _ffmpeg(cmd, job, step):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"FFmpeg failed [{step}]: {(r.stderr or '')[-400:]}")

def handle_remove_fillers(params, inp, out, job):
    try:
        import whisper
        append_log(job, "info", "remove_fillers", "Loading Whisper model...")
        model = whisper.load_model("base")
        result = model.transcribe(inp, word_timestamps=True)
        fillers = {"um","uh","like","you know","so","actually","basically","literally"}
        segs = []
        for seg in result["segments"]:
            for w in seg.get("words", []):
                word = w["word"].strip().lower().strip(".,!?")
                if word not in fillers:
                    segs.append((w["start"], w["end"]))
        if not segs:
            subprocess.run([FFMPEG,"-y","-i",inp,"-c","copy",out], capture_output=True)
            return
        segs = segs[:500]
        parts = [f"[0:v]trim={s}:{e},setpts=PTS-STARTPTS[v{i}];[0:a]atrim={s}:{e},asetpts=PTS-STARTPTS[a{i}]"
                 for i,(s,e) in enumerate(segs)]
        concat = "".join(f"[v{i}][a{i}]" for i in range(len(segs)))
        concat += f"concat=n={len(segs)}:v=1:a=1[outv][outa]"
        _ffmpeg([FFMPEG,"-y","-i",inp,"-filter_complex",";".join(parts)+";"+concat,
                 "-map","[outv]","-map","[outa]",out], job, "remove_fillers")
    except ImportError:
        append_log(job, "warn", "remove_fillers", "Whisper not available — copying")
        subprocess.run([FFMPEG,"-y","-i",inp,"-c","copy",out], capture_output=True)

def handle_remove_silence(params, inp, out, job):
    t = params.get("threshold_seconds", 1.0)
    _ffmpeg([FFMPEG,"-y","-i",inp,"-af",
             f"silenceremove=stop_periods=-1:stop_duration={t}:stop_threshold=-50dB",
             "-c:v","copy",out], job, "remove_silence")

def handle_burn_captions(params, inp, out, job):
    style = params.get("style","youtube")
    try:
        import whisper as _w
        model = _w.load_model("base")
        result = model.transcribe(inp)
        srt = out.replace(".mp4",".srt")
        with open(srt,"w") as f:
            for i,seg in enumerate(result["segments"]):
                def fmt(t):
                    h,m=int(t//3600),int((t%3600)//60); s,ms=int(t%60),int((t%1)*1000)
                    return f"{h:02}:{m:02}:{s:02},{ms:03}"
                f.write(f"{i+1}\n{fmt(seg['start'])} --> {fmt(seg['end'])}\n{seg['text'].strip()}\n\n")
        fs = {"youtube":18,"reels":28,"tiktok":32}.get(style,18)
        vf = f"subtitles={srt}:force_style='FontSize={fs},PrimaryColour=&HFFFFFF&,OutlineColour=&H000000&,Outline=2'"
        _ffmpeg([FFMPEG,"-y","-i",inp,"-vf",vf,out], job, "burn_captions")
    except Exception as e:
        append_log(job,"warn","burn_captions",f"Failed: {e} — copying")
        subprocess.run([FFMPEG,"-y","-i",inp,"-c","copy",out], capture_output=True)

def handle_trim(params, inp, out, job):
    cmd = [FFMPEG,"-y","-i",inp,"-ss",str(params.get("start",0))]
    if params.get("end") is not None:
        cmd += ["-to",str(params["end"])]
    _ffmpeg(cmd+["-c","copy",out], job, "trim")

def handle_speed(params, inp, out, job):
    factor = float(params.get("factor",1.5))
    if factor <= 0:
        raise ValueError("Speed factor must be > 0")
    vf = f"setpts={1/factor}*PTS"
    # Chain atempo for values outside 0.5–2.0
    atempo, f = [], factor
    while f > 2.0:  atempo.append("atempo=2.0"); f /= 2.0
    while f < 0.5:  atempo.append("atempo=0.5"); f /= 0.5
    atempo.append(f"atempo={f:.4f}")
    _ffmpeg([FFMPEG,"-y","-i",inp,"-vf",vf,"-af",",".join(atempo),out], job, "speed")

def handle_add_intro_text(params, inp, out, job):
    text = params.get("text","").replace("'","\\'")
    dur  = params.get("duration_seconds",3.0)
    vf = (f"drawtext=text='{text}':fontcolor=white:fontsize=48"
          f":x=(w-text_w)/2:y=(h-text_h)/2:enable='between(t,0,{dur})'"
          f":box=1:boxcolor=black@0.5")
    _ffmpeg([FFMPEG,"-y","-i",inp,"-vf",vf,"-c:a","copy",out], job, "add_intro_text")

def handle_add_outro_text(params, inp, out, job):
    probe = subprocess.run(["ffprobe","-v","quiet","-print_format","json","-show_format",inp],
                           capture_output=True, text=True)
    try:
        duration = float(json.loads(probe.stdout)["format"]["duration"])
    except Exception:
        duration = 0
    text = params.get("text","").replace("'","\\'")
    dur  = params.get("duration_seconds",3.0)
    start = max(0, duration - dur)
    vf = (f"drawtext=text='{text}':fontcolor=white:fontsize=48"
          f":x=(w-text_w)/2:y=(h-text_h)/2:enable='between(t,{start},{duration})'"
          f":box=1:boxcolor=black@0.5")
    _ffmpeg([FFMPEG,"-y","-i",inp,"-vf",vf,"-c:a","copy",out], job, "add_outro_text")

def handle_export_format(params, inp, out, job):
    _ffmpeg([FFMPEG,"-y","-i",inp,"-c","copy",out], job, "export_format")

OPERATION_HANDLERS = {
    "remove_fillers":  handle_remove_fillers,
    "remove_silence":  handle_remove_silence,
    "burn_captions":   handle_burn_captions,
    "trim":            handle_trim,
    "speed":           handle_speed,
    "add_intro_text":  handle_add_intro_text,
    "add_outro_text":  handle_add_outro_text,
    "export_format":   handle_export_format,
}

# ── Execution Engine ──────────────────────────────────────────────────────────
def process_job(job_id, api_key=""):
    job = load_job(job_id)
    if not job:
        return
    try:
        # PLANNING
        job["status"] = PLANNING
        append_log(job, "info", "planning", "Asking Claude to interpret your prompt...")
        try:
            plan_raw = get_plan_from_claude(job["prompt"], api_key)
        except ValueError as e:
            job["status"] = FAILED; job["error"] = str(e)
            append_log(job, "error", "planning", str(e)); save_job(job); return

        job["plan_raw"] = plan_raw
        append_log(job, "info", "planning", f"Plan: {plan_raw.get('summary','')}")

        # VALIDATING
        job["status"] = VALIDATING
        append_log(job, "info", "validating", "Validating plan...")
        try:
            plan_v = validate_plan(plan_raw)
        except ValueError as e:
            job["status"] = FAILED; job["error"] = str(e)
            append_log(job, "error", "validating", str(e)); save_job(job); return

        job["plan_validated"] = plan_v
        job["steps"] = [op["op"] for op in plan_v["operations"]]
        append_log(job, "info", "validating", f"Valid — {len(job['steps'])} step(s)")

        # PROCESSING
        job["status"] = PROCESSING
        save_job(job)
        current_input = job["input_path"]
        ops = plan_v["operations"]
        total = len(ops)

        for i, op in enumerate(ops):
            name = op["op"]
            step_out = os.path.join(TEMP_DIR, f"{job_id}_step{i}.mp4")
            job["current_step"] = name
            job["progress"] = int((i / total) * 90)
            append_log(job, "info", name, f"Step {i+1}/{total}: {name}")
            try:
                OPERATION_HANDLERS[name](op["params"], current_input, step_out, job)
            except Exception as e:
                job["status"] = FAILED
                job["error"] = f"Step '{name}' failed: {e}"
                append_log(job, "error", name, job["error"]); save_job(job); return

            if os.path.exists(step_out) and os.path.getsize(step_out) > 0:
                current_input = step_out
                append_log(job, "info", name, f"✓ Step {i+1} complete")
            else:
                append_log(job, "warn", name, f"Step {i+1} output empty — using previous")
            save_job(job)

        # Finalize
        final_name = f"{job_id}_final.mp4"
        final_path = os.path.join(OUTPUTS_DIR, final_name)
        shutil.copy2(current_input, final_path)
        # Cleanup temp
        for i in range(total):
            for ext in (".mp4", ".srt"):
                tmp = os.path.join(TEMP_DIR, f"{job_id}_step{i}{ext}")
                if os.path.exists(tmp):
                    os.remove(tmp)
        job["status"] = COMPLETED
        job["output_path"] = final_path
        job["output_filename"] = final_name
        job["progress"] = 100
        job["current_step"] = None
        append_log(job, "info", "completed", "✅ All done! Your video is ready.")
        save_job(job)

    except Exception as e:
        job = load_job(job_id) or job
        job["status"] = FAILED
        job["error"] = f"Unexpected error: {e}"
        append_log(job, "error", "system", job["error"])
        save_job(job)

# ── Concurrency Queue ─────────────────────────────────────────────────────────
_queue     = queue.Queue()
_key_store = {}
_key_lock  = threading.Lock()

def _worker():
    while True:
        job_id = _queue.get()
        with _key_lock:
            api_key = _key_store.pop(job_id, "")
        try:
            process_job(job_id, api_key)
        finally:
            _queue.task_done()

for _ in range(MAX_CONCURRENT):
    threading.Thread(target=_worker, daemon=True).start()

def enqueue_job(job_id, api_key=""):
    with _key_lock:
        _key_store[job_id] = api_key
    _queue.put(job_id)

# ── Utilities ─────────────────────────────────────────────────────────────────
def sanitize_filename(name):
    name = os.path.basename(name)
    name = re.sub(r'[^\w\-_\. ]', '_', name).strip().replace(' ', '_')
    return name or "upload.mp4"

def get_local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80)); return s.getsockname()[0]
    finally:
        s.close()

# ── HTTP Handler ──────────────────────────────────────────────────────────────
class VideoEngineHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args): pass

    def send_json(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(body))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, path):
        with open(path, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Content-Length", len(data))
        self.send_header("Content-Disposition", f'attachment; filename="{os.path.basename(path)}"')
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(data)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        path = urlparse(self.path).path

        if path in ("/", "/index.html"):
            html = open(os.path.join(BASE_DIR, "ui.html"), "rb").read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", len(html))
            self.end_headers()
            self.wfile.write(html)

        elif path == "/status":
            self.send_json(get_system_status())

        elif path == "/jobs":
            jobs = list_jobs()
            self.send_json([{
                "id": j["id"], "status": j["status"], "progress": j.get("progress",0),
                "current_step": j.get("current_step"), "prompt": j.get("prompt",""),
                "video_name": j.get("video_name",""), "error": j.get("error"),
                "plan": j.get("plan_validated") or j.get("plan_raw"),
                "logs": j.get("logs",[])[-10:],
                "output_filename": j.get("output_filename"),
                "created_at": j.get("created_at"),
            } for j in jobs])

        elif path.startswith("/job/"):
            job = load_job(path.split("/job/")[1])
            self.send_json(job if job else {"error":"not found"}, 200 if job else 404)

        elif path.startswith("/download/"):
            fp = os.path.join(OUTPUTS_DIR, os.path.basename(path.split("/download/")[1]))
            if os.path.exists(fp):
                self.send_file(fp)
            else:
                self.send_json({"error": "file not found"}, 404)
        else:
            self.send_json({"error": "not found"}, 404)

    def do_POST(self):
        if urlparse(self.path).path != "/upload":
            self.send_json({"error": "not found"}, 404); return

        content_type   = self.headers.get("Content-Type", "")
        content_length = int(self.headers.get("Content-Length", 0))

        if content_length > MAX_FILE_SIZE:
            self.send_json({"error": f"File too large. Max {MAX_FILE_SIZE//(1024*1024)}MB"}, 413); return

        body     = self.rfile.read(content_length)
        boundary = content_type.split("boundary=")[-1].encode()
        parts    = body.split(b"--" + boundary)
        fields, file_data, file_name = {}, None, "upload.mp4"

        for part in parts:
            if b"Content-Disposition" not in part:
                continue
            header, _, content = part.partition(b"\r\n\r\n")
            content    = content.rstrip(b"\r\n--")
            header_str = header.decode("utf-8", errors="ignore")
            if 'name="prompt"'  in header_str: fields["prompt"]  = content.decode("utf-8", errors="ignore")
            elif 'name="api_key"' in header_str: fields["api_key"] = content.decode("utf-8", errors="ignore")
            elif 'name="video"'  in header_str:
                m = re.search(r'filename="([^"]*)"', header_str)
                if m:
                    file_name = sanitize_filename(m.group(1))
                file_data = content

        if not file_data:
            self.send_json({"error": "No video file received"}, 400); return

        ext = os.path.splitext(file_name)[1].lower()
        if ext not in ALLOWED_EXTS:
            self.send_json({"error": f"Unsupported type. Allowed: {', '.join(ALLOWED_EXTS)}"}, 400); return

        tmp_id     = str(uuid.uuid4())[:8]
        input_path = os.path.join(INPUTS_DIR, f"{tmp_id}_{file_name}")
        with open(input_path, "wb") as f:
            f.write(file_data)

        job_id, job = create_job(file_name, fields.get("prompt","Clean up the video"), input_path)
        final_input = os.path.join(INPUTS_DIR, f"{job_id}_{file_name}")
        os.rename(input_path, final_input)
        job["input_path"] = final_input
        save_job(job)

        enqueue_job(job_id, fields.get("api_key",""))
        self.send_json({"job_id": job_id, "status": QUEUED})


class ThreadedHTTPServer(socketserver.ThreadingMixIn, HTTPServer):
    daemon_threads = True


if __name__ == "__main__":
    ip = get_local_ip()
    print(f"\n{'='*50}")
    print(f"  🎬 VideoEngine is RUNNING")
    print(f"  Local:  http://localhost:{PORT}")
    print(f"  Phone:  http://{ip}:{PORT}  (same WiFi)")
    print(f"  Supabase: {SUPABASE_URL}")
    print(f"{'='*50}\n")
    server = ThreadedHTTPServer(("0.0.0.0", PORT), VideoEngineHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n👋 VideoEngine stopped.")
