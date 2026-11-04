import { ChangeDetectionStrategy, Component, ElementRef, EventEmitter, Input, Output, ViewChild, signal } from '@angular/core';
import { Observable } from 'rxjs';
import { MarkdownViewComponent } from './markdown-view.component';

/** How the editor stores a picked image; the answer's `url` goes into the markdown. */
export type ImageUploader = (file: File) => Observable<{ url: string; filename?: string }>;

const IMAGE_TYPES = ['image/png', 'image/jpeg', 'image/gif', 'image/webp'];

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
          @if (imageUpload) {
            <button type="button" title="Insert an image" [disabled]="uploading()" (click)="picker.click()">
              {{ uploading() ? 'Uploading…' : 'Image' }}
            </button>
            <input #picker type="file" hidden accept="image/png,image/jpeg,image/gif,image/webp"
                   (change)="pick($any($event.target))" aria-label="Choose an image" />
          } @else if (imageHint) {
            <span class="tn-kb-muted tn-kb-small" role="note">{{ imageHint }}</span>
          }
          <button type="button" class="tn-md-toolbar__toggle" (click)="preview.set(!preview())">
            {{ preview() ? 'Hide preview' : 'Show preview' }}
          </button>
        </div>
        <textarea #area [attr.aria-label]="label" [placeholder]="placeholder" [style.min-height.px]="minHeight"
                  [value]="value" (input)="onInput(area.value)"
                  (paste)="onPaste($event)" (dragover)="onDragOver($event)" (drop)="onDrop($event)"></textarea>
        @if (uploadError()) { <p class="tn-kb-error" role="alert">{{ uploadError() }}</p> }
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
  /** Set to let people add images (paste, drop or the Image button); null hides the button. */
  @Input() imageUpload: ImageUploader | null = null;
  /** Shown instead of the button when images are not possible yet (e.g. the page is not saved). */
  @Input() imageHint = '';
  @Output() valueChange = new EventEmitter<string>();
  @ViewChild('area', { static: true }) area!: ElementRef<HTMLTextAreaElement>;

  readonly actions = ACTIONS;
  readonly preview = signal(true);
  readonly uploading = signal(false);
  readonly uploadError = signal('');

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

  pick(input: HTMLInputElement): void {
    const file = input.files?.[0];
    input.value = '';
    if (file) this.addImage(file);
  }

  onPaste(ev: ClipboardEvent): void {
    const file = Array.from(ev.clipboardData?.files ?? []).find(f => IMAGE_TYPES.includes(f.type));
    if (!file || !this.imageUpload) return;
    ev.preventDefault();
    this.addImage(file);
  }

  onDragOver(ev: DragEvent): void {
    if (this.imageUpload && Array.from(ev.dataTransfer?.items ?? []).some(i => i.kind === 'file')) ev.preventDefault();
  }

  onDrop(ev: DragEvent): void {
    const file = Array.from(ev.dataTransfer?.files ?? []).find(f => IMAGE_TYPES.includes(f.type));
    if (!file || !this.imageUpload) return;
    ev.preventDefault();
    this.addImage(file);
  }

  private addImage(file: File): void {
    if (!this.imageUpload || this.uploading()) return;
    if (!IMAGE_TYPES.includes(file.type)) {
      this.uploadError.set('Use a PNG, JPEG, GIF or WebP image.');
      return;
    }
    this.uploadError.set('');
    this.uploading.set(true);
    const el = this.area.nativeElement;
    const at = el.selectionStart;
    this.imageUpload(file).subscribe({
      next: up => {
        const alt = (up.filename ?? file.name).replace(/\.[^.]+$/, '').replace(/[[\]()]/g, ' ').trim() || 'image';
        const lead = at > 0 && el.value[at - 1] !== '\n' ? '\n' : '';
        const next = `${el.value.slice(0, at)}${lead}![${alt}](${up.url})\n${el.value.slice(at)}`;
        el.value = next;
        this.onInput(next);
        this.uploading.set(false);
      },
      error: err => {
        this.uploading.set(false);
        this.uploadError.set(err?.error?.detail && typeof err.error.detail === 'string'
          ? err.error.detail : 'The image could not be uploaded.');
      },
    });
  }
}
