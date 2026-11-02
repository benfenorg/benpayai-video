import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";
import { bindModelSelection } from "./models.js";
import { bindVideoControls } from "./video_controls.js";
import {
  currentLocale,
  loadTranslations,
  onLanguageChange,
  t,
} from "./i18n.js";

const nodeTypes = new Set(["BenpayVideo", "BenpayVideoFromImages"]);
const statusWidgets = new Map();
const configureButtons = new Map();
const regenerateButtons = new Map();
let lastStatus = null;
let statusMessage = "checking";
let activeDialog = null;
let promptedWorkflow = null;

function displayStatus(status) {
  lastStatus = status;
  statusMessage = null;
  renderStatus();
}

function renderStatus() {
  for (const [node, widget] of statusWidgets) {
    localizeNodeOptions(node);
    widget.value = statusMessage
      ? t(statusMessage)
      : lastStatus?.configured
        ? t("configured", { suffix: lastStatus.suffix || "" })
        : t("notConfigured");
    const button = configureButtons.get(node);
    if (button) button.name = t("configureKey");
    const regenerate = regenerateButtons.get(node);
    if (regenerate) {
      regenerate.name = t(regenerate.readyForGeneration ? "regenerateReady" : "regenerate");
    }
    node.setDirtyCanvas(true, true);
  }
}

function localizeNodeOptions(node) {
  const choices = {
    protocol: { prefix: "provider", values: ["doubao", "minimax"] },
    image_mode: {
      prefix: "mode",
      values: [
        "first_frame",
        "first_last_frame",
        "reference_images",
        "reference_video",
      ],
    },
  };
  for (const [name, { prefix, values }] of Object.entries(choices)) {
    const widget = node.widgets.find((item) => item.name === name);
    if (!widget) continue;
    widget.options.getOptionLabel = (value) =>
      values.includes(value) ? t(`${prefix}_${value}`, {}, value) : value || "";
  }
}

function setText(element, key) {
  element.dataset.benpayText = key;
  element.textContent = t(key);
}

function localizeDialog(dialog) {
  dialog.setAttribute("aria-label", t("dialogTitle"));
  for (const element of dialog.querySelectorAll("[data-benpay-text]")) {
    element.textContent = t(element.dataset.benpayText);
  }
}

async function refreshStatus() {
  const response = await api.fetchApi("/benpay-video/config-status", {
    cache: "no-store",
  });
  if (!response.ok) throw new Error("configuration status unavailable");
  const status = await response.json();
  displayStatus(status);
  return status;
}

function showKeyDialog() {
  if (activeDialog) return activeDialog;
  const pending = openKeyDialog().finally(() => {
    if (activeDialog === pending) activeDialog = null;
  });
  activeDialog = pending;
  return activeDialog;
}

function createDialogElements() {
  const dialog = document.createElement("dialog");
  dialog.setAttribute("aria-label", t("dialogTitle"));
  dialog.dataset.benpayConfiguration = "true";
  Object.assign(dialog.style, {
    width: "min(440px, 85vw)",
    background: "var(--comfy-menu-bg, #252525)",
    color: "var(--fg-color, #eee)",
    border: "1px solid var(--border-color, #666)",
    borderRadius: "8px",
    padding: "24px",
  });
  const title = document.createElement("h3");
  setText(title, "dialogTitle");
  const description = document.createElement("p");
  setText(description, "dialogDescription");
  const form = document.createElement("form");
  form.noValidate = true;
  const label = document.createElement("label");
  const labelText = document.createElement("span");
  setText(labelText, "keyLabel");
  label.append(labelText);
  const input = document.createElement("input");
  input.type = "password";
  input.autocomplete = "off";
  input.spellcheck = false;
  input.required = true;
  input.maxLength = 4096;
  input.style.cssText =
    "display:block;width:100%;box-sizing:border-box;margin:10px 0";
  label.append(input);
  const error = document.createElement("p");
  error.setAttribute("role", "alert");
  const cancel = document.createElement("button");
  cancel.type = "button";
  setText(cancel, "cancel");
  const save = document.createElement("button");
  save.type = "submit";
  setText(save, "save");
  save.style.marginLeft = "12px";
  form.append(label, error, cancel, save);
  dialog.append(title, description, form);
  return { dialog, form, input, error, cancel, save };
}

function openKeyDialog() {
  return new Promise((resolve) => {
    const { dialog, form, input, error, cancel, save } = createDialogElements();
    let saving = false;
    let closed = false;
    function closeDialog() {
      if (closed) return;
      closed = true;
      input.value = "";
      form.reset();
      dialog.close();
      dialog.remove();
      activeDialog = null;
      resolve();
    }
    cancel.addEventListener("click", closeDialog);
    dialog.addEventListener("cancel", (event) => {
      event.preventDefault();
      if (!saving) closeDialog();
    });
    dialog.addEventListener("close", closeDialog, { once: true });
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (saving) return;
      if (!input.value.trim()) {
        setText(error, "enterKey");
        return;
      }
      saving = true;
      save.disabled = cancel.disabled = true;
      error.textContent = "";
      delete error.dataset.benpayText;
      try {
        const response = await api.fetchApi("/benpay-video/config", {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "Accept-Language": currentLocale(),
          },
          body: JSON.stringify({ apiKey: input.value }),
        });
        const result = await response.json();
        if (!response.ok) {
          setText(error, result.code || "saveFailed");
          return;
        }
        displayStatus(result);
        closeDialog();
      } catch {
        setText(error, "saveUnconfirmed");
      } finally {
        saving = false;
        save.disabled = cancel.disabled = false;
      }
    });
    document.body.append(dialog);
    dialog.showModal();
    input.focus();
  });
}

app.registerExtension({
  name: "BenPay.ConfigurationStatus",
  async setup() {
    await loadTranslations();
    onLanguageChange(() => {
      renderStatus();
      const dialog = document.querySelector(
        "dialog[data-benpay-configuration]",
      );
      if (dialog) localizeDialog(dialog);
    });
    api.addEventListener("execution_error", async ({ detail }) => {
      if (!nodeTypes.has(detail.node_type)) return;
      if (!detail.exception_type?.endsWith(".ConfigurationError")) return;
      if (detail.prompt_id && promptedWorkflow === detail.prompt_id) return;
      promptedWorkflow = detail.prompt_id;
      try {
        if (!(await refreshStatus()).configured) await showKeyDialog();
      } catch {
        // Keep the configuration button available when status lookup fails.
      }
    });
  },
  async nodeCreated(node) {
    if (!nodeTypes.has(node.comfyClass)) return;
    regenerateButtons.set(node, bindVideoControls(node));
    // Model discovery must not prevent the credential controls from loading.
    bindModelSelection(node).catch(() => {});
    await loadTranslations();
    const widget = node.addWidget(
      "text",
      t("keyLabel"),
      t("checking"),
      () => {},
      { serialize: false },
    );
    widget.disabled = true;
    widget.serialize = false;
    statusWidgets.set(node, widget);
    const configureButton = node.addWidget("button", t("configureKey"), null, showKeyDialog, {
      serialize: false,
    });
    configureButton.serialize = false;
    configureButtons.set(node, configureButton);
    const onRemoved = node.onRemoved;
    node.onRemoved = function (...args) {
      statusWidgets.delete(node);
      configureButtons.delete(node);
      regenerateButtons.delete(node);
      return onRemoved?.apply(this, args);
    };
    try {
      await refreshStatus();
    } catch {
      statusMessage = "statusUnavailable";
      renderStatus();
    }
  },
});
