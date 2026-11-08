"""
ComfyUI-Blackbox-Media: Universal Video, GIF, WebP & Image Sequence streaming suite
with Transactional Auto-Chunking Queue, Lazy Frame Decoding,
Seamless Cross-Chunk RAFT Optical Flow Deflickering, and FFmpeg MP4/WebM/Audio Muxing.
License: MIT
"""

import os
import io
import re
import gc
import sys
import json
import math
import wave
import uuid
import base64
import shutil
import time
import subprocess
import numpy as np
import cv2
from PIL import Image, ImageSequence
from tqdm import tqdm
from aiohttp import web

import torch
import torch.nn.functional as F
from torchvision.models.optical_flow import raft_large, raft_small, Raft_Large_Weights, Raft_Small_Weights

import folder_paths
import comfy.model_management as mm
import comfy.utils
from server import PromptServer


# ==============================================================================
# CONSTANTS & FORMAT HELPERS
# ==============================================================================

VALID_IMAGE_EXTS = ('.png', '.jpg', '.jpeg', '.webp', '.bmp', '.tiff', '.tif', '.gif')
VALID_VIDEO_EXTS = ('.mp4', '.mkv', '.mov', '.avi', '.webm', '.m4v')
ALL_SUPPORTED_EXTS = VALID_IMAGE_EXTS + VALID_VIDEO_EXTS


def natural_sort_key(s: str):
    return [int(text) if text.isdigit() else text.lower() for text in re.split(r'(\d+)', s)]


def format_eta(seconds: float) -> str:
    if seconds <= 0 or math.isnan(seconds) or math.isinf(seconds):
        return "0s"
    sec = int(round(seconds))
    if sec < 60:
        return f"{sec}s"
    mins, s = divmod(sec, 60)
    if mins < 60:
        return f"{mins}m {s:02d}s"
    hrs, m = divmod(mins, 60)
    return f"{hrs}h {m:02d}m"


def extract_animation_frames_and_durations(pil_img: Image.Image):
    frames_rgb = []
    durations = []
    canvas = Image.new("RGBA", pil_img.size, (0, 0, 0, 0))

    for frame in ImageSequence.Iterator(pil_img):
        dur = frame.info.get("duration", 80)
        durations.append(int(dur) if (dur and dur > 10) else 80)

        frame_rgba = frame.convert("RGBA")
        canvas.alpha_composite(frame_rgba)
        frames_rgb.append(canvas.convert("RGB").copy())

    return frames_rgb, durations


def list_sequence_files_in_dir(dir_path: str):
    if not os.path.isdir(dir_path):
        return []
    files = [
        f for f in os.listdir(dir_path)
        if os.path.isfile(os.path.join(dir_path, f)) and f.lower().endswith(VALID_IMAGE_EXTS)
    ]
    files.sort(key=natural_sort_key)
    return [os.path.join(dir_path, f) for f in files]


def probe_media_container_info(target_path: str) -> dict:
    """
    Extracts container, codec, bitrate, and audio stream details via ffprobe if available.
    """
    info = {
        "file_size_mb": 0.0,
        "video_codec": "unknown",
        "pix_fmt": "rgb24",
        "bitrate_kbps": 0,
        "audio_codec": "none",
        "audio_sample_rate": 0,
        "audio_channels": 0,
    }
    if not os.path.exists(target_path):
        return info

    if os.path.isdir(target_path):
        seq_files = list_sequence_files_in_dir(target_path)
        total_bytes = sum(os.path.getsize(f) for f in seq_files[:500])
        if len(seq_files) > 500:
            total_bytes = int(total_bytes * (len(seq_files) / 500.0))
        info["file_size_mb"] = round(total_bytes / (1024 * 1024), 2)
        ext = os.path.splitext(seq_files[0])[1].lower().lstrip('.') if seq_files else "seq"
        info["video_codec"] = f"image_seq ({ext})"
        return info

    try:
        info["file_size_mb"] = round(os.path.getsize(target_path) / (1024 * 1024), 2)
    except OSError:
        pass

    ext = os.path.splitext(target_path)[1].lower()
    if ext in ('.gif', '.webp'):
        info["video_codec"] = ext.lstrip('.')
        return info

    try:
        cmd = [
            "ffprobe", "-v", "quiet",
            "-print_format", "json",
            "-show_format", "-show_streams",
            target_path
        ]
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=4.0, check=False)
        if res.returncode == 0 and res.stdout:
            data = json.loads(res.stdout)
            fmt = data.get("format", {})
            if "bit_rate" in fmt:
                try:
                    info["bitrate_kbps"] = int(fmt["bit_rate"]) // 1000
                except (ValueError, TypeError):
                    pass
            for s in data.get("streams", []):
                stype = s.get("codec_type")
                if stype == "video" and info["video_codec"] == "unknown":
                    info["video_codec"] = s.get("codec_name", "unknown")
                    info["pix_fmt"] = s.get("pix_fmt", "unknown")
                elif stype == "audio" and info["audio_codec"] == "none":
                    info["audio_codec"] = s.get("codec_name", "audio")
                    try:
                        info["audio_sample_rate"] = int(s.get("sample_rate", 0))
                    except (ValueError, TypeError):
                        pass
                    info["audio_channels"] = int(s.get("channels", 0))
    except Exception:
        pass

    return info


# ==============================================================================
# TRANSACTIONAL AUTO-CHUNKING STATE MACHINE (TWO-PHASE COMMIT)
# ==============================================================================

CHUNK_STREAM_SESSIONS = {}
LAST_ACTIVE_SIGNATURE = None


def cleanup_stale_swap_dirs_on_startup():
    """
    Removes orphaned blackbox_stream_* swap folders from ComfyUI/temp on startup.
    """
    try:
        temp_root = folder_paths.get_temp_directory()
        if os.path.isdir(temp_root):
            for name in os.listdir(temp_root):
                if name.startswith("blackbox_stream_"):
                    full_p = os.path.join(temp_root, name)
                    if os.path.isdir(full_p):
                        shutil.rmtree(full_p, ignore_errors=True)
    except Exception:
        pass


cleanup_stale_swap_dirs_on_startup()


def destroy_session(sig: str):
    global LAST_ACTIVE_SIGNATURE
    st = CHUNK_STREAM_SESSIONS.pop(sig, None)
    if st is not None:
        tdir = st.get("temp_dir")
        if tdir and os.path.exists(tdir):
            shutil.rmtree(tdir, ignore_errors=True)
    if LAST_ACTIVE_SIGNATURE == sig:
        LAST_ACTIVE_SIGNATURE = None


def destroy_all_sessions():
    for sig in list(CHUNK_STREAM_SESSIONS.keys()):
        destroy_session(sig)


def create_new_chunk_session(sig: str, total_frames: int) -> dict:
    global LAST_ACTIVE_SIGNATURE
    destroy_all_sessions()

    session_dir = os.path.join(
        folder_paths.get_temp_directory(),
        f"blackbox_stream_{int(time.time() * 1000) % 10000000}_{uuid.uuid4().hex[:6]}"
    )
    os.makedirs(session_dir, exist_ok=True)

    state = {
        "signature": sig,
        "cursor": 0,                  # Committed frame cursor (updated ONLY at the end of Saver)
        "pending_cursor": 0,          # Candidate cursor for the currently executing chunk
        "chunk_in_flight": False,     # True while a chunk is being processed; reset to False in Saver
        "total_frames": total_frames,
        "temp_dir": session_dir,
        "saved_files": [],
        "start_timestamp": time.time(),
        "completed_chunks": 0,
        # Committed cross-chunk context for OpticalFlowDeflicker:
        "ref_frame": None,
        "tail_frames": [],
        "first_frames": [],
        # Uncommitted context for the active chunk:
        "pending_deflicker": None,
        "cached_audio": None,
        "cached_probe": None,
    }
    CHUNK_STREAM_SESSIONS[sig] = state
    LAST_ACTIVE_SIGNATURE = sig
    return state


def get_session_for_node(meta: dict = None):
    if isinstance(meta, dict):
        sig = meta.get("session_signature")
        if sig and sig in CHUNK_STREAM_SESSIONS:
            return CHUNK_STREAM_SESSIONS[sig]
    if LAST_ACTIVE_SIGNATURE and LAST_ACTIVE_SIGNATURE in CHUNK_STREAM_SESSIONS:
        return CHUNK_STREAM_SESSIONS[LAST_ACTIVE_SIGNATURE]
    if len(CHUNK_STREAM_SESSIONS) == 1:
        return next(iter(CHUNK_STREAM_SESSIONS.values()))
    return None


@PromptServer.instance.routes.post("/blackbox_media/reset_chunks")
async def reset_all_chunk_sessions(request):
    destroy_all_sessions()
    gc.collect()
    mm.soft_empty_cache()
    return web.json_response({"status": "reset"})


# ==============================================================================
# LAZY VIDEO & SEQUENCE LISTS (ON-DEMAND FRAME DECODING)
# ==============================================================================

class LazyVideoFrameList(list):
    """
    Subclasses Python list for full compatibility with ComfyUI execution.py,
    decoding individual video frames on demand inside __getitem__ with zero upfront RAM allocation.
    """
    def __init__(self, video_path: str, target_indices: list, orig_w: int, orig_h: int):
        super().__init__()
        self.video_path = video_path
        self.target_indices = list(target_indices)
        self.orig_w = orig_w
        self.orig_h = orig_h
        self._cap = None
        self._current_pos = -1

    def _ensure_cap(self):
        if self._cap is None or not self._cap.isOpened():
            self._cap = cv2.VideoCapture(self.video_path)
            self._current_pos = 0

    def __len__(self):
        return len(self.target_indices)

    def __getitem__(self, idx):
        if isinstance(idx, slice):
            return [self[i] for i in range(*idx.indices(len(self)))]

        real_frame_idx = self.target_indices[idx]
        self._ensure_cap()

        if real_frame_idx != self._current_pos:
            if real_frame_idx > self._current_pos and (real_frame_idx - self._current_pos) <= 8:
                while self._current_pos < real_frame_idx:
                    self._cap.grab()
                    self._current_pos += 1
            else:
                self._cap.set(cv2.CAP_PROP_POS_FRAMES, real_frame_idx)
                self._current_pos = real_frame_idx

        ret, frame_bgr = self._cap.read()
        if ret:
            self._current_pos += 1
            frame_rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
            if frame_rgb.shape[1] != self.orig_w or frame_rgb.shape[0] != self.orig_h:
                frame_rgb = cv2.resize(frame_rgb, (self.orig_w, self.orig_h), interpolation=cv2.INTER_LANCZOS4)
            rgb_np = frame_rgb.astype(np.float32) / 255.0
        else:
            rgb_np = np.zeros((self.orig_h, self.orig_w, 3), dtype=np.float32)

        if idx == len(self.target_indices) - 1:
            self.close()

        return torch.from_numpy(rgb_np).unsqueeze(0)

    def __iter__(self):
        for i in range(len(self)):
            yield self[i]

    def close(self):
        if self._cap is not None:
            self._cap.release()
            self._cap = None

    def __del__(self):
        self.close()


class LazySequenceFrameList(list):
    def __init__(self, file_paths: list, orig_w: int, orig_h: int):
        super().__init__()
        self.file_paths = list(file_paths)
        self.orig_w = orig_w
        self.orig_h = orig_h

    def __len__(self):
        return len(self.file_paths)

    def __getitem__(self, idx):
        if isinstance(idx, slice):
            return [self[i] for i in range(*idx.indices(len(self)))]
        img = Image.open(self.file_paths[idx]).convert("RGB")
        if img.size != (self.orig_w, self.orig_h):
            img = img.resize((self.orig_w, self.orig_h), Image.Resampling.LANCZOS)
        rgb_np = np.array(img).astype(np.float32) / 255.0
        return torch.from_numpy(rgb_np).unsqueeze(0)

    def __iter__(self):
        for i in range(len(self)):
            yield self[i]


# ==============================================================================
# AUDIO EXTRACTION & MUXING HELPERS
# ==============================================================================

def extract_comfy_audio_from_video(video_path: str, start_sec: float = 0.0, duration_sec: float = 0.0):
    if not video_path or not os.path.isfile(video_path) or not video_path.lower().endswith(VALID_VIDEO_EXTS):
        return None

    sample_rate = 44100
    channels = 2
    cmd = ["ffmpeg", "-v", "error"]
    if start_sec > 0.0:
        cmd.extend(["-ss", f"{start_sec:.3f}"])
    cmd.extend(["-i", video_path])
    if duration_sec > 0.0:
        cmd.extend(["-t", f"{duration_sec:.3f}"])
    cmd.extend([
        "-vn",
        "-f", "s16le",
        "-acodec", "pcm_s16le",
        "-ar", str(sample_rate),
        "-ac", str(channels),
        "pipe:1"
    ])

    try:
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
        if res.returncode != 0 or len(res.stdout) == 0:
            return None

        pcm_np = np.frombuffer(res.stdout, dtype=np.int16).astype(np.float32) / 32768.0
        num_samples = pcm_np.size // channels
        if num_samples == 0:
            return None

        pcm_np = pcm_np[:num_samples * channels].reshape((num_samples, channels)).T
        waveform = torch.from_numpy(pcm_np.copy()).unsqueeze(0)
        return {"waveform": waveform, "sample_rate": sample_rate}
    except Exception as e:
        print(f"[BlackboxMediaLoader] Audio extraction note: {e}")
        return None


def save_comfy_audio_to_wav(audio_dict: dict, wav_path: str) -> bool:
    if not isinstance(audio_dict, dict) or "waveform" not in audio_dict:
        return False
    waveform = audio_dict["waveform"]
    if waveform is None:
        return False
    sample_rate = int(audio_dict.get("sample_rate", 44100))

    if isinstance(waveform, torch.Tensor):
        wav_np = waveform.detach().cpu().numpy()
    else:
        wav_np = np.array(waveform)

    if wav_np.size == 0:
        return False

    if wav_np.ndim == 3:
        wav_np = wav_np[0]
    elif wav_np.ndim == 1:
        wav_np = wav_np[np.newaxis, :]

    if wav_np.shape[0] <= 8 and wav_np.shape[0] < wav_np.shape[1]:
        wav_np = wav_np.T

    channels = wav_np.shape[1] if wav_np.ndim == 2 else 1
    pcm16 = np.clip(wav_np * 32767.0, -32768, 32767).astype(np.int16)

    with wave.open(wav_path, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm16.tobytes())
    return True


def build_atempo_filter_chain(tempo_factor: float) -> str:
    filters = []
    rem = float(tempo_factor)
    while rem > 2.0:
        filters.append("atempo=2.0")
        rem /= 2.0
    while rem < 0.5:
        filters.append("atempo=0.5")
        rem /= 0.5
    if abs(rem - 1.0) > 0.01:
        filters.append(f"atempo={rem:.4f}")
    return ",".join(filters)


# ==============================================================================
# API ENDPOINT 1: OS DIRECTORY PICKER DIALOG
# ==============================================================================

@PromptServer.instance.routes.get("/blackbox_media/browse_folder")
async def browse_folder_dialog(request):
    initial_dir = request.rel_url.query.get("initial_dir", "").strip().strip('"').strip("'")
    if not initial_dir or not os.path.isdir(initial_dir):
        initial_dir = folder_paths.get_input_directory()

    selected_dir = ""
    try:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        selected_dir = filedialog.askdirectory(initialdir=initial_dir, title="Select Folder")
        root.destroy()
    except Exception:
        if sys.platform.startswith("linux"):
            for cmd in [
                ["zenity", "--file-selection", "--directory", f"--filename={initial_dir}/", "--title=Select Folder"],
                ["kdialog", "--getexistingdirectory", initial_dir, "--title", "Select Folder"]
            ]:
                try:
                    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
                    if res.returncode == 0 and res.stdout.strip():
                        selected_dir = res.stdout.strip()
                        break
                except FileNotFoundError:
                    continue
        elif sys.platform == "darwin":
            try:
                applescript = f'POSIX path of (choose folder with prompt "Select Folder" default location POSIX file "{initial_dir}")'
                res = subprocess.run(["osascript", "-e", applescript], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
                if res.returncode == 0 and res.stdout.strip():
                    selected_dir = res.stdout.strip()
            except Exception:
                pass

    return web.json_response({"selected_dir": selected_dir or ""})


# ==============================================================================
# API ENDPOINT 2: INTERACTIVE FRAME PREVIEW PLAYER
# ==============================================================================
@PromptServer.instance.routes.get("/blackbox_media/load_frames")
async def get_animation_frames_for_player(request):
    query = request.rel_url.query
    filename = query.get("filename", "").strip()
    custom_path = query.get("custom_path", "").strip().strip('"').strip("'")

    def safe_int(val, default):
        try:
            if val is None or val == "":
                return default
            return int(float(val))
        except (ValueError, TypeError):
            return default

    def safe_float(val, default):
        try:
            if val is None or val == "":
                return default
            return float(val)
        except (ValueError, TypeError):
            return default

    start_frame = safe_int(query.get("start_frame"), 0)
    max_frames = safe_int(query.get("max_frames"), 0)
    frame_skip = max(1, safe_int(query.get("frame_skip"), 1))
    seq_fps = max(1.0, safe_float(query.get("sequence_fps"), 12.0))

    if custom_path:
        target_path = custom_path
    elif filename and filename != "none":
        target_path = folder_paths.get_annotated_filepath(filename)
    else:
        return web.json_response({"error": "No media file or folder specified"}, status=400)

    if not target_path or not os.path.exists(target_path):
        missing_name = custom_path if custom_path else filename
        return web.json_response({"error": f"File not found: {missing_name}"}, status=404)

    try:
        max_preview_dim = 640
        max_preview_frames = 250
        frames_b64 = []
        durations = []

        if os.path.isdir(target_path):
            seq_files = list_sequence_files_in_dir(target_path)
            total_available = len(seq_files)
            if total_available == 0:
                return web.json_response({"error": "No supported images found in folder"}, status=400)

            first_img = Image.open(seq_files[0]).convert("RGB")
            orig_w, orig_h = first_img.size
            file_format = "SEQ"

            scale = min(1.0, max_preview_dim / max(orig_w, orig_h))
            prev_w, prev_h = max(1, int(orig_w * scale)), max(1, int(orig_h * scale))

            start_frame = max(0, min(start_frame, total_available - 1))
            end_idx = total_available if max_frames <= 0 else min(total_available, start_frame + max_frames * frame_skip)
            selected_indices = list(range(start_frame, end_idx, frame_skip))

            base_dur = max(10, int(round(1000.0 / seq_fps))) * frame_skip

            for target_idx in selected_indices[:max_preview_frames]:
                img = Image.open(seq_files[target_idx]).convert("RGB")
                if scale < 1.0:
                    img = img.resize((prev_w, prev_h), Image.Resampling.BILINEAR)
                buf = io.BytesIO()
                img.save(buf, format="JPEG", quality=82)
                b64_str = base64.b64encode(buf.getvalue()).decode("utf-8")
                frames_b64.append(f"data:image/jpeg;base64,{b64_str}")
                durations.append(base_dur)

        elif target_path.lower().endswith(VALID_VIDEO_EXTS):
            cap = cv2.VideoCapture(target_path)
            if not cap.isOpened():
                return web.json_response({"error": "Could not open video file"}, status=400)

            orig_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)) or 640
            orig_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)) or 480
            vid_fps = float(cap.get(cv2.CAP_PROP_FPS))
            if vid_fps <= 0 or vid_fps > 240:
                vid_fps = 24.0
            total_available = max(1, int(cap.get(cv2.CAP_PROP_FRAME_COUNT)))
            file_format = "VIDEO"

            scale = min(1.0, max_preview_dim / max(orig_w, orig_h))
            prev_w, prev_h = max(1, int(orig_w * scale)), max(1, int(orig_h * scale))

            start_frame = max(0, min(start_frame, total_available - 1))
            end_idx = total_available if max_frames <= 0 else min(total_available, start_frame + max_frames * frame_skip)
            selected_indices = list(range(start_frame, end_idx, frame_skip))

            base_dur = max(10, int(round(1000.0 / vid_fps))) * frame_skip
            cap.set(cv2.CAP_PROP_POS_FRAMES, start_frame)
            cur_pos = start_frame

            for target_idx in selected_indices[:max_preview_frames]:
                while cur_pos < target_idx:
                    cap.grab()
                    cur_pos += 1
                ret, frame_bgr = cap.read()
                cur_pos += 1
                if not ret:
                    break
                frame_rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
                if scale < 1.0:
                    frame_rgb = cv2.resize(frame_rgb, (prev_w, prev_h), interpolation=cv2.INTER_AREA)
                pil_prev = Image.fromarray(frame_rgb)
                buf = io.BytesIO()
                pil_prev.save(buf, format="JPEG", quality=80)
                b64_str = base64.b64encode(buf.getvalue()).decode("utf-8")
                frames_b64.append(f"data:image/jpeg;base64,{b64_str}")
                durations.append(base_dur)

            cap.release()

        else:
            pil_img = Image.open(target_path)
            orig_w, orig_h = pil_img.size
            ext = os.path.splitext(target_path)[1].lower()
            file_format = "WEBP" if ext == ".webp" else "GIF"

            scale = min(1.0, max_preview_dim / max(orig_w, orig_h))
            prev_w, prev_h = max(1, int(orig_w * scale)), max(1, int(orig_h * scale))

            all_rgb_frames, all_durations = extract_animation_frames_and_durations(pil_img)
            total_available = len(all_rgb_frames)

            if total_available == 0:
                return web.json_response({"error": "Empty animation file"}, status=400)

            start_frame = max(0, min(start_frame, total_available - 1))
            end_idx = total_available if max_frames <= 0 else min(total_available, start_frame + max_frames * frame_skip)
            selected_indices = list(range(start_frame, end_idx, frame_skip))

            for target_idx in selected_indices[:max_preview_frames]:
                preview_img = all_rgb_frames[target_idx]
                if scale < 1.0:
                    preview_img = preview_img.resize((prev_w, prev_h), Image.Resampling.BILINEAR)

                buf = io.BytesIO()
                preview_img.save(buf, format="JPEG", quality=85)
                b64_str = base64.b64encode(buf.getvalue()).decode("utf-8")
                frames_b64.append(f"data:image/jpeg;base64,{b64_str}")
                durations.append(all_durations[target_idx] * frame_skip)

        return web.json_response({
            "width": orig_w,
            "height": orig_h,
            "file_format": file_format,
            "total_available": total_available,
            "start_frame": start_frame,
            "frame_count": len(frames_b64),
            "durations": durations,
            "frames": frames_b64
        })
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


# ==============================================================================
# OPTICAL FLOW & COLOR MATH FUNCTIONS
# ==============================================================================

def match_color_lab(target_rgb: np.ndarray, ref_rgb: np.ndarray, strength: float = 0.5) -> np.ndarray:
    if strength <= 0.0:
        return target_rgb

    target_u8 = np.clip(target_rgb * 255.0, 0, 255).astype(np.uint8)
    ref_u8 = np.clip(ref_rgb * 255.0, 0, 255).astype(np.uint8)

    target_lab = cv2.cvtColor(target_u8, cv2.COLOR_RGB2LAB).astype(np.float32)
    ref_lab = cv2.cvtColor(ref_u8, cv2.COLOR_RGB2LAB).astype(np.float32)

    for c in range(3):
        t_mean, t_std = target_lab[:, :, c].mean(), target_lab[:, :, c].std() + 1e-5
        r_mean, r_std = ref_lab[:, :, c].mean(), ref_lab[:, :, c].std() + 1e-5

        matched = (target_lab[:, :, c] - t_mean) * (r_std / t_std) + r_mean
        target_lab[:, :, c] = target_lab[:, :, c] * (1.0 - strength) + matched * strength

    target_lab = np.clip(target_lab, 0, 255).astype(np.uint8)
    res_rgb = cv2.cvtColor(target_lab, cv2.COLOR_LAB2RGB).astype(np.float32) / 255.0
    return res_rgb


def bilateral_structure_prep(img_np: np.ndarray, d: int = 7, sigma_color: float = 0.08, sigma_space: float = 5.0) -> np.ndarray:
    img_u8 = np.clip(img_np * 255.0, 0, 255).astype(np.uint8)
    filtered = cv2.bilateralFilter(img_u8, d=d, sigmaColor=sigma_color * 255.0, sigmaSpace=sigma_space)
    return filtered.astype(np.float32) / 255.0


def warp_frame(img_tensor: torch.Tensor, flow: torch.Tensor) -> torch.Tensor:
    B, C, H, W = img_tensor.shape
    xx = torch.arange(0, W, device=img_tensor.device).view(1, -1).repeat(H, 1)
    yy = torch.arange(0, H, device=img_tensor.device).view(-1, 1).repeat(1, W)
    grid = torch.stack((xx, yy), dim=0).float().unsqueeze(0)

    vgrid = grid + flow
    vgrid[:, 0, :, :] = 2.0 * vgrid[:, 0, :, :] / max(W - 1, 1) - 1.0
    vgrid[:, 1, :, :] = 2.0 * vgrid[:, 1, :, :] / max(H - 1, 1) - 1.0
    vgrid = vgrid.permute(0, 2, 3, 1)

    return F.grid_sample(img_tensor, vgrid, mode='bilinear', padding_mode='border', align_corners=True)


def compute_raft_flow(model, img1: torch.Tensor, img2: torch.Tensor, max_flow_dim: int = 1024) -> torch.Tensor:
    _, _, H, W = img1.shape
    use_downscale = max(H, W) > max_flow_dim
    if use_downscale:
        scale = max_flow_dim / max(H, W)
        flow_h = max(8, (int(H * scale) // 8) * 8)
        flow_w = max(8, (int(W * scale) // 8) * 8)

        img1_work = F.interpolate(img1, size=(flow_h, flow_w), mode='bilinear', align_corners=False)
        img2_work = F.interpolate(img2, size=(flow_h, flow_w), mode='bilinear', align_corners=False)
    else:
        img1_work, img2_work = img1, img2
        flow_h, flow_w = H, W

    pad_h = (8 - flow_h % 8) % 8
    pad_w = (8 - flow_w % 8) % 8

    if pad_h > 0 or pad_w > 0:
        img1_pad = F.pad(img1_work, (0, pad_w, 0, pad_h), mode='replicate')
        img2_pad = F.pad(img2_work, (0, pad_w, 0, pad_h), mode='replicate')
    else:
        img1_pad, img2_pad = img1_work, img2_work

    device = img1.device
    with torch.no_grad():
        if device.type == "cuda":
            with torch.autocast(device_type="cuda", dtype=torch.float16):
                flow = model(img1_pad, img2_pad)[-1].float()
        else:
            flow = model(img1_pad, img2_pad)[-1].float()

    if pad_h > 0 or pad_w > 0:
        flow = flow[:, :, :flow_h, :flow_w]

    if use_downscale:
        flow = F.interpolate(flow, size=(H, W), mode='bilinear', align_corners=False)
        flow[:, 0, :, :] *= (float(W) / float(flow_w))
        flow[:, 1, :, :] *= (float(H) / float(flow_h))

    return flow


# ==============================================================================
# NODE 1: BLACKBOX MEDIA LOADER
# ==============================================================================

class BlackboxMediaLoader:
    @classmethod
    def INPUT_TYPES(cls):
        input_dir = folder_paths.get_input_directory()
        files = [
            f for f in os.listdir(input_dir)
            if os.path.isfile(os.path.join(input_dir, f)) and f.lower().endswith(ALL_SUPPORTED_EXTS)
        ]
        prio_files = sorted([f for f in files if f.lower().endswith(('.gif', '.webp') + VALID_VIDEO_EXTS)])
        other_files = sorted([f for f in files if f not in prio_files])
        all_files = prio_files + other_files

        return {
            "required": {
                "media_source": (all_files if all_files else ["none"], {
                    "tooltip": "Select a media file (Video, GIF, WebP, or Image) from the ComfyUI input directory."
                }),
                "start_frame": ("INT", {
                    "default": 0, "min": 0, "max": 1000000, "step": 1,
                    "tooltip": "Zero-based starting frame index."
                }),
                "max_frames": ("INT", {
                    "default": 0, "min": 0, "max": 1000000, "step": 1,
                    "tooltip": "Maximum number of frames to process (0 = process all frames until the end)."
                }),
                "frame_skip": ("INT", {
                    "default": 1, "min": 1, "max": 30, "step": 1,
                    "tooltip": "Frame step factor (1 = process every frame, 2 = process every 2nd frame, etc.)."
                }),
                "chunk_size": ("INT", {
                    "default": 50, "min": 0, "max": 1000, "step": 5,
                    "tooltip": "Number of frames emitted per workflow pass (e.g. 50 frames = ~500 MB RAM). Prevents RAM/VRAM OOM inside heavy Blackbox pipelines (such as VAEDecode). Set to 0 to disable chunking."
                }),
                "if_interrupted": ([
                    "Restart from 0 (Safe Clean Start)",
                    "Resume from last committed chunk"
                ], {
                    "default": "Restart from 0 (Safe Clean Start)",
                    "tooltip": "Action to take if a previous chunked run was interrupted by an error or manual cancellation."
                }),
                "sequence_fps": ("FLOAT", {
                    "default": 12.0, "min": 1.0, "max": 120.0, "step": 0.5,
                    "tooltip": "Base frame rate (FPS) assigned when loading an Image Sequence folder."
                }),
            },
            "optional": {
                "custom_path": ("STRING", {
                    "default": "",
                    "multiline": False,
                    "placeholder": "Optional: Direct path to Video/GIF/WebP file OR Image Sequence folder",
                    "tooltip": "Optional override. Accepts an absolute path to a media file or a directory containing an image sequence."
                }),
            }
        }

    RETURN_TYPES = ("IMAGE", "BLACKBOX_STREAM_META", "AUDIO")
    RETURN_NAMES = ("image_list", "stream_meta", "audio")
    OUTPUT_IS_LIST = (True, False, False)
    FUNCTION = "load_media"
    CATEGORY = "Blackbox Media"

    @classmethod
    def VALIDATE_INPUTS(cls, media_source, custom_path="", **kwargs):
        # Returns custom error message if the specified file does not exist when queuing prompt
        if custom_path and str(custom_path).strip():
            p = str(custom_path).strip().strip('"').strip("'")
            if not os.path.exists(p):
                return f"Custom path does not exist: {p}"
            return True
        if not media_source or media_source == "none":
            return "No media_source selected."
        p = folder_paths.get_annotated_filepath(str(media_source))
        if not p or not os.path.exists(p):
            return f"Input media file not found: {media_source}"
        return True

    @classmethod
    def IS_CHANGED(cls, media_source, start_frame, max_frames, frame_skip, chunk_size=50, if_interrupted="Restart from 0 (Safe Clean Start)", sequence_fps=12.0, custom_path=""):
        return f"{time.time()}_{uuid.uuid4().hex[:6]}"

    def load_media(self, media_source, start_frame, max_frames, frame_skip, chunk_size=50, if_interrupted="Restart from 0 (Safe Clean Start)", sequence_fps=12.0, custom_path=""):
        global LAST_ACTIVE_SIGNATURE

        if custom_path and str(custom_path).strip() != "":
            target_path = str(custom_path).strip().strip('"').strip("'")
        else:
            target_path = folder_paths.get_annotated_filepath(str(media_source))

        if not os.path.exists(target_path):
            raise FileNotFoundError(f"[BlackboxMediaLoader] Path not found: {target_path}")

        frame_skip = max(1, int(frame_skip))
        chunk_size = max(0, int(chunk_size))
        sig = f"loader_{target_path}_{start_frame}_{max_frames}_{frame_skip}_{chunk_size}_{sequence_fps}"

        # 1. Inspect media geometry and build full frame index list
        if os.path.isdir(target_path):
            seq_files = list_sequence_files_in_dir(target_path)
            total_available = len(seq_files)
            if total_available == 0:
                raise ValueError(f"[BlackboxMediaLoader] No supported image files found in directory: {target_path}")

            first_img = Image.open(seq_files[0]).convert("RGB")
            orig_w, orig_h = first_img.size
            file_format = "SEQ"
            loop = 0
            orig_fps = float(sequence_fps)

            start_frame = max(0, min(start_frame, total_available - 1))
            end_idx = total_available if max_frames <= 0 else min(total_available, start_frame + max_frames * frame_skip)
            all_selected_files = seq_files[start_frame:end_idx:frame_skip]
            total_target_count = len(all_selected_files)
            base_dur = max(10, int(round(1000.0 / max(1.0, sequence_fps)))) * frame_skip
            all_target_durations = [base_dur] * total_target_count

        elif target_path.lower().endswith(VALID_VIDEO_EXTS):
            cap = cv2.VideoCapture(target_path)
            if not cap.isOpened():
                raise ValueError(f"[BlackboxMediaLoader] Failed to open video file: {target_path}")

            orig_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
            orig_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
            orig_fps = float(cap.get(cv2.CAP_PROP_FPS))
            if orig_fps <= 0 or orig_fps > 240:
                orig_fps = 24.0
            total_available = max(1, int(cap.get(cv2.CAP_PROP_FRAME_COUNT)))
            cap.release()

            ext = os.path.splitext(target_path)[1].lower()
            file_format = "WEBM" if ext == ".webm" else "MP4"
            loop = 0

            start_frame = max(0, min(start_frame, total_available - 1))
            end_idx = total_available if max_frames <= 0 else min(total_available, start_frame + max_frames * frame_skip)
            all_target_indices = list(range(start_frame, end_idx, frame_skip))
            total_target_count = len(all_target_indices)
            base_dur = max(10, int(round(1000.0 / orig_fps))) * frame_skip
            all_target_durations = [base_dur] * total_target_count

        else:
            pil_img = Image.open(target_path)
            orig_w, orig_h = pil_img.size
            ext = os.path.splitext(target_path)[1].lower()
            file_format = "WEBP" if ext == ".webp" else "GIF"
            loop = pil_img.info.get("loop", 0)

            all_rgb_frames, raw_durations = extract_animation_frames_and_durations(pil_img)
            total_available = len(all_rgb_frames)

            start_frame = max(0, min(start_frame, total_available - 1))
            end_idx = total_available if max_frames <= 0 else min(total_available, start_frame + max_frames * frame_skip)
            all_target_indices = list(range(start_frame, end_idx, frame_skip))
            total_target_count = len(all_target_indices)
            all_target_durations = [raw_durations[i] * frame_skip for i in all_target_indices]
            avg_d = float(np.mean(raw_durations)) if raw_durations else 80.0
            orig_fps = round(1000.0 / max(1.0, avg_d), 2)

        # 2. Transactional chunk boundary determination
        is_chunked = (chunk_size > 0) and (total_target_count > chunk_size)

        if not is_chunked:
            session = create_new_chunk_session(sig, total_target_count)
            c_start = 0
            c_end = total_target_count
            session["pending_cursor"] = c_end
            session["chunk_in_flight"] = True
            session["pending_deflicker"] = None
            is_first_chunk = True
            is_last_chunk = True
            chunk_idx = 1
            total_chunks = 1
        else:
            session = CHUNK_STREAM_SESSIONS.get(sig)
            if session is None or session.get("cursor", 0) >= total_target_count:
                session = create_new_chunk_session(sig, total_target_count)
            else:
                if session.get("chunk_in_flight", False):
                    if "Resume" in str(if_interrupted) and session.get("cursor", 0) > 0:
                        print(
                            f"\n[BlackboxMediaLoader] ⚠️ Interrupted run detected! Resuming from last committed frame #{session['cursor']}..."
                        )
                        session["pending_deflicker"] = None
                    else:
                        print(
                            f"\n[BlackboxMediaLoader] 🔄 Previous run was interrupted before commit. "
                            f"Resetting buffer and starting cleanly from frame 0."
                        )
                        session = create_new_chunk_session(sig, total_target_count)

            c_start = session["cursor"]
            c_end = min(total_target_count, c_start + chunk_size)
            session["pending_cursor"] = c_end
            session["chunk_in_flight"] = True
            session["pending_deflicker"] = None
            LAST_ACTIVE_SIGNATURE = sig

            is_first_chunk = (c_start == 0)
            is_last_chunk = (c_end >= total_target_count)
            chunk_idx = (c_start // chunk_size) + 1
            total_chunks = (total_target_count + chunk_size - 1) // chunk_size

        if session.get("cached_probe") is None:
            session["cached_probe"] = probe_media_container_info(target_path)
        probe_info = session["cached_probe"]

        chunk_durations = all_target_durations[c_start:c_end]

        # 3. Emit frames only for the active chunk (c_start .. c_end)
        if os.path.isdir(target_path):
            frames_out = LazySequenceFrameList(all_selected_files[c_start:c_end], orig_w, orig_h)
            extracted_audio = None
        elif target_path.lower().endswith(VALID_VIDEO_EXTS):
            frames_out = LazyVideoFrameList(target_path, all_target_indices[c_start:c_end], orig_w, orig_h)
            if is_first_chunk or session.get("cached_audio") is None:
                start_sec = start_frame / max(1.0, orig_fps)
                span_frames = max(1, (all_target_indices[-1] - start_frame + frame_skip)) if all_target_indices else 0
                duration_sec = span_frames / max(1.0, orig_fps)
                session["cached_audio"] = extract_comfy_audio_from_video(target_path, start_sec=start_sec, duration_sec=duration_sec)
            extracted_audio = session.get("cached_audio")
        else:
            frames_out = []
            for target_idx in all_target_indices[c_start:c_end]:
                rgb_img = all_rgb_frames[target_idx]
                rgb_np = np.array(rgb_img).astype(np.float32) / 255.0
                frames_out.append(torch.from_numpy(rgb_np).unsqueeze(0))
            extracted_audio = None

        avg_dur = float(np.mean(all_target_durations)) if all_target_durations else 80.0
        avg_fps = round(1000.0 / max(1.0, avg_dur), 2)

        stream_meta = {
            "durations": chunk_durations,
            "all_durations": all_target_durations,
            "loop": loop,
            "source_path": target_path,
            "source_format": file_format,
            "orig_width": orig_w,
            "orig_height": orig_h,
            "orig_file_fps": orig_fps,
            "total_available": total_available,
            "frame_count": total_target_count,
            "start_frame": start_frame,
            "frame_skip": frame_skip,
            "chunk_size": chunk_size,
            "avg_fps": avg_fps,
            "has_embedded_audio": extracted_audio is not None,
            "probe_info": probe_info,
            "session_signature": sig,
            "is_chunked": is_chunked,
            "is_first_chunk": is_first_chunk,
            "is_last_chunk": is_last_chunk,
            "chunk_index": chunk_idx,
            "total_chunks": total_chunks,
            "chunk_start": c_start,
            "chunk_end": c_end,
        }

        print(
            f"[BlackboxMediaLoader] Starting chunk {chunk_idx}/{total_chunks} "
            f"(frames {c_start}..{c_end - 1} of {total_target_count}) | "
            f"{file_format} {orig_w}x{orig_h} @ ~{avg_fps} FPS"
        )
        return (frames_out, stream_meta, extracted_audio)


# ==============================================================================
# NODE 2: BLACKBOX MEDIA INSPECTOR (DEEP TELEMETRY & UTILITY OUTPUTS)
# ==============================================================================

class BlackboxMediaInspector:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "stream_meta": ("BLACKBOX_STREAM_META", {
                    "tooltip": "Connect stream_meta from Blackbox Media Loader to inspect stream telemetry and extract geometry parameters."
                }),
            }
        }

    RETURN_TYPES = ("BLACKBOX_STREAM_META", "INT", "INT", "INT", "INT", "INT", "INT", "FLOAT", "STRING")
    RETURN_NAMES = (
        "stream_meta",
        "width",
        "height",
        "vae_safe_width",
        "vae_safe_height",
        "chunk_frames",
        "total_frames",
        "fps",
        "telemetry_report"
    )
    OUTPUT_NODE = True
    FUNCTION = "inspect_stream"
    CATEGORY = "Blackbox Media"

    def inspect_stream(self, stream_meta):
        meta = dict(stream_meta)
        all_durations = list(meta.get("all_durations", meta.get("durations", [80])))
        total_frames = int(meta.get("frame_count", len(all_durations)))
        c_start = int(meta.get("chunk_start", 0))
        c_end = int(meta.get("chunk_end", total_frames))
        chunk_frames = max(1, c_end - c_start)

        total_sec = round(sum(all_durations) / 1000.0, 2)
        orig_w = max(1, int(meta.get("orig_width", 512)))
        orig_h = max(1, int(meta.get("orig_height", 512)))
        avg_fps = float(meta.get("avg_fps", 12.5))
        fmt = meta.get("source_format", "GIF")
        probe = meta.get("probe_info", {}) or {}

        # Calculate VAE/DiT safe dimensions (multiples of 16)
        vae_w = max(16, (orig_w // 16) * 16)
        vae_h = max(16, (orig_h // 16) * 16)

        # Aspect ratio calculation
        common_div = math.gcd(orig_w, orig_h)
        ar_str = f"{orig_w // common_div}:{orig_h // common_div} ({orig_w / orig_h:.2f}:1)"

        # Resolution alignment check (64 / 16 / 2)
        if orig_w % 64 == 0 and orig_h % 64 == 0:
            align_status = "✅ Divisible by 64 (Ideal for DiT / Flux / Wan / SDXL)"
        elif orig_w % 16 == 0 and orig_h % 16 == 0:
            align_status = "✅ Divisible by 16 (VAE-Safe)"
        elif orig_w % 8 == 0 and orig_h % 8 == 0:
            align_status = f"ℹ️ Divisible by 8 (Recommend {vae_w}×{vae_h} for DiT/Flux)"
        elif orig_w % 2 == 0 and orig_h % 2 == 0:
            align_status = f"⚠️ Not divisible by 8/16! (Use vae_safe: {vae_w}×{vae_h})"
        else:
            align_status = f"⚠️ ODD dimensions ({orig_w}×{orig_h})! Saver will auto-pad to even; use {vae_w}×{vae_h} for VAE"

        # Timing analysis (CFR vs VFR)
        min_d, max_d = (min(all_durations), max(all_durations)) if all_durations else (80, 80)
        if max_d - min_d <= 2:
            timing_str = f"Constant (CFR ~{avg_fps} FPS, {min_d}ms/frame)"
        else:
            timing_str = f"Variable (VFR {min_d}..{max_d}ms, avg ~{avg_fps} FPS)"

        # Audio telemetry
        has_aud = meta.get("has_embedded_audio", False)
        aud_codec = probe.get("audio_codec", "none")
        if has_aud or aud_codec != "none":
            sr = probe.get("audio_sample_rate") or 44100
            ch = probe.get("audio_channels") or 2
            audio_line = f"Present ({aud_codec.upper()}, {sr} Hz, {ch} ch)"
        else:
            audio_line = "None (Silent stream — connect external audio to Saver to dub)"

        # Memory & Disk Swap Forecast
        frame_float32_mb = (orig_w * orig_h * 3 * 4) / (1024 * 1024)
        frame_uint8_mb = (orig_w * orig_h * 3) / (1024 * 1024)
        chunk_ram_mb = round(frame_float32_mb * chunk_frames, 1)
        full_ram_gb = round((frame_float32_mb * total_frames) / 1024.0, 2)
        disk_swap_mb = round(frame_uint8_mb * total_frames, 1)

        v_codec = probe.get("video_codec", fmt.lower())
        fsize_mb = probe.get("file_size_mb", 0.0)
        br_kbps = probe.get("bitrate_kbps", 0)
        br_str = f" | Bitrate: {br_kbps} kbps" if br_kbps > 0 else ""

        c_idx = meta.get("chunk_index", 1)
        c_tot = meta.get("total_chunks", 1)

        report_lines = [
            f"=== SOURCE MEDIA TELEMETRY ===",
            f"File / Folder : {os.path.basename(meta.get('source_path', 'Unknown'))} ({fsize_mb} MB)",
            f"Container     : {fmt} | Codec: {v_codec}{br_str}",
            f"Geometry      : {orig_w}×{orig_h} px | Aspect: {ar_str}",
            f"VAE Alignment : {align_status}",
            f"Timing Mode   : {timing_str} | Total Duration: {total_sec}s",
            f"Audio Stream  : {audio_line}",
            f"=== STREAM & MEMORY FORECAST ===",
            f"Frame Slicing : {total_frames} of {meta.get('total_available', total_frames)} frames (start: #{meta.get('start_frame', 0)}, skip: {meta.get('frame_skip', 1)})",
            f"Active Chunk  : Chunk {c_idx}/{c_tot} ({chunk_frames} frames: #{c_start}..#{c_end - 1})",
            f"RAM per Chunk : ~{chunk_ram_mb} MB (vs ~{full_ram_gb} GB unchunked float32)",
            f"Temp Disk Buf : ~{disk_swap_mb} MB uint8 swap in ComfyUI/temp"
        ]
        report_text = "\n".join(report_lines)

        return {
            "ui": {"summary_text": [report_text]},
            "result": (
                meta,
                orig_w,
                orig_h,
                vae_w,
                vae_h,
                chunk_frames,
                total_frames,
                avg_fps,
                report_text
            )
        }


# ==============================================================================
# NODE 3: BLACKBOX OPTICAL FLOW DEFLICKER
# ==============================================================================

class BlackboxOpticalFlowDeflicker:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "images": ("IMAGE", {
                    "tooltip": "Processed frames coming out of your Blackbox pipeline."
                }),
                "raft_model": (["raft_large (High Precision)", "raft_small (Fast)"], {
                    "default": "raft_large (High Precision)",
                    "tooltip": "Select the torchvision RAFT optical flow model variant."
                }),
                "temporal_blend": ("FLOAT", {
                    "default": 0.65, "min": 0.0, "max": 0.95, "step": 0.05,
                    "tooltip": "Strength of bidirectional optical-flow temporal smoothing across neighboring frames (0.0 = bypass RAFT)."
                }),
                "window_radius": ("INT", {
                    "default": 1, "min": 1, "max": 3, "step": 1,
                    "tooltip": "Temporal sliding window radius (1 = compare [t-1..t+1], 2 = compare [t-2..t+2])."
                }),
                "color_stabilization": ("FLOAT", {
                    "default": 0.50, "min": 0.0, "max": 1.0, "step": 0.05,
                    "tooltip": "Strength of rolling LAB histogram stabilization to eliminate brightness and color pumping."
                }),
                "occlusion_sensitivity": ("FLOAT", {
                    "default": 1.5, "min": 0.2, "max": 10.0, "step": 0.1,
                    "tooltip": "Forward-backward flow consistency threshold for ghosting protection (lower = stricter ghost rejection)."
                }),
                "seamless_loop": ("BOOLEAN", {
                    "default": True,
                    "tooltip": "Blend final frames with initial frames to create a seamless loop transition."
                }),
            },
            "optional": {
                "stream_meta": ("BLACKBOX_STREAM_META", {
                    "tooltip": "Optional: Connect stream_meta to preserve LAB color reference and sliding window continuity across chunk boundaries."
                }),
            }
        }

    INPUT_IS_LIST = True
    RETURN_TYPES = ("IMAGE",)
    RETURN_NAMES = ("stabilized_images",)
    OUTPUT_IS_LIST = (True,)
    FUNCTION = "deflicker"
    CATEGORY = "Blackbox Media"

    def deflicker(
        self,
        images,
        raft_model,
        temporal_blend,
        window_radius,
        color_stabilization,
        occlusion_sensitivity,
        seamless_loop,
        stream_meta=None
    ):
        raft_choice = raft_model[0]
        blend_strength = float(temporal_blend[0])
        radius = int(window_radius[0])
        color_stab = float(color_stabilization[0])
        occ_thresh = float(occlusion_sensitivity[0])
        is_loop = bool(seamless_loop[0])

        meta = None
        if stream_meta is not None:
            meta = stream_meta[0] if isinstance(stream_meta, list) and len(stream_meta) > 0 else stream_meta

        session = get_session_for_node(meta)

        if isinstance(meta, dict) and "is_chunked" in meta:
            is_chunked = bool(meta.get("is_chunked", False))
            is_first_chunk = bool(meta.get("is_first_chunk", True))
            is_last_chunk = bool(meta.get("is_last_chunk", True))
        elif session is not None and session.get("total_frames", 0) > 0:
            c_start = session.get("cursor", 0)
            c_end = session.get("pending_cursor", 0)
            tot = session.get("total_frames", 0)
            is_chunked = (c_end - c_start) < tot
            is_first_chunk = (c_start == 0)
            is_last_chunk = (c_end >= tot)
        else:
            is_chunked = False
            is_first_chunk = True
            is_last_chunk = True

        raw_frames = []
        target_h, target_w = None, None
        for item in images:
            for b in range(item.shape[0]):
                f_np = item[b].cpu().numpy().astype(np.float32)
                if target_h is None:
                    target_h, target_w = f_np.shape[0], f_np.shape[1]
                elif f_np.shape[0] != target_h or f_np.shape[1] != target_w:
                    f_np = cv2.resize(f_np, (target_w, target_h), interpolation=cv2.INTER_LANCZOS4)
                raw_frames.append(f_np)

        N_cur = len(raw_frames)
        if N_cur == 0:
            raise ValueError("[BlackboxOpticalFlowDeflicker] No input frames received!")

        # 1. Rolling LAB Color Stabilization (reads only committed ref_frame)
        color_locked_cur = []
        if session is not None and not is_first_chunk and session.get("ref_frame") is not None:
            ref_frame = session["ref_frame"].copy()
            if ref_frame.shape[0] != target_h or ref_frame.shape[1] != target_w:
                ref_frame = cv2.resize(ref_frame, (target_w, target_h), interpolation=cv2.INTER_LANCZOS4)
        else:
            ref_frame = raw_frames[0].copy()

        for i in range(N_cur):
            if (i > 0 or not is_first_chunk) and color_stab > 0:
                matched = match_color_lab(raw_frames[i], ref_frame, strength=color_stab)
                ref_frame = ref_frame * 0.8 + matched * 0.2
            else:
                matched = raw_frames[i].copy()
            color_locked_cur.append(matched)

        # 2. Cross-Chunk Sliding Window Preparation (staged in pending_deflicker until Saver commits)
        if not is_chunked or session is None or blend_strength <= 0.0:
            work_frames = color_locked_cur
            emit_start = 0
            emit_end = len(work_frames)
            loop_single = is_loop
            if session is not None:
                session["pending_deflicker"] = {
                    "ref_frame": ref_frame.copy(),
                    "first_frames": [],
                    "tail_frames": []
                }
        else:
            loop_single = False
            committed_first = [f.copy() for f in color_locked_cur[:radius]] if is_first_chunk else session.get("first_frames", [])
            prev_tail = [] if is_first_chunk else session.get("tail_frames", [])

            work_frames = prev_tail + color_locked_cur

            appended_loop_count = 0
            if is_last_chunk and is_loop and committed_first:
                work_frames = work_frames + committed_first
                appended_loop_count = len(committed_first)

            emit_start = 0 if is_first_chunk else min(radius, len(prev_tail))
            if is_last_chunk:
                emit_end = len(work_frames) - appended_loop_count
                next_tail = []
            else:
                emit_end = max(emit_start, len(work_frames) - radius)
                next_tail = [f.copy() for f in work_frames[max(0, len(work_frames) - 2 * radius):]]

            session["pending_deflicker"] = {
                "ref_frame": ref_frame.copy(),
                "first_frames": committed_first,
                "tail_frames": next_tail
            }

        W_len = len(work_frames)
        emit_indices = list(range(emit_start, emit_end))

        if W_len <= 1 or blend_strength <= 0.0:
            out_tensors = [torch.from_numpy(work_frames[idx]).unsqueeze(0) for idx in emit_indices]
            return (out_tensors,)

        device = mm.get_torch_device()
        use_pin = (device.type == "cuda")

        struct_cpu_tensors = []
        detail_cpu_tensors = []
        for f_np in work_frames:
            s_np = bilateral_structure_prep(f_np)
            s_t = torch.from_numpy(s_np).permute(2, 0, 1).unsqueeze(0) * 2.0 - 1.0
            d_t = torch.from_numpy(f_np).permute(2, 0, 1).unsqueeze(0)
            if use_pin:
                s_t = s_t.pin_memory()
                d_t = d_t.pin_memory()
            struct_cpu_tensors.append(s_t)
            detail_cpu_tensors.append(d_t)

        if "small" in raft_choice:
            weights = Raft_Small_Weights.DEFAULT
            flow_net = raft_small(weights=weights, progress=True).to(device).eval()
        else:
            weights = Raft_Large_Weights.DEFAULT
            flow_net = raft_large(weights=weights, progress=True).to(device).eval()

        gpu_cache = {}

        def get_gpu_pair(idx):
            if idx not in gpu_cache:
                gpu_cache[idx] = (
                    struct_cpu_tensors[idx].to(device, non_blocking=True),
                    detail_cpu_tensors[idx].to(device, non_blocking=True)
                )
            return gpu_cache[idx]

        pbar_ui = comfy.utils.ProgressBar(len(emit_indices))
        out_tensors = []

        for step_i, t in enumerate(tqdm(emit_indices, desc="[BlackboxDeflicker] RAFT Optical Flow", unit="frame")):
            active_indices = {t}
            for dt in range(-radius, radius + 1):
                if dt == 0:
                    continue
                n_idx = (t + dt) % W_len if loop_single else (t + dt)
                if 0 <= n_idx < W_len:
                    active_indices.add(n_idx)

            for cached_idx in list(gpu_cache.keys()):
                if cached_idx not in active_indices:
                    del gpu_cache[cached_idx]

            struct_t_gpu, detail_t_gpu = get_gpu_pair(t)
            accum_frame = detail_t_gpu.clone()
            total_weight = torch.ones((1, 1, target_h, target_w), device=device)

            for dt in range(-radius, radius + 1):
                if dt == 0:
                    continue
                neighbor_idx = (t + dt) % W_len if loop_single else (t + dt)
                if neighbor_idx < 0 or neighbor_idx >= W_len:
                    continue

                struct_n_gpu, detail_n_gpu = get_gpu_pair(neighbor_idx)

                flow_t_to_n = compute_raft_flow(flow_net, struct_t_gpu, struct_n_gpu)
                flow_n_to_t = compute_raft_flow(flow_net, struct_n_gpu, struct_t_gpu)

                warped_back_flow = warp_frame(flow_n_to_t, flow_t_to_n)
                fb_error = torch.norm(flow_t_to_n + warped_back_flow, dim=1, keepdim=True)
                occ_mask = torch.exp(- (fb_error ** 2) / (2.0 * (occ_thresh ** 2)))

                warped_neighbor_detail = warp_frame(detail_n_gpu, flow_t_to_n)
                photometric_diff = torch.mean(torch.abs(detail_t_gpu - warped_neighbor_detail), dim=1, keepdim=True)
                photo_mask = torch.exp(- (photometric_diff ** 2) / (2.0 * (0.15 ** 2)))

                dist_decay = 1.0 / abs(dt)
                weight = blend_strength * dist_decay * occ_mask * photo_mask

                accum_frame += warped_neighbor_detail * weight
                total_weight += weight

                del flow_t_to_n, flow_n_to_t, warped_back_flow, warped_neighbor_detail

            final_t = np.clip((accum_frame / total_weight).squeeze(0).permute(1, 2, 0).cpu().numpy(), 0.0, 1.0)
            out_tensors.append(torch.from_numpy(final_t).unsqueeze(0))

            del accum_frame, total_weight
            pbar_ui.update_absolute(step_i + 1, len(emit_indices))

        gpu_cache.clear()
        del flow_net, struct_cpu_tensors, detail_cpu_tensors, raw_frames, work_frames
        gc.collect()
        mm.soft_empty_cache()

        return (out_tensors,)


# ==============================================================================
# NODE 4: BLACKBOX MEDIA SAVER & MUXER
# ==============================================================================

class BlackboxMediaSaver:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "images": ("IMAGE", {
                    "tooltip": "Stabilized or processed frames to encode."
                }),
                "output_format": ([
                    "Auto (Match Source Format)",
                    "MP4 (H.264 + AAC Audio)",
                    "WebM (VP9 + Opus Audio)",
                    "WebP (TrueColor 24-bit Animation)",
                    "GIF (256-color Master Palette)"
                ], {
                    "default": "Auto (Match Source Format)",
                    "tooltip": "Target container format. Auto matches the source media type (Image Sequences default to 24-bit WebP)."
                }),
                "include_audio": ("BOOLEAN", {
                    "default": True,
                    "tooltip": "Mux audio stream into MP4/WebM output (uses external audio input if connected, otherwise original video audio). Ignored for GIF/WebP."
                }),
                "speed_multiplier": ("FLOAT", {
                    "default": 1.00, "min": 0.10, "max": 5.00, "step": 0.05,
                    "tooltip": "Playback speed multiplier (1.0 = original speed, 2.0 = 2x faster). Automatically synchronizes audio tempo."
                }),
                "override_fps": ("FLOAT", {
                    "default": 0.0, "min": 0.0, "max": 120.0, "step": 0.5,
                    "tooltip": "0.0 = keep source timing. Otherwise enforce a constant output frame rate (FPS)."
                }),
                "first_frame_hold_ms": ("INT", {
                    "default": 0, "min": 0, "max": 10000, "step": 50,
                    "tooltip": "Additional hold delay on the very first frame (in milliseconds)."
                }),
                "last_frame_hold_ms": ("INT", {
                    "default": 0, "min": 0, "max": 10000, "step": 50,
                    "tooltip": "Additional hold delay on the last frame before loop restart (in milliseconds)."
                }),
                "playback_mode": ([
                    "Normal (Forward)",
                    "Reverse (Backward)",
                    "Ping-Pong (Boomerang)"
                ], {
                    "default": "Normal (Forward)",
                    "tooltip": "Frame playback direction."
                }),
                "loop_count": ("INT", {
                    "default": 0, "min": 0, "max": 100, "step": 1,
                    "tooltip": "Loop count for GIF/WebP animations (0 = infinite loop)."
                }),
                "video_crf": ("INT", {
                    "default": 18, "min": 0, "max": 51, "step": 1,
                    "tooltip": "Constant Rate Factor for MP4/WebM encoding (lower = higher quality; 18 is visually lossless)."
                }),
                "max_colors": ("INT", {
                    "default": 256, "min": 16, "max": 256, "step": 16,
                    "tooltip": "Maximum color palette size for GIF export."
                }),
                "dither_mode": ([
                    "None (Clean Solid Colors)",
                    "Floyd-Steinberg (Smooth Gradients)"
                ], {
                    "default": "None (Clean Solid Colors)",
                    "tooltip": "Dithering algorithm used during 256-color GIF quantization."
                }),
                "webp_quality": ("INT", {
                    "default": 90, "min": 1, "max": 100, "step": 1,
                    "tooltip": "Compression quality for animated WebP (1-100)."
                }),
                "webp_lossless": ("BOOLEAN", {
                    "default": False,
                    "tooltip": "Use lossless compression for animated WebP."
                }),
                "output_scale": ("FLOAT", {
                    "default": 1.00, "min": 0.10, "max": 4.00, "step": 0.05,
                    "tooltip": "Final output resolution scaling factor (1.0 = original size)."
                }),
                "output_directory": ("STRING", {
                    "default": "",
                    "multiline": False,
                    "placeholder": "Empty = ComfyUI output directory",
                    "tooltip": "Custom directory path for saving the output file (or use the Browse button below)."
                }),
                "filename_prefix": ("STRING", {
                    "default": "Blackbox_Media",
                    "multiline": False,
                    "tooltip": "Base filename prefix for the exported media file."
                }),
                "overwrite_existing": ("BOOLEAN", {
                    "default": False,
                    "tooltip": "If enabled, saves directly as <filename_prefix>.<ext> without appending a timestamp."
                }),
            },
            "optional": {
                "stream_meta": ("BLACKBOX_STREAM_META", {
                    "tooltip": "Connect stream_meta from Loader or Inspector."
                }),
                "audio": ("AUDIO", {
                    "tooltip": "Optional audio track to mux into MP4/WebM (connect from Blackbox Media Loader or any external ComfyUI audio node to dub GIF/WebP/Sequences)."
                }),
            }
        }

    INPUT_IS_LIST = True
    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("saved_path",)
    OUTPUT_NODE = True
    FUNCTION = "save_media"
    CATEGORY = "Blackbox Media"

    def save_media(
        self,
        images,
        output_format,
        include_audio,
        speed_multiplier,
        override_fps,
        first_frame_hold_ms,
        last_frame_hold_ms,
        playback_mode,
        loop_count,
        video_crf,
        max_colors,
        dither_mode,
        webp_quality,
        webp_lossless,
        output_scale,
        output_directory,
        filename_prefix,
        overwrite_existing,
        stream_meta=None,
        audio=None
    ):
        def unbox(val, default=None):
            if val is None:
                return default
            return val[0] if isinstance(val, list) and len(val) > 0 else val

        output_format = str(unbox(output_format, "Auto (Match Source Format)"))
        include_audio = bool(unbox(include_audio, True))
        speed_multiplier = float(unbox(speed_multiplier, 1.0))
        override_fps = float(unbox(override_fps, 0.0))
        first_frame_hold_ms = int(unbox(first_frame_hold_ms, 0))
        last_frame_hold_ms = int(unbox(last_frame_hold_ms, 0))
        playback_mode = str(unbox(playback_mode, "Normal (Forward)"))
        loop_count = int(unbox(loop_count, 0))
        video_crf = int(unbox(video_crf, 18))
        max_colors = int(unbox(max_colors, 256))
        dither_mode = str(unbox(dither_mode, "None (Clean Solid Colors)"))
        webp_quality = int(unbox(webp_quality, 90))
        webp_lossless = bool(unbox(webp_lossless, False))
        output_scale = float(unbox(output_scale, 1.0))
        output_directory = str(unbox(output_directory, ""))
        filename_prefix = str(unbox(filename_prefix, "Blackbox_Media"))
        overwrite_existing = bool(unbox(overwrite_existing, False))

        meta = unbox(stream_meta, {}) or {}
        ext_audio = unbox(audio, None)

        session = get_session_for_node(meta)
        if session is None:
            session = create_new_chunk_session("fallback_direct", 0)

        if "is_chunked" in meta:
            is_chunked = bool(meta.get("is_chunked", False))
            is_last_chunk = bool(meta.get("is_last_chunk", True))
            chunk_idx = int(meta.get("chunk_index", 1))
            total_chunks = int(meta.get("total_chunks", 1))
            total_expected = int(meta.get("frame_count", session.get("total_frames", 0)))
        else:
            c_start = session.get("cursor", 0)
            c_end = session.get("pending_cursor", 0)
            total_expected = session.get("total_frames", 0)
            c_size = max(1, c_end - c_start)
            is_chunked = (total_expected > 0) and (c_size < total_expected)
            is_last_chunk = (c_end >= total_expected)
            chunk_idx = (c_start // c_size) + 1 if is_chunked else 1
            total_chunks = (total_expected + c_size - 1) // c_size if is_chunked else 1

        # 1. Stage chunk frames to disk
        new_chunk_files = []
        base_idx = len(session["saved_files"])

        for item in images:
            for b in range(item.shape[0]):
                f_np = item[b].cpu().numpy().astype(np.float32)
                img_u8 = np.clip(f_np * 255.0, 0, 255).astype(np.uint8)
                if abs(output_scale - 1.0) > 1e-3:
                    new_w = max(8, int(round(img_u8.shape[1] * output_scale)))
                    new_h = max(8, int(round(img_u8.shape[0] * output_scale)))
                    img_u8 = cv2.resize(img_u8, (new_w, new_h), interpolation=cv2.INTER_LANCZOS4)

                f_idx = base_idx + len(new_chunk_files)
                fpath = os.path.join(session["temp_dir"], f"frame_{f_idx:06d}.npy")
                np.save(fpath, img_u8)
                new_chunk_files.append(fpath)

        # 2. ATOMIC TWO-PHASE COMMIT
        session["saved_files"].extend(new_chunk_files)
        if session.get("pending_cursor", 0) > session.get("cursor", 0):
            session["cursor"] = session["pending_cursor"]
        session["chunk_in_flight"] = False
        session["completed_chunks"] = session.get("completed_chunks", 0) + 1

        pending_def = session.get("pending_deflicker")
        if pending_def is not None:
            session["ref_frame"] = pending_def.get("ref_frame")
            session["first_frames"] = pending_def.get("first_frames", [])
            session["tail_frames"] = pending_def.get("tail_frames", [])
            session["pending_deflicker"] = None

        if is_chunked and not is_last_chunk:
            del images
            gc.collect()
            mm.soft_empty_cache()

            elapsed = max(0.1, time.time() - session.get("start_timestamp", time.time()))
            done_c = max(1, session.get("completed_chunks", chunk_idx))
            rem_chunks = max(0, total_chunks - chunk_idx)
            eta_sec = (elapsed / done_c) * rem_chunks
            eta_str = format_eta(eta_sec)

            status_msg = (
                f"⏳ Chunk {chunk_idx}/{total_chunks} committed "
                f"({session['cursor']}/{total_expected} frames) | ETA: ~{eta_str}"
            )
            print(f"[BlackboxMediaSaver] {status_msg}")
            return {
                "ui": {
                    "has_more_chunks": [True],
                    "chunk_status": [status_msg]
                },
                "result": ("",)
            }

        # ======================================================================
        # FINAL ASSEMBLY & MUXING (ALL CHUNKS COMMITTED)
        # ======================================================================
        saved_files = session["saved_files"]
        N = len(saved_files)
        if N == 0:
            destroy_all_sessions()
            raise ValueError("[BlackboxMediaSaver] No frames available for final assembly!")

        base_durations = list(meta.get("all_durations", meta.get("durations", [80] * N)))
        if len(base_durations) < N:
            base_durations = (base_durations * ((N // len(base_durations)) + 1))[:N]
        elif len(base_durations) > N:
            base_durations = base_durations[:N]

        if override_fps > 0.0:
            base_dur = max(10, int(round(1000.0 / override_fps)))
            durations = [base_dur] * N
        else:
            durations = [max(10, int(round(d / max(0.05, speed_multiplier)))) for d in base_durations]

        if first_frame_hold_ms > 0:
            durations[0] += int(first_frame_hold_ms)
        if last_frame_hold_ms > 0:
            durations[-1] += int(last_frame_hold_ms)

        frame_order = list(range(N))
        if "Reverse" in playback_mode:
            frame_order = frame_order[::-1]
            durations = durations[::-1]
        elif "Ping-Pong" in playback_mode and N > 2:
            frame_order = frame_order + frame_order[-2:0:-1]
            durations = durations + durations[-2:0:-1]

        total_frames = len(frame_order)
        avg_dur_ms = float(np.mean(durations)) if durations else 80.0
        effective_fps = 1000.0 / max(1.0, avg_dur_ms)

        source_fmt = meta.get("source_format", "GIF")
        if "MP4" in output_format:
            target_ext = ".mp4"
        elif "WebM" in output_format:
            target_ext = ".webm"
        elif "WebP" in output_format and not output_format.startswith("Auto"):
            target_ext = ".webp"
        elif "GIF" in output_format:
            target_ext = ".gif"
        else:
            if source_fmt == "MP4":
                target_ext = ".mp4"
            elif source_fmt == "WEBM":
                target_ext = ".webm"
            elif source_fmt in ("WEBP", "SEQ"):
                target_ext = ".webp"
            else:
                target_ext = ".gif"

        out_dir_str = output_directory.strip().strip('"').strip("'")
        prefix = filename_prefix.strip() or "Blackbox_Media"
        for ext in [".gif", ".webp", ".mp4", ".webm"]:
            if prefix.lower().endswith(ext):
                prefix = prefix[:-len(ext)]

        timestamp = int(time.time() * 1000) % 1000000
        out_filename = f"{prefix}{target_ext}" if overwrite_existing else f"{prefix}_{timestamp}{target_ext}"

        if out_dir_str:
            if out_dir_str.lower().endswith(('.gif', '.webp', '.mp4', '.webm')):
                final_save_path = out_dir_str
                parent_dir = os.path.dirname(final_save_path)
                if parent_dir:
                    os.makedirs(parent_dir, exist_ok=True)
            else:
                os.makedirs(out_dir_str, exist_ok=True)
                final_save_path = os.path.join(out_dir_str, out_filename)
        else:
            final_save_path = os.path.join(folder_paths.get_output_directory(), out_filename)

        def load_buffered_u8(order_idx: int) -> np.ndarray:
            raw_idx = frame_order[order_idx]
            return np.load(saved_files[raw_idx])

        pbar_ui = comfy.utils.ProgressBar(total_frames + 1)

        try:
            if target_ext in (".mp4", ".webm"):
                first_u8 = load_buffered_u8(0)
                vid_h, vid_w = first_u8.shape[0], first_u8.shape[1]
                even_w = vid_w + (vid_w % 2)
                even_h = vid_h + (vid_h % 2)

                temp_wav_path = None
                audio_input_args = []
                audio_map_args = ["-an"]

                if include_audio:
                    orig_file_fps = float(meta.get("orig_file_fps", 24.0))
                    orig_avg_fps = float(meta.get("avg_fps", orig_file_fps))
                    tempo_ratio = effective_fps / max(0.1, orig_avg_fps)

                    af_chain = []
                    if "Reverse" in playback_mode:
                        af_chain.append("areverse")
                    if abs(tempo_ratio - 1.0) > 0.02:
                        atempo_str = build_atempo_filter_chain(tempo_ratio)
                        if atempo_str:
                            af_chain.append(atempo_str)

                    if ext_audio is not None:
                        temp_wav_path = os.path.join(folder_paths.get_temp_directory(), f"temp_ext_audio_{uuid.uuid4().hex[:8]}.wav")
                        if save_comfy_audio_to_wav(ext_audio, temp_wav_path):
                            audio_input_args = ["-i", temp_wav_path]
                            audio_map_args = ["-map", "0:v:0", "-map", "1:a:0?", "-shortest"]
                            if af_chain:
                                audio_map_args.extend(["-af", ",".join(af_chain)])
                    elif meta.get("source_path") and str(meta.get("source_path")).lower().endswith(VALID_VIDEO_EXTS):
                        src_video = meta["source_path"]
                        if os.path.exists(src_video):
                            start_fr = int(meta.get("start_frame", 0))
                            start_sec = start_fr / max(1.0, orig_file_fps)
                            audio_input_args = ["-ss", f"{start_sec:.3f}", "-i", src_video]
                            audio_map_args = ["-map", "0:v:0", "-map", "1:a:0?", "-shortest"]
                            if af_chain:
                                audio_map_args.extend(["-af", ",".join(af_chain)])

                if target_ext == ".mp4":
                    codec_args = [
                        "-c:v", "libx264",
                        "-crf", str(video_crf),
                        "-preset", "fast",
                        "-pix_fmt", "yuv420p",
                        "-movflags", "+faststart"
                    ]
                    if "-an" not in audio_map_args:
                        codec_args.extend(["-c:a", "aac", "-b:a", "192k"])
                else:
                    codec_args = [
                        "-c:v", "libvpx-vp9",
                        "-crf", str(video_crf),
                        "-b:v", "0",
                        "-row-mt", "1",
                        "-pix_fmt", "yuv420p"
                    ]
                    if "-an" not in audio_map_args:
                        codec_args.extend(["-c:a", "libopus", "-b:a", "160k"])

                ffmpeg_cmd = [
                    "ffmpeg", "-y",
                    "-f", "rawvideo",
                    "-vcodec", "rawvideo",
                    "-s", f"{even_w}x{even_h}",
                    "-pix_fmt", "rgb24",
                    "-r", f"{effective_fps:.3f}",
                    "-i", "pipe:0",
                    *audio_input_args,
                    *audio_map_args,
                    *codec_args,
                    final_save_path
                ]

                print(f"[BlackboxMediaSaver] Encoding {total_frames} frames to {target_ext.upper()} (~{effective_fps:.2f} FPS)...")
                proc = subprocess.Popen(ffmpeg_cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)

                try:
                    for idx in tqdm(range(total_frames), desc=f"[BlackboxSaver] Muxing {target_ext.upper()}", unit="frame"):
                        frame_u8 = load_buffered_u8(idx)
                        if frame_u8.shape[1] != even_w or frame_u8.shape[0] != even_h:
                            frame_u8 = cv2.copyMakeBorder(
                                frame_u8, 0, even_h - frame_u8.shape[0], 0, even_w - frame_u8.shape[1],
                                cv2.BORDER_REPLICATE
                            )
                        proc.stdin.write(frame_u8.tobytes())
                        pbar_ui.update_absolute(idx + 1, total_frames + 1)
                finally:
                    if proc.stdin:
                        proc.stdin.close()
                    proc.wait()
                    if temp_wav_path and os.path.exists(temp_wav_path):
                        try:
                            os.remove(temp_wav_path)
                        except OSError:
                            pass

                if proc.returncode != 0:
                    err_msg = proc.stderr.read().decode("utf-8", errors="ignore") if proc.stderr else "Unknown FFmpeg error"
                    raise RuntimeError(f"[BlackboxMediaSaver] FFmpeg error:\n{err_msg[-600:]}")

                pbar_ui.update_absolute(total_frames + 1, total_frames + 1)

                preview_filename = f"preview_{int(time.time() * 1000) % 1000000}.webp"
                preview_path = os.path.join(folder_paths.get_temp_directory(), preview_filename)
                preview_step = max(1, total_frames // 100)
                prev_pil_list = []
                for idx in range(0, min(total_frames, 120 * preview_step), preview_step):
                    u8_im = load_buffered_u8(idx)
                    p_im = Image.fromarray(u8_im, mode="RGB")
                    if max(p_im.width, p_im.height) > 480:
                        sc = 480.0 / max(p_im.width, p_im.height)
                        p_im = p_im.resize((max(8, int(p_im.width * sc)), max(8, int(p_im.height * sc))), Image.Resampling.BILINEAR)
                    prev_pil_list.append(p_im)

                if prev_pil_list:
                    prev_pil_list[0].save(
                        preview_path, format="WEBP", save_all=True, append_images=prev_pil_list[1:],
                        duration=int(avg_dur_ms * preview_step), loop=0, quality=75
                    )

            else:
                pil_frames = [Image.fromarray(load_buffered_u8(idx), mode="RGB") for idx in range(total_frames)]

                if target_ext == ".webp":
                    print(f"[BlackboxMediaSaver] Encoding {total_frames} frames to WebP (quality={webp_quality}, lossless={webp_lossless})...")
                    pil_frames[0].save(
                        final_save_path,
                        format="WEBP",
                        save_all=True,
                        append_images=pil_frames[1:],
                        duration=durations,
                        loop=loop_count,
                        quality=int(webp_quality),
                        lossless=bool(webp_lossless),
                        method=4
                    )
                    pbar_ui.update_absolute(total_frames + 1, total_frames + 1)
                else:
                    print(f"[BlackboxMediaSaver] Quantizing {total_frames} frames to GIF ({max_colors} colors)...")
                    sample_step = max(1, total_frames // 8)
                    sample_imgs = pil_frames[::sample_step]
                    strip_w, strip_h = sample_imgs[0].size
                    palette_strip = Image.new("RGB", (strip_w * len(sample_imgs), strip_h))
                    for idx, s_img in enumerate(sample_imgs):
                        palette_strip.paste(s_img, (idx * strip_w, 0))

                    master_palette_img = palette_strip.quantize(
                        colors=max(2, min(256, int(max_colors))),
                        method=Image.Quantize.MEDIANCUT,
                        dither=Image.Dither.NONE
                    )

                    dither_flag = Image.Dither.FLOYDSTEINBERG if "Floyd" in dither_mode else Image.Dither.NONE

                    quantized_frames = []
                    for idx, f in enumerate(tqdm(pil_frames, desc="[BlackboxSaver] Building GIF", unit="frame")):
                        quantized_frames.append(f.quantize(palette=master_palette_img, dither=dither_flag))
                        pbar_ui.update_absolute(idx + 1, total_frames + 1)

                    quantized_frames[0].save(
                        final_save_path,
                        save_all=True,
                        append_images=quantized_frames[1:],
                        duration=durations,
                        loop=loop_count,
                        optimize=False
                    )
                    pbar_ui.update_absolute(total_frames + 1, total_frames + 1)

                preview_filename = f"preview_{int(time.time() * 1000) % 1000000}{target_ext}"
                preview_path = os.path.join(folder_paths.get_temp_directory(), preview_filename)
                shutil.copyfile(final_save_path, preview_path)

        finally:
            total_elapsed = format_eta(time.time() - session.get("start_timestamp", time.time()))
            destroy_all_sessions()
            gc.collect()
            mm.soft_empty_cache()

        done_msg = f"✅ Export complete ({total_frames} frames in {total_elapsed}): {os.path.basename(final_save_path)}"
        print(f"[BlackboxMediaSaver] {done_msg} -> {final_save_path}")

        return {
            "ui": {
                "has_more_chunks": [False],
                "chunk_status": [done_msg],
                "images": [{
                    "filename": preview_filename,
                    "subfolder": "",
                    "type": "temp"
                }]
            },
            "result": (final_save_path,)
        }


# ==============================================================================
# NODE MAPPINGS
# ==============================================================================

WEB_DIRECTORY = "./js"

NODE_CLASS_MAPPINGS = {
    "BlackboxMediaLoader": BlackboxMediaLoader,
    "BlackboxMediaInspector": BlackboxMediaInspector,
    "BlackboxOpticalFlowDeflicker": BlackboxOpticalFlowDeflicker,
    "BlackboxMediaSaver": BlackboxMediaSaver,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "BlackboxMediaLoader": "Blackbox Media Loader",
    "BlackboxMediaInspector": "Blackbox Media Inspector",
    "BlackboxOpticalFlowDeflicker": "Blackbox Optical Flow Deflicker",
    "BlackboxMediaSaver": "Blackbox Media Saver & Muxer",
}

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS", "WEB_DIRECTORY"]
