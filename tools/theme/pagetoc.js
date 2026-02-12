// Page Table of Contents for mdBook
// Based on https://github.com/JorelAli/mdBook-pagetoc
// SPDX-License-Identifier: Apache-2.0

// Update active TOC item based on scroll position
var updateActiveTocItem = function() {
    var id = null;
    var headers = document.getElementsByClassName("header");
    var offset = 100;

    Array.prototype.forEach.call(headers, function(el) {
        if (window.pageYOffset + offset >= el.offsetTop) {
            id = el;
        }
    });

    if (!id) return;

    var pagetoc = document.getElementsByClassName("pagetoc")[0];
    if (!pagetoc) return;

    Array.prototype.forEach.call(pagetoc.getElementsByTagName("a"), function(el) {
        el.classList.remove("active");
        if (id.href === el.href) {
            el.classList.add("active");
        }
    });
};

// Build the table of contents from page headers
var buildPageToc = function() {
    var pagetoc = document.getElementsByClassName("pagetoc")[0];
    if (!pagetoc) return;

    var headers = document.querySelectorAll("main .header");
    if (headers.length <= 1) {
        // Hide TOC if only one or no headers
        var sidetoc = document.getElementsByClassName("sidetoc")[0];
        if (sidetoc) sidetoc.style.display = "none";
        return;
    }

    // Add title
    var title = document.createElement("div");
    title.className = "pagetoc-title";
    title.textContent = "On this page";
    pagetoc.appendChild(title);

    // Add links for each header
    Array.prototype.forEach.call(headers, function(header) {
        var link = document.createElement("a");
        var headerTag = header.parentElement.tagName;
        var level = parseInt(headerTag.charAt(1)) || 2;

        link.href = header.href;
        link.textContent = header.textContent || header.parentElement.textContent.replace(/^#\s*/, '').trim();
        link.className = "pagetoc-" + headerTag.toLowerCase();

        pagetoc.appendChild(link);
    });

    // Set up scroll listener
    window.addEventListener("scroll", updateActiveTocItem);
    updateActiveTocItem();
};

// Initialize on page load
window.addEventListener("load", buildPageToc);
