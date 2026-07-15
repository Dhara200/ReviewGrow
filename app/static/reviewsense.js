const SIDEBAR_STORAGE_KEY = "sidebar_collapsed";
const SIDEBAR_MOBILE_QUERY = "(max-width: 991.98px)";

function isMobileSidebar() {
    return window.matchMedia(SIDEBAR_MOBILE_QUERY).matches;
}

function getStoredSidebarCollapsed() {
    return localStorage.getItem(SIDEBAR_STORAGE_KEY) === "true";
}

function updateSidebarToggleState() {
    const isOpen = isMobileSidebar()
        ? document.body.classList.contains("sidebar-open")
        : !document.body.classList.contains("sidebar-collapsed");

    document.querySelectorAll("[data-sidebar-toggle]").forEach((button) => {
        button.setAttribute("aria-expanded", isOpen ? "true" : "false");
        button.setAttribute("title", isOpen ? "Close sidebar" : "Open sidebar");
    });
}

function syncSidebarFromStorage() {
    document.body.classList.remove("sidebar-open");

    if (isMobileSidebar()) {
        document.body.classList.remove("sidebar-collapsed");
    } else {
        document.body.classList.toggle("sidebar-collapsed", getStoredSidebarCollapsed());
    }

    updateSidebarToggleState();
}

function toggleSidebar(forceOpen) {
    if (isMobileSidebar()) {
        const shouldOpen = typeof forceOpen === "boolean"
            ? forceOpen
            : !document.body.classList.contains("sidebar-open");
        document.body.classList.toggle("sidebar-open", shouldOpen);
    } else {
        const shouldCollapse = typeof forceOpen === "boolean"
            ? !forceOpen
            : !document.body.classList.contains("sidebar-collapsed");
        document.body.classList.toggle("sidebar-collapsed", shouldCollapse);
        localStorage.setItem(SIDEBAR_STORAGE_KEY, shouldCollapse ? "true" : "false");
    }

    updateSidebarToggleState();
}

window.toggleSidebar = toggleSidebar;

document.addEventListener("DOMContentLoaded", () => {
    syncSidebarFromStorage();

    document.querySelectorAll("[data-sidebar-toggle]").forEach((button) => {
        button.addEventListener("click", () => toggleSidebar());
    });

    document.querySelectorAll("[data-sidebar-backdrop]").forEach((backdrop) => {
        backdrop.addEventListener("click", () => toggleSidebar(false));
    });

    document.querySelectorAll(".rs-sidebar a").forEach((link) => {
        link.addEventListener("click", () => {
            if (isMobileSidebar()) {
                toggleSidebar(false);
            }
        });
    });

    const sidebarMedia = window.matchMedia(SIDEBAR_MOBILE_QUERY);
    if (sidebarMedia.addEventListener) {
        sidebarMedia.addEventListener("change", syncSidebarFromStorage);
    } else {
        sidebarMedia.addListener(syncSidebarFromStorage);
    }

    const applyTheme = (theme) => {
        document.documentElement.setAttribute("data-theme", theme);
        document.documentElement.setAttribute("data-bs-theme", theme);
        localStorage.setItem("reviewsense-theme", theme);

        document.querySelectorAll("[data-theme-icon]").forEach((icon) => {
            icon.className = theme === "dark" ? "bi bi-sun" : "bi bi-moon-stars";
        });

        document.querySelectorAll("[data-theme-toggle]").forEach((button) => {
            button.setAttribute(
                "aria-label",
                theme === "dark" ? "Switch to light mode" : "Switch to dark mode"
            );
            button.setAttribute("title", theme === "dark" ? "Light mode" : "Dark mode");
        });
    };

    const currentTheme = document.documentElement.getAttribute("data-theme") || "light";
    applyTheme(currentTheme);

    document.querySelectorAll("[data-theme-toggle]").forEach((button) => {
        button.addEventListener("click", () => {
            const activeTheme = document.documentElement.getAttribute("data-theme") || "light";
            applyTheme(activeTheme === "dark" ? "light" : "dark");
        });
    });

    document.querySelectorAll("[data-contact-widget]").forEach((widget) => {
        const toggle = widget.querySelector("[data-contact-toggle]");
        const close = widget.querySelector("[data-contact-close]");
        const options = widget.querySelector("[data-contact-options]");

        if (!toggle || !close || !options) {
            return;
        }

        const setOpen = (isOpen) => {
            widget.classList.toggle("is-open", isOpen);
            toggle.setAttribute("aria-expanded", isOpen ? "true" : "false");
            options.setAttribute("aria-hidden", isOpen ? "false" : "true");
            (isOpen ? close : toggle).focus();
        };

        toggle.addEventListener("click", () => setOpen(true));
        close.addEventListener("click", () => setOpen(false));
    });

    const isExcelFile = (fileName) => /\.(xlsx|xls)$/i.test(fileName || "");

    document.querySelectorAll("[data-excel-upload]").forEach((input) => {
        input.addEventListener("change", () => {
            const hasInvalidFile = input.files.length > 0 && !isExcelFile(input.files[0].name);
            input.classList.toggle("is-invalid", hasInvalidFile);
            input.setCustomValidity(hasInvalidFile ? "Please upload only Excel files." : "");
        });
    });

    document.querySelectorAll("form").forEach((form) => {
        form.addEventListener("submit", (event) => {
            const excelInput = form.querySelector("[data-excel-upload]");

            if (excelInput) {
                const hasInvalidFile = excelInput.files.length === 0 || !isExcelFile(excelInput.files[0].name);
                excelInput.classList.toggle("is-invalid", hasInvalidFile);
                excelInput.setCustomValidity(hasInvalidFile ? "Please upload only Excel files." : "");

                if (hasInvalidFile) {
                    event.preventDefault();
                    excelInput.reportValidity();
                    return;
                }
            }

            const submitter = form.querySelector("button[type='submit']:focus") || form.querySelector("button[type='submit']");

            if (!submitter || submitter.dataset.loading === "false") {
                return;
            }

            submitter.dataset.originalText = submitter.innerHTML;
            submitter.disabled = true;
            submitter.innerHTML = "<span class='spinner-border spinner-border-sm' aria-hidden='true'></span><span>Working...</span>";
        });
    });
});
