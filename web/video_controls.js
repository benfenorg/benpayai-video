import { t } from "./i18n.js";

const fixedRatios = ["16:9", "9:16", "4:3", "3:4", "1:1", "21:9"];

export function bindVideoControls(node) {
  const widget = (name) => node.widgets.find((item) => item.name === name);
  const protocol = widget("protocol");
  const ratio = widget("ratio");
  const resolution = widget("resolution");
  const generation = widget("generation");
  const imageMode = widget("image_mode");
  const referenceVideos = widget("reference_video_urls");
  const imageNode = Boolean(imageMode);
  let regenerateButton = null;

  function synchronize() {
    node.properties ||= {};
    if (!node.properties.benpayRecoveryId) {
      // getRandomValues also works when ComfyUI is opened over LAN HTTP.
      const bytes = crypto.getRandomValues(new Uint8Array(16));
      node.properties.benpayRecoveryId = Array.from(bytes, (value) =>
        value.toString(16).padStart(2, "0"),
      ).join("");
    }
    if (!protocol || !ratio) return;
    if (resolution) {
      const minimax = protocol.value === "minimax";
      const resolutions = minimax
        ? ["768P", "480P"]
        : ["480p", "720p", "768p", "1080p"];
      const current = minimax
        ? String(resolution.value).toUpperCase()
        : String(resolution.value).toLowerCase();
      resolution.options.values = resolutions;
      const selected = resolutions.includes(current)
        ? current
        : minimax ? "768P" : "720p";
      if (resolution.value !== selected) {
        resolution.value = selected;
        resolution.callback?.(selected);
      }
    }
    if (imageMode) {
      const modes = ["first_frame", "first_last_frame", "reference_images"];
      if (protocol.value !== "minimax") modes.push("reference_video");
      imageMode.options.values = modes;
      if (!modes.includes(imageMode.value)) {
        imageMode.value = "first_frame";
        imageMode.callback?.(imageMode.value);
      }
    }
    if (referenceVideos) referenceVideos.disabled = protocol.value === "minimax";
    const adaptiveOnly = imageNode && protocol.value === "minimax";
    const values = adaptiveOnly
      ? ["adaptive"]
      : imageNode
        ? [...fixedRatios, "adaptive"]
        : [...fixedRatios];
    ratio.options.values = values;
    ratio.disabled = adaptiveOnly;
    if (!values.includes(ratio.value)) {
      ratio.value = adaptiveOnly || imageNode ? "adaptive" : "16:9";
      ratio.callback?.(ratio.value);
    }
    node.setDirtyCanvas(true, true);
  }

  if (protocol) {
    const onProtocolChanged = protocol.callback;
    protocol.callback = function (...args) {
      const result = onProtocolChanged?.apply(this, args);
      synchronize();
      return result;
    };
  }
  const onConfigure = node.onConfigure;
  node.onConfigure = function (...args) {
    const result = onConfigure?.apply(this, args);
    synchronize();
    return result;
  };

  if (generation) {
    regenerateButton = node.addWidget("button", t("regenerate"), null, () => {
      if (!window.confirm(t("regenerateConfirm"))) return;
      if (generation.value >= 0x7fffffff) return;
      generation.value = Number(generation.value || 0) + 1;
      generation.callback?.(generation.value);
      node.setDirtyCanvas(true, true);
    }, { serialize: false });
    regenerateButton.serialize = false;
  }
  synchronize();
  return regenerateButton;
}
