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

  // --- Manual sends (§6) -------------------------------------------------- //
  // Sharing NEVER marks a message sent. Opening a share sheet is not evidence a
  // message was sent — the organizer presses "Sent it" themselves (§7.3). These
  // handlers only hand the text to WhatsApp / the OS share sheet / the clipboard.
  //
  // Both the walkthrough (#share-btn / #copy-btn, one card) and the Messages list
  // (.share-btn / .copy-btn, every row) use these, hence id-or-class.
  document.querySelectorAll("#share-btn, .share-btn").forEach((btn) => {
    if (!navigator.share) btn.textContent = "Copy for Messenger";
    btn.addEventListener("click", async () => {
      const { text, url } = btn.dataset;
      try {
        if (navigator.share) await navigator.share({ text, url });
        else await navigator.clipboard.writeText(text + "\n" + url);
      } catch (err) {
        /* share sheet dismissed — nothing to undo, nothing was recorded */
      }
    });
  });

  document.querySelectorAll("#copy-btn, .copy-btn").forEach((btn) => {
    btn.addEventListener("click", async () => {
      await navigator.clipboard.writeText(btn.dataset.text);
      const original = btn.textContent;
      btn.textContent = "Copied ✓";
      setTimeout(() => (btn.textContent = original), 2000);
    });
  });

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

  // Full guest list — opened from the "See all →" button beside the bubbles.
  wireModal(document.getElementById("guests-modal"), "guests");

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

  // --- PWA: auto-refresh when a new version is deployed (§7) -------------- //
  // A long-lived organizer tab (especially the installed PWA, which rarely gets a hard
  // reload) can go stale after a deploy. Each org page is stamped with the image's build
  // id; we periodically re-check the live build id and, when it differs, reload to pick up
  // the new version. Guarded so we never reload out from under someone mid-edit.
  const loadedBuild = document.body.dataset.build;
  const versionUrl = document.body.dataset.versionUrl;
  if (loadedBuild && versionUrl) {
    const isEditing = () => {
      const el = document.activeElement;
      if (el && /^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName)) return true;
      // A form the user has started filling in — don't clobber unsaved work.
      return Array.from(document.forms).some((f) =>
        Array.from(f.elements).some(
          (e) => e.type !== "hidden" && e.value && e.value !== e.defaultValue
        )
      );
    };
    const check = async () => {
      if (document.visibilityState !== "visible" || isEditing()) return;
      try {
        const res = await fetch(versionUrl, { cache: "no-store" });
        if (!res.ok) return;
        const { build } = await res.json();
        if (build && build !== loadedBuild) location.reload();
      } catch {
        /* offline / transient — try again next tick */
      }
    };
    // Check when the user returns to the tab (the common case for the installed PWA)…
    document.addEventListener("visibilitychange", check);
    // …and on a slow poll to catch tabs left open in the foreground.
    setInterval(check, 5 * 60 * 1000);
  }
});
