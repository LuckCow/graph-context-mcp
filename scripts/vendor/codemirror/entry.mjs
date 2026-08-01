// Re-export surface for the vendored CodeMirror 6 bundle used by prose.html.
// Rebuild instructions: scripts/vendor/codemirror/README.md
export {
  EditorState,
  StateField,
  StateEffect,
  RangeSetBuilder,
  RangeSet,
  Compartment,
  EditorSelection,
} from "@codemirror/state";
export {
  EditorView,
  Decoration,
  WidgetType,
  keymap,
  placeholder,
  drawSelection,
  highlightSpecialChars,
} from "@codemirror/view";
export {
  defaultKeymap,
  history,
  historyKeymap,
  undo,
  redo,
} from "@codemirror/commands";
export { markdown, markdownLanguage } from "@codemirror/lang-markdown";
export { syntaxHighlighting, HighlightStyle } from "@codemirror/language";
export { tags } from "@lezer/highlight";
