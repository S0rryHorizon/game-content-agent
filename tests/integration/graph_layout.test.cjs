// Exercise the UI's actual layout function with a valid unequal-length merge.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const root = path.resolve(__dirname, "../..");
const app = fs.readFileSync(path.join(root, "src/game_content_agent/web/static/app.js"), "utf8");
const start = app.indexOf("  function graphPositions(draft) {");
const end = app.indexOf("\n  function renderGraph(draft) {", start);
assert.ok(start >= 0 && end > start, "graph layout function is available");
const graphPositions = new Function(`${app.slice(start, end)}\nreturn graphPositions;`)();

const original = JSON.parse(fs.readFileSync(path.join(root, "data/examples/normal.json"), "utf8"));
const longerBranch = structuredClone(original);
longerBranch.nodes.splice(3, 0, {
  ...longerBranch.nodes[2], node_id: "inspect_clues", title: "再核对线索",
});
longerBranch.edges = longerBranch.edges.flatMap((edge) =>
  edge.source === "seek_key" && edge.target === "gate"
    ? [
        { source: "seek_key", target: "inspect_clues", label: "核对线索" },
        { source: "inspect_clues", target: "gate", label: "前往北潮门" },
      ]
    : [edge]);

for (const draft of [original, longerBranch]) {
  const { positions } = graphPositions(draft);
  assert.equal(positions.size, draft.nodes.length);
  for (const edge of draft.edges) {
    assert.ok(
      positions.get(edge.target).x > positions.get(edge.source).x,
      `${edge.source} -> ${edge.target} must move right`,
    );
  }
}
const positions = graphPositions(longerBranch).positions;
assert.equal(positions.get("inspect_clues").x, positions.get("seek_key").x + 192);
assert.equal(positions.get("gate").x, positions.get("inspect_clues").x + 192);
assert.equal(positions.get("gate").x, positions.get("record_tide").x + 384);
