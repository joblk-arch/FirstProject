// Remembers per-browser expansion preferences for the fixed dashboard sections.
//
// - One versioned localStorage key; stores booleans only (section id -> open).
// - No private/job/user data is ever written to storage.
// - All storage access is wrapped in try/catch so Safari private mode,
//   disabled storage, or invalid JSON degrade gracefully to HTML defaults.
// - Initialized exactly once per page load. Data polling only replaces the
//   inner renderer roots (which live INSIDE the <details> wrappers), so the
//   open/closed state of each section survives repeated renders.
(function () {
  'use strict';

  var STORAGE_KEY = 'firstproject.dashboard.expansion.v1';

  // Fixed sections whose open/closed state is remembered per browser.
  var SECTIONS = [
    'reliability-section',
    'cluster-health-panel',
    'ups-panel',
    'work-distribution-panel',
    'token-ledger-panel',
    'start-build-panel'
  ];

  // HTML defaults: only "Start build" ships open; all others ship collapsed.
  var DEFAULTS = { 'start-build-panel': true };

  function sectionEl(id) {
    var el = document.getElementById(id);
    return el && el.tagName === 'DETAILS' ? el : null;
  }

  function readPrefs() {
    try {
      var raw = window.localStorage.getItem(STORAGE_KEY);
      if (raw == null) return null;
      var parsed = JSON.parse(raw);
      if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return null;
      var prefs = {};
      for (var i = 0; i < SECTIONS.length; i++) {
        var value = parsed[SECTIONS[i]];
        // Missing or non-boolean individual values fall back to the default.
        prefs[SECTIONS[i]] = typeof value === 'boolean' ? value : DEFAULTS[SECTIONS[i]] === true;
      }
      return prefs;
    } catch (err) {
      return null; // invalid JSON, storage disabled, private browsing
    }
  }

  function writePrefs() {
    var payload = {};
    for (var i = 0; i < SECTIONS.length; i++) {
      var el = sectionEl(SECTIONS[i]);
      if (el) payload[SECTIONS[i]] = el.open === true;
    }
    try {
      window.localStorage.setItem(STORAGE_KEY, JSON.stringify(payload));
    } catch (err) {
      /* Storage unavailable (e.g. Safari private mode): preferences are
         simply not persisted; the page keeps working. */
    }
  }

  function init() {
    var prefs = readPrefs();
    if (prefs) {
      for (var i = 0; i < SECTIONS.length; i++) {
        var el = sectionEl(SECTIONS[i]);
        if (el && el.open !== prefs[SECTIONS[i]]) el.open = prefs[SECTIONS[i]];
      }
    }
    // Persist only when the user actually toggles a section (native
    // <details> fires "toggle"); never on data refreshes.
    for (var j = 0; j < SECTIONS.length; j++) {
      var section = sectionEl(SECTIONS[j]);
      if (section) section.addEventListener('toggle', writePrefs);
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
