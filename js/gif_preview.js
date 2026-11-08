import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

app.registerExtension({
    name: "BlackboxMedia.UIExtensions",
    async beforeRegisterNodeDef(nodeType, nodeData, app) {

        // =====================================================================
        // 1. INTERACTIVE PLAYER FOR BLACKBOX MEDIA LOADER
        // =====================================================================
        if (nodeData.name === "BlackboxMediaLoader") {
            const onNodeCreated = nodeType.prototype.onNodeCreated;
            nodeType.prototype.onNodeCreated = function () {
                const r = onNodeCreated ? onNodeCreated.apply(this, arguments) : undefined;
                const node = this;
                node._isConfiguringGraph = false;

                const fileInput = document.createElement("input");
                fileInput.type = "file";
                fileInput.accept = "image/gif,image/webp,video/mp4,video/webm,video/x-matroska,video/quicktime,video/x-msvideo,.mp4,.mkv,.mov,.webm,.avi,image/png,image/jpeg";
                fileInput.style.display = "none";
                document.body.appendChild(fileInput);

                fileInput.addEventListener("change", async () => {
                    if (!fileInput.files || !fileInput.files.length) return;
                    const file = fileInput.files[0];
                    const formData = new FormData();
                    formData.append("image", file);
                    formData.append("overwrite", "true");

                    try {
                        infoLabel.style.color = "#aaa";
                        infoLabel.textContent = "Uploading media file to server...";
                        const resp = await api.fetchApi("/upload/image", {
                            method: "POST",
                            body: formData
                        });
                        if (resp.ok) {
                            const data = await resp.json();
                            const uploadedName = data.subfolder ? `${data.subfolder}/${data.name}` : data.name;
                            const mediaWidget = node.widgets?.find(w => w.name === "media_source");
                            const customPathWidget = node.widgets?.find(w => w.name === "custom_path");
                            if (customPathWidget) {
                                customPathWidget.value = "";
                            }
                            if (mediaWidget) {
                                if (mediaWidget.options && Array.isArray(mediaWidget.options.values)) {
                                    if (!mediaWidget.options.values.includes(uploadedName)) {
                                        mediaWidget.options.values.unshift(uploadedName);
                                    }
                                }
                                mediaWidget.value = uploadedName;
                            }
                            scheduleLoadAnimation();
                        }
                    } catch (err) {
                        console.error("[BlackboxMedia] File upload error:", err);
                    }
                });

                // Helper: only execute button callback on genuine user mouse click (ignores JSON/API graph loading)
                function isGenuineUserClick(event) {
                    if (node._isConfiguringGraph || app.configuringGraph) return false;
                    if (!event) return false;
                    return (event.type === "pointerdown" || event.type === "mousedown" || event.type === "click");
                }

                const uploadWidget = node.addWidget("button", "📁 Select Video / GIF / WebP File...", null, function (value, canvas, n, pos, event) {
                    if (!isGenuineUserClick(event)) return;
                    fileInput.click();
                });
                uploadWidget.serialize = false;

                const folderSeqBtn = node.addWidget("button", "📂 Select Image Sequence Folder...", null, async function (value, canvas, n, pos, event) {
                    if (!isGenuineUserClick(event)) return;
                    const customPathWidget = node.widgets?.find(w => w.name === "custom_path");
                    const currentDir = customPathWidget ? String(customPathWidget.value || "") : "";
                    try {
                        folderSeqBtn.name = "⏳ Opening system folder dialog...";
                        node.setDirtyCanvas(true, true);

                        const params = new URLSearchParams({ initial_dir: currentDir });
                        const resp = await api.fetchApi(`/blackbox_media/browse_folder?${params.toString()}`);
                        const data = await resp.json();

                        if (data && data.selected_dir && customPathWidget) {
                            customPathWidget.value = data.selected_dir;
                            scheduleLoadAnimation();
                        }
                    } catch (err) {
                        console.error("[BlackboxMedia] Folder selection error:", err);
                    } finally {
                        folderSeqBtn.name = "📂 Select Image Sequence Folder...";
                        node.setDirtyCanvas(true, true);
                    }
                });
                folderSeqBtn.serialize = false;

                const resetChunksBtn = node.addWidget("button", "🔄 Reset Chunk Progress (Start from 0)", null, async function (value, canvas, n, pos, event) {
                    if (!isGenuineUserClick(event)) return;
                    try {
                        await api.fetchApi("/blackbox_media/reset_chunks", { method: "POST" });
                        infoLabel.style.color = "#7ee787";
                        infoLabel.textContent = "Chunk session reset to frame 0.";
                    } catch (e) {
                        console.error(e);
                    }
                });
                resetChunksBtn.serialize = false;

                const container = document.createElement("div");
                container.style.cssText = `
                    display: flex;
                    flex-direction: column;
                    align-items: stretch;
                    justify-content: space-between;
                    background: #181818;
                    border: 1px solid #333;
                    border-radius: 6px;
                    padding: 8px;
                    box-sizing: border-box;
                    width: 100%;
                    height: 100%;
                    overflow: hidden;
                    user-select: none;
                `;

                const imgWrapper = document.createElement("div");
                imgWrapper.style.cssText = `
                    flex: 1 1 auto;
                    min-height: 80px;
                    width: 100%;
                    display: flex;
                    align-items: center;
                    justify-content: center;
                    overflow: hidden;
                    border-radius: 4px;
                    background: repeating-conic-gradient(#222 0% 25%, #2a2a2a 0% 50%) 50% / 16px 16px;
                `;

                const frameImg = document.createElement("img");
                frameImg.style.cssText = `
                    width: 100%;
                    height: 100%;
                    object-fit: contain;
                    cursor: pointer;
                    display: none;
                `;
                imgWrapper.appendChild(frameImg);

                const footerBlock = document.createElement("div");
                footerBlock.style.cssText = `
                    flex: 0 0 auto;
                    width: 100%;
                    margin-top: 8px;
                    display: flex;
                    flex-direction: column;
                `;

                const controlsRow = document.createElement("div");
                controlsRow.style.cssText = `
                    display: flex;
                    align-items: center;
                    gap: 6px;
                    width: 100%;
                `;

                const btnStyle = `
                    background: #2b2b2b;
                    color: #eee;
                    border: 1px solid #555;
                    border-radius: 4px;
                    padding: 3px 8px;
                    cursor: pointer;
                    font-size: 12px;
                    line-height: 1.2;
                    flex-shrink: 0;
                `;

                const playBtn = document.createElement("button");
                playBtn.textContent = "⏸";
                playBtn.style.cssText = btnStyle;

                const prevBtn = document.createElement("button");
                prevBtn.textContent = "◀";
                prevBtn.style.cssText = btnStyle;

                const nextBtn = document.createElement("button");
                nextBtn.textContent = "▶";
                nextBtn.style.cssText = btnStyle;

                const scrubber = document.createElement("input");
                scrubber.type = "range";
                scrubber.min = 0;
                scrubber.max = 0;
                scrubber.value = 0;
                scrubber.style.cssText = "flex: 1; min-width: 40px; cursor: pointer; accent-color: #4a9eff;";

                controlsRow.appendChild(playBtn);
                controlsRow.appendChild(prevBtn);
                controlsRow.appendChild(scrubber);
                controlsRow.appendChild(nextBtn);

                const infoLabel = document.createElement("div");
                infoLabel.style.cssText = `
                    font-size: 11px;
                    color: #aaa;
                    margin-top: 5px;
                    font-family: monospace;
                    text-align: center;
                    white-space: nowrap;
                    overflow: hidden;
                    text-overflow: ellipsis;
                `;
                infoLabel.textContent = "Select a Video, GIF, WebP, or Image Sequence...";

                footerBlock.appendChild(controlsRow);
                footerBlock.appendChild(infoLabel);

                container.appendChild(imgWrapper);
                container.appendChild(footerBlock);

                const playerWidget = node.addDOMWidget("media_player", "preview", container, {
                    serialize: false,
                    hideOnZoom: false
                });

                function updatePlayerLayout() {
                    if (!node.size) return;
                    const widgetStartY = playerWidget.last_y || 300;
                    const availableHeight = Math.max(170, node.size[1] - widgetStartY - 16);
                    container.style.height = `${availableHeight}px`;
                }

                playerWidget.computeSize = function (width) {
                    const widgetStartY = playerWidget.last_y || 300;
                    const dynamicHeight = node.size ? Math.max(170, node.size[1] - widgetStartY - 16) : 260;
                    return [width, dynamicHeight];
                };

                const origOnResize = node.onResize;
                node.onResize = function (size) {
                    if (origOnResize) origOnResize.apply(this, arguments);
                    updatePlayerLayout();
                };

                let state = {
                    framesB64: [],
                    durations: [],
                    origWidth: 0,
                    origHeight: 0,
                    fileFormat: "MEDIA",
                    startFrameOffset: 0,
                    totalAvailable: 0,
                    currentFrame: 0,
                    isPlaying: true,
                    timerId: null,
                    loadToken: 0,
                    debounceTimer: null
                };

                function stopTimer() {
                    if (state.timerId) {
                        clearTimeout(state.timerId);
                        state.timerId = null;
                    }
                }

                function showFrame(index) {
                    const total = state.framesB64.length;
                    if (!total) return;
                    state.currentFrame = ((index % total) + total) % total;

                    frameImg.style.display = "block";
                    frameImg.src = state.framesB64[state.currentFrame];
                    scrubber.value = state.currentFrame;

                    const dur = state.durations[state.currentFrame] || 80;
                    const globalIdx = state.startFrameOffset + state.currentFrame;
                    infoLabel.style.color = "#aaa";
                    infoLabel.textContent = `Frame ${state.currentFrame + 1}/${total} (#${globalIdx} of ${state.totalAvailable}, ${dur}ms) | ${state.fileFormat} ${state.origWidth}×${state.origHeight}`;
                }

                function tickNextFrame() {
                    stopTimer();
                    if (!state.isPlaying || state.framesB64.length <= 1) return;
                    const delay = Math.max(20, state.durations[state.currentFrame] || 80);
                    state.timerId = setTimeout(() => {
                        showFrame(state.currentFrame + 1);
                        tickNextFrame();
                    }, delay);
                }

                function setPlaying(play) {
                    state.isPlaying = play;
                    playBtn.textContent = state.isPlaying ? "⏸" : "▶";
                    if (state.isPlaying && state.framesB64.length > 1) {
                        tickNextFrame();
                    } else {
                        stopTimer();
                    }
                }

                playBtn.addEventListener("click", (e) => { e.stopPropagation(); setPlaying(!state.isPlaying); });
                imgWrapper.addEventListener("click", (e) => { e.stopPropagation(); setPlaying(!state.isPlaying); });
                prevBtn.addEventListener("click", (e) => { e.stopPropagation(); setPlaying(false); showFrame(state.currentFrame - 1); });
                nextBtn.addEventListener("click", (e) => { e.stopPropagation(); setPlaying(false); showFrame(state.currentFrame + 1); });
                scrubber.addEventListener("input", (e) => { e.stopPropagation(); setPlaying(false); showFrame(parseInt(e.target.value, 10) || 0); });

                async function loadAnimationIntoPlayer() {
                    const currentToken = ++state.loadToken;
                    setPlaying(false);

                    const getWVal = (name, defVal) => {
                        const w = node.widgets?.find(x => x.name === name);
                        return (w && w.value !== undefined && w.value !== null) ? w.value : defVal;
                    };

                    const filename = String(getWVal("media_source", "")).trim();
                    const customPath = String(getWVal("custom_path", "")).trim();
                    const startFrame = parseInt(getWVal("start_frame", 0), 10) || 0;
                    const maxFrames = parseInt(getWVal("max_frames", 0), 10) || 0;
                    const frameSkip = parseInt(getWVal("frame_skip", 1), 10) || 1;
                    const seqFps = parseFloat(getWVal("sequence_fps", 12.0)) || 12.0;

                    if ((!filename || filename === "none") && !customPath) {
                        frameImg.style.display = "none";
                        infoLabel.style.color = "#aaa";
                        infoLabel.textContent = "No media file or folder selected";
                        return;
                    }

                    infoLabel.style.color = "#aaa";
                    infoLabel.textContent = "Loading preview frames...";
                    try {
                        const params = new URLSearchParams({
                            filename: filename,
                            custom_path: customPath,
                            start_frame: String(startFrame),
                            max_frames: String(maxFrames),
                            frame_skip: String(frameSkip),
                            sequence_fps: String(seqFps)
                        });

                        const resp = await api.fetchApi(`/blackbox_media/load_frames?${params.toString()}`);
                        if (currentToken !== state.loadToken) return;

                        const data = await resp.json();
                        if (!resp.ok || data.error) {
                            state.framesB64 = [];
                            frameImg.style.display = "none";
                            infoLabel.style.color = "#ff7b72";
                            infoLabel.textContent = `⚠️ ${data.error || "File not found"}`;
                            return;
                        }

                        state.origWidth = data.width || 256;
                        state.origHeight = data.height || 256;
                        state.fileFormat = data.file_format || "MEDIA";
                        state.totalAvailable = data.total_available || 0;
                        state.startFrameOffset = data.start_frame || 0;
                        state.durations = data.durations || [];
                        state.framesB64 = data.frames || [];

                        scrubber.max = Math.max(0, state.framesB64.length - 1);
                        scrubber.value = 0;

                        if (state.framesB64.length > 0) {
                            showFrame(0);
                            updatePlayerLayout();
                            setPlaying(true);
                        } else {
                            frameImg.style.display = "none";
                            infoLabel.style.color = "#ff7b72";
                            infoLabel.textContent = "⚠️ No frames available for preview";
                        }
                    } catch (err) {
                        if (currentToken === state.loadToken) {
                            frameImg.style.display = "none";
                            infoLabel.style.color = "#ff7b72";
                            infoLabel.textContent = "⚠️ Server communication error";
                            console.error("[BlackboxMedia]", err);
                        }
                    }
                }

                function scheduleLoadAnimation() {
                    if (state.debounceTimer) clearTimeout(state.debounceTimer);
                    state.debounceTimer = setTimeout(() => {
                        loadAnimationIntoPlayer();
                    }, 80);
                }

                const hookWidget = (wName) => {
                    const w = node.widgets?.find(x => x.name === wName);
                    if (!w) return;
                    const origCallback = w.callback;
                    w.callback = function () {
                        const res = origCallback ? origCallback.apply(this, arguments) : undefined;
                        if (!node._isConfiguringGraph) {
                            scheduleLoadAnimation();
                        }
                        return res;
                    };
                };

                ["media_source", "custom_path", "start_frame", "max_frames", "frame_skip", "sequence_fps"].forEach(hookWidget);

                const origOnConfigure = node.onConfigure;
                node.onConfigure = function () {
                    node._isConfiguringGraph = true;
                    try {
                        if (origOnConfigure) origOnConfigure.apply(this, arguments);
                        // Ensure non-serialized buttons never inherit string values from API JSON inputs array
                        for (const w of (node.widgets || [])) {
                            if (w.type === "button") {
                                w.value = null;
                            }
                        }
                    } finally {
                        setTimeout(() => {
                            node._isConfiguringGraph = false;
                            scheduleLoadAnimation();
                        }, 50);
                    }
                };

                setTimeout(() => {
                    node.setSize([Math.max(node.size[0], 360), Math.max(node.size[1], 580)]);
                    updatePlayerLayout();
                    scheduleLoadAnimation();
                }, 150);

                const origOnRemoved = node.onRemoved;
                node.onRemoved = function () {
                    stopTimer();
                    if (fileInput && fileInput.parentNode) {
                        fileInput.parentNode.removeChild(fileInput);
                    }
                    if (origOnRemoved) origOnRemoved.apply(this, arguments);
                };

                return r;
            };
        }

        // =====================================================================
        // 2. TELEMETRY PANEL FOR BLACKBOX MEDIA INSPECTOR
        // =====================================================================
        if (nodeData.name === "BlackboxMediaInspector") {
            const onNodeCreated = nodeType.prototype.onNodeCreated;
            nodeType.prototype.onNodeCreated = function () {
                const r = onNodeCreated ? onNodeCreated.apply(this, arguments) : undefined;
                const node = this;

                const infoBox = document.createElement("pre");
                infoBox.style.cssText = `
                    background: #12161b;
                    color: #9cdcfe;
                    border: 1px solid #2d3b48;
                    border-radius: 6px;
                    padding: 8px;
                    margin-top: 4px;
                    font-family: monospace;
                    font-size: 11px;
                    line-height: 1.42;
                    white-space: pre-wrap;
                    box-sizing: border-box;
                    width: 100%;
                `;
                infoBox.textContent = "Run workflow to display detailed media stream telemetry and RAM/VAE forecasts...";

                const summaryWidget = node.addDOMWidget("telemetry_summary_box", "info", infoBox, {
                    serialize: false,
                    hideOnZoom: false
                });
                summaryWidget.computeSize = function (width) {
                    return [width, 200];
                };

                const origOnExecuted = node.onExecuted;
                node.onExecuted = function (message) {
                    if (origOnExecuted) origOnExecuted.apply(this, arguments);
                    if (message && message.summary_text && message.summary_text[0]) {
                        infoBox.textContent = message.summary_text[0];
                    }
                };

                setTimeout(() => {
                    node.setSize([Math.max(node.size[0], 430), Math.max(node.size[1], 360)]);
                }, 100);

                return r;
            };
        }

        // =====================================================================
        // 3. FOLDER PICKER & AUTO-CHUNKING QUEUE FOR BLACKBOX MEDIA SAVER
        // =====================================================================
        if (nodeData.name === "BlackboxMediaSaver") {
            const onNodeCreated = nodeType.prototype.onNodeCreated;
            nodeType.prototype.onNodeCreated = function () {
                const r = onNodeCreated ? onNodeCreated.apply(this, arguments) : undefined;
                const node = this;
                node._isConfiguringGraph = false;

                function isGenuineUserClick(event) {
                    if (node._isConfiguringGraph || app.configuringGraph) return false;
                    if (!event) return false;
                    return (event.type === "pointerdown" || event.type === "mousedown" || event.type === "click");
                }

                const browseBtn = node.addWidget("button", "📂 Select Output Folder...", null, async function (value, canvas, n, pos, event) {
                    if (!isGenuineUserClick(event)) return;
                    const outDirWidget = node.widgets?.find(w => w.name === "output_directory");
                    const currentDir = outDirWidget ? String(outDirWidget.value || "") : "";

                    try {
                        browseBtn.name = "⏳ Opening system folder dialog...";
                        node.setDirtyCanvas(true, true);

                        const params = new URLSearchParams({ initial_dir: currentDir });
                        const resp = await api.fetchApi(`/blackbox_media/browse_folder?${params.toString()}`);
                        const data = await resp.json();

                        if (data && data.selected_dir && outDirWidget) {
                            outDirWidget.value = data.selected_dir;
                        }
                    } catch (err) {
                        console.error("[BlackboxMedia] Folder selection error:", err);
                    } finally {
                        browseBtn.name = "📂 Select Output Folder...";
                        node.setDirtyCanvas(true, true);
                    }
                });
                browseBtn.serialize = false;

                const origOnConfigure = node.onConfigure;
                node.onConfigure = function () {
                    node._isConfiguringGraph = true;
                    try {
                        if (origOnConfigure) origOnConfigure.apply(this, arguments);
                        for (const w of (node.widgets || [])) {
                            if (w.type === "button") {
                                w.value = null;
                            }
                        }
                    } finally {
                        setTimeout(() => {
                            node._isConfiguringGraph = false;
                        }, 50);
                    }
                };

                const statusBanner = document.createElement("div");
                statusBanner.style.cssText = `
                    background: #141c24;
                    color: #7ee787;
                    border: 1px solid #2d4536;
                    border-radius: 5px;
                    padding: 6px 8px;
                    margin-top: 4px;
                    font-family: monospace;
                    font-size: 11px;
                    text-align: center;
                    box-sizing: border-box;
                    width: 100%;
                `;
                statusBanner.textContent = "Ready (Transactional Auto-Chunking Active)";

                const bannerWidget = node.addDOMWidget("chunk_status_banner", "status", statusBanner, {
                    serialize: false,
                    hideOnZoom: false
                });
                bannerWidget.computeSize = function (width) {
                    return [width, 36];
                };

                const origOnExecuted = node.onExecuted;
                node.onExecuted = function (message) {
                    if (origOnExecuted) origOnExecuted.apply(this, arguments);

                    if (message && message.chunk_status && message.chunk_status[0]) {
                        statusBanner.textContent = message.chunk_status[0];
                    }

                    if (message && message.has_more_chunks && message.has_more_chunks[0] === true) {
                        statusBanner.style.color = "#ffd33d";
                        setTimeout(async () => {
                            try {
                                await api.fetchApi("/free", {
                                    method: "POST",
                                    headers: { "Content-Type": "application/json" },
                                    body: JSON.stringify({ unload_models: false, free_memory: true })
                                });
                            } catch (e) {}

                            app.queuePrompt(0, 1);
                        }, 150);
                    } else {
                        statusBanner.style.color = "#7ee787";
                    }
                };

                return r;
            };
        }
    }
});
