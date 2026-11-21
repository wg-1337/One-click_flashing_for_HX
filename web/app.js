/* 一键刷机 — 前端逻辑 */
(() => {
  const $ = (id) => document.getElementById(id);
  const logEl = $("log");
  const dlg = $("dlg");
  const info = $("info");
  const snack = $("snack");

  let since = 0;
  let answered = new Set();
  let lastPhase = null;
  let promptOpen = false;
  let warned = false;
  let filter = "all";
  let rawLog = [];

  const STEP_ORDER = ["driver", "connect", "verify", "erase", "reboot", "guide", "freeze"];
  const PHASE_STEP = {
    idle: null, starting: "driver", checking_driver: "driver", driver_missing: "driver",
    waiting_device: "connect", connecting: "connect",
    reading_gpt: "verify",
    erase_prompt: "erase", erasing: "erase", erased: "erase",
    rebooting: "reboot",
    guide: "guide", adb_wait: "guide",
    freezing: "freeze", done: null, error: null,
  };
  const PHASE_TEXT = {
    idle: "空闲", starting: "准备中", checking_driver: "检查驱动",
    driver_missing: "缺少驱动", waiting_device: "等待设备进入 9008",
    connecting: "连接中", reading_gpt: "读取分区表",
    erase_prompt: "等待确认", erasing: "擦除中", erased: "擦除完成",
    rebooting: "重启设备", guide: "等待激活并连接 ADB", adb_wait: "等待 ADB",
    freezing: "停用应用", done: "完成", error: "出错",
  };
  const LVL = { info: "INFO", success: " OK ", warn: "WARN", error: "ERR ", muted: "  · " };
  const MANUAL_PHASES = ["reboot", "guide", "adb_wait", "freezing"];

  function syncManual() {
    const items = [...document.querySelectorAll("#manual-list li")];
    const firstUnchecked = items.findIndex((li) => !li.querySelector("input").checked);
    items.forEach((li, idx) => {
      const checked = li.querySelector("input").checked;
      li.classList.toggle("done", checked);
      li.classList.toggle("active", !checked && idx === firstUnchecked);
    });
    const n = firstUnchecked >= 0 ? firstUnchecked + 1 : 0;
    $("manual-hint").textContent = n ? `当前第 ${n} 步` : "5 步已完成";
  }

  function toast(text) {
    snack.textContent = text;
    snack.classList.add("show");
    clearTimeout(toast._t);
    toast._t = setTimeout(() => snack.classList.remove("show"), 2800);
  }

  const infoQueue = [];
  let infoOpen = false;

  function showInfo(title, text) {
    infoQueue.push([title, text]);
    if (!infoOpen) drainInfo();
  }

  function drainInfo() {
    const next = infoQueue.shift();
    if (!next) { infoOpen = false; return; }
    infoOpen = true;
    $("info-title").textContent = next[0];
    $("info-text").textContent = next[1];
    info.showModal();
    info.addEventListener("close", () => {
      infoOpen = false;
      setTimeout(drainInfo, 120);
    }, { once: true });
  }

  function addLine(ev) {
    rawLog.push(ev);
    const line = document.createElement("div");
    line.className = "line " + (ev.level || "info");
    if (filter !== "all" && ev.level !== filter) line.classList.add("hidden");
    line.innerHTML = `<span class="t">${ev.ts}</span>` +
                     `<span class="lv">${LVL[ev.level] || "INFO"}</span>` +
                     `<span class="m"></span>`;
    line.querySelector(".m").textContent = ev.text;
    logEl.appendChild(line);
  }

  function renderLog(events) {
    if (!events.length) return;
    for (const ev of events) addLine(ev);
    while (logEl.childElementCount > 700) logEl.removeChild(logEl.firstChild);
    logEl.scrollTop = logEl.scrollHeight;
  }

  function setChip(el, cls, value) {
    el.className = "chip " + (cls || "");
    el.querySelector("b").textContent = value;
  }

  function renderSteps(phase) {
    const activeKey = PHASE_STEP[phase] || null;
    const activeIdx = STEP_ORDER.indexOf(activeKey);
    document.querySelectorAll("#timeline li").forEach((li) => {
      const key = li.dataset.step;
      const idx = STEP_ORDER.indexOf(key);
      li.classList.remove("active", "done", "fail");
      if (phase === "error" && key === activeKey) return li.classList.add("fail");
      if (phase === "done") return li.classList.add("done");
      if (activeIdx < 0) return;
      if (idx < activeIdx) li.classList.add("done");
      else if (idx === activeIdx) li.classList.add("active");
    });
  }

  function renderSpec(d) {
    $("s-chip").textContent = d.hwid || "—";
    $("s-serial").textContent = d.serial || "—";
    $("s-storage").textContent = d.memory || "—";
    $("s-cap").textContent = d.capacity || "—";
    $("s-parts").textContent = d.partitions != null ? String(d.partitions) : "—";
    $("s-guid").textContent = d.disk_guid || "—";
    $("dev-hint").textContent = d.serial ? "已连接" : "未连接";
  }

  function renderProgress(p, running, phase) {
    const wrap = $("bar").parentElement;
    if (p && p.total > 0) {
      wrap.classList.remove("indet");
      const pct = Math.max(0, Math.min(100, (p.value / p.total) * 100));
      $("bar").style.width = pct.toFixed(1) + "%";
      $("phase-label").textContent = p.label || PHASE_TEXT[phase] || "进行中";
      $("meter-val").textContent = pct.toFixed(0) + "%";
    } else if (running) {
      wrap.classList.add("indet");
      $("bar").style.width = "30%";
      $("phase-label").textContent = PHASE_TEXT[phase] || "进行中";
      $("meter-val").textContent = "—";
    } else {
      wrap.classList.remove("indet");
      $("bar").style.width = "0%";
      $("phase-label").textContent = PHASE_TEXT[phase] || "空闲";
      $("meter-val").textContent = "—";
    }
  }

  function handlePrompt(p) {
    if (!p) { promptOpen = false; return; }
    if (answered.has(p.id) || promptOpen) return;
    promptOpen = true;
    $("dlg-title").textContent = p.title;
    $("dlg-text").textContent = p.text;
    $("dlg-sub").textContent = p.kind === "erase2" ? "再次确认，擦除后无法恢复" : "擦除后无法恢复";
    dlg.showModal();
    dlg.addEventListener("close", async () => {
      const yes = dlg.returnValue === "yes";
      answered.add(p.id);
      promptOpen = false;
      await fetch("/api/answer", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ id: p.id, answer: yes }),
      });
    }, { once: true });
  }

  function onPhase(phase, st) {
    if (phase === lastPhase) return;
    lastPhase = phase;
    $("btn-flash").disabled = st.running || phase === "done";
    $("btn-adb").disabled = st.running;

    $("manual-card").hidden = !MANUAL_PHASES.includes(phase);
    if (MANUAL_PHASES.includes(phase)) syncManual();

    if (phase === "reboot") {
      showInfo("设备即将重启",
        "接下来需要在平板上手动完成 5 步，页面下方的“现在需要你手动完成”会一直显示：\n\n" +
        "1. 恢复出厂设置\n2. 连接无网络热点完成激活\n3. 打开开发者选项\n4. 开启 USB 调试\n5. 连接电脑并授权\n\n" +
        "完成后会自动检测 ADB 连接。");
    }
    if (phase === "guide") toast("设备已重启，按下方 1–5 步操作");
    if (phase === "adb_wait") toast("等待设备连接，请确认已开启 USB 调试");
    if (phase === "done" && !warned) {
      warned = true;
      showInfo("请不要使用华为账号相关功能",
        "已停用 4 个应用：\n" +
        "com.huawei.hwid\ncom.huawei.android.hwouc\ncom.huawei.hms.dupdateengine\ncom.huawei.android.hsf\n\n" +
        "请不要打开应用市场、云空间、华为账号、系统更新等 HMS / HMS Core 相关功能，" +
        "否则可能重新触发限制，前面的步骤就白做了。\n\n" +
        "如需恢复：adb shell pm enable <包名>");
    }
    if (phase === "error") {
      toast("流程中断，请查看输出");
      $("btn-flash").disabled = false;
    }
  }

  async function tick() {
    try {
      const res = await fetch("/api/state?since=" + since, { cache: "no-store" });
      const st = await res.json();
      if (st.events && st.events.length) {
        since = st.events[st.events.length - 1].id;
        renderLog(st.events);
      }
      if (st.version) $("ver").textContent = "v" + st.version;
      setChip($("c-driver"), st.driver_ok ? "ok" : "warn", st.driver_ok ? "已安装" : "未安装");
      const port = (st.edl_ports || [])[0];
      setChip($("c-edl"), port ? "ok" : "", port || "未连接");
      setChip($("c-adb"), st.adb_serial ? "ok" : "", st.adb_serial ? "已连接" : "未连接");

      renderSpec(st.device || {});
      renderSteps(st.phase);
      renderProgress(st.progress, st.running, st.phase);
      handlePrompt(st.prompt);
      onPhase(st.phase, st);
    } catch (e) { /* 后端可能正在重启 */ }
    finally { setTimeout(tick, 700); }
  }

  $("btn-flash").onclick = async () => {
    $("btn-flash").disabled = true;
    const r = await fetch("/api/flash/start", { method: "POST" }).then((x) => x.json());
    if (!r.ok) { toast(r.error || "启动失败"); $("btn-flash").disabled = false; }
  };
  $("btn-adb").onclick = async () => {
    const btn = $("btn-adb");
    const label = btn.textContent;
    btn.disabled = true;
    btn.textContent = "探测中…";
    try {
      const r = await fetch("/api/adb/disable", { method: "POST" }).then((x) => x.json());
      if (r.ok) {
        const list = (r.results || []).map((x) => `${x.ok ? "✓" : "✗"} ${x.pkg}`).join("\n");
        showInfo("已停用相关应用",
          `设备：${r.serial}\n\n${list}\n\n` +
          "请不要打开应用市场、云空间、华为账号、系统更新等华为账号相关功能。\n" +
          "如需恢复：adb shell pm enable <包名>");
      } else if (r.reason === "no_device") {
        showInfo("未检测到设备",
          "用数据线连接电脑，在平板上打开 USB 调试并允许授权，然后重新点“仅 ADB 禁用”。");
      } else if (r.reason === "unauthorized") {
        showInfo("需要授权",
          "平板上应该弹出了 USB 调试授权窗口：勾选“始终允许”并确定，然后再试一次。");
      } else if (r.reason === "busy") {
        toast("任务进行中，请稍候");
      } else if (r.reason === "adb_missing") {
        showInfo("缺少 adb", "未找到 " + (r.detail || "adb") + "，请使用完整安装包。");
      } else {
        showInfo("停用未完成", "设备状态：" + (r.reason || "未知"));
      }
    } catch (e) {
      toast("请求失败");
    } finally {
      btn.disabled = false;
      btn.textContent = label;
    }
  };
  $("btn-driver").onclick = () => {
    toast("正在安装高通驱动，请在弹出的窗口里确认");
    fetch("/api/install-driver", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ target: "qcom" }),
    });
  };
  $("btn-addrv").onclick = () => {
    toast("正在安装 ADB 驱动");
    fetch("/api/install-driver", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ target: "adb" }),
    });
  };
  $("btn-reset").onclick = async () => {
    const r = await fetch("/api/reset", { method: "POST" }).then((x) => x.json());
    if (!r.ok) return toast(r.error || "重置失败");
    warned = false; lastPhase = null; toast("已重置");
  };

  $("btn-clear").onclick = () => { logEl.innerHTML = ""; rawLog = []; };
  $("btn-copy").onclick = async () => {
    try {
      await navigator.clipboard.writeText(rawLog.map((e) => `${e.ts} ${e.text}`).join("\n"));
      toast("已复制到剪贴板");
    } catch { toast("复制失败"); }
  };
  $("btn-fold").onclick = () => $("console").classList.toggle("folded");

  $("filters").onclick = (e) => {
    const btn = e.target.closest("button");
    if (!btn) return;
    filter = btn.dataset.lvl;
    [...$("filters").children].forEach((b) => b.classList.toggle("on", b === btn));
    document.querySelectorAll("#log .line").forEach((l) => {
      const lvl = [...l.classList].find((c) => ["info", "success", "warn", "error", "muted"].includes(c));
      l.classList.toggle("hidden", filter !== "all" && lvl !== filter);
    });
  };

  document.querySelectorAll("#manual-list input").forEach((cb) => {
    cb.onchange = syncManual;
  });
  document.querySelectorAll("#manual-list li").forEach((li) => {
    li.addEventListener("click", (e) => {
      if (e.target.tagName === "INPUT") return;
      const box = li.querySelector("input");
      box.checked = !box.checked;
      syncManual();
    });
  });

  tick();

  /* 预览：?demo=dialog|info */
  const demo = new URLSearchParams(location.search).get("demo");
  if (demo) {
    setTimeout(() => {
      if (demo === "manual") {
        $("manual-card").hidden = false;
        syncManual();
      } else if (demo === "reboot") {
        $("manual-card").hidden = false;
        syncManual();
        showInfo("设备即将重启",
          "接下来需要在平板上手动完成 5 步，页面下方的“现在需要你手动完成”会一直显示：\n\n" +
          "1. 恢复出厂设置\n2. 连接无网络热点完成激活\n3. 打开开发者选项\n4. 开启 USB 调试\n5. 连接电脑并授权");
      } else if (demo === "info") {
        showInfo("请不要使用华为账号相关功能",
          "已停用：com.huawei.hwid、com.huawei.android.hwouc、com.huawei.hms.dupdateengine、com.huawei.android.hsf。\n\n之后不要打开应用市场、云空间、华为账号等华为账号相关功能。");
      } else {
        handlePrompt({ id: -1, kind: "erase", title: "是否擦除 userdata 和 version？",
          text: "将擦除：userdata（48.29 GB）、version（576.00 MB）" });
      }
    }, 500);
  }
})();
