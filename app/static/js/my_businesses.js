(() => {
    "use strict";

    const modal = document.getElementById("deleteBusinessModal");
    if (!modal || modal.dataset.businessDeleteReady === "true") return;

    const nameTarget = document.getElementById("deleteBusinessName");
    const form = document.getElementById("deleteBusinessForm");
    if (!nameTarget || !form) return;

    modal.dataset.businessDeleteReady = "true";
    modal.addEventListener("show.bs.modal", (event) => {
        const trigger = event.relatedTarget;
        const businessId = trigger?.getAttribute("data-business-id") || "";
        const businessName = trigger?.getAttribute("data-business-name") || "this business";

        nameTarget.textContent = businessName;
        const urlTemplate = form.getAttribute("data-delete-url-template") || "";
        form.action = /^\d+$/.test(businessId) && urlTemplate
            ? urlTemplate.replace(/\/0$/, `/${businessId}`)
            : "";
    });
})();
