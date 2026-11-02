import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

const fallback = {
  configureKey: "Configure API Key",
  regenerate: "Generate new video",
  regenerateReady: "Ready for new generation — click Run",
  regenerateConfirm:
    "Create a new video on the next Run? Additional charges may apply.",
  dialogTitle: "Configure BenPay API Key",
  dialogDescription:
    "Save your API key locally, then run the workflow again. The key is not stored in workflows.",
  keyLabel: "API Key",
  cancel: "Cancel",
  save: "Save locally",
  checking: "Checking local configuration…",
  configured: "Configured · ••••••••{suffix}",
  notConfigured: "Not configured — click Configure API Key",
  statusUnavailable: "Cannot read configuration. Check local config.json.",
  enterKey: "Enter an API Key.",
  saveFailed: "Could not save configuration. Please retry.",
  saveUnconfirmed:
    "Save result is unconfirmed. Check your connection and retry.",
  configurationError: "Could not update the local configuration.",
};
let catalogs = {};
let loading = null;
let selectedLocale = null;

export function normalizeLocale(value) {
  const locale = String(value || "en")
    .replaceAll("_", "-")
    .toLowerCase();
  if (/^zh-(tw|hk|hant)(-|$)/.test(locale)) return "zh-TW";
  if (locale === "zh" || locale.startsWith("zh-")) return "zh";
  return "en";
}

export function currentLocale() {
  return (
    selectedLocale ||
    normalizeLocale(
      app.extensionManager?.setting?.get("Comfy.Locale") ??
        app.ui?.settings?.getSettingValue?.("Comfy.Locale"),
    )
  );
}

export async function loadTranslations() {
  if (!loading) {
    loading = api
      .fetchApi("/i18n")
      .then(async (response) => {
        if (!response.ok) throw new Error("translations unavailable");
        catalogs = await response.json();
      })
      .catch(() => {
        loading = null;
      });
  }
  await loading;
}

export function t(key, values = {}, fallbackText = null) {
  const message =
    catalogs[currentLocale()]?.benpayVideo?.[key] ??
    catalogs.en?.benpayVideo?.[key] ??
    fallback[key] ??
    fallbackText ??
    fallback.configurationError;
  return message.replace(/\{(\w+)\}/g, (_, name) => String(values[name] ?? ""));
}

export function onLanguageChange(callback) {
  const settings = app.ui?.settings;
  const listener = ({ detail }) => {
    selectedLocale = normalizeLocale(detail.value);
    callback();
  };
  settings?.addEventListener("Comfy.Locale.change", listener);
  return () => settings?.removeEventListener("Comfy.Locale.change", listener);
}
