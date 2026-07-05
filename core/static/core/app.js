/* evently — the only script on any page. No inline JS anywhere: the CSP is
   script-src 'self' (§8 item 2), so behaviour hangs off data attributes instead
   of onclick= handlers. Everything is guarded — each block no-ops on pages
   without its elements, and every page works without JS (forms + PRG). */

document.addEventListener("DOMContentLoaded", () => {
  // Click-to-select for the copyable RSVP-link inputs (dashboard).
  document.querySelectorAll("input[data-select-all]").forEach((el) => {
    el.addEventListener("click", () => el.select());
  });

  // Confirmation prompts: <button data-confirm="..."> or <form data-confirm="...">.
  document.querySelectorAll("button[data-confirm]").forEach((el) => {
    el.addEventListener("click", (e) => {
      if (!confirm(el.dataset.confirm)) e.preventDefault();
    });
  });
  document.querySelectorAll("form[data-confirm]").forEach((el) => {
    el.addEventListener("submit", (e) => {
      if (!confirm(el.dataset.confirm)) e.preventDefault();
    });
  });

  // --- Send queue (§6) --------------------------------------------------- //
  const sharedForm = document.getElementById("shared-form");

  // WhatsApp deep link: let it open in the new tab, then optimistically mark
  // this copy as shared (the guest's link click is the real signal).
  const waBtn = document.getElementById("wa-btn");
  if (waBtn && sharedForm) {
    waBtn.addEventListener("click", () => {
      setTimeout(() => sharedForm.submit(), 300);
    });
  }

  // Messenger: the OS share sheet via navigator.share; desktop browsers
  // without it degrade to copy-for-messenger (§7).
  const shareBtn = document.getElementById("share-btn");
  if (shareBtn && sharedForm) {
    if (!navigator.share) shareBtn.textContent = "Copy for Messenger";
    shareBtn.addEventListener("click", async () => {
      const { text, url } = shareBtn.dataset;
      try {
        if (navigator.share) await navigator.share({ text, url });
        else await navigator.clipboard.writeText(text + "\n" + url);
        sharedForm.submit();
      } catch (err) {
        /* share sheet dismissed — stay put */
      }
    });
  }

  const copyBtn = document.getElementById("copy-btn");
  if (copyBtn) {
    copyBtn.addEventListener("click", async () => {
      await navigator.clipboard.writeText(copyBtn.dataset.text);
      copyBtn.textContent = "Copied ✓";
    });
  }

  // --- Guest RSVP page: channel-change form (§2.5) ----------------------- //
  // Messenger needs no address — hide the value field when it's picked.
  const kindSel = document.getElementById("channel-kind");
  const valField = document.getElementById("channel-value-field");
  if (kindSel && valField) {
    const valInput = valField.querySelector("input");
    kindSel.addEventListener("change", () => {
      valField.hidden = kindSel.value === "messenger";
      valInput.placeholder =
        kindSel.value === "email" ? "e.g. you@example.com" : "e.g. 021 555 0123";
    });
  }

  // --- Guest RSVP page: "what is this?" info popup ----------------------- //
  // Auto-opens once per device (first ever visit), then only via the header
  // "What's this?" link. localStorage isn't restricted by the CSP.
  const infoModal = document.getElementById("info-modal");
  if (infoModal && typeof infoModal.showModal === "function") {
    const SEEN_KEY = "evently.infoSeen";
    const open = () => infoModal.showModal();
    const close = () => infoModal.close();

    document.querySelectorAll("[data-info-open]").forEach((el) =>
      el.addEventListener("click", open)
    );
    infoModal
      .querySelectorAll("[data-info-close]")
      .forEach((el) => el.addEventListener("click", close));
    // Click on the backdrop (the <dialog> itself, outside .info-body) closes it.
    infoModal.addEventListener("click", (e) => {
      if (e.target === infoModal) close();
    });

    let seen = null;
    try {
      seen = localStorage.getItem(SEEN_KEY);
      if (!seen) localStorage.setItem(SEEN_KEY, "1");
    } catch (err) {
      /* storage blocked (private mode) — just don't auto-open */
    }
    if (!seen) open();
  }

  // --- PWA: register the organizer service worker ------------------------ //
  const swUrl = document.body.dataset.sw;
  if (swUrl && "serviceWorker" in navigator) {
    navigator.serviceWorker.register(swUrl).catch(() => {});
  }
});
