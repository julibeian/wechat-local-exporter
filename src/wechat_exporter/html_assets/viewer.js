(function () {
  "use strict";

  const WINDOW_SIZE = 160;
  const MAX_SAVED_RESULTS = 500;
  const pendingChunks = new Map();
  const earlyChunks = new Map();
  let manifest = null;

  window.WeChatArchive = Object.freeze({
    setManifest(value) {
      manifest = value;
    },
    registerChunk(id, records) {
      const pending = pendingChunks.get(id);
      if (pending) {
        pendingChunks.delete(id);
        pending.resolve(records);
        return;
      }
      earlyChunks.set(id, records);
    },
  });

  document.addEventListener("DOMContentLoaded", function () {
    if (!manifest || !Array.isArray(manifest.chunks)) {
      showFatalError("归档索引无法读取。请确认 data/manifest.js 与 index.html 位于同一归档目录。");
      return;
    }
    initializeViewer();
  });

  function initializeViewer() {
    const elements = collectElements();
    const participantMap = new Map(
      (manifest.participants || []).map(function (participant) {
        return [participant.id, participant];
      })
    );
    const state = {
      elements,
      participantMap,
      currentChunkIndex: -1,
      currentRecords: [],
      windowStart: 0,
      searchToken: 0,
      highlightedId: "",
      printChunkIndex: -1,
    };

    fillSummary(elements);
    fillFilters(elements, participantMap);
    fillChunkSelect(elements);
    bindControls(state);
    cycleTheme(elements.themeButton, "auto", true);

    if (manifest.chunks.length === 0) {
      renderEmpty(state, "归档中没有可显示的消息。");
      return;
    }
    loadAndShowChunk(state, manifest.chunks.length - 1, { anchor: "end" });
  }

  function collectElements() {
    return {
      sidebar: byId("sidebar"),
      sidebarToggle: byId("sidebar-toggle"),
      filterForm: byId("filter-form"),
      searchInput: byId("search-input"),
      searchButton: byId("search-button"),
      clearButton: byId("clear-button"),
      memberFilter: byId("member-filter"),
      typeFilter: byId("type-filter"),
      dateFrom: byId("date-from"),
      dateTo: byId("date-to"),
      searchResults: byId("search-results"),
      searchCount: byId("search-count"),
      searchHelp: byId("search-help"),
      messageList: byId("message-list"),
      emptyState: byId("empty-state"),
      viewStatus: byId("view-status"),
      previousChunk: byId("previous-chunk"),
      nextChunk: byId("next-chunk"),
      firstChunk: byId("first-chunk"),
      latestChunk: byId("latest-chunk"),
      chunkSelect: byId("chunk-select"),
      earlierWindow: byId("earlier-window"),
      laterWindow: byId("later-window"),
      printButton: byId("print-button"),
      printDialog: byId("print-dialog"),
      printDescription: byId("print-description"),
      printCancel: byId("print-cancel"),
      printConfirm: byId("print-confirm"),
      printDocument: byId("print-document"),
      printRange: byId("print-range"),
      printCount: byId("print-count"),
      printGenerated: byId("print-generated"),
      printScopeNote: byId("print-scope-note"),
      printMessageList: byId("print-message-list"),
      themeButton: byId("theme-button"),
      dialog: byId("media-dialog"),
      dialogImage: byId("dialog-image"),
      dialogCaption: byId("dialog-caption"),
    };
  }

  function fillSummary(elements) {
    const summary = manifest.summary || {};
    setText("summary-messages", formatNumber(summary.messages || 0));
    const firstDate = summary.first_date || "日期未知";
    const lastDate = summary.last_date || "日期未知";
    setText("summary-range", firstDate === lastDate ? firstDate : firstDate + " 至 " + lastDate);
    setText("summary-chunks", formatNumber(summary.chunks || 0));
    setText(
      "summary-media",
      formatNumber(summary.media_exported || 0) + " 可用 / " + formatNumber(summary.media_missing || 0) + " 缺失"
    );
    setText("summary-generated", readableDateTime(manifest.generated_at));
    if (summary.first_date) {
      elements.dateFrom.min = summary.first_date;
      elements.dateFrom.max = summary.last_date;
      elements.dateTo.min = summary.first_date;
      elements.dateTo.max = summary.last_date;
    }
  }

  function fillFilters(elements, participantMap) {
    Array.from(participantMap.values())
      .sort(function (a, b) {
        return String(a.name || a.id).localeCompare(String(b.name || b.id), "zh-CN");
      })
      .forEach(function (participant) {
        const option = document.createElement("option");
        option.value = participant.id;
        option.textContent = participant.name || participant.id;
        elements.memberFilter.appendChild(option);
      });

    (manifest.message_types || []).forEach(function (item) {
      const option = document.createElement("option");
      option.value = item.type;
      option.textContent = item.label + "（" + formatNumber(item.count) + "）";
      elements.typeFilter.appendChild(option);
    });
  }

  function fillChunkSelect(elements) {
    manifest.chunks.forEach(function (chunk, index) {
      const option = document.createElement("option");
      option.value = String(index);
      option.textContent = chunkLabel(chunk, index);
      elements.chunkSelect.appendChild(option);
    });
  }

  function bindControls(state) {
    const elements = state.elements;
    elements.filterForm.addEventListener("submit", function (event) {
      event.preventDefault();
      runArchiveSearch(state);
    });
    elements.clearButton.addEventListener("click", function () {
      elements.filterForm.reset();
      elements.searchResults.replaceChildren();
      elements.searchCount.textContent = "0";
      elements.searchHelp.textContent = "输入关键词或选择筛选条件后搜索全部分块。";
      state.searchToken += 1;
      renderWindow(state, { anchor: "end" });
    });
    elements.memberFilter.addEventListener("change", function () {
      runArchiveSearch(state);
    });
    elements.typeFilter.addEventListener("change", function () {
      runArchiveSearch(state);
    });
    elements.dateFrom.addEventListener("change", function () {
      runArchiveSearch(state);
    });
    elements.dateTo.addEventListener("change", function () {
      runArchiveSearch(state);
    });
    elements.previousChunk.addEventListener("click", function () {
      loadAndShowChunk(state, state.currentChunkIndex - 1, { anchor: "end" });
    });
    elements.nextChunk.addEventListener("click", function () {
      loadAndShowChunk(state, state.currentChunkIndex + 1, { anchor: "start" });
    });
    elements.firstChunk.addEventListener("click", function () {
      loadAndShowChunk(state, 0, { anchor: "start" });
    });
    elements.latestChunk.addEventListener("click", function () {
      loadAndShowChunk(state, manifest.chunks.length - 1, { anchor: "end" });
    });
    elements.chunkSelect.addEventListener("change", function () {
      loadAndShowChunk(state, Number(elements.chunkSelect.value), { anchor: "start" });
    });
    elements.earlierWindow.addEventListener("click", function () {
      state.windowStart = Math.max(0, state.windowStart - WINDOW_SIZE);
      renderWindow(state);
      scrollMessagesToTop();
    });
    elements.laterWindow.addEventListener("click", function () {
      state.windowStart = Math.min(
        Math.max(0, state.currentRecords.length - 1),
        state.windowStart + WINDOW_SIZE
      );
      renderWindow(state);
      scrollMessagesToTop();
    });
    elements.printButton.addEventListener("click", function () {
      updatePrintDescription(state);
      if (typeof elements.printDialog.showModal === "function") {
        elements.printDialog.showModal();
      }
    });
    elements.printCancel.addEventListener("click", function () {
      elements.printDialog.close();
    });
    elements.printConfirm.addEventListener("click", function () {
      printCurrentChunk(state);
    });
    elements.sidebarToggle.addEventListener("click", function () {
      const open = elements.sidebar.classList.toggle("open");
      elements.sidebarToggle.setAttribute("aria-expanded", String(open));
    });
    document.addEventListener("keydown", function (event) {
      if (event.key === "Escape" && elements.sidebar.classList.contains("open")) {
        elements.sidebar.classList.remove("open");
        elements.sidebarToggle.setAttribute("aria-expanded", "false");
        elements.sidebarToggle.focus();
      }
    });
    elements.themeButton.addEventListener("click", function () {
      const current = document.documentElement.dataset.theme || "auto";
      const next = current === "auto" ? "light" : current === "light" ? "dark" : "auto";
      cycleTheme(elements.themeButton, next, false);
    });
    window.addEventListener("beforeprint", function () {
      // Ctrl+P must never silently print only the 160-message screen window.
      // This synchronous preparation uses the already loaded current chunk.
      preparePrintDocument(state);
    });
  }

  async function loadAndShowChunk(state, index, options) {
    if (index < 0 || index >= manifest.chunks.length) {
      return;
    }
    const chunk = manifest.chunks[index];
    state.elements.viewStatus.textContent = "正在加载 " + chunkLabel(chunk, index) + "…";
    state.elements.printButton.disabled = true;
    setNavigationDisabled(state.elements, true);
    try {
      const records = await loadChunkData(chunk);
      state.currentChunkIndex = index;
      state.currentRecords = records;
      clearPreparedPrint(state);
      state.highlightedId = options && options.highlightedId ? options.highlightedId : "";
      state.elements.chunkSelect.value = String(index);
      renderWindow(state, options || { anchor: "start" });
      setText("summary-current-chunk", String(index + 1) + " / " + String(manifest.chunks.length));
    } catch (error) {
      renderEmpty(state, "消息分块无法读取。请确认归档目录没有缺少文件。", error);
    } finally {
      setNavigationDisabled(state.elements, false);
      state.elements.printButton.disabled = state.currentChunkIndex < 0;
      updateNavigation(state);
    }
  }

  function loadChunkData(chunk) {
    if (earlyChunks.has(chunk.id)) {
      const records = earlyChunks.get(chunk.id);
      earlyChunks.delete(chunk.id);
      return Promise.resolve(records);
    }
    if (pendingChunks.has(chunk.id)) {
      return pendingChunks.get(chunk.id).promise;
    }

    let resolvePromise;
    let rejectPromise;
    const promise = new Promise(function (resolve, reject) {
      resolvePromise = resolve;
      rejectPromise = reject;
    });
    pendingChunks.set(chunk.id, {
      promise,
      resolve: resolvePromise,
      reject: rejectPromise,
    });
    const script = document.createElement("script");
    script.src = chunk.path;
    script.async = true;
    script.addEventListener("error", function () {
      const pending = pendingChunks.get(chunk.id);
      pendingChunks.delete(chunk.id);
      if (pending) {
        pending.reject(new Error("chunk load failed: " + chunk.id));
      }
      script.remove();
    });
    script.addEventListener("load", function () {
      const pending = pendingChunks.get(chunk.id);
      if (pending) {
        pendingChunks.delete(chunk.id);
        pending.reject(new Error("chunk did not register: " + chunk.id));
      }
      script.remove();
    });
    document.head.appendChild(script);
    return promise;
  }

  function renderWindow(state, options) {
    const records = state.currentRecords;
    if (options && typeof options.recordIndex === "number") {
      state.windowStart = clamp(
        options.recordIndex - Math.floor(WINDOW_SIZE / 2),
        0,
        Math.max(0, records.length - WINDOW_SIZE)
      );
    } else if (options && options.anchor === "end") {
      state.windowStart = Math.max(0, records.length - WINDOW_SIZE);
    } else if (options && options.anchor === "start") {
      state.windowStart = 0;
    } else {
      state.windowStart = clamp(
        state.windowStart,
        0,
        Math.max(0, records.length - WINDOW_SIZE)
      );
    }

    const end = Math.min(records.length, state.windowStart + WINDOW_SIZE);
    const visible = records.slice(state.windowStart, end);
    const fragment = document.createDocumentFragment();
    let lastDate = "";
    visible.forEach(function (record) {
      if (record.date !== lastDate) {
        fragment.appendChild(createDateDivider(record.date));
        lastDate = record.date;
      }
      fragment.appendChild(createMessage(record, state));
    });
    state.elements.messageList.replaceChildren(fragment);
    state.elements.emptyState.hidden = visible.length > 0;
    state.elements.earlierWindow.hidden = state.windowStart <= 0;
    state.elements.laterWindow.hidden = end >= records.length;
    state.elements.viewStatus.textContent = visible.length
      ? chunkLabel(manifest.chunks[state.currentChunkIndex], state.currentChunkIndex) +
        " · 显示第 " + formatNumber(state.windowStart + 1) + "–" + formatNumber(end) + " 条"
      : "当前分块没有可显示的消息";
    updateNavigation(state);

    if (state.highlightedId) {
      requestAnimationFrame(function () {
        const row = Array.from(state.elements.messageList.querySelectorAll(".message-row")).find(
          function (element) {
            return element.dataset.messageId === state.highlightedId;
          }
        );
        if (row) {
          row.classList.add("highlighted");
          row.scrollIntoView({ block: "center" });
          row.focus({ preventScroll: true });
        }
        state.highlightedId = "";
      });
    }
  }

  function createDateDivider(dateValue) {
    const divider = document.createElement("div");
    divider.className = "date-divider";
    divider.textContent = readableDate(dateValue);
    divider.setAttribute("role", "separator");
    return divider;
  }

  function createMessage(record, state, options) {
    const forPrint = Boolean(options && options.forPrint);
    if (record.type === "system") {
      const system = document.createElement("article");
      system.className = "system-message";
      system.dataset.messageId = record.id;
      const text = document.createElement("p");
      text.textContent = record.text || "系统消息";
      system.appendChild(text);
      return system;
    }

    const row = document.createElement("article");
    row.className = "message-row" + (record.outgoing ? " outgoing" : " incoming");
    if (!forPrint) {
      row.tabIndex = 0;
    }
    row.dataset.messageId = record.id;
    row.setAttribute(
      "aria-label",
      (record.sender_name || "未知成员") + "，" + readableDateTime(record.datetime) + "，" + messageAccessibleText(record)
    );
    if (!forPrint) {
      row.addEventListener("click", function (event) {
        if (event.target.closest("button, a, video")) {
          return;
        }
        row.classList.toggle("show-time");
      });
    }

    row.appendChild(createAvatar(state.participantMap.get(record.sender), record, options));
    const stack = document.createElement("div");
    stack.className = "message-stack";
    const sender = document.createElement("p");
    sender.className = "sender-name";
    sender.textContent = record.sender_name || "未知成员";
    stack.appendChild(sender);

    const bubble = document.createElement("div");
    bubble.className = "message-bubble message-type-" + safeClassName(record.type);
    if (record.text) {
      const text = document.createElement("p");
      text.className = "message-text";
      text.textContent = record.text;
      bubble.appendChild(text);
    }
    if (Array.isArray(record.media) && record.media.length) {
      bubble.appendChild(createMediaList(record.media, state, record, options));
    }
    if (record.attachment) {
      bubble.appendChild(createAttachment(record.attachment));
    }
    if (!bubble.childNodes.length) {
      const emptyText = document.createElement("p");
      emptyText.className = "message-text";
      emptyText.textContent = "此消息没有可显示的内容";
      bubble.appendChild(emptyText);
    }
    stack.appendChild(bubble);

    const time = document.createElement("time");
    time.className = "message-time";
    time.dateTime = record.datetime;
    time.textContent = readableDateTime(record.datetime);
    stack.appendChild(time);
    row.appendChild(stack);
    return row;
  }

  function createAvatar(participant, record, options) {
    const avatar = document.createElement("div");
    avatar.className = "avatar";
    const avatarInfo = participant && participant.avatar ? participant.avatar : null;
    if (avatarInfo && avatarInfo.status === "exported" && avatarInfo.path) {
      const image = document.createElement("img");
      image.src = avatarInfo.path;
      image.alt = "";
      image.loading = options && options.forPrint ? "eager" : "lazy";
      image.decoding = "async";
      image.addEventListener("error", function () {
        avatar.replaceChildren(createAvatarFallback(participant, record));
      });
      avatar.appendChild(image);
      return avatar;
    }
    avatar.appendChild(createAvatarFallback(participant, record));
    return avatar;
  }

  function createAvatarFallback(participant, record) {
    const fallback = document.createElement("span");
    const identity = String((participant && participant.id) || record.sender || "?");
    fallback.className = "avatar-fallback avatar-tone-" + String(stableTone(identity));
    fallback.textContent =
      (participant && participant.avatar && participant.avatar.fallback_text) ||
      firstVisibleCharacter(record.sender_name || "?");
    fallback.setAttribute("aria-hidden", "true");
    return fallback;
  }

  function createMediaList(mediaItems, state, record, options) {
    const forPrint = Boolean(options && options.forPrint);
    const list = document.createElement("div");
    list.className = "media-list";
    mediaItems.forEach(function (item) {
      if (item.status !== "exported" || !item.path) {
        const missing = document.createElement("div");
        missing.className = "media-missing";
        missing.textContent = item.reason || mediaKindLabel(item.kind) + "未能导出";
        list.appendChild(missing);
        return;
      }
      if (item.kind === "video") {
        if (forPrint) {
          list.appendChild(mediaFailure("视频未写入纸面或 PDF；请在原 HTML 归档中播放。"));
          return;
        }
        const video = document.createElement("video");
        video.className = "message-video";
        video.controls = true;
        video.preload = "metadata";
        video.playsInline = true;
        video.src = item.path;
        video.addEventListener("error", function () {
          video.replaceWith(mediaFailure("浏览器无法播放这段视频，可在资源目录中打开原文件。"));
        });
        list.appendChild(video);
        return;
      }
      const button = document.createElement(forPrint ? "div" : "button");
      if (!forPrint) {
        button.type = "button";
        button.setAttribute("aria-label", "查看" + mediaKindLabel(item.kind));
      }
      button.className = "media-open" + (item.kind === "sticker" ? " media-sticker" : "");
      const image = document.createElement("img");
      const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
      image.src = reduceMotion && item.animated && item.fallback_path ? item.fallback_path : item.path;
      image.alt = item.kind === "sticker" ? "动态表情" : "聊天图片";
      image.loading = forPrint ? "eager" : "lazy";
      image.decoding = "async";
      image.addEventListener("error", function () {
        button.replaceWith(mediaFailure(mediaKindLabel(item.kind) + "文件无法显示"));
      });
      button.appendChild(image);
      if (!forPrint) {
        button.addEventListener("click", function () {
          openMediaDialog(state.elements, item.path, image.alt, record);
        });
      }
      list.appendChild(button);
    });
    return list;
  }

  function createAttachment(attachment) {
    const card = attachment.status === "exported" && attachment.path
      ? document.createElement("a")
      : document.createElement("div");
    card.className = "attachment-card";
    if (card instanceof HTMLAnchorElement) {
      card.href = attachment.path;
      card.download = attachment.filename || "附件";
    }
    const name = document.createElement("span");
    name.className = "attachment-name";
    name.textContent = attachment.filename || "未命名文件";
    const meta = document.createElement("span");
    meta.className = "attachment-meta";
    meta.textContent = [
      attachment.extension ? String(attachment.extension).toUpperCase() : "文件",
      formatBytes(attachment.size),
    ].filter(Boolean).join(" · ");
    const status = document.createElement("span");
    status.className = "attachment-status";
    status.textContent = attachment.status === "exported"
      ? "点击打开本地副本"
      : attachment.reason || "仅保存文件信息，未包含原件";
    card.append(name, meta, status);
    return card;
  }

  function updatePrintDescription(state) {
    const elements = state.elements;
    if (state.currentChunkIndex < 0) {
      elements.printDescription.textContent = "请先等待当前消息分块加载完成。";
      elements.printConfirm.disabled = true;
      return;
    }
    const count = state.currentRecords.length;
    elements.printConfirm.disabled = false;
    if (manifest.chunks.length === 1) {
      elements.printDescription.textContent =
        "本归档只有 1 个分块，将打印其中全部 " + formatNumber(count) + " 条消息，也就是本归档的全部记录。";
      return;
    }
    elements.printDescription.textContent =
      "当前是第 " + String(state.currentChunkIndex + 1) + " / " + String(manifest.chunks.length) +
      " 个分块，将打印本分块全部 " + formatNumber(count) + " 条消息；不会打印其余分块。";
  }

  function clearPreparedPrint(state) {
    state.printChunkIndex = -1;
    state.elements.printMessageList.replaceChildren();
    state.elements.printRange.textContent = "尚未准备";
    state.elements.printCount.textContent = "—";
  }

  function preparePrintDocument(state) {
    if (state.currentChunkIndex < 0 || !state.currentRecords.length) {
      return false;
    }
    if (state.printChunkIndex === state.currentChunkIndex && state.elements.printMessageList.childNodes.length) {
      return true;
    }

    const chunk = manifest.chunks[state.currentChunkIndex];
    const fragment = document.createDocumentFragment();
    let lastDate = "";
    state.currentRecords.forEach(function (record) {
      if (record.date !== lastDate) {
        fragment.appendChild(createDateDivider(record.date));
        lastDate = record.date;
      }
      fragment.appendChild(createMessage(record, state, { forPrint: true }));
    });
    state.elements.printMessageList.replaceChildren(fragment);
    state.printChunkIndex = state.currentChunkIndex;
    state.elements.printRange.textContent = chunkLabel(chunk, state.currentChunkIndex);
    state.elements.printCount.textContent = formatNumber(state.currentRecords.length) + " 条";
    state.elements.printGenerated.textContent = readableDateTime(manifest.generated_at);
    state.elements.printScopeNote.textContent = manifest.chunks.length === 1
      ? "本归档只有一个分块，因此本次打印包含归档中的全部记录。"
      : "本次只打印第 " + String(state.currentChunkIndex + 1) + " / " + String(manifest.chunks.length) +
        " 个分块，不包含归档中的其他分块。";
    return true;
  }

  async function printCurrentChunk(state) {
    const elements = state.elements;
    if (!preparePrintDocument(state)) {
      updatePrintDescription(state);
      return;
    }
    elements.printConfirm.disabled = true;
    elements.printConfirm.textContent = "正在准备图片…";
    elements.viewStatus.textContent = "正在准备当前分块的打印版式…";
    try {
      await waitForPrintImages(elements.printMessageList, 3000);
      elements.printDialog.close();
      window.print();
    } finally {
      elements.printConfirm.disabled = false;
      elements.printConfirm.textContent = "准备并打开打印面板";
      const chunk = manifest.chunks[state.currentChunkIndex];
      elements.viewStatus.textContent = chunkLabel(chunk, state.currentChunkIndex) + " · 打印范围为当前分块全部消息";
    }
  }

  function waitForPrintImages(container, timeoutMs) {
    const pending = Array.from(container.querySelectorAll("img")).filter(function (image) {
      return !image.complete;
    });
    if (!pending.length) {
      return Promise.resolve();
    }
    const settled = Promise.all(pending.map(function (image) {
      return new Promise(function (resolve) {
        image.addEventListener("load", resolve, { once: true });
        image.addEventListener("error", resolve, { once: true });
      });
    }));
    const timeout = new Promise(function (resolve) {
      window.setTimeout(resolve, timeoutMs);
    });
    return Promise.race([settled, timeout]);
  }

  async function runArchiveSearch(state) {
    const elements = state.elements;
    const criteria = readCriteria(elements);
    if (!criteria.query && !criteria.member && !criteria.type && !criteria.dateFrom && !criteria.dateTo) {
      elements.searchResults.replaceChildren();
      elements.searchCount.textContent = "0";
      elements.searchHelp.textContent = "输入关键词或选择筛选条件后搜索全部分块。";
      renderWindow(state);
      return;
    }

    const token = state.searchToken + 1;
    state.searchToken = token;
    elements.searchButton.disabled = true;
    elements.searchResults.replaceChildren();
    let totalMatches = 0;
    const saved = [];
    try {
      for (let chunkIndex = 0; chunkIndex < manifest.chunks.length; chunkIndex += 1) {
        if (state.searchToken !== token) {
          return;
        }
        elements.searchHelp.textContent =
          "正在搜索第 " + String(chunkIndex + 1) + " / " + String(manifest.chunks.length) + " 个分块…";
        const records = await loadChunkData(manifest.chunks[chunkIndex]);
        records.forEach(function (record, recordIndex) {
          if (!matchesCriteria(record, criteria)) {
            return;
          }
          totalMatches += 1;
          if (saved.length < MAX_SAVED_RESULTS) {
            saved.push({ record, chunkIndex, recordIndex });
          }
        });
      }
      if (state.searchToken !== token) {
        return;
      }
      renderSearchResults(state, saved, totalMatches);
    } catch (error) {
      elements.searchHelp.textContent = "搜索中断：某个消息分块无法读取。";
    } finally {
      if (state.searchToken === token) {
        elements.searchButton.disabled = false;
      }
    }
  }

  function renderSearchResults(state, hits, totalMatches) {
    const fragment = document.createDocumentFragment();
    hits.forEach(function (hit) {
      const item = document.createElement("li");
      const button = document.createElement("button");
      button.type = "button";
      button.className = "search-result-button";
      const meta = document.createElement("span");
      meta.className = "result-meta";
      meta.textContent = (hit.record.sender_name || "未知成员") + " · " + readableDateTime(hit.record.datetime);
      const preview = document.createElement("span");
      preview.className = "result-preview";
      preview.textContent = messageAccessibleText(hit.record);
      button.append(meta, preview);
      button.addEventListener("click", function () {
        loadAndShowChunk(state, hit.chunkIndex, {
          recordIndex: hit.recordIndex,
          highlightedId: hit.record.id,
        });
        if (window.matchMedia("(max-width: 840px)").matches) {
          state.elements.sidebar.classList.remove("open");
          state.elements.sidebarToggle.setAttribute("aria-expanded", "false");
        }
      });
      item.appendChild(button);
      fragment.appendChild(item);
    });
    state.elements.searchResults.replaceChildren(fragment);
    state.elements.searchCount.textContent = formatNumber(totalMatches);
    if (totalMatches > MAX_SAVED_RESULTS) {
      state.elements.searchHelp.textContent =
        "共找到 " + formatNumber(totalMatches) + " 条；为保持流畅，仅列出前 " + formatNumber(MAX_SAVED_RESULTS) + " 条定位结果。";
    } else if (totalMatches) {
      state.elements.searchHelp.textContent = "共找到 " + formatNumber(totalMatches) + " 条，点击结果可跳转。";
    } else {
      state.elements.searchHelp.textContent = "没有找到符合条件的消息。";
    }
  }

  function readCriteria(elements) {
    return {
      query: normalizeSearch(elements.searchInput.value),
      member: elements.memberFilter.value,
      type: elements.typeFilter.value,
      dateFrom: elements.dateFrom.value,
      dateTo: elements.dateTo.value,
    };
  }

  function matchesCriteria(record, criteria) {
    if (criteria.member && record.sender !== criteria.member) {
      return false;
    }
    if (criteria.type && record.type !== criteria.type) {
      return false;
    }
    if (criteria.dateFrom && record.date < criteria.dateFrom) {
      return false;
    }
    if (criteria.dateTo && record.date > criteria.dateTo) {
      return false;
    }
    if (!criteria.query) {
      return true;
    }
    const attachmentName = record.attachment && record.attachment.filename ? record.attachment.filename : "";
    const haystack = normalizeSearch(
      [record.text, record.raw_text, record.sender_name, attachmentName, record.type]
        .filter(Boolean)
        .join("\n")
    );
    return haystack.includes(criteria.query);
  }

  function updateNavigation(state) {
    const elements = state.elements;
    const index = state.currentChunkIndex;
    elements.previousChunk.disabled = index <= 0;
    elements.firstChunk.disabled = index <= 0;
    elements.nextChunk.disabled = index < 0 || index >= manifest.chunks.length - 1;
    elements.latestChunk.disabled = index < 0 || index >= manifest.chunks.length - 1;
  }

  function setNavigationDisabled(elements, value) {
    elements.previousChunk.disabled = value;
    elements.nextChunk.disabled = value;
    elements.firstChunk.disabled = value;
    elements.latestChunk.disabled = value;
    elements.chunkSelect.disabled = value;
  }

  function renderEmpty(state, message, error) {
    state.elements.messageList.replaceChildren();
    state.elements.emptyState.hidden = false;
    const heading = state.elements.emptyState.querySelector("h2");
    const detail = state.elements.emptyState.querySelector("p");
    heading.textContent = message;
    detail.textContent = error ? "错误信息：" + String(error.message || error) : "";
    state.elements.viewStatus.textContent = message;
    state.elements.earlierWindow.hidden = true;
    state.elements.laterWindow.hidden = true;
  }

  function showFatalError(message) {
    const status = document.getElementById("view-status");
    const empty = document.getElementById("empty-state");
    if (status) {
      status.textContent = message;
    }
    if (empty) {
      empty.hidden = false;
      const heading = empty.querySelector("h2");
      const detail = empty.querySelector("p");
      heading.textContent = "归档无法打开";
      detail.textContent = message;
    }
  }

  function openMediaDialog(elements, path, alt, record) {
    elements.dialogImage.src = path;
    elements.dialogImage.alt = alt;
    elements.dialogCaption.textContent =
      (record.sender_name || "未知成员") + " · " + readableDateTime(record.datetime);
    if (typeof elements.dialog.showModal === "function") {
      elements.dialog.showModal();
    }
  }

  function mediaFailure(message) {
    const failure = document.createElement("div");
    failure.className = "media-missing";
    failure.textContent = message;
    return failure;
  }

  function cycleTheme(button, theme, initial) {
    if (theme === "auto") {
      delete document.documentElement.dataset.theme;
    } else {
      document.documentElement.dataset.theme = theme;
    }
    button.textContent = "主题：" + ({ auto: "自动", light: "浅色", dark: "深色" }[theme] || "自动");
    if (!initial) {
      try {
        sessionStorage.setItem("wechat-archive-theme", theme);
      } catch (error) {
        // Some hardened file:// contexts disable storage. Theme switching is
        // still useful for the current page and does not require persistence.
      }
    } else {
      try {
        const saved = sessionStorage.getItem("wechat-archive-theme");
        if (saved && saved !== "auto") {
          document.documentElement.dataset.theme = saved;
          button.textContent = "主题：" + ({ light: "浅色", dark: "深色" }[saved] || "自动");
        }
      } catch (error) {
        // Ignore unavailable storage in local-file privacy modes.
      }
    }
  }

  function messageAccessibleText(record) {
    if (record.text) {
      return record.text;
    }
    if (record.attachment) {
      return "文件 " + (record.attachment.filename || "未命名文件");
    }
    if (record.media && record.media.length) {
      return record.media.map(function (item) {
        return mediaKindLabel(item.kind) + (item.status === "exported" ? "" : "不可用");
      }).join("，");
    }
    return "无可见内容";
  }

  function chunkLabel(chunk, index) {
    const range = chunk.first_date === chunk.last_date
      ? chunk.first_date
      : (chunk.first_date || "日期未知") + " 至 " + (chunk.last_date || "日期未知");
    return "第 " + String(index + 1) + " 块 · " + range + " · " + formatNumber(chunk.count) + " 条";
  }

  function mediaKindLabel(kind) {
    return ({ image: "图片", sticker: "动态表情", video: "视频", file: "文件" }[kind] || "媒体");
  }

  function firstVisibleCharacter(value) {
    return Array.from(String(value || "?").replace(/\s/g, ""))[0] || "?";
  }

  function stableTone(value) {
    let hash = 0;
    Array.from(value).forEach(function (character) {
      hash = (hash * 31 + character.codePointAt(0)) >>> 0;
    });
    return hash % 12;
  }

  function safeClassName(value) {
    return String(value || "unknown").replace(/[^a-z0-9_-]/gi, "-").toLowerCase();
  }

  function normalizeSearch(value) {
    return String(value || "").normalize("NFKC").toLocaleLowerCase("zh-CN").trim();
  }

  function readableDate(value) {
    if (!value || value === "日期未知") {
      return "日期未知";
    }
    const date = new Date(value + "T00:00:00");
    if (Number.isNaN(date.getTime())) {
      return value;
    }
    return new Intl.DateTimeFormat("zh-CN", {
      year: "numeric",
      month: "long",
      day: "numeric",
      weekday: "short",
    }).format(date);
  }

  function readableDateTime(value) {
    if (!value || value === "时间未知") {
      return "时间未知";
    }
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) {
      return value;
    }
    return new Intl.DateTimeFormat("zh-CN", {
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hour12: false,
    }).format(date);
  }

  function formatNumber(value) {
    return new Intl.NumberFormat("zh-CN").format(Number(value || 0));
  }

  function formatBytes(value) {
    const bytes = Number(value);
    if (!Number.isFinite(bytes) || bytes < 0) {
      return "";
    }
    if (bytes < 1024) {
      return String(bytes) + " B";
    }
    if (bytes < 1024 * 1024) {
      return (bytes / 1024).toFixed(1) + " KB";
    }
    if (bytes < 1024 * 1024 * 1024) {
      return (bytes / (1024 * 1024)).toFixed(1) + " MB";
    }
    return (bytes / (1024 * 1024 * 1024)).toFixed(1) + " GB";
  }

  function scrollMessagesToTop() {
    const list = document.getElementById("message-list");
    if (list) {
      list.scrollIntoView({ block: "start" });
    }
  }

  function clamp(value, minimum, maximum) {
    return Math.min(maximum, Math.max(minimum, value));
  }

  function setText(id, value) {
    byId(id).textContent = String(value);
  }

  function byId(id) {
    const element = document.getElementById(id);
    if (!element) {
      throw new Error("Missing viewer element: " + id);
    }
    return element;
  }
})();
