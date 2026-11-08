<div align="center">

# 🎬 ComfyUI-Blackbox-Media

**面向 ComfyUI 的事务级流媒体分块处理、跨分块 RAFT 光流去闪烁与 FFmpeg 多格式混流套件**

[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](https://opensource.org/licenses/MIT)
[![ComfyUI](https://img.shields.io/badge/ComfyUI-Custom_Nodes-blue.svg)](https://github.com/comfyanonymous/ComfyUI)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-yellow.svg)](https://www.python.org/)
[![FFmpeg](https://img.shields.io/badge/FFmpeg-MP4_%7C_WebM_%7C_Audio-orange.svg)](https://ffmpeg.org/)

🌐 **语言 / Languages:** [**English**](README.md) | [**Русский**](README.ru.md) | [**Deutsch**](README.de.md) | [**中文**](README.zh.md)

</div>

---

## 📖 项目简介

ComfyUI 的标准视频和动画加载节点会将整个文件一次性解码为未压缩的 `float32` 张量并存入系统内存 (RAM)。例如，一段 5,000 帧的 `1280×720` 视频在加载时就会占用 **~58 GB 内存**，而在 `VAEDecode` 阶段还会再占用 **~58 GB 内存**，即使在配备 64 GB 内存的工作站上也会立即引发内存溢出 (OOM) 崩溃。

**ComfyUI-Blackbox-Media** 通过结合 **按需惰性帧解码 (Lazy Frame Decoding)**、**事务级自动分块队列 (Transactional Auto-Chunking)**、**跨分块 RAFT 光流去闪烁** 以及 **FFmpeg 管道直写混流**，彻底消除了这一架构瓶颈。

您可以将**任何**现有的单图 ComfyUI 工作流（如 Flux、SDXL、ControlNet、放大器、重绘、面部修复）封装为一个概念上的 **“黑盒 (Blackbox)”**，以极低且恒定的内存占用处理任意长度和分辨率的视频、GIF、WebP 或图像序列。

---

## 🏗️ 工作流架构

```text
+-----------------------------------------------------------------------------------+
|                              BLACKBOX MEDIA SUITE                                 |
+-----------------------------------------------------------------------------------+

  [ 视频 / GIF / WebP / 图像序列文件夹 ]
                     │
                     ▼
      ┌──────────────────────────────┐
      │    Blackbox Media Loader     │ (惰性按需解码 + 自动分块: 如每次 50 帧)
      └──────────────┬───────────────┘
                     │
        ┌────────────┼───────────────────────────────┐
        │ image_list │ stream_meta                   │ audio (PCM 音频张量)
        ▼            ▼                               │
  ╔═══════════════════════╗    ┌───────────────────────────┐
  ║                       ║    │ Blackbox Media Inspector  │ (可选遥测面板、内存预测
  ║     您的“黑盒”流水线    ║    │  & VAE Safe Dimensions    │  与 16 倍数安全尺寸引脚)
  ║   (BLACKBOX PIPELINE) ║    └─────────────┬─────────────┘
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
      │      Blackbox Optical Flow Deflicker         │ (滚动 LAB 色彩匹配 + 显存内
      │          (跨分块无缝光流防闪烁稳定器)          │  RAFT 滑动窗口缓存)
      └──────────────┬───────────────────────┬───────┘
                     │ stabilized_images     │ stream_meta
                     ▼                       ▼
      ┌──────────────────────────────────────────────┐
      │        Blackbox Media Saver & Muxer          │◄─── [ 可选外部 AUDIO 输入 ]
      │       (两阶段事务提交与 FFmpeg 管道编码器)     │     (为 GIF/WebP/序列配音)
      └──────────────┬───────────────────────┬───────┘
                     │                       │
                     │ (若仍有后续分块)        │ (当所有分块提交完成)
                     ▼                       ▼
         [ 自动释放内存并排队下一块 ]    [ 最终输出 MP4 / WebM / WebP / GIF ]
```

---

## ✨ 核心工程特性

* **零预占内存的惰性帧解码 (`LazyVideoFrameList` & `LazySequenceFrameList`)：** 在 `0.01 秒` 内打开数 GB 的 `.mp4`、`.mkv`、`.webm`、`.mov`、`.avi` 视频或包含上万张图片的文件夹。仅在黑盒请求时按需逐帧解码。
* **事务级自动分块队列 (Two-Phase Commit)：** 将长视频拆分为安全的批次（例如 `chunk_size = 50` 帧）。每个分块通过 `VAEDecode` 仅消耗约 500 MB 内存，在 `Saver` 中原子化提交至磁盘后自动调用 ComfyUI `/free` 释放内存，并自动排队下一分块（支持实时 **ETA 剩余时间预估**）。
* **崩溃恢复与断点续传：** `Loader` 和 `Deflicker` 不会提前篡改会话状态，仅在 `Saver` 完成分块写入后才进行原子提交。若中途因报错或手动点击 `Cancel` 中断，再次运行时可选择 **Restart from 0（从第 0 帧安全重跑）** 或 **Resume from last committed chunk（从最后成功的分块断点续传）**。
* **跨分块无缝 RAFT 光流去闪烁：** 采用双向 **RAFT** 光流（带前后向遮挡/鬼影掩码及 FP16 加速）与滚动 **LAB 色彩直方图稳定算法**，消除 AI 生成常见的帧间闪烁与亮度跳变，并在分块边界之间传递色彩基准与滑动窗口尾部帧，实现 100% 无缝拼接。
* **显存滑动窗口缓存 (`pin_memory` + `LRU`)：** 显存中在任意时刻仅保留 `2 * window_radius + 1` 帧，每帧图像仅通过 PCIe 总线 (`CPU -> GPU`) 传输一次。
* **全能 FFmpeg 混流与外部配音：** 支持通过 `ffmpeg` 管道流式编码 `H.264 MP4`、`VP9 WebM`，或导出 `24位真彩色 WebP` 与 `256色全局调色板 GIF`。支持自动裁剪、倒放 (`areverse`)、变速同步 (`atempo`) 原视频音轨，或接入任意外部 `AUDIO` 节点为无声 GIF/图像序列合成配音。

---

## 🧩 节点说明

### 1. `Blackbox Media Loader` (媒体流加载器)
加载视频、GIF、WebP 或图像序列目录，并输出逐帧列表 (`OUTPUT_IS_LIST = True`) 供黑盒内部节点逐帧处理。

| 参数 / 控件 | 类型 | 说明 |
| :--- | :--- | :--- |
| **交互式预览播放器** | `DOM Widget` | 支持缩放、播放/暂停、逐帧步进、进度条拖拽及帧耗时显示的实时预览器。 |
| **📁 Select Video / GIF / WebP File...** | `Button` | 调用系统原生文件对话框上传并选择媒体文件。 |
| **📂 Select Image Sequence Folder...** | `Button` | 调用系统原生文件夹选择器加载自然排序的图像序列 (`frame_1.png` ... `frame_10.png`)。 |
| **🔄 Reset Chunk Progress** | `Button` | 手动将分块进度重置回第 0 帧。 |
| `media_source` | `COMBO` | 从 `ComfyUI/input/` 目录选择媒体文件。 |
| `start_frame` / `max_frames` | `INT` | 起始帧索引（从 0 开始）与最大处理帧数（`0` = 处理至文件末尾）。 |
| `frame_skip` | `INT` | 抽帧步长（`1` = 逐帧处理，`2` = 每 2 帧取 1 帧）。 |
| `chunk_size` | `INT` | 单次工作流传递的帧数（默认 `50`；设为 `0` 则禁用分块）。 |
| `if_interrupted` | `COMBO` | 中断后重跑策略：`Restart from 0` 或 `Resume from last committed chunk`。 |
| `sequence_fps` | `FLOAT` | 加载图像序列文件夹时的基准帧率 (FPS)。 |
| `custom_path` *(可选)* | `STRING` | 媒体文件或图像序列目录的直接路径。 |

**输出引脚：** `image_list` (`IMAGE`)、`stream_meta` (`BLACKBOX_STREAM_META`)、`audio` (`AUDIO`)。

---

### 2. `Blackbox Media Inspector` (流媒体遥测与几何检查器)
只读媒体流遥测仪表盘与安全分辨率计算器。

* **面板遥测报告：** 显示容器格式、音视频编解码器 (`ffprobe`)、码率、宽高比、**VAE / DiT 分辨率对齐检查**（检测是否可被 64/16/2 整除）、**CFR 恒定帧率与 VFR 可变帧率**分析，以及 **单分块内存与磁盘交换空间预测**。
* **输出引脚：** `stream_meta`、`width`、`height`、`vae_safe_width`、`vae_safe_height`（向下取整为 16 的倍数，可直接连入黑盒内的 `ImageResize` 或 `EmptyLatentImage`）、`chunk_frames`、`total_frames`、`fps`、`telemetry_report`。

---

### 3. `Blackbox Optical Flow Deflicker` (光流去闪烁稳定器)
消除黑盒处理后的帧间闪烁、纹理沸腾与色彩跳变。

* **主要参数：** `raft_model` (`raft_large` / `raft_small`)、`temporal_blend` (`0.65`)、`window_radius` (`1..3`)、`color_stabilization` (`0.50`)、`occlusion_sensitivity` (`1.5`)、`seamless_loop` (`True`)，以及可选输入 `stream_meta`（用于跨分块无缝衔接）。

---

### 4. `Blackbox Media Saver & Muxer` (媒体流保存与混流器)
事务级分块接收器与最终多格式编码器。

* **输出格式：** `Auto (Match Source Format)`、`MP4 (H.264 + AAC Audio)`、`WebM (VP9 + Opus Audio)`、`WebP (TrueColor 24-bit Animation)`、`GIF (256-color Master Palette)`。
* **核心功能：** 音轨混流 (`include_audio` + 可选外部 `audio` 配音引脚)、变速与音画同步 (`speed_multiplier`、`override_fps`)、首尾帧定格 (`first_frame_hold_ms`、`last_frame_hold_ms`)、播放模式 (`Normal`、`Reverse` 倒放、`Ping-Pong` 乒乓循环)、质量控制 (`video_crf`、自动偶数宽高对齐、`webp_quality`、`max_colors`、`dither_mode`)、输出缩放 (`output_scale`) 以及系统文件夹选择按钮 (**📂 Select Output Folder...**)。

---

## 🛠️ 安装方法

1. 将本仓库克隆至 `ComfyUI/custom_nodes/` 目录：
   ```bash
   cd ComfyUI/custom_nodes
   git clone [https://github.com/your-repo/ComfyUI-Blackbox-Media.git](https://github.com/your-repo/ComfyUI-Blackbox-Media.git)
   ```
2. 安装 Python 依赖项（标准 ComfyUI 环境通常已内置）：
   ```bash
   pip install -r ComfyUI-Blackbox-Media/requirements.txt
   ```
3. 确保系统中已安装 **`ffmpeg`** 和 **`ffprobe`** 并已添加至系统环境变量 `PATH`。
4. 重启 ComfyUI 并刷新浏览器 (`Ctrl + F5`)。所有节点均位于 **`Blackbox Media`** 分类下。

---

## 📄 开源协议

本项目基于 **MIT License** 开源。
