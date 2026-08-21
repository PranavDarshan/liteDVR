const apiInput = document.querySelector("#api-base");
const defaultApi = window.location.protocol + "//" + window.location.hostname + ":8080/api";
const savedApi = localStorage.getItem("litedvr-api");
// A LAN browser must not inherit the development default localhost endpoint.
apiInput.value = savedApi && !/^https?:\/\/(localhost|127\.0\.0\.1)(:|\/|$)/i.test(savedApi)
  ? savedApi : defaultApi;
let api = apiInput.value.replace(/\/$/, "");
const el = (selector) => document.querySelector(selector);

let monitorItems = [];
let monitorPage = 0;
let archiveSource = null;
let hiddenArchiveSources = JSON.parse(localStorage.getItem("litedvr-hidden-sources") || "[]");
let hiddenArchiveGroups = JSON.parse(localStorage.getItem("litedvr-hidden-groups") || "[]");
let playbackQueue = [];
let playbackIndex = 0;
let currentRecording = null;
let recordingItems = [];
let timelineDate = new Date().toISOString().slice(0, 10);
let seekFrame = 0;
let pendingSeekValue = 0;
let syntheticPreview = false;
let syntheticPlaybackOffset = 0;
let syntheticSourceDuration = 0;
let dayWindows = [];
let currentWindow = null;
let windowDragActive = false;
let monitorWallSignature = "";
const liveSockets = new Set();
let liveStopped = false;
let recordingSocket = null;
let recordingSocketId = null;

function closeRecordingSocket() {
  if (recordingSocket) {
    recordingSocket.onclose = null;
    recordingSocket.close(1000, "playback window changed");
  }
  recordingSocket = null;
  recordingSocketId = null;
}

function openRecordingSocket(recording) {
  closeRecordingSocket();
  if (!recording || !recording.id) return;
  const url = api.replace(/^http/, "ws") + "/recordings/" + recording.id + "/playback";
  const socket = new WebSocket(url);
  recordingSocket = socket;
  recordingSocketId = recording.id;
  socket.onmessage = function(event) {
    try {
      const message = JSON.parse(event.data);
      if (message.type === "error") el("#timeline-status").textContent = message.message;
    } catch (_) {}
  };
  socket.onerror = function() { socket.close(); };
  socket.onclose = function() {
    if (recordingSocket === socket) {
      recordingSocket = null;
      recordingSocketId = null;
    }
  };
}

function sendRecordingSeek(offset) {
  if (recordingSocket && recordingSocket.readyState === WebSocket.OPEN) {
    recordingSocket.send(JSON.stringify({type: "seek", offset: Number(offset) || 0}));
  }
}

function activateTab(name) {
  if (name !== "recordings") closeRecordingSocket();
  document.querySelectorAll(".tab").forEach(function(tab) {
    tab.classList.toggle("active", tab.dataset.tab === name);
  });
  document.querySelectorAll(".tab-panel").forEach(function(panel) {
    panel.classList.toggle("active", panel.dataset.panel === name);
  });
  history.replaceState(null, "", "#" + name);
}

function formatBytes(value) {
  if (!value) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let size = value;
  let index = 0;
  while (size >= 1024 && index < units.length - 1) {
    size /= 1024;
    index += 1;
  }
  return size.toFixed(index ? 1 : 0) + " " + units[index];
}

function formatUptime(seconds) {
  const days = Math.floor(seconds / 86400);
  const hours = Math.floor(seconds % 86400 / 3600);
  return days ? days + "d " + hours + "h" : hours + "h " + Math.floor(seconds % 3600 / 60) + "m";
}

function isPlayable(recording) {
  // Fragmented MP4 files are playable while they are being written once the
  // initial headers/data exist. Avoid opening tiny files before that point.
  return Boolean(recording) && recording.status !== "ERROR" && Number(recording.file_size) >= 65536;
}

function sortPlayables(items) {
  return items.filter(isPlayable).slice().sort(function(a, b) {
    return a.start_time.localeCompare(b.start_time);
  });
}

function recordingBounds(recording) {
  const start = new Date(recording.start_time).getTime();
  const duration = Number(recording.duration) || 0;
  const end = start + Math.max(0, duration * 1000);
  return {start: start, end: end, duration: duration};
}

function formatClock(timestamp) {
  return new Date(timestamp).toLocaleTimeString([], {hour: "2-digit", minute: "2-digit", second: "2-digit"});
}

function prettyTime(value) {
  return new Date(value).toLocaleString();
}

function durationLabel(seconds) {
  if (!seconds) return "In progress";
  const minutes = Math.floor(seconds / 60);
  const remainder = Math.floor(seconds % 60);
  return minutes + "m " + String(remainder).padStart(2, "0") + "s";
}

function isSyntheticPreview(recording, mediaDuration) {
  return Boolean(recording && recording.duration && mediaDuration && mediaDuration > 0 && recording.duration >= 43200 && recording.duration / mediaDuration > 8);
}

function dayStartMs() {
  // Timeline labels are local camera/operator time, not UTC.
  return new Date(timelineDate + "T00:00:00").getTime();
}

function windowStartMs(index) {
  return dayStartMs() + index * 10800 * 1000;
}

function windowLabel(index) {
  return String(Math.floor(index * 3)).padStart(2, "0") + ":00";
}

function buildDayWindows(items) {
  const start = dayStartMs();
  const windowSizeMs = 3 * 3600 * 1000;
  const windows = Array.from({length: 8}, function(_, index) {
    return {index: index, start: start + index * windowSizeMs, end: start + (index + 1) * windowSizeMs, chunks: []};
  });
  items.slice().sort(function(a, b) {
    return a.start_time.localeCompare(b.start_time);
  }).forEach(function(recording) {
    const bounds = recordingBounds(recording);
    const index = Math.max(0, Math.min(7, Math.floor((bounds.start - start) / windowSizeMs)));
    const item = Object.assign({}, recording, {
      window_index: index,
      window_offset_seconds: Math.max(0, (bounds.start - windows[index].start) / 1000),
      window_end_offset_seconds: Math.max(0, (bounds.end - windows[index].start) / 1000)
    });
    windows[index].chunks.push(item);
  });
  return windows;
}

function findWindowForRecording(recording) {
  if (!recording) return null;
  if (typeof recording.window_index === "number" && dayWindows[recording.window_index]) {
    return dayWindows[recording.window_index];
  }
  return dayWindows.find(function(window) {
    return window.chunks.some(function(chunk) { return chunk.id === recording.id; });
  }) || null;
}

function chooseWindow(index, autoplay) {
  closeRecordingSocket();
  currentWindow = dayWindows[index] || null;
  if (!currentWindow) return;
  const playable = currentWindow.chunks.filter(isPlayable).sort(function(a, b) {
    return a.start_time.localeCompare(b.start_time);
  });
  playbackQueue = playable;
  playbackIndex = 0;
  renderSegmentTimeline(currentWindow);
  el("#playback-range-label").textContent = windowLabel(currentWindow.index) + " - " + String(Math.floor((currentWindow.index + 1) * 3)).padStart(2, "0") + ":00";
  if (!playable.length) {
    if (currentWindow.chunks.length) openRecordingSocket(currentWindow.chunks[0]);
    const player = el("#player");
    player.pause();
    player.removeAttribute("src");
    player.load();
    el("#playback-section").hidden = true;
    el("#timeline-status").textContent = currentWindow.chunks.length
      ? "This 3 hour block is still recording; playback is available after the MP4 is finalized."
      : "This 3 hour block has no recorded chunks.";
    return;
  }
  showPlayback(playable[0], playable, 0, autoplay === true);
  openRecordingSocket(playable[0]);
  if (autoplay) {
    el("#player").play().catch(function() {});
  }
}

function renderDayTimeline(windows) {
  const timeline = el("#timeline");
  if (!timeline) return;
  timeline.replaceChildren();
  const playhead = document.createElement("div");
  playhead.id = "timeline-playhead";
  playhead.className = "timeline-playhead";
  playhead.hidden = true;
  const track = document.createElement("div");
  track.className = "timeline-track day-track";
  windows.forEach(function(window) {
    const button = document.createElement("button");
    button.type = "button";
    const hasPlayable = window.chunks.some(isPlayable);
    const hasActive = window.chunks.some(function(chunk) { return chunk.status === "RECORDING"; });
    button.className = "timeline-segment " + (hasPlayable ? "complete" : hasActive ? "active" : "error");
    button.style.left = (window.index * 12.5) + "%";
    button.style.width = "12.5%";
    button.setAttribute("aria-label", windowLabel(window.index) + " - " + String(Math.floor((window.index + 1) * 3)).padStart(2, "0") + ":00");
    const fills = [];
    let cursor = 0;
    window.chunks.slice().sort(function(a, b) {
      return a.start_time.localeCompare(b.start_time);
    }).forEach(function(chunk) {
      const startPercent = Math.max(0, (new Date(chunk.start_time).getTime() - window.start) / (3 * 3600 * 1000) * 100);
      const endPercent = Math.max(0, (new Date(chunk.end_time || chunk.start_time).getTime() - window.start) / (3 * 3600 * 1000) * 100);
      if (startPercent > cursor) fills.push("#373c3a " + cursor + "% " + startPercent + "%");
      fills.push("#a6c98c " + startPercent + "% " + endPercent + "%");
      cursor = Math.max(cursor, endPercent);
    });
    if (!fills.length) fills.push("#373c3a 0% 100%");
    else if (cursor < 100) fills.push("#373c3a " + cursor + "% 100%");
    button.style.backgroundImage = "linear-gradient(to right," + fills.join(",") + ")";
    button.style.backgroundBlendMode = "screen";
    button.onclick = function() {
      chooseWindow(window.index, true);
    };
    const label = document.createElement("span");
    label.textContent = windowLabel(window.index);
    label.setAttribute("aria-hidden", "true");
    button.append(label);
    track.append(button);
  });
  timeline.append(playhead, track);
}

function renderSegmentTimeline(window) {
  const segment = el("#segment-timeline");
  if (!segment || !window) return;
  segment.replaceChildren();
  const start = window.start;
  const windowMs = 10800000;
  segment.style.backgroundImage = "repeating-linear-gradient(to right, transparent 0, transparent calc(16.666% - 1px), rgba(255,255,255,0.12) calc(16.666% - 1px), rgba(255,255,255,0.12) 16.666%)";
  const labelRow = document.createElement("div");
  labelRow.className = "track-labels";
  const windowStartHour = window.index * 3;
  [0, 0.5, 1, 1.5, 2, 2.5, 3].forEach(function(relativeHour) {
    const totalMinutes = Math.round((windowStartHour + relativeHour) * 60);
    const hour = Math.floor(totalMinutes / 60) % 24;
    const minute = totalMinutes % 60;
    const label = String(hour).padStart(2, "0") + ":" + String(minute).padStart(2, "0");
    const span = document.createElement("span");
    span.textContent = label;
    labelRow.append(span);
  });
  const chunksLayer = document.createElement("div");
  chunksLayer.className = "timeline-chunks";
  const ticks = document.createElement("div");
  ticks.className = "timeline-ticks";
  for (let minute = 0; minute <= 180; minute += 10) {
    const tick = document.createElement("i");
    tick.className = "timeline-tick" + (minute % 30 === 0 ? " major" : "");
    tick.style.left = (minute / 180 * 100) + "%";
    tick.setAttribute("aria-hidden", "true");
    ticks.append(tick);
  }
  const gaps = [];
  let cursor = 0;
  window.chunks.filter(isPlayable).slice().sort(function(a, b) {
    return a.start_time.localeCompare(b.start_time);
  }).forEach(function(chunk) {
    const chunkStart = Math.max(0, (new Date(chunk.start_time).getTime() - start) / windowMs * 100);
    const chunkEnd = Math.max(0, (new Date(chunk.end_time || chunk.start_time).getTime() - start) / windowMs * 100);
    if (chunkStart > cursor) {
      gaps.push({left: cursor, width: chunkStart - cursor});
    }
    const bar = document.createElement("button");
    bar.type = "button";
    bar.className = "timeline-chunk";
    bar.style.left = chunkStart + "%";
    bar.style.width = Math.max(0.6, chunkEnd - chunkStart) + "%";
    bar.setAttribute("aria-label", prettyTime(chunk.start_time) + " - " + durationLabel(chunk.duration) + ". Drag anywhere on the bar to seek.");
    chunksLayer.append(bar);
    cursor = Math.max(cursor, chunkEnd);
  });
  if (cursor < 100) gaps.push({left: cursor, width: 100 - cursor});
  gaps.forEach(function(gap) {
    const gapEl = document.createElement("div");
    gapEl.className = "timeline-gap";
    gapEl.style.left = gap.left + "%";
    gapEl.style.width = gap.width + "%";
    chunksLayer.append(gapEl);
  });
  const playhead = document.createElement("div");
  playhead.className = "timeline-playhead";
  playhead.id = "segment-playhead";
  chunksLayer.append(playhead);
  const timeLabel = document.createElement("span");
  timeLabel.id = "timeline-time-label";
  timeLabel.textContent = "--:--:--";
  // Keep the clock outside the recording fill even if an older stylesheet is cached.
  timeLabel.style.position = "absolute";
  timeLabel.style.top = "-62px";
  timeLabel.style.left = "0";
  timeLabel.style.zIndex = "30";
  timeLabel.style.background = "#111313";
  timeLabel.style.padding = "2px 6px 3px 0";
  segment.append(labelRow, chunksLayer, ticks, timeLabel);

  const toSeconds = function(clientX) {
    const rect = segment.getBoundingClientRect();
    const ratio = Math.max(0, Math.min(1, (clientX - rect.left) / rect.width));
    return ratio * 10800;
  };
  const onPointer = function(event) {
    if (!currentWindow) return;
    event.preventDefault();
    const seconds = toSeconds(event.clientX);
    seekWithinWindow(seconds);
  };
  segment.onpointerdown = function(event) {
    windowDragActive = true;
    onPointer(event);
    segment.setPointerCapture(event.pointerId);
  };
  segment.onpointermove = function(event) {
    if (!windowDragActive) return;
    onPointer(event);
  };
  segment.onpointerup = function(event) {
    windowDragActive = false;
    try { segment.releasePointerCapture(event.pointerId); } catch (_) {}
  };
  segment.onpointercancel = function() {
    windowDragActive = false;
  };
}

function seekWithinWindow(seconds) {
  if (!currentWindow) return;
  const target = Math.max(0, Math.min(10800, Number(seconds)));
  const targetMs = currentWindow.start + target * 1000;
  const chunks = currentWindow.chunks.filter(isPlayable).slice().sort(function(a, b) {
    return a.start_time.localeCompare(b.start_time);
  });
  if (!chunks.length) return;
  let choice = null;
  let offset = 0;
  let inGap = true;
  for (const chunk of chunks) {
    const bounds = recordingBounds(chunk);
    if (targetMs >= bounds.start && targetMs <= bounds.end) {
      choice = chunk;
      offset = (targetMs - bounds.start) / 1000;
      inGap = false;
      break;
    }
    if (targetMs < bounds.start) {
      break;
    }
  }
  if (!choice || inGap) {
    el("#timeline-status").textContent = "This time is a recording gap and cannot be seeked.";
    return;
  }
  el("#timeline-status").textContent = "";
  const player = el("#player");
  if (currentRecording && currentRecording.id === choice.id && player.readyState >= 1) {
    const targetOffset = syntheticPreview && player.duration ? offset % player.duration : offset;
    player.currentTime = Math.max(0, Math.min(Number(player.duration) || targetOffset, targetOffset));
    if (!player.paused) player.play().catch(function() {});
    updateDaySelection(choice.start_time, offset);
    sendRecordingSeek(offset);
  } else {
    showPlayback(choice, currentWindow.chunks, offset, !player.paused);
    openRecordingSocket(choice);
    sendRecordingSeek(offset);
  }
}

function text(value) {
  const node = document.createElement("span");
  node.textContent = value;
  return node;
}

function message(value, error) {
  el("#message").textContent = value;
  el("#message").style.color = error ? "#f87171" : "#fbbf24";
}

async function request(path, options) {
  const opts = options || {};
  opts.headers = Object.assign({"Content-Type": "application/json"}, opts.headers || {});
  const response = await fetch(api + path, opts);
  if (!response.ok) throw new Error((await response.text()) || response.statusText);
  return response.status === 204 ? null : response.json();
}

function renderOverview(monitors, system) {
  el("#metric-cameras").textContent = monitors.items.length;
  el("#metric-recording").textContent = system.active_recordings;
  el("#metric-storage").textContent = formatBytes(system.disk.free);
  el("#metric-storage-detail").textContent = formatBytes(system.disk.total) + " total capacity";
  el("#metric-uptime").textContent = formatUptime(system.uptime_seconds);

  const list = el("#overview-monitors");
  list.replaceChildren();
  monitors.items.slice(0, 5).forEach(function(monitor) {
    const card = document.createElement("div");
    card.className = "card";
    const info = document.createElement("span");
    info.textContent = monitor.group_name + " / " + monitor.name;
    const status = text(monitor.status);
    status.className = "status " + monitor.status;
    card.append(info, status);
    list.append(card);
  });
  if (!monitors.items.length) list.append(text("No cameras configured."));
}

function renderMonitorWall() {
  const groupId = el("#monitor-group-filter").value;
  const visible = monitorItems.filter(function(monitor) {
    return !groupId || String(monitor.group_id) === groupId;
  });
  const pageCount = Math.max(1, Math.ceil(visible.length / 4));
  monitorPage = Math.min(monitorPage, pageCount - 1);
  const signature = groupId + "|" + monitorPage + "|" + visible.slice(monitorPage * 4, monitorPage * 4 + 4).map(function(monitor) {
    return monitor.id;
  }).join(",");
  // Background status polling must not tear down live sockets every 10s.
  if (signature === monitorWallSignature && el("#monitor-wall").children.length === Math.min(4, visible.length)) return;
  monitorWallSignature = signature;

  const wall = el("#monitor-wall");
  wall.querySelectorAll(".monitor-tile").forEach(function(tile) {
    if (tile._liveSocket) tile._liveSocket.close();
    if (tile._liveObjectUrl) URL.revokeObjectURL(tile._liveObjectUrl);
  });
  wall.replaceChildren();

  visible.slice(monitorPage * 4, monitorPage * 4 + 4).forEach(function(monitor) {
    const tile = document.createElement("article");
    tile.className = "monitor-tile";
    const image = document.createElement("img");
    image.alt = monitor.name + " live view";
    image.loading = "lazy";
    const header = document.createElement("div");
    header.className = "monitor-tile-header";
    const title = document.createElement("strong");
    title.textContent = monitor.name;
    const status = text(monitor.status);
    status.className = "status " + monitor.status;
    const meta = text(monitor.group_name);
    meta.className = "monitor-meta";
    const fullscreen = document.createElement("button");
    fullscreen.className = "camera-fullscreen-button";
    fullscreen.type = "button";
    fullscreen.textContent = "Full view";
    fullscreen.onclick = function() {
      if (tile.requestFullscreen) tile.requestFullscreen().catch(function() {});
    };

    function connectLive() {
      if (liveStopped) return;
      if (!tile.isConnected) return;
      status.textContent = "CONNECTING";
      status.className = "status OFFLINE";
      const socket = new WebSocket(api.replace(/^http/, "ws") + "/monitors/" + monitor.id + "/live");
      socket.binaryType = "blob";
      tile._liveSocket = socket;
      liveSockets.add(socket);
      socket.onmessage = function(event) {
        if (tile._liveObjectUrl) URL.revokeObjectURL(tile._liveObjectUrl);
        tile._liveObjectUrl = URL.createObjectURL(event.data);
        image.src = tile._liveObjectUrl;
        if (el("#live-view-overlay").dataset.monitorId === String(monitor.id)) {
          el("#live-view-image").src = tile._liveObjectUrl;
        }
        status.textContent = "LIVE";
        status.className = "status RECORDING";
      };
      socket.onerror = function() { socket.close(); };
      socket.onclose = function() {
        liveSockets.delete(socket);
        if (tile.isConnected) {
          status.textContent = "LIVE UNAVAILABLE";
          status.className = "status OFFLINE";
          setTimeout(connectLive, 10000);
        }
      };
    }

    header.append(title, status);
    const actions = document.createElement("div");
    actions.className = "monitor-tile-actions";
    actions.append(fullscreen);
    tile.append(image, header, actions, meta);
    wall.append(tile);
    connectLive();
  });

  if (!visible.length) wall.append(text("No cameras available for this group."));
  el("#monitor-page").textContent = "Page " + (monitorPage + 1) + " of " + pageCount;
  el("#monitor-prev").disabled = monitorPage === 0;
  el("#monitor-next").disabled = monitorPage >= pageCount - 1;
}

function renderRecordingCameraTabs(sources) {
  const tabs = el("#recording-camera-tabs");
  tabs.replaceChildren();

  const all = document.createElement("button");
  all.type = "button";
  all.className = "filter-tab" + (archiveSource ? "" : " active");
  all.textContent = "All cameras";
  all.onclick = function() {
    archiveSource = null;
    renderRecordingCameraTabs(sources);
    loadRecordings();
  };
  tabs.append(all);

  sources.items.filter(function(source) {
    return !hiddenArchiveSources.includes(source.group_name + "\u0000" + source.camera_name);
  }).forEach(function(source) {
    const choice = document.createElement("span");
    choice.className = "filter-choice";
    const tab = document.createElement("button");
    tab.type = "button";
    const selected = archiveSource && archiveSource.group_name === source.group_name && archiveSource.camera_name === source.camera_name;
    tab.className = "filter-tab" + (selected ? " active" : "");
    tab.textContent = source.group_name + " / " + source.camera_name;
    tab.onclick = function() {
      archiveSource = source;
      el("#recording-group").value = "";
      renderRecordingCameraTabs(sources);
      loadRecordings();
    };
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "filter-delete";
    remove.textContent = "x";
    remove.title = "Hide this camera filter";
    remove.setAttribute("aria-label", "Hide " + source.camera_name + " filter");
    remove.onclick = function(event) {
      event.stopPropagation();
      hiddenArchiveSources.push(source.group_name + "\u0000" + source.camera_name);
      localStorage.setItem("litedvr-hidden-sources", JSON.stringify(hiddenArchiveSources));
      if (archiveSource === source) archiveSource = null;
      renderRecordingCameraTabs(sources);
      loadRecordings();
    };
    choice.append(tab, remove);
    tabs.append(choice);
  });
}

function renderRecordingGroupTabs(groups, sources) {
  const tabs = el("#recording-group-tabs");
  tabs.replaceChildren();

  const all = document.createElement("button");
  all.type = "button";
  all.className = "filter-tab" + (!el("#recording-group").value && !archiveSource ? " active" : "");
  all.textContent = "All groups";
  all.onclick = function() {
    archiveSource = null;
    el("#recording-group").value = "";
    renderRecordingGroupTabs(groups, sources);
    renderRecordingCameraTabs(sources);
    loadRecordings();
  };
  tabs.append(all);

  groups.items.filter(function(group) {
    return !hiddenArchiveGroups.includes(group.name);
  }).forEach(function(group) {
    const choice = document.createElement("span");
    choice.className = "filter-choice";
    const tab = document.createElement("button");
    tab.type = "button";
    tab.className = "filter-tab" + (el("#recording-group").value === String(group.id) && !archiveSource ? " active" : "");
    tab.textContent = group.name + " (" + group.monitor_count + ")";
    tab.onclick = function() {
      archiveSource = null;
      el("#recording-group").value = group.id;
      renderRecordingGroupTabs(groups, sources);
      renderRecordingCameraTabs(sources);
      loadRecordings();
    };
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "filter-delete";
    remove.textContent = "x";
    remove.title = "Hide this group filter";
    remove.setAttribute("aria-label", "Hide " + group.name + " filter");
    remove.onclick = function(event) {
      event.stopPropagation();
      hiddenArchiveGroups.push(group.name);
      localStorage.setItem("litedvr-hidden-groups", JSON.stringify(hiddenArchiveGroups));
      if (el("#recording-group").value === String(group.id)) el("#recording-group").value = "";
      renderRecordingGroupTabs(groups, sources);
      loadRecordings();
    };
    choice.append(tab, remove);
    tabs.append(choice);
  });
}

function showPlayback(recording, queue, offset, autoplay) {
  if (!isPlayable(recording)) {
    message("This recording has no playable video data yet.", true);
    return;
  }
  playbackQueue = (queue || [recording]).filter(isPlayable);
  playbackIndex = Math.max(0, playbackQueue.findIndex(function(item) {
    return item.id === recording.id;
  }));
  currentRecording = recording;
  currentWindow = findWindowForRecording(recording) || currentWindow;

  const section = el("#playback-section");
  const player = el("#player");
  el("#playback-title").textContent = recording.group_name + " / " + recording.monitor_name + " — " + prettyTime(recording.start_time) + " — " + durationLabel(recording.duration);
  el("#download").href = api + "/recordings/" + recording.id + "/download";

  const rate = Number(el("#playback-speed").value || "1");
  player.playbackRate = rate;
  player.defaultPlaybackRate = rate;
  syntheticPreview = false;
  syntheticPlaybackOffset = 0;
  syntheticSourceDuration = 0;
  player.loop = false;
  activateRecording(recording, offset || 0, autoplay === true);
  if (currentWindow) renderSegmentTimeline(currentWindow);
  section.hidden = false;
  section.scrollIntoView({behavior: "smooth", block: "start"});
  el("#timeline-status").textContent = "";
}

function playableQueueFor(recording, items) {
  return items.filter(function(item) {
    return isPlayable(item) && item.group_name === recording.group_name && item.monitor_name === recording.monitor_name;
  }).sort(function(a, b) {
    return a.start_time.localeCompare(b.start_time);
  });
}

function globalSeconds(timestamp, offset) {
  return Math.max(0, Math.min(86400, (new Date(timestamp).getTime() - dayStartMs()) / 1000 + (offset || 0)));
}

function updateDaySelection(timestamp, offset) {
  const seconds = globalSeconds(timestamp, offset);
  // The old global seek input was removed in favor of the 3-hour timeline.
  // Keep this update optional so playback cannot fail with a null-element error.
  const legacySeek = el("#playback-seek");
  if (legacySeek) legacySeek.value = seconds;
  el("#playback-clock").textContent = formatClock(dayStartMs() + seconds * 1000);
  updatePlayhead(timestamp);
  if (currentWindow) {
    const playhead = el("#segment-playhead");
    if (playhead) {
      const position = Math.max(0, Math.min(100,
        (new Date(timestamp).getTime() - currentWindow.start) / 10800000 * 100));
      playhead.style.left = position + "%";
      playhead.hidden = false;
    }
  }
  const timeLabel = el("#timeline-time-label");
  if (timeLabel) timeLabel.textContent = formatClock(dayStartMs() + seconds * 1000);
}

function updateGlobalControls(timestamp, offset) {
  updateDaySelection(timestamp, offset);
}

function updatePlayhead(timestamp) {
  const playhead = el("#timeline-playhead");
  if (!playhead) return;
  const start = dayStartMs();
  const position = (new Date(timestamp).getTime() - start) / 86400000 * 100;
  playhead.style.left = Math.max(0, Math.min(100, position)) + "%";
  playhead.hidden = false;
}

function locateRecordingAtSecond(seconds, items) {
  const dayStart = dayStartMs();
  const target = dayStart + Number(seconds) * 1000;
  const ordered = sortPlayables(items);
  let previous = null;

  for (const item of ordered) {
    const bounds = recordingBounds(item);
    if (target >= bounds.start && target <= Math.max(bounds.end, bounds.start + 1000)) {
      return {recording: item, offset: Math.max(0, (target - bounds.start) / 1000), exact: true};
    }
    if (bounds.start > target) {
      return {recording: item, offset: 0, exact: false};
    }
    previous = {recording: item, offset: Math.max(0, bounds.duration), exact: false};
  }
  return previous;
}

function activateRecording(recording, offset, autoplay) {
  const player = el("#player");
  const source = api + "/recordings/" + recording.id + "/stream";
  const shouldPlay = autoplay === true || (autoplay !== false && !player.paused);

  currentRecording = recording;
  el("#timeline-status").textContent = "Loading recording…";
  if (player.src !== source || player.error) {
    player.src = source;
    player.load();
  }

  player.onloadedmetadata = function() {
    syntheticSourceDuration = Number(player.duration) || 0;
    syntheticPreview = isSyntheticPreview(recording, syntheticSourceDuration);
    syntheticPlaybackOffset = Math.max(0, offset || 0);
    if (syntheticPreview) {
      el("#timeline-status").textContent = "Synthetic 24h preview: this day is looped from a shorter clip.";
    }
    if (typeof offset === "number" && Number.isFinite(offset)) {
      const target = syntheticPreview && syntheticSourceDuration ? Math.max(0, offset % syntheticSourceDuration) : Math.max(0, offset);
      if (typeof player.fastSeek === "function") {
        player.fastSeek(target);
      } else {
        player.currentTime = target;
      }
    }
    if (shouldPlay) player.play().catch(function() {});
    if (!syntheticPreview) el("#timeline-status").textContent = "";
  };

  if (player.readyState >= 1 && typeof offset === "number" && Number.isFinite(offset)) {
    syntheticSourceDuration = Number(player.duration) || 0;
    syntheticPreview = isSyntheticPreview(recording, syntheticSourceDuration);
    syntheticPlaybackOffset = Math.max(0, offset || 0);
    const target = syntheticPreview && syntheticSourceDuration ? Math.max(0, offset % syntheticSourceDuration) : Math.max(0, offset);
    if (typeof player.fastSeek === "function") {
      player.fastSeek(target);
    } else {
      player.currentTime = target;
    }
  }
  if (shouldPlay) player.play().catch(function() {});
  updateDaySelection(recording.start_time, offset || 0);
}

function seekGlobal(seconds) {
  if (!recordingItems.length) return;
  const choice = locateRecordingAtSecond(seconds, recordingItems);
  if (!choice || !choice.recording) {
    el("#timeline-status").textContent = "No recording available at " + formatClock(dayStartMs() + Number(seconds) * 1000);
    return;
  }
  playbackIndex = playbackQueue.findIndex(function(item) {
    return item.id === choice.recording.id;
  });
  if (playbackIndex < 0) playbackIndex = 0;
  if (!currentRecording || currentRecording.id !== choice.recording.id) {
    playbackQueue = playableQueueFor(choice.recording, recordingItems);
    playbackIndex = Math.max(0, playbackQueue.findIndex(function(item) {
      return item.id === choice.recording.id;
    }));
    activateRecording(choice.recording, choice.offset, !el("#player").paused);
  } else {
    const player = el("#player");
    syntheticPlaybackOffset = Math.max(0, Number(seconds) - globalSeconds(choice.recording.start_time, 0));
    if (player.readyState >= 1) {
      const target = syntheticPreview && syntheticSourceDuration ? Math.max(0, choice.offset % syntheticSourceDuration) : Math.max(0, choice.offset);
      if (typeof player.fastSeek === "function") {
        player.fastSeek(target);
      } else {
        player.currentTime = target;
      }
    } else {
      player.onloadedmetadata = function() {
        const target = syntheticPreview && syntheticSourceDuration ? Math.max(0, choice.offset % syntheticSourceDuration) : Math.max(0, choice.offset);
        if (typeof player.fastSeek === "function") {
          player.fastSeek(target);
        } else {
          player.currentTime = target;
        }
      };
    }
    updateDaySelection(choice.recording.start_time, choice.offset);
    if (!player.paused) player.play().catch(function() {});
  }
  el("#timeline-status").textContent = choice.exact ? "" : "Jumped to the nearest available segment.";
}

function requestSeek(seconds) {
  pendingSeekValue = Number(seconds);
  if (seekFrame) return;
  seekFrame = requestAnimationFrame(function() {
    seekFrame = 0;
    seekGlobal(pendingSeekValue);
  });
}

function renderDayTimelineLegacy(items) {
  const timeline = el("#timeline");
  if (!timeline) return;
  timeline.replaceChildren();

  const playhead = document.createElement("div");
  playhead.id = "timeline-playhead";
  playhead.className = "timeline-playhead";
  playhead.hidden = true;
  el("#timeline-date-label").textContent = timelineDate;

  const labels = document.createElement("div");
  labels.className = "timeline-labels";
  ["00:00", "06:00", "12:00", "18:00", "24:00"].forEach(function(label) {
    const item = document.createElement("span");
    item.textContent = label;
    labels.append(item);
  });

  const track = document.createElement("div");
  track.className = "timeline-track day-track";
  const dayStart = dayStartMs();
  const dayLength = 86400000;

  items.slice().sort(function(a, b) {
    return a.start_time.localeCompare(b.start_time);
  }).forEach(function(recording) {
    const start = Math.max(0, new Date(recording.start_time).getTime() - dayStart);
    const duration = Math.max(1000, (recording.duration || 0) * 1000);
    const segment = document.createElement("button");
    segment.type = "button";
    segment.className = "timeline-segment " + (recording.status === "ERROR" ? "error" : recording.status === "RECORDING" ? "active" : recording.status === "INTERRUPTED" ? "partial" : "complete");
    segment.style.left = Math.min(100, start / dayLength * 100) + "%";
    segment.style.width = Math.min(100, duration / dayLength * 100) + "%";
    segment.setAttribute("aria-label", prettyTime(recording.start_time) + " - " + durationLabel(recording.duration));
    segment.textContent = recording.monitor_name;
    segment.onclick = function() {
      showPlayback(recording, playableQueueFor(recording, items));
    };
    track.append(segment);
  });

  if (!items.length) {
    const empty = document.createElement("div");
    empty.className = "timeline-empty";
    empty.textContent = "No recordings were found for this day.";
    track.append(empty);
  }

  timeline.append(labels, playhead, track);
}

async function loadRecordings() {
  try {
    closeRecordingSocket();
    const params = new URLSearchParams({limit: "500", sort: el("#recording-sort").value});
    if (archiveSource) {
      params.set("group_name", archiveSource.group_name);
      params.set("camera_name", archiveSource.camera_name);
    } else if (el("#recording-group").value) {
      params.set("group_id", el("#recording-group").value);
    }
    if (el("#recording-date").value) params.set("date", el("#recording-date").value);
    params.set("tz_offset_minutes", String(new Date().getTimezoneOffset()));

    const result = await request("/recordings?" + params.toString());
    // A timeline is always scoped to one camera. Never merge multiple camera
    // clocks into a single seek track.
    if (!archiveSource) {
      recordingItems = [];
      dayWindows = [];
      currentWindow = null;
      const segment = el("#segment-timeline");
      if (segment) segment.replaceChildren();
      el("#timeline-status").textContent = "Select one camera above to load its timeline.";
      renderDayTimeline([]);
      const recentAll = el("#recent-recordings");
      recentAll.replaceChildren();
      result.items.slice(0, 4).forEach(function(recording) {
        const card = document.createElement("div");
        card.className = "card";
        const info = text(recording.monitor_name + " / " + prettyTime(recording.start_time) + " · " + durationLabel(recording.duration));
        card.append(info);
        recentAll.append(card);
      });
      if (!result.items.length) recentAll.append(text("No recent recordings."));
      return;
    }
    let timelineItems = result.items;
    if (archiveSource && archiveSource.monitor_id) {
      const dayTimeline = await request("/timeline?monitor_id=" + archiveSource.monitor_id + "&date=" + timelineDate + "&tz_offset_minutes=" + new Date().getTimezoneOffset());
      timelineItems = dayTimeline.recordings.map(function(item) {
        return Object.assign({}, item, {start_time: item.start, end_time: item.end});
      });
    }
    recordingItems = timelineItems;
    dayWindows = buildDayWindows(timelineItems);
    renderDayTimeline(dayWindows);
    const preferredWindow = currentWindow && dayWindows[currentWindow.index] && dayWindows[currentWindow.index].chunks.length ? currentWindow.index : dayWindows.findIndex(function(window) {
      return window.chunks.length;
    });
    if (preferredWindow >= 0) {
      chooseWindow(preferredWindow, false);
    } else {
      currentWindow = null;
      const segment = el("#segment-timeline");
      if (segment) segment.replaceChildren();
    }

    const recent = el("#recent-recordings");
    recent.replaceChildren();
    result.items.slice(0, 4).forEach(function(recording) {
      const card = document.createElement("div");
      card.className = "card";
      const info = text(recording.monitor_name + " / " + prettyTime(recording.start_time) + " · " + durationLabel(recording.duration));
      const play = document.createElement("button");
      play.textContent = isPlayable(recording) ? "Play" : "Unavailable";
      play.disabled = !isPlayable(recording);
      play.onclick = function() {
        showPlayback(recording, playableQueueFor(recording, result.items));
      };
      card.append(info, play);
      recent.append(card);
    });
    if (!result.items.length) recent.append(text("No recent recordings."));
    el("#timeline-status").textContent = result.items.length ? "" : "No recordings match these filters.";
  } catch (error) {
    message(error.message, true);
  }
}

async function refresh(reloadRecordings) {
  try {
    const values = await Promise.all([
      request("/health"),
      request("/groups"),
      request("/monitors"),
      request("/settings"),
      request("/system/status"),
      request("/recording-sources")
    ]);
    const health = values[0];
    const groups = values[1];
    const monitors = values[2];
    const settings = values[3];
    const system = values[4];
    const sources = values[5];
    monitorItems = monitors.items;

    const currentKeys = new Set(monitors.items.map(function(monitor) {
      return monitor.group_name + "\u0000" + monitor.name;
    }));
    hiddenArchiveSources = hiddenArchiveSources.filter(function(key) {
      return !currentKeys.has(key);
    });
    localStorage.setItem("litedvr-hidden-sources", JSON.stringify(hiddenArchiveSources));

    const sourceMap = new Map(sources.items.map(function(source) {
      return [source.group_name + "\u0000" + source.camera_name, source];
    }));
    monitors.items.forEach(function(monitor) {
      const key = monitor.group_name + "\u0000" + monitor.name;
      sourceMap.set(key, {
        group_id: monitor.group_id,
        monitor_id: monitor.id,
        group_name: monitor.group_name,
        camera_name: monitor.name,
        latest: null
      });
    });
    sources.items = Array.from(sourceMap.values());
    if (!archiveSource && sources.items.length) archiveSource = sources.items[0];

    el("#server").textContent = health.status === "ok" ? "Backend connected" : "Backend unavailable";
    renderOverview(monitors, system);

    const groupSelect = el("#monitor-group");
    const groupList = el("#groups");
    groupSelect.replaceChildren();
    groupList.replaceChildren();
    groups.items.forEach(function(group) {
      const option = document.createElement("option");
      option.value = group.id;
      option.textContent = group.name + " (" + group.monitor_count + ")";
      groupSelect.append(option);

      const card = document.createElement("div");
      card.className = "card";
      card.append(text(group.name + " - " + group.monitor_count + " camera(s)"));
      const remove = document.createElement("button");
      remove.textContent = "Delete";
      remove.className = "danger";
      remove.onclick = async function() {
        try {
          await request("/groups/" + group.id + "?cascade=1", {method: "DELETE"});
          // Remove the group and its cameras from the visible state immediately;
          // the next background poll will reconcile with the server.
          if (el("#recording-group").value === String(group.id)) el("#recording-group").value = "";
          if (archiveSource && archiveSource.group_id === group.id) archiveSource = null;
          await refresh();
        } catch (error) {
          message(error.message, true);
        }
      };
      card.append(remove);
      groupList.append(card);
    });

    const selectedGroup = el("#recording-group").value;
    const recordingGroup = el("#recording-group");
    recordingGroup.replaceChildren();
    const allGroups = document.createElement("option");
    allGroups.value = "";
    allGroups.textContent = "All groups";
    recordingGroup.append(allGroups);
    groups.items.forEach(function(group) {
      const option = document.createElement("option");
      option.value = group.id;
      option.textContent = group.name;
      recordingGroup.append(option);
    });
    recordingGroup.value = selectedGroup;

    const monitorGroupFilter = el("#monitor-group-filter");
    const selectedMonitorGroup = monitorGroupFilter.value;
    monitorGroupFilter.replaceChildren();
    const allMonitorGroups = document.createElement("option");
    allMonitorGroups.value = "";
    allMonitorGroups.textContent = "All groups";
    monitorGroupFilter.append(allMonitorGroups);
    groups.items.forEach(function(group) {
      const option = document.createElement("option");
      option.value = group.id;
      option.textContent = group.name;
      monitorGroupFilter.append(option);
    });
    monitorGroupFilter.value = selectedMonitorGroup;
    renderMonitorWall();

    const monitorList = el("#monitors");
    monitorList.replaceChildren();
    monitors.items.forEach(function(monitor) {
      const card = document.createElement("div");
      card.className = "card";
      const info = document.createElement("div");
      info.append(text(monitor.group_name + " / " + monitor.name + " - "));
      const status = text(monitor.status);
      status.className = "status " + monitor.status;
      info.append(status);
      const remove = document.createElement("button");
      remove.textContent = "Remove";
      remove.className = "danger";
      remove.onclick = async function() {
        if (confirm("Remove " + monitor.name + "?")) {
          await request("/monitors/" + monitor.id, {method: "DELETE"});
          monitorItems = monitorItems.filter(function(item) { return item.id !== monitor.id; });
          if (archiveSource && archiveSource.monitor_id === monitor.id) archiveSource = null;
          await refresh();
        }
      };
      card.append(info, remove);
      monitorList.append(card);
    });

    renderRecordingGroupTabs(groups, sources);
    renderRecordingCameraTabs(sources);
    if (!monitors.items.length) monitorList.append(text("No cameras configured."));

    el("#settings-form").retention_days.value = settings.retention_days;
    el("#settings-form").recordings_path.value = settings.recordings_path;
    message("", false);
    if (reloadRecordings !== false) await loadRecordings();
  } catch (error) {
    el("#server").textContent = "Backend unavailable";
    message(error.message, true);
  }
}

document.querySelectorAll(".tab").forEach(function(tab) {
  tab.onclick = function() {
    activateTab(tab.dataset.tab);
  };
});

el("#monitor-group-filter").onchange = function() {
  monitorPage = 0;
  renderMonitorWall();
};
el("#monitor-prev").onclick = function() {
  monitorPage -= 1;
  renderMonitorWall();
};
el("#monitor-next").onclick = function() {
  monitorPage += 1;
  renderMonitorWall();
};

el("#monitor-fullscreen").onclick = function() {
  const panel = document.querySelector('[data-panel="monitor"]');
  const active = panel.classList.toggle("monitor-tab-fullscreen");
  this.textContent = active ? "Exit wall" : "Full wall";
  if (active && document.documentElement.requestFullscreen) {
    document.documentElement.requestFullscreen().catch(function() {});
  } else if (!active && document.fullscreenElement && document.exitFullscreen) {
    document.exitFullscreen().catch(function() {});
  }
};
document.addEventListener("fullscreenchange", function() {
  const panel = document.querySelector('[data-panel="monitor"]');
  const button = el("#monitor-fullscreen");
  if (panel && document.fullscreenElement === null && panel.classList.contains("monitor-tab-fullscreen")) {
    panel.classList.remove("monitor-tab-fullscreen");
    if (button) button.textContent = "Full wall";
  }
});

el("#stop-live").onclick = function() {
  liveStopped = true;
  liveSockets.forEach(function(socket) { try { socket.close(1000, "operator stopped live view"); } catch (_) {} });
  liveSockets.clear();
  document.querySelectorAll(".monitor-tile .status").forEach(function(status) {
    status.textContent = "STOPPED";
    status.className = "status OFFLINE";
  });
};

function closeAllSocketsForPageExit() {
  closeRecordingSocket();
  liveSockets.forEach(function(socket) {
    try { socket.close(1000, "page closed"); } catch (_) {}
  });
  liveSockets.clear();
}
window.addEventListener("pagehide", closeAllSocketsForPageExit);
window.addEventListener("beforeunload", closeAllSocketsForPageExit);

el("#start-live").onclick = function() {
  liveStopped = false;
  monitorWallSignature = "";
  renderMonitorWall();
};

document.querySelectorAll(".jump-cameras").forEach(function(button) {
  button.onclick = function() {
    activateTab("cameras");
  };
});
document.querySelectorAll(".jump-recordings").forEach(function(button) {
  button.onclick = function() {
    activateTab("recordings");
  };
});

const initialTab = location.hash.slice(1);
activateTab(["overview", "monitor", "cameras", "recordings", "settings"].includes(initialTab) ? initialTab : "overview");

el("#save-api").onclick = function() {
  api = apiInput.value.replace(/\/$/, "");
  localStorage.setItem("litedvr-api", api);
  refresh();
};

el("#group-form").onsubmit = async function(event) {
  event.preventDefault();
  try {
    await request("/groups", {method: "POST", body: JSON.stringify({name: el("#group-name").value})});
    event.target.reset();
    await refresh();
  } catch (error) {
    message(error.message, true);
  }
};

el("#monitor-form").onsubmit = async function(event) {
  event.preventDefault();
  const form = new FormData(event.target);
  const payload = Object.fromEntries(form);
  payload.group_id = Number(payload.group_id);
  payload.segment_minutes = 180;
  payload.recording_enabled = form.has("recording_enabled");
  try {
    await request("/monitors", {method: "POST", body: JSON.stringify(payload)});
    event.target.reset();
    await refresh();
  } catch (error) {
    message(error.message, true);
  }
};

el("#settings-form").onsubmit = async function(event) {
  event.preventDefault();
  const form = new FormData(event.target);
  try {
    await request("/settings", {
      method: "PUT",
      body: JSON.stringify({
        retention_days: Number(form.get("retention_days")),
        recordings_path: form.get("recordings_path")
      })
    });
    message("Settings saved.", false);
    await refresh();
  } catch (error) {
    message(error.message, true);
  }
};

// Keep status and camera lists current without requiring a browser refresh.
setInterval(function() {
  if (!document.hidden) refresh(false);
}, 10000);

el("#recording-group").onchange = function() {
  archiveSource = null;
  loadRecordings();
};

el("#recording-filters").onsubmit = function(event) {
  event.preventDefault();
  loadRecordings();
};

el("#recording-date").value = timelineDate;
el("#recording-date").onchange = function() {
  timelineDate = el("#recording-date").value || new Date().toISOString().slice(0, 10);
  loadRecordings();
};

el("#previous-day").onclick = function() {
  const date = new Date(timelineDate + "T00:00:00");
  date.setUTCDate(date.getUTCDate() - 1);
  timelineDate = date.toISOString().slice(0, 10);
  el("#recording-date").value = timelineDate;
  loadRecordings();
};

el("#today-timeline").onclick = function() {
  timelineDate = new Date().toISOString().slice(0, 10);
  el("#recording-date").value = timelineDate;
  loadRecordings();
};

el("#next-day").onclick = function() {
  const date = new Date(timelineDate + "T00:00:00");
  date.setUTCDate(date.getUTCDate() + 1);
  timelineDate = date.toISOString().slice(0, 10);
  el("#recording-date").value = timelineDate;
  loadRecordings();
};

el("#close-player").onclick = function() {
  closeRecordingSocket();
  el("#player").pause();
  el("#player").removeAttribute("src");
  el("#playback-section").hidden = true;
};

el("#player").onplay = function() {
  el("#playback-toggle").textContent = "Pause";
};

el("#player").onpause = function() {
  el("#playback-toggle").textContent = "Play";
};
el("#player").onwaiting = function() {
  el("#timeline-status").textContent = "Loading recording data…";
};
el("#player").oncanplay = function() {
  if (!el("#playback-section").hidden) el("#timeline-status").textContent = "";
};
el("#player").onerror = function() {
  el("#timeline-status").textContent = "The recording is not ready yet. Try the selected chunk again.";
};

el("#player").onended = function() {
  playbackIndex += 1;
  if (playbackIndex < playbackQueue.length) {
    const next = playbackQueue[playbackIndex];
    el("#playback-title").textContent = next.group_name + " / " + next.monitor_name + " — " + prettyTime(next.start_time);
    showPlayback(next, playbackQueue);
    el("#player").play().catch(function() {});
  }
};

el("#player").ontimeupdate = function() {
  const current = currentRecording;
  if (current) {
    const timestamp = new Date(new Date(current.start_time).getTime() + el("#player").currentTime * 1000).toISOString();
    // `timestamp` already includes the media offset; do not add currentTime again.
    updateGlobalControls(timestamp, 0);
  }
};

el("#playback-toggle").onclick = function() {
  const player = el("#player");
  if (player.paused) {
    player.play().catch(function() {});
  } else {
    player.pause();
  }
};

el("#playback-speed").onchange = function() {
  const rate = Number(this.value);
  const player = el("#player");
  player.playbackRate = rate;
  player.defaultPlaybackRate = rate;
};

const legacyPlaybackSeek = el("#playback-seek");
if (legacyPlaybackSeek) {
  legacyPlaybackSeek.oninput = function() {
    requestSeek(this.value);
  };
}

el("#live-view-overlay").onclick = function(event) {
  if (event.target.id === "live-view-overlay") {
    el("#close-live-view").click();
  }
};

el("#close-live-view").onclick = function() {
  const overlay = el("#live-view-overlay");
  overlay.hidden = true;
  delete overlay.dataset.monitorId;
  el("#live-view-image").removeAttribute("src");
};

el("#player").onended = function() {
  playbackIndex += 1;
  if (playbackIndex < playbackQueue.length) {
    const next = playbackQueue[playbackIndex];
    el("#playback-title").textContent = next.group_name + " / " + next.monitor_name + " - " + prettyTime(next.start_time);
    showPlayback(next, playbackQueue);
    el("#player").play().catch(function() {});
  } else if (syntheticPreview && syntheticSourceDuration) {
    syntheticPlaybackOffset += syntheticSourceDuration;
    const player = el("#player");
    if (typeof player.fastSeek === "function") {
      player.fastSeek(0);
    } else {
      player.currentTime = 0;
    }
    player.play().catch(function() {});
  }
};

el("#player").ontimeupdate = function() {
  const current = currentRecording;
  if (current) {
    const mediaOffset = syntheticPreview ? (syntheticPlaybackOffset + el("#player").currentTime) : el("#player").currentTime;
    const timestamp = new Date(new Date(current.start_time).getTime() + mediaOffset * 1000).toISOString();
    // `timestamp` already includes the media offset; do not add it twice.
    updateGlobalControls(timestamp, 0);
  }
};

refresh();
