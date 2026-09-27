// Фоновые сцены табло. Каждая — класс bg-<name> в backgrounds.css; layers —
// сколько пустых дочерних элементов ей нужно (круги у orbs).
const BACKGROUND_PRESETS = [
    { name: "orbs", layers: 5 },
    { name: "angled", layers: 0 },
    { name: "octagons", layers: 0 },
    { name: "mosaic", layers: 0 },
];

const BACKGROUND_INTERVAL_MS = 120000;
const BACKGROUND_FADE_MS = 6000;

// Две сцены: видимая и подменная. Новый пресет ставится в подменную, она
// проявляется, старая растворяется и после перехода очищается — скрытая
// сцена пуста и не тратит ни отрисовку, ни анимации.
//
// Для отладки: ?bg=angled или ?bg=angled,mosaic — ограничить набор,
// ?bgInterval=15 — сменять раз в 15 секунд, клавиша B — следующий пресет.
class BackgroundController {
    constructor(root) {
        this.root = root;
        this.layers = root ? Array.from(root.querySelectorAll(".background-scene")) : [];
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
        this.fill(this.layers[this.visibleIndex], this.pickNext());
        this.layers[this.visibleIndex].classList.add("is-active");

        if (this.layers.length > 1 && this.presets.length > 1) {
            this.switchInterval = setInterval(() => this.next(), this.intervalMs);
        }

        document.addEventListener("keydown", (event) => {
            if (["b", "B", "и", "И"].includes(event.key)) {
                this.next();
            }
        });
    }

    next() {
        if (this.layers.length < 2 || this.presets.length < 2) {
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
        layer.replaceChildren(...children);
    }

    clear(layer) {
        layer.className = "background-scene";
        delete layer.dataset.preset;
        layer.replaceChildren();
    }

    destroy() {
        clearInterval(this.switchInterval);
        clearTimeout(this.cleanupTimeout);
    }
}
