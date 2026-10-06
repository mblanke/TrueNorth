import { ChangeDetectionStrategy, Component, ViewEncapsulation } from '@angular/core';

/**
 * The flat look shared by the Wiki and Support pages (`.tn-kb` on the page root),
 * plus markdown rendering (`.tn-md`) and the markdown editor. Rendered once by each
 * of those pages; with encapsulation off Angular adds the styles to the document the
 * first time, so the pages' own component styles stay tiny. Built only from theme
 * tokens so every runtime theme gets it.
 */
@Component({
  selector: 'tn-kb-styles',
  standalone: true,
  template: '',
  encapsulation: ViewEncapsulation.None,
  changeDetection: ChangeDetectionStrategy.OnPush,
  styles: [`
    .tn-kb { padding: 24px 28px; max-width: 1280px; color: var(--text-primary); line-height: 1.55; }
    .tn-kb h1 { font-size: 1.6rem; font-weight: 600; letter-spacing: -.3px; margin: 2px 0 4px; }
    .tn-kb h2 { font-size: 1rem; font-weight: 600; margin: 0 0 10px; }
    .tn-kb-head { display: flex; justify-content: space-between; align-items: flex-start; gap: 12px; flex-wrap: wrap; margin-bottom: 18px; }
    .tn-kb-actions { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
    .tn-kb-muted { color: var(--text-secondary); }
    .tn-kb-small { font-size: .82rem; }
    .tn-kb-crumbs { font-size: .8rem; color: var(--text-secondary); margin-bottom: 2px; }
    .tn-kb-crumbs a, .tn-kb a.tn-kb-link { color: var(--accent); text-decoration: none; cursor: pointer; }
    .tn-kb-crumbs a:hover, .tn-kb a.tn-kb-link:hover { text-decoration: underline; }
    .tn-kb-panel { background: var(--bg-card); border: 1px solid var(--border); border-radius: var(--radius-md, 8px); padding: 16px; min-width: 0; }
    .tn-kb-panel + .tn-kb-panel { margin-top: 14px; }
    .tn-kb-row { display: flex; justify-content: space-between; align-items: center; gap: 12px; padding: 11px 0; border-top: 1px solid var(--border); }
    .tn-kb-row:first-child { border-top: 0; padding-top: 0; }
    .tn-kb-row:last-child { padding-bottom: 0; }
    .tn-kb-row p { margin: 2px 0 0; }
    .tn-kb-tag { display: inline-block; font-size: .72rem; padding: 0 7px; border-radius: 4px; border: 1px solid var(--border); color: var(--text-secondary); white-space: nowrap; line-height: 1.6; }
    .tn-kb-tag.critical { border-color: var(--alert); color: var(--alert); }
    .tn-kb-tag.high { border-color: var(--warning); color: var(--warning); }
    .tn-kb-key { font-family: var(--font-mono, ui-monospace, monospace); font-size: .78rem; color: var(--text-secondary); white-space: nowrap; }
    .tn-kb-split { display: grid; grid-template-columns: 240px minmax(0, 1fr); gap: 18px; align-items: start; }
    .tn-kb-detail { display: grid; grid-template-columns: minmax(0, 1fr) 310px; gap: 18px; align-items: start; }
    .tn-kb-table { width: 100%; border-collapse: collapse; font-size: .88rem; }
    .tn-kb-table th { text-align: left; font-weight: 500; color: var(--text-secondary); font-size: .78rem; }
    .tn-kb-table th, .tn-kb-table td { padding: 10px 8px; border-bottom: 1px solid var(--border); vertical-align: top; }
    .tn-kb-table tr:last-child td { border-bottom: 0; }
    .tn-kb-tabs { display: flex; gap: 2px; border-bottom: 1px solid var(--border); margin-bottom: 14px; flex-wrap: wrap; }
    .tn-kb-tabs button { background: none; border: 0; border-bottom: 2px solid transparent; margin-bottom: -1px; padding: 7px 12px; color: var(--text-secondary); font: inherit; cursor: pointer; }
    .tn-kb-tabs button[aria-pressed="true"] { color: var(--accent); border-bottom-color: var(--accent); }
    .tn-kb-field { display: block; font-size: .78rem; color: var(--text-secondary); margin: 12px 0 4px; }
    .tn-kb input.tn-kb-input, .tn-kb select.tn-kb-input, .tn-kb textarea.tn-kb-input {
      width: 100%; box-sizing: border-box; font: inherit; padding: 8px 10px; border: 1px solid var(--border);
      border-radius: 6px; background: var(--bg-input, var(--bg-card)); color: var(--text-primary);
    }
    .tn-kb-note { border-left: 3px solid var(--accent); background: var(--accent-muted, transparent); padding: 10px 12px; margin: 12px 0; font-size: .88rem; }
    .tn-kb-error { border-left: 3px solid var(--alert); padding: 10px 12px; margin: 12px 0; font-size: .88rem; }
    .tn-kb-tree a { display: block; padding: 4px 8px; border-radius: 5px; color: var(--text-primary); text-decoration: none; cursor: pointer; font-size: .88rem; }
    .tn-kb-tree a:hover { background: var(--accent-muted, rgba(0,0,0,.04)); }
    .tn-kb-tree a.active { color: var(--accent); background: var(--accent-muted, transparent); font-weight: 600; }
    .tn-kb-tree a.draft { color: var(--text-muted); font-style: italic; }
    .tn-kb-tree .kids { margin-left: 12px; border-left: 1px solid var(--border); padding-left: 4px; }
    .tn-kb-comment { padding: 12px 0; border-top: 1px solid var(--border); }
    .tn-kb-comment:first-child { border-top: 0; padding-top: 0; }
    .tn-kb-comment.internal { border: 1px solid var(--warning); border-radius: 6px; padding: 10px 12px; margin: 10px 0; background: var(--warn-soft, transparent); }
    .tn-kb-kv { display: grid; grid-template-columns: 76px minmax(0, 1fr); gap: 10px; align-items: center; font-size: .88rem; }
    .tn-kb-kv dt { color: var(--text-secondary); }
    .tn-kb-kv dd { margin: 0; min-width: 0; }
    .tn-kb-board { display: grid; grid-template-columns: repeat(5, minmax(190px, 1fr)); gap: 10px; overflow-x: auto; padding-bottom: 6px; }
    .tn-kb-col { background: var(--bg-secondary, rgba(0,0,0,.03)); border-radius: 8px; padding: 8px; min-height: 360px; }
    .tn-kb-col h2 { font-size: .74rem; text-transform: uppercase; letter-spacing: 1px; color: var(--text-secondary); margin: 4px 4px 8px; }
    .tn-kb-card { display: block; background: var(--bg-card); border: 1px solid var(--border); border-radius: 6px; padding: 9px 10px; margin-bottom: 8px; font-size: .86rem; cursor: grab; color: var(--text-primary); text-decoration: none; }
    .tn-kb-card .meta { display: flex; justify-content: space-between; gap: 6px; margin-top: 6px; font-size: .74rem; color: var(--text-secondary); }
    .tn-kb-card.cdk-drag-preview { box-shadow: 0 6px 18px rgba(0,0,0,.18); }
    .tn-kb-card.cdk-drag-placeholder { opacity: .35; }

    /* contain: paint — nothing user-written can draw outside its own box. */
    .tn-md { overflow-wrap: anywhere; contain: paint; }
    .tn-md > :first-child { margin-top: 0; }
    .tn-md > :last-child { margin-bottom: 0; }
    .tn-md h1, .tn-md h2, .tn-md h3 { margin: 1.2em 0 .4em; font-weight: 600; line-height: 1.3; }
    .tn-md h1 { font-size: 1.35rem; } .tn-md h2 { font-size: 1.15rem; } .tn-md h3 { font-size: 1rem; }
    .tn-md p, .tn-md ul, .tn-md ol, .tn-md pre, .tn-md table, .tn-md blockquote { margin: 0 0 .8em; }
    .tn-md a { color: var(--accent); }
    .tn-md code { font-family: var(--font-mono, ui-monospace, monospace); font-size: .85em; background: var(--bg-secondary, rgba(0,0,0,.05)); padding: 1px 4px; border-radius: 3px; }
    .tn-md pre { background: var(--bg-secondary, rgba(0,0,0,.05)); padding: 10px 12px; border-radius: 6px; overflow-x: auto; }
    .tn-md pre code { background: none; padding: 0; }
    .tn-md blockquote { border-left: 3px solid var(--border); padding-left: 12px; color: var(--text-secondary); }
    .tn-md table { border-collapse: collapse; }
    .tn-md th, .tn-md td { border: 1px solid var(--border); padding: 5px 9px; }
    .tn-md img { max-width: 100%; }

    .tn-md-editor { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
    .tn-md-editor.preview-off { grid-template-columns: 1fr; }
    .tn-md-editor textarea { width: 100%; box-sizing: border-box; resize: vertical; padding: 10px; border: 1px solid var(--border); border-radius: 6px;
      background: var(--bg-input, var(--bg-card)); color: var(--text-primary); font: .85rem/1.5 var(--font-mono, ui-monospace, monospace); }
    .tn-md-editor__preview { border: 1px dashed var(--border); border-radius: 6px; padding: 10px 12px; min-width: 0; overflow: auto; }
    .tn-md-toolbar { display: flex; gap: 4px; flex-wrap: wrap; margin-bottom: 6px; }
    .tn-md-toolbar button { border: 1px solid var(--border); background: var(--bg-card); color: var(--text-primary); border-radius: 4px; padding: 2px 8px; font-size: .78rem; cursor: pointer; }
    .tn-md-toolbar__toggle { margin-left: auto; }

    @media (max-width: 860px) {
      .tn-kb { padding: 16px; }
      .tn-kb-split, .tn-kb-detail, .tn-md-editor { grid-template-columns: 1fr; }
    }
  `],
})
export class KbStylesComponent {}
