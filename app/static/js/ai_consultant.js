document.addEventListener("DOMContentLoaded", () => {
    document.querySelector("[data-period-select]")?.addEventListener("change", (event) => event.currentTarget.form?.submit());
    document.querySelector("[data-history-window-select]")?.addEventListener("change", (event) => event.currentTarget.form?.submit());
    const competitorModal = document.getElementById("competitorDiscoveryModal");
    if (competitorModal) {
        const results = competitorModal.querySelector("[data-competitor-results]");
        const searchButton = competitorModal.querySelector("[data-competitor-search]");
        const confirmButton = competitorModal.querySelector("[data-competitor-confirm]");
        const selectedCount = competitorModal.querySelector("[data-selected-count]");
        const maximum = Number(competitorModal.dataset.competitorMax || "5");
        const escapeHtml = (value) => { const element = document.createElement("span"); element.textContent = value ?? ""; return element.innerHTML; };
        const updateSelection = () => {
            const checked = results.querySelectorAll('input[name="place_id"]:checked');
            selectedCount.textContent = String(checked.length);
            confirmButton.disabled = checked.length === 0;
            results.querySelectorAll('input[name="place_id"]:not(:checked)').forEach((input) => { input.disabled = checked.length >= maximum; });
        };
        searchButton.addEventListener("click", async () => {
            const query = competitorModal.querySelector("#competitorQuery").value.trim();
            const radius = competitorModal.querySelector("#competitorRadius").value;
            results.innerHTML = '<div class="consultant-loading-card"><div class="consultant-skeleton consultant-skeleton-title"></div><div class="consultant-skeleton"></div><div class="consultant-skeleton consultant-skeleton-short"></div></div>';
            try {
                const url = new URL(competitorModal.dataset.competitorSearchUrl, window.location.origin); url.searchParams.set("q", query); url.searchParams.set("radius", radius);
                const response = await fetch(url); const data = await response.json();
                if (!response.ok) throw new Error(data.message || "Competitor search could not be completed.");
                const candidates = data.candidates || [];
                if (!candidates.length) { results.innerHTML = '<div class="consultant-empty-state"><h3>No matching businesses found</h3><p>Try a broader category or search radius.</p></div>'; return; }
                results.innerHTML = `<div class="competitor-candidates">${candidates.map((candidate) => `<label class="competitor-candidate"><input class="form-check-input" type="checkbox" name="place_id" value="${escapeHtml(candidate.google_place_id)}" ${candidate.tracked ? "disabled" : ""}><span><h3>${escapeHtml(candidate.name)}</h3><p>${candidate.rating ?? "—"} stars · ${candidate.user_rating_count} reviews · ${candidate.distance_meters} m</p><p>${escapeHtml(candidate.formatted_address)}</p><p>${escapeHtml((candidate.primary_type || "Business").replaceAll("_", " "))}${candidate.tracked ? " · Already tracked" : ""}</p>${candidate.google_maps_url ? `<a href="${escapeHtml(candidate.google_maps_url)}" target="_blank" rel="noopener noreferrer">View on Google Maps</a>` : ""}</span></label>`).join("")}</div>`;
                results.querySelectorAll('input[name="place_id"]').forEach((input) => input.addEventListener("change", updateSelection)); updateSelection();
            } catch (error) { results.innerHTML = `<div class="alert alert-warning" role="alert">${escapeHtml(error.message)}</div>`; }
        });
    }
    const dataNode = document.getElementById("googleReviewGrowthData");
    const canvas = document.getElementById("googleReviewGrowthChart");

    if (dataNode && canvas && window.Chart) {
        try {
            const trend = JSON.parse(dataNode.textContent || "{}");
            const points = trend.points || [];
            const styles = getComputedStyle(document.documentElement);
            const primary = styles.getPropertyValue("--rs-primary").trim() || "#2563eb";
            const border = styles.getPropertyValue("--rs-border").trim() || "#e5e7eb";
            const muted = styles.getPropertyValue("--rs-muted").trim() || "#64748b";
            const series = {
                review_count: { label: "Review volume", suffix: "", beginAtZero: true },
                rating: { label: "Average rating", suffix: "", beginAtZero: false },
                positive_percentage: { label: "Positive sentiment", suffix: "%", beginAtZero: true },
                response_rate: { label: "Response rate", suffix: "%", beginAtZero: true }
            };
            const chart = new Chart(canvas, {
                type: "line",
                data: {
                    labels: points.map((point) => point.label),
                    datasets: [{
                            label: series.review_count.label,
                            data: points.map((point) => point.review_count),
                            borderColor: primary,
                            backgroundColor: "rgba(37, 99, 235, .12)",
                            tension: .32,
                            fill: true,
                            pointRadius: 3
                    }]
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    interaction: { mode: "index", intersect: false },
                    plugins: {
                        legend: { display: false },
                        tooltip: { callbacks: { label: (context) => `${context.dataset.label}: ${context.parsed.y ?? "Unavailable"}` } }
                    },
                    scales: {
                        x: { ticks: { color: muted }, grid: { display: false } },
                        y: { beginAtZero: true, ticks: { precision: 0, color: muted }, grid: { color: border } }
                    }
                }
            });
            document.querySelectorAll("[data-chart-series]").forEach((button) => button.addEventListener("click", () => {
                const key = button.dataset.chartSeries;
                const definition = series[key];
                if (!definition) return;
                document.querySelectorAll("[data-chart-series]").forEach((item) => item.classList.toggle("active", item === button));
                chart.data.datasets[0].label = definition.label;
                chart.data.datasets[0].data = points.map((point) => point[key]);
                chart.options.scales.y.beginAtZero = definition.beginAtZero;
                chart.options.scales.y.suggestedMax = key === "rating" ? 5 : key.endsWith("percentage") || key === "response_rate" ? 100 : undefined;
                chart.update();
            }));
        } catch (error) {
            canvas.closest(".growth-chart-wrap")?.setAttribute("hidden", "hidden");
        }
    }

    const competitorHistoryNode = document.getElementById("competitorHistoryChartData");
    const competitorHistoryCanvas = document.getElementById("competitorHistoryChart");
    if (competitorHistoryNode && competitorHistoryCanvas && window.Chart) {
        try {
            const history = JSON.parse(competitorHistoryNode.textContent || "{}");
            const styles = getComputedStyle(document.documentElement);
            const primary = styles.getPropertyValue("--rs-primary").trim() || "#2563eb";
            const muted = styles.getPropertyValue("--rs-muted").trim() || "#64748b";
            const border = styles.getPropertyValue("--rs-border").trim() || "#e5e7eb";
            const palette = ["#64748b", "#8b5cf6", "#0ea5e9", "#f59e0b", "#14b8a6", "#ef4444"];
            const sourceFor = (mode) => mode === "rating" ? history.rating_series : mode === "review_count" ? history.review_count_series : history.rank_series;
            const datasetsFor = (mode) => {
                if (mode === "rank") return [{ label: "Your selected-competitor rank", data: history.rank_series.customer, borderColor: primary, backgroundColor: "rgba(37,99,235,.12)", borderWidth: 3, pointRadius: 3, spanGaps: false }];
                const source = sourceFor(mode);
                const datasets = (source.subjects || []).map((subject, index) => ({
                    label: subject.name,
                    data: subject[mode],
                    borderColor: subject.is_customer ? primary : palette[index % palette.length],
                    backgroundColor: "transparent",
                    borderWidth: subject.is_customer ? 3 : 1.8,
                    pointRadius: subject.is_customer ? 3 : 2,
                    borderDash: subject.is_customer ? [] : [5, 3],
                    spanGaps: false
                }));
                datasets.push({ label: "Selected competitor average", data: source.competitor_average, borderColor: "#94a3b8", backgroundColor: "transparent", borderWidth: 2, borderDash: [2, 4], pointRadius: 0, spanGaps: false });
                return datasets;
            };
            let mode = "rating";
            const chart = new Chart(competitorHistoryCanvas, {
                type: "line",
                data: { labels: history.rating_series.labels || [], datasets: datasetsFor(mode) },
                options: {
                    responsive: true, maintainAspectRatio: false,
                    interaction: { mode: "index", intersect: false },
                    plugins: { legend: { display: true, labels: { color: muted, usePointStyle: true, boxWidth: 8 } }, tooltip: { callbacks: { label: (context) => `${context.dataset.label}: ${context.parsed.y ?? "Unavailable"}` } } },
                    scales: { x: { ticks: { color: muted, maxTicksLimit: 10 }, grid: { display: false } }, y: { min: 0, max: 5, ticks: { color: muted }, grid: { color: border } } }
                }
            });
            document.querySelectorAll("[data-competitor-chart-mode]").forEach((button) => button.addEventListener("click", () => {
                mode = button.dataset.competitorChartMode;
                document.querySelectorAll("[data-competitor-chart-mode]").forEach((item) => { const active = item === button; item.classList.toggle("active", active); item.setAttribute("aria-selected", String(active)); });
                const source = sourceFor(mode);
                chart.data.labels = source.labels || [];
                chart.data.datasets = datasetsFor(mode);
                if (mode === "rating") { chart.options.scales.y.min = 0; chart.options.scales.y.max = 5; chart.options.scales.y.reverse = false; chart.options.scales.y.ticks.precision = 1; }
                else if (mode === "rank") { chart.options.scales.y.min = 1; chart.options.scales.y.max = Math.max(...(source.competitor_count || [1])); chart.options.scales.y.reverse = true; chart.options.scales.y.ticks.precision = 0; }
                else { chart.options.scales.y.min = 0; chart.options.scales.y.max = undefined; chart.options.scales.y.reverse = false; chart.options.scales.y.ticks.precision = 0; }
                competitorHistoryCanvas.setAttribute("aria-label", mode === "rating" ? "Historical rating comparison" : mode === "review_count" ? "Historical public review count comparison" : "Rank among currently selected competitors over time");
                chart.update();
            }));
        } catch (error) {
            competitorHistoryCanvas.closest(".competitor-history-chart-card")?.setAttribute("hidden", "hidden");
        }
    }

    const competitorJob = document.querySelector("[data-competitor-job]");
    if (competitorJob?.dataset.jobId) {
        const pollCompetitorJob = async () => {
            try {
                const response = await fetch(`/analysis-jobs/${competitorJob.dataset.jobId}/status`, { headers: { "Accept": "application/json" } });
                if (!response.ok) return;
                const job = await response.json();
                const label = competitorJob.querySelector("[data-competitor-job-label]");
                const outcome = job.result?.outcome;
                if (label) label.textContent = job.status === "pending" ? "Refresh queued" : "Refresh in progress";
                if (job.status === "completed") {
                    if (label) label.textContent = outcome === "partially_completed" ? "Competitors partially refreshed" : outcome === "failed" ? "Competitor refresh failed" : "Competitors refreshed successfully";
                    window.setTimeout(() => window.location.reload(), 700);
                    return;
                }
                if (job.status === "failed") { if (label) label.textContent = "Competitor refresh failed"; return; }
                if (job.status === "pending" || job.status === "processing") window.setTimeout(pollCompetitorJob, 3000);
            } catch (error) { window.setTimeout(pollCompetitorJob, 5000); }
        };
        window.setTimeout(pollCompetitorJob, 1200);
    }

    const consultantJob = document.querySelector("[data-consultant-job]");
    if (!consultantJob?.dataset.jobId) return;

    const pollConsultantJob = async () => {
        try {
            const response = await fetch(`/analysis-jobs/${consultantJob.dataset.jobId}/status`);
            if (!response.ok) return;
            const job = await response.json();
            const label = consultantJob.querySelector("[data-consultant-job-label]");
            if (label) {
                label.textContent = job.status === "pending"
                    ? "Queued"
                    : job.status.charAt(0).toUpperCase() + job.status.slice(1);
            }
            if (job.status === "completed") {
                window.location.reload();
                return;
            }
            if (job.status === "pending" || job.status === "processing") {
                window.setTimeout(pollConsultantJob, 3000);
            }
        } catch (error) {
            window.setTimeout(pollConsultantJob, 5000);
        }
    };

    window.setTimeout(pollConsultantJob, 1500);
});
