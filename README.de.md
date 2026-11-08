<div align="center">

# 🎬 ComfyUI-Blackbox-Media

**Transaktionales Media-Streaming, Chunk-übergreifendes RAFT Optical-Flow-Deflickering & FFmpeg-Muxing für ComfyUI**

[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](https://opensource.org/licenses/MIT)
[![ComfyUI](https://img.shields.io/badge/ComfyUI-Custom_Nodes-blue.svg)](https://github.com/comfyanonymous/ComfyUI)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-yellow.svg)](https://www.python.org/)
[![FFmpeg](https://img.shields.io/badge/FFmpeg-MP4_%7C_WebM_%7C_Audio-orange.svg)](https://ffmpeg.org/)

🌐 **Sprachen:** [**English**](README.md) | [**Русский**](README.ru.md) | [**Deutsch**](README.de.md) | [**中文**](README.zh.md)

</div>

---

## 📖 Überblick

Standardmäßige Video- und Animations-Loader in ComfyUI dekodieren komplette Dateien sofort als unkomprimierte `float32`-Tensoren in den Arbeitsspeicher (RAM). Ein Video mit 5.000 Frames in `1280×720` belegt bereits beim Laden **~58 GB RAM** und weitere **~58 GB** während `VAEDecode`, was selbst auf 64-GB-Systemen sofort zu Out-Of-Memory-Abstürzen (OOM) führt.

**ComfyUI-Blackbox-Media** beseitigt diesen architektonischen Engpass durch die Kombination aus **On-Demand Lazy Frame Decoding**, **transaktionalem Auto-Chunking**, **Chunk-übergreifendem RAFT Optical Flow Deflickering** und **direktem FFmpeg-Pipe-Muxing**.

Betten Sie **jeden beliebigen** Einzelbild-Workflow (Flux, SDXL, ControlNet, Upscaler, Inpainting, Face Detailer) als **„Blackbox“** ein und verarbeiten Sie Videos, GIFs, WebPs oder Bildsequenzen beliebiger Länge und Auflösung bei minimalem, konstantem Speicherverbrauch.

---

## 🏗️ Workflow-Architektur

```text
+-----------------------------------------------------------------------------------+
|                              BLACKBOX MEDIA SUITE                                 |
+-----------------------------------------------------------------------------------+

  [ Video / GIF / WebP / Bildsequenz-Ordner ]
                     │
                     ▼
      ┌──────────────────────────────┐
      │    Blackbox Media Loader     │ (Lazy Decoding + Auto-Chunking: z.B. 50 Frames)
      └──────────────┬───────────────┘
                     │
        ┌────────────┼───────────────────────────────┐
        │ image_list │ stream_meta                   │ audio (PCM-Tensor)
        ▼            ▼                               │
  ╔═══════════════════════╗    ┌───────────────────────────┐
  ║                       ║    │ Blackbox Media Inspector  │ (Optionale Telemetrie,
  ║     IHRE BLACKBOX     ║    │  & VAE Safe Dimensions    │  RAM-Prognose & Pins)
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
      │      Blackbox Optical Flow Deflicker         │ (Gleitendes LAB + RAFT
      │     (Nahtlose Stabilisierung über Chunks)    │  Sliding Window im VRAM)
      └──────────────┬───────────────────────┬───────┘
                     │ stabilized_images     │ stream_meta
                     ▼                       ▼
      ┌──────────────────────────────────────────────┐
      │        Blackbox Media Saver & Muxer          │◄─── [ Optionales externes Audio ]
      │  (Two-Phase Commit & FFmpeg Stream Encoder)  │     (Vertonung für GIF/WebP/Seq)
      └──────────────┬───────────────────────┬───────┘
                     │                       │
                     │ (Weitere Chunks)      │ (Alle Chunks abgeschlossen)
                     ▼                       ▼
         [ Auto-Free RAM & Queue ]   [ Finales MP4 / WebM / WebP / GIF ]
```

---

## ✨ Technische Hauptmerkmale

* **Zero-RAM Lazy Frame Decoding (`LazyVideoFrameList` & `LazySequenceFrameList`):** Öffnet mehrere Gigabyte große `.mp4`-, `.mkv`-, `.webm`-, `.mov`-, `.avi`-Dateien oder Ordner mit Tausenden Einzelbildern in `0,01s`. Frames werden erst bei Abruf einzeln dekodiert.
* **Transaktionales Auto-Chunking (Two-Phase Commit):** Teilt lange Medienströme in speichersichere Pakete auf (z. B. `chunk_size = 50` Frames). Jeder Chunk durchläuft `VAEDecode` mit nur ~500 MB RAM, wird im `Saver` atomar auf die Festplatte geschrieben, gibt den ComfyUI-RAM via `/free` frei und reiht automatisch den nächsten Chunk inklusive **ETA-Berechnung** ein.
* **Absturz- und Unterbrechungssicherheit:** Weder `Loader` noch `Deflicker` verändern den Sitzungsstatus vorzeitig. Erst nach erfolgreichem Speichern im `Saver` erfolgt das Commit. Nach einem Fehler oder Abbruch (`Cancel`) können Sie wählen zwischen **Restart from 0 (Sicherer Neustart)** oder **Resume from last committed chunk (Fortsetzen ab dem letzten gespeicherten Chunk)**.
* **Nahtloses Chunk-übergreifendes RAFT Optical Flow Deflickering:** Beseitigt KI-typisches Flackern und Helligkeitspumpen mittels bidirektionalem **RAFT** Optical Flow (inkl. Okklusions-/Ghosting-Maskierung und FP16-Autocast) sowie gleitendem **LAB-Farbabgleich**. Farb-Referenzen und Frame-Ränder werden über Chunk-Grenzen hinweg übergeben.
* **VRAM Sliding-Window-Cache (`pin_memory` + `LRU`):** Hält zu jedem Zeitpunkt nur `2 * window_radius + 1` Frames im VRAM. Jeder Frame wird exakt einmal über den PCIe-Bus (`CPU -> GPU`) übertragen.
* **Universeller FFmpeg-Muxer & Audio-Synchronisation:** Streamt Frames direkt in `ffmpeg`-Pipes (`H.264 MP4` oder `VP9 WebM`) oder kodiert `24-Bit TrueColor WebP` und `256-Farben Master-Palette GIF`. Schneidet, invertiert (`areverse`) und synchronisiert das Audiotempo (`atempo`) automatisch oder ermöglicht das Nachvertonen stummer GIFs/Bildsequenzen über externe `AUDIO`-Nodes.

---

## 🧩 Node-Referenz

### 1. `Blackbox Media Loader`
Lädt Videos, animierte GIFs, WebPs oder Bildsequenz-Ordner und gibt eine Einzelbild-Liste (`OUTPUT_IS_LIST = True`) für die Blackbox aus.

| Parameter / Steuerung | Typ | Beschreibung |
| :--- | :--- | :--- |
| **Interaktiver Player** | `DOM Widget` | Skalierbarer Vorschau-Player mit Play/Pause, Einzelbildschritt, Scrubber und Timing-Anzeige. |
| **📁 Select Video / GIF / WebP File...** | `Button` | Lädt und wählt eine Mediendatei über den nativen Dateidialog des Betriebssystems. |
| **📂 Select Image Sequence Folder...** | `Button` | Öffnet den System-Ordnerdialog für natürlich sortierte Bildsequenzen (`frame_1.png` ... `frame_10.png`). |
| **🔄 Reset Chunk Progress** | `Button` | Setzt den aktuellen Chunk-Fortschritt manuell auf Frame 0 zurück. |
| `media_source` | `COMBO` | Dateiauswahl aus dem `ComfyUI/input/`-Verzeichnis. |
| `start_frame` / `max_frames` | `INT` | Startframe (ab 0) und maximale Frame-Anzahl (`0` = gesamte Datei). |
| `frame_skip` | `INT` | Schrittweite (`1` = jeder Frame, `2` = jeder zweite Frame usw.). |
| `chunk_size` | `INT` | Frames pro Workflow-Durchlauf (`50` Standard; `0` deaktiviert Chunking). |
| `if_interrupted` | `COMBO` | Verhalten nach Abbruch: `Restart from 0` oder `Resume from last committed chunk`. |
| `sequence_fps` | `FLOAT` | Basis-FPS beim Laden eines Bildsequenz-Ordners. |
| `custom_path` *(optional)* | `STRING` | Direkter Dateipfad oder Pfad zu einem Bildsequenz-Ordner. |

**Ausgänge:** `image_list` (`IMAGE`), `stream_meta` (`BLACKBOX_STREAM_META`), `audio` (`AUDIO`).

---

### 2. `Blackbox Media Inspector`
Telemetrie-Dashboard und Geometrie-Rechner.

* **Telemetrie-Anzeige:** Zeigt Containerformat, Video-/Audio-Codecs (`ffprobe`), Bitrate, Seitenverhältnis, **VAE / DiT Alignment Check** (Teilbarkeit durch 64/16/2), **CFR vs. VFR**-Timing-Analyse sowie **RAM- und Disk-Swap-Prognosen**.
* **Ausgänge:** `stream_meta`, `width`, `height`, `vae_safe_width`, `vae_safe_height` (auf Vielfache von 16 abgerundete Maße für `ImageResize` / `EmptyLatentImage`), `chunk_frames`, `total_frames`, `fps`, `telemetry_report`.

---

### 3. `Blackbox Optical Flow Deflicker`
Eliminiert zeitliches Flackern und Farbsprünge nach der Blackbox-Verarbeitung.

* **Parameter:** `raft_model` (`raft_large` / `raft_small`), `temporal_blend` (`0.65`), `window_radius` (`1..3`), `color_stabilization` (`0.50`), `occlusion_sensitivity` (`1.5`), `seamless_loop` (`True`), sowie optional `stream_meta` für nahtlose Chunk-Übergänge.

---

### 4. `Blackbox Media Saver & Muxer`
Transaktionaler Chunk-Empfänger und Multiformat-Encoder.

* **Formate:** `Auto (Match Source Format)`, `MP4 (H.264 + AAC Audio)`, `WebM (VP9 + Opus Audio)`, `WebP (TrueColor 24-bit Animation)`, `GIF (256-color Master Palette)`.
* **Funktionen:** Audio-Muxing (`include_audio` + optionaler `audio`-Pin), Geschwindigkeitsanpassung (`speed_multiplier`, `override_fps`), Haltezeiten (`first_frame_hold_ms`, `last_frame_hold_ms`), Abspielmodi (`Normal`, `Reverse`, `Ping-Pong`), Qualitätssteuerung (`video_crf`, `webp_quality`, `max_colors`, `dither_mode`), Skalierung (`output_scale`) und Zielordner-Dialog (**📂 Select Output Folder...**).

---

## 🛠️ Installation

1. Repository in das Verzeichnis `ComfyUI/custom_nodes/` klonen:
   ```bash
   cd ComfyUI/custom_nodes
   git clone [https://github.com/your-repo/ComfyUI-Blackbox-Media.git](https://github.com/your-repo/ComfyUI-Blackbox-Media.git)
   ```
2. Python-Abhängigkeiten installieren:
   ```bash
   pip install -r ComfyUI-Blackbox-Media/requirements.txt
   ```
3. Stellen Sie sicher, dass **`ffmpeg`** und **`ffprobe`** installiert und im System-`PATH` verfügbar sind.
4. ComfyUI neu starten und den Browser aktualisieren (`Strg + F5`). Alle Nodes befinden sich in der Kategorie **`Blackbox Media`**.

---

## 📄 Lizenz

Veröffentlicht unter der **MIT-Lizenz**.
