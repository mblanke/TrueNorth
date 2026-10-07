import { ChangeDetectionStrategy, Component, ElementRef, EventEmitter, Input, Output, ViewChild, signal } from '@angular/core';
import { MarkdownViewComponent } from './markdown-view.component';

interface ToolbarAction {
  label: string;
  title: string;
  before: string;
  after?: string;
  placeholder: string;
  block?: boolean;
}

const ACTIONS: ToolbarAction[] = [
  { label: 'B', title: 'Bold', before: '**', after: '**', placeholder: 'bold text' },
  { label: 'I', title: 'Italic', before: '_', after: '_', placeholder: 'italic text' },
  { label: 'H2', title: 'Heading', before: '## ', placeholder: 'Heading', block: true },
  { label: 'List', title: 'Bulleted list', before: '- ', placeholder: 'item', block: true },
  { label: 'Code', title: 'Code', before: '`', after: '`', placeholder: 'code' },
  { label: 'Link', title: 'Link', before: '[', after: '](https://)', placeholder: 'link text' },
  {
    label: 'Table', title: 'Table', block: true, placeholder: '',
    before: '| Column | Column |\n| --- | --- |\n| Cell | Cell |\n',
  },
];

/**
 * Plain markdown textarea with a small toolbar and a live preview beside it
 * (stacked on narrow screens). Two-way bind with `[(value)]`.
 */
@Component({
  selector: 'tn-markdown-editor',
  standalone: true,
  imports: [MarkdownViewComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <div class="tn-md-editor" [class.preview-off]="!preview()">
      <div class="tn-md-editor__input">
        <div class="tn-md-toolbar" role="toolbar" aria-label="Formatting">
          @for (a of actions; track a.label) {
            <button type="button" [title]="a.title" (click)="apply(a)">{{ a.label }}</button>
          }
          <button type="button" class="tn-md-toolbar__toggle" (click)="preview.set(!preview())">
            {{ preview() ? 'Hide preview' : 'Show preview' }}
          </button>
        </div>
        <textarea #area [attr.aria-label]="label" [placeholder]="placeholder" [style.min-height.px]="minHeight"
                  [value]="value" (input)="onInput(area.value)"></textarea>
      </div>
      @if (preview()) {
        <div class="tn-md-editor__preview" aria-label="Preview">
          <span class="tn-kb-muted tn-kb-small">Preview</span>
          <tn-markdown-view [source]="value" />
        </div>
      }
    </div>
  `,
})
export class MarkdownEditorComponent {
  @Input() value = '';
  @Input() label = 'Text';
  @Input() placeholder = '';
  @Input() minHeight = 260;
  @Input() set showPreview(v: boolean) { this.preview.set(v); }
  @Output() valueChange = new EventEmitter<string>();
  @ViewChild('area', { static: true }) area!: ElementRef<HTMLTextAreaElement>;

  readonly actions = ACTIONS;
  readonly preview = signal(true);

  onInput(v: string): void {
    this.value = v;
    this.valueChange.emit(v);
  }

  apply(a: ToolbarAction): void {
    const el = this.area.nativeElement;
    const { selectionStart: s, selectionEnd: e, value } = el;
    const selected = value.slice(s, e) || a.placeholder;
    const lead = a.block && s > 0 && value[s - 1] !== '\n' ? '\n' : '';
    const insert = `${lead}${a.before}${selected}${a.after ?? ''}`;
    const next = value.slice(0, s) + insert + value.slice(e);
    el.value = next;
    this.onInput(next);
    const cursor = s + lead.length + a.before.length;
    el.focus();
    el.setSelectionRange(cursor, cursor + selected.length);
  }
}
