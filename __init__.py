"""ComfyUI video generation nodes for BenpayAI protocols."""

from .nodes import BenpayAIVideo, BenpayAIVideoFromImages


NODE_CLASS_MAPPINGS = {
    "BenpayAIVideo": BenpayAIVideo,
    "BenpayAIVideoFromImages": BenpayAIVideoFromImages,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "BenpayAIVideo": "BenpayAI Video 文生视频",
    "BenpayAIVideoFromImages": "BenpayAI Video 图片/参考视频生成",
}
