/* =========================================
   DESI CLOUD - JAVASCRIPT
   ========================================= */

"use strict";


document.addEventListener("DOMContentLoaded", function () {

    // Smooth scrolling for internal links
    document.querySelectorAll('a[href^="#"]').forEach(function (link) {

        link.addEventListener("click", function (event) {

            const targetId = this.getAttribute("href");

            if (!targetId || targetId === "#") {
                return;
            }

            const target = document.querySelector(targetId);

            if (target) {

                event.preventDefault();

                target.scrollIntoView({
                    behavior: "smooth",
                    block: "start"
                });

            }

        });

    });


    // Prevent repeated clicks on Telegram buttons
    document.querySelectorAll('a[href*="t.me/"]').forEach(function (link) {

        link.addEventListener("click", function () {

            this.setAttribute("aria-busy", "true");

            window.setTimeout(() => {
                this.removeAttribute("aria-busy");
            }, 1500);

        });

    });


    // Add a subtle reveal effect to cards
    const cards = document.querySelectorAll(
        ".stat-card, .feature-card, .info-card"
    );

    if ("IntersectionObserver" in window) {

        const observer = new IntersectionObserver(
            function (entries) {

                entries.forEach(function (entry) {

                    if (entry.isIntersecting) {

                        entry.target.classList.add("is-visible");

                        observer.unobserve(entry.target);

                    }

                });

            },
            {
                threshold: 0.12
            }
        );


        cards.forEach(function (card) {
            observer.observe(card);
        });

    } else {

        cards.forEach(function (card) {
            card.classList.add("is-visible");
        });

    }


    // Copy file name if a copy button exists
    document.querySelectorAll("[data-copy]").forEach(function (button) {

        button.addEventListener("click", async function () {

            const value = this.getAttribute("data-copy");

            if (!value || !navigator.clipboard) {
                return;
            }

            try {

                await navigator.clipboard.writeText(value);

                const originalText = this.textContent;

                this.textContent = "Copied!";

                window.setTimeout(() => {
                    this.textContent = originalText;
                }, 1500);

            } catch (error) {

                console.error("Clipboard operation failed.");

            }

        });

    });

});
