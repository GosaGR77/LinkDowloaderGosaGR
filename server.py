#!/usr/bin/env python3
"""Video Downloader Backend v4 - with watermark removal + clip maker"""

import os
import threading
import uuid
import json
import subprocess
import sys
from flask import Flask, request, jsonify
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

DOWNLOAD_DIR = os.path.join(os.path.expanduser("~"), "Downloads", "VideoDownloader")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

jobs = {}
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


def run_download(job_id, url, format_id, output_dir, mode, regions=None):
    jobs[job_id] = {"status": "downloading", "progress": 0,
                    "filename": "", "error": "", "speed": "", "eta": "", "stage": ""}

    def progress_hook(d):
        if d["status"] == "downloading":
            pct = d.get("_percent_str", "0%").strip().replace("%", "")
            try:
                p = float(pct)
                if mode == "clean_wm":
                    jobs[job_id]["progress"] = p * 0.6
                else:
                    jobs[job_id]["progress"] = p
                jobs[job_id]["speed"] = d.get("_speed_str", "")
                jobs[job_id]["eta"] = d.get("_eta_str", "")
            except:
                pass
        elif d["status"] == "finished":
            jobs[job_id]["filename"] = d.get("filename", "")
            if mode != "clean_wm":
                jobs[job_id]["progress"] = 100

    import yt_dlp

    jobs[job_id]["stage"] = "Downloading video..."

    ydl_opts = {
        "outtmpl": os.path.join(output_dir, "%(title)s.%(ext)s"),
        "progress_hooks": [progress_hook],
        "noplaylist": True,
    }

    if mode == "audio":
        ydl_opts["format"] = "bestaudio/best"
        ydl_opts["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "320",
        }]
    elif mode in ("clean", "clean_wm"):
        if format_id and format_id != "best":
            ydl_opts["format"] = format_id
        else:
            ydl_opts["format"] = "bestvideo+bestaudio/best"
        ydl_opts["merge_output_format"] = "mp4"
        ydl_opts["postprocessor_args"] = {"merger": ["-sn"]}
        ydl_opts["writesubtitles"] = False
        ydl_opts["writeautomaticsub"] = False
    else:
        if format_id and format_id != "best":
            ydl_opts["format"] = format_id
        else:
            ydl_opts["format"] = "bestvideo+bestaudio/best"
        ydl_opts["merge_output_format"] = "mp4"

    downloaded_file = None
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            if mode == "audio":
                title = info.get("title", "video")
                for f in os.listdir(output_dir):
                    if f.endswith(".mp3") and title[:20].replace("/","_") in f:
                        downloaded_file = os.path.join(output_dir, f)
                        break
            else:
                title = info.get("title", "video")
                for f in os.listdir(output_dir):
                    if f.endswith(".mp4") and title[:15].replace("/","_").replace("\\","_") in f:
                        downloaded_file = os.path.join(output_dir, f)
                        break
                if not downloaded_file:
                    mp4s = [os.path.join(output_dir, f) for f in os.listdir(output_dir) if f.endswith(".mp4")]
                    if mp4s:
                        downloaded_file = max(mp4s, key=os.path.getmtime)

    except Exception as e:
        jobs[job_id]["status"] = "error"
        jobs[job_id]["error"] = str(e)
        return

    if mode == "clean_wm" and downloaded_file and os.path.exists(downloaded_file):
        jobs[job_id]["stage"] = "Removing watermarks..."
        jobs[job_id]["progress"] = 62

        remover_script = os.path.join(SCRIPT_DIR, "watermark_remover.py")
        base, ext = os.path.splitext(downloaded_file)
        out_path = base + "_clean" + ext

        region_args = []
        if regions:
            for r in regions:
                region_args += ["--regions", f"{r[0]},{r[1]},{r[2]},{r[3]}"]

        try:
            proc = subprocess.Popen(
                [sys.executable, remover_script, downloaded_file, out_path] + region_args,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding='utf-8',
                errors='replace'
            )

            for line in proc.stdout:
                line = line.strip()
                if line:
                    if line.startswith("[") and "%]" in line:
                        try:
                            pct_str = line[1:line.index("%]")]
                            pct = int(pct_str.strip())
                            jobs[job_id]["progress"] = 62 + int(pct * 0.36)
                        except:
                            pass
                        msg = line[line.index("]")+1:].strip()
                        jobs[job_id]["stage"] = msg

            proc.wait()

            if proc.returncode == 0 and os.path.exists(out_path):
                os.remove(downloaded_file)
                clean_name = base + "_CLEAN" + ext
                os.rename(out_path, clean_name)
                jobs[job_id]["filename"] = clean_name
                jobs[job_id]["progress"] = 100
                jobs[job_id]["status"] = "done"
            else:
                jobs[job_id]["status"] = "error"
                jobs[job_id]["error"] = "Watermark removal failed. Original file kept."
                jobs[job_id]["filename"] = downloaded_file
        except Exception as e:
            jobs[job_id]["status"] = "error"
            jobs[job_id]["error"] = str(e)
        return

    jobs[job_id]["status"] = "done"
    jobs[job_id]["progress"] = 100


def run_clip(job_id, url, format_id, output_dir, start_sec, end_sec, aspect_ratio):
    """
    Clip a video without downloading the whole file.

    Strategy: use yt-dlp to get the direct stream URL(s), then hand them
    straight to FFmpeg with -ss / -to so FFmpeg seeks in the HTTP stream
    and only pulls the bytes it needs.  No full download, no temp file.
    """
    import yt_dlp
    import re

    duration = end_sec - start_sec
    if duration <= 0:
        jobs[job_id] = {"status": "error", "progress": 0, "filename": "",
                        "error": "End time must be after start time.", "speed": "", "eta": "", "stage": ""}
        return

    jobs[job_id] = {
        "status": "downloading", "progress": 0,
        "filename": "", "error": "", "speed": "", "eta": "",
        "stage": "Resolving stream URL..."
    }

    # ── Step 1: resolve direct stream URLs (no download) ──
    fmt = format_id if format_id and format_id != "best" else "bestvideo+bestaudio/best"
    ydl_opts = {"quiet": True, "no_warnings": True, "noplaylist": True, "format": fmt}

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as e:
        jobs[job_id]["status"] = "error"
        jobs[job_id]["error"] = str(e)
        return

    title = info.get("title", "video")
    safe_title = re.sub(r'[\\/:*?"<>|]', '_', title)[:50]
    clip_suffix = f"_clip_{int(start_sec)}-{int(end_sec)}s"
    if aspect_ratio == "9:16":
        clip_suffix += "_vertical"
    out_path = os.path.join(output_dir, safe_title + clip_suffix + ".mp4")

    # Pull the best video+audio URLs out of the resolved info
    requested = info.get("requested_formats") or []
    if len(requested) >= 2:
        # Separate video and audio streams
        video_url = next((f["url"] for f in requested if f.get("vcodec","none") != "none"), None)
        audio_url = next((f["url"] for f in requested if f.get("acodec","none") != "none" and f.get("vcodec","none") == "none"), None)
    else:
        # Single muxed stream
        video_url = info.get("url")
        audio_url = None

    if not video_url:
        jobs[job_id]["status"] = "error"
        jobs[job_id]["error"] = "Could not resolve a stream URL for this video."
        return

    # ── Step 2: FFmpeg seeks in the HTTP stream – downloads only the clip ──
    jobs[job_id]["stage"] = f"Downloading clip {int(start_sec)}s → {int(end_sec)}s..."
    jobs[job_id]["progress"] = 5

    # Build video filter
    vf_parts = []
    if aspect_ratio == "9:16":
        vf_parts.append("crop=ih*9/16:ih:(iw-ih*9/16)/2:0")
        vf_parts.append("scale=1080:1920:flags=lanczos")

    # Build FFmpeg command
    # -ss before -i = fast HTTP seek (only the needed bytes are transferred)
    if audio_url and audio_url != video_url:
        # Two-stream input (bestvideo + bestaudio)
        ffmpeg_cmd = [
            "ffmpeg", "-y",
            "-ss", str(start_sec), "-to", str(end_sec), "-i", video_url,
            "-ss", str(start_sec), "-to", str(end_sec), "-i", audio_url,
        ]
    else:
        # Single muxed stream
        ffmpeg_cmd = [
            "ffmpeg", "-y",
            "-ss", str(start_sec), "-to", str(end_sec), "-i", video_url,
        ]

    if vf_parts:
        ffmpeg_cmd += ["-vf", ",".join(vf_parts),
                       "-c:v", "libx264", "-preset", "fast", "-crf", "20"]
    else:
        ffmpeg_cmd += ["-c:v", "copy"]   # no re-encode needed for original aspect

    ffmpeg_cmd += [
        "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart",
        out_path
    ]

    try:
        proc = subprocess.Popen(
            ffmpeg_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding='utf-8',
            errors='replace'
        )

        for line in proc.stdout:
            # Progress from time=HH:MM:SS
            m = re.search(r"time=(\d+):(\d+):([\d.]+)", line)
            if m:
                elapsed = int(m.group(1))*3600 + int(m.group(2))*60 + float(m.group(3))
                pct = min(elapsed / max(duration, 1), 1.0)
                jobs[job_id]["progress"] = 5 + int(pct * 93)
                jobs[job_id]["stage"] = (
                    f"{'Converting 9:16' if aspect_ratio=='9:16' else 'Clipping'}"
                    f"... {int(pct*100)}%"
                )
            # Speed info from bitrate line
            bm = re.search(r"speed=\s*([\d.]+)x", line)
            if bm:
                jobs[job_id]["speed"] = bm.group(1) + "x"

        proc.wait()

        if proc.returncode == 0 and os.path.exists(out_path):
            jobs[job_id]["filename"] = out_path
            jobs[job_id]["progress"] = 100
            jobs[job_id]["status"] = "done"
            jobs[job_id]["stage"] = ""
        else:
            jobs[job_id]["status"] = "error"
            jobs[job_id]["error"] = "FFmpeg failed. Make sure ffmpeg is installed and the URL is still valid."

    except FileNotFoundError:
        jobs[job_id]["status"] = "error"
        jobs[job_id]["error"] = "FFmpeg not found. Please install ffmpeg and make sure it's on your PATH."
    except Exception as e:
        jobs[job_id]["status"] = "error"
        jobs[job_id]["error"] = str(e)


@app.route("/info", methods=["POST"])
def get_info():
    data = request.json
    url = data.get("url", "").strip()
    if not url:
        return jsonify({"error": "No URL provided"}), 400

    import yt_dlp
    ydl_opts = {"quiet": True, "no_warnings": True, "noplaylist": True}

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)

        formats = []
        seen = set()
        for f in info.get("formats", []):
            vcodec = f.get("vcodec", "none")
            height = f.get("height")
            fps = f.get("fps")
            ext = f.get("ext", "")
            fid = f.get("format_id", "")
            filesize = f.get("filesize") or f.get("filesize_approx")
            tbr = f.get("tbr")
            if vcodec == "none" or not height:
                continue
            key = (height, fps, ext)
            if key in seen:
                continue
            seen.add(key)
            label = f"{height}p"
            if fps and fps > 30:
                label += f" {int(fps)}fps"
            label += f" ({ext.upper()})"
            size_str = f"{filesize/1024/1024:.1f} MB" if filesize else ""
            formats.append({"id": fid, "label": label, "height": height,
                            "fps": fps or 30, "ext": ext, "size": size_str, "tbr": tbr or 0})

        formats.sort(key=lambda x: (x["height"], x["fps"], x["tbr"]), reverse=True)
        formats.insert(0, {"id": "best", "label": "Best Quality (auto merge)",
                           "height": 9999, "fps": 9999, "ext": "mp4", "size": "", "tbr": 9999})

        return jsonify({
            "title": info.get("title", "Unknown"),
            "thumbnail": info.get("thumbnail", ""),
            "duration": info.get("duration", 0),
            "uploader": info.get("uploader", ""),
            "formats": formats,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/download", methods=["POST"])
def start_download():
    data = request.json
    url = data.get("url", "").strip()
    format_id = data.get("format_id", "best")
    output_dir = data.get("output_dir", DOWNLOAD_DIR)
    mode = data.get("mode", "video")
    regions = data.get("regions", None)
    if not url:
        return jsonify({"error": "No URL"}), 400

    job_id = str(uuid.uuid4())[:8]
    thread = threading.Thread(
        target=run_download,
        args=(job_id, url, format_id, output_dir, mode, regions)
    )
    thread.daemon = True
    thread.start()
    return jsonify({"job_id": job_id})


@app.route("/clip", methods=["POST"])
def start_clip():
    data = request.json
    url = data.get("url", "").strip()
    format_id = data.get("format_id", "best")
    output_dir = data.get("output_dir", DOWNLOAD_DIR)
    start_sec = float(data.get("start_sec", 0))
    end_sec = float(data.get("end_sec", 30))
    aspect_ratio = data.get("aspect_ratio", "original")  # "original" or "9:16"

    if not url:
        return jsonify({"error": "No URL"}), 400
    if end_sec <= start_sec:
        return jsonify({"error": "End time must be after start time"}), 400
    if end_sec - start_sec > 3600:
        return jsonify({"error": "Clip duration cannot exceed 1 hour"}), 400

    job_id = str(uuid.uuid4())[:8]
    thread = threading.Thread(
        target=run_clip,
        args=(job_id, url, format_id, output_dir, start_sec, end_sec, aspect_ratio)
    )
    thread.daemon = True
    thread.start()
    return jsonify({"job_id": job_id})


@app.route("/progress/<job_id>")
def get_progress(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    return jsonify(job)


@app.route("/default-dir")
def get_default_dir():
    return jsonify({"dir": DOWNLOAD_DIR})


if __name__ == "__main__":
    print("\n  Video Downloader Server v4 running!")
    print(f"  Downloads: {DOWNLOAD_DIR}")
    print("  Open index.html in your browser\n")
    app.run(port=7842, debug=False)