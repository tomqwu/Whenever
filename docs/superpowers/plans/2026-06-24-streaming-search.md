# Streaming Search Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `POST /api/search/stream` that pushes each fare cell to the browser as it completes (NDJSON), replacing the current single-JSON-dump approach so the grid fills in live while results arrive.

**Architecture:** Extract a `_build_cell()` helper from `run_search` used by both the existing route and a new streaming generator. The generator uses `concurrent.futures.as_completed` to yield cells in completion order. The frontend replaces the `#run` click handler to consume the NDJSON stream and progressively populate a skeleton grid. The old `/api/search` endpoint and `render()` function remain untouched.

**Tech Stack:** Python 3.10+, Flask `Response` + `stream_with_context`, `concurrent.futures`, browser `ReadableStream` (native Fetch API), NDJSON (`application/x-ndjson`), pytest, Playwright (e2e).

## Global Constraints

- `source .venv/bin/activate` before any Python command.
- Coverage gate: `pytest --cov=app --cov=watch --cov=scheduler --cov=export --cov-fail-under=99` must remain green.
- Resource-warning gate: `pytest -W error::ResourceWarning` must remain pristine.
- All existing tests must stay green.
- TDD: write failing test → watch it fail → write minimal implementation → watch it pass → commit.
- Commit message: `feat: stream search results incrementally (NDJSON) so the grid fills live`.
- Branch: `feat/streaming-search` (already checked out).
- Web app only — no changes to `watch.py`, `scheduler.py`, `export.py`.
- Activate venv: `source /Users/tomwu/Projects/Whenever/.venv/bin/activate`.

---

### Task 1: Extract `_build_cell` helper + unit test

**Files:**
- Modify: `/Users/tomwu/Projects/Whenever/app.py` (add `_build_cell`, use it in `run_search`)
- Create: `/Users/tomwu/Projects/Whenever/tests/unit/test_stream.py`

**Interfaces:**
- Produces: `_build_cell(origin, code, dep, ret, adults, child_ages, fare, threshold) -> dict` with keys: `dep, ret, cheapest_cad, stops, nonstop_cad, chosen, chosen_cad, source, book`.
- `run_search` output shape stays identical.

- [ ] **Step 1: Write the failing test for `_build_cell`**

Create `/Users/tomwu/Projects/Whenever/tests/unit/test_stream.py`:

```python
"""Tests for streaming search: _build_cell helper and /api/search/stream endpoint."""
import json
import app as appmod


# ---------------------------------------------------------------------------
# _build_cell helper
# ---------------------------------------------------------------------------

def test_build_cell_picks_nonstop_within_threshold():
    fare = {"cheapest_cad": 1000, "stops": 1, "nonstop_cad": 1100, "source": "test", "book": None}
    cell = appmod._build_cell("YYZ", "PVG", "2026-12-12", "2027-01-04", 2, [11], fare, 0.25)
    assert cell["chosen"] == "nonstop"          # 1100 <= 1000 * 1.25
    assert cell["chosen_cad"] == 1100
    assert cell["dep"] == "2026-12-12"
    assert cell["ret"] == "2027-01-04"
    assert cell["cheapest_cad"] == 1000
    assert cell["nonstop_cad"] == 1100
    assert cell["source"] == "test"
    # book falls back to kayak because fare["book"] is None
    assert cell["book"].startswith("https://www.kayak.com")


def test_build_cell_picks_cheapest_when_nonstop_too_pricey():
    fare = {"cheapest_cad": 1000, "stops": 1, "nonstop_cad": 2000, "source": "test", "book": "https://b"}
    cell = appmod._build_cell("YYZ", "XXX", "2026-12-12", "2027-01-04", 2, [], fare, 0.10)
    assert cell["chosen"] == "cheapest"         # 2000 > 1000 * 1.10
    assert cell["chosen_cad"] == 1000
    assert cell["book"] == "https://b"          # provider link kept


def test_build_cell_no_data():
    fare = {"cheapest_cad": None, "stops": None, "nonstop_cad": None, "source": "no-data"}
    cell = appmod._build_cell("YYZ", "XXX", "2026-12-12", "2027-01-04", 1, [], fare, 0.25)
    assert cell["cheapest_cad"] is None
    assert cell["chosen_cad"] is None
    assert cell["source"] == "no-data"
    # kayak fallback link must still be present
    assert cell["book"].startswith("https://www.kayak.com")
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd /Users/tomwu/Projects/Whenever && source .venv/bin/activate && pytest tests/unit/test_stream.py -v 2>&1 | head -40
```

Expected: `AttributeError: module 'app' has no attribute '_build_cell'`

- [ ] **Step 3: Add `_build_cell` to `app.py` and refactor `run_search` to use it**

In `/Users/tomwu/Projects/Whenever/app.py`, insert `_build_cell` just before `run_search` (around line 475):

```python
def _build_cell(origin, code, dep, ret, adults, child_ages, fare, threshold):
    """Build the cell dict for a single dep×ret combo.

    fare: dict from get_fare() with keys cheapest_cad, stops, nonstop_cad, source, book.
    threshold: float fraction (e.g. 0.25 for 25 %).
    Returns dict with keys: dep, ret, cheapest_cad, stops, nonstop_cad, chosen, chosen_cad, source, book.
    """
    cheap = fare.get("cheapest_cad")
    ns = fare.get("nonstop_cad")
    chosen = "cheapest"
    chosen_cad = cheap
    if ns and cheap and ns <= cheap * (1 + threshold):
        chosen, chosen_cad = "nonstop", ns
    return {
        "dep": dep, "ret": ret,
        "cheapest_cad": cheap, "stops": fare.get("stops"),
        "nonstop_cad": ns, "chosen": chosen, "chosen_cad": chosen_cad,
        "source": fare.get("source"),
        "book": fare.get("book") or kayak_link(origin, code, dep, ret, adults, child_ages),
    }
```

Then replace the cell-building block inside `run_search` (in the "Assemble results" loop). The current code (lines ~521-533):

```python
                f = fare_results[(di, dpi, ri)]
                cheap = f.get("cheapest_cad")
                ns = f.get("nonstop_cad")
                chosen = "cheapest"
                chosen_cad = cheap
                if ns and cheap and ns <= cheap * (1 + threshold):
                    chosen, chosen_cad = "nonstop", ns
                row.append({
                    "dep": dep, "ret": ret,
                    "cheapest_cad": cheap, "stops": f.get("stops"),
                    "nonstop_cad": ns, "chosen": chosen, "chosen_cad": chosen_cad,
                    "source": f.get("source"),
                    "book": f.get("book") or kayak_link(origin, code, dep, ret, adults, child_ages),
                })
```

Replace with:

```python
                f = fare_results[(di, dpi, ri)]
                row.append(_build_cell(origin, code, dep, ret, adults, child_ages, f, threshold))
```

- [ ] **Step 4: Run test to verify it passes**

```bash
cd /Users/tomwu/Projects/Whenever && source .venv/bin/activate && pytest tests/unit/test_stream.py -v 2>&1 | head -40
```

Expected: 3 tests PASS.

- [ ] **Step 5: Run all existing tests to confirm no regressions**

```bash
cd /Users/tomwu/Projects/Whenever && source .venv/bin/activate && pytest tests/unit/ -v 2>&1 | tail -20
```

Expected: all existing tests still PASS.

- [ ] **Step 6: Commit**

```bash
cd /Users/tomwu/Projects/Whenever && source .venv/bin/activate && git add app.py tests/unit/test_stream.py && git commit -m "refactor: extract _build_cell helper; add unit tests"
```

---

### Task 2: Backend — `POST /api/search/stream` endpoint

**Files:**
- Modify: `/Users/tomwu/Projects/Whenever/app.py` (add `/api/search/stream` route)
- Modify: `/Users/tomwu/Projects/Whenever/tests/unit/test_stream.py` (add streaming endpoint tests)

**Interfaces:**
- Consumes: `_build_cell` (Task 1), `_search_args_from_body`, `get_fare`, `build_recommendation`, `providers_configured`.
- Produces: `POST /api/search/stream` → `200 application/x-ndjson` or `400 application/json`.
- NDJSON line order: `meta` → N × `cell` → `recommendation` → `done`.

- [ ] **Step 1: Write failing tests for the streaming endpoint**

Add to `/Users/tomwu/Projects/Whenever/tests/unit/test_stream.py`:

```python
# ---------------------------------------------------------------------------
# /api/search/stream endpoint
# ---------------------------------------------------------------------------

def _stream_lines(client, payload):
    """POST to /api/search/stream and return parsed NDJSON lines as a list of dicts."""
    resp = client.post("/api/search/stream", json=payload)
    return resp, [json.loads(line) for line in resp.data.split(b"\n") if line.strip()]


_STREAM_PAYLOAD = {
    "origin": "YYZ",
    "destinations": [
        {"city": "Shanghai", "iata": "PVG"},
        {"city": "Beijing", "iata": "PEK"},
    ],
    "dep_dates": ["2026-12-12", "2026-12-13"],
    "ret_dates": ["2027-01-04", "2027-01-05"],
}

_FAKE_FARE = {
    "cheapest_cad": 1000, "stops": 1, "nonstop_cad": 1100,
    "source": "test", "book": "https://example.com",
}


def test_stream_400_on_missing_origin(client):
    resp = client.post("/api/search/stream", json={
        "origin": "", "destinations": [],
        "dep_dates": ["2026-12-12"], "ret_dates": ["2027-01-04"],
    })
    assert resp.status_code == 400
    body = resp.get_json()
    assert "error" in body
    # Must NOT be streaming — content type must be JSON, not ndjson
    assert "application/json" in resp.content_type


def test_stream_400_on_missing_dates(client):
    resp = client.post("/api/search/stream", json={
        "origin": "YYZ",
        "destinations": [{"city": "X", "iata": "XXX"}],
    })
    assert resp.status_code == 400


def test_stream_first_line_is_meta(client, monkeypatch):
    monkeypatch.setattr(appmod, "get_fare", lambda *a, **k: _FAKE_FARE)
    monkeypatch.setattr(appmod, "build_recommendation", lambda *a, **k: "Best value: test")

    resp, lines = _stream_lines(client, _STREAM_PAYLOAD)
    assert resp.status_code == 200
    assert "application/x-ndjson" in resp.content_type

    meta = lines[0]
    assert meta["type"] == "meta"
    assert meta["origin"] == "YYZ"
    # 2 dests × 2 dep × 2 ret = 8 cells
    assert meta["total_cells"] == 8
    assert len(meta["results"]) == 2
    assert meta["results"][0] == {"city": "Shanghai", "iata": "PVG"}
    assert meta["results"][1] == {"city": "Beijing", "iata": "PEK"}
    assert set(meta["dep_dates"]) == {"2026-12-12", "2026-12-13"}
    assert set(meta["ret_dates"]) == {"2027-01-04", "2027-01-05"}


def test_stream_cell_count_and_shape(client, monkeypatch):
    monkeypatch.setattr(appmod, "get_fare", lambda *a, **k: _FAKE_FARE)
    monkeypatch.setattr(appmod, "build_recommendation", lambda *a, **k: "Best value: test")

    resp, lines = _stream_lines(client, _STREAM_PAYLOAD)
    cell_lines = [l for l in lines if l.get("type") == "cell"]
    assert len(cell_lines) == 8   # total_cells == 8

    for c in cell_lines:
        assert c["type"] == "cell"
        assert c["dest_index"] in (0, 1)
        assert "dep" in c
        assert "ret" in c
        assert "cheapest_cad" in c
        assert "chosen" in c
        assert "book" in c


def test_stream_recommendation_and_done(client, monkeypatch):
    monkeypatch.setattr(appmod, "get_fare", lambda *a, **k: _FAKE_FARE)
    monkeypatch.setattr(appmod, "build_recommendation", lambda *a, **k: "Best value: test")

    resp, lines = _stream_lines(client, _STREAM_PAYLOAD)
    types = [l["type"] for l in lines]
    assert types[0] == "meta"
    assert types[-1] == "done"
    assert "recommendation" in types

    rec = next(l for l in lines if l["type"] == "recommendation")
    assert rec["text"] == "Best value: test"


def test_stream_line_order(client, monkeypatch):
    """meta must come first, done last, cells in between."""
    monkeypatch.setattr(appmod, "get_fare", lambda *a, **k: _FAKE_FARE)
    monkeypatch.setattr(appmod, "build_recommendation", lambda *a, **k: "rec")

    resp, lines = _stream_lines(client, _STREAM_PAYLOAD)
    types = [l["type"] for l in lines]
    assert types[0] == "meta"
    assert types[-1] == "done"
    assert types[-2] == "recommendation"
    assert all(t == "cell" for t in types[1:-2])
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /Users/tomwu/Projects/Whenever && source .venv/bin/activate && pytest tests/unit/test_stream.py -v -k "stream" 2>&1 | head -50
```

Expected: all new stream tests fail with 404 (route doesn't exist yet).

- [ ] **Step 3: Add the streaming endpoint to `app.py`**

Add the following after the existing `api_search` route (around line 589), before `api_export_csv`:

```python
from flask import stream_with_context


@app.route("/api/search/stream", methods=["POST"])
def api_search_stream():
    """POST /api/search/stream — same body as /api/search.

    Returns application/x-ndjson:
      {"type":"meta", ...}           (first)
      {"type":"cell", ...}           (one per cell, as completed)
      {"type":"recommendation", ...} (after all cells)
      {"type":"done"}                (last)

    If the body is invalid, returns 400 JSON (not streamed).
    """
    b = request.get_json(force=True)
    args = _search_args_from_body(b)
    if args is None:
        return jsonify(_SEARCH_ARGS_400), 400

    # Capture all args now — generator must not touch `request` after this point.
    origin = args["origin"]
    dests = args["dests"]
    adults = args["adults"]
    child_ages = args["child_ages"]
    dep_dates = args["dep_dates"]
    ret_dates = args["ret_dates"]
    threshold_pct = args["threshold_pct"]
    families = args["families"]

    @stream_with_context
    def generate():
        children = len(child_ages)
        threshold = threshold_pct / 100.0

        # Build the flat task list: (dest_index, code, dep, ret)
        tasks = []
        for di, dest in enumerate(dests):
            code = (dest.get("iata") or "").upper()[:3]
            for dep in dep_dates:
                for ret in ret_dates:
                    tasks.append((di, code, dep, ret))

        total_cells = len(tasks)

        # --- meta line ---
        meta = {
            "type": "meta",
            "origin": origin,
            "adults": adults,
            "child_ages": child_ages,
            "families": families,
            "dep_dates": dep_dates,
            "ret_dates": ret_dates,
            "providers": providers_configured(),
            "results": [{"city": d.get("city"), "iata": (d.get("iata") or "").upper()[:3]}
                        for d in dests],
            "total_cells": total_cells,
        }
        yield json.dumps(meta) + "\n"

        # --- cell lines (as completed) ---
        # Accumulate cells in order for the final recommendation.
        # cells_by_dest[di] collects (dep, ret, cell_dict) tuples.
        cells_by_dest: dict = {di: [] for di in range(len(dests))}

        def _fetch(task):
            di, code, dep, ret = task
            fare = get_fare(origin, code, dep, ret, adults, children)
            cell = _build_cell(origin, code, dep, ret, adults, child_ages, fare, threshold)
            return di, cell

        workers = max(1, SEARCH_CONCURRENCY)
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_fetch, t): t for t in tasks}
            for fut in concurrent.futures.as_completed(futures):
                di, cell = fut.result()
                cells_by_dest[di].append(cell)
                line = {"type": "cell", "dest_index": di, **cell}
                yield json.dumps(line) + "\n"

        # --- recommendation line ---
        # Reconstruct `results` in dest order (same shape as run_search output).
        results = []
        for di, dest in enumerate(dests):
            code = (dest.get("iata") or "").upper()[:3]
            flat = [c for c in cells_by_dest[di] if c["chosen_cad"]]
            best = min(flat, key=lambda c: c["chosen_cad"]) if flat else None
            # Rebuild grid in dep/ret order for consistency.
            grid_cells = {(c["dep"], c["ret"]): c for c in cells_by_dest[di]}
            grid = [
                [grid_cells.get((dep, ret), {
                    "dep": dep, "ret": ret,
                    "cheapest_cad": None, "stops": None, "nonstop_cad": None,
                    "chosen": "cheapest", "chosen_cad": None, "source": "no-data",
                    "book": kayak_link(origin, code, dep, ret, adults, child_ages),
                }) for ret in ret_dates]
                for dep in dep_dates
            ]
            results.append({
                "city": dest.get("city"), "iata": code,
                "grid": grid, "best": best,
            })

        rec_text = build_recommendation(origin, results, adults, child_ages, families)
        yield json.dumps({"type": "recommendation", "text": rec_text}) + "\n"

        # --- done line ---
        yield json.dumps({"type": "done"}) + "\n"

    return Response(generate(), mimetype="application/x-ndjson")
```

Note: `from flask import stream_with_context` — check that `stream_with_context` is already imported at the top of `app.py`. The current import is `from flask import Flask, request, jsonify, render_template, Response`. Add `stream_with_context` to this import.

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd /Users/tomwu/Projects/Whenever && source .venv/bin/activate && pytest tests/unit/test_stream.py -v 2>&1 | tail -30
```

Expected: all tests PASS.

- [ ] **Step 5: Run full unit test suite to check for regressions and coverage**

```bash
cd /Users/tomwu/Projects/Whenever && source .venv/bin/activate && pytest tests/unit/ --cov=app --cov=watch --cov=scheduler --cov=export --cov-fail-under=99 --cov-report=term-missing -W error::ResourceWarning 2>&1 | tail -30
```

Expected: all PASS, coverage ≥ 99%.

- [ ] **Step 6: Commit**

```bash
cd /Users/tomwu/Projects/Whenever && source .venv/bin/activate && git add app.py tests/unit/test_stream.py && git commit -m "feat: add POST /api/search/stream NDJSON streaming endpoint"
```

---

### Task 3: Frontend — streaming `#run` click handler + skeleton grid

**Files:**
- Modify: `/Users/tomwu/Projects/Whenever/templates/index.html`

**Interfaces:**
- Consumes: `/api/search/stream` (Task 2) NDJSON.
- Produces: live grid fill using existing CSS classes (`pick-ns`, `best`, `td.err`, `money()`, `fmtDate()`).
- Keeps: existing `render()` function (used by export/fallback); refactors shared cell-HTML into `_cellHtml(c, thr)`.

- [ ] **Step 1: Plan the JS changes (no code written yet)**

The `$('#run').onclick` handler at line 191 of `index.html` must:
1. POST to `/api/search/stream` instead of `/api/search`.
2. Check response ok; if content-type is JSON (400), alert the error.
3. Read `response.body.getReader()`, decode NDJSON line-by-line.
4. On `meta`: store dep/ret dates and dests; render skeleton (summary cards with "…", grid tables with "…" placeholders); reset `cellsDone=0, total=meta.total_cells, bestByCityIdx={}`.
5. On `cell`: find city's table by `dest_index` and cell `td[data-k="dep|ret"]`; replace placeholder with cell HTML using `_cellHtml(c, thr)`; update city's best tracking; update progress bar.
6. On `recommendation`: show `#rec`.
7. On `done`: finalize progress, re-enable run button.

Also add `_cellHtml(c, thr)` helper that produces the inner HTML for a cell (used by both the streaming path and `render()`).

- [ ] **Step 2: Write the updated `index.html`**

Replace the `$('#run').onclick` handler and add `_cellHtml`. In `templates/index.html`, replace the `$('#run').onclick = async ()=>{...};` block (lines 191-215) and add `_cellHtml` before it, and update `render()` to call `_cellHtml`.

New helper to add before `$('#run').onclick`:

```javascript
function _cellHtml(c, thr) {
  if (c.cheapest_cad == null) return '<td class="err" data-k="' + c.dep + '|' + c.ret + '">n/a</td>';
  const pickNS = c.nonstop_cad && c.cheapest_cad && c.nonstop_cad <= c.cheapest_cad * thr;
  const cls = pickNS ? 'pick-ns' : '';
  let inner = '<a class="price" href="' + c.book + '" target="_blank" rel="noopener">' + money(c.cheapest_cad) + '</a>' +
    '<div class="stops">' + (c.stops != null ? c.stops + ' stop' + (c.stops === 1 ? '' : 's') : '') + '</div>';
  if (c.nonstop_cad) inner += '<div class="ns">ns ' + money(c.nonstop_cad) + '</div>';
  return '<td class="' + cls + '" data-k="' + c.dep + '|' + c.ret + '">' + inner + '</td>';
}
```

New `$('#run').onclick` handler:

```javascript
$('#run').onclick = async () => {
  const dests = CITIES.filter(c => c.on).map(c => ({ city: c.city, iata: c.iata }));
  if (!dests.length) { alert('Pick at least one destination city (use "Get top cities").'); return; }
  const thr = 1 + (parseFloat($('#thresh').value) || 25) / 100;
  const fams = parseInt($('#families').value) || 1;
  const payload = {
    origin: $('#depCode').value.trim().toUpperCase(),
    destinations: dests,
    adults: parseInt($('#adults').value) || 1,
    child_ages: kidAges(),
    families: fams,
    dep_start: $('#depStart').value, dep_span: parseInt($('#depSpan').value) || 2,
    ret_start: $('#retStart').value, ret_span: parseInt($('#retSpan').value) || 2,
    nonstop_threshold: parseFloat($('#thresh').value) || 25,
  };
  $('#run').disabled = true;
  $('#runHint').textContent = 'Searching ' + dests.length + ' cities…';
  $('#prog').style.width = '5%';
  $('#rec').style.display = 'none';
  $('#summary').innerHTML = '';
  $('#grids').innerHTML = '';

  let cellsDone = 0, total = 0;
  const bestByCityIdx = {};  // dest_index -> best cell so far

  try {
    const response = await fetch('/api/search/stream', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });

    // Non-ok or JSON error (400)
    if (!response.ok) {
      let msg = 'Search failed (' + response.status + ')';
      try { const e = await response.json(); if (e.error) msg = e.error; } catch (_) {}
      alert(msg);
      return;
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buf = '';

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      const parts = buf.split('\n');
      buf = parts.pop();  // last partial line stays in buffer
      for (const raw of parts) {
        const line = raw.trim();
        if (!line) continue;
        let msg;
        try { msg = JSON.parse(line); } catch (_) { continue; }

        if (msg.type === 'meta') {
          total = msg.total_cells;
          // Build skeleton: summary cards + grid tables
          const sum = $('#summary');
          sum.innerHTML = '';
          const g = $('#grids');
          g.innerHTML = '';
          msg.results.forEach((r, di) => {
            // Summary card skeleton
            const card = document.createElement('div');
            card.className = 'card';
            card.id = 'card-' + di;
            card.innerHTML = '<div class="city">' + r.city + ' <small>(' + r.iata + ')</small></div>' +
              '<div class="price" id="card-price-' + di + '">…</div>' +
              '<div class="meta" id="card-meta-' + di + '">searching…</div>' +
              '<div class="meta">group ×' + fams + ': <span id="card-group-' + di + '">…</span></div>';
            sum.appendChild(card);

            // Grid table skeleton
            const blk = document.createElement('div');
            blk.className = 'cityblock';
            blk.id = 'blk-' + di;
            let html = '<h2>' + r.city + ' <small>(' + r.iata + ')</small></h2>' +
              '<table id="tbl-' + di + '"><thead><tr><th>dep \\ ret</th>' +
              msg.ret_dates.map(d => '<th>' + fmtDate(d) + '</th>').join('') +
              '</tr></thead><tbody>';
            msg.dep_dates.forEach(dep => {
              html += '<tr><td class="rowh">' + fmtDate(dep) + '</td>';
              msg.ret_dates.forEach(ret => {
                html += '<td class="loading" data-k="' + dep + '|' + ret + '">…</td>';
              });
              html += '</tr>';
            });
            html += '</tbody></table>';
            blk.innerHTML = html;
            g.appendChild(blk);
          });
          $('#prog').style.width = '10%';

        } else if (msg.type === 'cell') {
          const di = msg.dest_index;
          const key = msg.dep + '|' + msg.ret;
          const tbl = document.getElementById('tbl-' + di);
          if (tbl) {
            const td = tbl.querySelector('td[data-k="' + key + '"]');
            if (td) {
              // Build new td content
              if (msg.cheapest_cad == null) {
                td.className = 'err';
                td.innerHTML = 'n/a';
              } else {
                const pickNS = msg.nonstop_cad && msg.cheapest_cad && msg.nonstop_cad <= msg.cheapest_cad * thr;
                td.className = pickNS ? 'pick-ns' : '';
                let inner = '<a class="price" href="' + msg.book + '" target="_blank" rel="noopener">' + money(msg.cheapest_cad) + '</a>' +
                  '<div class="stops">' + (msg.stops != null ? msg.stops + ' stop' + (msg.stops === 1 ? '' : 's') : '') + '</div>';
                if (msg.nonstop_cad) inner += '<div class="ns">ns ' + money(msg.nonstop_cad) + '</div>';
                td.innerHTML = inner;
              }
            }
          }
          // Update best tracking for summary card
          if (msg.chosen_cad != null) {
            const prev = bestByCityIdx[di];
            if (!prev || msg.chosen_cad < prev.chosen_cad) {
              bestByCityIdx[di] = msg;
              const priceEl = document.getElementById('card-price-' + di);
              const metaEl = document.getElementById('card-meta-' + di);
              const groupEl = document.getElementById('card-group-' + di);
              if (priceEl) priceEl.textContent = money(msg.chosen_cad);
              if (metaEl) metaEl.textContent = fmtDate(msg.dep) + ' → ' + fmtDate(msg.ret) + ' · ' + (msg.chosen === 'nonstop' ? 'nonstop' : msg.stops + ' stop(s)');
              if (groupEl) groupEl.textContent = money(msg.chosen_cad * fams);
            }
          }
          cellsDone++;
          const pct = total > 0 ? 10 + (cellsDone / total) * 85 : 10;
          $('#prog').style.width = pct + '%';

        } else if (msg.type === 'recommendation') {
          const rec = $('#rec');
          rec.style.display = 'block';
          rec.innerHTML = '<h3>Best-value pick</h3>' + (msg.text || '');
          // Apply best-cell highlights
          Object.entries(bestByCityIdx).forEach(([di, b]) => {
            const tbl = document.getElementById('tbl-' + di);
            if (tbl && b) {
              const td = tbl.querySelector('td[data-k="' + b.dep + '|' + b.ret + '"]');
              if (td) td.classList.add('best');
            }
          });

        } else if (msg.type === 'done') {
          $('#prog').style.width = '100%';
          setTimeout(() => { $('#prog').style.width = '0%'; }, 600);
        }
      }
    }
  } catch (e) {
    alert('Search failed: ' + e);
  } finally {
    $('#run').disabled = false;
    $('#runHint').textContent = '';
  }
};
```

Update `render()` so it reuses `_cellHtml` for cell markup. In the `render()` function, replace the inner cell-building loop:

Old (lines ~243-252):
```javascript
      row.forEach(c=>{
        if(c.cheapest_cad==null){ html+='<td class="err">n/a</td>'; return; }
        const pickNS = c.nonstop_cad && c.cheapest_cad && c.nonstop_cad <= c.cheapest_cad*thr;
        const cls = pickNS?'pick-ns':'';
        let cell = `<a class="price" href="${c.book}" target="_blank" rel="noopener">${money(c.cheapest_cad)}</a>`+
          `<div class="stops">${c.stops!=null?c.stops+' stop'+(c.stops===1?'':'s'):''}</div>`;
        if(c.nonstop_cad) cell+=`<div class="ns">ns ${money(c.nonstop_cad)}</div>`;
        html+=`<td class="${cls}" data-k="${c.dep}|${c.ret}">${cell}</td>`;
      });
```

New:
```javascript
      row.forEach(c=>{ html += _cellHtml(c, thr); });
```

- [ ] **Step 3: Verify the file changes compile/parse correctly (no syntax errors)**

```bash
cd /Users/tomwu/Projects/Whenever && source .venv/bin/activate && python3 -c "
from flask import Flask
app = Flask(__name__, template_folder='templates')
with app.app_context():
    from flask import render_template_string
    import open as _open
    with open('templates/index.html') as f:
        src = f.read()
    print('Template loaded OK, length:', len(src))
" 2>&1 || echo "Check template manually"
```

Also eyeball the template to verify the JS looks correct.

- [ ] **Step 4: Run existing unit tests (index renders)**

```bash
cd /Users/tomwu/Projects/Whenever && source .venv/bin/activate && pytest tests/unit/test_smoke.py tests/unit/test_routes.py -v 2>&1 | tail -20
```

Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
cd /Users/tomwu/Projects/Whenever && source .venv/bin/activate && git add templates/index.html && git commit -m "feat: streaming frontend — live grid fill via NDJSON reader"
```

---

### Task 4: E2E tests for the streaming search

**Files:**
- Create: `/Users/tomwu/Projects/Whenever/tests/e2e/test_stream_ui.py`

**Interfaces:**
- Consumes: `seed_live_server` fixture from `tests/e2e/conftest.py` (patches `get_fare`, `build_recommendation`; uses real China seeds).
- Produces: Playwright tests that drive the browser through the streaming search and assert final state.

- [ ] **Step 1: Write failing e2e tests**

Create `/Users/tomwu/Projects/Whenever/tests/e2e/test_stream_ui.py`:

```python
"""E2E tests for /api/search/stream: drive the browser through a live-server
streaming search and assert progressive rendering produces the correct final state."""

import json
import pytest


def test_streaming_search_full_flow(seed_live_server, page):
    """Toronto (YYZ) → China: click run, wait for stream to complete, assert grid populated."""
    page.goto(seed_live_server)

    # Load top cities (China seeds)
    page.click("#loadCities")
    page.wait_for_selector(".chip")

    # Click run — triggers /api/search/stream
    page.click("#run")

    # Wait for the recommendation to appear (stream complete)
    page.wait_for_selector("#rec", state="visible", timeout=15000)

    # #rec should contain "Best value"
    rec_text = page.inner_text("#rec")
    assert "Best value" in rec_text, f"Expected 'Best value' in #rec, got: {rec_text!r}"

    # At least one summary card should be visible
    page.wait_for_selector("#summary .card")
    summary_text = page.inner_text("#summary")
    assert any(city in summary_text for city in ("Beijing", "Shanghai", "Guangzhou")), \
        f"Expected a China city in #summary, got: {summary_text!r}"

    # Grid table(s) must be present
    assert page.query_selector("table") is not None, "Expected at least one grid table"

    # At least one cell should show a price (not the placeholder "…")
    # The mock fare is cheapest_cad=8000 → renders as "$8,000"
    page.wait_for_function(
        "() => document.querySelector('td a.price') !== null",
        timeout=15000,
    )
    price_link = page.query_selector("td a.price")
    assert price_link is not None, "Expected at least one rendered price link in a cell"
    price_text = price_link.inner_text()
    assert "$" in price_text, f"Expected a $ price in cell, got: {price_text!r}"


def test_streaming_search_run_button_re_enabled(seed_live_server, page):
    """After stream completes, the run button must be re-enabled."""
    page.goto(seed_live_server)
    page.click("#loadCities")
    page.wait_for_selector(".chip")
    page.click("#run")

    # Wait for done (rec visible)
    page.wait_for_selector("#rec", state="visible", timeout=15000)

    # Button must be re-enabled
    is_disabled = page.get_attribute("#run", "disabled")
    assert is_disabled is None, "Run button should be re-enabled after stream completes"


def test_streaming_search_progress_bar(seed_live_server, page):
    """Progress bar should become non-zero during search and reset after."""
    page.goto(seed_live_server)
    page.click("#loadCities")
    page.wait_for_selector(".chip")
    page.click("#run")

    # Wait for stream to complete
    page.wait_for_selector("#rec", state="visible", timeout=15000)

    # After done + 600 ms timeout, progress bar should be ~0% (reset)
    page.wait_for_timeout(800)
    width = page.eval_on_selector("#prog", "el => el.style.width")
    assert width in ("0%", ""), f"Expected progress bar reset to 0%, got {width!r}"
```

- [ ] **Step 2: Run the e2e tests to verify they pass**

```bash
cd /Users/tomwu/Projects/Whenever && source .venv/bin/activate && pytest tests/e2e/test_stream_ui.py -v 2>&1 | tail -30
```

Expected: all 3 tests PASS.

- [ ] **Step 3: Run the full e2e suite to check for regressions**

```bash
cd /Users/tomwu/Projects/Whenever && source .venv/bin/activate && pytest tests/e2e/ -v 2>&1 | tail -30
```

Expected: all existing e2e tests PASS (including `test_toronto_china_full_flow` which now exercises the streaming path).

- [ ] **Step 4: Commit**

```bash
cd /Users/tomwu/Projects/Whenever && source .venv/bin/activate && git add tests/e2e/test_stream_ui.py && git commit -m "test(e2e): streaming search UI tests with seed_live_server"
```

---

### Task 5: Document `POST /api/search/stream` in `docs/API.md`

**Files:**
- Modify: `/Users/tomwu/Projects/Whenever/docs/API.md`

- [ ] **Step 1: Add documentation for the new endpoint**

Append the following section to `/Users/tomwu/Projects/Whenever/docs/API.md` (after the `/api/search` section, before the end of file):

```markdown
---

### `POST /api/search/stream`

Streaming variant of `/api/search`. Accepts an **identical request body** but returns
results incrementally as `application/x-ndjson` (one JSON object per line, newline-delimited).
Use this endpoint to populate the flight grid live as each cell's fare arrives.

**Body:** identical to `POST /api/search` (see above).

**Response:** `200 application/x-ndjson` — one compact JSON object per line:

#### Line types (in order)

| type | When | Key fields |
|------|------|------------|
| `meta` | First line | `origin`, `adults`, `child_ages`, `families`, `dep_dates`, `ret_dates`, `providers`, `results` (array of `{city,iata}`), `total_cells` |
| `cell` | One per cell, as completed | `dest_index` (index into `meta.results`), plus all fields from a `/api/search` grid cell: `dep`, `ret`, `cheapest_cad`, `stops`, `nonstop_cad`, `chosen`, `chosen_cad`, `source`, `book` |
| `recommendation` | After all cells | `text` (same string as `/api/search`'s `recommendation` field) |
| `done` | Last line | _(no extra fields)_ |

#### Example stream

```ndjson
{"type":"meta","origin":"YYZ","adults":2,"child_ages":[11,9],"families":3,"dep_dates":["2026-12-12","2026-12-13"],"ret_dates":["2027-01-04","2027-01-05"],"providers":["travelpayouts"],"results":[{"city":"Shanghai","iata":"PVG"}],"total_cells":4}
{"type":"cell","dest_index":0,"dep":"2026-12-13","ret":"2027-01-05","cheapest_cad":8400,"stops":1,"nonstop_cad":null,"chosen":"cheapest","chosen_cad":8400,"source":"travelpayouts","book":"https://..."}
{"type":"cell","dest_index":0,"dep":"2026-12-12","ret":"2027-01-04","cheapest_cad":8000,"stops":1,"nonstop_cad":8500,"chosen":"cheapest","chosen_cad":8000,"source":"travelpayouts","book":"https://..."}
...
{"type":"recommendation","text":"Best value: Shanghai on 2026-12-12 at CA$8,000/family."}
{"type":"done"}
```

Cells arrive in **completion order** (whichever future resolves first), not in dep×ret order.
The `dest_index` field maps each cell back to the correct city in `meta.results`.

**Errors** — returned as plain JSON (not streamed):

| Status | Condition |
|--------|-----------|
| 400 | Same validation as `/api/search`: missing/empty `origin`, `destinations`, or dates |
```

- [ ] **Step 2: Verify the docs file looks correct**

```bash
cat /Users/tomwu/Projects/Whenever/docs/API.md | tail -60
```

- [ ] **Step 3: Commit**

```bash
cd /Users/tomwu/Projects/Whenever && source .venv/bin/activate && git add docs/API.md && git commit -m "docs: document POST /api/search/stream NDJSON endpoint"
```

---

### Task 6: Full gate + final report

**Files:**
- Create: `/Users/tomwu/Projects/Whenever/.git/sdd/streaming-report.md`

- [ ] **Step 1: Run the full test gate**

```bash
cd /Users/tomwu/Projects/Whenever && source .venv/bin/activate && pytest --cov=app --cov=watch --cov=scheduler --cov=export --cov-fail-under=99 --cov-report=term-missing -W error::ResourceWarning 2>&1 | tail -40
```

Expected: all PASS, coverage ≥ 99%, no ResourceWarning.

- [ ] **Step 2: Run e2e suite**

```bash
cd /Users/tomwu/Projects/Whenever && source .venv/bin/activate && pytest tests/e2e/ -v 2>&1 | tail -30
```

Expected: all PASS.

- [ ] **Step 3: Write the report**

Create `/Users/tomwu/Projects/Whenever/.git/sdd/streaming-report.md` with the implementation summary, commit SHAs, coverage %, and any concerns.

- [ ] **Step 4: Final squash commit (optional)**

If desired, do a single final commit staging all changed files:

```bash
cd /Users/tomwu/Projects/Whenever && source .venv/bin/activate && git add app.py templates/index.html tests/unit/test_stream.py tests/e2e/test_stream_ui.py docs/API.md && git commit -m "feat: stream search results incrementally (NDJSON) so the grid fills live

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Self-Review Against Spec

**Spec coverage check:**

| Requirement | Task |
|-------------|------|
| `_build_cell` DRY helper extracted from `run_search` | Task 1 |
| `run_search` output unchanged | Task 1 (existing tests verify) |
| `POST /api/search/stream` endpoint | Task 2 |
| 400 path returns JSON (not streamed) | Task 2 (test_stream_400_on_missing_origin) |
| NDJSON: meta → cells → recommendation → done | Task 2 |
| `meta.total_cells` = len(dests)×len(dep_dates)×len(ret_dates) | Task 2 |
| `cell` lines have `dest_index` | Task 2 |
| Cells fetched with ThreadPoolExecutor + as_completed | Task 2 (implementation) |
| `build_recommendation` called after all cells | Task 2 |
| Flask `stream_with_context` used | Task 2 |
| Frontend skeleton on meta | Task 3 |
| Frontend cell-by-cell fill | Task 3 |
| Frontend progress bar | Task 3 |
| Frontend recommendation on `recommendation` | Task 3 |
| Frontend re-enable run on `done` | Task 3 |
| `render()` kept + uses `_cellHtml` | Task 3 |
| E2E: streaming search populates grid + rec | Task 4 |
| E2E: price visible in cell | Task 4 |
| Coverage ≥ 99%, pristine | Task 6 |
| `docs/API.md` updated | Task 5 |
| Report at `.git/sdd/streaming-report.md` | Task 6 |
| Commit message exact | Task 6 |

**Placeholder scan:** None found — all steps have concrete code.

**Type consistency:** `_build_cell` returns the same 9-key dict used in `run_search` and emitted as `cell` lines. `dest_index` is consistently an int. `meta.results[i]` always `{city, iata}`.
