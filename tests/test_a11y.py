#!/usr/bin/env python3
"""Structural accessibility checks for the web dashboard (stdlib only).

    python tests/run_all.py

Findings UX-P3-003 / MOB-P3-003: the suite only matched source strings, so a
structural accessibility regression in the web UI (a dropped ``role``, a missing
``alt``, a duplicated ``id``) could ship unnoticed. This module parses
``server/web/index.html`` with :mod:`html.parser` and asserts the invariants the
dashboard must keep, and scans ``server/web/app.js`` for the attributes it sets on
the markup it generates (chart canvases, the runner panel's ``role="button"``).

Checked automatically:
  * ``<html lang>`` is set
  * every ``<img>`` has a non-empty ``alt``
  * every ``<button>`` / ``[role=button]`` has text or an accessible name
  * form controls inside a form/fieldset/.formrow have an accessible name
  * the modal carries ``role="dialog"`` + ``aria-modal``
  * the tab strip is ``role="tablist"`` with ``role="tab"`` children
  * an ``aria-live`` region exists
  * every ``<canvas>`` has an accessible alternative (``role``/``aria-label``)
  * no duplicate ``id`` values
  * the viewport meta opts into ``viewport-fit=cover``

Colour contrast, screen-reader flows, reduced motion and touch-target sizing need a
real browser / assistive tech and are listed in ``docs/ACCESSIBILITY.md``.
"""
import os
import re
from html.parser import HTMLParser

from harness import Checker, REPO

WEB = os.path.join(REPO, "server", "web")
INDEX = os.path.join(WEB, "index.html")
APPJS = os.path.join(WEB, "app.js")

# HTML void elements are recorded but never pushed on the open-element stack.
VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link",
        "meta", "param", "source", "track", "wbr"}
# A control inside one of these is expected to carry its own accessible name.
FORM_CONTEXT_TAGS = ("form", "fieldset")
FORM_CONTEXT_CLASSES = ("formrow", "chk")
CONTROL_TAGS = ("input", "select", "textarea")


class _Doc(HTMLParser):
    """Collect every start tag (with its source line and parent) and its text."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []          # open elements, innermost last
        self.elements = []       # document order
        self.ids = []            # (id, line)

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v if v is not None else "") for k, v in attrs}
        el = {"tag": tag, "attrs": a, "line": self.getpos()[0],
              "parent": self.stack[-1] if self.stack else None,
              "text": [], "_doc": self}
        self.elements.append(el)
        if a.get("id"):
            self.ids.append((a["id"], el["line"]))
        if tag not in VOID:
            self.stack.append(el)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID and self.stack and self.stack[-1]["tag"] == tag:
            self.stack.pop()

    def handle_data(self, data):
        # a data run belongs to every element currently open, so an ancestor's
        # text includes the text of its inline children (a <button> with a <span>)
        for frame in self.stack:
            frame["text"].append(data)

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i]["tag"] == tag:
                del self.stack[i:]
                return


def _text(el):
    return "".join(el["text"]).strip()


def _explicit_name(el):
    """The element's own accessible name from ARIA/title, or ''."""
    a = el["attrs"]
    return (a.get("aria-label") or a.get("aria-labelledby") or a.get("title") or "").strip()


def _has_class(el, name):
    return name in (el["attrs"].get("class") or "").split()


def _ancestor(el, pred):
    p = el.get("parent")
    while p is not None:
        if pred(p):
            return p
        p = p.get("parent")
    return None


def _is_descendant(el, ancestor):
    return _ancestor(el, lambda p: p is ancestor) is not None


def _preceding_label(el):
    """The nearest <label> that is a previous sibling of ``el`` (or None)."""
    parent = el.get("parent")
    if parent is None:
        return None
    found = None
    for e in el["_doc"].elements:
        if e is el:
            break
        if e.get("parent") is parent and e["tag"] == "label":
            found = e
    return found


def _labelled(el, by_for):
    """A form control is named by aria/title/placeholder or an associated label."""
    if _explicit_name(el) or (el["attrs"].get("placeholder") or "").strip():
        return True
    if _ancestor(el, lambda p: p["tag"] == "label"):          # wrapping <label>
        return True
    fid = el["attrs"].get("id")
    if fid and (by_for.get(fid) or "").strip():                # <label for=id>
        return True
    lab = _preceding_label(el)                                 # <label> sibling
    return lab is not None and bool(_text(lab))


def _in_form_context(el):
    if _ancestor(el, lambda p: p["tag"] in FORM_CONTEXT_TAGS):
        return True
    return _ancestor(el, lambda p: any(_has_class(p, c) for c in FORM_CONTEXT_CLASSES)) is not None


def _line_of(js, pos):
    return js.count("\n", 0, pos) + 1


def _js_controls(js):
    """Text-entry controls the page builds in app.js that lack a visible/ARIA name.

    ``<select>`` elements are left alone: their inline ``<option>`` text names the
    choice, and the filter/chart pickers take their context from the heading or
    chart beside them (docs/ACCESSIBILITY.md).
    """
    out = []
    for m in re.finditer(r"<(input|textarea)\b[^>]*>", js):
        tag = m.group(0)
        if re.search(r'type\s*=\s*["\']?(checkbox|radio|hidden)', tag, re.I):
            continue                                           # labelled by a wrapping <label>
        if re.search(r"\b(aria-label|aria-labelledby|title|placeholder)\s*=", tag, re.I):
            continue
        if re.search(r"<label\b", js[max(0, m.start() - 700):m.start()], re.I):
            continue
        out.append((_line_of(js, m.start()), tag[:90]))
    return out


def run():
    c = Checker("a11y")
    html = open(INDEX, encoding="utf-8").read()
    js = open(APPJS, encoding="utf-8").read()
    doc = _Doc()
    doc.feed(html)
    by_for = {}
    for el in doc.elements:
        if el["tag"] == "label" and el["attrs"].get("for"):
            by_for[el["attrs"]["for"]] = _text(el)

    violations = []

    def bad(location, el, detail):
        if el is None:
            msg = "%s  %s" % (location, detail)
        else:
            where = "<%s%s>" % (el["tag"],
                                (' id="%s"' % el["attrs"]["id"]) if el["attrs"].get("id") else "")
            msg = "%s:%d  %s  %s" % (location, el["line"], where, detail)
        violations.append(msg)
        print("  VIOLATION " + msg)

    # ---- document language ------------------------------------------------
    html_el = next((e for e in doc.elements if e["tag"] == "html"), None)
    c.check("<html> declares a language",
            bool(html_el and html_el["attrs"].get("lang", "").strip()),
            html_el and html_el["attrs"].get("lang"))
    if html_el and not html_el["attrs"].get("lang", "").strip():
        bad("index.html", html_el, 'missing non-empty lang=""')

    # ---- image alternatives ----------------------------------------------
    imgs = [e for e in doc.elements if e["tag"] == "img"]
    for e in imgs:
        if not e["attrs"].get("alt", "").strip():
            bad("index.html", e, 'missing non-empty alt=""')
    c.check("every <img> has a non-empty alt (%d img)" % len(imgs),
            all(e["attrs"].get("alt", "").strip() for e in imgs), "")

    # ---- buttons have a name ---------------------------------------------
    buttons = [e for e in doc.elements
               if e["tag"] == "button" or e["attrs"].get("role") == "button"]
    unnamed = [e for e in buttons if not (_text(e) or _explicit_name(e))]
    for e in unnamed:
        bad("index.html", e, "button has no text and no aria-label")
    js_role_buttons = [m.start() for m in re.finditer(r'role\s*=\s*["\']button["\']', js)]
    js_button_bad = [p for p in js_role_buttons if "aria-label" not in js[p:p + 300]]
    for p in js_button_bad:
        msg = "app.js:%d  role=button without aria-label" % _line_of(js, p)
        violations.append(msg)
        print("  VIOLATION " + msg)
    c.check("every <button>/[role=button] has text or an aria-label (%d buttons)"
            % len(buttons), not unnamed and not js_button_bad, "")

    # ---- form controls have an accessible name ----------------------------
    controls = [e for e in doc.elements
                if e["tag"] in CONTROL_TAGS and e["attrs"].get("type", "").lower() != "hidden"]
    form_controls = [e for e in controls if _in_form_context(e)]
    unlabelled = [e for e in form_controls if not _labelled(e, by_for)]
    for e in unlabelled:
        bad("index.html", e, "form control inside a form/formrow has no label/aria-label")
    js_unlabelled = _js_controls(js)
    for line, tag in js_unlabelled:
        msg = "app.js:%d  form control without label/aria-label: %s" % (line, tag)
        violations.append(msg)
        print("  VIOLATION " + msg)
    # standalone filter selects take their context from the section heading; they
    # are surfaced as a manual check rather than a hard failure (docs/ACCESSIBILITY.md)
    loose = [e for e in controls if e not in form_controls and not _labelled(e, by_for)]
    for e in loose:
        print("  NOTE      index.html:%d  <%s id=%r> relies on the surrounding heading "
              "(manual check)" % (e["line"], e["tag"], e["attrs"].get("id")))
    c.check("form controls in a form/fieldset/.formrow have a label or aria-label (%d)"
            % len(form_controls), not unlabelled and not js_unlabelled, "")

    # ---- modal dialog semantics ------------------------------------------
    modal = next((e for e in doc.elements if e["attrs"].get("id") == "modal"), None)
    js_modal = ('setAttribute("role", "dialog")' in js
                and 'setAttribute("aria-modal"' in js)
    modal_ok = bool(modal and modal["attrs"].get("role") == "dialog"
                    and modal["attrs"].get("aria-modal", "").strip()) or js_modal
    if not modal_ok:
        bad("index.html", modal, 'modal missing role="dialog" + aria-modal')
    c.check("the modal carries role=dialog + aria-modal", modal_ok, "")

    # ---- tab strip semantics ---------------------------------------------
    tablists = [e for e in doc.elements if e["attrs"].get("role") == "tablist"]
    tabs_ok = any(
        len([e for e in doc.elements
             if e["attrs"].get("role") == "tab" and _is_descendant(e, tl)]) >= 2
        for tl in tablists)
    if not tabs_ok and 'role="tablist"' in js and re.search(r'role\s*=\s*["\']tab["\']', js):
        tabs_ok = True
    if not tabs_ok:
        nav = next((e for e in doc.elements if e["tag"] == "nav"), None)
        bad("index.html", nav, 'tab strip missing role="tablist" with role="tab" children')
    c.check('the tab strip is role=tablist with role=tab children', tabs_ok, "")

    # ---- a live region exists --------------------------------------------
    live = [e for e in doc.elements
            if (e["attrs"].get("aria-live") or "").strip()
            or e["attrs"].get("role") in ("status", "alert", "log")]
    live_ok = bool(live) or "aria-live" in js
    if not live_ok:
        bad("index.html", None, "no aria-live region for the live SSE updates")
    c.check("a live region (aria-live) exists", live_ok, "")

    # ---- canvas alternatives ---------------------------------------------
    canvases = [e for e in doc.elements if e["tag"] == "canvas"]
    static_ok = all(e["attrs"].get("role") and _explicit_name(e) for e in canvases)
    js_canvas = ('setAttribute("role", "img")' in js
                 and 'setAttribute("aria-label"' in js)
    canvas_ok = static_ok or js_canvas
    if not canvas_ok:
        for e in canvases:
            bad("index.html", e, 'canvas missing role/aria-label (and app.js sets neither)')
    c.check("every <canvas> has an accessible alternative (%d canvas)" % len(canvases),
            canvas_ok, "")

    # ---- unique ids -------------------------------------------------------
    seen, dupes = {}, []
    for idv, line in doc.ids:
        if idv in seen:
            dupes.append((idv, line, seen[idv]))
            msg = "index.html:%d  duplicate id=%r (first at line %d)" % (line, idv, seen[idv])
            violations.append(msg)
            print("  VIOLATION " + msg)
        else:
            seen[idv] = line
    c.check("no duplicate id values (%d ids)" % len(doc.ids), not dupes, dupes)

    # ---- safe-area viewport ----------------------------------------------
    vp = next((e for e in doc.elements
               if e["tag"] == "meta" and e["attrs"].get("name") == "viewport"), None)
    vp_ok = bool(vp and "viewport-fit=cover" in vp["attrs"].get("content", ""))
    if not vp_ok:
        bad("index.html", vp, "viewport meta missing viewport-fit=cover")
    c.check("the viewport meta opts into viewport-fit=cover", vp_ok, "")

    c.check("no structural accessibility violations", not violations,
            "%d violation(s)" % len(violations))
    return c.fails


if __name__ == "__main__":
    import sys
    sys.exit(1 if run() else 0)
