import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const catalog = {
  doubao: [{ model: "doubao-seedance-2.0" }, { model: "shared-model" }],
  minimax: [{ model: "MiniMax-H3" }, { model: "shared-model" }],
};
const source = await readFile(new URL("../web/models.js", import.meta.url), "utf8");
const isolatedSource = source.replace(
  'import { api } from "../../scripts/api.js";',
  `const api = { fetchApi: async () => ({ ok: true, json: async () => (${JSON.stringify(catalog)}) }) };`,
);
const { bindModelSelection } = await import(
  `data:text/javascript;base64,${Buffer.from(isolatedSource).toString("base64")}`
);

function createNode(modelValue = "doubao-seedance-2.0") {
  return {
    widgets: [
      { name: "protocol", value: "doubao" },
      { name: "model", value: modelValue },
    ],
    setDirtyCanvas() {},
  };
}

test("switching providers updates the model and preserves the original callback", async () => {
  const node = createNode();
  const [protocol, model] = node.widgets;
  let calls = 0;
  protocol.callback = function (value) {
    assert.equal(this, protocol);
    assert.equal(value, protocol.value);
    calls++;
  };
  await bindModelSelection(node);
  assert.deepEqual(model.options.values, ["doubao-seedance-2.0", "shared-model"]);
  protocol.value = "minimax";
  protocol.callback("minimax");
  assert.equal(model.value, "MiniMax-H3");
  assert.deepEqual(model.options.values, ["MiniMax-H3", "shared-model"]);
  protocol.value = "doubao";
  protocol.callback("doubao");
  assert.equal(model.value, "doubao-seedance-2.0");
  assert.equal(calls, 2);
});

test("custom and shared model IDs survive provider changes", async () => {
  for (const value of ["manual-model", "shared-model"]) {
    const node = createNode(value);
    await bindModelSelection(node);
    node.widgets[0].value = "minimax";
    node.widgets[0].callback("minimax");
    assert.equal(node.widgets[1].value, value);
  }
});

test("loading a workflow synchronizes mismatched models after configuration", async () => {
  const node = createNode();
  node.onConfigure = function () {
    this.widgets[0].value = "minimax";
    this.widgets[1].value = "doubao-seedance-2.0";
  };
  await bindModelSelection(node);
  node.onConfigure();
  assert.equal(node.widgets[1].value, "MiniMax-H3");
});

test("late model discovery synchronizes an already loaded workflow", async () => {
  const node = createNode();
  node.widgets[0].value = "minimax";
  await bindModelSelection(node);
  assert.equal(node.widgets[1].value, "MiniMax-H3");
});
