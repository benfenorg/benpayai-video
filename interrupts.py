"""Make network waits responsive to ComfyUI's processing-interrupt flag."""

import asyncio
from collections.abc import Awaitable
from contextlib import suppress
from typing import TypeVar

from comfy.model_management import throw_exception_if_processing_interrupted

T = TypeVar("T")
INTERRUPT_CHECK_SECONDS = 0.25


async def run_interruptible(operation: Awaitable[T]) -> T:
    task = asyncio.ensure_future(operation)
    try:
        while True:
            throw_exception_if_processing_interrupted()
            completed, _ = await asyncio.wait({task}, timeout=INTERRUPT_CHECK_SECONDS)
            if completed:
                throw_exception_if_processing_interrupted()
                return await task
    finally:
        if not task.done():
            task.cancel()
        # Consume child cancellation/errors while preserving the original result
        # or interrupt. A cancelled video download can finish its file cleanup.
        with suppress(asyncio.CancelledError, Exception):
            await task
