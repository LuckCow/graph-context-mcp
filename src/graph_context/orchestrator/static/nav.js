/* Shared section navigation for the inspection server's pages.
 *
 * The server hosts several independent single-file pages (the eval
 * dashboard, the turn-log viewer, the prose editor). Each used to carry
 * its own hand-written header links, so every new page meant editing
 * every old one and the set drifted -- the viewer could not reach the
 * prose editor at all. The site map lives here instead, in exactly one
 * place: add a section to SECTIONS and every page grows the link.
 *
 * Usage, in a page's <header>:
 *
 *     <div data-gc-nav="logs"></div>
 *     <script type="module" src="/static/nav.js"></script>
 *
 * ``data-gc-nav`` names the section the page IS (matched against
 * ``key``), which renders as the current item. An optional
 * ``data-home`` overrides where that current item points -- hash-routed
 * pages pass their in-page root (``#/``) so "go back to the top of this
 * section" stays a hash change instead of a document reload.
 *
 * Styles are injected from here too: the pages keep their own inline
 * CSS (the turn-log viewer is documented as openable straight off the
 * filesystem, where an external stylesheet would not load and would
 * leave the page unstyled), so the nav ships the few rules it needs and
 * borrows the palette variables every page already defines. The src is
 * ABSOLUTE because the viewer is also served under ``/runs/<id>/log``,
 * where a relative path would miss; opened as a bare file:// document it
 * simply does not load and the page renders without nav, which is the
 * honest outcome -- there is no server to navigate to.
 */

export const SECTIONS = [
  { key: "inspection", href: "/", label: "Inspection" },
  { key: "logs", href: "/logs", label: "Turn log" },
  { key: "prose", href: "/prose", label: "Prose" },
];

export const BRAND = "graph-context";

const STYLE_ID = "gc-nav-style";
const CSS = `
.gc-nav { display: inline-flex; align-items: center; flex-wrap: wrap;
  gap: 2px; margin-right: 10px; }
.gc-nav .gc-brand { font-size: 13px; font-weight: 650; color: var(--ink);
  margin-right: 8px; white-space: nowrap; }
.gc-nav a { font: inherit; font-size: 13px; line-height: 1.4;
  padding: 3px 9px; border-radius: 999px; white-space: nowrap;
  color: var(--muted); text-decoration: none; }
.gc-nav a:hover { color: var(--ink); background: var(--chip);
  text-decoration: none; }
.gc-nav a[aria-current="page"] { color: var(--accent); background: var(--chip);
  font-weight: 600; }
`;

function injectStyles(doc) {
  if (doc.getElementById(STYLE_ID)) return;
  const style = doc.createElement("style");
  style.id = STYLE_ID;
  style.textContent = CSS;
  doc.head.appendChild(style);
}

/** Render the section links into one host element, marking `current`. */
export function renderNav(host) {
  const current = host.dataset.gcNav || "";
  const home = host.dataset.home || "";
  host.textContent = "";
  host.classList.add("gc-nav");
  const brand = host.ownerDocument.createElement("span");
  brand.className = "gc-brand";
  brand.textContent = BRAND;
  host.appendChild(brand);
  const nav = host.ownerDocument.createElement("nav");
  nav.setAttribute("aria-label", "Sections");
  nav.style.display = "contents";
  for (const section of SECTIONS) {
    const link = host.ownerDocument.createElement("a");
    const active = section.key === current;
    link.href = active && home ? home : section.href;
    link.textContent = section.label;
    if (active) link.setAttribute("aria-current", "page");
    nav.appendChild(link);
  }
  host.appendChild(nav);
}

injectStyles(document);
for (const host of document.querySelectorAll("[data-gc-nav]")) renderNav(host);
