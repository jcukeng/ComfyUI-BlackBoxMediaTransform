<div align="center">

# 🎬 ComfyUI-Blackbox-Media

**Transactional Media Streaming, Cross-Chunk RAFT Optical Flow Deflickering & FFmpeg Muxing Suite for ComfyUI**

[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](https://opensource.org/licenses/MIT)
[![ComfyUI](https://img.shields.io/badge/ComfyUI-Custom_Nodes-blue.svg)](https://github.com/comfyanonymous/ComfyUI)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-yellow.svg)](https://www.python.org/)
[![FFmpeg](https://img.shields.io/badge/FFmpeg-MP4_%7C_WebM_%7C_Audio-orange.svg)](https://ffmpeg.org/)

🌐 **Languages:** [**English**](README.md) | [**Русский**](README.ru.md) | [**Deutsch**](README.de.md) | [**中文**](README.zh.md)

</div>

---

## 📖 Overview

Standard ComfyUI video and animation loaders decode entire files into uncompressed `float32` tensors in RAM all at once. For example, a 5,000-frame video at `1280×720` consumes **~58 GB of RAM** on load and another **~58 GB** inside `VAEDecode`, causing instant Out-Of-Memory (OOM) crashes even on 64 GB workstations.

**ComfyUI-Blackbox-Media** eliminates this architectural bottleneck by combining **On-Demand Lazy Frame Decoding**, **Transactional Auto-Chunking**, **Cross-Chunk RAFT Optical Flow Deflickering**, and **Direct FFmpeg Pipe Muxing**.

Wrap **any** existing single-image ComfyUI workflow (Flux, SDXL, ControlNet, Upscalers, Inpainting, Face Detailers) into a conceptual **"Blackbox"** and process videos, GIFs, WebPs, or image sequences of arbitrary length and resolution with a minimal, fixed memory footprint.

---

## 🏗️ Workflow Architecture

```text
+-----------------------------------------------------------------------------------+
|                              BLACKBOX MEDIA SUITE                                 |
+-----------------------------------------------------------------------------------+

  [ Video / GIF / WebP / Image Sequence ]
                     │
                     ▼
      ┌──────────────────────────────┐
      │    Blackbox Media Loader     │ (Lazy decoding + Auto-Chunking: e.g. 50 frames)
      └──────────────┬───────────────┘
                     │
        ┌────────────┼───────────────────────────────┐
        │ image_list │ stream_meta                   │ audio (PCM Tensor)
        ▼            ▼                               │
  ╔═══════════════════════╗    ┌───────────────────────────┐
  ║                       ║    │ Blackbox Media Inspector  │ (Optional Telemetry,
  ║     YOUR BLACKBOX     ║    │  & VAE Safe Dimensions    │  RAM Forecast & Pins)
  ║       PIPELINE        ║    └─────────────┬─────────────┘
  ║  -------------------  ║                  │
  ║  • VAEEncode          ║                  │
  ║  • KSampler / Flux    ║                  │
  ║  • ControlNet / LoRA  ║                  │
  ║  • VAEDecode          ║                  │
  ║  • Upscale / Detailer ║                  │
  ╚══════════╦════════════╝                  │
             │ processed_images              │ stream_meta
             ▼                               ▼
      ┌──────────────────────────────────────────────┐
      │      Blackbox Optical Flow Deflicker         │ (Rolling LAB + RAFT Sliding
      │      (Seamless Cross-Chunk Stabilization)    │  Window in VRAM)
      └──────────────┬───────────────────────┬───────┘
                     │ stabilized_images     │ stream_meta
                     ▼                       ▼
      ┌──────────────────────────────────────────────┐
      │        Blackbox Media Saver & Muxer          │◄─── [ Optional External Audio ]
      │  (Two-Phase Commit & FFmpeg Stream Encoder)  │     (Dubbing for GIF/WebP/Seq)
      └──────────────┬───────────────────────┬───────┘
                     │                       │
                     │ (If more chunks)      │ (When all chunks committed)
                     ▼                       ▼
         [ Auto-Free RAM & Queue ]   [ Final MP4 / WebM / WebP / GIF ]
```

---

## ✨ Key Engineering Features

* **Zero-RAM Lazy Frame Decoding (`LazyVideoFrameList` & `LazySequenceFrameList`):** Opens multi-gigabyte `.mp4`, `.mkv`, `.webm`, `.mov`, `.avi` files or directories containing thousands of images in `0.01s`. Frames are decoded on demand one by one as your Blackbox requests them.
* **Transactional Auto-Chunking (Two-Phase Commit):** Splits long media streams into safe batches (e.g., `chunk_size = 50` frames). Each chunk passes through `VAEDecode` using ~500 MB of RAM, commits atomically to disk in `Saver`, triggers ComfyUI `/free` memory cleanup, and automatically queues the next chunk with live ETA calculation.
* **Crash & Interruption Resilience:** Neither `Loader` nor `Deflicker` mutates session state prematurely. State is committed only after `Saver` writes the chunk to disk. If a run fails mid-chunk (OOM or manual Cancel), you can choose between **Restart from 0 (Safe Clean Start)** or **Resume from last committed chunk**.
* **Seamless Cross-Chunk RAFT Optical Flow Deflicker:** Eliminates AI frame-to-frame flicker and brightness pumping using bidirectional **RAFT** optical flow (with forward-backward occlusion masks and FP16 autocast) + rolling **LAB histogram matching**. Preserves color references and sliding-window frame tails across chunk boundaries so chunk seams are 100% invisible.
* **VRAM Sliding Window Cache (`pin_memory` + `LRU`):** Holds only `2 * window_radius + 1` frames in VRAM at any millisecond. Every frame is transferred over PCIe (`CPU -> GPU`) exactly once.
* **Universal FFmpeg Muxer & Audio Dubbing:** Streams frames directly into `ffmpeg` pipes (`H.264 MP4` or `VP9 WebM`) or encodes `24-bit TrueColor WebP` and `256-color Master Palette GIF`. Automatically extracts, trims, reverses (`areverse`), and time-stretches (`atempo`) audio to match `speed_multiplier` or `override_fps`, or dubs silent GIFs/sequences using any external `AUDIO` node.

---

## 🧩 Nodes Reference

### 1. `Blackbox Media Loader`
Loads video files, animated GIFs, animated WebPs, or Image Sequence directories and emits a single-frame list (`OUTPUT_IS_LIST = True`) for frame-by-frame processing inside your Blackbox.

| Parameter / Control | Type | Description |
| :--- | :--- | :--- |
| **Interactive Player** | `DOM Widget` | Resizable preview player with play/pause, frame stepping, timeline scrubber, and live frame timing readout. |
| **📁 Select Video / GIF / WebP File...** | `Button` | Uploads and selects a media file via native OS file dialog. |
| **📂 Select Image Sequence Folder...** | `Button` | Opens native OS folder picker to load a directory of naturally sorted images (`frame_1.png` ... `frame_10.png`). |
| **🔄 Reset Chunk Progress** | `Button` | Manually resets active chunk session back to frame 0. |
| `media_source` | `COMBO` | Select media file from ComfyUI `input/` directory. |
| `start_frame` | `INT` | Zero-based starting frame index. |
| `max_frames` | `INT` | Maximum frames to process (`0` = process entire file to the end). |
| `frame_skip` | `INT` | Frame step factor (`1` = every frame, `2` = every 2nd frame, etc.). |
| `chunk_size` | `INT` | Frames emitted per workflow pass (`50` default; `0` disables chunking). |
| `if_interrupted` | `COMBO` | Action after error/cancel: `Restart from 0` or `Resume from last committed chunk`. |
| `sequence_fps` | `FLOAT` | Base frame rate (FPS) assigned when loading an Image Sequence folder. |
| `custom_path` *(optional)* | `STRING` | Direct path to a media file or image sequence folder. |

**Outputs:** `image_list` (`IMAGE`), `stream_meta` (`BLACKBOX_STREAM_META`), `audio` (`AUDIO`).

---

### 2. `Blackbox Media Inspector`
Read-only telemetry dashboard and geometry calculator.

* **On-Node Telemetry Report:** Displays container format, video/audio codecs (`ffprobe`), bitrate, aspect ratio, **VAE / DiT Alignment Check** (divisibility by 64/16/2), **CFR vs VFR** timing analysis, and **RAM / Disk Swap Forecast**.
* **Outputs:**
  * `stream_meta` (`BLACKBOX_STREAM_META` passthrough)
  * `width`, `height` (`INT` — original dimensions)
  * `vae_safe_width`, `vae_safe_height` (`INT` — dimensions rounded down to the nearest multiple of 16 for direct connection to `ImageResize` or `EmptyLatentImage` inside your Blackbox)
  * `chunk_frames`, `total_frames` (`INT`)
  * `fps` (`FLOAT`)
  * `telemetry_report` (`STRING`)

---

### 3. `Blackbox Optical Flow Deflicker`
Eliminates temporal flickering, AI texture boiling, and color shifts after Blackbox processing.

| Parameter | Type | Default | Description |
| :--- | :--- | :--- | :--- |
| `images` | `IMAGE` | *Required* | Processed frames coming out of your Blackbox pipeline. |
| `raft_model` | `COMBO` | `raft_large` | `raft_large (High Precision)` or `raft_small (Fast)`. |
| `temporal_blend` | `FLOAT` | `0.65` | Bidirectional optical-flow smoothing strength (`0.0` bypasses RAFT). |
| `window_radius` | `INT` | `1` | Temporal window radius (`1` = `[t-1..t+1]`, `2` = `[t-2..t+2]`). |
| `color_stabilization` | `FLOAT` | `0.50` | Rolling LAB histogram stabilization strength. |
| `occlusion_sensitivity` | `FLOAT` | `1.5` | Ghosting rejection threshold (lower = stricter). |
| `seamless_loop` | `BOOLEAN` | `True` | Blends end frames with start frames for seamless looping. |
| `stream_meta` *(optional)* | `META` | `None` | Preserves LAB color reference and sliding window across chunks. |

---

### 4. `Blackbox Media Saver & Muxer`
Transactional chunk sink and final multi-format encoder.

| Parameter | Type | Default | Description |
| :--- | :--- | :--- | :--- |
| `output_format` | `COMBO` | `Auto` | `Auto`, `MP4 (H.264 + AAC)`, `WebM (VP9 + Opus)`, `WebP (24-bit)`, `GIF (256-color)`. |
| `include_audio` | `BOOLEAN` | `True` | Muxes external `audio` pin or source video audio into MP4/WebM. |
| `speed_multiplier` | `FLOAT` | `1.00` | Speeds up or slows down video and automatically syncs audio tempo (`atempo`). |
| `override_fps` | `FLOAT` | `0.0` | Enforces constant FPS (`0.0` keeps original frame durations). |
| `first_frame_hold_ms` | `INT` | `0` | Extra pause on the first frame (ms). |
| `last_frame_hold_ms` | `INT` | `0` | Extra pause on the last frame (ms). |
| `playback_mode` | `COMBO` | `Normal` | `Normal (Forward)`, `Reverse (Backward)`, or `Ping-Pong (Boomerang)`. |
| `loop_count` | `INT` | `0` | Loop count for GIF/WebP (`0` = infinite loop). |
| `video_crf` | `INT` | `18` | Quality factor for MP4/WebM (`0..51`; `18` is visually lossless). Auto-pads odd dimensions to even. |
| `max_colors` / `dither_mode` | `INT` / `COMBO` | `256` / `None` | Master palette quantization controls for GIF. |
| `webp_quality` / `webp_lossless` | `INT` / `BOOL` | `90` / `False` | Compression controls for 24-bit animated WebP. |
| `output_scale` | `FLOAT` | `1.00` | Final resolution scale factor (`0.10x .. 4.00x`, Lanczos). |
| `output_directory` | `STRING` | `""` | Custom output directory (includes **📂 Select Output Folder...** button). |
| `filename_prefix` | `STRING` | `Blackbox_Media` | Output filename prefix (`overwrite_existing` supported). |
| `stream_meta` / `audio` | `Optional` | `None` | Connect `stream_meta` and optional external `AUDIO` (to dub GIFs/sequences). |

---

## 🛠️ Installation

1. Clone this repository into your `ComfyUI/custom_nodes/` directory:
   ```bash
   cd ComfyUI/custom_nodes
   git clone [https://github.com/your-repo/ComfyUI-Blackbox-Media.git](https://github.com/your-repo/ComfyUI-Blackbox-Media.git)
   ```
2. Install Python dependencies (usually pre-installed in ComfyUI):
   ```bash
   pip install -r ComfyUI-Blackbox-Media/requirements.txt
   ```
3. Make sure **`ffmpeg`** and **`ffprobe`** are installed and available in your system `PATH`.
4. Restart ComfyUI and refresh your browser (`Ctrl + F5`). Nodes are located under the **`Blackbox Media`** category.

---

## 📄 License

Distributed under the **MIT License**.
