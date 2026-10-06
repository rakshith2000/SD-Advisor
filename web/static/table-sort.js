/* Client-side table sorting.
 *
 * Reorders rows in place. No request, no reload, no scroll position lost.
 *
 * The one rule that matters: every comparison is made against the data-*
 * value on the row, never against the text in the cell. The Age column
 * renders "10 Days 12 Hrs" and "2 Days"; comparing those as strings puts
 * 2 Days after 10 Days - a table that looks sorted, is not, and is read as
 * authoritative. The server emits age_minutes alongside the rendered text for
 * exactly this reason, and a header whose data-sort has no matching row
 * attribute is reported in the console rather than silently doing nothing.
 *
 * Opt in from markup:
 *
 *   <table class="data" data-sortable="board">
 *     <thead><tr>
 *       <th data-sort="age" data-type="num">
 *         <button type="button">Age<span class="sort-indicator"></span></button>
 *       </th>
 *     </tr></thead>
 *     <tbody><tr data-age="14400"> ... </tr></tbody>
 *   </table>
 */

(function () {
  'use strict';

  var STORE_PREFIX = 'ata-sort-';

  function readKey(row, key, type) {
    var raw = row.dataset[key];
    if (type === 'num') {
      var n = parseFloat(raw);
      // Never NaN. Every comparison with NaN is false, which does not throw -
      // it just leaves the order quietly wrong.
      return isNaN(n) ? 0 : n;
    }
    return (raw || '').toLowerCase();
  }

  function compare(a, b, key, type, sign) {
    var x = readKey(a, key, type);
    var y = readKey(b, key, type);
    if (x < y) { return -sign; }
    if (x > y) { return sign; }
    return 0;
  }

  function apply(table, key, direction) {
    var body = table.tBodies[0];
    if (!body) { return; }

    var header = table.querySelector('th[data-sort="' + key + '"]');
    var type = (header && header.dataset.type) || 'text';
    var rows = Array.prototype.slice.call(body.rows);
    var sign = direction === 'desc' ? -1 : 1;

    // Array.prototype.sort is stable, so rows this column cannot separate
    // keep the order the server sent - attention score, then age. Without
    // that guarantee the table reshuffles its ties on every click and a lead
    // working down a list loses their place.
    rows.sort(function (a, b) { return compare(a, b, key, type, sign); });

    var frag = document.createDocumentFragment();
    rows.forEach(function (row) { frag.appendChild(row); });
    body.appendChild(frag);

    mark(table, key, direction);
  }

  function restore(table) {
    var body = table.tBodies[0];
    if (!body || !table._ataOriginal) { return; }
    var frag = document.createDocumentFragment();
    table._ataOriginal.forEach(function (row) { frag.appendChild(row); });
    body.appendChild(frag);
    mark(table, null, null);
  }

  function mark(table, key, direction) {
    var descending = direction === 'desc';

    table.querySelectorAll('th[data-sort]').forEach(function (th) {
      var active = key !== null && th.dataset.sort === key;
      var numeric = th.dataset.type === 'num';

      th.classList.toggle('is-sorted', active);
      th.setAttribute('aria-sort',
        active ? (descending ? 'descending' : 'ascending') : 'none');

      var indicator = th.querySelector('.sort-indicator');
      if (indicator) {
        indicator.textContent = active ? (descending ? '▼' : '▲') : '';
      }

      var button = th.querySelector('button');
      if (button) {
        var next = nextDirection(th, active ? direction : null);
        var phrase = numeric
          ? (next === 'desc' ? 'highest first' : 'lowest first')
          : (next === 'desc' ? 'Z to A' : 'A to Z');
        button.setAttribute('title', 'Sort by ' + label(th) + ', ' + phrase);
      }
    });

    var note = table.dataset.sortNote && document.getElementById(table.dataset.sortNote);
    if (note) {
      if (key === null) {
        note.textContent = note.dataset.default || '';
      } else {
        var th = table.querySelector('th[data-sort="' + key + '"]');
        var numeric = th && th.dataset.type === 'num';
        note.textContent = 'Sorted by ' + label(th).toLowerCase() + ', ' + (numeric
          ? (descending ? 'highest first' : 'lowest first')
          : (descending ? 'Z to A' : 'A to Z'));
      }
    }

    var reset = table.dataset.sortReset && document.getElementById(table.dataset.sortReset);
    if (reset) { reset.hidden = key === null; }
  }

  function label(th) {
    if (!th) { return 'this column'; }
    if (th.dataset.label) { return th.dataset.label; }
    var button = th.querySelector('button');
    return (button ? button.textContent : th.textContent).trim();
  }

  function nextDirection(th, current) {
    if (current) { return current === 'desc' ? 'asc' : 'desc'; }
    // First click: numbers descending, names ascending. Score, age and idle
    // are all read from the worst end, and opening a board about the worst
    // tickets on the calmest ones is not useful. A name list wants A to Z.
    return th.dataset.type === 'num' ? 'desc' : 'asc';
  }

  function remember(table, key, direction) {
    if (!table.dataset.sortable) { return; }
    try {
      if (key === null) {
        localStorage.removeItem(STORE_PREFIX + table.dataset.sortable);
      } else {
        localStorage.setItem(STORE_PREFIX + table.dataset.sortable,
                             key + ':' + direction);
      }
    } catch (e) {
      // Private browsing, or storage disabled by policy. The sort still works
      // for this page; it just will not be remembered.
    }
  }

  function recall(table) {
    if (!table.dataset.sortable) { return null; }
    try {
      var stored = localStorage.getItem(STORE_PREFIX + table.dataset.sortable);
      if (!stored) { return null; }
      var parts = stored.split(':');
      // Validate against the headers actually present. A column removed from
      // the table would otherwise leave a stored preference that silently
      // sorts nothing.
      if (!table.querySelector('th[data-sort="' + parts[0] + '"]')) { return null; }
      if (parts[1] !== 'asc' && parts[1] !== 'desc') { return null; }
      return { key: parts[0], direction: parts[1] };
    } catch (e) {
      return null;
    }
  }

  function warnMissingData(table) {
    var body = table.tBodies[0];
    var first = body && body.rows[0];
    if (!first) { return; }
    table.querySelectorAll('th[data-sort]').forEach(function (th) {
      if (!(th.dataset.sort in first.dataset)) {
        // A header with no matching row attribute is a link that appears to
        // sort and does not - the failure is invisible on screen, so it is
        // made visible here.
        console.warn('table-sort: no data-' + th.dataset.sort +
                     ' on the rows of', table);
      }
    });
  }

  function init(table) {
    var body = table.tBodies[0];
    if (!body) { return; }

    // Captured before anything is reordered, so "back to the ranking" is the
    // server's order rather than a re-sort that approximates it.
    table._ataOriginal = Array.prototype.slice.call(body.rows);
    warnMissingData(table);

    table.querySelectorAll('th[data-sort]').forEach(function (th) {
      var button = th.querySelector('button');
      if (!button) { return; }
      button.addEventListener('click', function () {
        var active = th.classList.contains('is-sorted');
        var current = active ? th.getAttribute('aria-sort') : null;
        var direction = nextDirection(
          th, current === 'descending' ? 'desc' : current === 'ascending' ? 'asc' : null);
        apply(table, th.dataset.sort, direction);
        remember(table, th.dataset.sort, direction);
      });
    });

    var reset = table.dataset.sortReset && document.getElementById(table.dataset.sortReset);
    if (reset) {
      reset.addEventListener('click', function (event) {
        event.preventDefault();
        restore(table);
        remember(table, null, null);
      });
    }

    var stored = recall(table);
    if (stored) {
      apply(table, stored.key, stored.direction);
    } else {
      mark(table, null, null);
    }
  }

  function boot() {
    document.querySelectorAll('table[data-sortable]').forEach(init);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
