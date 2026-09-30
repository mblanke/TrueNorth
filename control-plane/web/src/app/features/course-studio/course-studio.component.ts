import { Component, DestroyRef, ElementRef, OnInit, ViewChild, computed, inject, signal } from '@angular/core';
import { HttpErrorResponse } from '@angular/common/http';
import { DomSanitizer, SafeHtml } from '@angular/platform-browser';
import { NgTemplateOutlet } from '@angular/common';
import { RouterLink } from '@angular/router';
import { AuthService } from '@core/services/auth.service';
import {
  Message, RunDetail, RunSummary, canReply, pageRef, pollMs, primaryAction, runnerLooksIdle, shortTime, stageDots,
} from '@core/arc2/studio';
import { CourseStudioService } from './course-studio.service';

type Tab = 'files' | 'outline' | 'quiz' | 'preview' | 'code' | 'lab' | 'validation';
interface Draft { name: string }

/** Deployment marking for the banners. Set per deployment; the Studio never infers it. */
const MARKING = 'Dynamic page · highest possible classification: UNCLASSIFIED (set per deployment)';

/**
 * ARC² Course Studio: Beau Farley's ARC² layout (projects | pipeline chat | workspace
 * tabs | agent bar) in TrueNorth's colours. Send starts stage 1 through the host runner;
 * Accept and feedback resume /arc2 at the outline and preview reviews.
 */
@Component({
  selector: 'tn-course-studio',
  standalone: true,
  imports: [NgTemplateOutlet, RouterLink],
  template: `
    <div class="banner">{{ marking }}</div>
    <div class="app">
      <!-- Projects -->
      <aside class="side" aria-label="Projects">
        <div class="brand"><div class="logo" aria-hidden="true">A²</div><h1>ARC²</h1><p>AI Rapid Course Creator</p></div>
        <div class="seg" role="tablist">
          <button type="button" aria-pressed="true">Projects</button>
          <button type="button" disabled title="Later: watch runs read-only">Observe</button>
          <button type="button" disabled title="Later: who may author, approve and publish">Access</button>
        </div>
        <form class="newp" (submit)="newProject($event)">
          <input class="input" #npName placeholder="New project name…" aria-label="New project name" maxlength="120">
          <button type="submit" class="btn-new">+ New Project</button>
        </form>
        <div class="projects">
          @if (draft(); as d) {
            <button type="button" class="proj" [attr.aria-current]="!selected()" (click)="selectDraft()">
              <strong>{{ d.name }}</strong><span>Describe the course</span><small>Not started</small>
            </button>
          }
          @for (r of runs(); track r.slug) {
            <button type="button" class="proj" [attr.aria-current]="selected() === r.slug" (click)="select(r.slug)">
              <strong>{{ r.name }}</strong><span>{{ r.phase_text }}</span>
              <small>{{ time(r.updated_at) }}{{ r.code ? ' · ' + r.code : '' }}</small>
            </button>
          } @empty {
            @if (!draft()) { <p class="small muted pad">No courses yet. Name one above to start.</p> }
          }
          @if (listError()) { <p class="small err pad">Couldn’t load projects. <button type="button" class="linkbtn" (click)="refreshList()">Try again</button></p> }
        </div>
        <div class="side-foot">
          <a routerLink="/authoring" class="linkbtn">← TrueNorth</a>
          <button type="button" class="linkbtn" (click)="auth.logout()">Sign Out</button>
        </div>
      </aside>

      <!-- Pipeline chat -->
      <section class="chat" aria-label="Pipeline">
        <div class="chat-mode"><span aria-current="true">Standard</span><span class="off" title="Seen in ARC²; not in this version">Essentials</span></div>
        <div class="msgs" #msgs aria-live="polite">
          @if (!selected() && draft(); as d) {
            <div class="msg user">{{ d.name }}</div>
            <div class="msg pipe"><b>PIPELINE</b>Project created. Describe the course below: audience, length, level, topic and limits
              (for example “SOC tier-1 analysts, 4 h, beginner, self-paced; triaging suspicious logins; no live malware”).</div>
          }
          @for (m of messages(); track $index) {
            @if (m.role === 'user') { <div class="msg user">{{ m.text }}<time>{{ time(m.ts) }}</time></div> }
            @else { <div class="msg pipe" [class.warn]="m.error"><b>{{ m.error ? 'PIPELINE · STOPPED' : 'PIPELINE' }}</b><span class="pre">{{ m.text }}</span><time>{{ time(m.ts) }}</time></div> }
          }
          @if (run(); as r) {
            @if (r.phase === 'queued' || r.phase === 'running') {
              <div class="msg pipe live"><b>PIPELINE</b>{{ r.phase_text }}…</div>
            }
            @if (idle()) {
              <div class="msg pipe warn"><b>RUNNER</b>Nothing has picked this up yet. Start the ARC² runner on this machine:
                <code>make arc2-runner</code></div>
            }
          }
          @if (note(); as n) { <div class="msg pipe warn"><b>NOT SENT</b>{{ n }}</div> }
        </div>
        <div class="approve">
          <button type="button" class="btn-approve" [disabled]="!action().enabled || busy()" (click)="accept()">{{ action().label }}</button>
          <p>{{ action().hint }}</p>
        </div>
        <form class="send" (submit)="send($event)">
          <input class="input" #box [placeholder]="selected() ? 'Describe what needs to be changed…' : 'Audience, length, level, topic, limits…'"
                 aria-label="Message to ARC²" maxlength="4000" [disabled]="!canSend()">
          <button type="submit" class="btn-send" [disabled]="!canSend() || busy()">Send</button>
        </form>
      </section>

      <!-- Workspace -->
      <section class="work" aria-label="Workspace">
        <nav class="tabs" aria-label="Course views">
          @for (t of tabs; track t[0]) {
            <button type="button" [attr.aria-pressed]="tab() === t[0]" (click)="setTab(t[0])">{{ t[1] }}
              @if (t[0] === 'validation' && (run()?.actions_open ?? 0) > 0) { <span class="n">{{ run()!.actions_open }}</span> }
            </button>
          }
        </nav>
        @if (run(); as r) {
          <div class="workhead">
            <div><h2>{{ heads[tab()] }}</h2><p>{{ r.name }} · {{ r.phase_text }}</p></div>
            <div class="toolbar">
              @if ((tab() === 'preview' && r.phase === 'preview') || (tab() === 'outline' && r.phase === 'outline')) {
                <button type="button" class="btn-out" (click)="accept()" [disabled]="busy()">{{ tab() === 'outline' ? 'Accept Outline' : 'Approve & Package' }}</button>
                <span class="hint">or describe changes in chat</span>
              }
              @if (r.phase === 'packaged' || r.package_ready) {
                <button type="button" class="btn-out" (click)="download(r.slug)">Download cmi5 ZIP</button>
              }
            </div>
          </div>
          @if (tab() === 'preview' && r.pages.length) {
            <div class="chips">
              @for (p of r.pages; track p) {
                <button type="button" class="chip" [attr.aria-pressed]="page() === p" (click)="openPage(p)">{{ pageLabel(p) }}</button>
              }
            </div>
          }
          <div class="body">
            @switch (tab()) {
              @case ('outline') {
                @if (r.outline?.modules?.length) {
                  <div class="grid2"><div>
                    @for (m of r.outline!.modules!; track m.id; let i = $index) {
                      <div class="card"><h3>Module {{ i + 1 }} · {{ m.title }}</h3>
                        <p class="small muted">{{ m.minutes }} min</p>
                        @for (o of m.objective_ids ?? []; track o) { <div class="row"><span class="small"><span class="mono muted">{{ o }}</span> {{ objective(o) }}</span></div> }
                      </div>
                    }
                  </div><div>
                    @if (r.outline!.cuts?.length) { <div class="card"><h3>Left out on purpose</h3>@for (c of r.outline!.cuts!; track $index) { <p class="small">• {{ c }}</p> }</div> }
                  </div></div>
                } @else { <ng-container *ngTemplateOutlet="none" /> }
              }
              @case ('quiz') {
                @for (m of r.modules; track $index) {
                  @if (m.quiz.length) {
                    <div class="card"><h3>{{ m.title }} <span class="tag">{{ m.quiz.length }} questions</span></h3>
                      @for (q of m.quiz; track $index) {
                        <div class="row"><div><p class="q small">{{ q.question }}</p>
                          <ol class="opts">@for (o of q.options; track $index) { <li [class.a]="o.startsWith(q.answer + ')')">{{ o }}</li> }</ol></div></div>
                      }
                    </div>
                  }
                } @empty { <ng-container *ngTemplateOutlet="none" /> }
              }
              @case ('preview') {
                @if (pageHtml(); as html) { <iframe class="frame" sandbox="" title="Course page preview" [srcdoc]="html"></iframe> }
                @else if (!r.pages.length) { <ng-container *ngTemplateOutlet="none" /> }
                @else { <p class="small muted">Loading the page…</p> }
              }
              @case ('code') {
                @if (code(); as c) { <pre class="code">{{ c }}</pre><p class="small muted">Read-only.</p> }
                @else { <ng-container *ngTemplateOutlet="none" /> }
              }
              @case ('lab') {
                @if (r.lab.injects.length) {
                  <div class="grid2"><div class="card"><h3>Inject timeline</h3>
                    @for (i of r.lab.injects; track i.id) {
                      <div class="row"><span class="small"><span class="mono muted">t+{{ i.t_offset_min }}</span> <b>{{ i.id }}</b><br><span class="muted">{{ i.description }}</span></span>
                        <span>@if (i.critical) { <span class="tag red">critical</span> } @if (i.author_required) { <span class="tag warn">author required</span> }</span></div>
                    }</div>
                    <div><div class="card"><h3>Range</h3><p class="mono">{{ r.lab.range || '—' }}</p></div>
                      @if (r.lab.noise_floor.length) { <div class="card"><h3>Noise floor</h3>@for (n of r.lab.noise_floor; track n.id) { <p class="small"><b>{{ n.id }}</b> {{ n.description }}</p> }</div> }</div></div>
                } @else { <ng-container *ngTemplateOutlet="none" /> }
              }
              @case ('validation') {
                <div class="grid2"><div class="card"><h3>Open items · {{ r.actions_open }} @if (r.actions_blocking) { <span class="tag warn">{{ r.actions_blocking }} block promotion</span> }</h3>
                  @for (a of openActions(); track a.id) {
                    <div class="row"><span class="small"><b>{{ a.category }}</b> · {{ a.text }}</span><span class="tag" [class.warn]="a.blocks_promotion">{{ a.blocks_promotion ? 'blocking' : 'advisory' }}</span></div>
                  } @empty { <p class="small muted">Nothing open.</p> }</div>
                  <div><div class="card"><h3>QA</h3>
                    <div class="row"><span class="small muted">Result</span><span class="tag" [class.ok]="r.qa.result === 'pass'" [class.red]="r.qa.result === 'fail'">{{ r.qa.result || 'not run' }}</span></div>
                    <div class="row"><span class="small muted">Cycle</span><span class="small">{{ r.qa.cycle ?? 0 }} of 3</span></div></div>
                    @if (r.findings.length) { <div class="card"><h3>Findings</h3>@for (f of r.findings; track $index) { <p class="small"><span class="mono">{{ f.check }}</span> · {{ f.message }}</p> }</div> }
                  </div></div>
              }
              @default {
                <div class="card"><h3>build/arc2/{{ r.slug }}/</h3>
                  @for (f of r.files; track f.path) { <div class="row"><span class="mono small">{{ f.path }}</span><span class="tag">{{ f.kind }}</span></div> }
                  @empty { <p class="small muted">No files yet.</p> }
                </div>
              }
            }
          </div>
        } @else {
          <div class="workhead"><div><h2>{{ draft() ? draft()!.name : 'Course Studio' }}</h2>
            <p>{{ draft() ? 'Not started. Describe the course in the chat and press Send.' : 'Name a project on the left to start a course.' }}</p></div></div>
          <div class="body"><div class="card"><h3>Nothing generated yet</h3>
            <p class="small muted">Send starts the Content Architect. It writes the outline and stops for your review before anything else is generated.</p></div></div>
        }
      </section>
    </div>

    <div class="agents" role="status">
      <span class="phase"><span class="dot now"></span>{{ run()?.phase_text ?? (draft() ? 'Waiting to start' : 'Idle') }}</span>
      <span class="sep"></span><span class="lbl">Agents:</span>
      @for (s of dots(); track s.key) { <span class="who" [class.done]="s.state === 'done'"><span [class]="'dot ' + s.state"></span>{{ s.name }}</span> }
    </div>
    <div class="banner">{{ marking }}</div>

    <ng-template #none><div class="card"><h3>Not generated yet</h3><p class="small muted">This appears once the ARC² agents have written it.</p></div></ng-template>
  `,
  styles: [`
    :host { display:flex; flex-direction:column; min-height:100vh; background:var(--tn-bg); color:var(--tn-ink); font:14px/1.5 var(--tn-font); }
    .banner { background:var(--tn-accent); color:#fff; text-align:center; font-size:11px; font-weight:700; letter-spacing:.8px; padding:3px 8px; text-transform:uppercase; }
    .app { flex:1; display:grid; grid-template-columns:240px 300px minmax(0,1fr); min-height:0; height:calc(100vh - 80px); }
    .side { background:var(--tn-surface); border-right:1px solid var(--tn-line); display:flex; flex-direction:column; min-height:0; }
    .brand { text-align:center; padding:18px 12px 14px; border-bottom:1px solid var(--tn-line); }
    .logo { width:52px; height:52px; margin:0 auto 8px; border-radius:10px; display:grid; place-items:center; background:radial-gradient(circle at 40% 35%,#d4344c,var(--tn-accent) 75%); box-shadow:0 0 18px rgba(181,18,43,.22); font-weight:800; font-size:17px; color:#fff; }
    .brand h1 { margin:0; font-size:22px; color:var(--tn-accent); letter-spacing:.5px; }
    .brand p { margin:2px 0 0; font-size:10px; letter-spacing:1.6px; color:var(--tn-ink); text-transform:uppercase; }
    .seg { display:flex; gap:6px; padding:12px 10px 8px; }
    .seg button { flex:1; background:var(--tn-surface); border:1px solid var(--tn-line); border-radius:7px; padding:6px 4px; font:inherit; font-size:12px; color:var(--tn-ink); }
    .seg button[aria-pressed="true"] { background:var(--tn-soft); border-color:var(--tn-accent); color:var(--tn-accent); font-weight:600; }
    .seg button:disabled { opacity:.55; }
    .newp { padding:4px 10px 12px; border-bottom:1px solid var(--tn-line); }
    .input { width:100%; background:var(--tn-surface); border:1px solid var(--tn-line); border-radius:7px; padding:8px 10px; color:var(--tn-ink); font:inherit; }
    .input:disabled { opacity:.5; }
    .btn-new { width:100%; margin-top:8px; background:var(--tn-button); border:0; border-radius:7px; padding:8px; color:#fff; cursor:pointer; font:inherit; }
    .projects { overflow:auto; padding:8px; flex:1; }
    .proj { display:block; width:100%; text-align:left; background:transparent; border:1px solid transparent; border-radius:8px; padding:10px 12px; margin-bottom:4px; cursor:pointer; font:inherit; color:inherit; }
    .proj strong { display:block; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
    .proj span { display:block; font-size:12px; color:var(--tn-muted); }
    .proj small { display:block; font-size:11px; color:var(--tn-muted); opacity:.8; }
    .proj[aria-current="true"] { background:var(--tn-soft); border-color:var(--tn-accent); box-shadow:inset 3px 0 0 var(--tn-accent); }
    .proj[aria-current="true"] strong { color:var(--tn-accent); }
    .side-foot { display:flex; justify-content:space-between; border-top:1px solid var(--tn-line); padding:12px 14px; font-size:13px; }
    .linkbtn { background:none; border:0; color:var(--tn-accent); cursor:pointer; font:inherit; padding:0; text-decoration:none; }
    .pad { padding:8px 12px; } .err { color:var(--tn-accent); }
    .chat { background:var(--tn-bg); border-right:1px solid var(--tn-line); display:flex; flex-direction:column; min-height:0; }
    .chat-mode { display:flex; gap:16px; padding:8px 14px; border-bottom:1px solid var(--tn-line); font-size:11px; }
    .chat-mode .off { color:var(--tn-muted); }
    .msgs { flex:1; overflow:auto; padding:14px 12px; display:flex; flex-direction:column; gap:12px; }
    .msg { border-radius:12px; padding:11px 13px; font-size:13px; max-width:92%; overflow-wrap:anywhere; }
    .msg.user { align-self:flex-end; background:var(--tn-soft); border:1px solid var(--tn-line); }
    .msg.pipe { align-self:flex-start; background:var(--tn-surface); border:1px solid var(--tn-line); }
    .msg.pipe b { display:block; font-size:11px; letter-spacing:.8px; color:var(--tn-accent); margin-bottom:3px; }
    .msg.warn { border-color:var(--tn-accent); } .msg.warn b { color:var(--tn-warm-ink); }
    .msg.live b::after { content:" ●"; animation:blink 1.2s infinite; }
    @keyframes blink { 50% { opacity:.2; } }
    .msg time { display:block; font-size:10px; color:var(--tn-muted); margin-top:5px; }
    .msg code { display:block; margin-top:6px; background:#292326; color:#fff; border-radius:6px; padding:6px 8px; font:12px ui-monospace,Menlo,monospace; }
    .pre { white-space:pre-wrap; }
    .approve { display:flex; align-items:center; gap:12px; padding:12px; border-top:1px solid var(--tn-line); }
    .btn-approve { background:var(--tn-button); color:#fff; border:0; border-radius:8px; padding:10px 14px; font:inherit; font-weight:700; cursor:pointer; min-width:120px; line-height:1.25; }
    .btn-approve:disabled { background:#efe3e5; color:var(--tn-muted); cursor:not-allowed; }
    .approve p { margin:0; font-size:12px; color:var(--tn-muted); }
    .send { display:flex; gap:8px; padding:0 12px 12px; }
    .send .input { flex:1; }
    .btn-send { background:var(--tn-button); border:0; border-radius:7px; padding:0 14px; color:#fff; cursor:pointer; font:inherit; }
    .btn-send:disabled { opacity:.45; cursor:not-allowed; }
    .work { display:flex; flex-direction:column; min-width:0; min-height:0; }
    .tabs { display:flex; gap:4px; padding:0 16px; border-bottom:1px solid var(--tn-line); background:var(--tn-surface); overflow-x:auto; }
    .tabs button { background:none; border:0; border-bottom:2px solid transparent; padding:11px 12px 9px; color:var(--tn-ink); cursor:pointer; white-space:nowrap; font:inherit; }
    .tabs button[aria-pressed="true"] { color:var(--tn-accent); border-bottom-color:var(--tn-accent); }
    .n { background:var(--tn-accent); color:#fff; border-radius:9px; padding:0 6px; font-size:11px; margin-left:4px; }
    .workhead { display:flex; justify-content:space-between; align-items:flex-start; gap:14px; padding:12px 18px; border-bottom:1px solid var(--tn-line); background:var(--tn-surface); flex-wrap:wrap; }
    .workhead h2 { margin:0; font-size:15px; } .workhead p { margin:2px 0 0; font-size:12px; color:var(--tn-muted); }
    .toolbar { display:flex; gap:12px; align-items:center; flex-wrap:wrap; }
    .btn-out { background:var(--tn-surface); color:var(--tn-accent); border:1px solid var(--tn-accent); border-radius:7px; padding:6px 11px; font:inherit; font-weight:600; cursor:pointer; }
    .hint { font-size:11px; color:var(--tn-muted); }
    .chips { display:flex; gap:8px; flex-wrap:wrap; padding:10px 18px; border-bottom:1px solid var(--tn-line); }
    .chip { background:var(--tn-surface); border:1px solid var(--tn-line); border-radius:6px; padding:4px 10px; font:12px ui-monospace,Menlo,monospace; cursor:pointer; color:var(--tn-ink); }
    .chip[aria-pressed="true"] { background:var(--tn-soft); border-color:var(--tn-accent); color:var(--tn-accent); font-weight:600; }
    .body { flex:1; overflow:auto; padding:18px; min-height:0; }
    .frame { display:block; width:100%; height:100%; min-height:520px; border:1px solid var(--tn-line); border-radius:8px; background:#fff; }
    .card { background:var(--tn-surface); border:1px solid var(--tn-line); border-radius:10px; padding:14px 16px; margin-bottom:12px; }
    .card h3 { margin:0 0 8px; font-size:14px; }
    .muted { color:var(--tn-muted); } .small { font-size:12px; } .mono { font:12px/1.5 ui-monospace,Menlo,monospace; }
    .row { display:flex; justify-content:space-between; gap:12px; padding:9px 0; border-top:1px solid var(--tn-line); align-items:flex-start; }
    .row:first-of-type { border-top:0; padding-top:0; }
    .tag { display:inline-block; border-radius:5px; padding:2px 7px; font-size:11px; white-space:nowrap; background:var(--tn-bg); color:var(--tn-muted); border:1px solid var(--tn-line); }
    .tag.ok { background:var(--tn-ok-soft); color:var(--tn-ok); } .tag.warn { background:var(--tn-warm); color:var(--tn-warm-ink); } .tag.red { background:var(--tn-soft); color:var(--tn-accent); }
    .grid2 { display:grid; grid-template-columns:minmax(0,1.3fr) minmax(0,1fr); gap:12px; align-items:start; }
    pre.code { margin:0; background:#fbf6f6; border:1px solid var(--tn-line); border-radius:8px; padding:14px; overflow:auto; font:12px/1.55 ui-monospace,Menlo,monospace; }
    .q { margin:0 0 4px; font-weight:600; } .opts { margin:6px 0 0; padding-left:18px; list-style:none; } .opts li.a { color:var(--tn-ok); font-weight:600; }
    .agents { display:flex; align-items:center; gap:16px; padding:7px 14px; background:var(--tn-surface); border-top:1px solid var(--tn-line); font-size:12px; overflow-x:auto; white-space:nowrap; }
    .agents .phase { font-weight:700; display:flex; gap:6px; align-items:center; }
    .agents .sep { width:1px; height:16px; background:var(--tn-line); } .agents .lbl { color:var(--tn-muted); }
    .dot { width:7px; height:7px; border-radius:50%; background:var(--tn-line); display:inline-block; margin-right:5px; }
    .dot.done { background:var(--tn-ok); } .dot.now, .dot.running { background:var(--tn-accent); box-shadow:0 0 8px var(--tn-accent); } .dot.failed { background:var(--tn-warm-ink); }
    @media (max-width:1100px) { .app { grid-template-columns:220px 260px minmax(0,1fr); } .grid2 { grid-template-columns:1fr; } }
    @media (max-width:800px) { .app { grid-template-columns:1fr; height:auto; } .projects { max-height:200px; } .msgs { max-height:380px; } }
  `],
})
export class CourseStudioComponent implements OnInit {
  private readonly api = inject(CourseStudioService);
  readonly auth = inject(AuthService);
  private readonly destroyRef = inject(DestroyRef);
  private readonly sanitizer = inject(DomSanitizer);
  @ViewChild('box') private box?: ElementRef<HTMLInputElement>;
  @ViewChild('npName') private npName?: ElementRef<HTMLInputElement>;
  @ViewChild('msgs') private msgsEl?: ElementRef<HTMLElement>;

  readonly marking = MARKING;
  readonly tabs: [Tab, string][] = [['files', 'Files'], ['outline', 'Outline'], ['quiz', 'Quiz Bank'], ['preview', 'Preview'], ['code', 'Code'], ['lab', 'Lab'], ['validation', 'Validation']];
  readonly heads: Record<Tab, string> = {
    files: 'Run files', outline: 'Review your course outline', quiz: 'Quiz bank', preview: 'Review your generated course',
    code: 'Code', lab: 'Lab', validation: 'Validation',
  };

  readonly runs = signal<RunSummary[]>([]);
  readonly runnerSeen = signal<string | null>(null);
  readonly listError = signal(false);
  readonly selected = signal<string | null>(null);
  readonly run = signal<RunDetail | null>(null);
  readonly draft = signal<Draft | null>(null);
  readonly tab = signal<Tab>('outline');
  readonly page = signal<string | null>(null);
  readonly pageHtml = signal<SafeHtml | null>(null);
  readonly code = signal<string | null>(null);
  readonly busy = signal(false);
  readonly note = signal<string | null>(null);

  readonly messages = computed<Message[]>(() => this.run()?.messages ?? []);
  readonly action = computed(() => primaryAction(this.run()));
  readonly dots = computed(() => stageDots(this.run()));
  readonly idle = computed(() => runnerLooksIdle(this.run(), this.runnerSeen()));
  readonly canSend = computed(() => (this.selected() ? canReply(this.run()) : !!this.draft()));
  readonly openActions = computed(() => (this.run()?.human_actions ?? []).filter(a => a.status === 'open'));
  readonly time = shortTime;
  private timer?: ReturnType<typeof setTimeout>;
  private destroyed = false;

  ngOnInit(): void {
    this.refreshList(true);
    this.destroyRef.onDestroy(() => { this.destroyed = true; clearTimeout(this.timer); });
  }

  refreshList(selectFirst = false): void {
    this.api.list().subscribe({
      next: l => {
        this.listError.set(false);
        this.runs.set(l.runs);
        this.runnerSeen.set(l.runner_seen);
        if (selectFirst && !this.selected() && !this.draft() && l.runs.length) this.select(l.runs[0].slug);
      },
      error: () => this.listError.set(true),
    });
  }

  select(slug: string): void {
    this.selected.set(slug);
    this.note.set(null);
    this.page.set(null);
    this.pageHtml.set(null);
    this.code.set(null);
    this.load(slug, true);
  }

  selectDraft(): void {
    this.selected.set(null);
    this.run.set(null);
    this.note.set(null);
    clearTimeout(this.timer);
    setTimeout(() => this.box?.nativeElement.focus());
  }

  private load(slug: string, pickTab = false): void {
    clearTimeout(this.timer);
    if (this.destroyed) return;
    this.api.get(slug).subscribe({
      next: r => {
        if (this.destroyed || this.selected() !== slug) return;
        this.run.set(r);
        if (pickTab) this.tab.set(r.phase === 'preview' || r.phase === 'packaged' ? 'preview' : 'outline');
        if (this.tab() === 'preview' && !this.page() && r.pages.length) this.openPage(r.pages[0]);
        this.scrollChat();
        this.timer = setTimeout(() => { this.load(slug); this.refreshList(); }, pollMs(r));
      },
      error: () => { if (!this.destroyed) this.timer = setTimeout(() => this.load(slug), 15000); },
    });
  }

  setTab(t: Tab): void {
    this.tab.set(t);
    const r = this.run();
    if (!r) return;
    if (t === 'preview' && !this.page() && r.pages.length) this.openPage(r.pages[0]);
    if (t === 'code' && !this.code()) {
      const cfg = r.files.find(f => f.path.endsWith('course-config.json'))?.path;
      if (cfg) this.api.file(r.slug, cfg).subscribe({ next: f => this.code.set(`// ${f.path}\n${f.text}`), error: () => {} });
    }
  }

  openPage(path: string): void {
    const r = this.run();
    if (!r) return;
    this.page.set(path);
    this.pageHtml.set(null);
    this.api.file(r.slug, path).subscribe({
      // The frame is sandbox="" (no scripts, opaque origin), so the page's own markup and
      // its <style> can be shown as written; Angular's sanitiser would strip the styles.
      next: f => this.pageHtml.set(this.sanitizer.bypassSecurityTrustHtml(
        '<!doctype html><meta charset="utf-8"><style>body{font:14px/1.65 -apple-system,"Segoe UI",sans-serif;color:#292326;margin:18px 24px}'
        + 'h2{color:#b5122b}table{border-collapse:collapse;font-size:13px}th,td{border:1px solid #eadbdd;padding:6px 8px;text-align:left;vertical-align:top}'
        + 'th{color:#b5122b}</style>' + f.text)),
      error: () => this.pageHtml.set(this.sanitizer.bypassSecurityTrustHtml('<p>Could not load this page.</p>')),
    });
  }

  pageLabel(path: string): string {
    const p = pageRef(path);
    return p ? `${p.module} · page ${p.page}` : path;
  }

  objective(id: string): string {
    return this.run()?.objectives.find(o => o.id === id)?.text ?? '';
  }

  newProject(e: Event): void {
    e.preventDefault();
    const name = this.npName?.nativeElement.value.trim() ?? '';
    if (!name) { this.npName?.nativeElement.focus(); return; }
    this.draft.set({ name });
    if (this.npName) this.npName.nativeElement.value = '';
    this.selectDraft();
  }

  /** Send: a draft's first message starts the course; later messages are feedback at a review. */
  send(e: Event): void {
    e.preventDefault();
    const input = this.box?.nativeElement;
    const text = input?.value.trim() ?? '';
    if (!text || this.busy()) return;
    const slug = this.selected();
    this.busy.set(true);
    this.note.set(null);
    const req = slug ? this.api.reply(slug, 'feedback', text) : this.api.create(this.draft()!.name, text);
    req.subscribe({
      next: r => {
        if (input) input.value = '';
        if (!slug) this.draft.set(null);
        this.busy.set(false);
        this.selected.set(r.slug);
        this.run.set(r);
        this.refreshList();
        this.load(r.slug);
      },
      error: (err: HttpErrorResponse) => { this.busy.set(false); this.note.set(this.message(err)); },
    });
  }

  accept(): void {
    const slug = this.selected();
    if (!slug || !this.action().enabled || this.busy()) return;
    this.busy.set(true);
    this.note.set(null);
    this.api.reply(slug, 'accept').subscribe({
      next: r => { this.busy.set(false); this.run.set(r); this.refreshList(); this.load(slug); },
      error: (err: HttpErrorResponse) => { this.busy.set(false); this.note.set(this.message(err)); },
    });
  }

  download(slug: string): void {
    this.api.packageZip(slug).subscribe({
      next: blob => {
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url; a.download = `${slug}-cmi5.zip`; a.click();
        setTimeout(() => URL.revokeObjectURL(url), 1000);
      },
      error: (err: HttpErrorResponse) => this.note.set(this.message(err)),
    });
  }

  private message(err: HttpErrorResponse): string {
    const detail = (err.error as { detail?: unknown })?.detail;
    if (typeof detail === 'string') return detail;
    return err.status === 0 ? 'The API is unreachable.' : `The request failed (${err.status}).`;
  }

  private scrollChat(): void {
    setTimeout(() => { const el = this.msgsEl?.nativeElement; if (el) el.scrollTop = el.scrollHeight; });
  }
}
