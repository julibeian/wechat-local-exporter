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
      loadToken: 0,
      loading: false,
      virtual: null,
      hits: [],
      hitIndex: -1,
    };

    fillSummary(elements);
    fillFilters(elements, participantMap);
    fillChunkSelect(elements);
    bindControls(state);
    const pane = byId("conversation-pane");
    let scheduled = false;
    pane.addEventListener("scroll", function () {
      if (scheduled || !state.virtual) return;
      scheduled = true;
      requestAnimationFrame(function () {
        scheduled = false;
        updateViewport(state);
      });
    }, { passive: true });
    // At the physical edge a further gesture continues into the adjacent
    // chunk. Do not intercept wheel deltas or replace native scrolling.
    function continueAtEdge(delta) {
      if (state.loading || !state.virtual) return;
      if (delta < 0 && pane.scrollTop <= 1 && state.currentChunkIndex > 0) {
        loadAndShowChunk(state, state.currentChunkIndex - 1, { anchor: "end" });
      } else if (delta > 0 && pane.scrollHeight - pane.clientHeight - pane.scrollTop <= 1
        && state.currentChunkIndex < manifest.chunks.length - 1) {
        loadAndShowChunk(state, state.currentChunkIndex + 1, { anchor: "start" });
      }
    }
    pane.addEventListener("wheel", function (event) { continueAtEdge(event.deltaY); }, { passive: true });
    pane.addEventListener("keydown", function (event) {
      if (event.key === "End" || event.key === "Home") {
        event.preventDefault();
        if (event.ctrlKey || event.metaKey) {
          loadAndShowChunk(state, event.key === "Home" ? 0 : manifest.chunks.length - 1,
            { anchor: event.key === "Home" ? "start" : "end" });
        } else {
          renderWindow(state, { anchor: event.key === "Home" ? "start" : "end" });
        }
        return;
      }
      if (event.key === "PageDown" || event.key === "ArrowDown") continueAtEdge(1);
      if (event.key === "PageUp" || event.key === "ArrowUp") continueAtEdge(-1);
    });
    let touchY = 0;
    pane.addEventListener("touchstart", function (event) { touchY = event.touches[0].clientY; }, { passive: true });
    pane.addEventListener("touchend", function (event) { continueAtEdge(touchY - event.changedTouches[0].clientY); }, { passive: true });
    new ResizeObserver(function () {
      if (state.virtual) updateViewport(state, true);
    }).observe(pane);
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
    setText("summary-chunks", formatNumber(summary.chunks || 0) + " × 2,000");
    setText("summary-generated", String(manifest.generated_at || "").slice(0, 10));
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

    const types = [
      ["text", "文本", "type"], ["image", "图片", "image"], ["sticker", "表情", "smile"],
      ["voice", "语音", "mic"], ["video", "视频", "video"], ["file", "文件", "file"],
      ["quote", "回复", "corner-up-left"], ["chat_history", "转发", "share-2"],
      ["card", "卡片", "layout"], ["location", "位置", "map-pin"]
    ];
    types.forEach(function (item) {
      const option = document.createElement("option");
      option.value = item[0];
      option.textContent = item[1];
      elements.typeFilter.appendChild(option);
    });
    enhanceFilter(elements.memberFilter, "成员", function (value) {
      const participant = participantMap.get(value);
      return participant ? createAvatar(participant, {sender: value, sender_name: participant.name}) : null;
    });
    enhanceFilter(elements.typeFilter, "消息类型", function (value) {
      const type = types.find(function (item) { return item[0] === value; });
      return createIcon(type ? type[2] : "layers");
    });
  }

  function createIcon(name) {
    const image = document.createElement("img");
    image.className = "ui-icon";
    image.src = "assets/icons/" + name + ".svg";
    image.alt = "";
    return image;
  }

  function enhanceFilter(select, label, decorate) {
    const wrapper = document.createElement("div");
    wrapper.className = "filter-picker";
    const trigger = document.createElement("button");
    trigger.type = "button";
    trigger.className = "filter-trigger";
    trigger.id = select.id + "-trigger";
    trigger.setAttribute("aria-label", label);
    trigger.setAttribute("aria-haspopup", "listbox");
    trigger.setAttribute("aria-expanded", "false");
    const list = document.createElement("div");
    list.className = "filter-menu";
    list.id = select.id + "-menu";
    list.setAttribute("role", "listbox");
    list.setAttribute("aria-label", label);
    list.hidden = true;
    trigger.setAttribute("aria-controls", list.id);
    const choices = Array.from(select.options).map(function (option) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "filter-option";
      button.setAttribute("role", "option");
      button.dataset.value = option.value;
      const check = createIcon("check");
      check.classList.add("option-check");
      button.append(check);
      const decoration = decorate(option.value);
      if (decoration) button.append(decoration);
      const name = document.createElement("span");
      name.textContent = option.textContent;
      button.append(name);
      button.addEventListener("click", function () {
        select.value = option.value;
        sync(); close(); trigger.focus();
        select.dispatchEvent(new Event("change", {bubbles: true}));
      });
      list.append(button);
      return button;
    });
    function sync() {
      const selected = select.selectedOptions[0];
      trigger.replaceChildren();
      if (selected.value) {
        const decoration = decorate(selected.value);
        if (decoration) trigger.append(decoration);
      }
      const text = document.createElement("span");
      text.textContent = selected.textContent;
      trigger.append(text, createIcon("chevrons-down"));
      choices.forEach(function (button) {
        button.setAttribute("aria-selected", String(button.dataset.value === select.value));
      });
    }
    function close() { list.hidden = true; trigger.setAttribute("aria-expanded", "false"); }
    function open() {
      document.querySelectorAll(".filter-menu").forEach(function (menu) { menu.hidden = true; });
      document.querySelectorAll(".filter-trigger").forEach(function (button) { button.setAttribute("aria-expanded", "false"); });
      list.hidden = false; trigger.setAttribute("aria-expanded", "true");
      const bounds = trigger.getBoundingClientRect();
      const below = window.innerHeight - bounds.bottom - 12;
      const above = bounds.top - 12;
      const upward = below < Math.min(list.scrollHeight, 440) && above > below;
      wrapper.classList.toggle("opens-up", upward);
      list.style.maxHeight = Math.max(80, Math.min(440, upward ? above : below)) + "px";
      const selected = choices[select.selectedIndex] || choices[0];
      selected.focus({preventScroll: true});
      selected.scrollIntoView({block: "nearest"});
    }
    trigger.addEventListener("click", function () { list.hidden ? open() : close(); });
    wrapper.addEventListener("keydown", function (event) {
      if (event.key === "Escape") { close(); trigger.focus(); event.preventDefault(); }
      if (event.key === "Tab") close();
      if (["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) {
        event.preventDefault();
        if (list.hidden) { open(); return; }
        const i = choices.indexOf(document.activeElement);
        const next = event.key === "Home" ? 0 : event.key === "End" ? choices.length - 1
          : (i + (event.key === "ArrowDown" ? 1 : -1) + choices.length) % choices.length;
        choices[next].focus();
      }
    });
    document.addEventListener("click", function (event) { if (!wrapper.contains(event.target)) close(); });
    select.addEventListener("change", sync);
    select.form.addEventListener("reset", function () { setTimeout(sync, 0); close(); });
    wrapper.append(trigger, list);
    select.after(wrapper);
    sync();
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
    byId("settings-button").addEventListener("click", function () {
      const panel = byId("archive-settings");
      panel.hidden = !panel.hidden;
      byId("settings-button").setAttribute("aria-expanded", String(!panel.hidden));
    });
    byId("search-previous").addEventListener("click", function () { jumpToHit(state, -1); });
    byId("search-next").addEventListener("click", function () { jumpToHit(state, 1); });
    elements.filterForm.addEventListener("submit", function (event) {
      event.preventDefault();
      runArchiveSearch(state);
    });
    elements.clearButton.addEventListener("click", function () {
      elements.filterForm.reset();
      state.filterCriteria = null;
      state.filteredSource = null;
      state.hits = [];
      state.hitIndex = -1;
      byId("search-previous").disabled = true;
      byId("search-next").disabled = true;
      elements.searchButton.disabled = false;
      byId("date-summary").textContent = "全部日期";
      elements.searchResults.closest(".search-panel").hidden = true;
      elements.messageList.querySelectorAll(".highlighted").forEach(function (row) { row.classList.remove("highlighted"); });
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
      loadAndShowChunk(state, state.currentChunkIndex - 1, { anchor: "end" });
    });
    elements.laterWindow.addEventListener("click", function () {
      loadAndShowChunk(state, state.currentChunkIndex + 1, { anchor: "start" });
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
    const token = ++state.loadToken;
    state.loading = true;
    const chunk = manifest.chunks[index];
    state.elements.viewStatus.textContent = "正在加载 " + chunkLabel(chunk, index) + "…";
    state.elements.printButton.disabled = true;
    setNavigationDisabled(state.elements, true);
    try {
      const records = await loadChunkData(chunk);
      if (token !== state.loadToken) return;
      state.currentChunkIndex = index;
      state.currentRecords = records;
      clearPreparedPrint(state);
      state.highlightedId = options && options.highlightedId ? options.highlightedId : "";
      state.elements.chunkSelect.value = String(index);
      renderWindow(state, options || { anchor: "start" });
      setText("summary-current-chunk", String(index + 1) + " / " + String(manifest.chunks.length));
    } catch (error) {
      if (token !== state.loadToken) return;
      renderEmpty(state, "消息分块无法读取。请确认归档目录没有缺少文件。", error);
    } finally {
      if (token !== state.loadToken) return;
      state.loading = false;
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
    const request = {
      promise,
      resolve: resolvePromise,
      reject: rejectPromise,
    };
    pendingChunks.set(chunk.id, request);
    const script = document.createElement("script");
    script.src = chunk.path;
    script.async = true;
    script.addEventListener("error", function () {
      const pending = pendingChunks.get(chunk.id);
      if (pending === request) {
        pendingChunks.delete(chunk.id);
        pending.reject(new Error("chunk load failed: " + chunk.id));
      }
      script.remove();
    });
    script.addEventListener("load", function () {
      const pending = pendingChunks.get(chunk.id);
      // A registration resolves immediately, before this script's load event.
      // A search may already have started a new request for this same chunk.
      if (pending === request) {
        pendingChunks.delete(chunk.id);
        pending.reject(new Error("chunk did not register: " + chunk.id));
      }
      script.remove();
    });
    document.head.appendChild(script);
    return promise;
  }

  // Native scrolling with measured spacers. Keep overlapping DOM nodes alive
  // so an ordinary wheel gesture does not reload avatars or restart GIFs.
  function renderWindow(state, options) {
    if (state.filteredSource !== state.currentRecords || state.filteredCriteria !== state.filterCriteria) {
      state.filteredSource = state.currentRecords;
      state.filteredCriteria = state.filterCriteria;
      state.filteredRecords = state.filterCriteria ? state.currentRecords.filter(function (record) {
        return matchesCriteria(record, state.filterCriteria);
      }) : state.currentRecords;
    }
    const records = state.filteredRecords;
    if (options && typeof options.recordIndex === "number") {
      const target = state.currentRecords[options.recordIndex];
      options = Object.assign({}, options, {recordIndex: Math.max(0, records.indexOf(target))});
    }
    if (!state.virtual || state.virtual.records !== records) {
      if (state.virtual) state.virtual.observer.disconnect();
      const top = document.createElement("div");
      const bottom = document.createElement("div");
      top.className = bottom.className = "virtual-spacer";
      top.setAttribute("aria-hidden", "true");
      bottom.setAttribute("aria-hidden", "true");
      const heights = records.map(function (record, index) {
        const dateHeight = index === 0 || record.date !== records[index - 1].date ? 52 : 0;
        const mediaHeight = (record.media || []).some(function (media) { return media.status === "exported"; }) ? 150 : 0;
        return 66 + dateHeight + mediaHeight + Math.floor(String(record.text || "").length / 42) * 21;
      });
      const virtual = { records, heights, offsets: [], nodes: new Map(), top, bottom,
        start: -1, end: -1, observer: null, pinEnd: false };
      state.virtual = virtual;
      rebuildOffsets(virtual);
      virtual.observer = new ResizeObserver(function (entries) {
        if (state.virtual !== virtual) return;
        const pane = byId("conversation-pane");
        const position = pane.scrollTop - listOffset(state);
        const anchor = indexAt(virtual.offsets, position);
        const within = position - virtual.offsets[anchor];
        let changed = false;
        entries.forEach(function (entry) {
          const index = Number(entry.target.dataset.index);
          const height = entry.borderBoxSize && entry.borderBoxSize[0]
            ? entry.borderBoxSize[0].blockSize : entry.target.getBoundingClientRect().height;
          if (height > 0 && Math.abs(virtual.heights[index] - height) > 0.5) {
            virtual.heights[index] = height;
            changed = true;
          }
        });
        if (!changed) return;
        rebuildOffsets(virtual);
        setSpacers(virtual);
        // Compensation is instantaneous. CSS smooth scrolling here would fight
        // the user's wheel and make every decoded image move the viewport.
        pane.scrollTop = virtual.pinEnd ? pane.scrollHeight - pane.clientHeight
          : listOffset(state) + virtual.offsets[anchor] + within;
        requestAnimationFrame(function () { updateViewport(state); });
      });
      state.elements.messageList.replaceChildren(top, bottom);
    }
    const virtual = state.virtual;
    const pane = byId("conversation-pane");
    const opts = options || {};
    virtual.pinEnd = opts.anchor === "end";
    if (opts.anchor === "start") pane.scrollTop = 0;
    if (typeof opts.recordIndex === "number") {
      pane.scrollTop = listOffset(state) + virtual.offsets[opts.recordIndex];
    }
    updateViewport(state, true, typeof opts.recordIndex === "number" ? opts.recordIndex
      : opts.anchor === "end" ? Math.max(0, records.length - 1) : undefined);
    if (opts.anchor === "end") pane.scrollTop = pane.scrollHeight - pane.clientHeight;
    if (state.highlightedId) {
      const highlighted = state.highlightedId;
      requestAnimationFrame(function () {
        if (state.virtual !== virtual) return;
        const row = Array.from(state.elements.messageList.querySelectorAll("[data-message-id]")).find(
          function (element) { return element.dataset.messageId === highlighted; });
        if (row) {
          row.classList.add("highlighted");
          row.scrollIntoView({ block: "center", behavior: "instant" });
          row.focus({ preventScroll: true });
        }
      });
      state.highlightedId = "";
    }
    state.elements.emptyState.hidden = records.length > 0;
    state.elements.earlierWindow.hidden = state.currentChunkIndex <= 0;
    state.elements.laterWindow.hidden = state.currentChunkIndex >= manifest.chunks.length - 1;
    updateNavigation(state);
  }

  function listOffset(state) {
    const pane = byId("conversation-pane");
    return state.elements.messageList.getBoundingClientRect().top - pane.getBoundingClientRect().top + pane.scrollTop;
  }

  function rebuildOffsets(virtual) {
    virtual.offsets = [0];
    virtual.heights.forEach(function (height, i) { virtual.offsets.push(virtual.offsets[i] + height); });
  }

  function indexAt(offsets, position) {
    let low = 0, high = Math.max(0, offsets.length - 2);
    while (low < high) {
      const mid = Math.ceil((low + high) / 2);
      if (offsets[mid] <= position) low = mid;
      else high = mid - 1;
    }
    return low;
  }

  function setSpacers(virtual) {
    if (virtual.start < 0) return;
    virtual.top.style.height = virtual.offsets[virtual.start] + "px";
    virtual.bottom.style.height = (virtual.offsets[virtual.records.length] - virtual.offsets[virtual.end]) + "px";
  }

  function updateViewport(state, force, target) {
    const virtual = state.virtual;
    if (!virtual || !virtual.records.length) return;
    const pane = byId("conversation-pane");
    const position = Math.max(0, pane.scrollTop - listOffset(state));
    const index = typeof target === "number" ? target : indexAt(virtual.offsets, position);
    if (virtual.pinEnd && pane.scrollHeight - pane.scrollTop - pane.clientHeight > 4 && target === undefined) virtual.pinEnd = false;
    const start = clamp(index - 35, 0, Math.max(0, virtual.records.length - WINDOW_SIZE));
    const end = Math.min(virtual.records.length, start + WINDOW_SIZE);
    if (!force && index >= virtual.start + 12 && index < virtual.end - 35) return;
    if (virtual.start === start && virtual.end === end && !force) return;
    virtual.start = start;
    virtual.end = end;
    state.windowStart = start;
    for (const [i, node] of virtual.nodes) {
      if (i < start || i >= end) {
        virtual.observer.unobserve(node);
        node.remove();
        virtual.nodes.delete(i);
      }
    }
    // Insert only new nodes in chronological order; retained nodes never detach.
    let next = virtual.bottom;
    for (let i = end - 1; i >= start; i -= 1) {
      let node = virtual.nodes.get(i);
      if (!node) {
        node = document.createElement("div");
        node.className = "virtual-item";
        node.dataset.index = String(i);
        if (i === 0 || virtual.records[i].date !== virtual.records[i - 1].date) {
          node.appendChild(createDateDivider(virtual.records[i].date));
        }
        node.appendChild(createMessage(virtual.records[i], state));
        state.elements.messageList.insertBefore(node, next);
        virtual.nodes.set(i, node);
        virtual.observer.observe(node);
      }
      next = node;
    }
    setSpacers(virtual);
    state.elements.viewStatus.textContent = "第 " + (state.currentChunkIndex + 1) + " / " + manifest.chunks.length +
      " 块 · " + formatNumber(virtual.records.length) + " 条消息";
  }

  function jumpToHit(state, delta) {
    if (!state.hits.length) return;
    state.hitIndex = (state.hitIndex + delta + state.hits.length) % state.hits.length;
    const hit = state.hits[state.hitIndex];
    state.elements.searchResults.querySelectorAll("button").forEach(function (button, index) {
      button.setAttribute("aria-current", String(index === state.hitIndex));
    });
    loadAndShowChunk(state, hit.chunkIndex, { recordIndex: hit.recordIndex, highlightedId: hit.record.id });
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
    if (record.text && !record.attachment && !record.payment) {
      const text = document.createElement("p");
      text.className = "message-text";
      text.textContent = record.text;
      bubble.appendChild(text);
    }
    if (Array.isArray(record.media) && record.media.length) {
      bubble.appendChild(createMediaList(record.media, state, record, options));
    }
    if (record.attachment) {
      bubble.classList.add("card-bubble");
      bubble.appendChild(createAttachment(record.attachment));
    }
    if (record.payment) {
      bubble.classList.add("card-bubble");
      bubble.appendChild(createPayment(record.payment));
    }
    if (!bubble.childNodes.length) {
      const emptyText = document.createElement("p");
      emptyText.className = "message-text";
      emptyText.textContent = "此消息没有可显示的内容";
      bubble.appendChild(emptyText);
    }
    stack.appendChild(bubble);
    if (record.quote) {
      const quote = document.createElement("blockquote");
      quote.className = "message-quote";
      quote.textContent = (record.quote.sender_name || "未知成员") + "：" + (record.quote.text || "原消息内容不可用");
      stack.appendChild(quote);
    }

    const time = document.createElement("time");
    time.className = "message-time";
    time.dateTime = record.datetime;
    time.textContent = readableDateTime(record.datetime);
    sender.appendChild(time);
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
    status.textContent = attachment.status === "exported" ? "打开文件" : "文件信息";
    card.title = attachment.status === "exported" ? "打开本地副本" : attachment.reason || "可通过批量聊天文件提取原件";
    const extension = String(attachment.extension || "").toLowerCase();
    const icon = createIcon(["xls", "xlsx", "csv"].includes(extension) ? "file-excel" : "file");
    icon.classList.add("attachment-icon");
    if (["xls", "xlsx", "csv"].includes(extension)) icon.classList.add("spreadsheet-icon");
    card.append(name, meta, icon, status);
    return card;
  }

  function createPayment(payment) {
    const card = document.createElement("div");
    const settled = Number(payment.paysubtype) === 3;
    card.className = "payment-card" + (settled ? " settled" : "");
    const icon = createIcon(settled ? "check-circle" : "repeat");
    icon.classList.add("payment-icon");
    const amount = document.createElement("strong");
    amount.textContent = payment.amount || "微信转账";
    const status = document.createElement("span");
    status.className = "payment-status";
    const visibleStatus = payment.status_text === "微信转账" ? "" : payment.status_text;
    status.textContent = visibleStatus || ({1: "待收款", 3: "已收款"}[payment.paysubtype]) || payment.memo || "转账记录";
    const footer = document.createElement("span");
    footer.className = "payment-footer";
    footer.textContent = "微信转账";
    card.append(icon, amount, status, footer);
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
    const token = ++state.searchToken;
    state.hits = [];
    state.hitIndex = -1;
    byId("search-previous").disabled = true;
    byId("search-next").disabled = true;
    state.filterCriteria = Object.assign({}, criteria, {query: ""});
    renderWindow(state, {anchor: "start"});
    elements.searchResults.closest(".search-panel").hidden = false;
    byId("date-summary").textContent = criteria.dateFrom || criteria.dateTo
      ? (criteria.dateFrom || "最早") + " → " + (criteria.dateTo || "最新") : "全部日期";
    if (!criteria.query && !criteria.member && !criteria.type && !criteria.dateFrom && !criteria.dateTo) {
      elements.searchResults.replaceChildren();
      elements.searchCount.textContent = "0";
      elements.searchHelp.textContent = "输入关键词或选择筛选条件后搜索全部分块。";
      elements.searchButton.disabled = false;
      elements.searchResults.closest(".search-panel").hidden = true;
      renderWindow(state);
      return;
    }

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
      if (!criteria.query && saved.length) {
        await loadAndShowChunk(state, saved[0].chunkIndex, {recordIndex: saved[0].recordIndex});
      }
    } catch (error) {
      elements.searchHelp.textContent = "搜索中断：某个消息分块无法读取。";
    } finally {
      if (state.searchToken === token) {
        elements.searchButton.disabled = false;
      }
    }
  }

  function renderSearchResults(state, hits, totalMatches) {
    state.hits = hits;
    state.hitIndex = -1;
    byId("search-previous").disabled = !hits.length;
    byId("search-next").disabled = !hits.length;
    const fragment = document.createDocumentFragment();
    hits.forEach(function (hit, hitIndex) {
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
        state.hitIndex = hitIndex;
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
    const category = ["link", "mini_program", "contact_card", "video_card", "music", "transfer", "red_packet"].includes(record.type) ? "card" : record.type;
    if (criteria.type && category !== criteria.type) {
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
      [record.text, record.raw_text, record.sender_name, attachmentName, record.type,
       record.quote && record.quote.text, record.quote && record.quote.sender_name,
       record.payment && record.payment.amount]
        .filter(Boolean)
        .join("\n")
    );
    return haystack.includes(criteria.query);
  }

  function updateNavigation(state) {
    const elements = state.elements;
    const index = state.currentChunkIndex;
    elements.previousChunk.disabled = index <= 0;
      elements.firstChunk.disabled = index < 0;
    elements.nextChunk.disabled = index < 0 || index >= manifest.chunks.length - 1;
      elements.latestChunk.disabled = index < 0;
  }

  function setNavigationDisabled(elements, value) {
    elements.previousChunk.disabled = value;
    elements.nextChunk.disabled = value;
    elements.firstChunk.disabled = value;
    elements.latestChunk.disabled = value;
    elements.chunkSelect.disabled = value;
  }

  function renderEmpty(state, message, error) {
    if (state.virtual) state.virtual.observer.disconnect();
    state.virtual = null;
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
    button.replaceChildren(createIcon("moon"));
    button.title = "主题：" + ({ auto: "自动", light: "浅色", dark: "深色" }[theme] || "自动");
    button.setAttribute("aria-label", button.title);
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
          button.title = "主题：" + ({ light: "浅色", dark: "深色" }[saved] || "自动");
          button.setAttribute("aria-label", button.title);
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
