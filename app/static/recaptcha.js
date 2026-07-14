(() => {
    "use strict";

    const attachRecaptcha = (form) => {
        const siteKey = form.dataset.recaptchaSiteKey;
        const action = form.dataset.recaptchaAction;
        const tokenInput = form.querySelector("[data-recaptcha-token]");
        const errorElement = form.querySelector("[data-recaptcha-error]");
        let pending = false;

        if (!siteKey || !action || !tokenInput) {
            return;
        }

        form.addEventListener("submit", async (event) => {
            event.preventDefault();
            event.stopImmediatePropagation();

            if (pending || !form.checkValidity()) {
                form.reportValidity();
                return;
            }

            const submitButton = event.submitter || form.querySelector("button[type='submit'], input[type='submit']");
            pending = true;
            tokenInput.value = "";
            errorElement?.classList.add("d-none");

            if (submitButton) {
                submitButton.disabled = true;
            }

            try {
                if (!window.grecaptcha) {
                    throw new Error("reCAPTCHA unavailable");
                }

                await new Promise((resolve) => window.grecaptcha.ready(resolve));
                tokenInput.value = await window.grecaptcha.execute(siteKey, {action});
                form.submit();
            } catch (_error) {
                pending = false;
                tokenInput.value = "";
                if (submitButton) {
                    submitButton.disabled = false;
                }
                errorElement?.classList.remove("d-none");
            }
        }, true);
    };

    document.addEventListener("DOMContentLoaded", () => {
        document.querySelectorAll("[data-recaptcha-form]").forEach(attachRecaptcha);
    });
})();
