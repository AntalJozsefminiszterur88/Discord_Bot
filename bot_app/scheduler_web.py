from bot_app.core import *
from zoneinfo import ZoneInfo


BUDAPEST_TZ = ZoneInfo("Europe/Budapest")


def now_budapest() -> datetime.datetime:
    return datetime.datetime.now(BUDAPEST_TZ)


def sort_scheduled_messages(items: list[dict]) -> list[dict]:
    status_order = {
        "pending": 0,
        "processing": 1,
        "failed": 2,
        "sent": 3,
    }
    return sorted(
        items,
        key=lambda item: (
            status_order.get(str(item.get("status", "pending")), 9),
            str(item.get("scheduled_at", "")),
        ),
    )


def parse_scheduler_datetime(raw_value: str) -> Optional[datetime.datetime]:
    value = raw_value.strip()
    if not value:
        return None

    if value.endswith("Z"):
        value = value[:-1] + "+00:00"

    try:
        parsed = datetime.datetime.fromisoformat(value)
    except ValueError:
        return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=BUDAPEST_TZ)
    else:
        parsed = parsed.astimezone(BUDAPEST_TZ)
    return parsed


def normalize_recurrence(raw_recurrence: Optional[str]) -> str:
    value = str(raw_recurrence or "").strip().lower()
    if value in {"yearly", "annual", "year", "yearly_repeat", "évente", "evente"}:
        return "yearly"
    if value in {"none", "once", "single", "egyszeri"}:
        return "none"
    return "none"


def compute_next_yearly_occurrence(source_datetime: datetime.datetime) -> datetime.datetime:
    target_year = source_datetime.year + 1
    max_day = calendar.monthrange(target_year, source_datetime.month)[1]
    target_day = min(source_datetime.day, max_day)
    return source_datetime.replace(year=target_year, day=target_day)


def calculate_elapsed_years(
    base_datetime: datetime.datetime, reference_datetime: datetime.datetime
) -> int:
    years = reference_datetime.year - base_datetime.year
    if (
        (reference_datetime.month, reference_datetime.day)
        < (base_datetime.month, base_datetime.day)
    ):
        years -= 1
    return max(0, years)


def render_scheduled_message_text(
    template: str,
    base_datetime: Optional[datetime.datetime],
    reference_datetime: datetime.datetime,
) -> str:
    rendered = template
    if base_datetime is not None:
        elapsed_years = calculate_elapsed_years(base_datetime, reference_datetime)
        rendered = rendered.replace("{dátum}", str(elapsed_years))
        rendered = rendered.replace("{datum}", str(elapsed_years))
    return rendered


def resolve_effective_scheduled_at(
    base_scheduled_at: datetime.datetime,
    recurrence: str,
    now: Optional[datetime.datetime] = None,
) -> tuple[Optional[datetime.datetime], Optional[str]]:
    if now is None:
        current_time = now_budapest()
    elif now.tzinfo is None:
        current_time = now.replace(tzinfo=BUDAPEST_TZ)
    else:
        current_time = now.astimezone(BUDAPEST_TZ)
    scheduled_at = base_scheduled_at

    if recurrence == "yearly":
        while scheduled_at <= current_time:
            scheduled_at = compute_next_yearly_occurrence(scheduled_at)
        return scheduled_at, None

    if scheduled_at <= current_time:
        return None, "Egyszeri üzenetnél a scheduled_at időpontnak a jövőben kell lennie"

    return scheduled_at, None


def normalize_channel_id(raw_channel_id) -> Optional[int]:
    if raw_channel_id in {None, ""}:
        return None
    try:
        channel_id = int(raw_channel_id)
    except (TypeError, ValueError):
        return None
    if channel_id <= 0:
        return None
    return channel_id


def load_scheduled_messages() -> list[dict]:
    if not os.path.exists(SCHEDULED_MESSAGES_FILE):
        return []

    try:
        with open(SCHEDULED_MESSAGES_FILE, "r", encoding="utf-8") as state_file:
            data = json.load(state_file)
    except (OSError, json.JSONDecodeError):
        return []

    if not isinstance(data, list):
        return []

    loaded_messages = []
    for raw_item in data:
        if not isinstance(raw_item, dict):
            continue

        item_id = str(raw_item.get("id") or uuid4())
        message = str(raw_item.get("message") or "").strip()
        if not message:
            continue

        channel_id = normalize_channel_id(raw_item.get("channel_id"))
        if channel_id is None:
            continue

        scheduled_at = parse_scheduler_datetime(str(raw_item.get("scheduled_at") or ""))
        if scheduled_at is None:
            continue
        base_scheduled_at = parse_scheduler_datetime(
            str(raw_item.get("base_scheduled_at") or raw_item.get("origin_scheduled_at") or "")
        )
        if base_scheduled_at is None:
            base_scheduled_at = scheduled_at

        status = str(raw_item.get("status") or "").lower()
        if status not in {"pending", "processing", "sent", "failed"}:
            status = "sent" if raw_item.get("sent") else "pending"
        if status == "processing":
            status = "pending"
        recurrence = normalize_recurrence(raw_item.get("recurrence"))
        if recurrence == "yearly" and status == "sent":
            status = "pending"

        loaded_messages.append(
            {
                "id": item_id,
                "channel_id": channel_id,
                "message": message,
                "scheduled_at": scheduled_at.isoformat(timespec="seconds"),
                "base_scheduled_at": base_scheduled_at.isoformat(timespec="seconds"),
                "status": status,
                "recurrence": recurrence,
                "created_at": str(
                    raw_item.get("created_at")
                    or now_budapest().isoformat(timespec="seconds")
                ),
                "processed_at": raw_item.get("processed_at"),
                "last_sent_at": raw_item.get("last_sent_at"),
                "last_error": raw_item.get("last_error"),
            }
        )

    return sort_scheduled_messages(loaded_messages)


def save_scheduled_messages() -> None:
    try:
        with open(SCHEDULED_MESSAGES_FILE, "w", encoding="utf-8") as state_file:
            json.dump(scheduled_messages, state_file, indent=2, ensure_ascii=False)
    except OSError as e:
        print(f"Failed to save scheduled messages: {e}")


def build_scheduler_html(default_channel_id: Optional[int]) -> str:
    default_channel = "" if default_channel_id is None else str(default_channel_id)
    html_content = """
<!doctype html>
<html lang="hu">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>Discord Üzenet Időzítő</title>
  <style>
    :root {
      --bg: #0f172a;
      --panel: #111827;
      --panel-alt: #1f2937;
      --text: #f3f4f6;
      --muted: #9ca3af;
      --accent: #22c55e;
      --danger: #ef4444;
      --border: #334155;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: "Segoe UI", Tahoma, sans-serif;
      background: radial-gradient(circle at top, #1e293b 0%, #0f172a 55%);
      color: var(--text);
      min-height: 100vh;
      padding: 24px 14px 32px;
    }
    .wrap {
      max-width: 980px;
      margin: 0 auto;
      display: grid;
      gap: 18px;
    }
    .card {
      background: rgba(17, 24, 39, 0.92);
      border: 1px solid var(--border);
      border-radius: 14px;
      padding: 18px;
      box-shadow: 0 8px 28px rgba(0, 0, 0, 0.28);
    }
    h1, h2 {
      margin: 0 0 14px 0;
      font-weight: 700;
      letter-spacing: .2px;
    }
    p {
      margin: 0 0 16px 0;
      color: var(--muted);
      line-height: 1.45;
    }
    .hint {
      margin: 8px 0 0 0;
      color: #cbd5e1;
      font-size: 12px;
      line-height: 1.35;
    }
    .grid {
      display: grid;
      gap: 12px;
      grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
    }
    label {
      display: block;
      margin-bottom: 6px;
      font-size: 13px;
      color: #cbd5e1;
    }
    input, textarea, select {
      width: 100%;
      border: 1px solid var(--border);
      background: var(--panel-alt);
      color: var(--text);
      border-radius: 9px;
      padding: 10px 11px;
      font-size: 14px;
    }
    textarea { min-height: 110px; resize: vertical; }
    .actions {
      margin-top: 10px;
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
    }
    button {
      border: 0;
      border-radius: 9px;
      padding: 10px 14px;
      font-size: 14px;
      font-weight: 600;
      cursor: pointer;
      color: #fff;
      background: var(--accent);
    }
    button.refresh { background: #2563eb; }
    button.edit-row {
      background: #f59e0b;
      padding: 7px 10px;
      font-size: 12px;
      margin-right: 6px;
    }
    button.cancel { background: #475569; }
    button.delete {
      background: var(--danger);
      padding: 7px 10px;
      font-size: 12px;
    }
    #status {
      margin-top: 10px;
      font-size: 13px;
      color: #cbd5e1;
      min-height: 18px;
    }
    table {
      width: 100%;
      border-collapse: collapse;
      font-size: 13px;
    }
    th, td {
      border-bottom: 1px solid var(--border);
      text-align: left;
      padding: 8px 6px;
      vertical-align: top;
    }
    th { color: #dbeafe; font-weight: 700; }
    tr:last-child td { border-bottom: 0; }
    .status-pill {
      border-radius: 999px;
      padding: 2px 8px;
      font-size: 11px;
      display: inline-block;
      text-transform: uppercase;
      letter-spacing: .4px;
      border: 1px solid var(--border);
    }
    .status-pending { color: #fbbf24; }
    .status-processing { color: #f59e0b; }
    .status-sent { color: #4ade80; }
    .status-failed { color: #f87171; }
    code {
      background: #0b1220;
      border-radius: 6px;
      padding: 1px 5px;
      font-size: 12px;
    }
  </style>
</head>
<body>
  <div class="wrap">
    <section class="card">
      <h1>Discord Üzenet Időzítő</h1>
      <p>Itt tudsz dátum + időpont alapján üzenetet időzíteni. Éves ismétlésnél az időpont mezőbe az esemény eredeti dátumát is megadhatod, a bot pedig a következő évfordulótól küldi az üzenetet.</p>
      <form id="scheduleForm">
        <div class="grid">
          <div>
            <label for="scheduledAt">Esemény dátuma és időpontja</label>
            <input id="scheduledAt" name="scheduledAt" type="datetime-local" required />
          </div>
          <div>
            <label for="channelId">Csatorna ID (opcionális)</label>
            <input id="channelId" name="channelId" type="text" value="__DEFAULT_CHANNEL__" placeholder="pl. 123456789012345678" />
          </div>
          <div>
            <label for="recurrence">Ismétlődés</label>
            <select id="recurrence" name="recurrence">
              <option value="yearly" selected>Évente ismétlődő</option>
              <option value="none">Egyszeri</option>
            </select>
          </div>
        </div>
        <div style="margin-top: 12px;">
          <label for="message">Üzenet</label>
          <textarea id="message" name="message" required maxlength="2000" placeholder="Ide írd a küldendő üzenetet..."></textarea>
          <p class="hint">Tipp: a <code>{dátum}</code> vagy <code>{datum}</code> helyére a bot az esemény eredeti dátumától eltelt évek számát írja. Példa: ha az esemény 2017-ben történt, 2026-ban a token értéke 9 lesz.</p>
        </div>
        <div class="actions">
          <button id="submitBtn" type="submit">Üzenet időzítése</button>
          <button id="cancelEditBtn" class="cancel" type="button" style="display:none;">Szerkesztés megszakítása</button>
          <button class="refresh" type="button" id="refreshBtn">Lista frissítése</button>
        </div>
        <div id="status"></div>
      </form>
    </section>

    <section class="card">
      <h2>Időzített Üzenetek</h2>
      <div style="overflow-x:auto;">
        <table>
          <thead>
            <tr>
              <th>Állapot</th>
              <th>Ismétlődés</th>
              <th>Következő időpont</th>
              <th>Csatorna</th>
              <th>Üzenet</th>
              <th>Hiba</th>
              <th>Művelet</th>
            </tr>
          </thead>
          <tbody id="messageTableBody"></tbody>
        </table>
      </div>
    </section>
  </div>

  <script>
    const form = document.getElementById("scheduleForm");
    const statusBox = document.getElementById("status");
    const tableBody = document.getElementById("messageTableBody");
    const refreshBtn = document.getElementById("refreshBtn");
    const datetimeInput = document.getElementById("scheduledAt");
    const recurrenceSelect = document.getElementById("recurrence");
    const messageInput = document.getElementById("message");
    const channelIdInput = document.getElementById("channelId");
    const submitBtn = document.getElementById("submitBtn");
    const cancelEditBtn = document.getElementById("cancelEditBtn");
    const defaultChannelId = channelIdInput.value;
    let cachedItems = [];
    let editingMessageId = null;

    function setStatus(text, isError = false) {
      statusBox.textContent = text;
      statusBox.style.color = isError ? "#fca5a5" : "#cbd5e1";
    }

    function escapeHtml(text) {
      return String(text)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/\"/g, "&quot;")
        .replace(/'/g, "&#039;");
    }

    function formatDate(value) {
      if (!value) return "-";
      const dt = new Date(value);
      if (Number.isNaN(dt.getTime())) return value;
      return dt.toLocaleString("hu-HU");
    }

    function statusClass(status) {
      const value = String(status || "").toLowerCase();
      if (["pending", "processing", "sent", "failed"].includes(value)) {
        return "status-" + value;
      }
      return "";
    }

    function statusLabel(status) {
      const value = String(status || "").toLowerCase();
      if (value === "pending") return "függőben";
      if (value === "processing") return "feldolgozás";
      if (value === "sent") return "elküldve";
      if (value === "failed") return "hiba";
      return value || "-";
    }

    function recurrenceLabel(recurrence) {
      return String(recurrence || "").toLowerCase() === "yearly" ? "évente" : "egyszeri";
    }

    function toDatetimeLocalValue(value) {
      if (!value) return "";
      const normalized = String(value).replace(" ", "T");
      return normalized.length >= 16 ? normalized.slice(0, 16) : normalized;
    }

    function setEditMode(item) {
      editingMessageId = String(item.id || "");
      datetimeInput.value = toDatetimeLocalValue(item.base_scheduled_at || item.scheduled_at || "");
      channelIdInput.value = String(item.channel_id || "");
      recurrenceSelect.value = String(item.recurrence || "").toLowerCase() === "yearly" ? "yearly" : "none";
      messageInput.value = String(item.message || "");
      submitBtn.textContent = "Módosítás mentése";
      cancelEditBtn.style.display = "inline-block";
      setStatus("Szerkesztési mód: módosítsd az adatokat, majd mentsd.");
      form.scrollIntoView({ behavior: "smooth", block: "start" });
    }

    function resetEditMode() {
      editingMessageId = null;
      form.reset();
      channelIdInput.value = defaultChannelId;
      recurrenceSelect.value = "yearly";
      submitBtn.textContent = "Üzenet időzítése";
      cancelEditBtn.style.display = "none";
    }

    function findItemById(itemId) {
      return cachedItems.find(item => String(item.id || "") === String(itemId || ""));
    }

    async function loadMessages() {
      setStatus("Lista betöltése...");
      try {
        const response = await fetch("/scheduled-messages");
        if (!response.ok) {
          throw new Error("HTTP " + response.status);
        }
        const payload = await response.json();
        const items = Array.isArray(payload.items) ? payload.items : [];
        cachedItems = items;
        if (items.length === 0) {
          tableBody.innerHTML = '<tr><td colspan="7">Nincs időzített üzenet.</td></tr>';
          setStatus("Nincs időzített üzenet.");
          return;
        }

        tableBody.innerHTML = items.map(item => {
          const statusRaw = item.status || "pending";
          const message = escapeHtml(item.message || "");
          const errorText = escapeHtml(item.last_error || "");
          const channelId = escapeHtml(item.channel_id || "");
          const recurrence = escapeHtml(recurrenceLabel(item.recurrence));
          const itemId = escapeHtml(item.id || "");
          return `
            <tr>
              <td><span class="status-pill ${statusClass(statusRaw)}">${escapeHtml(statusLabel(statusRaw))}</span></td>
              <td>${recurrence}</td>
              <td>${escapeHtml(formatDate(item.scheduled_at))}</td>
              <td><code>${channelId}</code></td>
              <td>${message}</td>
              <td>${errorText || "-"}</td>
              <td>
                <button class="edit-row" data-action="edit" data-id="${itemId}">Szerkesztés</button>
                <button class="delete" data-action="delete" data-id="${itemId}">Törlés</button>
              </td>
            </tr>
          `;
        }).join("");
        setStatus("Lista frissítve.");
      } catch (error) {
        setStatus("Nem sikerült betölteni a listát: " + error.message, true);
      }
    }

    async function deleteMessage(itemId) {
      if (!itemId) return;
      try {
        const response = await fetch("/scheduled-messages/" + encodeURIComponent(itemId), {
          method: "DELETE"
        });
        if (!response.ok) {
          const text = await response.text();
          throw new Error(text || ("HTTP " + response.status));
        }
        if (editingMessageId === String(itemId || "")) {
          resetEditMode();
        }
        setStatus("Időzített üzenet törölve.");
        await loadMessages();
      } catch (error) {
        setStatus("Törlés sikertelen: " + error.message, true);
      }
    }

    function startEdit(itemId) {
      const item = findItemById(itemId);
      if (!item) {
        setStatus("A kiválasztott elem már nem található.", true);
        return;
      }
      setEditMode(item);
    }

    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const message = messageInput.value.trim();
      const scheduledAt = datetimeInput.value.trim();
      const channelId = channelIdInput.value.trim();
      const recurrence = recurrenceSelect.value;
      const isEditMode = Boolean(editingMessageId);

      if (!message || !scheduledAt) {
        setStatus("Az időpont és az üzenet kötelező.", true);
        return;
      }

      setStatus(isEditMode ? "Módosítás mentése..." : "Mentés...");
      try {
        const payload = {
          message,
          scheduled_at: scheduledAt,
          recurrence,
          channel_id: channelId
        };
        const endpoint = isEditMode
          ? "/scheduled-messages/" + encodeURIComponent(editingMessageId)
          : "/scheduled-messages";
        const method = isEditMode ? "PUT" : "POST";

        const response = await fetch(endpoint, {
          method,
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload)
        });

        if (!response.ok) {
          const text = await response.text();
          throw new Error(text || ("HTTP " + response.status));
        }

        resetEditMode();
        setStatus(isEditMode ? "Időzített üzenet módosítva." : "Időzített üzenet elmentve.");
        await loadMessages();
      } catch (error) {
        setStatus((isEditMode ? "Módosítás" : "Mentés") + " sikertelen: " + error.message, true);
      }
    });

    tableBody.addEventListener("click", (event) => {
      const btn = event.target.closest("button[data-action]");
      if (!btn) return;
      const itemId = btn.getAttribute("data-id");
      const action = btn.getAttribute("data-action");
      if (action === "delete") {
        deleteMessage(itemId);
        return;
      }
      if (action === "edit") {
        startEdit(itemId);
      }
    });

    refreshBtn.addEventListener("click", () => loadMessages());

    cancelEditBtn.addEventListener("click", () => {
      resetEditMode();
      setStatus("Szerkesztés megszakítva.");
    });

    resetEditMode();
    loadMessages();
  </script>
</body>
</html>
"""
    return html_content.replace("__DEFAULT_CHANNEL__", default_channel)


async def handle_scheduler_page(request):
    return web.Response(
        text=build_scheduler_html(TARGET_CHANNEL_ID),
        content_type="text/html",
    )


async def handle_list_scheduled_messages(request):
    async with scheduled_messages_lock:
        items = sort_scheduled_messages([dict(item) for item in scheduled_messages])
    return web.json_response({"items": items})


async def handle_create_scheduled_message(request):
    try:
        data = await request.json()
    except Exception:
        return web.Response(status=400, text="Érvénytelen JSON kérés")

    if not isinstance(data, dict):
        return web.Response(status=400, text="Érvénytelen kérésformátum")

    message = str(data.get("message") or "").strip()
    if not message:
        return web.Response(status=400, text="Az üzenet megadása kötelező")
    if len(message) > 2000:
        return web.Response(status=400, text="Az üzenet legfeljebb 2000 karakter lehet")

    scheduled_at_raw = str(data.get("scheduled_at") or "").strip()
    base_scheduled_at = parse_scheduler_datetime(scheduled_at_raw)
    if base_scheduled_at is None:
        return web.Response(
            status=400,
            text="A scheduled_at mezőnek érvényes dátum-időnek kell lennie (pl. 2026-03-02T19:30)",
        )

    channel_id = normalize_channel_id(data.get("channel_id"))
    if channel_id is None:
        if TARGET_CHANNEL_ID is None:
            return web.Response(
                status=400,
                text="A channel_id kötelező, mert nincs alapértelmezett TARGET_CHANNEL_ID beállítva",
            )
        channel_id = TARGET_CHANNEL_ID

    recurrence = normalize_recurrence(data.get("recurrence") or "yearly")
    scheduled_at, schedule_error = resolve_effective_scheduled_at(
        base_scheduled_at, recurrence
    )
    if schedule_error is not None or scheduled_at is None:
        return web.Response(status=400, text=schedule_error or "Érvénytelen scheduled_at")

    new_item = {
        "id": str(uuid4()),
        "channel_id": channel_id,
        "message": message,
        "scheduled_at": scheduled_at.isoformat(timespec="seconds"),
        "base_scheduled_at": base_scheduled_at.isoformat(timespec="seconds"),
        "status": "pending",
        "recurrence": recurrence,
        "created_at": now_budapest().isoformat(timespec="seconds"),
        "processed_at": None,
        "last_sent_at": None,
        "last_error": None,
    }

    async with scheduled_messages_lock:
        scheduled_messages.append(new_item)
        scheduled_messages[:] = sort_scheduled_messages(scheduled_messages)
        save_scheduled_messages()

    return web.json_response(new_item, status=201)


async def handle_update_scheduled_message(request):
    item_id = str(request.match_info.get("message_id") or "").strip()
    if not item_id:
        return web.Response(status=400, text="Hiányzó üzenet azonosító")

    try:
        data = await request.json()
    except Exception:
        return web.Response(status=400, text="Érvénytelen JSON kérés")

    if not isinstance(data, dict):
        return web.Response(status=400, text="Érvénytelen kérésformátum")

    async with scheduled_messages_lock:
        target_item = next(
            (item for item in scheduled_messages if str(item.get("id")) == item_id),
            None,
        )
        if target_item is None:
            return web.Response(status=404, text="Az időzített üzenet nem található")

        message = str(data.get("message", target_item.get("message") or "")).strip()
        if not message:
            return web.Response(status=400, text="Az üzenet megadása kötelező")
        if len(message) > 2000:
            return web.Response(status=400, text="Az üzenet legfeljebb 2000 karakter lehet")

        if "channel_id" in data:
            channel_id = normalize_channel_id(data.get("channel_id"))
            if channel_id is None and TARGET_CHANNEL_ID is None:
                return web.Response(
                    status=400,
                    text="A channel_id kötelező, mert nincs alapértelmezett TARGET_CHANNEL_ID beállítva",
                )
            if channel_id is None:
                channel_id = TARGET_CHANNEL_ID
        else:
            channel_id = normalize_channel_id(target_item.get("channel_id"))
            if channel_id is None:
                channel_id = TARGET_CHANNEL_ID
        if channel_id is None:
            return web.Response(
                status=400,
                text="A channel_id kötelező, mert nincs alapértelmezett TARGET_CHANNEL_ID beállítva",
            )

        recurrence = normalize_recurrence(
            data.get("recurrence", target_item.get("recurrence") or "yearly")
        )

        if "scheduled_at" in data:
            raw_base = str(data.get("scheduled_at") or "").strip()
        else:
            raw_base = str(
                target_item.get("base_scheduled_at") or target_item.get("scheduled_at") or ""
            ).strip()
        base_scheduled_at = parse_scheduler_datetime(raw_base)
        if base_scheduled_at is None:
            return web.Response(
                status=400,
                text="A scheduled_at mezőnek érvényes dátum-időnek kell lennie (pl. 2026-03-02T19:30)",
            )

        scheduled_at, schedule_error = resolve_effective_scheduled_at(
            base_scheduled_at, recurrence
        )
        if schedule_error is not None or scheduled_at is None:
            return web.Response(status=400, text=schedule_error or "Érvénytelen scheduled_at")

        target_item["message"] = message
        target_item["channel_id"] = channel_id
        target_item["recurrence"] = recurrence
        target_item["base_scheduled_at"] = base_scheduled_at.isoformat(timespec="seconds")
        target_item["scheduled_at"] = scheduled_at.isoformat(timespec="seconds")
        target_item["status"] = "pending"
        target_item["processed_at"] = None
        target_item["last_error"] = None
        save_scheduled_messages()
        updated_item = dict(target_item)

    return web.json_response(updated_item, status=200)


async def handle_delete_scheduled_message(request):
    item_id = str(request.match_info.get("message_id") or "").strip()
    if not item_id:
        return web.Response(status=400, text="Hiányzó üzenet azonosító")

    async with scheduled_messages_lock:
        match_index = next(
            (
                index
                for index, item in enumerate(scheduled_messages)
                if str(item.get("id")) == item_id
            ),
            None,
        )
        if match_index is None:
            return web.Response(status=404, text="Az időzített üzenet nem található")

        removed = scheduled_messages.pop(match_index)
        save_scheduled_messages()

    return web.json_response({"deleted": removed.get("id")})


async def dispatch_scheduled_message(item: dict) -> Optional[str]:
    channel_id = normalize_channel_id(item.get("channel_id"))
    if channel_id is None:
        return "Érvénytelen channel_id az időzített elemben"

    channel = bot.get_channel(channel_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(channel_id)
        except Exception as e:
            return f"A(z) {channel_id} csatorna nem érhető el: {e}"

    send_time = now_budapest()
    base_scheduled_at = parse_scheduler_datetime(str(item.get("base_scheduled_at") or ""))
    if base_scheduled_at is None:
        base_scheduled_at = parse_scheduler_datetime(str(item.get("scheduled_at") or ""))

    message_template = str(item.get("message") or "")
    rendered_message = render_scheduled_message_text(
        message_template, base_scheduled_at, send_time
    )

    try:
        await channel.send(
            rendered_message,
            allowed_mentions=discord.AllowedMentions(everyone=False),
        )
    except Exception as e:
        return f"Nem sikerült elküldeni az üzenetet: {e}"
    return None


async def scheduled_message_dispatch_loop():
    await bot.wait_until_ready()
    while not bot.is_closed():
        try:
            due_item_ids = []
            now = now_budapest()
            should_save = False

            async with scheduled_messages_lock:
                for item in scheduled_messages:
                    if str(item.get("status") or "pending") != "pending":
                        continue

                    scheduled_at = parse_scheduler_datetime(str(item.get("scheduled_at") or ""))
                    if scheduled_at is None:
                        item["status"] = "failed"
                        item["processed_at"] = now.isoformat(timespec="seconds")
                        item["last_error"] = "Érvénytelen dátumformátum a tárolt adatokban"
                        should_save = True
                        continue

                    if scheduled_at <= now:
                        item["status"] = "processing"
                        due_item_ids.append(str(item.get("id")))
                        should_save = True

                if should_save:
                    save_scheduled_messages()

            for item_id in due_item_ids:
                async with scheduled_messages_lock:
                    target_item = next(
                        (
                            item
                            for item in scheduled_messages
                            if str(item.get("id")) == item_id
                        ),
                        None,
                    )
                    if target_item is None:
                        continue
                    item_snapshot = dict(target_item)

                dispatch_error = await dispatch_scheduled_message(item_snapshot)
                processed_at = now_budapest().isoformat(timespec="seconds")

                async with scheduled_messages_lock:
                    target_item = next(
                        (
                            item
                            for item in scheduled_messages
                            if str(item.get("id")) == item_id
                        ),
                        None,
                    )
                    if target_item is None:
                        continue

                    target_item["processed_at"] = processed_at
                    if dispatch_error is None:
                        recurrence = normalize_recurrence(target_item.get("recurrence"))
                        target_item["last_sent_at"] = processed_at
                        target_item["last_error"] = None
                        if recurrence == "yearly":
                            current_scheduled_at = parse_scheduler_datetime(
                                str(target_item.get("scheduled_at") or "")
                            )
                            if current_scheduled_at is None:
                                current_scheduled_at = now_budapest()

                            next_scheduled_at = compute_next_yearly_occurrence(
                                current_scheduled_at
                            )
                            current_time = now_budapest()
                            while next_scheduled_at <= current_time:
                                next_scheduled_at = compute_next_yearly_occurrence(
                                    next_scheduled_at
                                )

                            target_item["scheduled_at"] = next_scheduled_at.isoformat(
                                timespec="seconds"
                            )
                            target_item["status"] = "pending"
                        else:
                            target_item["status"] = "sent"
                    else:
                        target_item["status"] = "failed"
                        target_item["last_error"] = dispatch_error
                        print(
                            f"Scheduled message dispatch failed ({item_id}): {dispatch_error}"
                        )
                    save_scheduled_messages()
        except Exception as e:
            print(f"Scheduler loop error: {e}")

        await asyncio.sleep(SCHEDULER_POLL_INTERVAL_SECONDS)


