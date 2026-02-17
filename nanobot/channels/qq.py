"""QQ channel implementation using botpy SDK."""

import asyncio
from collections import deque
import mimetypes
import tempfile
from typing import TYPE_CHECKING
import aiohttp

from loguru import logger

from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.base import BaseChannel
from nanobot.config.schema import QQConfig

try:
    import botpy
    from botpy.message import C2CMessage

    QQ_AVAILABLE = True
except ImportError:
    QQ_AVAILABLE = False
    botpy = None
    C2CMessage = None

if TYPE_CHECKING:
    from botpy.message import C2CMessage


def _make_bot_class(channel: "QQChannel") -> "type[botpy.Client]":
    """Create a botpy Client subclass bound to the given channel."""
    intents = botpy.Intents(public_messages=True, direct_message=True)

    class _Bot(botpy.Client):
        def __init__(self):
            super().__init__(intents=intents)

        async def on_ready(self):
            logger.info(f"QQ bot ready: {self.robot.name}")

        async def on_c2c_message_create(self, message: "C2CMessage"):
            await channel._on_message(message)

        async def on_direct_message_create(self, message):
            await channel._on_message(message)

    return _Bot


class QQChannel(BaseChannel):
    """QQ channel using botpy SDK with WebSocket connection."""

    name = "qq"

    def __init__(self, config: QQConfig, bus: MessageBus):
        super().__init__(config, bus)
        self.config: QQConfig = config
        self._client: "botpy.Client | None" = None
        self._processed_ids: deque = deque(maxlen=1000)
        self._bot_task: asyncio.Task | None = None

    async def start(self) -> None:
        """Start the QQ bot."""
        if not QQ_AVAILABLE:
            logger.error("QQ SDK not installed. Run: pip install qq-botpy")
            return

        if not self.config.app_id or not self.config.secret:
            logger.error("QQ app_id and secret not configured")
            return

        self._running = True
        BotClass = _make_bot_class(self)
        self._client = BotClass()

        self._bot_task = asyncio.create_task(self._run_bot())
        logger.info("QQ bot started (C2C private message)")

    async def _run_bot(self) -> None:
        """Run the bot connection with auto-reconnect."""
        while self._running:
            try:
                await self._client.start(appid=self.config.app_id, secret=self.config.secret)
            except Exception as e:
                logger.warning(f"QQ bot error: {e}")
            if self._running:
                logger.info("Reconnecting QQ bot in 5 seconds...")
                await asyncio.sleep(5)

    async def stop(self) -> None:
        """Stop the QQ bot."""
        self._running = False
        if self._bot_task:
            self._bot_task.cancel()
            try:
                await self._bot_task
            except asyncio.CancelledError:
                pass
        logger.info("QQ bot stopped")

    async def send(self, msg: OutboundMessage) -> None:
        """Send a message through QQ."""
        if not self._client:
            logger.warning("QQ client not initialized")
            return
        try:
            await self._client.api.post_c2c_message(
                openid=msg.chat_id,
                msg_type=0,
                content=msg.content,
            )
        except Exception as e:
            logger.error(f"Error sending QQ message: {e}")

    async def _download_media(self, url: str) -> str | None:
        """Download media to a temporary file."""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as response:
                    if response.status == 200:
                        content = await response.read()
                        content_type = response.headers.get("Content-Type", "")
                        ext = mimetypes.guess_extension(content_type) or ".jpg"

                        with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as tf:
                            tf.write(content)
                            return tf.name
        except Exception as e:
            logger.error(f"Failed to download media: {e}")
        return None

    async def _on_message(self, data: "C2CMessage") -> None:
        """Handle incoming message from QQ."""
        try:
            # Dedup by message ID
            if data.id in self._processed_ids:
                return
            self._processed_ids.append(data.id)

            author = data.author
            user_id = str(getattr(author, 'id', None) or getattr(author, 'user_openid', 'unknown'))
            raw_content = (data.content or "").strip()

            # --- 指令解析与上下文管理 ---
            content = raw_content
            metadata = {"message_id": data.id}

            if content.startswith("/"):
                # 分割指令，最多分3部分：/cmd arg text
                parts = content.split(" ", 2)
                cmd = parts[0].lower()

                # 1. 重置指令：/reset
                # 告诉 Agent 清空当前会话的历史记录
                if cmd == "/reset":
                    metadata["reset_session"] = True
                    content = "Reset conversation context." # 替换提示语
                
                # 2. 上下文长度指令：/context 10 [后续消息]
                # 临时指定本次对话读取的历史消息数量
                elif cmd == "/context":
                    if len(parts) >= 2 and parts[1].isdigit():
                        metadata["memory_window"] = int(parts[1])
                        # 如果后面还有内容，则作为本次消息内容；否则仅作为系统设置提示
                        content = parts[2] if len(parts) > 2 else f"Set context window to {parts[1]}"
            # ---------------------------

            # Handle attachments (images)
            media = []
            if hasattr(data, "attachments") and data.attachments:
                for att in data.attachments:
                    if hasattr(att, "url") and att.url:
                        if path := await self._download_media(att.url):
                            media.append(path)

            if not content and not media:
                return

            await self._handle_message(
                sender_id=user_id,
                chat_id=user_id,
                content=content or "[Image Message]",
                metadata=metadata,
                media=media,
            )
        except Exception as e:
            logger.error(f"Error handling QQ message: {e}")
