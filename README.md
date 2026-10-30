# BenPay-Video for ComfyUI

Generate videos from text or images directly in ComfyUI with BenPay.

## Features

- Create videos from text prompts, images, or reference videos.
- Choose a video model and adjust resolution, duration, and aspect ratio.
- Preview and save generated videos in ComfyUI.
- Get started with ready-to-use example workflows.
- Save your API key once for future use.
- Follow ComfyUI’s language setting: English, Simplified Chinese, or Traditional Chinese.

## Installation

Install the node in your ComfyUI `custom_nodes` directory:

```bash
cd ComfyUI/custom_nodes
git clone https://github.com/benfenorg/benpay-video.git
```

Restart ComfyUI and find the two BenPay nodes under `video/BenPay`.

You can also use `comfyui-setup.sh` on macOS/Linux or `comfyui-setup.ps1` on
Windows to install the node and configure your API key automatically.

## Example workflows

- [Text to video · t2v](example_workflows/text-to-video-t2v.json)
- [Image to video · i2v](example_workflows/image-to-video-i2v.json)

Download a JSON file and open it in ComfyUI, or select an example from ComfyUI's
workflow templates. Both examples default to **720p · 5 seconds**.

Enter your prompt and choose a model. For image to video, upload a first-frame
image before running. The generated video appears in the node for preview and
is saved to ComfyUI's output directory.

If querying or downloading fails, rerun the same node with unchanged inputs to
resume its task. Keep the seed fixed when retrying. Successful downloads are
reused; click **Regenerate on next run** and confirm to create another video.
Save your workflow to keep task recovery available after reloading.
Creating a new task may incur another charge even if an earlier task is running.
MiniMax image-to-video uses the **adaptive** aspect ratio; text-to-video supports
16:9, 9:16, 4:3, 3:4, 1:1 and 21:9.
Reference-image mode accepts 1–4 images for Doubao and 1–5 for MiniMax.
Reference-video mode is available for Doubao only, with at most 4 reference videos.

For programmatic use, see [API task recovery](docs/api-task-recovery.md).

## API key

1. Click **Configure API Key** on either BenPay node.
2. Enter your BenPay API key and click **Save to local**.
3. Run your workflow.

If you run a workflow without a configured key, the configuration dialog opens
automatically. Save your key, then run the workflow again. Both nodes share the
same saved key.

Your key is stored locally in plaintext in the node's `config.json`. The interface
shows a masked value, and the key is never included in workflow files. Node
updates preserve your saved configuration.

## License

This project is licensed under the MIT License.
