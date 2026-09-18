class SettingsPage {
    constructor() {
        this.messageElement = document.getElementById("message");
        this.authCard = document.getElementById("auth-card");
        this.authTitle = document.getElementById("auth-title");
        this.authHint = document.getElementById("auth-hint");
        this.authForm = document.getElementById("auth-form");
        this.authSubmit = document.getElementById("auth-submit");
        this.passwordInput = document.getElementById("password");
        this.passwordRepeatField = document.getElementById("password-repeat-field");
        this.passwordRepeatInput = document.getElementById("password-repeat");
        this.settingsForm = document.getElementById("settings-form");
        this.groupsContainer = document.getElementById("settings-groups");
        this.logoutButton = document.getElementById("logout-button");
        this.reloadButton = document.getElementById("reload-button");
        this.changePasswordButton = document.getElementById("change-password-button");
        this.filesSection = document.getElementById("files-section");
        this.vkUsersStatus = document.getElementById("vk-users-status");
        this.vkUsersList = document.getElementById("vk-users-list");
        this.vkUsersAddButton = document.getElementById("vk-users-add");
        this.vkUsersSaveButton = document.getElementById("vk-users-save");
        this.credentialsStatus = document.getElementById("credentials-status");
        this.credentialsForm = document.getElementById("credentials-form");
        this.credentialsFileInput = document.getElementById("credentials-file");
        this.credentialsUploadButton = document.getElementById("credentials-upload");
        // Статус ключа с сервера: есть ли файл, можно ли его писать.
        this.credentialsInfo = null;

        // "set" — пароль задаётся впервые, "change" — меняется, "login" — вход.
        this.authMode = "login";

        this.authForm.addEventListener("submit", (event) => this.onAuthSubmit(event));
        this.settingsForm.addEventListener("submit", (event) => this.onSave(event));
        this.logoutButton.addEventListener("click", () => this.onLogout());
        this.reloadButton.addEventListener("click", () => this.loadSettings());
        this.changePasswordButton.addEventListener("click", () => this.showAuth("change"));
        this.vkUsersAddButton.addEventListener("click", () => this.onVkUserAdd());
        this.vkUsersSaveButton.addEventListener("click", () => this.onVkUsersSave());
        this.credentialsForm.addEventListener("submit", (event) => this.onCredentialsUpload(event));

        this.init();
    }

    async init() {
        const session = await this.request("/api/settings/session");
        if (!session.ok) {
            return;
        }

        if (session.body.authenticated) {
            await this.loadSettings();
            return;
        }

        this.showAuth(session.body.password_set ? "login" : "set");
    }

    async request(url, options = {}) {
        try {
            const isMultipart = options.body instanceof FormData;
            const response = await fetch(url, {
                headers: isMultipart ? {} : { "Content-Type": "application/json" },
                cache: "no-store",
                ...options,
            });
            const body = await response.json().catch(() => ({}));

            if (!response.ok) {
                this.showMessage(body.error || `Ошибка запроса (HTTP ${response.status})`, "error");
            }
            return { ok: response.ok, status: response.status, body };
        } catch (error) {
            this.showMessage(`Сервер недоступен: ${error.message}`, "error");
            return { ok: false, status: 0, body: {} };
        }
    }

    showMessage(text, kind) {
        this.messageElement.textContent = text;
        this.messageElement.className = `settings-message is-${kind}`;
        this.messageElement.hidden = !text;
    }

    clearMessage() {
        this.messageElement.hidden = true;
        this.messageElement.textContent = "";
    }

    showAuth(mode) {
        this.authMode = mode;
        const isNewPassword = mode !== "login";

        this.authTitle.textContent = mode === "set" ? "Задайте пароль" : mode === "change" ? "Смена пароля" : "Вход";
        this.authHint.textContent = mode === "set"
            ? "Пароль спрашивается при каждом следующем заходе в настройки. Не короче 6 символов."
            : mode === "change"
                ? "Введите новый пароль. Не короче 6 символов."
                : "Введите пароль от настроек.";

        this.authSubmit.textContent = isNewPassword ? "Сохранить пароль" : "Войти";
        this.passwordRepeatField.hidden = !isNewPassword;
        this.passwordRepeatInput.required = isNewPassword;
        this.passwordInput.autocomplete = isNewPassword ? "new-password" : "current-password";
        this.passwordInput.value = "";
        this.passwordRepeatInput.value = "";

        this.authCard.hidden = false;
        this.settingsForm.hidden = true;
        this.filesSection.hidden = true;
        this.logoutButton.hidden = mode !== "change";
        this.passwordInput.focus();
    }

    async onAuthSubmit(event) {
        event.preventDefault();
        this.clearMessage();

        const password = this.passwordInput.value;
        if (this.authMode !== "login" && password !== this.passwordRepeatInput.value) {
            this.showMessage("Пароли не совпадают", "error");
            return;
        }

        const url = this.authMode === "login" ? "/api/settings/login" : "/api/settings/password";
        const result = await this.request(url, {
            method: "POST",
            body: JSON.stringify({ password }),
        });

        if (!result.ok) {
            this.passwordInput.value = "";
            this.passwordRepeatInput.value = "";
            return;
        }

        if (this.authMode === "change") {
            this.showMessage("Пароль изменён", "success");
        }
        await this.loadSettings();
    }

    async onLogout() {
        await this.request("/api/settings/logout", { method: "POST" });
        this.clearMessage();
        this.showAuth("login");
    }

    async loadSettings() {
        const result = await this.request("/api/settings");
        if (!result.ok) {
            if (result.status === 401) {
                this.showAuth("login");
            }
            return;
        }

        this.renderGroups(result.body.groups || []);
        this.authCard.hidden = true;
        this.settingsForm.hidden = false;
        this.filesSection.hidden = false;
        this.logoutButton.hidden = false;

        await Promise.all([this.loadVkUsers(), this.loadCredentials()]);
    }

    renderGroups(groups) {
        this.groupsContainer.innerHTML = "";

        groups.forEach((group) => {
            const card = document.createElement("section");
            card.className = "settings-card glass-effect";

            const title = document.createElement("h2");
            title.textContent = group.title;
            card.appendChild(title);

            group.fields.forEach((field) => card.appendChild(this.renderField(field)));
            this.groupsContainer.appendChild(card);
        });
    }

    renderField(field) {
        const wrapper = document.createElement("label");
        wrapper.className = field.kind === "bool" ? "settings-field settings-checkbox" : "settings-field";

        const input = this.createInput(field);
        input.id = `field-${field.key}`;
        input.dataset.key = field.key;
        input.dataset.kind = field.kind;

        const label = document.createElement("span");
        label.className = "settings-label";
        label.textContent = field.label;
        label.appendChild(this.renderSource(field));

        if (field.kind === "bool") {
            wrapper.appendChild(input);
            wrapper.appendChild(label);
            return wrapper;
        }

        wrapper.appendChild(label);
        wrapper.appendChild(input);

        if (field.hint) {
            const hint = document.createElement("span");
            hint.className = "settings-field-hint";
            hint.textContent = field.hint;
            wrapper.appendChild(hint);
        }

        return wrapper;
    }

    createInput(field) {
        if (field.kind === "bool") {
            const input = document.createElement("input");
            input.type = "checkbox";
            input.checked = Boolean(field.value);
            return input;
        }

        if (field.kind === "select") {
            const select = document.createElement("select");
            (field.choices || []).forEach((choice) => {
                const option = document.createElement("option");
                option.value = choice;
                option.textContent = choice;
                option.selected = choice === field.value;
                select.appendChild(option);
            });
            return select;
        }

        const input = document.createElement("input");
        // Секрет приходит плейсхолдером; отправив его обратно, оставим токен как был.
        input.type = field.kind === "secret" ? "password" : "text";
        input.value = field.value === null || field.value === undefined ? "" : String(field.value);
        input.autocomplete = "off";
        return input;
    }

    renderSource(field) {
        const badge = document.createElement("span");
        const stored = field.source === "settings";
        badge.className = stored ? "settings-source is-stored" : "settings-source";
        badge.textContent = stored ? "из настроек" : field.env;
        badge.title = stored
            ? "Значение хранится в настройках и переживёт пересоздание контейнера"
            : `Значение взято из переменной окружения ${field.env}`;
        return badge;
    }

    collectValues() {
        const values = {};
        this.groupsContainer.querySelectorAll("[data-key]").forEach((input) => {
            values[input.dataset.key] = input.dataset.kind === "bool" ? input.checked : input.value;
        });
        return values;
    }

    async onSave(event) {
        event.preventDefault();
        this.clearMessage();

        const submitButton = this.settingsForm.querySelector('button[type="submit"]');
        submitButton.disabled = true;
        try {
            const result = await this.request("/api/settings", {
                method: "POST",
                body: JSON.stringify({ values: this.collectValues() }),
            });

            if (result.status === 401) {
                this.showAuth("login");
                return;
            }
            if (!result.ok) {
                return;
            }

            this.renderGroups(result.body.groups || []);
            this.showMessage("Настройки сохранены и применены", "success");
        } finally {
            submitButton.disabled = false;
        }
    }

    // ------------------------------------------------------------------
    // Участники VK
    // ------------------------------------------------------------------

    renderFileStatus(element, file, presentText, missingText) {
        element.textContent = file.exists ? presentText : missingText;
        element.className = file.exists ? "settings-file-status is-present" : "settings-file-status";
        if (!file.writable) {
            // Старый способ монтирования :ro — правки некуда сохранить.
            element.textContent += ` Файл ${file.path} доступен только для чтения — изменения не сохранятся.`;
            element.className = "settings-file-status is-readonly";
        }
        element.title = file.path;
    }

    async loadVkUsers() {
        const result = await this.request("/api/settings/vk-users");
        if (!result.ok) {
            return;
        }

        const file = result.body.file || {};
        this.renderFileStatus(
            this.vkUsersStatus,
            file,
            `Файл ${file.path} загружен.`,
            "Файла ещё нет — он создастся при первом сохранении.",
        );
        this.vkUsersAddButton.disabled = !file.writable;
        this.vkUsersSaveButton.disabled = !file.writable;
        this.renderVkUsers(result.body.users || []);
    }

    renderVkUsers(users) {
        this.vkUsersList.innerHTML = "";

        if (users.length === 0) {
            const empty = document.createElement("p");
            empty.className = "settings-users-empty";
            empty.textContent = "Список пуст: дежурные пойдут в уведомления без упоминаний.";
            this.vkUsersList.appendChild(empty);
            return;
        }

        const header = document.createElement("div");
        header.className = "settings-users-row is-header";
        ["Имя как в таблице", "VK id", "Подпись (необязательно)", ""].forEach((text) => {
            const cell = document.createElement("span");
            cell.textContent = text;
            header.appendChild(cell);
        });
        this.vkUsersList.appendChild(header);

        users.forEach((user) => this.vkUsersList.appendChild(this.renderVkUserRow(user)));
    }

    renderVkUserRow(user) {
        const row = document.createElement("div");
        row.className = "settings-users-row";

        const fields = [
            ["name", "Фамилия Имя", user.name],
            ["id", "123456789", user.id],
            ["label", "Как обратиться", user.label],
        ];
        fields.forEach(([key, placeholder, value]) => {
            const input = document.createElement("input");
            input.type = "text";
            input.dataset.userField = key;
            input.placeholder = placeholder;
            input.value = value === null || value === undefined ? "" : String(value);
            input.autocomplete = "off";
            if (key === "id") {
                input.inputMode = "numeric";
            }
            row.appendChild(input);
        });

        const remove = document.createElement("button");
        remove.type = "button";
        remove.className = "settings-button ghost settings-users-remove";
        remove.textContent = "\u00d7";
        remove.title = "Убрать из списка";
        remove.addEventListener("click", () => {
            row.remove();
            if (!this.vkUsersList.querySelector(".settings-users-row:not(.is-header)")) {
                this.renderVkUsers([]);
            }
        });
        row.appendChild(remove);

        return row;
    }

    collectVkUsers() {
        return Array.from(this.vkUsersList.querySelectorAll(".settings-users-row:not(.is-header)")).map((row) => {
            const user = {};
            row.querySelectorAll("[data-user-field]").forEach((input) => {
                user[input.dataset.userField] = input.value;
            });
            return user;
        });
    }

    onVkUserAdd() {
        const users = this.collectVkUsers();
        users.push({ name: "", id: "", label: "" });
        this.renderVkUsers(users);

        const rows = this.vkUsersList.querySelectorAll(".settings-users-row:not(.is-header)");
        rows[rows.length - 1].querySelector("input").focus();
    }

    async onVkUsersSave() {
        this.clearMessage();
        this.vkUsersSaveButton.disabled = true;
        try {
            const result = await this.request("/api/settings/vk-users", {
                method: "POST",
                body: JSON.stringify({ users: this.collectVkUsers() }),
            });

            if (result.status === 401) {
                this.showAuth("login");
                return;
            }
            if (!result.ok) {
                return;
            }

            await this.loadVkUsers();
            this.showMessage("Список участников VK сохранён", "success");
        } finally {
            this.vkUsersSaveButton.disabled = false;
        }
    }

    // ------------------------------------------------------------------
    // Ключ Google
    // ------------------------------------------------------------------

    async loadCredentials() {
        const result = await this.request("/api/settings/credentials");
        if (!result.ok) {
            return;
        }

        const file = result.body.file || {};
        this.credentialsInfo = file;
        const present = file.error
            ? `Ключ есть, но ${file.error}. Загрузите файл заново.`
            : `Ключ загружен: ${file.client_email || "e-mail не указан"}.`;
        this.renderFileStatus(
            this.credentialsStatus,
            file,
            present,
            "Ключа нет — без него таблица не читается. Загрузите JSON-файл сервисного аккаунта.",
        );
        this.credentialsUploadButton.textContent = file.exists ? "Заменить ключ" : "Загрузить";
        this.credentialsUploadButton.disabled = !file.writable;
        this.credentialsFileInput.disabled = !file.writable;
        this.credentialsFileInput.value = "";
    }

    async onCredentialsUpload(event) {
        event.preventDefault();
        this.clearMessage();

        const file = this.credentialsFileInput.files[0];
        if (!file) {
            this.showMessage("Выберите файл ключа", "error");
            return;
        }

        const replace = Boolean(this.credentialsInfo && this.credentialsInfo.exists);
        if (replace && !window.confirm("Заменить текущий ключ? Старый доступ к таблице перестанет работать.")) {
            return;
        }

        const form = new FormData();
        form.append("file", file);
        form.append("replace", replace ? "1" : "0");

        this.credentialsUploadButton.disabled = true;
        try {
            const result = await this.request("/api/settings/credentials", { method: "POST", body: form });

            if (result.status === 401) {
                this.showAuth("login");
                return;
            }
            if (!result.ok) {
                return;
            }

            await this.loadCredentials();
            this.showMessage("Ключ загружен, таблица будет прочитана заново", "success");
        } finally {
            this.credentialsUploadButton.disabled = false;
        }
    }
}

document.addEventListener("DOMContentLoaded", () => {
    window.settingsPage = new SettingsPage();
});
