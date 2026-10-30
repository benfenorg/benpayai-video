# API task recovery

When submitting to ComfyUI's `/prompt` endpoint, API clients can optionally
provide a stable `extra_data.extra_pnginfo.workflow.id` to enable task recovery.
The workflow ID and node ID identify the node; request parameters and credential
fingerprints keep different inputs and API keys separate.

Without a stable workflow/node identity, each invocation runs independently.
It does not save recovery records or reuse tasks across requests. Query retries
still apply within that invocation. Creation requests are never automatically
retried.

Browser workflows receive a node recovery ID automatically. Save the workflow
to preserve it across reloads. Keep inputs, seed and generation unchanged to
resume a task. Use **Regenerate on next run** to explicitly start another video.

Recovery uses the saved query address, without requiring fresh route discovery.
Records live under `benpay-video-tasks` in the ComfyUI user directory and contain
task IDs, query addresses and fingerprints, without API key values, prompts or
input media. Corrupt records stop automatic creation.

If a creation result is unconfirmed, check the remote task list before
regenerating. The provider may already be processing the original request, and
another task may incur another charge.
