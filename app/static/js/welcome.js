(function () {
    "use strict";

    const root = document.documentElement;
    const welcome = document.querySelector("[data-reviewgrow-welcome]");

    if (!root.classList.contains("reviewgrow-welcome-pending") || !welcome) {
        return;
    }

    const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    const displayDuration = reducedMotion ? 120 : 2000;
    const fadeDuration = reducedMotion ? 80 : 500;

    window.setTimeout(() => {
        welcome.classList.add("is-leaving");

        window.setTimeout(() => {
            welcome.remove();
            root.classList.remove("reviewgrow-welcome-pending");
        }, fadeDuration);
    }, displayDuration);
})();
