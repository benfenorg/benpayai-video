import { api } from "../../scripts/api.js";

let catalogRequest = null;

async function fetchModelCatalog() {
  const response = await api.fetchApi("/benpay-video/models", {
    cache: "no-store",
  });
  if (!response.ok) throw new Error("video models unavailable");
  return response.json();
}

async function loadModelCatalog() {
  if (!catalogRequest) {
    catalogRequest = fetchModelCatalog();
  }
  try {
    return await catalogRequest;
  } catch (error) {
    catalogRequest = null;
    throw error;
  }
}

export async function bindModelSelection(node) {
  const protocol = node.widgets.find((widget) => widget.name === "protocol");
  const model = node.widgets.find((widget) => widget.name === "model");
  if (!protocol || !model) return;
  const catalog = await loadModelCatalog();
  const knownModels = new Set(
    Object.values(catalog).flatMap((products) =>
      products.map((product) => product.model),
    ),
  );

  function synchronizeModel() {
    const products = catalog[protocol.value] || [];
    const current = (model.value || "").trim();
    if (
      products.length &&
      (!current ||
        (knownModels.has(current) &&
          !products.some((product) => product.model === current)))
    ) {
      model.value = products[0].model;
      model.callback?.(model.value);
    }
    const values = products.map((product) => product.model);
    if (model.value && !values.includes(model.value)) values.push(model.value);
    model.options = { ...model.options, values };
    node.setDirtyCanvas(true, true);
  }

  const onProtocolChanged = protocol.callback;
  protocol.callback = function (...args) {
    const result = onProtocolChanged?.apply(this, args);
    synchronizeModel();
    return result;
  };
  const onConfigure = node.onConfigure;
  node.onConfigure = function (...args) {
    const result = onConfigure?.apply(this, args);
    synchronizeModel();
    return result;
  };
  synchronizeModel();
}
