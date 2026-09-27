// Сторож плавности. Считает кадры через requestAnimationFrame окнами по
// 10 секунд; если два окна подряд вышли ниже 30 кадров/с — включает
// облегчённый режим: класс is-lite на <html> снимает размытие стекла и
// замораживает анимации фона (CSS в style.css и backgrounds.css). Обратно
// режим не выключается до перезагрузки — чтобы табло не «мигало» туда-сюда.
//
// ?lite=1 — включить облегчённый режим сразу, ?lite=0 — не следить вовсе.
// Последнее измеренное значение видно в <html data-fps="…">.
const FPS_WARMUP_MS = 15000;
const FPS_WINDOW_MS = 10000;
const FPS_THRESHOLD = 30;
const FPS_BAD_WINDOWS = 2;
// Пауза между кадрами длиннее секунды — вкладка была скрыта или браузер
// замер по своим причинам. Такое окно не считается.
const FPS_GAP_MS = 1000;

class PerformanceGuard {
    constructor(root) {
        this.root = root;
        this.isLite = false;
        this.badWindows = 0;
        this.warmupTimeout = null;
        this.frameRequest = null;

        const lite = new URLSearchParams(window.location.search).get("lite");
        this.forced = lite === "1";
        this.disabled = lite === "0";
    }

    start() {
        if (this.forced) {
            this.enableLite("включён параметром ?lite=1");
            return;
        }
        if (this.disabled) {
            return;
        }
        // Первые секунды после загрузки заняты шрифтами, каскадом ячеек и
        // проявлением страницы — их не меряем.
        this.warmupTimeout = setTimeout(() => this.measure(), FPS_WARMUP_MS);
    }

    measure() {
        let windowStart = null;
        let lastFrame = null;
        let frames = 0;

        const onFrame = (time) => {
            if (this.isLite) {
                return;
            }
            if (lastFrame !== null && time - lastFrame > FPS_GAP_MS) {
                windowStart = null;
            }
            lastFrame = time;

            if (windowStart === null) {
                windowStart = time;
                frames = 0;
            } else {
                frames += 1;
                const elapsed = time - windowStart;
                if (elapsed >= FPS_WINDOW_MS) {
                    this.report((frames * 1000) / elapsed);
                    windowStart = time;
                    frames = 0;
                }
            }
            this.frameRequest = requestAnimationFrame(onFrame);
        };
        this.frameRequest = requestAnimationFrame(onFrame);
    }

    report(fps) {
        this.root.dataset.fps = fps.toFixed(1);
        this.badWindows = fps < FPS_THRESHOLD ? this.badWindows + 1 : 0;
        if (this.badWindows >= FPS_BAD_WINDOWS) {
            this.enableLite(`${fps.toFixed(1)} кадров/с`);
        }
    }

    enableLite(reason) {
        this.isLite = true;
        this.root.classList.add("is-lite");
        console.warn(`Табло: облегчённый режим (${reason})`);
    }

    destroy() {
        clearTimeout(this.warmupTimeout);
        cancelAnimationFrame(this.frameRequest);
    }
}
