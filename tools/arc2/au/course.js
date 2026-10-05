/*
 * course.js - ARC² module player for one cmi5 Assignable Unit. No dependencies.
 *
 * Loaded by mod_NNN/index.html after ../cmi5.js. Reads ./course-config.json (schema
 * arc2/course-config/0.1), renders the module's pages one at a time, then the formative quiz,
 * and drives the cmi5 session through window.Cmi5AU:
 *   start() -> progress(pct) per page -> complete() after the last page
 *   -> score(correct/total) after the quiz -> terminate() on Exit (keepalive on pagehide).
 *
 * Without cmi5 launch parameters (no endpoint/fetch/actor/registration/activityId in the query)
 * the module still renders, untracked, so a reviewer can open it from a plain web server. Pages
 * are fetched relative to index.html, so serve the package over HTTP; file:// blocks fetch.
 *
 * The quiz is the module's formative check and is graded here, so its answers are readable by
 * the learner. The summative assessment is never in this package; it is instructor-acknowledged.
 */
(async () => {
  'use strict';
  const $ = (sel) => document.querySelector(sel);
  const setStatus = (text) => { $('#status').textContent = text; };
  const log = (err) => { const el = $('#log'); el.hidden = false; el.textContent += (err && err.message ? err.message : String(err)) + '\n'; };

  let cfg;
  try {
    cfg = await (await fetch('./course-config.json')).json();
  } catch (e) {
    setStatus('course-config.json could not be loaded: ' + e.message);
    return;
  }
  document.title = cfg.title;
  $('#title').textContent = cfg.title;

  // cmi5 session, or untracked preview when the launch parameters are absent.
  let au = null;
  try {
    au = await new window.Cmi5AU(location.search).start();
    setStatus(`Tracked session (${au.mode}). Mastery score: ${au.masteryScore ?? cfg.masteryScore ?? 'none'}.`);
  } catch (e) {
    setStatus('Preview only, not tracked: ' + e.message);
  }
  const tracked = au !== null;
  const judged = tracked && au.judged;
  const masteryScore = tracked && au.masteryScore !== null ? au.masteryScore : (typeof cfg.masteryScore === 'number' ? cfg.masteryScore : 0.5);
  const safe = (fn) => Promise.resolve().then(fn).catch(log);

  // Pages
  const pages = (cfg.content && cfg.content.pages) || [];
  let index = 0;
  let completed = false;
  const render = async (n) => {
    index = Math.max(0, Math.min(pages.length - 1, n));
    let html;
    try { html = await (await fetch(pages[index])).text(); } catch (e) { html = `<p>Page could not be loaded: ${e.message}</p>`; }
    $('#page').innerHTML = html;
    $('#nav-pos').textContent = `${index + 1} / ${pages.length}`;
    $('#prev').disabled = index === 0;
    $('#next').textContent = index === pages.length - 1 ? 'Finish reading' : 'Next';
    if (tracked) safe(() => au.progress(Math.round((100 * (index + 1)) / pages.length)));
    window.scrollTo(0, 0);
  };
  const finishReading = async () => {
    if (!completed) {
      completed = true;
      if (judged) await safe(() => au.complete());
    }
    if (cfg.quiz && cfg.quiz.questions && cfg.quiz.questions.length) renderQuiz();
    else setStatus(tracked ? 'Module complete. Use Exit to return to the LMS.' : 'Module complete (preview).');
  };
  $('#prev').onclick = () => render(index - 1);
  $('#next').onclick = () => (index === pages.length - 1 ? finishReading() : render(index + 1));

  // Quiz: one radio group per question, options labelled A-D, answer is the letter.
  const LETTERS = ['A', 'B', 'C', 'D', 'E', 'F'];
  let scored = false;
  const renderQuiz = () => {
    const quiz = $('#quiz');
    quiz.hidden = false;
    const q = cfg.quiz;
    const items = q.questions.map((qq, qi) => {
      const opts = qq.options.map((opt, oi) => {
        const id = `q${qi}o${oi}`;
        return `<div><input type="radio" name="q${qi}" id="${id}" value="${LETTERS[oi]}"><label for="${id}">${LETTERS[oi]}) ${escapeHtml(opt)}</label></div>`;
      }).join('');
      return `<fieldset><legend>${qi + 1}. ${escapeHtml(qq.stem)}</legend>${opts}</fieldset>`;
    }).join('');
    quiz.innerHTML = `<h2>${escapeHtml(q.title || 'Quiz')}</h2><form id="quiz-form">${items}<p><button type="submit">Submit answers</button></p></form><p id="quiz-result" aria-live="polite"></p>`;
    $('#quiz-form').onsubmit = async (ev) => {
      ev.preventDefault();
      if (scored) return;
      const data = new FormData(ev.target);
      let correct = 0;
      q.questions.forEach((qq, qi) => { if (data.get(`q${qi}`) === qq.answer) correct += 1; });
      const scaled = q.questions.length ? correct / q.questions.length : 0;
      const passed = scaled >= masteryScore;
      scored = true;
      $('#quiz-result').textContent = `${correct} / ${q.questions.length} correct (${Math.round(scaled * 100)}%). ${passed ? 'Passed' : 'Not passed'} at mastery ${masteryScore}.`;
      if (judged) await safe(() => au.score(scaled));
      quiz.querySelectorAll('input,button').forEach((el) => { el.disabled = true; });
      setStatus(tracked ? 'Quiz submitted. Use Exit to return to the LMS.' : 'Quiz submitted (preview).');
    };
    quiz.scrollIntoView();
  };

  // Exit and best-effort terminate if the window closes.
  $('#exit').onclick = async () => {
    if (tracked) await safe(() => au.terminate());
    else setStatus('Preview closed.');
  };
  addEventListener('pagehide', () => {
    if (tracked && !au.terminated) au.terminate({ keepalive: true, redirect: false }).catch(() => {});
  });

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }

  if (pages.length) await render(0);
  else await finishReading();
})();
