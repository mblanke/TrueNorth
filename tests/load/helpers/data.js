// helpers/data.js — Test data generators for TrueNorth Range k6 tests

// Lightweight UUID v4 generator (no external deps)
export function uuidv4() {
  return "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, function (c) {
    const r = (Math.random() * 16) | 0;
    const v = c === "x" ? r : (r & 0x3) | 0x8;
    return v.toString(16);
  });
}

// ── Random pickers ───────────────────────────────────────────────────────────
function pick(arr) {
  return arr[Math.floor(Math.random() * arr.length)];
}

function randomInt(min, max) {
  return Math.floor(Math.random() * (max - min + 1)) + min;
}

// ── Adjective / noun pools for names ─────────────────────────────────────────
const adjectives = [
  "alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf",
  "hotel", "india", "juliet", "kilo", "lima", "mike", "november",
  "oscar", "papa", "quebec", "romeo", "sierra", "tango", "uniform",
  "victor", "whiskey", "xray", "yankee", "zulu",
];

const nouns = [
  "falcon", "viper", "thunder", "horizon", "sentinel", "guardian",
  "phoenix", "raptor", "shadow", "storm", "fortress", "bastion",
  "archer", "titan", "spartan", "patriot", "trident", "anvil",
  "hammer", "shield", "eagle", "wolf", "panther", "cobra",
];

const templateTypes = [
  "network-attack", "phishing-sim", "incident-response", "red-team",
  "blue-team", "purple-team", "malware-analysis", "forensics",
  "cloud-pentest", "iot-security", "scada-defense", "apt-simulation",
];

const difficultyLevels = ["beginner", "intermediate", "advanced", "expert"];

// ── Generators ───────────────────────────────────────────────────────────────

export function randomRangeName() {
  return `range-${pick(adjectives)}-${pick(nouns)}-${randomInt(1000, 9999)}`;
}

export function randomRangePayload(templateId, tenantId) {
  return JSON.stringify({
    name:        randomRangeName(),
    template_id: templateId || uuidv4(),
    tenant_id:   tenantId   || uuidv4(),
  });
}

export function randomTemplatePayload() {
  return JSON.stringify({
    name:        `tpl-${pick(templateTypes)}-${randomInt(100, 999)}`,
    type:        pick(templateTypes),
    vm_count:    randomInt(5, 200),
    network_config: {
      subnets:    randomInt(1, 10),
      vlans:      randomInt(1, 5),
      firewalls:  randomInt(0, 3),
    },
    description: `Auto-generated template for ${pick(templateTypes)} exercises`,
  });
}

export function randomScenarioPayload() {
  return JSON.stringify({
    name:        `scenario-${pick(adjectives)}-${randomInt(100, 999)}`,
    difficulty:  pick(difficultyLevels),
    duration_minutes: pick([30, 60, 90, 120, 180, 240]),
    objectives:  [
      `Identify ${pick(nouns)} vulnerabilities`,
      `Exploit ${pick(nouns)} service`,
      `Document findings for ${pick(adjectives)} report`,
    ],
    description: `Load-test generated scenario targeting ${pick(templateTypes)}`,
  });
}

export function randomExercisePayload(scenarioId, rangeId) {
  return JSON.stringify({
    name:        `exercise-${pick(adjectives)}-${pick(nouns)}-${randomInt(100, 999)}`,
    scenario_id: scenarioId || uuidv4(),
    range_id:    rangeId    || uuidv4(),
    max_participants: randomInt(5, 50),
    scheduled_start:  new Date(Date.now() + randomInt(60, 3600) * 1000).toISOString(),
  });
}

// ── Pre-generated arrays (for SharedArray usage) ─────────────────────────────
export function generateTemplates(n) {
  const arr = [];
  for (let i = 0; i < n; i++) {
    arr.push(JSON.parse(randomTemplatePayload()));
  }
  return arr;
}

export function generateScenarios(n) {
  const arr = [];
  for (let i = 0; i < n; i++) {
    arr.push(JSON.parse(randomScenarioPayload()));
  }
  return arr;
}

export function generateRangePayloads(n, templateIds, tenantId) {
  const arr = [];
  for (let i = 0; i < n; i++) {
    const tid = templateIds ? pick(templateIds) : uuidv4();
    arr.push(JSON.parse(randomRangePayload(tid, tenantId)));
  }
  return arr;
}