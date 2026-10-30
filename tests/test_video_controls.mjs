import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const source = await readFile(new URL("../web/video_controls.js", import.meta.url), "utf8");
const isolated = source.replace('import { t } from "./i18n.js";', "const t = (key) => key;");
const { bindVideoControls } = await import(
  `data:text/javascript;base64,${Buffer.from(isolated).toString("base64")}`
);

function createNode(image = true) {
  const node = {
    properties: {},
    widgets: [
      { name: "protocol", value: "doubao" },
      { name: "ratio", value: "9:16", options: {} },
      { name: "generation", value: 0 },
      { name: "resolution", value: "720p", options: {} },
    ],
    setDirtyCanvas() {},
    addWidget(type, name, value, callback) {
      const widget = { type, name, value, callback };
      this.widgets.push(widget);
      return widget;
    },
  };
  if (image) {
    node.widgets.push({ name: "image_mode", value: "first_frame", options: {} });
    node.widgets.push({ name: "reference_video_urls", value: "https://example.com/ref.mp4" });
  }
  return node;
}

test("MiniMax image ratios are adaptive only; switching back restores Doubao options", () => {
  const node = createNode();
  bindVideoControls(node);
  const [protocol, ratio] = node.widgets;
  protocol.value = "minimax";
  protocol.callback("minimax");
  assert.deepEqual(ratio.options.values, ["adaptive"]);
  assert.equal(ratio.value, "adaptive");
  assert.equal(ratio.disabled, true);
  protocol.value = "doubao";
  protocol.callback("doubao");
  assert.equal(ratio.disabled, false);
  assert.ok(ratio.options.values.includes("9:16"));
  assert.equal(ratio.options.values.length, 7);
});

test("text-to-video exposes the six documented ratios", () => {
  const node = createNode(false);
  node.widgets[0].value = "minimax";
  bindVideoControls(node);
  assert.deepEqual(node.widgets[1].options.values, ["16:9", "9:16", "4:3", "3:4", "1:1", "21:9"]);
  assert.equal(node.widgets[1].disabled, false);
});

test("both nodes use documented MiniMax resolutions when switching providers", () => {
  for (const image of [false, true]) {
    const node = createNode(image);
    bindVideoControls(node);
    const protocol = node.widgets[0];
    const resolution = node.widgets.find((widget) => widget.name === "resolution");
    protocol.value = "minimax";
    protocol.callback("minimax");
    assert.deepEqual(resolution.options.values, ["768P", "480P"]);
    assert.equal(resolution.value, "768P");
    resolution.value = "480P";
    protocol.value = "doubao";
    protocol.callback("doubao");
    assert.deepEqual(resolution.options.values, ["480p", "720p", "768p", "1080p"]);
    assert.equal(resolution.value, "480p");
  }
});

test("loading MiniMax workflows fixes invalid resolutions and normalizes casing", () => {
  for (const [saved, expected] of [["720p", "768P"], ["1080p", "768P"], ["768p", "768P"], ["480p", "480P"], ["480P", "480P"]]) {
    const node = createNode();
    node.onConfigure = function () {
      this.widgets[0].value = "minimax";
      this.widgets.find((widget) => widget.name === "resolution").value = saved;
    };
    bindVideoControls(node);
    node.onConfigure();
    assert.equal(node.widgets.find((widget) => widget.name === "resolution").value, expected);
  }
});

test("switching to MiniMax removes reference video mode and disables its URLs", () => {
  const node = createNode();
  const mode = node.widgets.find((widget) => widget.name === "image_mode");
  const urls = node.widgets.find((widget) => widget.name === "reference_video_urls");
  mode.value = "reference_video";
  bindVideoControls(node);
  assert.ok(mode.options.values.includes("reference_video"));
  node.widgets[0].value = "minimax";
  node.widgets[0].callback("minimax");
  assert.deepEqual(mode.options.values, ["first_frame", "first_last_frame", "reference_images"]);
  assert.equal(mode.value, "first_frame");
  assert.equal(urls.disabled, true);
  assert.equal(urls.value, "https://example.com/ref.mp4");
  node.widgets[0].value = "doubao";
  node.widgets[0].callback("doubao");
  assert.ok(mode.options.values.includes("reference_video"));
  assert.equal(urls.disabled, false);
});

test("loading a MiniMax workflow normalizes obsolete reference video mode", () => {
  const node = createNode();
  node.onConfigure = function () {
    this.widgets[0].value = "minimax";
    this.widgets.find((widget) => widget.name === "image_mode").value = "reference_video";
  };
  bindVideoControls(node);
  node.onConfigure();
  const mode = node.widgets.find((widget) => widget.name === "image_mode");
  assert.equal(mode.value, "first_frame");
  assert.equal(mode.options.values.includes("reference_video"), false);
});

test("workflow loading preserves recovery identity and normalizes MiniMax ratio", () => {
  const node = createNode();
  node.onConfigure = function () {
    this.properties = { benpayRecoveryId: "saved-node-id" };
    this.widgets[0].value = "minimax";
    this.widgets[1].value = "9:16";
  };
  bindVideoControls(node);
  node.onConfigure();
  assert.equal(node.properties.benpayRecoveryId, "saved-node-id");
  assert.equal(node.widgets[1].value, "adaptive");
});

test("regeneration requires confirmation and affects only the next run", () => {
  const node = createNode();
  bindVideoControls(node);
  const button = node.widgets.find((widget) => widget.name === "regenerate");
  const previousWindow = globalThis.window;
  try {
    globalThis.window = { confirm: () => false };
    button.callback();
    assert.equal(node.widgets[2].value, 0);
    globalThis.window.confirm = () => true;
    button.callback();
    assert.equal(node.widgets[2].value, 1);
  } finally {
    globalThis.window = previousWindow;
  }
});

test("node initialization works without secure-context randomUUID", () => {
  const original = Object.getOwnPropertyDescriptor(globalThis, "crypto");
  const getRandomValues = globalThis.crypto.getRandomValues.bind(globalThis.crypto);
  try {
    Object.defineProperty(globalThis, "crypto", {
      value: { getRandomValues },
      configurable: true,
    });
    const first = createNode();
    const second = createNode();
    bindVideoControls(first);
    bindVideoControls(second);
    assert.match(first.properties.benpayRecoveryId, /^[0-9a-f]{32}$/);
    assert.notEqual(first.properties.benpayRecoveryId, second.properties.benpayRecoveryId);
    const saved = first.properties.benpayRecoveryId;
    first.onConfigure();
    assert.equal(first.properties.benpayRecoveryId, saved);
  } finally {
    Object.defineProperty(globalThis, "crypto", original);
  }
});
