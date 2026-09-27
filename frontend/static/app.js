// Время смен. В API его нет — оно одинаковое для всех дней и живёт в подписях
// таблицы (колонки G и I), поэтому держим его здесь константой.
const SHIFTS = {
    morning: { label: "Утро", start: 8 * 60, end: 10 * 60, time: "8:00–10:00", short: "8–10" },
    evening: { label: "Вечер", start: 17 * 60, end: 20 * 60, time: "17:00–20:00", short: "17–20" },
    saturday: { label: "Суббота", start: 8 * 60, end: 16 * 60, time: "8:00–16:00", short: "8–16" },
};

// Статичная разметка иконок — данных в ней нет, поэтому innerHTML здесь безопасен.
const SHIFT_ICONS = {
    morning:
        '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="4.2"/>' +
        '<path d="M12 2.5v2.6M12 18.9v2.6M2.5 12h2.6M18.9 12h2.6M5.3 5.3l1.8 1.8M16.9 16.9l1.8 1.8M5.3 18.7l1.8-1.8M16.9 7.1l1.8-1.8"/></svg>',
    evening:
        '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M20 14.6A8.2 8.2 0 0 1 9.4 4a8.2 8.2 0 1 0 10.6 10.6z"/></svg>',
    saturday:
        '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="8.2"/><path d="M12 7.5V12l3 2"/></svg>',
};

const MONTHS_GENITIVE = [
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
];
const WEEKDAYS_LONG = ["воскресенье", "понедельник", "вторник", "среда", "четверг", "пятница", "суббота"];
const DAY_MS = 24 * 60 * 60 * 1000;
const REQUEST_TIMEOUT_MS = 15000;
const VERSION_CHECK_INTERVAL_MS = 5 * 60 * 1000;

class DutyScheduleApp {
    constructor() {
        this.clockHmElement = document.getElementById("clock-hm");
        this.clockSecElement = document.getElementById("clock-sec");
        this.heroDateElement = document.getElementById("hero-date");
        this.heroDutyElement = document.getElementById("hero-duty");
        this.scheduleElement = document.getElementById("schedule");
        this.statusElement = document.getElementById("status");
        this.statusTextElement = document.getElementById("status-text");

        // Серверное время: момент по UTC, сдвиг пояса сервера и отметка
        // performance.now(), от которой идут локальные часы.
        this.serverEpoch = null;
        this.serverOffsetMinutes = 0;
        this.serverCapturedAt = null;

        this.data = null;
        this.isFetching = false;
        this.renderedDate = null;
        this.renderedMinute = null;
        this.heroKey = null;
        this.scheduleLayoutKey = null;
        this.dayElements = new Map();

        this.dataUpdateInterval = null;
        this.tickTimeout = null;
        this.background = null;
        this.heroProgressBars = [];
        this.enteringTimeout = null;
        this.versionCheckInterval = null;
        this.isReloading = false;
        this.performanceGuard = null;

        this.init();
    }

    init() {
        this.fetchData();
        this.dataUpdateInterval = setInterval(() => this.fetchData(), 30000);
        this.versionCheckInterval = setInterval(() => this.checkVersion(), VERSION_CHECK_INTERVAL_MS);
        this.performanceGuard = new PerformanceGuard(document.documentElement);
        this.performanceGuard.start();
        // Фон создаётся до первого tick(): часы сразу выставляют ему оттенок
        // части суток.
        this.background = new BackgroundController(document.querySelector(".background-animation"));
        this.background.start();
        this.tick();

        requestAnimationFrame(() => document.body.classList.add("is-ready"));
    }

    // --- Данные ----------------------------------------------------------

    async fetchData() {
        if (this.isFetching) {
            return;
        }

        this.isFetching = true;
        try {
            const payload = await this.requestJson("/api/data");
            if (!payload.success) {
                throw new Error("Некорректный ответ сервера");
            }

            this.processData(payload.data);
        } catch (error) {
            console.error("Ошибка загрузки данных:", error);
            this.handleFetchError(error);
        } finally {
            this.isFetching = false;
        }
    }

    // Запрос без кэша и с таймаутом. Без таймаута повисший запрос навсегда
    // оставил бы isFetching поднятым, и табло перестало бы обновляться.
    async requestJson(url) {
        const controller = new AbortController();
        const timeout = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
        try {
            const response = await fetch(`${url}?_=${Date.now()}`, {
                headers: {
                    "Cache-Control": "no-cache",
                    "Pragma": "no-cache",
                },
                cache: "no-store",
                signal: controller.signal,
            });
            if (!response.ok) {
                throw new Error(`HTTP ${response.status}`);
            }
            return await response.json();
        } catch (error) {
            if (error.name === "AbortError") {
                throw new Error(`Сервер не ответил за ${REQUEST_TIMEOUT_MS / 1000} с`);
            }
            throw error;
        } finally {
            clearTimeout(timeout);
        }
    }

    // После деплоя (watchtower) табло само подхватывает новую версию: иначе
    // телевизор показывал бы старые JS/CSS, пока страницу не обновят руками.
    // Версия страницы — в <html data-version>, текущая — в /version.
    async checkVersion() {
        const pageVersion = document.documentElement.dataset.version;
        if (!pageVersion || this.isReloading) {
            return;
        }
        try {
            const payload = await this.requestJson("/version");
            if (payload.version && payload.version !== pageVersion) {
                console.info(`Табло: новая версия ${payload.version} (была ${pageVersion}), перезагрузка`);
                this.isReloading = true;
                // Страница гаснет так же плавно, как проявляется при загрузке.
                document.body.classList.remove("is-ready");
                setTimeout(() => window.location.reload(), 900);
            }
        } catch (error) {
            // Сервер недоступен — проверим в следующий раз.
        }
    }

    processData(data) {
        this.syncServerTime(data.server_time);
        this.data = data;

        // Ошибка при пустом кэше — показывать нечего. Ошибка при непустом кэше
        // значит лишь, что последнее обновление не прошло: старые данные
        // остаются верными, поэтому табло продолжает их показывать.
        if (data.error && !data.last_updated) {
            this.showScheduleMessage("Не удалось загрузить график", data.error);
            this.heroKey = null;
            this.heroDutyElement.replaceChildren(this.createHeroMessage("Нет данных"));
            this.setStatus("error", "Нет данных из таблицы", data.error);
            return;
        }

        this.renderAll(true);

        const updated = this.formatTimestamp(data.last_updated);
        if (data.error) {
            this.setStatus("warning", `Таблица не обновляется · данные от ${updated}`, data.error);
        } else {
            this.setStatus("ok", `Обновлено ${updated}`);
        }
        this.restartAnimation(this.statusElement, "is-pulse");
    }

    handleFetchError(error) {
        if (this.data) {
            const updated = this.formatTimestamp(this.data.last_updated);
            this.setStatus("error", `Нет связи с сервером · данные от ${updated}`, error.message);
            return;
        }
        this.showScheduleMessage("Не удалось связаться с сервером", error.message);
        this.setStatus("error", "Нет связи с сервером", error.message);
    }

    setStatus(state, text, details = "") {
        this.statusElement.dataset.state = state;
        this.statusTextElement.textContent = text;
        this.statusElement.title = details || "";
    }

    // --- Время -----------------------------------------------------------

    syncServerTime(serverTime) {
        const epoch = Date.parse(serverTime);
        if (Number.isNaN(epoch)) {
            return;
        }
        this.serverEpoch = epoch;
        this.serverCapturedAt = performance.now();
        this.serverOffsetMinutes = this.parseOffsetMinutes(serverTime);
    }

    // Часы показывают время в поясе сервера, а не браузера: сдвиг берём из
    // ISO-строки ("+05:00"). Если его нет — пояс браузера.
    parseOffsetMinutes(isoString) {
        if (/Z$/i.test(isoString)) {
            return 0;
        }
        const match = isoString.match(/([+-])(\d{2}):?(\d{2})$/);
        if (!match) {
            return -new Date().getTimezoneOffset();
        }
        const sign = match[1] === "-" ? -1 : 1;
        return sign * (Number(match[2]) * 60 + Number(match[3]));
    }

    // «Настенное» время сервера в полях getUTC*() — так пояс браузера не
    // вмешивается в расчёты.
    getServerWallClock() {
        let epoch = Date.now();
        let offset = -new Date().getTimezoneOffset();
        if (this.serverEpoch !== null) {
            epoch = this.serverEpoch + (performance.now() - this.serverCapturedAt);
            offset = this.serverOffsetMinutes;
        }
        return new Date(epoch + offset * 60000);
    }

    tick() {
        const now = this.getServerWallClock();
        const hours = String(now.getUTCHours()).padStart(2, "0");
        const minutes = String(now.getUTCMinutes()).padStart(2, "0");
        const seconds = String(now.getUTCSeconds()).padStart(2, "0");
        this.clockHmElement.textContent = `${hours}:${minutes}`;
        this.clockSecElement.textContent = `:${seconds}`;

        const dateIso = this.toIsoDate(now);
        const minuteOfDay = now.getUTCHours() * 60 + now.getUTCMinutes();
        if (dateIso !== this.renderedDate) {
            this.heroDateElement.textContent = this.formatLongDate(now);
        }
        if (minuteOfDay !== this.renderedMinute) {
            this.background.setDaypart(now.getUTCHours());
        }
        if (this.data && (dateIso !== this.renderedDate || minuteOfDay !== this.renderedMinute)) {
            this.renderAll(false);
        }
        this.renderedDate = dateIso;
        this.renderedMinute = minuteOfDay;

        // Выравниваемся по границе секунды, чтобы секунды не «проскакивали».
        const delay = 1000 - (now.getTime() % 1000) + 20;
        this.tickTimeout = setTimeout(() => this.tick(), delay);
    }

    // --- Рендер ----------------------------------------------------------

    renderAll(dataChanged) {
        if (!this.data || (this.data.error && !this.data.last_updated)) {
            return;
        }
        const now = this.getServerWallClock();
        const todayIso = this.toIsoDate(now);
        const minuteOfDay = now.getUTCHours() * 60 + now.getUTCMinutes();

        this.renderHero(todayIso, minuteOfDay);
        if (dataChanged || todayIso !== this.renderedDate) {
            this.renderSchedule(this.data.weeks || [], todayIso);
        }
        this.updateShiftStates(todayIso, minuteOfDay);
    }

    // Смена, которая идёт сейчас, подсвечивается и в сетке, завершённые
    // сегодняшние — приглушаются. Пересчитывается раз в минуту.
    updateShiftStates(todayIso, minuteOfDay) {
        this.dayElements.forEach((entry, date) => {
            Object.entries(entry.slots).forEach(([kind, slot]) => {
                const state = date === todayIso ? this.getShiftState(kind, minuteOfDay) : "";
                slot.classList.toggle("is-current", state === "current");
                slot.classList.toggle("is-done", state === "done");
            });
        });
    }

    getShifts(day) {
        if (!day) {
            return [];
        }
        if (day.weekday === "СБ" || day.weekday === "ВС") {
            const names = this.splitPersons(day.evening);
            return names.length ? [{ kind: "saturday", names }] : [];
        }
        return ["morning", "evening"]
            .map((kind) => ({ kind, names: this.splitPersons(day[kind]) }))
            .filter((shift) => shift.names.length > 0);
    }

    splitPersons(value) {
        // В ячейке бывает несколько дежурных (суббота) — каждый отдельно.
        return (value || "")
            .split(",")
            .map((name) => name.trim())
            .filter(Boolean);
    }

    // Главный блок показывает сегодняшние смены, пока хоть одна не закончилась.
    // После последней смены (или в день без дежурств) — ближайший день с дежурством.
    renderHero(todayIso, minuteOfDay) {
        const days = (this.data.weeks || []).flat();
        const todayShifts = this.getShifts(days.find((day) => day.date === todayIso));
        const pendingToday = todayShifts.some((shift) => minuteOfDay < SHIFTS[shift.kind].end);

        let heading = "Сегодня";
        let note = "";
        let shifts = todayShifts;
        let isToday = true;

        if (!pendingToday) {
            const nextDay = days.find((day) => day.date > todayIso && this.getShifts(day).length > 0);
            if (!todayShifts.length) {
                note = "Сегодня дежурств нет";
            }
            if (nextDay) {
                heading = this.formatRelativeDay(nextDay.date, todayIso);
                shifts = this.getShifts(nextDay);
                isToday = false;
            } else {
                shifts = [];
            }
        }

        const view = shifts.map((shift) => ({
            ...shift,
            state: isToday ? this.getShiftState(shift.kind, minuteOfDay) : "upcoming",
        }));
        const key = JSON.stringify({ heading, note, view });
        if (key === this.heroKey) {
            this.updateHeroProgress(minuteOfDay);
            return;
        }
        this.heroKey = key;
        this.heroProgressBars = [];

        if (!view.length) {
            this.heroDutyElement.replaceChildren(
                this.createHeroMessage(note || "Дежурств на ближайшие две недели нет")
            );
            return;
        }

        const header = this.createElement("div", "hero-heading");
        header.append(this.createElement("span", "hero-heading-day", heading));
        if (note) {
            header.append(this.createElement("span", "hero-heading-note", note));
        }

        const cards = this.createElement("div", "hero-shifts");
        view.forEach((shift) => cards.append(this.createHeroShift(shift)));

        // Стартовое значение прогресса ставится до вставки в документ —
        // иначе полоса «доехала» бы до него анимацией от нуля.
        this.updateHeroProgress(minuteOfDay);
        this.heroDutyElement.replaceChildren(header, cards);
    }

    updateHeroProgress(minuteOfDay) {
        (this.heroProgressBars || []).forEach(({ kind, bar }) => {
            const shift = SHIFTS[kind];
            const fraction = (minuteOfDay - shift.start) / (shift.end - shift.start);
            bar.style.transform = `scaleX(${Math.min(1, Math.max(0, fraction)).toFixed(4)})`;
        });
    }

    getShiftState(kind, minuteOfDay) {
        const shift = SHIFTS[kind];
        if (minuteOfDay >= shift.end) {
            return "done";
        }
        if (minuteOfDay >= shift.start) {
            return "current";
        }
        return "upcoming";
    }

    createHeroShift(shift) {
        const meta = SHIFTS[shift.kind];
        const card = this.createElement("div", `hero-shift shift-${shift.kind} is-${shift.state}`);

        const top = this.createElement("div", "hero-shift-top");
        top.append(this.createIcon(shift.kind));
        top.append(this.createElement("span", "hero-shift-label", meta.label));
        top.append(this.createElement("span", "hero-shift-time", meta.time));
        if (shift.state === "current") {
            top.append(this.createElement("span", "hero-shift-badge", "сейчас"));
        } else if (shift.state === "done") {
            top.append(this.createElement("span", "hero-shift-badge", "завершено"));
        }

        const names = this.createElement("div", "hero-shift-names");
        shift.names.forEach((name) => names.append(this.createElement("span", "hero-person", name)));

        card.append(top, names);

        if (shift.state === "current") {
            const track = this.createElement("div", "hero-shift-progress");
            const bar = this.createElement("span");
            track.append(bar);
            card.append(track);
            this.heroProgressBars.push({ kind: shift.kind, bar });
        }
        return card;
    }

    createHeroMessage(text) {
        return this.createElement("div", "hero-message", text);
    }

    renderSchedule(weeks, todayIso) {
        if (!weeks.length) {
            this.showScheduleMessage("Нет дежурств на ближайшие 2 недели");
            return;
        }

        // Пока окно дат и «сегодня» те же, перестраивать сетку незачем: меняем
        // только ячейки, в которых поменялись люди.
        const layoutKey = todayIso + "|" + weeks.map((week) => week.map((day) => day.date).join(",")).join(";");
        if (layoutKey === this.scheduleLayoutKey) {
            weeks.flat().forEach((day) => this.updateDay(day));
            return;
        }

        this.scheduleLayoutKey = layoutKey;
        this.dayElements.clear();
        const fragment = document.createDocumentFragment();
        weeks.forEach((week, weekIndex) => fragment.append(this.createWeek(week, todayIso, weekIndex)));
        this.scheduleElement.replaceChildren(fragment);

        // Каскадное появление ячеек — только при полной перестройке сетки
        // (загрузка и смена дня), точечные обновления его не запускают.
        this.restartAnimation(this.scheduleElement, "is-entering");
        clearTimeout(this.enteringTimeout);
        this.enteringTimeout = setTimeout(() => this.scheduleElement.classList.remove("is-entering"), 2500);
    }

    createWeek(week, todayIso, weekIndex) {
        const element = this.createElement("div", "week panel");
        const containsToday = week.some((day) => day.date === todayIso);

        const labels = this.createElement("div", "week-labels");
        const caption = this.createElement("div", "week-caption");
        caption.append(this.createElement("span", "week-title", containsToday ? "Эта неделя" : "Неделя"));
        if (week.length) {
            caption.append(
                this.createElement("span", "week-range", `${week[0].date_str}–${week[week.length - 1].date_str}`)
            );
        }
        labels.append(caption);
        ["morning", "evening"].forEach((kind) => {
            const label = this.createElement("div", `shift-label shift-${kind}`);
            label.append(this.createIcon(kind));
            label.append(this.createElement("span", "shift-label-name", SHIFTS[kind].label));
            label.append(this.createElement("span", "shift-label-time", SHIFTS[kind].short));
            labels.append(label);
        });
        labels.style.setProperty("--i", weekIndex * 7);
        element.append(labels);

        week.forEach((day, dayIndex) => {
            const dayElement = this.createDay(day, todayIso);
            dayElement.style.setProperty("--i", weekIndex * 7 + dayIndex + 1);
            element.append(dayElement);
        });
        return element;
    }

    createDay(day, todayIso) {
        const isSaturday = day.weekday === "СБ" || day.weekday === "ВС";
        const classes = ["day"];
        if (isSaturday) classes.push("is-saturday");
        if (day.date === todayIso) classes.push("is-today");
        if (day.date < todayIso) classes.push("is-past");

        const element = this.createElement("div", classes.join(" "));
        element.dataset.date = day.date;

        const head = this.createElement("div", "day-head");
        head.append(this.createElement("span", "day-weekday", day.weekday));
        head.append(this.createElement("span", "day-date", day.date_str));
        if (day.date === todayIso) {
            head.append(this.createElement("span", "day-today", "сегодня"));
        }
        element.append(head);

        const slots = {};
        if (isSaturday) {
            const slot = this.createElement("div", "slot slot-saturday");
            slots.saturday = slot;
            element.append(slot);
        } else {
            ["morning", "evening"].forEach((kind) => {
                const slot = this.createElement("div", `slot slot-${kind}`);
                slots[kind] = slot;
                element.append(slot);
            });
        }

        const entry = { element, slots, signature: null };
        this.dayElements.set(day.date, entry);
        this.fillDay(entry, day);
        return element;
    }

    updateDay(day) {
        const entry = this.dayElements.get(day.date);
        if (!entry) {
            return;
        }
        if (this.fillDay(entry, day)) {
            // В ячейке поменялись люди — она мягко вспыхивает.
            this.restartAnimation(entry.element, "is-changed");
        }
    }

    // Возвращает true, если содержимое ячейки поменялось.
    fillDay(entry, day) {
        const signature = `${day.morning}|${day.evening}`;
        if (signature === entry.signature) {
            return false;
        }
        entry.signature = signature;

        if (entry.slots.saturday) {
            const slot = entry.slots.saturday;
            const time = this.createElement("div", "slot-time");
            time.append(this.createIcon("saturday"));
            time.append(document.createTextNode(SHIFTS.saturday.time));
            slot.replaceChildren(time, ...this.createPersons(day.evening));
        } else {
            entry.slots.morning.replaceChildren(...this.createPersons(day.morning));
            entry.slots.evening.replaceChildren(...this.createPersons(day.evening));
        }
        return true;
    }

    createPersons(value) {
        const names = this.splitPersons(value);
        if (!names.length) {
            return [this.createElement("span", "slot-empty", "—")];
        }
        return names.map((name) => {
            const person = this.createElement("div", "person");
            const [surname, ...rest] = name.split(/\s+/);
            person.append(this.createElement("span", "person-surname", surname));
            if (rest.length) {
                person.append(this.createElement("span", "person-name", rest.join(" ")));
            }
            return person;
        });
    }

    showScheduleMessage(title, details = "") {
        this.scheduleLayoutKey = null;
        this.dayElements.clear();
        const message = this.createElement("div", "schedule-message panel");
        message.append(this.createElement("div", "schedule-message-title", title));
        if (details) {
            message.append(this.createElement("div", "schedule-message-details", details));
        }
        this.scheduleElement.replaceChildren(message);
    }

    // --- Утилиты ---------------------------------------------------------

    // CSS-анимация по классу проигрывается заново только если класс снять,
    // дать браузеру это увидеть (reflow) и поставить снова.
    restartAnimation(element, className) {
        element.classList.remove(className);
        void element.offsetWidth;
        element.classList.add(className);
    }

    createElement(tag, className, text) {
        const element = document.createElement(tag);
        if (className) {
            element.className = className;
        }
        if (text !== undefined) {
            element.textContent = text;
        }
        return element;
    }

    createIcon(kind) {
        const icon = this.createElement("span", `shift-icon icon-${kind}`);
        icon.innerHTML = SHIFT_ICONS[kind];
        return icon;
    }

    toIsoDate(wallClock) {
        return wallClock.toISOString().slice(0, 10);
    }

    formatLongDate(wallClock) {
        const weekday = WEEKDAYS_LONG[wallClock.getUTCDay()];
        return `${weekday[0].toUpperCase()}${weekday.slice(1)}, ${wallClock.getUTCDate()} ${MONTHS_GENITIVE[wallClock.getUTCMonth()]}`;
    }

    formatRelativeDay(dateIso, todayIso) {
        const date = new Date(`${dateIso}T00:00:00Z`);
        const diff = Math.round((date - new Date(`${todayIso}T00:00:00Z`)) / DAY_MS);
        if (diff === 1) {
            return "Завтра";
        }
        const weekday = WEEKDAYS_LONG[date.getUTCDay()];
        return `${weekday[0].toUpperCase()}${weekday.slice(1)}, ${date.getUTCDate()} ${MONTHS_GENITIVE[date.getUTCMonth()]}`;
    }

    // Время обновления таблицы — тоже в поясе сервера.
    formatTimestamp(seconds) {
        if (!seconds) {
            return "--:--";
        }
        const wall = new Date(seconds * 1000 + this.serverOffsetMinutes * 60000);
        return `${String(wall.getUTCHours()).padStart(2, "0")}:${String(wall.getUTCMinutes()).padStart(2, "0")}`;
    }

    destroy() {
        clearTimeout(this.tickTimeout);
        clearInterval(this.dataUpdateInterval);
        clearInterval(this.versionCheckInterval);
        this.background.destroy();
        this.performanceGuard.destroy();
    }
}

document.addEventListener("DOMContentLoaded", () => {
    window.app = new DutyScheduleApp();
    window.addEventListener("beforeunload", () => {
        if (window.app) {
            window.app.destroy();
        }
    });
});
