document.addEventListener("DOMContentLoaded", function () {

    const downloadForm = document.getElementById("downloadForm");

    if (downloadForm) {

        downloadForm.addEventListener("submit", function () {

            const button = downloadForm.querySelector("button");

            button.disabled = true;

            button.innerHTML = `
                <span>PREPARING DOWNLOAD...</span>
                <span>⌛</span>
            `;

        });

    }

});
