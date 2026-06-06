class DutyScheduleApp {
    constructor() {
        this.currentTimeElement = document.getElementById("current-time");
        this.lastUpdatedElement = document.getElementById("last-updated-time");
        this.todayDateElement = document.getElementById("today-date");
        this.todayDutyContainer = document.getElementById("today-duty-container");
        this.scheduleContainer = document.getElementById("schedule-container");

        this.serverTimeBase = null;
        this.serverTimeCapturedAt = null;
        this.dataUpdateInterval = null;
        this.timeUpdateInterval = null;
        this.isFetching = false;

        this.init();
    }

    init() {
        this.fetchData();
        this.dataUpdateInterval = setInterval(() => this.fetchData(), 30000);
        this.updateLocalTime();
        this.timeUpdateInterval = setInterval(() => this.updateLocalTime(), 1000);
        document.body.style.opacity = "0";
        document.body.style.transition = "opacity 0.5s ease-in-out";
        setTimeout(() => {
            document.body.style.opacity = "1";
        }, 100);
    }

    async fetchData() {
        if (this.isFetching) {
            return;
        }

        this.isFetching = true;
        try {
            const timestamp = Date.now();
            const response = await fetch(`/api/data?_=${timestamp}`, {
                headers: {
                    "Cache-Control": "no-cache",
                    "Pragma": "no-cache",
                },
                cache: "no-store",
            });

            if (!response.ok) {
                throw new Error(`HTTP ${response.status}`);
            }

            const payload = await response.json();
            if (!payload.success) {
                throw new Error("Invalid response");
            }

            this.processData(payload);
        } catch (error) {
            console.error("Ошибка загрузки данных:", error);
            this.scheduleContainer.innerHTML = `
                <div class="error-container">
                    <div class="alert alert-danger glass-effect">
                        <h4>Ошибка при загрузке данных</h4>
                        <p>${error.message}</p>
                    </div>
                </div>
            `;
        } finally {
            this.isFetching = false;
        }
    }

    processData(payload) {
        const data = payload.data;
        this.serverTimeBase = new Date(data.server_time);
        this.serverTimeCapturedAt = Date.now();

        this.todayDateElement.textContent = this.formatDate(new Date(data.today));
        this.updateTodayDuty(data.today_duty, data.error);
        this.updateSchedule(data.weeks, data.error);

        const updateTime = data.last_updated
            ? new Date(data.last_updated * 1000).toLocaleTimeString("ru-RU", {
                hour: "2-digit",
                minute: "2-digit",
                second: "2-digit",
            })
            : "--:--:--";
        this.updateLastUpdatedTime(updateTime);
    }

    updateTodayDuty(todayDuty, error) {
        if (error) {
            this.todayDutyContainer.innerHTML = `
                <div class="error-duty">Ошибка загрузки</div>
            `;
            return;
        }

        if (!todayDuty) {
            this.todayDutyContainer.innerHTML = `
                <div class="no-duty">На сегодня дежурные не назначены</div>
            `;
            return;
        }

        const morning = todayDuty.morning || "";
        const evening = todayDuty.evening || "";
        let html = '<div class="today-duty">';

        if (morning && evening) {
            html += `
                <div class="duty-line">
                    <span class="duty-label">Утро:</span> ${morning}
                    <span class="duty-separator">|</span>
                    <span class="duty-label">Вечер:</span> ${evening}
                </div>
            `;
        } else if (morning) {
            html += `
                <div class="duty-line">
                    <span class="duty-label">Утро:</span> ${morning}
                </div>
            `;
        } else if (evening) {
            html += `
                <div class="duty-line">
                    <span class="duty-label">Вечер:</span> ${evening}
                </div>
            `;
        } else {
            html += '<div class="no-duty">На сегодня дежурные не назначены</div>';
        }

        html += "</div>";
        this.todayDutyContainer.innerHTML = html;
    }

    updateSchedule(weeks, error) {
        if (error) {
            this.scheduleContainer.innerHTML = `
                <div class="error-container">
                    <div class="alert alert-danger glass-effect">
                        <h4>Ошибка при загрузке данных</h4>
                        <p>${error}</p>
                    </div>
                </div>
            `;
            return;
        }

        if (!weeks || weeks.length === 0) {
            this.scheduleContainer.innerHTML = `
                <div class="text-center text-muted py-4">
                    Нет дежурств на ближайшие 2 недели
                </div>
            `;
            return;
        }

        const today = this.serverTimeBase
            ? this.serverTimeBase.toISOString().split("T")[0]
            : new Date().toISOString().split("T")[0];
        let html = "";

        weeks.forEach((week) => {
            html += '<div class="week-row glass-effect"><div class="row">';

            week.forEach((duty) => {
                const isToday = duty.date === today;
                const isWeekend = duty.weekday === "СБ" || duty.weekday === "ВС";
                const dayClasses = [
                    "col",
                    "day-cell",
                    isToday ? "today-highlight" : "",
                    duty.weekday === "СБ" ? "saturday" : "",
                    duty.weekday === "ВС" ? "sunday" : "",
                ].filter(Boolean).join(" ");

                html += `
                    <div class="${dayClasses}">
                        <div class="date-header">
                            <span class="weekday ${duty.weekday === "СБ" ? "saturday" : duty.weekday === "ВС" ? "sunday" : ""}">
                                ${duty.weekday}
                            </span>
                            ${duty.date_str}
                        </div>
                        <div class="duty-name">
                `;

                if (isWeekend) {
                    html += duty.evening || '<div class="no-duty-cell">—</div>';
                } else {
                    if (duty.morning) {
                        html += `<div class="morning-line">Утро: ${duty.morning}</div>`;
                    }
                    if (duty.evening) {
                        html += `<div class="evening-line">Вечер: ${duty.evening}</div>`;
                    }
                    if (!duty.morning && !duty.evening) {
                        html += '<div class="no-duty-cell">—</div>';
                    }
                }

                html += `
                        </div>
                    </div>
                `;
            });

            html += "</div></div>";
        });

        this.scheduleContainer.innerHTML = html;
    }

    updateLocalTime() {
        if (!this.currentTimeElement) {
            return;
        }

        let now = new Date();
        if (this.serverTimeBase && this.serverTimeCapturedAt) {
            const elapsed = Date.now() - this.serverTimeCapturedAt;
            now = new Date(this.serverTimeBase.getTime() + elapsed);
        }

        this.currentTimeElement.textContent = now.toLocaleTimeString("ru-RU", {
            hour: "2-digit",
            minute: "2-digit",
            second: "2-digit",
        });
    }

    updateLastUpdatedTime(value) {
        if (this.lastUpdatedElement) {
            this.lastUpdatedElement.textContent = value;
        }
    }

    formatDate(date) {
        return date.toLocaleDateString("ru-RU", {
            day: "2-digit",
            month: "2-digit",
            year: "numeric",
        });
    }

    destroy() {
        if (this.timeUpdateInterval) {
            clearInterval(this.timeUpdateInterval);
        }
        if (this.dataUpdateInterval) {
            clearInterval(this.dataUpdateInterval);
        }
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
