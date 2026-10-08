// helpers/data.js — Request bodies for the current API (docs/interfaces/openapi.json:
// TemplateIn, ScenarioIn, RangeIn, ExerciseIn). Every name starts with "k6-", so what a
// run leaves behind on a shared target is easy to find.

function pick(arr) {
  return arr[Math.floor(Math.random() * arr.length)];
}

function randomInt(min, max) {
  return Math.floor(Math.random() * (max - min + 1)) + min;
}

const adjectives = ["alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf", "hotel", "kilo", "lima"];
const nouns = ["falcon", "viper", "horizon", "sentinel", "phoenix", "raptor", "bastion", "trident", "anvil", "cobra"];

export function uniqueName(kind) {
  return `k6-${kind}-${pick(adjectives)}-${pick(nouns)}-${Date.now().toString(36)}${randomInt(100, 999)}`;
}

/** TemplateIn: a name and the range's YAML. The mock provisioner builds any template. */
export function templateBody() {
  const name = uniqueName("tpl");
  return { name, yaml: `name: ${name}\nenvironment: enterprise\n`, is_public: false };
}

/** ScenarioIn: a name and the scenario's YAML (no objectives: nothing is scored). */
export function scenarioBody() {
  const name = uniqueName("scn");
  return { name, yaml: `name: ${name}\nobjectives: []\n`, is_public: false };
}

/** RangeIn: a template is required. */
export function rangeBody(templateId) {
  return { name: uniqueName("range"), template_id: templateId };
}

/** ExerciseIn: on a range and a scenario of the caller's tenant. */
export function exerciseBody(rangeId, scenarioId) {
  return { name: uniqueName("ex"), range_id: rangeId, scenario_id: scenarioId };
}
