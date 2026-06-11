/* CDP smoke for Curriculum Forge UI against the deployed stack (real data). */
import puppeteer from 'puppeteer-core';
import fs from 'node:fs';

const chromePath = [
  'C:/Program Files/Google/Chrome/Application/chrome.exe',
  'C:/Program Files (x86)/Google/Chrome/Application/chrome.exe',
].find((p) => fs.existsSync(p));

const SHOTS = 'devtools-shots';
const browser = await puppeteer.launch({ executablePath: chromePath, headless: 'new', defaultViewport: { width: 1600, height: 900 } });
const page = await browser.newPage();
const errors = [];
page.on('pageerror', (e) => errors.push(`pageerror: ${e.message}`));
page.on('console', (m) => {
  if (m.type() === 'error' && !/Failed to load resource|Http failure/.test(m.text())) errors.push(m.text());
});

// Curriculum Forge with the real curriculum
await page.goto('http://localhost:4200/curriculum-forge', { waitUntil: 'networkidle2', timeout: 60000 });
await new Promise((r) => setTimeout(r, 2000));
await page.evaluate(() => {
  document.querySelector('.curriculum-card')?.dispatchEvent(new MouseEvent('click', { bubbles: true }));
});
await new Promise((r) => setTimeout(r, 2000));
await page.screenshot({ path: `${SHOTS}/curriculum-forge.png` });
const forgeState = await page.evaluate(() => ({
  cards: document.querySelectorAll('.curriculum-card').length,
  docs: document.querySelectorAll('.doc-row').length,
  quizzes: document.querySelectorAll('.quiz-card').length,
}));

// Quiz player with the real quiz
const quizId = process.argv[2];
await page.goto(`http://localhost:4200/quiz-player?quiz=${quizId}`, { waitUntil: 'networkidle2' });
await new Promise((r) => setTimeout(r, 1500));
await page.screenshot({ path: `${SHOTS}/quiz-player-intro.png` });
await page.evaluate(() => {
  [...document.querySelectorAll('button')].find((b) => b.textContent?.includes('Start attempt'))?.click();
});
await new Promise((r) => setTimeout(r, 1800));
await page.screenshot({ path: `${SHOTS}/quiz-player-question.png` });
const quizState = await page.evaluate(() => ({
  options: document.querySelectorAll('.option').length,
  stem: document.querySelector('.q-stem')?.textContent?.slice(0, 80) ?? '',
}));

// My Progress (radar may be empty without assertions for dev user — page must render clean)
await page.goto('http://localhost:4200/my-progress', { waitUntil: 'networkidle2' });
await new Promise((r) => setTimeout(r, 2000));
await page.screenshot({ path: `${SHOTS}/my-progress.png` });

await browser.close();
console.log(JSON.stringify({ forgeState, quizState, errors }, null, 2));
