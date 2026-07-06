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

  // --- Add-guests picker: whole-household selection ---------------------- //
  // Ticking a household covers every member with the one shared link, so its
  // members' individual boxes are checked-off and disabled (no double invite).
  document.querySelectorAll("input[data-household]").forEach((hh) => {
    const members = document.querySelectorAll(
      `input[data-in-household="${hh.dataset.household}"]`
    );
    const sync = () => {
      members.forEach((m) => {
        if (hh.checked) m.checked = false;
        m.disabled = hh.checked;
      });
    };
    hh.addEventListener("change", sync);
    sync();
  });

  // --- Guest RSVP page: channel-change form (§2.5) ----------------------- //
  // Messenger needs no address — hide the value field when it's picked.
  const kindSel = document.getElementById("channel-kind");
  const valField = document.getElementById("channel-value-field");
  if (kindSel && valField) {
    const valInput = valField.querySelector("input");
    const syncKind = () => {
      valField.hidden = kindSel.value === "messenger";
      valInput.placeholder =
        kindSel.value === "email" ? "e.g. you@example.com" : "e.g. 021 555 0123";
    };
    kindSel.addEventListener("change", syncKind);
    syncKind(); // reflect the pre-selected kind on load (e.g. reopened after an error)
  }

  // --- Guest RSVP page: <dialog> modals (info popup + feedback) ---------- //
  // Shared wiring: open via [data-<name>-open] anywhere, close via
  // [data-<name>-close] inside, and a backdrop click. No-ops if the browser
  // lacks <dialog>.showModal (very old) — the trigger stays inert, page works.
  const wireModal = (modal, name) => {
    if (!modal || typeof modal.showModal !== "function") return null;
    const open = () => modal.showModal();
    const close = () => modal.close();
    document.querySelectorAll(`[data-${name}-open]`).forEach((el) =>
      el.addEventListener("click", open)
    );
    modal
      .querySelectorAll(`[data-${name}-close]`)
      .forEach((el) => el.addEventListener("click", close));
    modal.addEventListener("click", (e) => {
      if (e.target === modal) close(); // click outside .info-body = backdrop
    });
    return { open, close };
  };

  // "What's this?" — auto-opens once per device (first ever visit), then only
  // via the header link. localStorage isn't restricted by the CSP.
  const info = wireModal(document.getElementById("info-modal"), "info");
  if (info) {
    const SEEN_KEY = "evently.infoSeen";
    let seen = null;
    try {
      seen = localStorage.getItem(SEEN_KEY);
      if (!seen) localStorage.setItem(SEEN_KEY, "1");
    } catch (err) {
      /* storage blocked (private mode) — just don't auto-open */
    }
    if (!seen) info.open();
  }

  // Feedback — opened from the footer link; auto-opens on a rejected submit
  // (blank message) so the guest can retry without hunting for the link again.
  const feedbackModal = document.getElementById("feedback-modal");
  const feedback = wireModal(feedbackModal, "feedback");
  if (feedback && feedbackModal.hasAttribute("data-open-on-load")) feedback.open();

  // --- Repeatable form rows (contact channels, household members) -------- //
  // A "remove" flags the row deleted (hidden input → "1") and hides it, but never
  // pulls it from the DOM — so the parallel-array field indices, and the
  // preferred/primary radio values that point at them, stay stable. New rows are
  // cloned from a <template> and appended. Degrades cleanly: the server renders the
  // rows and reads the same fields, so the form works with JS off.
  document.querySelectorAll("[data-add-row]").forEach((addBtn) => {
    const container = addBtn.parentElement.querySelector("[data-rows]");
    if (!container) return;
    const template = container.querySelector("template[data-row-template]");
    if (!template) return;

    const liveRowCount = () => container.querySelectorAll("[data-delete]").length;

    const wireRemove = (row) => {
      const btn = row.querySelector("[data-remove-row]");
      if (!btn) return;
      btn.addEventListener("click", () => {
        const del = row.querySelector("[data-delete]");
        if (del) del.value = "1";
        const radio = row.querySelector('input[type="radio"]');
        if (radio) radio.checked = false;
        row.hidden = true;
      });
    };

    container
      .querySelectorAll("[data-delete]")
      .forEach((del) => wireRemove(del.parentElement));

    addBtn.addEventListener("click", () => {
      const row = template.content.firstElementChild.cloneNode(true);
      const radio = row.querySelector('input[type="radio"]');
      if (radio) radio.value = String(liveRowCount()); // stable index, matches DOM order
      container.insertBefore(row, template);
      wireRemove(row);
    });
  });

  // --- PWA: register the organizer service worker ------------------------ //
  const swUrl = document.body.dataset.sw;
  if (swUrl && "serviceWorker" in navigator) {
    navigator.serviceWorker.register(swUrl).catch(() => {});
  }
});
