(function () {
  "use strict";
  const escape = value => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  const steps = [
    ["studio-reference", "原视频与剪辑分轨", "观看参考片 · 检查时间轴"],
    ["studio-subtitles", "字幕分析", "逐条字幕 · 字体与模板"],
    ["studio-recognition", "配音与画面文字识别", "声音画像 · 文案与 OCR"],
    ["studio-cutroom", "溯剪 × Cutroom", "新脚本 · 素材与配音配置"],
    ["studio-delivery", "预览与剪映草稿", "审看效果 · 保存可编辑工程"]
  ];
  const stepIcons = [
    '<rect x="3" y="4" width="18" height="16" rx="3"/><path d="m10 9 5 3-5 3zM7 4v16M17 4v16M3 9h4M3 15h4M17 9h4M17 15h4"/>',
    '<rect x="3" y="5" width="18" height="14" rx="3"/><path d="M7 10h10M7 14h4m3 0h3"/>',
    '<rect x="9" y="3" width="6" height="11" rx="3"/><path d="M6 11v1a6 6 0 0 0 12 0v-1M12 18v3m-3 0h6"/>',
    '<circle cx="6" cy="6" r="3"/><circle cx="6" cy="18" r="3"/><path d="m8.5 7.5 12 12m-12-3 12-12M14 10l6.5-6.5"/>',
    '<rect x="3" y="4" width="18" height="13" rx="3"/><path d="m10 8 5 3-5 3zM8 21h8m-4-4v4"/>'
  ];
  const stepMarker = index => `<span class="studio-nav-number" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.65" stroke-linecap="round" stroke-linejoin="round">${stepIcons[index]}</svg><span class="studio-nav-index">${String(index + 1).padStart(2,"0")}</span></span>`;
  window.mountSuoJianStudio = function (root, bootstrap) {
    root.studioCleanup?.();
    const bench = root.querySelector(".sj-workbench"), main = root.querySelector(".sj-main");
    if (!bench || !main) return;
    bench.classList.add("sj-studio");
    const rawTitle = bootstrap.blueprint?.source?.file_name || "当前参考视频";
    const title = bootstrap.job && rawTitle.startsWith(bootstrap.job + "_") ? rawTitle.slice(9) : rawTitle;
    const chapter = (id, number, title, note) => {
      const section = document.createElement("section");
      section.id = id; section.className = "sj-section sj-card studio-chapter";
      section.dataset.chapter = number;
      section.innerHTML = `<div class="studio-section-head"><span class="studio-step">${number}</span><div><small>${escape(note)}</small><h2>${escape(title)}</h2></div></div>`;
      return section;
    };
    // Move the original nodes, retaining the player, seek and audio listeners.
    const reference = chapter("studio-reference", "01", "原视频与剪辑草稿分轨", "参考分析 · 并非已生成的新草稿");
    const subtitles = chapter("studio-subtitles", "02", "字幕分析", "逐条文字与制作规格");
    const recognition = chapter("studio-recognition", "03", "配音与画面文字识别", "语音转写与画面文字分开核对");
    [".video-stage", ".video-tools", ".summary-strip", ".timeline-wrap", ".stem-panel"].forEach(selector => {
      const element = main.querySelector(selector); if (element) reference.append(element);
    });
    const production = main.querySelector(".subtitle-plan"), grid = main.querySelector(".sj-grid");
    if (production) subtitles.append(production);
    const graphicsGrid = document.createElement("div"), voiceGrid = document.createElement("div");
    graphicsGrid.className = voiceGrid.className = "studio-analysis-grid";
    if (grid) Array.from(grid.children).forEach((card, i) => (i === 0 || i === 3 ? graphicsGrid : voiceGrid).append(card));
    subtitles.append(graphicsGrid);
    const transcript = main.querySelector(".transcript-card");
    if (transcript) {
      const captions = transcript.querySelector(".transcript-lines");
      if (captions && captions.querySelector("summary")?.textContent.includes("逐条字幕")) subtitles.append(captions);
      else subtitles.insertAdjacentHTML("beforeend", '<p class="muted studio-caption-empty">暂未识别到逐条字幕。可在下方核对配音和画面文字；不会把画面标牌直接当成字幕。</p>');
      recognition.append(voiceGrid, transcript);
    }
    // Remove only the now-empty creative wrapper, not any analysis content.
    const oldCreative = grid?.parentElement;
    if (oldCreative) oldCreative.remove();
    const download = main.querySelector(".download-link");
    if (download) {
      const downloadSection = download.closest(".sj-section");
      if (downloadSection) { downloadSection.classList.add("studio-analysis-download"); recognition.append(downloadSection); }
    }
    const flow = main.querySelector(".unified-flow");
    if (flow) main.insertBefore(reference, flow); else main.prepend(reference);
    reference.after(subtitles, recognition);
    if (bootstrap.job && bootstrap.apiBase) recognition.insertAdjacentHTML("beforeend", `<a class="sj-cta studio-offline-link" href="/presenter?job=${encodeURIComponent(bootstrap.job)}">真人口播复刻 → 选人物素材 · 按脚本组装原声视频</a>`);
    if (!flow) {
      const offline = chapter("studio-cutroom", "04", "溯剪 × Cutroom", "离线报告仅用于查看分析");
      offline.insertAdjacentHTML("beforeend", '<p class="muted">在本机工作台中配置新脚本、园区素材和配音；离线报告不会直接调用 API 或生成工程。</p><a class="sj-cta studio-offline-link" href="http://127.0.0.1:8788/">打开本机工作台</a>');
      const delivery = chapter("studio-delivery", "05", "预览与剪映草稿生成", "需要本机执行服务");
      delivery.insertAdjacentHTML("beforeend", '<p class="muted">生成预览与保存剪映草稿，请在本机工作台完成。</p>');
      main.append(offline, delivery);
    }
    const header = root.querySelector(".sj-head");
    if (header) {
      header.querySelector("h1").innerHTML = '参考视频分析 <small>工作台</small>';
      header.querySelector(".sj-brand .muted").textContent = title;
    }
    const sidebar = document.createElement("aside");
    sidebar.className = "studio-sidebar"; sidebar.setAttribute("aria-label", "工作台侧边导航");
    sidebar.innerHTML = `<div class="studio-sidebar-brand"><img src="${escape(bootstrap.iconUrl || "preview_assets/suojian-icon.png")}" alt="溯剪图标"><div><b>溯剪 <span>SuoJian</span></b><small>视频剪法工作台</small></div><button class="studio-fold" type="button" aria-label="收起侧边导航" aria-expanded="true">‹</button></div><a class="studio-new" href="/" aria-label="返回制作首页"><span>⌂</span><b>返回制作首页</b></a><div class="studio-nav-label">快速导航 <span>五步工作流</span></div><nav class="studio-nav" aria-label="分析步骤">${steps.map(([id, label, hint], i) => `<a href="#${id}" data-step="${id}" ${i === 0 ? 'aria-current="step"' : ""}>${stepMarker(i)}<div><b>${label}</b><small>${hint}</small></div></a>`).join("")}</nav><div class="studio-tasks-heading"><b>最近任务</b><span data-task-count>本机记录</span></div><label class="studio-search"><span>⌕</span><input type="search" aria-label="搜索历史任务" placeholder="搜索任务或视频名称"></label><div class="studio-task-list" aria-live="polite"><p class="muted">正在读取本机任务…</p></div><div class="studio-sidebar-foot"><i></i><span>本机工作台 · 8788</span></div>`;
    const tasksSection = document.createElement("details");
    tasksSection.className = "studio-tasks";
    const tasksHeading = sidebar.querySelector(".studio-tasks-heading");
    const tasksSummary = document.createElement("summary");
    tasksSummary.className = "studio-tasks-heading";
    tasksSummary.append(...tasksHeading.childNodes);
    sidebar.insertBefore(tasksSection, tasksHeading);
    tasksSection.append(tasksSummary, sidebar.querySelector(".studio-search"), sidebar.querySelector(".studio-task-list"));
    tasksHeading.remove();
    try { tasksSection.open = localStorage.getItem("suojian_recent_tasks_open") === "1"; } catch (_) {}
    tasksSection.addEventListener("toggle", () => {
      try { localStorage.setItem("suojian_recent_tasks_open", tasksSection.open ? "1" : "0"); } catch (_) {}
    });
    bench.prepend(sidebar);
    if (!bootstrap.apiBase) sidebar.querySelector(".studio-new").href = "http://127.0.0.1:8788/";
    sidebar.querySelectorAll("[data-step]").forEach((link, index) => {
      link.setAttribute("aria-label", `${String(index + 1).padStart(2,"0")} ${steps[index][1]} ${steps[index][2]}`);
      link.title = steps[index][1];
    });
    let compact = false;
    try { compact = localStorage.getItem("suojian_sidebar_compact") === "1"; } catch (_) {}
    function setCompact() {
      bench.classList.toggle("sidebar-compact", compact);
      const button = sidebar.querySelector(".studio-fold");
      button.textContent = compact ? "›" : "‹";
      button.setAttribute("aria-label", compact ? "展开侧边导航" : "收起侧边导航");
      button.setAttribute("aria-expanded", String(!compact));
    }
    setCompact();
    sidebar.querySelector(".studio-fold").onclick = () => { compact = !compact; setCompact(); try { localStorage.setItem("suojian_sidebar_compact", compact ? "1" : "0"); } catch (_) {} };
    sidebar.querySelectorAll("[data-step]").forEach(link => link.onclick = event => {
      const target = root.querySelector(`#${link.dataset.step}`);
      if (target) { event.preventDefault(); target.scrollIntoView({behavior:matchMedia("(prefers-reduced-motion: reduce)").matches ? "instant" : "smooth", block:"start"}); }
    });
    let tasks = [], disposed = false, taskTimer, scheduled = false;
    function paintTasks() {
      const query = sidebar.querySelector("input[type=search]").value.trim().toLowerCase();
      const selected = tasks.filter(task => `${task.title} ${task.job}`.toLowerCase().includes(query));
      sidebar.querySelector(".studio-task-list").innerHTML = selected.length ? selected.map(task => `<a class="studio-task ${task.job === bootstrap.job ? "is-current" : ""}" href="/?job=${encodeURIComponent(task.job)}" ${task.job === bootstrap.job ? 'aria-current="page"' : ""}><div><b>${escape(task.title)}</b><span>${escape(task.stage)}</span></div><footer><span class="studio-task-status ${task.needs_attention ? "needs-attention" : ""}">${escape(task.status_label)}</span><small>${escape(task.job)}</small></footer></a>`).join("") : '<p class="muted">没有匹配的任务。</p>';
      const attention = tasks.filter(task => task.needs_attention).length;
      sidebar.querySelector("[data-task-count]").textContent = attention ? `${attention} 项待查看` : `${tasks.length} 项记录`;
    }
    sidebar.querySelector("input[type=search]").oninput = paintTasks;
    async function loadTasks() {
      if (disposed || !bench.isConnected) return;
      try {
        const response = await fetch(`${bootstrap.apiBase || "/api"}/workbench/tasks`);
        if (!response.ok) throw Error("历史任务暂时不可用");
        tasks = (await response.json()).tasks || []; paintTasks();
      } catch (_) { sidebar.querySelector(".studio-task-list").innerHTML = '<p class="muted">暂时无法读取历史任务，当前分析不受影响。</p>'; }
      taskTimer = setTimeout(loadTasks, 15000);
    }
    if (bootstrap.job && bootstrap.apiBase) loadTasks();
    else sidebar.querySelector(".studio-task-list").innerHTML = '<p class="muted">离线预览页；本机工作台中可查看历史任务。</p>';
    function highlightStep() {
      scheduled = false;
      let active = steps[0][0];
      steps.forEach(([id]) => { const element = root.querySelector(`#${id}`); if (element && element.getBoundingClientRect().top <= 190) active = id; });
      sidebar.querySelectorAll("[data-step]").forEach(link => {
        if (link.dataset.step === active) link.setAttribute("aria-current", "step"); else link.removeAttribute("aria-current");
      });
    }
    const onScroll = () => { if (!scheduled) { scheduled = true; requestAnimationFrame(highlightStep); } };
    window.addEventListener("scroll", onScroll, {passive:true}); highlightStep();
    root.studioCleanup = () => { disposed = true; clearTimeout(taskTimer); window.removeEventListener("scroll", onScroll); };
  };
})();
