"""ComfyUI video generation nodes for BenPay protocols."""

from server import PromptServer

from .configuration_routes import register_configuration_routes
from .nodes import BenpayVideo, BenpayVideoFromImages

WEB_DIRECTORY = "./web"


register_configuration_routes(PromptServer.instance.routes)


NODE_CLASS_MAPPINGS = {
    "BenpayVideo": BenpayVideo,
    "BenpayVideoFromImages": BenpayVideoFromImages,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "BenpayVideo": "BenPay Video Text to Video",
    "BenpayVideoFromImages": "BenPay Video Image to Video",
}
