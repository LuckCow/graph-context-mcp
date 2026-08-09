# Vendored CodeMirror 6 bundle

`src/graph_context/orchestrator/static/codemirror.bundle.js` is a prebuilt,
minified ESM bundle of CodeMirror 6, consumed by `prose.html` via
`import * as CM from "/static/codemirror.bundle.js"`. It is checked into the
repo so the inspection server stays dependency-free at runtime (ADR 054
amends ADR 025's no-library rule: no build step lives *in the repo or CI* —
the artifact is rebuilt manually with the commands below only when upgrading).

## Rebuild (one-off, needs npm registry access)

```bash
mkdir /tmp/cm-build && cd /tmp/cm-build && npm init -y
npm install --no-audit --no-fund \
    codemirror@6 @codemirror/state @codemirror/view @codemirror/language \
    @codemirror/commands @codemirror/lang-markdown @lezer/highlight esbuild
cp <repo>/scripts/vendor/codemirror/entry.mjs .
npx esbuild entry.mjs --bundle --format=esm --minify \
    --outfile=codemirror.bundle.js
cp codemirror.bundle.js \
    <repo>/src/graph_context/orchestrator/static/codemirror.bundle.js
```

Then refresh the version list in
`src/graph_context/orchestrator/static/codemirror.bundle.LICENSE`
(`npm ls --all` inside the build dir shows the resolved tree; every package
is MIT) and sanity-check the exports:

```bash
node --input-type=module -e "
import * as cm from '/tmp/cm-build/codemirror.bundle.js';
console.log(typeof cm.EditorView, typeof cm.EditorState);"
```

`entry.mjs` is the module surface — everything `prose.html` uses must be
re-exported there. Adding an export means rebuilding the bundle.
