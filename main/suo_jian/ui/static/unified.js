(function () {
  "use strict";
  const escape = value => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  window.mountSuoJianExecution = function (root, bootstrap) {
    if (!bootstrap.job || !bootstrap.apiBase) return;
    const job = bootstrap.job, base = `${bootstrap.apiBase}/unified`;
    const entry = root.querySelector("#openWizard");
    entry.textContent = "制作新视频／保存草稿";
    const panel = document.createElement("div");
    panel.className = "unified-flow";
    panel.innerHTML = `<section class="sj-section sj-card unified-panel studio-chapter" id="studio-cutroom" data-chapter="04"><div class="studio-section-head"><span class="studio-step">04</span><div><h2>制作配置</h2></div></div>

      <div class="unified-grid"><label>剪法模板<select data-field="template"></select></label><label>本次模板名称<input data-field="name" maxlength="80" placeholder="例如：项目快切 · 高对比字幕"></label></div>
      <div class="unified-actions"><button class="sj-cta secondary" data-action="save-template">将本次分析保存为剪法模板</button><button class="sj-cta secondary" data-action="save-packaging">保存当前包装为模板</button></div>
      <label class="unified-script-label">新视频完整脚本<textarea data-field="script" rows="6" placeholder="输入脚本"></textarea></label>
      <div class="unified-grid"><label>项目素材库<select data-field="library"></select></label><label>配音配置<select data-field="tts"></select></label></div>
      <div class="unified-grid"><label>搜索配音音色<input data-field="voice-search" type="search" placeholder="搜索音色名称、性别、语言或风格" autocomplete="off"></label><label>本次配音音色<select data-field="voice" aria-describedby="voice-catalog-note"></select></label></div>
      <p class="unified-note" id="voice-catalog-note" data-view="voice-note" role="status"></p>
      <div class="unified-consents"><label><input type="checkbox" data-field="rights">我有权使用所选素材</label><label><input type="checkbox" data-field="delegate">委托自动选镜、锁定画面及选择包装；最后由我审看</label><label><input type="checkbox" data-field="cost">同意本次配音及必要的连接检测，可能产生费用</label></div>

      <div class="unified-actions"><button class="sj-cta" data-action="generate">生成试听预览</button><button class="sj-cta secondary" data-action="adopt">读取已有预览（不重新配音）</button><button class="sj-cta secondary" data-action="resume" hidden>恢复后台任务</button></div>
      <div class="unified-state" role="status" aria-live="polite"><b data-view="phase">准备就绪</b><p data-view="message"></p><div class="unified-steps"><span>创建任务</span><span>脚本与分镜</span><span>配音与包装</span><span>渲染与质检</span></div></div>
      </section><section class="sj-section sj-card unified-panel studio-chapter studio-delivery" id="studio-delivery" data-chapter="05"><div class="studio-section-head"><span class="studio-step">05</span><div><h2>预览与剪映草稿生成</h2></div></div><div data-view="empty" class="studio-preview-empty"><span>▷</span><b>暂无预览</b></div><div data-view="result" hidden><video data-view="preview" controls preload="metadata" aria-label="生成视频预览"></video><div data-view="editability"></div><ul data-view="issues"></ul><details><summary>还原范围与已知差异</summary><ul data-view="limits"></ul></details><div class="studio-export-area"><label class="unified-check"><input type="checkbox" data-field="ack">我了解上方质检提示和还原差异，保存草稿后继续检查</label><label>花字导出方式<select data-field="editable-emphasis"><option value="layered">可编辑花字＋独立装饰＋原生动画（推荐）</option><option value="appearance">保留原花字图片外观</option><option value="text">转为可改字文字（标签、渐变等装饰简化）</option></select></label><button class="sj-cta" data-action="export">保存可编辑剪映草稿</button><p data-view="export-result"></p></div></div></section>`;
    const main = root.querySelector(".sj-main");
    const packagingPanel = document.createElement("div");
    packagingPanel.className = "studio-packaging-editor";
    packagingPanel.innerHTML = `<h3>调整预览包装</h3>
      <div class="unified-grid"><label>基础字幕模板<select data-packaging="subtitle"></select></label><label>画面边框／水印<select data-packaging="brand"></select></label></div>
      <label class="unified-check"><input type="checkbox" data-field="auto-bgm" checked>自动匹配背景音乐</label><p class="unified-note" data-view="music-note"></p><details><summary>音乐、音效与图形包装</summary><div class="unified-grid"><label>背景音乐<select data-packaging="bgm"></select></label><label>音效<select data-packaging="sfx"></select></label><label>贴纸<select data-packaging="sticker"></select></label><label>标题卡<select data-packaging="title_card"></select></label><label>花字<select data-packaging="fancy_text"></select></label></div></details>
      <div class="unified-actions"><button class="sj-cta secondary" data-action="reference-packaging">去掉边框，字幕贴近参考片</button><button class="sj-cta secondary" data-action="match-reference">按参考片匹配字幕与镜头</button><button class="sj-cta secondary" data-action="match-bgm">自动匹配参考片 BGM</button><button class="sj-cta" data-action="repackage" disabled>更新当前预览（复用配音）</button></div><p class="unified-note" data-view="packaging-note" role="status">正在读取包装选项…</p>`;
    panel.querySelector("#studio-delivery").insertBefore(packagingPanel, panel.querySelector('[data-view="empty"]'));
    main.append(panel);
    entry.onclick = () => panel.querySelector("#studio-cutroom").scrollIntoView({behavior:"smooth", block:"start"});
    const field = name => panel.querySelector(`[data-field="${name}"]`);
    const view = name => panel.querySelector(`[data-view="${name}"]`);
    const button = name => panel.querySelector(`[data-action="${name}"]`);
    let noticeUntil = 0;
    const message = text => { view("message").textContent = text; noticeUntil = Date.now() + 12000; };
    let templates = [], ttsProfiles = [], selectedVoice = "", state = {}, disposed = false, catalogReady = false, currentPreview = "";
    let packagingCatalog = {}, packagingPlanId = "", packagingReady = false, lastPackagingRevision = "", packagingName = "";
    let packagingDirty = false;
    const packagingSlots = ["subtitle", "brand", "bgm", "sfx", "sticker", "title_card", "fancy_text"];
    const packagingField = slot => panel.querySelector(`[data-packaging="${slot}"]`);
    const packagingChoices = () => Object.fromEntries(packagingSlots.map(slot => [slot, packagingField(slot).value || null]));
    function referencePackaging() {
      packagingDirty = true;
      packagingField("brand").value = "";
      const candidates = (packagingCatalog.subtitle || []).filter(row => row.primary_hex === "#FFFFFF" && row.outline_hex);
      const preferred = candidates.find(row => row.fontname_guess?.includes("Smiley")) || candidates[0];
      if (preferred) packagingField("subtitle").value = preferred.id;
      view("packaging-note").textContent = "包装已修改，待更新预览";
    }
    function applyTemplate() {
      const template = templates.find(row => row.template_id === field("template").value);
      packagingDirty = false;
      if (!template) return;
      field("name").value = template.name;
      if (template.packaging_choices) {
        for (const slot of packagingSlots) packagingField(slot).value = template.packaging_choices[slot] || "";
        field("auto-bgm").checked = false;
        view("packaging-note").textContent = "";
      } else {
        view("packaging-note").textContent = "模板包装未配置";
      }
    }
    function showPackagingNote() {
      const subtitle = state.reference_match?.policy?.subtitle;
      view("packaging-note").textContent = subtitle ?
        `字幕 ${subtitle.primary_hex}${state.emphasis_report?.length ? ` · ${state.emphasis_report.length} 处重点` : ""}` :
        packagingName ? `当前包装：${packagingName}` : "";
    }
    async function loadPackaging() {
      const value = await api(`/packaging/${job}`);
      packagingCatalog = value.catalog;
      packagingPlanId = value.current?.plan_id || "";
      packagingName = value.current?.name || "";
      for (const slot of packagingSlots) {
        options(packagingField(slot), packagingCatalog[slot] || [], "id", "name", slot === "subtitle" ? "选择字幕样式" : "不使用");
        packagingField(slot).value = value.current?.choices[slot] || "";
      }
      if (!value.current) referencePackaging();
      packagingReady = true;
      showPackagingNote();
      button("repackage").disabled = !state.has_preview || ["running", "waiting_worker"].includes(state.status);
    }
    async function api(path, body) {
      const response = await fetch(base + path, body === undefined ? {} : {
        method: "POST", headers: {"Content-Type":"application/json"}, body: JSON.stringify(body)
      });
      const value = await response.json();
      if (!response.ok) throw Error(value.error || "请求失败，请检查本机服务。");
      return value;
    }
    function options(select, values, key, label, empty) {
      const old = select.value;
      select.innerHTML = `<option value="">${escape(empty)}</option>` + values.map(row => `<option value="${escape(row[key])}">${escape(row[label])}</option>`).join("");
      if (values.some(row => row[key] === old)) select.value = old;
      else if (values.length === 1) select.value = values[0][key];
    }
    function renderVoices() {
      const profile = ttsProfiles.find(row => row.profileId === field("tts").value);
      const voices = profile?.voices || [];
      const query = field("voice-search").value.trim().toLocaleLowerCase();
      const matches = voices.filter(row => [row.displayName, row.voiceId, row.gender,
        ...(row.languages || []), ...(row.styles || []), ...(row.emotions || [])].join(" ").toLocaleLowerCase().includes(query));
      options(field("voice"), matches, "voiceId", "displayName", query ? "选择搜索结果中的音色" : "选择本次配音音色");
      field("voice").value = matches.some(row => row.voiceId === selectedVoice) ? selectedVoice : "";
      field("voice").disabled = !matches.length;
      view("voice-note").textContent = !profile ? "请先选择配音配置。" : !voices.length ?
        "当前服务暂无可用音色，请在 剪辑工作台 更新音色目录。" :
        query && !matches.length ? "没有匹配音色" : "";
    }
    function selectProfile() {
      const profile = ttsProfiles.find(row => row.profileId === field("tts").value);
      const voices = profile?.voices || [];
      selectedVoice = voices.some(row => row.voiceId === selectedVoice) ? selectedVoice :
        (voices.some(row => row.voiceId === profile?.defaultVoiceId) ? profile.defaultVoiceId : "");
      field("voice-search").value = "";
      renderVoices();
    }
    async function loadCatalog() {
      const value = await api("/catalog");
      templates = value.editing_templates;
      options(field("template"), templates, "template_id", "name", "请先保存或选择剪法模板");
      options(field("library"), value.libraries.filter(row => row.online && row.purpose === "task_specific"), "library_id", "name", "选择项目素材库");
      ttsProfiles = value.tts_profiles.filter(row => row.enabled);
      options(field("tts"), ttsProfiles, "profileId", "displayName", "选择配音配置");
      selectProfile();
      const match = templates.find(row => row.source_job === job);
      if (!field("template").value && match) field("template").value = match.template_id;
      const selected = templates.find(row => row.template_id === field("template").value);
      if (selected && !field("name").value) field("name").value = selected.name;
      catalogReady = true;
    }
    function display(value) {
      state = value;
      const music = value.music_match;
      view("music-note").textContent = music ? `背景音乐：${music.selected.name} · ${music.selected.start.toFixed(1)} 秒起 · 淡出 ${music.fade_out} 秒` : "";
      const badge = root.querySelector("#draftStatus");
      if (badge) badge.textContent = ({running:"正在生成",waiting_worker:"等待后台执行",ready:"等待审看",qa_blocked:"预览有质检提示",exported:"草稿已保存",failed:"生成中断",needs_check:"需要核对"})[value.status] || "等待生成配置";
      const busy = ["running", "waiting_worker"].includes(value.status);
      button("generate").disabled = busy || !catalogReady;
      button("adopt").disabled = busy;
      button("export").disabled = busy || !value.has_preview;
      button("repackage").disabled = busy || !value.has_preview || !packagingReady;
      button("reference-packaging").disabled = busy || !packagingReady;
      button("match-reference").disabled = busy || !value.has_preview || !packagingReady;
      button("match-bgm").disabled = busy || !value.has_preview || !packagingReady;
      packagingSlots.forEach(slot => { packagingField(slot).disabled = busy || !packagingReady; });
      button("resume").hidden = !["failed", "running", "waiting_worker"].includes(value.status);
      view("phase").textContent = value.phase || "准备就绪";
      if (Date.now() >= noticeUntil) {
        if (value.error) view("message").textContent = value.error;
        else if (busy) view("message").textContent = value.mode === "repackage" ? "正在更新包装" : value.queue_position ? `排队第 ${value.queue_position} 位` : "正在处理";
        else if (value.has_preview) view("message").textContent = "待审看";
      }
      panel.classList.toggle("is-running", busy);
      view("result").hidden = !value.has_preview;
      view("empty").hidden = Boolean(value.has_preview);
      if (value.has_preview) {
        const url = `${base}/preview/${job}?candidate=${encodeURIComponent(value.candidate_id || "")}`;
        if (currentPreview !== url) { view("preview").src = url; currentPreview = url; }
        const edit = value.editability || {};
        view("editability").textContent = `${edit.clips || 0} 个独立镜头 · ${edit.texts || 0} 条可编辑文字 · ${(edit.audio || []).join(" / ")} · ${edit.overlays || 0} 个独立图层`;
        view("issues").innerHTML = (value.qa_issues || []).map(issue => `<li>${escape(issue.detail)}</li>`).join("");
        if (value.reference_match) {
          const report=value.reference_match;
          showPackagingNote();
          const motionName={pan_left:"向左横移",pan_right:"向右横移",pan_up:"向上移动",pan_down:"向下移动",zoom_in:"推近",zoom_out:"拉远",static:"静止",unknown:"尚无法判断"};
          const substitutes=report.shots.filter(row => row.substitution !== "exact" || row.short_material_loop || row.repeated_asset);
          view("editability").textContent += ` · 参考片 ${report.sourceShotCount} 镜头 · ${substitutes.length} 处替代`;
          view("issues").innerHTML += substitutes.map(row => `<li>镜头 ${row.shot}：目标 ${escape(motionName[row.desired] || row.desired)}，实测 ${escape(motionName[row.measured] || row.measured)}${row.short_material_loop ? "，素材较短需循环" : ""}${row.repeated_asset ? "，复用素材" : ""}（已采用可用素材替代）</li>`).join("");
        }
        view("limits").innerHTML = (value.limitations || []).map(text => `<li>${escape(text)}</li>`).join("");
      }
      view("export-result").textContent = value.draft_path ? `已保存：${value.draft_path}` : "";
      if (!busy && value.packaging_revision && value.packaging_revision !== lastPackagingRevision) {
        lastPackagingRevision = value.packaging_revision;
        loadPackaging().catch(error => message(error.message));
      }
    }
    async function action(name, fn) {
      const target = button(name); target.disabled = true; noticeUntil = 0;
      try { await fn(); } catch (error) { message(error.message); }
      finally { target.disabled = ["generate", "adopt", "repackage", "match-reference", "match-bgm"].includes(name) && ["running", "waiting_worker"].includes(state.status); }
    }
    button("save-template").onclick = () => action("save-template", async () => {
      const value = await api(`/templates/${job}`, {name:field("name").value.trim()});
      await loadCatalog(); field("template").value = value.template.template_id;
      message("剪法模板已保存。首次生成后可把实际采用的包装组合同步到 剪辑工作台。");
    });
    button("save-packaging").onclick = () => action("save-packaging", async () => {
      await api(`/save-packaging/${job}`, {template_id:field("template").value});
      await loadCatalog(); applyTemplate(); message("已同步完整包装规则，后续选择这个剪法模板时会一并复用。");
    });
    button("generate").onclick = () => action("generate", async () => {
      if (!field("voice").value) throw Error("请选择本次配音音色；可以清空搜索查看全部音色。");
      const value = await api(`/generate/${job}`, {template_id:field("template").value, script:field("script").value,
        library_id:field("library").value, tts_profile_id:field("tts").value, tts_voice_id:field("voice").value,
        packaging_choices:packagingReady && packagingDirty ? packagingChoices() : undefined, match_reference:true, auto_bgm:field("auto-bgm").checked,
        owns_materials:field("rights").checked, delegate_routine:field("delegate").checked, confirm_cost:field("cost").checked});
      display(value);
    });
    button("adopt").onclick = () => action("adopt", async () => {
      display(await api(`/adopt/${job}`, {confirm_reuse:true, template_id:field("template").value || null}));
    });
    button("resume").onclick = () => action("resume", async () => display(await api(`/resume/${job}`, {})));
    button("reference-packaging").onclick = referencePackaging;
    button("match-reference").onclick = () => action("match-reference", async () => {
      display(await api(`/repackage/${job}`, {choices:packagingChoices(), expected_plan_id:packagingPlanId, refresh_preview:true, match_reference:true}));
      message("正在匹配参考片的单行字幕、主色、位置和镜头数量，配音沿用现有音轨。");
    });
    packagingSlots.forEach(slot => { packagingField(slot).onchange = () => {
      packagingDirty = true;
      if (slot === "bgm") field("auto-bgm").checked = false;
    }; });
    button("match-bgm").onclick = () => action("match-bgm", async () => {
      display(await api(`/repackage/${job}`, {choices:packagingChoices(), expected_plan_id:packagingPlanId, refresh_preview:true, auto_bgm:true}));
      message("正在分析参考音乐并更新预览，现有配音保留。");
    });
    button("repackage").onclick = () => action("repackage", async () => {
      const value = await api(`/repackage/${job}`, {choices:packagingChoices(), expected_plan_id:packagingPlanId, refresh_preview:true});
      display(value);
      message("正在更新包装预览，现有镜头和配音会复用；没有重新调用配音服务。");
    });
    button("export").onclick = () => action("export", async () => {
      if (!field("ack").checked) throw Error("请先阅读并确认质检提示与还原差异。");
      view("export-result").textContent = "正在复制独立素材并写入草稿，请稍候…";
      display(await api(`/export/${job}`, {confirm_save:true, acknowledge_qa:field("ack").checked, emphasis_mode:field("editable-emphasis").value}));
    });
    field("template").onchange = applyTemplate;
    field("tts").onchange = selectProfile;
    field("voice-search").oninput = renderVoices;
    field("voice").onchange = () => { selectedVoice = field("voice").value; };
    async function poll() {
      if (disposed || !panel.isConnected) { disposed = true; return; }
      try { display(await api(`/status/${job}`)); } catch (error) { message(error.message); }
      setTimeout(poll, 2500);
    }
    (async () => {
      try {
        await loadCatalog();
        const value = await api(`/status/${job}`);
        if (value.script) field("script").value = value.script;
        const config = value.config || {};
        for (const [name, key] of [["template", "template_id"], ["library", "library_id"], ["tts", "tts_profile_id"]]) {
          if (config[key] && Array.from(field(name).options).some(option => option.value === config[key])) field(name).value = config[key];
        }
        if (config.tts_voice_id) selectedVoice = config.tts_voice_id;
        selectProfile();
        lastPackagingRevision = value.packaging_revision || "";
        display(value);
        await loadPackaging();
        applyTemplate();
      } catch (error) { message(error.message); }
      poll();
    })();
  };
})();
