// Фоновые сцены табло. Каждая — класс bg-<name> в backgrounds.css; layers —
// сколько пустых дочерних элементов ей нужно (круги у orbs), setup — чем
// заполнить их CSS-переменные, если параметры случайные.
const BACKGROUND_PRESETS = [
    { name: "orbs", layers: 5 },
    { name: "angled", layers: 0 },
    { name: "octagons", layers: 0 },
    { name: "mosaic", layers: 0 },
    { name: "aurora", layers: 4 },
    { name: "waves", layers: 3 },
    { name: "stripes", layers: 0 },
    { name: "bokeh", layers: 22, setup: setupBokeh },
];

// Части суток для цвета подложки: [название, час начала]. Ночь идёт
// через полночь, поэтому встречается дважды.
const DAYPARTS = [
    ["night", 0],
    ["morning", 8],
    ["day", 12],
    ["evening", 16],
    ["night", 20],
];
const DAYPART_ORDER = ["night", "morning", "day", "evening"];

function randomBetween(min, max) {
    return min + Math.random() * (max - min);
}

function setupBokeh(span) {
    const duration = randomBetween(30, 55);
    span.style.setProperty("--x", `${randomBetween(-2, 98).toFixed(1)}%`);
    span.style.setProperty("--size", `${randomBetween(5, 16).toFixed(1)}vh`);
    span.style.setProperty("--o", randomBetween(0.12, 0.26).toFixed(2));
    span.style.setProperty("--dx", `${randomBetween(-8, 8).toFixed(1)}vw`);
    span.style.setProperty("--dur", `${duration.toFixed(1)}s`);
    span.style.setProperty("--delay", `${(-Math.random() * duration).toFixed(1)}s`);
}

const BACKGROUND_INTERVAL_MS = 120000;
const BACKGROUND_FADE_MS = 6000;
// Цвет подложки перетекает в цвет новой части суток за 10 минут.
const SKY_FADE_MS = 10 * 60 * 1000;

// Две сцены: видимая и подменная. Новый пресет ставится в подменную, она
// проявляется, старая растворяется и после перехода очищается — скрытая
// сцена пуста и не тратит ни отрисовку, ни анимации.
//
// Для отладки: ?bg=angled или ?bg=angled,mosaic — ограничить набор,
// ?bgInterval=15 — сменять раз в 15 секунд, клавиша B — следующий пресет,
// ?daypart=evening — зафиксировать цвет части суток, клавиша D — следующая
// часть суток, ?skyFade=5 — перетекать за 5 секунд вместо 10 минут.
class BackgroundController {
    constructor(root) {
        this.root = root;
        this.layers = root ? Array.from(root.querySelectorAll(".background-scene")) : [];
        this.skyLayers = root ? Array.from(root.querySelectorAll(".background-sky > div")) : [];
        this.skyTimeout = null;
        this.skyCleanup = null;
        this.visibleIndex = 0;
        this.current = null;
        this.switchInterval = null;
        this.cleanupTimeout = null;

        const params = new URLSearchParams(window.location.search);
        const requested = (params.get("bg") || "")
            .split(",")
            .map((name) => name.trim())
            .filter(Boolean);
        const presets = BACKGROUND_PRESETS.filter((preset) => requested.includes(preset.name));
        this.presets = presets.length ? presets : BACKGROUND_PRESETS;

        const daypart = params.get("daypart");
        this.forcedDaypart = DAYPART_ORDER.includes(daypart) ? daypart : null;
        this.daypart = null;
        const skyFadeSeconds = Number(params.get("skyFade"));
        this.skyFadeMs = skyFadeSeconds > 0 ? skyFadeSeconds * 1000 : SKY_FADE_MS;

        const intervalSeconds = Number(params.get("bgInterval"));
        this.intervalMs = intervalSeconds > 0
            ? Math.max(intervalSeconds * 1000, BACKGROUND_FADE_MS + 1000)
            : BACKGROUND_INTERVAL_MS;
    }

    start() {
        if (!this.layers.length) {
            return;
        }
        this.root.style.setProperty("--bg-fade", `${BACKGROUND_FADE_MS}ms`);
        this.root.style.setProperty("--sky-fade", `${this.skyFadeMs}ms`);
        this.fill(this.layers[this.visibleIndex], this.pickNext());
        this.layers[this.visibleIndex].classList.add("is-active");

        if (this.layers.length > 1 && this.presets.length > 1) {
            this.switchInterval = setInterval(() => this.next(), this.intervalMs);
        }

        document.addEventListener("keydown", (event) => {
            if (["b", "B", "и", "И"].includes(event.key)) {
                this.next();
            } else if (["d", "D", "в", "В"].includes(event.key)) {
                const index = DAYPART_ORDER.indexOf(this.daypart);
                this.forcedDaypart = DAYPART_ORDER[(index + 1) % DAYPART_ORDER.length];
                this.showSky(this.forcedDaypart, false);
            }
        });
    }

    next() {
        if (this.layers.length < 2 || this.presets.length < 2) {
            return;
        }
        // Скрытая вкладка кадров не рисует: переход застрял бы на полпути.
        if (document.hidden) {
            return;
        }
        // Новый переход до конца предыдущего: уходящая сцена переиспользуется
        // и просто проявляется заново с того места, где была.
        clearTimeout(this.cleanupTimeout);

        const outgoing = this.layers[this.visibleIndex];
        this.visibleIndex = 1 - this.visibleIndex;
        const incoming = this.layers[this.visibleIndex];

        this.fill(incoming, this.pickNext());
        // Стили новой сцены должны примениться до смены прозрачности,
        // иначе браузер склеит оба шага и перехода не будет.
        void incoming.offsetWidth;
        incoming.classList.add("is-active");
        outgoing.classList.remove("is-active");

        this.cleanupTimeout = setTimeout(() => this.clear(outgoing), BACKGROUND_FADE_MS + 200);
    }

    // Случайный пресет, но не тот же, что сейчас на экране.
    pickNext() {
        const pool = this.presets.filter((preset) => preset !== this.current);
        const options = pool.length ? pool : this.presets;
        this.current = options[Math.floor(Math.random() * options.length)];
        return this.current;
    }

    fill(layer, preset) {
        const isActive = layer.classList.contains("is-active");
        layer.className = `background-scene bg-${preset.name}`;
        if (isActive) {
            layer.classList.add("is-active");
        }
        layer.dataset.preset = preset.name;
        const children = Array.from({ length: preset.layers }, () => document.createElement("span"));
        if (preset.setup) {
            children.forEach((child, index) => preset.setup(child, index));
        }
        layer.replaceChildren(...children);
    }

    // Вызывается из часов табло раз в минуту — по серверному времени, а не
    // браузерному. Первый вызов приходится на загрузку: цвет ставится сразу,
    // без перехода.
    setDaypart(hour) {
        let part = DAYPARTS[0][0];
        DAYPARTS.forEach(([name, startHour]) => {
            if (hour >= startHour) {
                part = name;
            }
        });
        part = this.forcedDaypart || part;
        if (part !== this.daypart) {
            this.showSky(part, this.daypart === null);
        }
    }

    // Новый слой неба проявляется поверх текущего; текущий остаётся под ним
    // непрозрачным (is-previous) до конца перехода и только потом гаснет.
    showSky(part, instant) {
        const incoming = this.skyLayers.find((layer) => layer.classList.contains(`sky-${part}`));
        if (!incoming) {
            return;
        }
        const outgoing = this.skyLayers.find((layer) => layer.classList.contains("is-current"));
        this.daypart = part;
        if (this.root) {
            this.root.dataset.daypart = part;
        }

        clearTimeout(this.skyTimeout);
        if (this.skyCleanup) {
            this.skyCleanup();
            this.skyCleanup = null;
        }
        this.skyLayers.forEach((layer) => {
            if (layer !== incoming && layer !== outgoing) {
                layer.classList.remove("is-current", "is-previous");
            }
        });
        if (outgoing === incoming) {
            return;
        }
        if (outgoing) {
            outgoing.classList.remove("is-current");
            outgoing.classList.add("is-previous");
        }
        incoming.classList.remove("is-previous");

        if (instant) {
            incoming.style.transition = "none";
            incoming.classList.add("is-current");
            void incoming.offsetWidth;
            incoming.style.transition = "";
            if (outgoing) {
                outgoing.classList.remove("is-previous");
            }
            return;
        }

        void incoming.offsetWidth;
        incoming.classList.add("is-current");

        // Старый слой гасим, только когда новый действительно проявился: пока
        // вкладка не рисует кадры, переход стоит, и таймер снял бы подложку
        // раньше времени. Таймер с запасом — на случай, если событие потеряется.
        const release = () => {
            clearTimeout(this.skyTimeout);
            incoming.removeEventListener("transitionend", onEnd);
            if (outgoing && !outgoing.classList.contains("is-current")) {
                outgoing.classList.remove("is-previous");
            }
        };
        const onEnd = (event) => {
            if (event.target === incoming && event.propertyName === "opacity") {
                release();
            }
        };
        incoming.addEventListener("transitionend", onEnd);
        this.skyCleanup = () => incoming.removeEventListener("transitionend", onEnd);
        this.skyTimeout = setTimeout(release, this.skyFadeMs * 2 + 1000);
    }

    clear(layer) {
        layer.className = "background-scene";
        delete layer.dataset.preset;
        layer.replaceChildren();
    }

    destroy() {
        clearInterval(this.switchInterval);
        clearTimeout(this.cleanupTimeout);
        clearTimeout(this.skyTimeout);
    }
}
