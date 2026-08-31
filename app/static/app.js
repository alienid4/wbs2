/* 專案 WBS 追蹤 — 前端。零外部相依。 */
let currentPerson = null;  // Server 模式登入後的自己；單機版一律是 null（不強制登入）
// 這個工作項目，「我」改不改得動——跟後端 server.py 的 _can_edit_others 同一套規則：
// 沒登入（單機版）或我是 manager/admin 一律能改；一般使用者只能改自己負責的。
// 純粹前端判斷用來決定「要不要把欄位灰掉」，真正擋下的防線在後端，這裡只是不要
// 讓看起來能改、點下去才被 403 打回票，體驗上很奇怪。
function canEditTask(owner) {
  if (!currentPerson) return true;
  if (currentPerson.role !== "user") return true;
  return (owner || "") === currentPerson.name;
}
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, c =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

const api = async (url, opt) => {
  const r = await fetch(url, opt);
  const t = await r.text();
  let j; try { j = JSON.parse(t); } catch { j = { error: t }; }
  if (r.status === 401) { showLogin(); throw new Error("尚未登入"); }
  if (!r.ok && r.status !== 409) throw new Error(j.error || r.status);
  return j;
};
const post = (url, body) => api(url, {
  method: "POST", headers: { "Content-Type": "application/json" },
  body: JSON.stringify(body ?? {})
});

let toastT;
function toast(msg) {
  const el = $("#toast"); el.textContent = msg; el.classList.add("on");
  clearTimeout(toastT); toastT = setTimeout(() => el.classList.remove("on"), 2600);
}

const WHY = { overdue: "red", will_slip: "red", must_start: "red", critical: "red", cyclic: "red", tight: "amber", ok: "grey", done: "green" };
const STATUSES = ["未開始", "進行中", "已完成", "延遲"];

/* ---------------------------------------------------------- tabs */
// 頂層分頁：今日／每個專案（動態插入）／週報／設定——都是同一顆按鈕群，用事件代理
// 而不是逐一綁 onclick，這樣動態插入的專案分頁按鈕不用另外補綁。
$("#tabs").addEventListener("click", e => {
  const b = e.target.closest("button[data-tab]");
  if (!b) return;
  localStorage.setItem("lastTab", b.dataset.tab); // 記住這次點的分頁，下次開啟直接回到這裡
  $$("#tabs button").forEach(x => x.classList.toggle("on", x === b));
  $$(".tab").forEach(s => s.classList.toggle("on", s.id === "tab-" + b.dataset.tab));
  if (b.dataset.pid) return renderProjectPage(+b.dataset.pid);
  const loader = { overview: loadOverviewTab, today: loadToday, workload: loadWorkloadTab,
                  report: loadReportTab, settings: loadSettings }[b.dataset.tab];
  if (loader) loader();
});
// 子分頁（總覽／WBS 表／文件與階段）：同一個道理，代理到 main，不逐一綁
$("main").addEventListener("click", e => {
  const b = e.target.closest(".subtabs button");
  if (!b) return;
  const nav = b.closest(".subtabs");
  nav.querySelectorAll("button").forEach(x => x.classList.toggle("on", x === b));
  nav.parentElement.querySelectorAll(":scope > .subtab").forEach(s =>
    s.classList.toggle("on", s.dataset.subpanel === b.dataset.sub));
});

/* ---------------------------------------------------------- 今日 */
async function renderOnboardBanner() {
  const box = $("#onboardBanner");
  if (!box || box.dataset.dismissed) return;
  const [cfg] = await Promise.all([api("/api/config")]);
  await ensureProjects();
  const states = await Promise.all(projects.map(p => api(`/api/projects/${p.id}/state`)));
  const todo = [];
  if (!(cfg.holidays || []).length)
    todo.push(["尚未設定國定假日", "未設定將導致浮時計算納入非工作日", "settings"]);
  const unfrozen = states.filter(st => st && !st.summary.baseline_set);
  if (unfrozen.length)
    todo.push([`${unfrozen.map(st => st.project.code).join("、")} 尚未凍結基準線`,
      "凍結基準線前無承諾完工日可供比較，落後判定無法正確顯示",
      "p" + unfrozen[0].project.id]);
  if (!todo.length) { box.innerHTML = ""; return; }
  box.innerHTML = `<div class="onboard">
    <b>提醒事項：</b>
    ${todo.map(([t, h, tab]) => `<span class="ob-item" data-tab="${tab}">${esc(t)}
      <span class="sub">（${esc(h)}）</span></span>`).join("　")}
    <button class="ghost dismiss">關閉</button>
  </div>`;
  $$(".ob-item", box).forEach(el => el.onclick = () =>
    $(`#tabs button[data-tab=${el.dataset.tab}]`).click());
  $(".dismiss", box).onclick = () => { box.dataset.dismissed = "1"; box.innerHTML = ""; };
}

async function loadToday() {
  renderOnboardBanner();
  const d = await api("/api/today");
  const day = new Date(d.date + "T00:00:00");
  const wd = "日一二三四五六"[day.getDay()];
  $("#dayTitle").textContent = `${d.date}（週${wd}）`;
  $("#dayNote").textContent = d.is_workday ? "工作日" : "非工作日，無須完成之項目";

  const n = k => d[k].length;
  $("#kpis").innerHTML = [
    ["red", n("overdue"), "已逾期"],
    ["red", n("must"), "不能拖"],
    ["amber", n("should"), "浮時吃緊"],
    ["", n("may"), "有彈性"],
    ["red", d.blocked_gates.length, "階段缺件"],
  ].map(([c, v, l]) => `<div class="kpi ${v ? c : ""}"><b>${v}</b><span>${l}</span></div>`).join("");

  const item = t => {
    const locked = !canEditTask(t.owner);
    const dis = locked ? " disabled" : "";
    return `
    <div class="item f-${t.flag} ${locked ? "rowLocked" : ""}" data-id="${t.id}"
        ${locked ? 'title="這不是你負責的項目，只有主管/管理者能改"' : ""}>
      <div class="main">
        <div class="ttl">${t.mark} ${esc(t.project_code)}｜${esc(t.wbs_no)} ${esc(t.name)}</div>
        <div class="meta">
          ${t.block ? `<span class="tag">${esc(t.block)}</span> ` : ""}
          ${esc(t.planned_start)} ～ ${esc(t.planned_end)}
          ・最晚完成 ${esc(t.lf) || "—"}
          ${t.stage_code ? `・階段 ${esc(t.stage_code)}` : ""}
          ${t.note ? `<br>${esc(t.note).slice(0, 120)}` : ""}
        </div>
      </div>
      <div class="progwrap">
        <div class="prog"><i style="width:${t.progress || 0}%"></i></div>
        <select class="pg"${dis}>${[0, 50, 100].map(x => `<option value="${x}" ${x === (t.progress || 0) ? "selected" : ""}>${x}%</option>`).join("")}</select>
        <select class="st"${dis}>${STATUSES.map(s => `<option ${s === t.status ? "selected" : ""}>${s}</option>`).join("")}</select>
        <button class="ghost save"${dis}>記錄</button>
      </div>
      <div class="why ${WHY[t.flag]}">${esc(t.flag_reason)}</div>
    </div>`;
  };

  const group = (title, key, note) => {
    if (!d[key].length) return "";
    return `<div class="group"><div class="grouphd"><h2>${title}</h2>
      <span class="pill">${d[key].length} 項</span>
      ${note ? `<span class="sub">${note}</span>` : ""}</div>
      ${d[key].map(item).join("")}</div>`;
  };

  let html = "";
  html += group("🛑 已逾期，請優先處理", "overdue", "逾期每增加一天，後續項目排程隨之延後");
  html += group("🔴 今日不可延遲", "must", "浮時已為零或即將用盡");
  html += group("🟡 浮時吃緊", "should", "尚餘 1–2 天可調度");
  html += group("⚪ 尚有彈性", "may", "此類落後尚不影響專案整體進度");

  if (d.blocked_gates.length) {
    html += `<div class="group"><div class="grouphd"><h2>📄 階段文件缺件</h2>
      <span class="pill">${d.blocked_gates.length}</span>
      <span class="sub">工作已完成但文件未齊備，致階段無法結案</span></div>` +
      d.blocked_gates.map(g => `<div class="item f-tight"><div class="main">
        <div class="ttl">${esc(g.project)}　${esc(g.stage)}</div>
        <div class="meta">出場條件：${esc(g.exit_gate || "—")}<br>
        尚缺：${g.missing.map(m => esc(m.name) + "（" + esc(m.doc_code) + "）").join("、")}</div>
      </div></div>`).join("") + "</div>";
  }
  if (!html) html = `<div class="empty">今日無排定工作，亦無逾期項目。<br>
    <span class="sub">若預期應有項目卻未顯示，請確認 WBS 是否已展開至週交付層級。</span></div>`;
  $("#todayBody").innerHTML = html;

  $$("#todayBody .save").forEach(btn => btn.onclick = async e => {
    const box = e.target.closest(".item");
    await post(`/api/tasks/${box.dataset.id}/checkin`, {
      progress: +$(".pg", box).value, status: $(".st", box).value,
    });
    toast("已記錄，系統將自動彙入週報"); loadToday();
  });
}

/* ---------------------------------------------------------- 專案分頁 */
let projects = [];

async function ensureProjects() {
  if (!projects.length) projects = await api("/api/projects");
  return projects;
}

let people = [];

async function ensurePeople() {
  if (!people.length) people = await api("/api/people");
  return people;
}

// ed 代碼：0 唯讀 / 1 文字可編 / 2 狀態下拉 / 3 日期輸入 / 4 進度下拉(0/50/100)
// 第 4 欄 adv=1 代表「進階欄位」，預設收起來，按「進階欄位」才展開。
const COLS = [
  ["wbs_no", "項次", 1, 0], ["stage_code", "階段", 1, 0], ["name", "工作項目", 1, 0],
  ["planned_start", "開始", 3, 0], ["planned_end", "結束", 3, 0],
  ["predecessors", "前置", 1, 1], ["hard_deadline", "硬性死線", 3, 1],
  ["baseline_end", "承諾完成(基準)", 0, 1],
  ["lf", "最晚完成", 0, 1], ["total_float", "總浮時", 0, 1], ["live_float", "剩餘浮時", 0, 1],
  ["status", "狀態", 2, 0], ["progress", "進度", 4, 0], ["owner", "負責人", 1, 0],
  ["actual_finish", "實際完成", 3, 1],
  ["flag_reason", "原因", 0, 0], ["note", "備註", 1, 0],
];
const PROGRESS_STEPS = [0, 50, 100];

// 儲存失敗（400）時用來還原格子原值，不讓打錯字的輸入悶不吭聲留在畫面上
async function saveTask(patch, revert) {
  try {
    await post("/api/tasks", patch);
    return true;
  } catch (e) {
    toast(e.message || "儲存失敗");
    if (revert) revert();
    return false;
  }
}

// 階段圖＋狀態句（＋里程碑＋KPI）不再是要點進去才看得到的子分頁——常駐在頂端，
// 不管你在「WBS 表」還是「文件與階段」都看得到，回報用的資訊不該藏在一次點擊之後。
function projectSectionHTML() {
  return `
    <div class="projhd"><h2></h2></div>
    <div class="toolbar projtools">
      <button class="ghost scanBtn">掃描文件目錄</button>
      <button class="ghost mkdirBtn">建立目錄骨架</button>
      <span class="spacer"></span>
      <span class="sub scanInfo"></span>
    </div>
    <div class="projhero"></div>
    <nav class="subtabs">
      <button data-sub="wbs" class="on">WBS 表</button>
      <button data-sub="docs">文件與階段</button>
    </nav>
    <div class="subtab on" data-subpanel="wbs"></div>
    <div class="subtab" data-subpanel="docs"></div>`;
}

async function initProjectPages() {
  await ensureProjects();
  const nav = $("#tabs"), reportBtn = $('#tabs button[data-tab="report"]');
  const main = $("main"), reportSection = $("#tab-report");
  projects.forEach(p => {
    const btn = document.createElement("button");
    btn.dataset.tab = "p" + p.id; btn.dataset.pid = p.id;
    // 分頁只顯示名稱，不顯示 P01/P02 這種代號——使用者要看就知道是哪個案子，不用先
    // 記代號對照表。截掉括號附註（例如「（Phase 1/2）」），分頁列才不會被拉太長；
    // 完整名稱本來就在每個專案分頁自己的標題（.projhd h2）裡完整顯示一次。
    const shortName = (p.name || p.code).split(/[（(]/)[0].trim();
    btn.title = p.name;
    btn.innerHTML = `<span class="proj-dot" style="background:${esc(p.color || "#2563eb")}"></span>${esc(shortName)}`;
    btn.draggable = true;
    btn.ondragstart = e => {
      e.dataTransfer.setData("text/plain", String(p.id));
      e.dataTransfer.effectAllowed = "move";
      btn.classList.add("dragging");
    };
    btn.ondragend = () => btn.classList.remove("dragging");
    btn.ondragover = e => e.preventDefault();
    btn.ondrop = async e => {
      e.preventDefault();
      const srcId = +e.dataTransfer.getData("text/plain");
      if (!srcId || srcId === p.id) return;
      const order = projects.map(pp => pp.id);
      const si = order.indexOf(srcId), ti = order.indexOf(p.id);
      order.splice(ti, 0, order.splice(si, 1)[0]);
      try {
        await post("/api/projects/reorder", { order });
        toast("專案順序已更新，正在重新整理畫面…");
        // 專案分頁的按鈕跟區塊是照順序一次建好的，順序改變後最乾淨的做法是
        // 整頁重新載入，不要手動搬移一堆已經建好的 DOM（容易漏掉某個綁定）。
        location.reload();
      } catch (err) {
        toast(err.message || "調整順序失敗");
      }
    };
    nav.insertBefore(btn, reportBtn);

    const section = document.createElement("section");
    section.id = "tab-p" + p.id; section.className = "tab";
    section.innerHTML = projectSectionHTML();
    section.querySelector(".projhd h2").textContent = `${p.code}　${p.name}`;
    main.insertBefore(section, reportSection);
  });
}

async function renderProjectPage(pid) {
  const section = $(`#tab-p${pid}`);
  if (!section) return;
  const [st, d] = await Promise.all([
    api(`/api/projects/${pid}/state`), api(`/api/projects/${pid}/docs`),
  ]);
  await ensurePeople();
  renderProjectTools(pid, section);
  renderProjectHero(section, st, d);
  renderWbsPanel(pid, section, st);
  renderDocsPanel(pid, section, d);
}

// 掃描文件目錄／建立目錄骨架不是「文件與階段」子分頁專屬的動作，是整個專案層級的
// 工具，跟階段圖同一層級常駐在最頂端，不用切進子分頁才找得到。
function renderProjectTools(pid, section) {
  const bar = section.querySelector(".projtools");
  bar.querySelector(".scanBtn").onclick = async () => {
    const r = await post(`/api/projects/${pid}/scan`);
    toast(r.error ? r.error : `掃描完成，共 ${r.files} 個檔案，其中 ${r.matched} 個已依文件代碼比對成功`);
    renderProjectPage(pid);
  };
  bar.querySelector(".mkdirBtn").onclick = async () => {
    const r = await post(`/api/projects/${pid}/build-folders`);
    toast(r.error ? r.error : (r.created.length ? `已建立 ${r.created.length} 個資料夾` : "目錄結構已存在，未重複建立"));
  };
}

// 「今天」落在哪一站：日期區間內就是那站；卡在兩站中間的空隙，算給接下來還沒開始
// 那站（意思是「現在該往那邊推進了」）；全部都過去了就算在最後一站。
function findTodayStageIndex(stages) {
  const today = new Date(); today.setHours(0, 0, 0, 0);
  const parse = s => s ? new Date(s + "T00:00:00") : null;
  for (let i = 0; i < stages.length; i++) {
    const s = parse(stages[i].planned_start), e = parse(stages[i].planned_end);
    if (s && e && today >= s && today <= e) return i;
  }
  for (let i = 0; i < stages.length; i++) {
    const s = parse(stages[i].planned_start);
    if (s && today < s) return i;
  }
  return stages.length - 1;
}

function stagePathHTML(stages, cur) {
  const todayIdx = findTodayStageIndex(stages);
  return stages.map((stg, i) => {
    const done = stg.status === "已完成";
    const now = !done && cur && cur.code === stg.code;
    const cls = done ? "done" : now ? "now" : "";
    const dot = done ? "✓" : now ? "●" : "○";
    const line = i < stages.length - 1
      ? `<div class="sp-line ${done ? "done" : ""}"></div>` : "";
    const dateTxt = (stg.planned_start && stg.planned_end)
      ? `${stg.planned_start.slice(5).replace("-", "/")}~${stg.planned_end.slice(5).replace("-", "/")}`
      : "—";
    const marker = i === todayIdx
      ? `<span class="sp-today" title="今天"><span class="sp-today-tri">▽</span></span>`
      : `<span class="sp-today ph"></span>`;
    // 「進行中」只講狀態、不講講到哪——沒有這個數字，「進行中」跟「剛開始」在畫面上長得
    // 一樣。跟階段卡片的 stagepct 用同一個 avg_progress，兩處看到的數字要一致。
    const pct = stg.gate ? stg.gate.avg_progress : 0;
    const pctCls = pct === 100 ? "full" : pct === 0 ? "" : "partial";
    return `<div class="sp-item ${cls} clickable" data-code="${esc(stg.code)}">
      ${marker}
      <span class="dot">${dot}</span>
      <span class="lb">${esc(stg.code)}<br>${esc(stg.name)}</span>
      <span class="sp-pct ${pctCls}">${pct}%</span>
      <span class="spdate">${dateTxt}</span>
    </div>${line}`;
  }).join("");
}

// 每個專案記住使用者上次點開看哪一站的細節，換分頁再切回來還在同一站，
// 不用每次都重新點——存在模組層的 Map，不是 DOM 上，重整頁面才會重設。
const stageSelection = new Map();

// 階段總覽框自己的鎖——只鎖這個框裡的名稱/日期編輯跟拖拉排序，不影響其他地方
// （原本做過一個全站通用的鎖，範圍太大被使用者反映拿掉了，只留這個框自己的）。
let heroLocked = localStorage.getItem("heroLocked") !== "0";

function pctBadgeClass(ready, total) {
  if (!total) return "";
  if (ready === total) return "full";
  if (ready === 0) return "";
  return "partial";
}

// 精簡總覽列：不用捲動，一次看完所有站——節點只留代號/名稱/日期跟一個「文件交了
// 幾/幾份」的徽章，細節不塞在這裡，點下去才展開（見 stageDetailHTML）。
function stageStripHTML(stages, curCode, selCode) {
  const todayIdx = findTodayStageIndex(stages);
  return `<div class="strip">
    <div class="rail"></div>
    <div class="rail-done" style="width:${stages.length > 1
      ? (stages.filter(s => s.status === "已完成").length / (stages.length - 1) * 100) : 0}%"></div>
    ${stages.map((stg, i) => {
      const done = stg.status === "已完成";
      const now = !done && curCode === stg.code;
      const cls = (done ? "done" : now ? "now" : "") + (stg.code === selCode ? " sel" : "");
      const dot = done ? "✓" : (i + 1);
      const dateTxt = (stg.planned_start && stg.planned_end)
        ? `${stg.planned_start.slice(5).replace("-", "/")}~${stg.planned_end.slice(5).replace("-", "/")}`
        : "—";
      const g = stg.gate || {};
      const marker = i === todayIdx
        ? `<span class="sp-today"><span class="sp-today-tri">▽</span></span>` : `<span class="sp-today ph"></span>`;
      // 名稱可以直接改（點了就能編輯，跟文件名稱同一套 contenteditable）；日期也可以直接改——
      // 這裡顯示的是「底下任務裡最早開始／最晚結束」彙總出來的範圍，不是獨立欄位，所以
      // 改「開始」實際上是去改「開始最早的那個任務」，改「結束」是去改「結束最晚的那個任務」，
      // 永遠只精準動到一個任務，不會有「一次改動悄悄波及好幾個任務」的問題
      // （2026-08-28 使用者要求：能直接在這裡改，但要避免不知道波及了誰）。
      return `<div class="sp-node ${cls}" data-code="${esc(stg.code)}" data-id="${stg.id}" draggable="true">
        ${marker}
        <span class="dot">${dot}</span>
        <span class="lb">${esc(stg.code)}<br>
          <span class="sp-name" contenteditable data-code="${esc(stg.code)}">${esc(stg.name)}</span>
        </span>
        <span class="spdate" data-code="${esc(stg.code)}"
              data-editable="${stg.planned_start && stg.planned_end ? "1" : "0"}">${dateTxt}</span>
        <span class="sp-badge ${pctBadgeClass(g.all_ready, g.all_total)}">${g.all_ready ?? 0}/${g.all_total ?? 0}</span>
      </div>`;
    }).join("")}
  </div>`;
}

// 點開的細節區：一次只看一站——左邊是這站在做什麼＋底下掛哪些工作項目，
// 右邊是這站要交的文件清單（沿用文件與階段子分頁同一份資料，不重新定義一次）。
function stageDetailHTML(stage, docStage, tasks, pid) {
  const docs = docStage ? docStage.docs : [];
  const stageTasks = tasks.filter(t => t.stage_code === stage.code);
  return `<div class="stagedetail">
    <div>
      <h2>${esc(stage.code)} ${esc(stage.name)}</h2>
      <p class="purpose">${esc(stage.purpose || "—")}<br>出場條件：${esc(stage.exit_gate || "—")}</p>
      ${stageTasks.length ? `<ul class="dtasks">${stageTasks.map(t => {
        const locked = !canEditTask(t.owner);
        return `
        <li class="${t.status === "已完成" ? "done" : t.status === "進行中" ? "now" : ""} ${locked ? "rowLocked" : ""}"
            ${locked ? 'title="這不是你負責的項目，只有主管/管理者能改"' : ""}>
          <span class="dtname">${esc(t.name)}</span>
          <span class="dtdates">
            <input type="date" class="dtdate" data-id="${t.id}" data-k="planned_start"
                   value="${esc(t.planned_start || "")}" data-orig="${esc(t.planned_start || "")}"${locked ? " disabled" : ""}>
            ～
            <input type="date" class="dtdate" data-id="${t.id}" data-k="planned_end"
                   value="${esc(t.planned_end || "")}" data-orig="${esc(t.planned_end || "")}"${locked ? " disabled" : ""}>
          </span>
        </li>`;
      }).join("")}</ul>` : '<p class="sub">此階段目前無工作項目。</p>'}
      <button class="ghost gotoDocs" data-code="${esc(stage.code)}">在「文件與階段」編輯 →</button>
    </div>
    <div class="ddocs">
      <div class="dhd">本階段應繳文件</div>
      ${docs.length ? docs.map(x => {
        // 掃到檔案就讓名稱本身可以點開下載——不然使用者在這個精簡視圖只看得到
        // 「交了沒」，看不到「交的是哪個檔案」，還要跳去文件與階段子分頁才能點。
        const nameHTML = x.latest
          ? `<a href="/api/docs/file/${x.latest.id}" target="_blank" rel="noopener">${esc(x.name)}</a>`
          : esc(x.name);
        return `<div class="docit ${x.ready ? "ready" : ""} ${x.required ? "" : "opt"}">
          <span class="box"></span>${nameHTML}${x.required ? "" : '<span class="tag">選繳</span>'}
        </div>`;
      }).join("") : '<p class="sub">此階段未設定應繳文件。</p>'}
    </div>
  </div>`;
}

/* ---- 常駐頂端：精簡階段總覽（點開看單一站細節）+ 里程碑時間軸 + KPI ---- */
function renderProjectHero(section, st, d) {
  const box = section.querySelector(".projhero");
  const s = st.summary;
  const pid = st.project.id;
  const curCode = st.current_stage ? st.current_stage.code : null;
  if (!stageSelection.has(pid)) stageSelection.set(pid, curCode || (st.stages[0] && st.stages[0].code));
  const selCode = stageSelection.get(pid);
  const selStage = st.stages.find(x => x.code === selCode) || st.stages[0];
  const selDocStage = d.stages.find(x => x.code === selCode);

  box.innerHTML = `
    <div class="stagehero ${heroLocked ? "locked" : ""}">
      <button class="heroLockBtn" title="鎖定或解鎖本區塊之編輯">${heroLocked ? "🔒" : "🔓"}</button>
      ${stageStripHTML(st.stages, curCode, selCode)}
      ${selStage ? stageDetailHTML(selStage, selDocStage, st.tasks, pid) : ""}
      <p class="statusline">${esc(st.status_sentence)}</p>
      <div class="statusact">
        <span class="sub">以上狀態摘要可直接用於主管報告，正式呈報請至：</span>
        <button class="ghost gotoReport">📧 前往週報</button>
      </div>
    </div>
    ${milestoneBlockHTML(st.milestones)}
    <div class="cards">${[
      ["", s.total, "工作項目", "total"], ["green", s.done, "已完成", "done"],
      ["red", s.overdue, "逾期", "overdue"], ["red", s.at_risk, "高風險", "at_risk"],
      ["amber", s.min_float ?? "—", "最小浮時(d)", "min_float"],
      ["red", s.docs_missing, "文件缺件", "docs_missing"],
    ].map(([c, v, l, metric]) =>
      `<div class="kpi clickable ${v ? c : ""}" data-metric="${metric}" title="點選檢視項目明細">
        <b>${esc(v)}</b><span>${l}</span></div>`).join("")}</div>`;

  const gotoBtn = box.querySelector(".gotoReport");
  if (gotoBtn) gotoBtn.onclick = () => $('#tabs button[data-tab="report"]').click();

  $$(".sp-node", box).forEach(el => el.onclick = e => {
    if (e.target.closest(".sp-name, .spdate")) return; // 點名稱/日期是要編輯，不是要選站
    stageSelection.set(pid, el.dataset.code);
    renderProjectHero(section, st, d);
  });
  $$(".sp-name", box).forEach(el => {
    el.draggable = false;
    el.onclick = e => e.stopPropagation();
    el.onmousedown = e => e.stopPropagation(); // 避免點名稱編輯時先觸發父層的 dragstart
    const orig = el.textContent;
    el.onblur = async () => {
      const v = el.textContent.trim();
      if (v === orig || !v) { if (!v) el.textContent = orig; return; }
      const stage = st.stages.find(s => s.code === el.dataset.code);
      try {
        await post(`/api/stages/${stage.id}`, { name: v });
        toast("已更新"); renderProjectPage(pid);
      } catch (e) {
        el.textContent = orig; toast(e.message || "儲存失敗");
      }
    };
    el.onkeydown = e => { if (e.key === "Enter") { e.preventDefault(); el.blur(); } };
  });
  // 日期平常是文字（節省寬度，9 站才擠得進一個畫面不用捲動），點一下才臨時展開成
  // 兩個日期輸入框；改的是「底下開始最早/結束最晚的那個任務」，永遠只精準動一個任務。
  $$(".spdate", box).forEach(span => {
    if (span.dataset.editable !== "1") return;
    span.onclick = e => {
      e.stopPropagation();
      const code = span.dataset.code;
      const stage = st.stages.find(s => s.code === code);
      const stageTasks = st.tasks.filter(t => t.stage_code === code && t.planned_start && t.planned_end);
      if (!stageTasks.length) return;
      const startTask = stageTasks.reduce((a, b) => (a.planned_start <= b.planned_start ? a : b));
      const endTask = stageTasks.reduce((a, b) => (a.planned_end >= b.planned_end ? a : b));
      span.innerHTML = `
        <input type="date" class="sp-date-inp" value="${esc(stage.planned_start)}"
               title="「${esc(startTask.name)}」之開始日">～<input type="date" class="sp-date-inp"
               value="${esc(stage.planned_end)}" title="「${esc(endTask.name)}」之結束日">`;
      const [startInp, endInp] = span.querySelectorAll("input");
      const commit = async (task, field, inp) => {
        const ok = await saveTask({ id: task.id, [field]: inp.value });
        if (ok) { toast(`已更新「${task.name}」之${field === "planned_start" ? "開始" : "結束"}日期`); renderProjectPage(pid); }
      };
      startInp.onclick = e2 => e2.stopPropagation();
      endInp.onclick = e2 => e2.stopPropagation();
      startInp.onchange = () => commit(startTask, "planned_start", startInp);
      endInp.onchange = () => commit(endTask, "planned_end", endInp);
      startInp.focus();
    };
  });
  // 精簡總覽列的節點也能直接拖拉調順序，不用跳去「文件與階段」子分頁才找得到拖拉手把——
  // 使用者反映「就是想在這兩顆圈圈底下調」，這裡跟文件與階段子分頁共用同一支
  // reorder API，行為（連帶改代號、不動日期）完全一致，只是換一個能拖的地方。
  let dragSrcNodeId = null;
  $$(".sp-node", box).forEach(el => {
    el.ondragstart = e => {
      // 這個節點沒有獨立的拖拉手把，CSS 的 pointer-events 擋不到它（點它是要看細節，
      // 不能整個擋掉），鎖定與否只能在這裡用 JS 判斷。用這個框自己的鎖，不是全站的。
      if (heroLocked) {
        e.preventDefault();
        toast("此區塊目前為鎖定狀態，請先點選右上角鎖頭圖示解鎖後再調整順序");
        return;
      }
      dragSrcNodeId = +el.dataset.id;
      el.classList.add("dragging");
      e.dataTransfer.effectAllowed = "move";
    };
    el.ondragend = () => el.classList.remove("dragging");
    el.ondragover = e => e.preventDefault();
    el.ondrop = async e => {
      e.preventDefault();
      const targetId = +el.dataset.id;
      if (!dragSrcNodeId || dragSrcNodeId === targetId) return;
      const order = st.stages.map(s => s.id);
      const si = order.indexOf(dragSrcNodeId), ti = order.indexOf(targetId);
      order.splice(ti, 0, order.splice(si, 1)[0]);
      try {
        await post(`/api/projects/${pid}/stages/reorder`, { order });
        toast("階段順序已更新");
        renderProjectPage(pid);
      } catch (err) {
        toast(err.message || "調整順序失敗");
      }
    };
  });
  const gotoDocs = box.querySelector(".gotoDocs");
  if (gotoDocs) gotoDocs.onclick = () => goToStage(pid, gotoDocs.dataset.code);
  $$(".kpi", box).forEach(el => el.onclick = () => drillKpi(section, st, el.dataset.metric));

  const heroLockBtn = box.querySelector(".heroLockBtn");
  if (heroLockBtn) heroLockBtn.onclick = () => {
    heroLocked = !heroLocked;
    localStorage.setItem("heroLocked", heroLocked ? "1" : "0");
    renderProjectHero(section, st, d);
  };

  // 日期直接在這裡點了就能改，不用跳去 WBS 表——改的是同一筆任務紀錄，WBS 表本來就會
  // 看到最新值，不是另外一套資料（使用者明講：「這裡編輯以後，WBS 也要跟著自動變更」）。
  $$(".dtdate", box).forEach(inp => inp.onchange = async () => {
    const ok = await saveTask({ id: +inp.dataset.id, [inp.dataset.k]: inp.value },
      () => { inp.value = inp.dataset.orig; });
    if (ok) { toast("已更新"); renderProjectPage(pid); }
  });
}

// KPI 數字只給結論、不給細節，容易讓人「知道有問題、但不知道是哪一項」——
// 點卡片就直接跳去 WBS 表，把符合的那幾列亮出來，不用自己在表裡一列一列找。
function tasksForMetric(tasks, metric) {
  switch (metric) {
    case "total": return tasks;
    case "done": return tasks.filter(t => t.status === "已完成");
    case "overdue": return tasks.filter(t => t.flag === "overdue");
    case "at_risk": return tasks.filter(t =>
      ["will_slip", "must_start", "critical", "cyclic"].includes(t.flag) && t.status !== "已完成");
    case "min_float": {
      const open = tasks.filter(t => t.status !== "已完成" && t.total_float != null);
      if (!open.length) return [];
      const min = Math.min(...open.map(t => t.total_float));
      return open.filter(t => t.total_float === min);
    }
    default: return [];
  }
}

function drillKpi(section, st, metric) {
  if (metric === "docs_missing") {
    const docsBtn = section.querySelector('.subtabs button[data-sub="docs"]');
    if (docsBtn) docsBtn.click();
    return;
  }
  const wbsBtn = section.querySelector('.subtabs button[data-sub="wbs"]');
  if (wbsBtn) wbsBtn.click();
  const table = section.querySelector(".wbsTable");
  if (!table) return;
  $$("tbody tr", table).forEach(tr => tr.classList.remove("flash"));
  const ids = tasksForMetric(st.tasks, metric).map(t => t.id);
  if (!ids.length) { toast("目前無符合項目"); return; }
  let first = null;
  ids.forEach(id => {
    const tr = table.querySelector(`tr[data-id="${id}"]`);
    if (tr) { tr.classList.add("flash"); if (!first) first = tr; }
  });
  if (first) first.scrollIntoView({ behavior: "smooth", block: "center" });
}

// 點階段路徑圖上的任一站（不管是從專案自己的頁面點，還是從跨專案的「總覽」點）→
// 切到那個專案分頁、切到「文件與階段」子分頁、把那一階段的卡片展開並捲到看得到。
// 如果目前還沒切到那個專案分頁，要先 await 整頁重新渲染完，才能去操作裡面的元素——
// 不然點擊當下觸發的非同步重繪跟這裡的展開/捲動動作會搶時序，抓到還沒建好的 DOM。
async function goToStage(pid, code) {
  const tabBtn = $(`#tabs button[data-pid="${pid}"]`);
  const section = $(`#tab-p${pid}`);
  if (tabBtn && !section.classList.contains("on")) {
    $$("#tabs button").forEach(x => x.classList.toggle("on", x === tabBtn));
    $$(".tab").forEach(s => s.classList.toggle("on", s === section));
    await renderProjectPage(pid);
  }
  section.querySelector('.subtabs button[data-sub="docs"]').click();
  const card = section.querySelector(`.stage[data-code="${code}"]`);
  if (!card) return;
  const body = card.querySelector(".stagebody");
  if (body) body.style.display = "block";
  card.scrollIntoView({ behavior: "smooth", block: "start" });
}

/* ---- 跨專案總覽：一次看所有案子的階段圖＋狀態句 ---- */
// 燈號只回答一個問題：「照現在的承諾日算，這個案子準時嗎？」——跟階段卡片那句
// 「原訂完工 vs 現在預測」用同一組數字，不是另外發明一套判斷邏輯。
// 還沒凍結基準線就沒有承諾日可比較，給灰燈，不能硬算成紅或綠（那是假精確）。
function healthLight(summary) {
  if (!summary.baseline_set) return { c: "grey", t: "尚未凍結基準線，無承諾完工日可供比較" };
  if (summary.finish_variance_days > 0) return { c: "red", t: `落後原訂 ${summary.finish_variance_days} 個工作日` };
  return { c: "green", t: summary.finish_variance_days < 0 ? "提前，符合原訂承諾完工日" : "準時，符合原訂承諾完工日" };
}

async function loadOverviewTab() {
  await ensureProjects();
  const states = (await Promise.all(projects.map(p => api(`/api/projects/${p.id}/state`))))
    .filter(Boolean);
  $("#overviewBody").innerHTML = states.map(st => {
    const hl = healthLight(st.summary);
    return `
    <div class="paper projoview" data-pid="${st.project.id}">
      <h2 class="ovhd">
        <span class="health-light ${hl.c}" title="${esc(hl.t)}"></span>
        <span class="proj-dot" style="background:${esc(st.project.color || "#2563eb")}"></span>
        ${esc(st.project.code)}　${esc(st.project.name)}
      </h2>
      <div class="stagehero compact">
        <div class="stagepath">${stagePathHTML(st.stages, st.current_stage)}</div>
        <p class="statusline">${esc(st.status_sentence)}</p>
      </div>
    </div>`;
  }).join("") || '<div class="empty">目前尚無任何專案。</div>';

  $$("#overviewBody .ovhd").forEach(h => h.onclick = () => {
    $(`#tabs button[data-pid="${h.closest(".projoview").dataset.pid}"]`).click();
  });
  $$("#overviewBody .sp-item").forEach(el => {
    const pid = +el.closest(".projoview").dataset.pid;
    el.onclick = () => goToStage(pid, el.dataset.code);
  });
}

/* ---------------------------------------------------------- 負荷評估 */
// 「能力負荷」（誰手上有什麼）跟「時間負荷」（哪天/哪週最壅塞）共用同一份
// /api/workload 資料，只是算法跟呈現不同——不是兩支各自打 API 的獨立功能。
let workloadPeople = [];
let workloadGran = localStorage.getItem("workloadGran") || "week";
let workloadHidePast = localStorage.getItem("workloadHidePast") === "1";
const CAP_DAY_PX = 2.4;
// 一個人身上的工作項目超過這個數字，逐項畫長條會擠成一團看不清楚，改成
// 用「階段」聚合成一條——這是既有的分組單位，不用另外發明一套。
const WORKLOAD_AGG_THRESHOLD = 8;

function workloadDateRange(people) {
  const dates = [];
  people.forEach(p => p.tasks.forEach(t => { dates.push(t.planned_start); dates.push(t.planned_end); }));
  if (!dates.length) return null;
  dates.sort();
  let start = new Date(dates[0] + "T00:00:00");
  start.setDate(1);
  const endRaw = new Date(dates[dates.length - 1] + "T00:00:00");
  let end = new Date(endRaw.getFullYear(), endRaw.getMonth() + 1, 1);
  if (workloadHidePast) {
    // 「只看近三個月」不只是把起點往後拉，終點也一起夾到「起點 + 3 個月」——
    // 可視寬度不變，天數變少，時間軸自動撐得更寬，這才是使用者要的「放大」，
    // 不是單純砍掉已過的部分而已。
    const thisMonth = new Date(); thisMonth.setDate(1); thisMonth.setHours(0, 0, 0, 0);
    if (thisMonth > start) start = thisMonth;
    const capped = new Date(start.getFullYear(), start.getMonth() + 3, 1);
    if (capped < end) end = capped;
    if (start >= end) end = new Date(start.getFullYear(), start.getMonth() + 1, 1);
  }
  return { start, end };
}

async function loadWorkloadTab() {
  const data = await api("/api/workload");
  workloadPeople = data.people;
  renderCapacityBody(workloadPeople);
  renderTimeBody(workloadPeople, workloadGran);
  $$(".granBtn").forEach(b => b.onclick = () => {
    workloadGran = b.dataset.gran;
    localStorage.setItem("workloadGran", workloadGran);
    $$(".granBtn").forEach(x => x.classList.toggle("on", x === b));
    renderTimeBody(workloadPeople, workloadGran);
  });
  const hideChk = $("#hidePastMonths");
  hideChk.checked = workloadHidePast;
  hideChk.onchange = () => {
    workloadHidePast = hideChk.checked;
    localStorage.setItem("workloadHidePast", workloadHidePast ? "1" : "0");
    renderCapacityBody(workloadPeople);
    renderTimeBody(workloadPeople, workloadGran);
  };
}

function renderCapacityBody(people) {
  const box = $("#capacityBody");
  const range = workloadDateRange(people);
  if (!range) { box.innerHTML = '<p class="sub">目前沒有已排定日期的未完成工作項目。</p>'; return; }
  const { start, end } = range;
  const totalDays = Math.round((end - start) / 86400000);
  const availW = Math.max(box.clientWidth - 320, 0);
  const dayPx = Math.max(CAP_DAY_PX, availW / Math.max(totalDays, 1));
  const totalW = totalDays * dayPx;
  const xOf = iso => Math.round((new Date(iso + "T00:00:00") - start) / 86400000) * dayPx;
  const months = [];
  let cur = new Date(start);
  while (cur < end) {
    const next = new Date(cur.getFullYear(), cur.getMonth() + 1, 1);
    months.push({ label: `${cur.getFullYear()}/${String(cur.getMonth() + 1).padStart(2, "0")}`,
      x: xOf(cur.toISOString().slice(0, 10)), w: Math.round((next - cur) / 86400000) * dayPx });
    cur = next;
  }
  const today = new Date(); today.setHours(0, 0, 0, 0);
  const todayX = (today >= start && today < end) ? xOf(today.toISOString().slice(0, 10)) : null;

  const groupTasksByKey = {};
  const rowsHTML = people.map(p => {
    const label = p.name || "（未指派）";
    const overlapCount = p.tasks.filter(t => t.overlap).length;

    // 「未指派」是一大桶互不相干的工作項目湊在一起，畫成甘特圖長條只會是一團
    // 沒有意義的色塊——這桶本來要回答的問題是「有哪些工作沒人接」，不是
    // 「誰被排爆了」，所以只顯示筆數，用點開明細表回答，不畫時間軸。
    if (p.name === "") {
      return `<div class="prow unassigned" data-owner="" style="min-height:40px">
        <div class="who"><b>${esc(label)}</b><span class="cnt">${p.tasks.length} 項工作項目，點開看清單</span></div>
        <div class="track"></div>
      </div>`;
    }

    const aggregated = p.tasks.length > WORKLOAD_AGG_THRESHOLD;
    let items; // { start, end, color, label, group }[]，group 有東西代表這是聚合出來的一條
    if (aggregated) {
      const groups = new Map();
      p.tasks.forEach(t => {
        const key = `${t.project_code}|${t.stage_code}`;
        if (!groups.has(key)) {
          groups.set(key, { start: t.planned_start, end: t.planned_end, color: t.project_color,
            label: `${t.project_code} ${t.stage_code ? t.stage_name : "未分類"}`, tasks: [] });
        }
        const g = groups.get(key);
        if (t.planned_start < g.start) g.start = t.planned_start;
        if (t.planned_end > g.end) g.end = t.planned_end;
        g.tasks.push(t);
      });
      items = [...groups.values()];
    } else {
      items = p.tasks.map(t => ({ start: t.planned_start, end: t.planned_end, color: t.project_color,
        label: `${t.project_code} ${t.name}`, overlap: t.overlap,
        title: `${t.project_code} ${t.wbs_no} ${t.name}　${t.planned_start} ~ ${t.planned_end}` }));
    }

    // 用貪婪區間排列（跟行事曆軟體同一套做法）把「真的日期重疊」的項目分到不同行，
    // 沒有重疊的項目共用同一行——不是照陣列順序硬性每 6 項換一行，那樣會把明明
    // 沒有衝突的項目也拆開、看起來很亂。
    const sorted = [...items].sort((a, b) => a.start < b.start ? -1 : 1);
    const laneEnds = [];
    const lanes = sorted.map(it => {
      let lane = laneEnds.findIndex(end => end < it.start);
      if (lane === -1) { lane = laneEnds.length; laneEnds.push(it.end); }
      else laneEnds[lane] = it.end;
      return { it, lane };
    });
    const laneCount = laneEnds.length;
    const bars = lanes.map(({ it, lane }) => {
      // 「隱藏已過月份」把軸的起點往後拉之後，開始日在起點之前的項目，長條的
      // 左邊要貼齊在最左邊（0px），不能算出負值跑到「人員」欄位那邊去；
      // 右邊同理夾到 totalW——「只看近三個月」把終點也往前拉了，超出視窗的
      // 結束日不該把長條撐到畫面外面去，貼齊在最右邊就好，標題還是看得到完整日期。
      const x = Math.max(xOf(it.start), 0);
      const w = Math.max(Math.min(xOf(it.end), totalW) - x, 6);
      if (it.tasks) {
        const groupKey = `${p.name}|${it.tasks[0].project_code}|${it.tasks[0].stage_code}`;
        groupTasksByKey[groupKey] = it.tasks;
        return `<div class="pbar agg" data-groupkey="${esc(groupKey)}" style="left:${x}px;top:${lane * 20 + 2}px;width:${w}px;background:${esc(it.color)}"
          title="點開看這 ${it.tasks.length} 項工作　${esc(it.start)} ~ ${esc(it.end)}">${esc(it.label)}<span class="aggn">${it.tasks.length}</span></div>`;
      }
      return `<div class="pbar ${it.overlap ? "overlap" : ""}" style="left:${x}px;top:${lane * 20 + 2}px;width:${w}px;background:${esc(it.color)}"
        title="${esc(it.title)}">${esc(it.label)}</div>`;
    }).join("");
    const rowH = Math.max(56, laneCount * 20 + 16);
    const cnt = aggregated ? `${p.tasks.length} 項工作，已按階段聚合成 ${items.length} 條`
      : (overlapCount ? `${overlapCount} 項工作重疊` : "目前無重疊");
    return `<div class="prow" data-owner="${esc(p.name)}" style="min-height:${rowH}px">
      <div class="who"><b>${esc(label)}</b><span class="cnt ${overlapCount ? "warn" : ""}">${esc(cnt)}</span></div>
      <div class="track" style="height:${rowH - 16}px;width:${totalW}px">${bars}${todayX !== null ? `<div class="today" style="left:${todayX}px"></div>` : ""}</div>
    </div>`;
  }).join("");

  const legendTasks = [...new Map(people.flatMap(p => p.tasks).map(t => [t.project_code, t])).values()];
  box.innerHTML = `
    <div class="ganttwrap">
      <div class="gantthd">
        <div class="gcorner">人員</div>
        <div class="gmonths" style="width:${totalW}px">${months.map(m =>
          `<div class="gmonth" style="left:${m.x}px;width:${m.w}px">${m.label}</div>`).join("")}</div>
      </div>
      <div class="legend">
        ${legendTasks.map(t => `<span><i class="sw" style="background:${esc(t.project_color)}"></i>${esc(t.project_code)} ${esc(t.project_name)}</span>`).join("")}
        <span style="margin-left:auto"><i class="sw" style="background:transparent;outline:2px solid var(--red);outline-offset:1px"></i>紅框＝日期重疊</span>
      </div>
      <div class="workbody">${rowsHTML || '<p class="sub" style="padding:14px">目前沒有未完成的工作項目。</p>'}</div>
    </div>
    <div class="detailwrap"></div>`;

  // 月份表頭在自己的橫向捲動框外面，捲動人員列表時要手動同步，跟甘特圖同一招。
  const workbody = $(".workbody", box), monthsWrap = $(".gmonths", box);
  workbody.onscroll = () => { monthsWrap.style.transform = `translateX(${-workbody.scrollLeft}px)`; };

  $$(".prow .who", box).forEach(who => who.onclick = () => {
    const owner = who.closest(".prow").dataset.owner;
    renderCapacityDetail(people.find(x => x.name === owner), `${owner || "（未指派）"} — 工作項目明細`);
  });
  $$(".pbar.agg", box).forEach(bar => bar.onclick = () => {
    const tasks = groupTasksByKey[bar.dataset.groupkey];
    if (!tasks) return;
    renderCapacityDetail({ tasks }, `${tasks[0].project_code} ${tasks[0].stage_name || "未分類"} — 這一階段的工作項目`);
  });
}

function renderCapacityDetail(p, title) {
  const box = $(".detailwrap", $("#capacityBody"));
  if (!p || !p.tasks.length) { box.innerHTML = ""; return; }
  box.innerHTML = `<div class="paper" style="margin-top:14px">
    <h2>${esc(title || "工作項目明細")}</h2>
    <table class="wltable"><tr><th></th><th>專案</th><th>工作項目</th><th>開始</th><th>結束</th></tr>
    ${p.tasks.map(t => `<tr class="${t.overlap ? "warnrow" : ""}">
      <td><span class="dot" style="background:${esc(t.project_color)}"></span></td>
      <td>${esc(t.project_code)}</td><td>${esc(t.wbs_no)} ${esc(t.name)}</td>
      <td>${esc(t.planned_start)}</td><td>${esc(t.planned_end)}</td></tr>`).join("")}
    </table></div>`;
}

function renderTimeBody(people, gran) {
  const box = $("#timeBody");
  const range = workloadDateRange(people);
  if (!range) { box.innerHTML = '<p class="sub">目前沒有已排定日期的未完成工作項目。</p>'; return; }
  const { start, end } = range;
  const buckets = [];
  if (gran === "week") {
    // 週一為週首，跟系統既有的週報／每日收尾節奏一致。
    const cur = new Date(start);
    cur.setDate(cur.getDate() - ((cur.getDay() + 6) % 7));
    while (cur < end) {
      const bEnd = new Date(cur); bEnd.setDate(bEnd.getDate() + 6);
      buckets.push({ s: new Date(cur), e: bEnd, label: `${cur.getMonth() + 1}/${cur.getDate()}` });
      cur.setDate(cur.getDate() + 7);
    }
  } else {
    const cur = new Date(start);
    while (cur < end) {
      buckets.push({ s: new Date(cur), e: new Date(cur), label: `${cur.getMonth() + 1}/${cur.getDate()}` });
      cur.setDate(cur.getDate() + 1);
    }
  }
  const isoOf = d => d.toISOString().slice(0, 10);
  const level = n => n === 0 ? "" : n === 1 ? "l1" : n === 2 ? "l2" : n === 3 ? "l3" : "l4";

  const rows = people.map(p => {
    let peak = 0;
    const cells = buckets.map(b => {
      const bs = isoOf(b.s), be = isoOf(b.e);
      const count = p.tasks.filter(t => t.planned_start <= be && bs <= t.planned_end).length;
      peak = Math.max(peak, count);
      return `<i class="${level(count)}" title="${esc(b.label)}：${count} 項"></i>`;
    }).join("");
    const label = p.name || "（未指派）";
    const unit = gran === "week" ? "週" : "日";
    const peakLabel = peak > 1 ? `尖峰${unit}：${peak} 項同時進行` : (peak === 1 ? "最多同時 1 項" : "無排程");
    return `<div class="hrow">
      <div class="who"><b>${esc(label)}</b><span class="cnt ${peak > 1 ? "warn" : ""}">${esc(peakLabel)}</span></div>
      <div class="heatwrap"><div class="heat">${cells}</div></div>
    </div>`;
  }).join("");

  box.innerHTML = `<div class="ganttwrap">${rows || '<p class="sub" style="padding:14px">目前沒有未完成的工作項目。</p>'}</div>`;
}

function milestoneBlockHTML(milestones) {
  const wrap = inner => `<div class="paper mstimeline">
    <div class="mst-hd"><h3 style="margin:0">重要里程碑</h3>
    <span class="sub">依日期排序；卡片內容依所屬階段之任務與缺件自動產生</span></div>
    ${inner}</div>`;
  if (!milestones.length) return "";
  let nowUsed = false;
  const marked = milestones.map(m => {
    let cls = "";
    if (m.done) cls = "done";
    else if (!nowUsed) { cls = "now"; nowUsed = true; }
    return { m, cls };
  });
  const track = marked.map(({ m, cls }, i) => {
    const date = (m.baseline_end || m.planned_end || "").slice(5) || "—";
    const dot = cls === "done" ? "✓" : String(i + 1);
    const line = i < marked.length - 1 ? `<div class="mst-ln ${cls === "done" ? "done" : ""}"></div>` : "";
    return `<div class="mst-pt ${cls}"><span class="mst-date">${esc(date)}</span>
      <span class="mst-dot">${dot}</span><span class="mst-lb">${esc(m.name)}</span></div>${line}`;
  }).join("");
  const cards = marked.map(({ m, cls }, i) => {
    const seg = m.segment;
    let body;
    if (!seg) {
      body = `<p class="sub">未連結任何階段（「階段」欄空白），無法自動產生內容。</p>`;
    } else {
      const parts = [];
      if (seg.done.length) parts.push(`<ul class="ul-done">${seg.done.map(n => `<li>${esc(n)}</li>`).join("")}</ul>`);
      if (seg.open.length) parts.push(`<ul class="ul-todo">${seg.open.map(n => `<li>${esc(n)}</li>`).join("")}</ul>`);
      if (seg.missing_docs.length) parts.push(`<ul class="ul-todo">${seg.missing_docs
        .map(d => `<li>缺件：${esc(d.name)}（${esc(d.doc_code)}）</li>`).join("")}</ul>`);
      body = parts.length ? parts.join("") : `<p class="sub">此階段目前無待辦事項或缺件。</p>`;
    }
    const due = m.baseline_end || m.planned_end || "—";
    // 落後時純規則沿「前置」欄位往回找根本原因組成的一句話，不是圖，方便直接
    // 貼進週報或跟主管解釋「為什麼」；沒有落後就不會有這段（後端只在落後時才生）。
    const delayHTML = m.delay_chain ? `<p class="mst-delay">⚠️ ${esc(m.delay_chain)}</p>` : "";
    return `<div class="mst-card ${cls}">
      <div class="mst-card-hd">${i + 1}. ${esc(m.name)}　<span class="sub">${esc(due)} 前</span></div>
      ${delayHTML}${body}</div>`;
  }).join("");
  return wrap(`<div class="mst-track">${track}</div><div class="mst-cards">${cards}</div>`);
}

/* ---- WBS 表 ---- */
function wbsPanelHTML() {
  return `
    <div class="toolbar">
      <div class="viewswitch">
        <button class="wbsViewBtn" data-view="table">表格</button>
        <button class="wbsViewBtn" data-view="gantt">甘特圖</button>
      </div>
      <button class="ghost addTask">＋ 新增工作項目</button>
      <select class="taskKind">
        <option value="L0">一般工作項目</option>
        <option value="M">里程碑</option>
        <option value="L1">子項目（掛在里程碑底下）</option>
      </select>
      <select class="taskParentMs" style="display:none"></select>
      <button class="ghost advToggle">進階欄位</button>
      <span class="spacer"></span>
      <a class="ghost btn" href="/api/export/csv">匯出 CSV</a>
      <a class="ghost btn" href="/api/export/ics">匯出 .ics 到日曆</a>
    </div>
    <div class="baselineBar"></div>
    <div class="bulkbar" style="display:none">
      <span class="bulkCount"></span>
      <label class="sub">設定負責人：</label>
      <select class="bulkOwner"></select>
      <button class="ghost bulkApply">套用到已選項目</button>
      <button class="ghost bulkClear">取消選取</button>
    </div>
    <div class="tablewrap"><table class="wbsTable"></table></div>
    <p class="hint wbsTableHint">點選欄位格即可編輯。<b>前置</b>欄請填寫其他項次之編號（以逗號分隔），浮時與關鍵路徑之計算皆依此欄位；未填寫則視為無前置項次。「進階欄位」預設收合，設定 WBS 結構時再行展開。</p>
    <div class="ganttlegend" style="display:none">
      <span><i class="sw" style="background:var(--grey)"></i>未開始</span>
      <span><i class="sw" style="background:var(--accent2)"></i>進行中</span>
      <span><i class="sw" style="background:var(--green)"></i>已完成</span>
      <span><i class="sw" style="background:var(--red)"></i>延遲</span>
      <span><i class="sw" style="background:var(--line)"></i>原訂基準線</span>
      <span class="hint ganttHintTxt"></span>
      <button class="ghost ganttUndoBtn" disabled>復原上一步</button>
      <button class="ganttLockBtn" title="鎖定或解鎖甘特圖拖曳編輯"></button>
    </div>
    <div class="ganttwrap" style="display:none"></div>`;
}

function fillWbsTable(table, allTasks, stages, peopleList) {
  const childCount = new Map();
  allTasks.forEach(t => { if (t.parent_wbs) childCount.set(t.parent_wbs, (childCount.get(t.parent_wbs) || 0) + 1); });
  const tasks = allTasks.filter(t => !t.parent_wbs || wbsExpanded.has(t.parent_wbs));
  const th = `<th><input type="checkbox" class="chkAll" title="全選"></th><th></th>` +
    COLS.map(([, label, , adv]) => `<th${adv ? ' class="advcol"' : ""}>${label}</th>`).join("") + "<th></th>";
  const rows = tasks.map(t => {
    const locked = !canEditTask(t.owner);
    const dis = locked ? " disabled" : "";
    const tds = COLS.map(([k, , ed, adv]) => {
      let v = t[k]; if (v === null || v === undefined) v = "";
      const advCls = adv ? " advcol" : "";
      if (k === "status") {
        return `<td class="${advCls}"><select data-k="status" data-id="${t.id}"${dis}>${STATUSES.map(x =>
          `<option ${x === t.status ? "selected" : ""}>${x}</option>`).join("")}</select></td>`;
      }
      if (k === "progress") {
        return `<td class="num${advCls}"><select data-k="progress" data-id="${t.id}"${dis}>${PROGRESS_STEPS.map(x =>
          `<option value="${x}" ${x === (t.progress || 0) ? "selected" : ""}>${x}%</option>`).join("")}</select></td>`;
      }
      if (k === "stage_code") {
        // 代號＋名稱一起顯示（例如「S01 需求規劃」），不要只印代號——
        // 光印 S01 會跟階段自己的代號長得一模一樣，使用者會誤認成同一個東西
        // （2026-08-27 使用者實測回饋：「都是用 S01，對我來講，連結就是等於相同的東西」）。
        // 改成下拉選單也一併避免打錯字打出不存在的階段代號。
        return `<td class="${advCls}"><select data-k="stage_code" data-id="${t.id}"${dis}>
          <option value="">（未設定）</option>${(stages || []).map(s =>
            `<option value="${esc(s.code)}" ${s.code === t.stage_code ? "selected" : ""}>${esc(s.code)} ${esc(s.name)}</option>`).join("")}
        </select></td>`;
      }
      if (k === "owner") {
        // 下拉選單，不是打字——負責人來源是設定頁維護的人員名單，不會每次都是同一個人，
        // 打字容易同一個人打出兩種寫法（例如「王小明」跟「王 小明」）沒辦法算同一個人。
        return `<td class="${advCls}"><select data-k="owner" data-id="${t.id}"${dis}>
          <option value="">（未指派）</option>${(peopleList || []).map(p =>
            `<option ${p.name === t.owner ? "selected" : ""}>${esc(p.name)}</option>`).join("")}
        </select></td>`;
      }
      if (k === "name") {
        const n = childCount.get(t.wbs_no) || 0;
        const expanded = wbsExpanded.has(t.wbs_no);
        const toggleHTML = n
          ? `<span class="wbsToggle" data-wbs="${esc(t.wbs_no)}">${expanded ? "▾" : "▸"}</span>`
          : "";
        const badgeHTML = n
          ? `<span class="badge ${expanded ? "" : "collapsed"}">${expanded ? `${n} 個子項目` : `${n} 個子項目已收合`}</span>`
          : "";
        const indent = t.parent_wbs ? " wbschild" : "";
        const editAttr = locked ? "" : " contenteditable";
        return `<td class="${advCls}${indent}">${toggleHTML}<span${editAttr} data-k="name" data-id="${t.id}">${t.mark} ${t.level === "M" ? "◆ " : ""}${esc(v)}</span>${badgeHTML}</td>`;
      }
      const num = ["total_float", "live_float"].includes(k) ? " num" : "";
      if (ed === 3) {
        return `<td class="${advCls}"><input type="date" value="${esc(v)}" data-k="${k}" data-id="${t.id}" data-orig="${esc(v)}"${dis}></td>`;
      }
      const editAttr = ed === 1 && !locked ? ` contenteditable data-k="${k}" data-id="${t.id}"` : "";
      return `<td class="${num}${advCls}"${editAttr}>${esc(v)}</td>`;
    }).join("");
    return `<tr class="${t.done ? "done" : ""} ${t.critical && !t.done ? "crit" : ""} ${t.level === "M" ? "milestone" : ""} ${locked ? "rowLocked" : ""}"
        data-id="${t.id}" ${locked ? `title="這不是你負責的項目，只有主管/管理者能改"` : ""}>
      <td><input type="checkbox" class="rowChk" data-id="${t.id}"${dis}></td>
      <td class="draghandle" title="拖曳調整順序，開始日期將隨之更新">⠿</td>${tds}
      <td>${locked ? "" : `<button class="ghost del" data-id="${t.id}">刪</button>`}</td></tr>`;
  }).join("");
  table.innerHTML = `<thead><tr>${th}</tr></thead><tbody>${rows}</tbody>`;
}

function renderBaselineBar(s, proj) {
  if (!s.baseline_set) {
    return `<div class="baseline warn">
      ⚠️ 尚未凍結基準線，無承諾完工日可供比較，落後判定無法正確顯示。
      請於「設定」頁填妥目標完工日後，按此凍結：
      <button class="ghost freeze" data-mode="freeze">凍結基準線</button></div>`;
  }
  const fv = s.finish_variance_days;
  const cls = fv > 0 ? "bad" : fv < 0 ? "good" : "ok";
  const txt = fv > 0 ? `落後原訂 ${fv} 個工作日` : fv < 0 ? `提前 ${Math.abs(fv)} 個工作日` : "準時";
  return `<div class="baseline ${cls}">
    原訂完工 <b>${esc(s.baseline_finish)}</b>　現在預測 <b>${esc(s.forecast_finish)}</b>
    <span class="tag">${txt}</span>
    <button class="ghost freeze" data-mode="rebaseline">重新基準化</button></div>`;
}

async function doBaseline(pid, mode) {
  try {
    if (mode === "rebaseline") {
      const reason = prompt("請輸入重新基準化之理由（將留存於紀錄中）：");
      if (reason === null) return;
      if (!reason.trim()) return toast("請填寫理由，此為重新基準化之必要紀錄，不可留白");
      await post(`/api/projects/${pid}/baseline`, { reason });
      toast("已完成重新基準化，變更紀錄已存檔");
    } else {
      const r = await post(`/api/projects/${pid}/baseline`, {});
      toast(`基準線已凍結，目標完工日：${r.baseline_end}`);
    }
  } catch (e) {
    return toast(e.message || "凍結失敗");
  }
  projects = []; // 專案清單快取含 baseline_end，凍結後要讓設定頁重新抓一次
  renderProjectPage(pid);
}

let wbsView = localStorage.getItem("wbsView") || "table";

function applyWbsView(panel) {
  const isGantt = wbsView === "gantt";
  panel.querySelector(".tablewrap").style.display = isGantt ? "none" : "";
  panel.querySelector(".wbsTableHint").style.display = isGantt ? "none" : "";
  panel.querySelector(".ganttwrap").style.display = isGantt ? "" : "none";
  panel.querySelector(".ganttlegend").style.display = isGantt ? "flex" : "none";
  if (isGantt) panel.querySelector(".bulkbar").style.display = "none";
  $$(".wbsViewBtn", panel).forEach(b => b.classList.toggle("on", b.dataset.view === wbsView));
}

function renderWbsPanel(pid, section, st) {
  const panel = section.querySelector('[data-subpanel="wbs"]');
  if (!panel.dataset.built) { panel.innerHTML = wbsPanelHTML(); panel.dataset.built = "1"; }

  const drawGantt = () => {
    renderGanttChart(pid, panel.querySelector(".ganttwrap"), st.tasks, st.stages, () => renderProjectPage(pid));
    applyGanttLockUI(panel);
    applyGanttUndoUI(panel, pid);
  };

  $$(".wbsViewBtn", panel).forEach(b => b.onclick = () => {
    wbsView = b.dataset.view;
    localStorage.setItem("wbsView", wbsView);
    applyWbsView(panel);
    if (wbsView === "gantt") drawGantt();
  });
  applyWbsView(panel);
  if (wbsView === "gantt") drawGantt();

  panel.querySelector(".ganttLockBtn").onclick = () => {
    ganttLocked = !ganttLocked;
    localStorage.setItem("ganttLocked", ganttLocked ? "1" : "0");
    applyGanttLockUI(panel);
  };
  panel.querySelector(".ganttUndoBtn").onclick = async () => {
    if (!ganttLastChange || ganttLastChange.pid !== pid) return;
    const { patch } = ganttLastChange;
    ganttLastChange = null;
    const ok = await saveTask(patch);
    if (ok) toast("已復原上一步變更");
    renderProjectPage(pid);
  };

  const kindSel = panel.querySelector(".taskKind");
  const parentSel = panel.querySelector(".taskParentMs");
  const milestones = st.tasks.filter(t => t.level === "M");
  parentSel.innerHTML = milestones.map(m => `<option value="${esc(m.wbs_no)}">${esc(m.wbs_no)} ${esc(m.name)}</option>`).join("")
    || `<option value="">（尚無里程碑，請先新增一個）</option>`;
  kindSel.onchange = () => { parentSel.style.display = kindSel.value === "L1" ? "" : "none"; };

  panel.querySelector(".addTask").onclick = async () => {
    const kind = kindSel.value;
    let wbs_no = "NEW" + (st.tasks.length + 1);
    const fields = { project_id: pid, name: "新工作項目", status: "未開始", progress: 0 };
    if (kind === "M") {
      fields.level = "M";
    } else if (kind === "L1") {
      const parentWbs = parentSel.value;
      if (!parentWbs) { toast("請先新增一個里程碑，子項目才能掛上去"); return; }
      const siblingCount = st.tasks.filter(t => t.parent_wbs === parentWbs).length;
      wbs_no = `${parentWbs}.${siblingCount + 1}`;
      fields.level = "L1";
      fields.parent_wbs = parentWbs;
      const parentTask = st.tasks.find(t => t.wbs_no === parentWbs);
      if (parentTask) fields.stage_code = parentTask.stage_code;
      wbsExpanded.add(parentWbs);
      saveWbsExpanded();
    } else {
      fields.level = "L0";
    }
    fields.wbs_no = wbs_no;
    await post("/api/tasks", fields);
    renderProjectPage(pid);
  };
  panel.querySelector(".advToggle").onclick = function () {
    panel.querySelector(".wbsTable").classList.toggle("show-adv");
    this.classList.toggle("on");
  };

  const baselineBar = panel.querySelector(".baselineBar");
  baselineBar.innerHTML = renderBaselineBar(st.summary, st.project);
  const fb = baselineBar.querySelector(".freeze");
  if (fb) fb.onclick = () => doBaseline(pid, fb.dataset.mode);

  const table = panel.querySelector(".wbsTable");
  fillWbsTable(table, st.tasks, st.stages, people);

  const rerender = () => renderProjectPage(pid);

  // 批次選取＋設定負責人——21 項工作項目一個個點下拉選單太累，勾選要改的
  // 那幾列，選一次負責人，一次套用，走的是既有 /api/tasks 批次更新，
  // 不是另開一套資料。
  const bulkbar = panel.querySelector(".bulkbar");
  const bulkOwnerSel = panel.querySelector(".bulkOwner");
  bulkOwnerSel.innerHTML = `<option value="">（未指派）</option>${people.map(p =>
    `<option>${esc(p.name)}</option>`).join("")}`;
  const selectedIds = new Set();
  const updateBulkbar = () => {
    bulkbar.style.display = selectedIds.size ? "flex" : "none";
    bulkbar.querySelector(".bulkCount").textContent = `已選 ${selectedIds.size} 項`;
  };
  $$(".rowChk", table).forEach(cb => cb.onchange = () => {
    if (cb.checked) selectedIds.add(+cb.dataset.id); else selectedIds.delete(+cb.dataset.id);
    table.querySelector(".chkAll").checked = selectedIds.size === $$(".rowChk", table).length;
    updateBulkbar();
  });
  table.querySelector(".chkAll").onchange = e => {
    $$(".rowChk", table).forEach(cb => { cb.checked = e.target.checked;
      if (e.target.checked) selectedIds.add(+cb.dataset.id); else selectedIds.delete(+cb.dataset.id); });
    updateBulkbar();
  };
  bulkbar.querySelector(".bulkClear").onclick = () => {
    selectedIds.clear();
    $$(".rowChk, .chkAll", table).forEach(cb => { cb.checked = false; });
    updateBulkbar();
  };
  bulkbar.querySelector(".bulkApply").onclick = async () => {
    if (!selectedIds.size) return;
    const owner = bulkOwnerSel.value;
    await post("/api/tasks", [...selectedIds].map(id => ({ id, owner })));
    toast(`已將 ${selectedIds.size} 項工作項目的負責人設為${owner ? `「${owner}」` : "（未指派）"}`);
    rerender();
  };
  $$(".wbsToggle", table).forEach(btn => btn.onclick = e => {
    e.stopPropagation();
    const wbs = btn.dataset.wbs;
    if (wbsExpanded.has(wbs)) wbsExpanded.delete(wbs); else wbsExpanded.add(wbs);
    saveWbsExpanded();
    rerender();
  });
  $$("[contenteditable]", table).forEach(td => {
    const orig = td.textContent;
    td.onblur = async () => {
      let v = td.textContent.trim();
      if (td.dataset.k === "name") v = v.replace(/^[🛑🔴🟡⚪✅]\s*(◆\s*)?/, "");
      const ok = await saveTask({ id: +td.dataset.id, [td.dataset.k]: v }, () => { td.textContent = orig; });
      if (ok) rerender();
    };
    td.onkeydown = e => { if (e.key === "Enter") { e.preventDefault(); td.blur(); } };
  });
  $$("input[type=date]", table).forEach(inp => {
    inp.onchange = async () => {
      const ok = await saveTask({ id: +inp.dataset.id, [inp.dataset.k]: inp.value }, () => { inp.value = inp.dataset.orig; });
      if (ok) rerender();
    };
  });
  $$("select[data-k=status]", table).forEach(sel => sel.onchange = async () => {
    const ok = await saveTask({ id: +sel.dataset.id, status: sel.value });
    if (ok) { toast(sel.value === "已完成" ? "已標記完成，實際完成日預設為今日，可於該欄位修改" : "已更新"); rerender(); }
  });
  $$("select[data-k=progress]", table).forEach(sel => sel.onchange = async () => {
    const ok = await saveTask({ id: +sel.dataset.id, progress: +sel.value });
    if (ok) rerender();
  });
  $$("select[data-k=stage_code]", table).forEach(sel => sel.onchange = async () => {
    const ok = await saveTask({ id: +sel.dataset.id, stage_code: sel.value });
    if (ok) rerender();
  });
  $$("select[data-k=owner]", table).forEach(sel => sel.onchange = async () => {
    const ok = await saveTask({ id: +sel.dataset.id, owner: sel.value });
    if (ok) rerender();
  });
  $$(".del", table).forEach(b => b.onclick = async () => {
    if (!confirm("確定刪除此列？")) return;
    await api(`/api/tasks/${b.dataset.id}`, { method: "DELETE" }); rerender();
  });

  let dragSrcId = null;
  $$("tbody tr", table).forEach(tr => {
    const handle = tr.querySelector(".draghandle");
    handle.draggable = true;
    handle.ondragstart = e => {
      dragSrcId = +tr.dataset.id;
      tr.classList.add("dragging");
      e.dataTransfer.effectAllowed = "move";
    };
    handle.ondragend = () => tr.classList.remove("dragging");
    tr.ondragover = e => e.preventDefault();
    tr.ondrop = async e => {
      e.preventDefault();
      const targetId = +tr.dataset.id;
      if (!dragSrcId || dragSrcId === targetId) return;
      await reorderTasksByDrag(pid, st.tasks, dragSrcId, targetId);
      rerender();
    };
  });
}

const addDaysISO = (iso, days) => {
  const d = new Date(iso + "T00:00:00");
  d.setDate(d.getDate() + Math.round(days));
  return d.toISOString().slice(0, 10);
};

// 拖拉調整順序＝順手改開始日期（使用者明確選的方向：畫面順序永遠跟日期一致，
// 不做一個跟日期脫鉤的獨立排序欄，避免「畫面順序」跟「時程」兩套真相互相打架）。
// 只重新分配「一批已有的開始日期」給移動後的新順序，每項任務自己原本的天數（工期）不變。
async function reorderTasksByDrag(pid, tasks, srcId, targetId) {
  const dated = tasks.filter(t => t.planned_start);
  const order = dated.map(t => t.id);
  const srcIdx = order.indexOf(srcId), tgtIdx = order.indexOf(targetId);
  if (srcIdx < 0 || tgtIdx < 0) {
    toast("兩列皆須填寫開始日期，方可使用拖曳調整順序");
    return;
  }
  const starts = dated.map(t => t.planned_start).sort();
  order.splice(tgtIdx, 0, order.splice(srcIdx, 1)[0]);
  const byId = Object.fromEntries(dated.map(t => [t.id, t]));
  const patches = [];
  order.forEach((id, i) => {
    const t = byId[id];
    const newStart = starts[i];
    if (newStart === t.planned_start) return;
    const patch = { id, planned_start: newStart };
    if (t.planned_end) {
      const durDays = (new Date(t.planned_end + "T00:00:00") - new Date(t.planned_start + "T00:00:00")) / 86400000;
      patch.planned_end = addDaysISO(newStart, durDays);
    }
    patches.push(patch);
  });
  if (!patches.length) return;
  try {
    await post("/api/tasks", patches);
    toast(`已調整 ${patches.length} 項工作之開始日期`);
  } catch (e) {
    toast(e.message || "調整順序失敗");
  }
}

/* ---- WBS 表：甘特圖檢視 ----
   跟表格檢視共用同一批任務資料與同一支 saveTask API，只是換一種呈現方式；
   拖曳長條＝改日期，走的是既有的批次 PATCH，不是另開一套儲存邏輯。 */
const GANTT_DAY_PX_MIN = 2.4;
const GANTT_ROW_H = 32;
const GANTT_GRP_H = 28;

// 拖曳容易手滑，預設鎖定，要解鎖才能拖——跟階段總覽框的鎖同一個邏輯，
// 各自獨立的 localStorage 開關，不共用一把鎖。
let ganttLocked = localStorage.getItem("ganttLocked") !== "0";
// 只記最近一次拖曳前的日期，供「復原上一步」用——單層復原，不是完整歷史，
// 夠應付「手滑拖錯」這個情境，不需要做到多層 undo stack。
let ganttLastChange = null;

function applyGanttLockUI(panel) {
  const lockBtn = panel.querySelector(".ganttLockBtn");
  const hintTxt = panel.querySelector(".ganttHintTxt");
  const wrap = panel.querySelector(".ganttwrap");
  lockBtn.textContent = ganttLocked ? "🔒" : "🔓";
  wrap.classList.toggle("locked", ganttLocked);
  hintTxt.textContent = ganttLocked ? "甘特圖目前為鎖定狀態，點右側鎖頭圖示解鎖後才能拖曳調整日期"
    : "拖曳長條可調整日期，拖曳左右邊緣可調整開始／結束日";
}

function applyGanttUndoUI(panel, pid) {
  panel.querySelector(".ganttUndoBtn").disabled = !(ganttLastChange && ganttLastChange.pid === pid);
}

// 甘特圖左欄名稱只拿掉括號附註（通常是補充說明，不是拿來辨識這一列是哪個項目），
// 完整名稱仍留在 title，滑鼠停留看得到；欄寬另外可以手動拖曳調整，兩個一起用
// 才夠應付使用者名稱本來就取得長的情況。
function ganttShortName(name) {
  return (name || "").replace(/[（(][^）)]*[）)]/g, "").replace(/\s+/g, " ").trim() || name;
}

let ganttLeftW = +localStorage.getItem("ganttLeftW") || 320;

// 里程碑底下的子項目（level=L1，透過 parent_wbs 掛上去）預設收合，WBS 表跟
// 甘特圖共用同一份展開狀態（存哪些里程碑的 wbs_no 目前是展開的），不是各自分開記。
let wbsExpanded = new Set(JSON.parse(localStorage.getItem("wbsExpanded") || "[]"));
function saveWbsExpanded() { localStorage.setItem("wbsExpanded", JSON.stringify([...wbsExpanded])); }

function ganttStatusClass(t) {
  if (t.status === "已完成") return "done";
  if (t.critical && !t.done) return "late";
  if (t.status === "進行中") return "now";
  return "pending";
}

function ganttDateRange(tasks) {
  const dates = [];
  tasks.forEach(t => {
    [t.planned_start, t.planned_end, t.baseline_start, t.baseline_end].forEach(d => {
      if (d) dates.push(d);
    });
  });
  if (!dates.length) return null;
  dates.sort();
  let start = new Date(dates[0] + "T00:00:00");
  let end = new Date(dates[dates.length - 1] + "T00:00:00");
  start.setDate(1);
  end = new Date(end.getFullYear(), end.getMonth() + 1, 1);
  return { start, end };
}

function ganttHTML(pid, tasks, stages, availW) {
  const range = ganttDateRange(tasks);
  if (!range) return `<p class="sub" style="padding:14px">尚無已排定日期的工作項目，無法繪製甘特圖。</p>`;
  const { start, end } = range;
  const totalDays = Math.round((end - start) / 86400000);
  // 時間軸範圍比可視寬度窄時，把每天的寬度撐大到剛好填滿，不要留一大片空白；
  // 範圍長到超過可視寬度時，退回最小寬度，靠水平捲動查看（不能無限壓縮到看不清楚）。
  const dayPx = Math.max(GANTT_DAY_PX_MIN, (availW || 0) / Math.max(totalDays, 1));
  const totalW = totalDays * dayPx;
  const dayOf = iso => Math.round((new Date(iso + "T00:00:00") - start) / 86400000);
  const xOf = iso => dayOf(iso) * dayPx;

  const months = [];
  let cur = new Date(start);
  while (cur < end) {
    const next = new Date(cur.getFullYear(), cur.getMonth() + 1, 1);
    months.push({ label: `${cur.getFullYear()}/${String(cur.getMonth() + 1).padStart(2, "0")}`,
      x: xOf(cur.toISOString().slice(0, 10)), w: Math.round((next - cur) / 86400000) * dayPx });
    cur = next;
  }

  // 里程碑底下收合的子項目不列進畫面（但時間軸範圍還是照全部任務算，
  // 收合不應該讓時間軸跟著縮短）。
  const childCount = new Map();
  tasks.forEach(t => { if (t.parent_wbs) childCount.set(t.parent_wbs, (childCount.get(t.parent_wbs) || 0) + 1); });
  const visTasks = tasks.filter(t => !t.parent_wbs || wbsExpanded.has(t.parent_wbs));

  // 依階段順序分組；沒有掛階段的工作項目集中放最後一組。
  const byStage = new Map();
  visTasks.forEach(t => {
    const key = t.stage_code || "";
    if (!byStage.has(key)) byStage.set(key, []);
    byStage.get(key).push(t);
  });
  const rows = [];
  stages.forEach(stg => {
    const ts = byStage.get(stg.code) || [];
    if (!ts.length) return;
    rows.push({ type: "stage", code: stg.code, name: stg.name });
    ts.forEach(t => rows.push({ type: "task", t }));
  });
  const rest = byStage.get("") || [];
  if (rest.length) {
    rows.push({ type: "stage", code: "", name: "未設定階段" });
    rest.forEach(t => rows.push({ type: "task", t }));
  }

  const today = new Date(); today.setHours(0, 0, 0, 0);
  const todayISO = today.toISOString().slice(0, 10);
  const todayX = (today >= start && today < end) ? xOf(todayISO) : null;
  const bodyH = rows.reduce((h, r) => h + (r.type === "stage" ? GANTT_GRP_H : GANTT_ROW_H), 0);

  const leftRows = rows.map(r => {
    if (r.type === "stage") return `<div class="grow" style="height:${GANTT_GRP_H}px"><span class="rowtext">${esc(r.code)} ${esc(r.name)}</span></div>`;
    const n = childCount.get(r.t.wbs_no) || 0;
    const expanded = wbsExpanded.has(r.t.wbs_no);
    const toggleHTML = n ? `<span class="gtoggle" data-wbs="${esc(r.t.wbs_no)}">${expanded ? "▾" : "▸"}</span>` : "";
    const badgeHTML = n ? `<span class="badge ${expanded ? "" : "collapsed"}">${n}</span>` : "";
    const childCls = r.t.parent_wbs ? " gchild" : "";
    return `<div class="trow${childCls}" style="height:${GANTT_ROW_H}px" data-id="${r.t.id}" title="${esc(r.t.name)}">
        ${toggleHTML}<span class="wbs">${esc(r.t.mark || "")}${r.t.level === "M" ? " ◆" : ""}</span><span class="rowtext">${esc(ganttShortName(r.t.name))}</span>${badgeHTML}
      </div>`;
  }).join("");

  // 月份格線改用單一 CSS 漸層畫在 .gright 背景上，不要每一列各自產生一批
  // absolute-positioned 的 .gcell（9 個月 × 30 幾列 = 兩三百個疊層元素）——
  // 這麼多疊層在使用者實機 Chrome 上會讓瀏覽器的繪圖層搞混，導致捲動後內容
  // 沒有正確重繪（文字消失但 DOM/樣式都查得到，是繪圖層級的問題，不是資料問題）。
  const gridStops = ["transparent 0px"];
  months.forEach(m => {
    gridStops.push(`transparent ${m.x}px`, `var(--line) ${m.x}px`, `var(--line) ${m.x + 1}px`, `transparent ${m.x + 1}px`);
  });
  gridStops.push(`transparent ${totalW}px`);
  const gridBg = `linear-gradient(to right, ${gridStops.join(",")})`;

  const rightRows = rows.map(r => {
    if (r.type === "stage") return `<div class="grow" style="height:${GANTT_GRP_H}px;position:relative"></div>`;
    const t = r.t;
    let bar = "";
    if (t.level === "M" && (t.planned_end || t.planned_start)) {
      const d = t.planned_end || t.planned_start;
      bar = `<div class="gms" data-id="${t.id}" data-s="${esc(d)}" style="left:${xOf(d) - 7}px" title="${esc(t.name)}　${esc(d)}"></div>`;
    } else if (t.planned_start && t.planned_end) {
      const x = xOf(t.planned_start), w = Math.max(xOf(t.planned_end) - x, 6);
      let baseHTML = "";
      if (t.baseline_start && t.baseline_end) {
        const bx = xOf(t.baseline_start), bw = Math.max(xOf(t.baseline_end) - bx, 6);
        baseHTML = `<div class="gbase" style="left:${bx}px;width:${bw}px"></div>`;
      }
      const cls = ganttStatusClass(t);
      bar = `${baseHTML}<div class="gbar ${cls}" data-id="${t.id}" data-s="${esc(t.planned_start)}" data-e="${esc(t.planned_end)}"
        style="left:${x}px;width:${w}px" title="${esc(t.name)}　${esc(t.planned_start)} ~ ${esc(t.planned_end)}　${t.progress || 0}%">
        <span class="gedge l" data-edge="s"></span><span class="glabel">${t.progress ? t.progress + "%" : ""}</span><span class="gedge r" data-edge="e"></span>
      </div>`;
    } else {
      bar = `<span class="gnodate">未排定日期</span>`;
    }
    return `<div class="trow" style="height:${GANTT_ROW_H}px;position:relative">${bar}</div>`;
  }).join("");

  const todayLineHTML = todayX === null ? "" :
    `<div class="gtoday" style="left:${todayX}px;height:${bodyH}px"><span>今天</span></div>`;

  return `
    <div class="gantthd">
      <div class="gcorner" style="flex-basis:${ganttLeftW}px;width:${ganttLeftW}px">工作項目</div>
      <div class="gmonths" style="width:${totalW}px">${months.map(m =>
        `<div class="gmonth" style="left:${m.x}px;width:${m.w}px">${m.label}</div>`).join("")}</div>
    </div>
    <div class="gantbody">
      <div class="gleft" style="flex-basis:${ganttLeftW}px;width:${ganttLeftW}px;max-width:${ganttLeftW}px">${leftRows}</div>
      <div class="gresize" title="拖曳調整欄寬"></div>
      <div class="gright" style="width:${totalW}px;background-image:${gridBg};background-repeat:no-repeat">${rightRows}${todayLineHTML}</div>
    </div>`;
}

function renderGanttChart(pid, container, tasks, stages, onChanged) {
  // 時間軸範圍比容器窄時要撐滿寬度，所以要先量出扣掉左側工作項目欄
  // （欄寬使用者可拖曳調整，存在 ganttLeftW）後剩下的可視寬度，
  // 再算每天要畫多寬——兩邊算出來的 dayPx 才會一致（拖曳日期換算才不會
  // 跟畫面對不起來）。
  const availW = Math.max(container.clientWidth - ganttLeftW - 6, 0);
  container.innerHTML = ganttHTML(pid, tasks, stages, availW);
  const range = ganttDateRange(tasks);
  if (!range) return;
  const totalDays = Math.round((range.end - range.start) / 86400000);
  const dayPx = Math.max(GANTT_DAY_PX_MIN, availW / Math.max(totalDays, 1));
  const dayOf = iso => Math.round((new Date(iso + "T00:00:00") - range.start) / 86400000);
  const isoOf = days => addDaysISO(range.start.toISOString().slice(0, 10), days);

  $$(".gtoggle", container).forEach(btn => btn.onclick = e => {
    e.stopPropagation();
    const wbs = btn.dataset.wbs;
    if (wbsExpanded.has(wbs)) wbsExpanded.delete(wbs); else wbsExpanded.add(wbs);
    saveWbsExpanded();
    renderGanttChart(pid, container, tasks, stages, onChanged);
  });

  // 拖曳中間那條分隔線調整左欄寬度——存進 localStorage，換頁/重整後還記得。
  const resizer = container.querySelector(".gresize");
  resizer.onmousedown = e => {
    e.preventDefault();
    const startX = e.clientX, startW = ganttLeftW;
    const onMove = e2 => {
      ganttLeftW = Math.max(140, Math.min(600, startW + (e2.clientX - startX)));
      container.querySelector(".gleft").style.flexBasis = ganttLeftW + "px";
      container.querySelector(".gleft").style.width = ganttLeftW + "px";
      container.querySelector(".gleft").style.maxWidth = ganttLeftW + "px";
      container.querySelector(".gcorner").style.flexBasis = ganttLeftW + "px";
      container.querySelector(".gcorner").style.width = ganttLeftW + "px";
    };
    const onUp = () => {
      document.removeEventListener("mousemove", onMove);
      document.removeEventListener("mouseup", onUp);
      localStorage.setItem("ganttLeftW", ganttLeftW);
      renderGanttChart(pid, container, tasks, stages, onChanged);
    };
    document.addEventListener("mousemove", onMove);
    document.addEventListener("mouseup", onUp);
  };

  // 月份表頭在捲動容器外面（不再跟著內部捲動框走，因為那個捲動框已經拿掉），
  // 得手動同步水平位移才會跟著時間軸一起橫向捲動。
  const rightWrap = container.querySelector(".gantbody");
  const monthsWrap = container.querySelector(".gmonths");
  rightWrap.onscroll = () => { monthsWrap.style.transform = `translateX(${-rightWrap.scrollLeft}px)`; };

  const startDrag = (el, e, mode) => {
    if (ganttLocked) { toast("甘特圖目前為鎖定狀態，請先解鎖才能拖曳調整日期"); return; }
    e.preventDefault(); e.stopPropagation();
    const id = +el.dataset.id;
    const task = tasks.find(t => t.id === id);
    if (!task) return;
    const startX = e.clientX;
    const origS = el.dataset.s, origE = el.dataset.e || el.dataset.s;
    const origLeft = parseFloat(el.style.left);
    const origW = el.classList.contains("gbar") ? parseFloat(el.style.width) : 0;
    const onMove = e2 => {
      const dx = e2.clientX - startX;
      const dDays = Math.round(dx / dayPx);
      if (mode === "move") {
        el.style.left = (origLeft + dDays * dayPx) + "px";
      } else if (mode === "start") {
        const w = Math.max(origW - dDays * dayPx, dayPx);
        el.style.left = (origLeft + (origW - w)) + "px";
        el.style.width = w + "px";
      } else if (mode === "end") {
        el.style.width = Math.max(origW + dDays * dayPx, dayPx) + "px";
      }
    };
    const onUp = async e2 => {
      document.removeEventListener("mousemove", onMove);
      document.removeEventListener("mouseup", onUp);
      const dDays = Math.round((e2.clientX - startX) / dayPx);
      if (!dDays) return;
      const patch = { id };
      if (mode === "move") {
        patch.planned_start = isoOf(dayOf(origS) + dDays);
        patch.planned_end = isoOf(dayOf(origE) + dDays);
      } else if (mode === "start") {
        patch.planned_start = isoOf(Math.min(dayOf(origS) + dDays, dayOf(origE)));
      } else if (mode === "end") {
        patch.planned_end = isoOf(Math.max(dayOf(origE) + dDays, dayOf(origS)));
      }
      const undoPatch = { id, planned_start: task.planned_start, planned_end: task.planned_end };
      const ok = await saveTask(patch);
      if (ok) { ganttLastChange = { pid, patch: undoPatch }; toast("已更新日期"); onChanged(); }
      else onChanged();
    };
    document.addEventListener("mousemove", onMove);
    document.addEventListener("mouseup", onUp);
  };

  $$(".gbar", container).forEach(bar => {
    bar.onmousedown = e => {
      const edge = e.target.dataset.edge;
      startDrag(bar, e, edge === "s" ? "start" : edge === "e" ? "end" : "move");
    };
  });
  $$(".gms", container).forEach(ms => {
    ms.onmousedown = e => startDrag(ms, e, "move");
  });
}

/* ---- 文件與階段 ---- */
function docsPanelHTML() {
  // 圖示說明常駐顯示，不是要點開才看得到——第一次用、或不熟系統的人光看圖示
  // 猜不出意思，尤其星星跟燈號長什麼樣、代表什麼，得先講清楚才不用每次都來問。
  return `
    <div class="iconLegend">
      <span><b>⭐</b> 必繳文件，尚未繳交</span>
      <span><b>⚪</b> 選繳文件，尚未繳交</span>
      <span><b>✅</b> 已繳交</span>
      <span><b>🔴🟡</b> 進度／時程落後（僅顯示於今日待辦與總覽燈號，與文件繳交狀態為獨立指標）</span>
    </div>
    <div class="docsBody"></div>`;
}

function renderDocsPanel(pid, section, d) {
  const panel = section.querySelector('[data-subpanel="docs"]');
  if (!panel.dataset.built) { panel.innerHTML = docsPanelHTML(); panel.dataset.built = "1"; }
  const rerenderAll = () => renderProjectPage(pid);

  // 改一個小欄位（選負責人、調進度、改備註…）不該讓整塊畫面「跳掉」收合回去——
  // 使用者選完負責人還要重新點開同一個階段，才看得到剛剛選了什麼，很擾民
  // （2026-08-28 使用者實測抓到）。重繪前先記住哪些階段是展開的，重繪完照樣展開回去。
  const prevExpanded = new Set(
    $$(".stage", panel).filter(el => el.querySelector(".stagebody")?.style.display === "block")
      .map(el => el.dataset.code));

  section.querySelector(".projtools .scanInfo").textContent =
    "文件根目錄：" + (d.docs_root || "（未設定，請至設定頁填寫）");
  const body = panel.querySelector(".docsBody");
  body.innerHTML = d.stages.map(st => {
    const g = st.gate;
    // 大百分比用平均進度（avg_progress），不是「幾項完全交了」的二元計數——
    // 兩項標 50%、一項 0% 時要顯示 33%，不能卡在 0%（使用者實測抓到的落差：
    // 二元計數看不出「大家都做了一半」跟「完全沒人動」的差別）。
    // 「共 X/Y 項」保留二元計數，回答的是另一個問題：「真的交了幾項」。
    const pct = g.avg_progress ?? 0;
    // 紅/橘/黃/綠這組顏色刻意保留給「進度／時程」專用（總覽的燈號、今日待辦的「不能拖」），
    // 這裡講的是「必繳文件還沒交齊」，是分類不是進度，用藍色星星標示「必繳」這件事，
    // 不會被誤讀成任何一種進度顏色（2026-08-28 使用者明講：進度色系都不要用在這裡）。
    const light = g.missing.length ? (st.status === "未開始" ? "⚪" : "⭐") : "✅";
    return `<div class="stage" data-code="${esc(st.code)}" data-id="${st.id}">
      <div class="stagehd">
        <span class="draghandle" title="拖曳調整階段順序">⠿</span>
        <span>${light}</span>
        <span class="nm">${esc(st.code)}　${esc(st.name)}</span>
        <select class="stst" data-id="${st.id}">${["未開始", "進行中", "已完成"].map(x =>
      `<option ${x === st.status ? "selected" : ""}>${x}</option>`).join("")}</select>
        <span class="sub">${esc(st.planned_start || "")} ~ ${esc(st.planned_end || "")}</span>
        <span class="stagepct ${pct === 100 ? "full" : pct === 0 ? "" : "partial"}">${pct}%</span>
        <div class="bar"><i style="width:${pct}%"></i></div>
        <span class="sub">共 ${g.all_ready}/${g.all_total} 項已交　·　必繳 ${g.required_ready}/${g.required_total}</span>
      </div>
      <div class="stagebody" style="display:none">
        <p class="sub" style="margin:10px 0">目的：${esc(st.purpose || "—")}<br>
           出場條件：${esc(st.exit_gate || "—")}</p>
        ${st.docs.map(x => `
          <div class="doc">
            <span>${x.ready ? "✅" : (x.required ? "⭐" : "⚪")}</span>
            <span class="dcode">${esc(x.doc_code)}</span>
            <div class="dn">
              <b contenteditable data-k="name" data-id="${x.id}">${esc(x.name)}</b>
              ${x.required ? "" : '<span class="tag">選繳</span>'}
              <div class="sub" contenteditable data-k="note" data-id="${x.id}">${esc(x.note || "")}</div>
              ${x.files.length ? `<div class="files">${x.files.map(f =>
                `<span class="fileRow"><a href="/api/docs/file/${f.id}" target="_blank" rel="noopener">📎 ${esc(f.filename)}</a>`
                + (f.version ? " (v" + esc(f.version) + ")" : "")
                + ` <button class="ghost delFile" data-id="${f.id}" data-name="${esc(f.filename)}">刪</button></span>`
              ).join("<br>")}</div>`
        : '<div class="files">尚未在目錄中找到對應檔案</div>'}
            </div>
            <div class="docact">
              <button class="ghost upload" data-id="${x.id}">📤 上傳</button>
              <input type="file" class="uploadInput" data-id="${x.id}" style="display:none">
              <label class="sub docprog" style="white-space:nowrap">承辦
                <select class="docowner" data-id="${x.id}">
                  <option value="">（未指派）</option>${people.map(p =>
                    `<option ${p.name === x.owner ? "selected" : ""}>${esc(p.name)}</option>`).join("")}
                </select>
              </label>
              <label class="sub docprog" style="white-space:nowrap">進度
                <select class="manual" data-id="${x.id}">${[0, 50, 100].map(v =>
                  `<option value="${v}" ${v === (x.progress || 0) ? "selected" : ""}>${v}%</option>`).join("")}</select>
                ${x.has_file ? '<span class="sub">（已掃描到對應檔案，視為已交）</span>' : ""}
              </label>
              <button class="ghost delDoc" data-id="${x.id}" data-name="${esc(x.name)}">刪</button>
            </div>
          </div>`).join("")}
        ${st.orphans.length ? `<p class="sub" style="margin-top:10px">
          此階段目錄下另有 ${st.orphans.length} 個檔案未能對應到應繳項目（檔名缺少文件代碼）：<br>
          ${st.orphans.slice(0, 8).map(f => esc(f.filename)).join("、")}</p>` : ""}
        <button class="ghost addDoc" data-stage="${esc(st.code)}" style="margin-top:8px">＋ 新增文件項目</button>
      </div></div>`;
  }).join("");

  $$(".stage", body).forEach(el => {
    if (prevExpanded.has(el.dataset.code)) {
      const b = el.querySelector(".stagebody");
      if (b) b.style.display = "block";
    }
  });

  $$(".stagehd", body).forEach(h => h.onclick = () => {
    const b = h.nextElementSibling;
    b.style.display = b.style.display === "none" ? "block" : "none";
  });
  let dragSrcStageId = null;
  $$(".stage", body).forEach(card => {
    const handle = card.querySelector(".draghandle");
    handle.draggable = true;
    handle.onclick = e => e.stopPropagation(); // 只是點一下手把不該觸發展開/收合
    handle.ondragstart = e => {
      dragSrcStageId = +card.dataset.id;
      card.classList.add("dragging");
      e.dataTransfer.effectAllowed = "move";
    };
    handle.ondragend = () => card.classList.remove("dragging");
    card.ondragover = e => e.preventDefault();
    card.ondrop = async e => {
      e.preventDefault();
      const targetId = +card.dataset.id;
      if (!dragSrcStageId || dragSrcStageId === targetId) return;
      const order = $$(".stage", body).map(c => +c.dataset.id);
      const srcIdx = order.indexOf(dragSrcStageId), tgtIdx = order.indexOf(targetId);
      order.splice(tgtIdx, 0, order.splice(srcIdx, 1)[0]);
      try {
        await post(`/api/projects/${pid}/stages/reorder`, { order });
        toast("階段順序已更新");
        rerenderAll();
      } catch (err) {
        toast(err.message || "調整順序失敗");
      }
    };
  });
  $$(".stst", body).forEach(sel => {
    sel.onclick = e => e.stopPropagation();
    sel.onchange = async e => {
      e.stopPropagation();
      const r = await post(`/api/stages/${sel.dataset.id}`, { status: sel.value });
      if (r.blocked) {
        const miss = r.gate.missing.map(m => `${m.name}（${m.doc_code}）`).join("、");
        if (confirm(`此階段之必繳文件尚未齊備：\n\n${miss}\n\n` +
          `出場關卡尚未通過，仍要標記為已完成？\n（強制通過將列入週報缺件清單）`)) {
          await post(`/api/stages/${sel.dataset.id}`, { status: sel.value, force: true });
        } else { rerenderAll(); return; }
      }
      toast("階段狀態已更新"); rerenderAll();
    };
  });
  $$(".upload", body).forEach(btn => btn.onclick = () => {
    body.querySelector(`.uploadInput[data-id="${btn.dataset.id}"]`).click();
  });
  $$(".uploadInput", body).forEach(inp => inp.onchange = async () => {
    const file = inp.files[0];
    if (!file) return;
    toast("上傳中…");
    const fd = new FormData();
    fd.append("file", file);
    try {
      const r = await fetch(`/api/docreq/${inp.dataset.id}/upload`, { method: "POST", body: fd });
      const j = await r.json();
      if (!r.ok || j.error) { toast(j.error || "上傳失敗"); return; }
      toast(`已上傳「${j.filename}」，比對成功`);
      rerenderAll();
    } catch (e) {
      toast("上傳失敗：" + (e.message || e));
    } finally {
      inp.value = "";
    }
  });
  $$(".manual", body).forEach(sel => {
    sel.onclick = e => e.stopPropagation();
    sel.onchange = async () => {
      try {
        await post(`/api/docreq/${sel.dataset.id}`, { progress: +sel.value });
        rerenderAll();
      } catch (e) {
        toast(e.message || "儲存失敗");
      }
    };
  });
  $$(".docowner", body).forEach(sel => {
    sel.onclick = e => e.stopPropagation();
    sel.onchange = async () => {
      try {
        await post(`/api/docreq/${sel.dataset.id}`, { owner: sel.value });
        rerenderAll();
      } catch (e) {
        toast(e.message || "儲存失敗");
      }
    };
  });
  $$(".dn [contenteditable]", body).forEach(el => {
    el.onclick = e => e.stopPropagation();
    const orig = el.textContent;
    el.onblur = async () => {
      const v = el.textContent.trim();
      if (v === orig) return;
      if (el.dataset.k === "name" && !v) { el.textContent = orig; toast("名稱為必填欄位"); return; }
      try {
        await post(`/api/docreq/${el.dataset.id}`, { [el.dataset.k]: v });
        toast("已儲存");
      } catch (e) {
        el.textContent = orig;
        toast(e.message || "儲存失敗");
      }
    };
  });
  $$(".delDoc", body).forEach(btn => btn.onclick = async e => {
    e.stopPropagation();
    if (!confirm(`確定刪除文件項目「${btn.dataset.name}」？\n（僅刪除此筆應繳文件要求，已上傳之檔案不受影響）`)) return;
    try {
      await api(`/api/docreq/${btn.dataset.id}`, { method: "DELETE" });
      toast("已刪除"); rerenderAll();
    } catch (e) {
      toast(e.message || "刪除失敗");
    }
  });
  $$(".delFile", body).forEach(btn => btn.onclick = async e => {
    e.stopPropagation();
    if (!confirm(`確定刪除檔案「${btn.dataset.name}」？\n（僅刪除此版次，如需重新上傳請於刪除後執行）`)) return;
    try {
      await api(`/api/docs/file/${btn.dataset.id}`, { method: "DELETE" });
      toast("已刪除，可重新上傳"); rerenderAll();
    } catch (e) {
      toast(e.message || "刪除失敗");
    }
  });
  $$(".addDoc", body).forEach(btn => btn.onclick = async e => {
    e.stopPropagation();
    const name = prompt("新文件項目名稱：");
    if (!name || !name.trim()) return;
    const doc_code = prompt("請輸入文件代碼（英數字，同一階段內須唯一，例如 XXX-YYY）：");
    if (!doc_code || !doc_code.trim()) return;
    try {
      await post("/api/docreq", {
        project_id: pid, stage_code: btn.dataset.stage,
        doc_code: doc_code.trim(), name: name.trim(), required: true,
      });
      toast("已新增"); rerenderAll();
    } catch (e) {
      toast(e.message || "新增失敗");
    }
  });
}

/* ---------------------------------------------------------- 週報 */
function mdToHtml(md) {
  const lines = md.split("\n"); const out = []; let inTable = false, inList = false;
  const inline = s => esc(s)
    .replace(/\*\*(.+?)\*\*/g, "<b>$1</b>")
    .replace(/`(.+?)`/g, "<code>$1</code>");
  const closeList = () => { if (inList) { out.push("</ul>"); inList = false; } };
  const closeTable = () => { if (inTable) { out.push("</table>"); inTable = false; } };
  for (let i = 0; i < lines.length; i++) {
    const L = lines[i];
    if (/^\s*$/.test(L)) { closeList(); closeTable(); continue; }
    if (/^<\/?details|^<\/?summary/.test(L)) { closeList(); closeTable(); out.push(L); continue; }
    if (/^\|/.test(L)) {
      if (/^\|[\s\-:|]+\|$/.test(L)) continue;
      const cells = L.split("|").slice(1, -1);
      if (!inTable) {
        out.push("<table><tr>" + cells.map(c => `<th>${inline(c.trim())}</th>`).join("") + "</tr>");
        inTable = true;
      } else out.push("<tr>" + cells.map(c => `<td>${inline(c.trim())}</td>`).join("") + "</tr>");
      continue;
    }
    closeTable();
    let m;
    if ((m = L.match(/^(#{1,4})\s+(.*)$/))) { closeList(); out.push(`<h${m[1].length}>${inline(m[2])}</h${m[1].length}>`); }
    else if (/^>\s?/.test(L)) { closeList(); out.push(`<blockquote>${inline(L.replace(/^>\s?/, ""))}</blockquote>`); }
    else if (/^---+$/.test(L)) { closeList(); out.push("<hr>"); }
    else if ((m = L.match(/^[-*]\s+(.*)$/))) {
      if (!inList) { out.push("<ul>"); inList = true; }
      out.push(`<li>${inline(m[1])}</li>`);
    } else { closeList(); out.push(`<p>${inline(L)}</p>`); }
  }
  closeList(); closeTable();
  return out.join("\n");
}

let reportMd = "";
async function loadReportTab() {
  if (!$("#weekPick").value) $("#weekPick").value = new Date().toISOString().slice(0, 10);
  const list = await api("/api/reports");
  $("#pastReports").innerHTML = '<option value="">— 歷史週報 —</option>' +
    list.map(r => `<option value="${r.week_end}">${r.week_end}</option>`).join("");
}
$("#genReport").onclick = async () => {
  const r = await post("/api/report", { week_end: $("#weekPick").value });
  reportMd = r.content_md;
  $("#reportBody").innerHTML = `<div class="md">${mdToHtml(reportMd)}</div>`;
  $("#dlReport").href = `/api/report/${r.week_end}/md`;
  $("#dlReportHtml").href = `/api/report/${r.week_end}/html`;
  toast("週報已產生並存檔"); loadReportTab();
};
$("#copyReport").onclick = async () => {
  if (!reportMd) return toast("請先產生週報");
  try { await navigator.clipboard.writeText(reportMd); toast("已複製 Markdown 全文"); }
  catch { toast("複製失敗，請用下載 .md"); }
};
$("#pastReports").onchange = async e => {
  if (!e.target.value) return;
  const r = await api(`/api/report/${e.target.value}`);
  reportMd = r.content_md || "";
  $("#reportBody").innerHTML = `<div class="md">${mdToHtml(reportMd)}</div>`;
  $("#dlReport").href = `/api/report/${e.target.value}/md`;
  $("#dlReportHtml").href = `/api/report/${e.target.value}/html`;
};

/* ---------------------------------------------------------- 設定 */
const CFG_FIELDS = [
  ["docs_root", "文件根目錄", "所有專案文件的最上層資料夾，例如 D:/專案文件"],
  ["port", "服務埠號", "預設 8765"],
  ["report_time", "週報時段", "HH:MM"],
  ["daily_checkin_time", "每日收尾時間", "HH:MM"],
  ["amber_float_days", "黃燈浮時門檻", "浮時小於等於幾天算吃緊"],
  ["notify_time", "桌面提醒時間", "HH:MM，程式開著且過了這個時間點才會檢查；下方要先勾選開啟"],
];
const ROLE_LABEL = { user: "一般使用者", manager: "主管", admin: "管理者" };
function renderPeopleBody() {
  const isAdmin = currentPerson && currentPerson.role === "admin";
  $("#peopleBody").innerHTML = people.length
    ? people.map(p => `<span class="personTag">${esc(p.name)}
        ${currentPerson && isAdmin
            ? `<select class="roleSel" data-id="${p.id}" data-name="${esc(p.name)}">
                ${["user", "manager", "admin"].map(r =>
                  `<option value="${r}" ${p.role === r ? "selected" : ""}>${ROLE_LABEL[r]}</option>`).join("")}
              </select>`
            : (p.role !== "user" ? `<i class="sub">（${ROLE_LABEL[p.role]}）</i>` : "")}
        ${currentPerson ? (p.has_password
            ? (isAdmin ? `<button class="resetPw" data-id="${p.id}" data-name="${esc(p.name)}" title="重設密碼，本人下次登入輸入的密碼就是新密碼">重設密碼</button>` : "")
            : '<i class="sub">尚未設密碼</i>') : ""}
        <button class="delPerson" data-id="${p.id}" data-name="${esc(p.name)}" title="移除">×</button></span>`).join("")
    : '<p class="sub">尚無人員資料，請於下方新增。</p>';
  $$(".delPerson", $("#peopleBody")).forEach(btn => btn.onclick = async () => {
    if (!confirm(`確定將「${btn.dataset.name}」自人員名單移除？\n（已填入任務或文件之負責人資料將維持不變，僅自下拉選單移除）`)) return;
    try {
      await api(`/api/people/${btn.dataset.id}`, { method: "DELETE" });
      people = []; await ensurePeople(); renderPeopleBody();
    } catch (e) {
      toast(e.message || "刪除失敗");
    }
  });
  $$(".resetPw", $("#peopleBody")).forEach(btn => btn.onclick = async () => {
    if (!confirm(`確定重設「${btn.dataset.name}」的密碼？\n（重設後他下次登入輸入的密碼就會變成新密碼，你不用也不會知道新密碼是什麼）`)) return;
    try {
      await post("/api/people/reset-password", { id: +btn.dataset.id });
      people = []; await ensurePeople(); renderPeopleBody();
      toast(`已重設「${btn.dataset.name}」的密碼`);
    } catch (e) {
      toast(e.message || "重設失敗");
    }
  });
  $$(".roleSel", $("#peopleBody")).forEach(sel => sel.onchange = async () => {
    try {
      await post("/api/people/set-role", { id: +sel.dataset.id, role: sel.value });
      people = []; await ensurePeople(); renderPeopleBody();
      toast(`已把「${sel.dataset.name}」設為${ROLE_LABEL[sel.value]}`);
    } catch (e) {
      toast(e.message || "設定失敗");
      people = []; await ensurePeople(); renderPeopleBody();
    }
  });
}
$("#addPersonBtn").onclick = async () => {
  const inp = $("#newPersonName");
  const name = inp.value.trim();
  if (!name) return;
  try {
    await post("/api/people", { name });
    inp.value = "";
    people = []; await ensurePeople(); renderPeopleBody();
    toast(`已新增「${name}」`);
  } catch (e) {
    toast(e.message || "新增失敗");
  }
};

// 這兩個按鈕危險，鎖定模式下本來就會被擋（跟其他編輯動作同一套 CSS），
// 這裡另外再加一次文字確認，因為「清空」就算有備份，執行當下畫面還是會整個換掉，
// 不是單純手滑就能無感復原的等級，值得多一步確認。
$("#clearDataBtn").onclick = async () => {
  // 這個動作會把畫面上的真實資料整個換掉（雖然有自動備份），普通的「確定/取消」
  // 太容易手滑點過去——改成要求手動打一段確認字，逼自己真的看清楚要做什麼。
  const typed = prompt('此操作將以示範資料取代目前所有真實資料（已自動備份，可還原）。\n'
    + '請輸入「ClearALL」以確認執行：');
  if (typed !== "ClearALL") {
    if (typed !== null) toast("未輸入「ClearALL」，操作已取消");
    return;
  }
  try {
    const also_text = $("#alsoTextBackup").checked;
    const r = await post("/api/admin/clear-data", { also_text });
    $("#dataOpsMsg").textContent = r.backup_text
      ? `已備份至 data/backups/${r.backup}（供還原）、${r.backup_text}（文字版，供核對）。畫面已切換為示範資料。`
      : `已備份至 data/backups/${r.backup}。畫面已切換為示範資料。`;
    toast("已完成清空並備份，畫面即將重新整理");
    setTimeout(() => location.reload(), 1200);
  } catch (e) {
    toast(e.message || "清空失敗");
  }
};
$("#restoreDataBtn").onclick = async () => {
  if (!confirm("確定還原最近一次備份？\n目前資料將被備份檔覆蓋。")) return;
  try {
    const r = await post("/api/admin/restore-backup", {});
    $("#dataOpsMsg").textContent = `已還原：${r.restored}`;
    toast("已還原，畫面即將重新整理");
    setTimeout(() => location.reload(), 1200);
  } catch (e) {
    toast(e.message || "還原失敗");
  }
};

// 單純存一份備份，不清空、不重整畫面——隨時想存就存，跟「清空」那個高風險動作分開，
// 不用打確認字。
$("#saveBackupBtn").onclick = async () => {
  const also_text = $("#saveText").checked;
  const also_encrypted = $("#saveEncrypted").checked;
  let password = "";
  if (also_encrypted) {
    password = prompt("請設定密檔備份密碼（還原時須使用相同密碼）：") || "";
    if (!password) { toast("未設定密碼，密檔備份已取消"); return; }
  }
  try {
    const r = await post("/api/admin/backup", { also_text, also_encrypted, password });
    const parts = [`data/backups/${r.backup}`];
    if (r.backup_text) parts.push(r.backup_text);
    if (r.backup_enc) parts.push(r.backup_enc + "（密檔，密碼遺失將無法還原，請妥善保存）");
    $("#saveBackupMsg").textContent = "已儲存：" + parts.join("、");
    toast("已備份");
  } catch (e) {
    toast(e.message || "備份失敗");
  }
};

async function loadSettings() {
  await ensureProjects();
  await ensurePeople();
  renderPeopleBody();
  $("#projForm").innerHTML = projects.map(p => `
    <div class="projcard">
      <div class="projcard-hd">${esc(p.code)}
        ${p.baseline_end ? `<span class="sub">已凍結基準線：${esc(p.baseline_end)}</span>`
                          : `<span class="sub">尚未凍結基準線</span>`}</div>
      <div class="projfields">
        <div class="field"><label>專案代號</label><input data-pk="code" data-pid="${p.id}" value="${esc(p.code)}"></div>
        <div class="field"><label>專案名稱</label><input data-pk="name" data-pid="${p.id}" value="${esc(p.name)}"></div>
        <div class="field"><label>主要負責人</label><select data-pk="owner" data-pid="${p.id}">
          <option value="">（未指派）</option>${people.map(person =>
            `<option ${person.name === p.owner ? "selected" : ""}>${esc(person.name)}</option>`).join("")}
        </select></div>
        <div class="field"><label>開始日</label><input type="date" data-pk="start_date" data-pid="${p.id}" value="${esc(p.start_date || "")}"></div>
        <div class="field"><label>目標完工日</label><input type="date" data-pk="end_date" data-pid="${p.id}" value="${esc(p.end_date || "")}"></div>
      </div>
      <div class="field" style="margin-top:6px">
        <label>協同負責人（可複選，非必填——不只一個人負責這個專案時用）</label>
        <div class="coowners" data-pid="${p.id}">${people.map(person => `
          <label class="chk" style="display:inline-flex;margin-right:14px">
            <input type="checkbox" class="coOwnerChk" value="${esc(person.name)}"
              ${(p.co_owners || []).includes(person.name) ? "checked" : ""}> ${esc(person.name)}
          </label>`).join("")}</div>
      </div>
      <p class="sub">凍結前可隨時修改目標完工日；凍結後修改此欄不影響承諾完工日，如需調整承諾完工日，請至該專案分頁之「WBS 表」子分頁執行「重新基準化」</p>
    </div>`).join("");
  $$(".coowners", $("#projForm")).forEach(box => $$(".coOwnerChk", box).forEach(chk => chk.onchange = async () => {
    const names = $$(".coOwnerChk", box).filter(c => c.checked).map(c => c.value);
    await post("/api/projects", { id: +box.dataset.pid, co_owners: names });
    toast("已儲存");
  }));
  $$("#projForm [data-pk]").forEach(inp => inp.onchange = async () => {
    await post("/api/projects", { id: +inp.dataset.pid, [inp.dataset.pk]: inp.value });
    projects = []; toast("已儲存"); await ensureProjects();
  });

  const cfg = await api("/api/config");
  $("#cfgForm").innerHTML = CFG_FIELDS.map(([k, l, h]) =>
    `<label>${l}</label><div><input id="cfg_${k}" value="${esc(cfg[k] ?? "")}">
      <div class="sub">${esc(h)}</div></div>`).join("") +
    `<label>每日工作時段</label><div><input id="cfg_blocks" value="${esc(JSON.stringify(cfg.blocks || {}))}">
      <div class="sub">各專案代號對應之工作時段，用於產生日曆區塊</div></div>` +
    `<label>國定假日</label><div><input id="cfg_holidays" value="${esc((cfg.holidays || []).join(","))}">
      <div class="sub">格式為 YYYY-MM-DD，以逗號分隔。未填寫將影響浮時計算之準確性。</div></div>` +
    `<label>桌面提醒</label><div><label class="chk" style="display:flex;align-items:center;gap:6px">
      <input type="checkbox" id="cfg_notify_enabled" ${cfg.notify_enabled ? "checked" : ""}> 開啟桌面提醒</label>
      <div class="sub">只在本程式視窗開著的時候才可能跳通知（Windows 系統通知，不寄信、不連外部服務）；
        程式沒開著就不會有任何提醒。有逾期／今天不能拖／階段缺件時才會跳，平常不會無故打擾。</div></div>` +
    `<label>Email 提醒</label><div><label class="chk" style="display:flex;align-items:center;gap:6px">
      <input type="checkbox" id="cfg_notify_email_enabled" ${cfg.notify_email_enabled ? "checked" : ""}> 額外寄一封信到指定信箱</label>
      <div class="sub">跟桌面提醒同一個觸發時機，額外寄一封信到下面填的信箱；同樣只在本程式視窗開著時才會觸發。</div>
      <div style="margin-top:6px"><input id="cfg_notify_email_to" placeholder="收件信箱，例如 you@gmail.com" value="${esc(cfg.notify_email_to || "")}">
      <div class="sub">未填寫時，就算勾選了上面的選項也不會寄信。</div></div>
      <div style="margin-top:10px" class="chk">
        <label class="chk" style="display:inline-flex;align-items:center;gap:4px;margin-right:16px">
          <input type="radio" name="cfg_email_mode" id="cfg_email_mode_outlook" value="outlook" ${cfg.notify_email_mode !== "smtp" ? "checked" : ""}> 用本機 Outlook 寄
        </label>
        <label class="chk" style="display:inline-flex;align-items:center;gap:4px">
          <input type="radio" name="cfg_email_mode" id="cfg_email_mode_smtp" value="smtp" ${cfg.notify_email_mode === "smtp" ? "checked" : ""}> 用 SMTP 帳號密碼寄
        </label>
      </div>
      <div class="sub" id="cfg_email_mode_outlook_hint" style="margin-top:2px">
        借用這台機器上已經登入的 Outlook 自動寄信，不用另外填帳號密碼；Outlook 沒有安裝、
        沒有登入、或公司政策鎖住自動化時會靜默失敗，不影響桌面提醒照常運作。</div>
      <div id="cfg_smtp_fields" style="margin-top:6px;display:${cfg.notify_email_mode === "smtp" ? "grid" : "none"};grid-template-columns:1fr 110px;gap:6px">
        <input id="cfg_smtp_host" placeholder="SMTP 伺服器，例如 smtp.gmail.com" value="${esc(cfg.smtp_host || "")}">
        <input id="cfg_smtp_port" placeholder="埠號" value="${esc(cfg.smtp_port ?? 465)}">
        <input id="cfg_smtp_user" placeholder="寄件帳號，例如 you@gmail.com" value="${esc(cfg.smtp_user || "")}" style="grid-column:1/3">
        <input id="cfg_smtp_pass" type="password" placeholder="密碼（Gmail 等服務請用「應用程式密碼」，不是登入密碼）" value="${esc(cfg.smtp_pass || "")}" style="grid-column:1/3">
        <div class="sub" style="grid-column:1/3">固定走 SSL（預設 465 埠）。密碼明碼存在本機 config.json，不會上傳、不會進 git；
          電腦沒裝 Outlook 或不想借用 Outlook 時用這個模式。</div>
      </div></div>`;

  const outlookRadio = $("#cfg_email_mode_outlook"), smtpRadio = $("#cfg_email_mode_smtp");
  const toggleEmailMode = () => {
    $("#cfg_smtp_fields").style.display = smtpRadio.checked ? "grid" : "none";
    $("#cfg_email_mode_outlook_hint").style.display = smtpRadio.checked ? "none" : "block";
  };
  outlookRadio.onchange = toggleEmailMode;
  smtpRadio.onchange = toggleEmailMode;

  await loadTemplateTab();
}

// 文件命名規則的範本編輯——改的是 stage_template.json 共用範本，不是某個專案自己
// 的文件清單。按階段分頁顯示，跟其他地方一樣「有分頁就分頁」，不要塞成一長串。
async function loadTemplateTab() {
  // 加/刪文件項目後會整個重繪這個分頁，記住目前停在哪個階段子分頁，
  // 重繪完照樣停在那裡，不要每次都跳回第一站。
  const prevSub = $("#tplStageTabs button.on")?.dataset.sub;

  const tpl = await api("/api/stage-template");
  $("#tplRuleBody").innerHTML = `<p><code>${esc(tpl.filename_rule)}</code><br>
    <span class="sub">例：${esc(tpl.filename_example)}</span></p>`;

  const nav = $("#tplStageTabs");
  nav.innerHTML = tpl.stages.map((st, i) => {
    const sub = `tplst-${esc(st.code)}`;
    const on = prevSub ? sub === prevSub : i === 0;
    return `<button data-sub="${sub}" class="${on ? "on" : ""}">${esc(st.code)} ${esc(st.name)}</button>`;
  }).join("");

  // 這些階段子分頁面板要跟 nav 是同一層的兄弟節點，子分頁切換的通用邏輯
  // （main 的 click 代理）是用 nav.parentElement 直接找 :scope > .subtab，
  // 包一層額外的容器 div 會讓它找不到，點了不會真的切換內容。
  $$(".tplStagePanel").forEach(el => el.remove());
  const panelsHTML = tpl.stages.map((st, i) => {
    const sub = `tplst-${esc(st.code)}`;
    const on = prevSub ? sub === prevSub : i === 0;
    return `
    <div class="subtab tplStagePanel ${on ? "on" : ""}" data-subpanel="${sub}">
      <p class="sub" style="margin:10px 0 14px">出場條件：${esc(st.exit_gate || "—")}</p>
      ${st.docs.map(d => `
        <div class="doc">
          <span class="dcode" contenteditable data-stage="${esc(st.code)}" data-code="${esc(d.code)}" data-k="code"
                title="代碼只影響之後新建立的專案，已經用這份範本建立過的專案不會被改動">${esc(d.code)}</span>
          <div class="dn">
            <b contenteditable data-stage="${esc(st.code)}" data-code="${esc(d.code)}" data-k="name">${esc(d.name)}</b>
            <label class="sub" style="margin-left:8px;white-space:nowrap">
              <input type="checkbox" class="tplRequired" data-stage="${esc(st.code)}"
                     data-code="${esc(d.code)}" ${d.required ? "checked" : ""}> 必繳
            </label>
            <div class="sub" contenteditable data-stage="${esc(st.code)}" data-code="${esc(d.code)}" data-k="note">${esc(d.note || "")}</div>
          </div>
          <button class="ghost delTplDoc" data-stage="${esc(st.code)}" data-code="${esc(d.code)}"
                  data-name="${esc(d.name)}">刪</button>
        </div>`).join("") || '<p class="sub">此階段之範本目前未設定任何文件項目。</p>'}
      <button class="ghost addTplDoc" data-stage="${esc(st.code)}" style="margin-top:10px">＋ 新增文件項目</button>
    </div>`;
  }).join("");
  nav.insertAdjacentHTML("afterend", panelsHTML);
  const panels = nav.parentElement;

  $$("[contenteditable]", panels).forEach(el => {
    const orig = el.textContent;
    el.onblur = async () => {
      const v = el.textContent.trim();
      if (v === orig) return;
      if (el.dataset.k === "name" && !v) { el.textContent = orig; toast("名稱為必填欄位"); return; }
      if (el.dataset.k === "code" && !v) { el.textContent = orig; toast("代碼為必填欄位"); return; }
      // 代碼改的是「查找鍵」本身，不能沿用其他欄位那套 {[k]: v} 寫法（後端的 code
      // 參數是拿來找是哪一筆，不是要改成什麼）——用 new_code 表達「要改成什麼」，
      // 跟 code（找哪一筆）分開。改成功後代碼變了，其他欄位（名稱/備註）DOM 上
      // 記著的 data-code 還是舊的，之後編輯會找不到，所以整個分頁重新渲染一次。
      const body = el.dataset.k === "code"
        ? { stage_code: el.dataset.stage, code: el.dataset.code, new_code: v }
        : { stage_code: el.dataset.stage, code: el.dataset.code, [el.dataset.k]: v };
      try {
        await post("/api/stage-template/doc/edit", body);
        toast("已儲存");
        if (el.dataset.k === "code") loadTemplateTab();
      } catch (e) {
        el.textContent = orig; toast(e.message || "儲存失敗");
      }
    };
    el.onkeydown = e => { if (e.key === "Enter") { e.preventDefault(); el.blur(); } };
  });
  $$(".tplRequired", panels).forEach(cb => cb.onchange = async () => {
    try {
      await post("/api/stage-template/doc/edit",
        { stage_code: cb.dataset.stage, code: cb.dataset.code, required: cb.checked });
      toast("已更新");
    } catch (e) {
      cb.checked = !cb.checked; toast(e.message || "更新失敗");
    }
  });
  $$(".delTplDoc", panels).forEach(btn => btn.onclick = async () => {
    if (!confirm(`確定自範本刪除「${btn.dataset.name}」？\n（僅影響日後新建專案套用之範本，不影響現有專案）`)) return;
    try {
      await api(`/api/stage-template/doc/${btn.dataset.stage}/${btn.dataset.code}`, { method: "DELETE" });
      toast("已刪除"); loadTemplateTab();
    } catch (e) {
      toast(e.message || "刪除失敗");
    }
  });
  $$(".addTplDoc", panels).forEach(btn => btn.onclick = async () => {
    const name = prompt("新文件項目名稱：");
    if (!name || !name.trim()) return;
    const code = prompt("請輸入文件代碼（英數字，同一階段內須唯一，例如 XXX-YYY）：");
    if (!code || !code.trim()) return;
    try {
      await post("/api/stage-template/doc",
        { stage_code: btn.dataset.stage, code: code.trim(), name: name.trim(), required: true });
      toast("已新增"); loadTemplateTab();
    } catch (e) {
      toast(e.message || "新增失敗");
    }
  });
}
$("#saveCfg").onclick = async () => {
  const body = {};
  CFG_FIELDS.forEach(([k]) => {
    let v = $("#cfg_" + k).value;
    if (["port", "amber_float_days"].includes(k)) v = +v;
    body[k] = v;
  });
  try { body.blocks = JSON.parse($("#cfg_blocks").value || "{}"); }
  catch { return toast("工作時段格式錯誤，須為 JSON 格式"); }
  body.holidays = $("#cfg_holidays").value.split(",").map(s => s.trim()).filter(Boolean);
  body.notify_enabled = $("#cfg_notify_enabled").checked;
  body.notify_email_enabled = $("#cfg_notify_email_enabled").checked;
  body.notify_email_to = $("#cfg_notify_email_to").value.trim();
  body.notify_email_mode = $("#cfg_email_mode_smtp").checked ? "smtp" : "outlook";
  body.smtp_host = $("#cfg_smtp_host").value.trim();
  body.smtp_port = +$("#cfg_smtp_port").value || 465;
  body.smtp_user = $("#cfg_smtp_user").value.trim();
  body.smtp_pass = $("#cfg_smtp_pass").value;
  await post("/api/config", body);
  toast("已儲存。變更埠號後須重新啟動服務。");
};

$("#testEmail").onclick = async () => {
  $("#saveCfg").click();
  $("#testEmail").disabled = true;
  $("#cfgMsg").textContent = "寄送中…";
  try {
    const r = await post("/api/notify/test-email", {});
    if (r.ok) {
      $("#cfgMsg").textContent = r.note ? `已送出。${r.note}` : "測試信已寄出，請去收件匣確認。";
    } else {
      $("#cfgMsg").textContent = `寄送失敗：${r.error || "未知錯誤"}`;
    }
  } catch (e) {
    $("#cfgMsg").textContent = `寄送失敗：${e.message || e}`;
  } finally {
    $("#testEmail").disabled = false;
  }
};

/* ---------------------------------------------------------- 版本徽章 */
// 不做即時跳動的時鐘——作業系統本來就有，戰情室式重複顯示沒有意義。這裡顯示的是
// 「上次啟動時間」：只有你真的重開 start.bat 才會變，是「畫面上跑的是不是今天改過
// 的新程式碼」唯一可信的證據；版號是手動維護的數字，改了程式碼忘記重啟一樣會顯示
// 新版號，容易誤判「已經生效」。
async function loadVersion() {
  try {
    const v = await api("/api/version");
    $("#hdrVer").innerHTML =
      `<span title="服務程序啟動時間，重新啟動 start.bat 後更新">上次啟動 ${esc(v.started_at)}</span>
       <b>v${esc(v.version)}</b>`;
  } catch {
    // 刻意不悄悄不顯示——這個徽章存在的目的就是讓人看出「新程式碼有沒有生效」，
    // 拿不到版本資訊本身就是最強的那個訊號（通常代表伺服器行程比程式碼舊，要重開
    // start.bat），悄悄吞掉等於把這個徵兆蓋住，正好違背這個功能自己的目的。
    $("#hdrVer").innerHTML =
      '<span class="verwarn" title="無法連線至 /api/version，通常表示伺服器程序版本落後於目前程式碼，請重新啟動 start.bat">⚠ 版本資訊未同步，請重新啟動 start.bat</span>';
  }
}

/* ---------------------------------------------------------- 登入
   只有 Server 模式（config.json 的 bind_host 不是 127.0.0.1）才會要求登入；
   單機版 auth_required 是 false，畫面直接跳過登入、行為跟以前完全一樣。 */
const LAST_LOGIN_NAME_KEY = "wbsLastLoginName";

async function showLogin() {
  $("#loginOverlay").style.display = "flex";
  $("#whoami").style.display = "none";
  // 姓名是真正的 <input>（不是選單）——瀏覽器的密碼管理員只認得到真正的
  // input+password 這組搭配，才會在登入成功後跳出「要不要記住密碼」，選單
  // 它存不了對應的帳密。用 <datalist> 給既有姓名的建議清單（列表來自
  // /api/auth/login-names，登入前就打得到），保留「可以直接選」的方便，
  // 同時還是一個真正的文字欄位。記住上次登入的人，下次開啟直接幫他填好，
  // 只要輸密碼、讓瀏覽器記住的密碼自動帶出來就好。
  $("#loginNameInput").value = localStorage.getItem(LAST_LOGIN_NAME_KEY) || "";
  try {
    const names = await api("/api/auth/login-names");
    $("#loginNameList").innerHTML = names.map(n => `<option value="${esc(n)}">`).join("");
  } catch { /* 名單抓不到也不擋登入畫面，使用者還是能手動打名字 */ }
}
function hideLogin() {
  $("#loginOverlay").style.display = "none";
}

let loginMode = "login";  // "login" | "register"
$("#loginToggleMode").onclick = () => {
  loginMode = loginMode === "login" ? "register" : "login";
  const reg = loginMode === "register";
  $("#loginTitle").textContent = reg ? "註冊新帳號" : "專案 WBS 追蹤";
  $("#loginHint").textContent = reg
    ? "適用於人員名單裡完全沒有你名字的情況。姓名要獨一無二，之後就是你的登入帳號。"
    : "這台是共用 Server，請先登入。如果人員名單裡已經有你的名字，第一次登入輸入的密碼就會變成你的密碼。";
  $("#loginPassword2").style.display = reg ? "block" : "none";
  $("#loginPassword2Label").style.display = reg ? "block" : "none";
  $("#loginSubmitBtn").textContent = reg ? "註冊並登入" : "登入";
  $("#loginToggleMode").textContent = reg ? "我已經有帳號，改用登入" : "名單裡沒有我的名字，我要註冊新帳號";
  $("#loginMsg").textContent = "";
};

$("#loginForm").onsubmit = async (e) => {
  e.preventDefault();
  $("#loginMsg").textContent = "";
  const name = $("#loginNameInput").value.trim();
  const password = $("#loginPassword").value;
  if (loginMode === "register" && password !== $("#loginPassword2").value) {
    $("#loginMsg").textContent = "兩次輸入的密碼不一樣";
    return;
  }
  try {
    const r = await post(loginMode === "register" ? "/api/auth/register" : "/api/auth/login",
      { name, password });
    localStorage.setItem(LAST_LOGIN_NAME_KEY, name);
    hideLogin();
    showWhoami(r.person);
    boot();
  } catch (err) {
    $("#loginMsg").textContent = err.message || (loginMode === "register" ? "註冊失敗" : "登入失敗");
  }
};

$("#logoutBtn").onclick = async () => {
  await post("/api/auth/logout", {});
  location.reload();
};

function showWhoami(person) {
  currentPerson = person || null;
  applyRoleVisibility();
  if (!person) { $("#whoami").style.display = "none"; return; }
  $("#whoamiName").textContent = person.name +
    (person.role !== "user" ? `（${ROLE_LABEL[person.role]}）` : "");
  $("#whoami").style.display = "flex";
}

// 「設定」子分頁裡有幾塊是系統管理層級的東西（專案基本資料、人員名單、環境
// 設定、清空/還原資料）——一般使用者不該看到，manager/admin 才看得到。
// 這只是前端藏起來方便使用；真正的防線在後端每支 API 各自的角色檢查，藏
// UI 只是不要讓一般使用者以為自己「可以」點那些東西。單機版（沒有登入）
// 不受影響，維持全部看得到的舊行為。
function applyRoleVisibility() {
  const restricted = currentPerson && currentPerson.role === "user";
  $$(".mgrOnly").forEach(el => el.style.display = restricted ? "none" : "");
  $$(".adminOnly").forEach(el => el.style.display =
    (currentPerson && currentPerson.role !== "admin") ? "none" : "");
  // 如果目前選到的「設定」子分頁剛好被藏起來（例如一般使用者殘留上次選到
  // 「專案基本資料」），改跳去「文件命名規則」，不要留一片空白給他看。
  const settingsTab = $("#tab-settings");
  const activeSub = settingsTab && $(".subtabs button.on[data-sub]", settingsTab);
  if (restricted && activeSub && activeSub.classList.contains("mgrOnly")) {
    const fallback = $('.subtabs button[data-sub="naming"]', settingsTab);
    if (fallback) fallback.click();
  }
}

/* ---------------------------------------------------------- init */
async function boot() {
  await ensureProjects();
  $("#hdrSub").textContent = projects.map(p => p.code + " " + p.name).join("　·　");
  loadVersion();
  await initProjectPages();
  // 開啟畫面回到上次停留的分頁，不是每次都固定跳回「今日」——記的是分頁本身
  // （含個別專案分頁），存在 localStorage 裡跟著這台瀏覽器走，換分頁會即時更新。
  const lastTab = localStorage.getItem("lastTab");
  const lastBtn = lastTab && $(`#tabs button[data-tab="${lastTab}"]`);
  if (lastBtn) {
    lastBtn.click();
  } else {
    loadToday();
  }
}

(async () => {
  const me = await api("/api/auth/me");
  if (me.auth_required && !me.person) {
    showLogin();
    return;  // boot() 會在登入成功後由 #loginForm 的 submit handler 呼叫
  }
  showWhoami(me.person);
  boot();
})();
