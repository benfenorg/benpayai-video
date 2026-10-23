# ComfyUI BenpayAI Video

ComfyUI custom nodes for video generation through BenpayAI video protocols. The nodes load the currently available video suppliers and products from Benfengate at startup, while keeping the model field editable for manual model selection.

## Features

- Select a video protocol from the suppliers currently exposed by Benfengate.
- Use the recommended model loaded from the public supplier catalog.
- Enter any model identifier manually when needed.
- Text-to-video and first-frame-to-video generation.
- Automatic task polling, video download, and ComfyUI preview output.
- Local catalog cache and built-in fallback values when Benfengate is unavailable.

## Installation

Install this project under the `custom_nodes` directory of your ComfyUI installation:

```bash
cd ComfyUI/custom_nodes
git clone <repository-url> benpayai-video
```

Restart ComfyUI after installation. The nodes are available under the `video/BenPay` category:

- **BenpayAI Video Text to Video**
- **BenpayAI Video Image/Reference Video to Video**

## Configuration

The public Benfengate catalog and video gateway routes are loaded from:

```text
GET https://{BENFEN_GATEWAY}/api/v1/benpay-ai/suppliers
GET https://{BENFEN_GATEWAY}/api/v1/benpay-ai/call-config
```

The default gateway is `https://cli-gateway.benfenpay.com`.

The catalog request is public and does not require an API key. Set the video gateway API key before generating a video:

```bash
export BENPAYAI_API_KEY="your-api-key"
```

The node reads the complete HTTPS create and query URLs from `data.videoProtocols` in `call-config` and uses those URLs directly. The video gateway domain is configured in one place by Benfengate.

## Catalog loading and fallback

At startup, the node reads the local catalog cache and then requests the latest supplier catalog. A successful response updates the in-memory catalog and the cache. If the request fails, the node uses the previous cache; if no cache exists, it falls back to the built-in `doubao` protocol and `doubao-seedance-2.0` model. Catalog loading has a five-second timeout and never prevents node registration.

The default cache file is `benpayai-video-suppliers.json` in the ComfyUI user directory. Set `BENPAYAI_SUPPLIER_CACHE_FILE` to use another path.

## Video protocols

The current protocol routes are:

- Doubao: `POST /doubao/v1/video/generations`, `GET /doubao/v1/video/generations/{task_id}`
- MiniMax: `POST /minimax/v2/video_generation`, `GET /minimax/v2/video_generation/{task_id}`

The `protocol` input uses supplier identifiers returned by Benfengate. The `model` input is an editable string whose default comes from the first video product for the selected startup default protocol.

The image node supports `first_frame`, `first_last_frame`, `reference_images`, and `reference_video` modes. Reference video URLs must use HTTPS or a data URL. Doubao and MiniMax requests are assembled by separate protocol adapters because their payload formats differ.

## License

This project is licensed under the MIT License.
