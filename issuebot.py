"""Discord listener. Tag the bot (usually as a reply to a report) and it triages into GitHub.

Conversation follow-ups happen in a thread: once the bot answers there, allowed users can
reply in that thread without tagging it again.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from dataclasses import dataclass, field

import discord
from anthropic import AsyncAnthropic

from agent import Request, run_triage
from config import Config, load_config
from github_api import GitHub

log = logging.getLogger("issuebot")
NO_PINGS = discord.AllowedMentions.none()
MESSAGE_LINK = re.compile(
    r"https?://(?:ptb\.|canary\.)?discord(?:app)?\.com/channels/(\d+)/(\d+)/(\d+)"
)
MAX_LINKS = 3
LINK_CONTEXT_MESSAGES = 5  # messages before a linked report, for context


@dataclass
class Session:
    req: Request
    transcript: list[dict]
    updated: float = field(default_factory=time.time)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


def strip_mentions(text: str, bot_id: int) -> str:
    return re.sub(rf"<@!?{bot_id}>", "", text).strip()


def describe(m: discord.Message) -> str:
    parts = [m.content or ""]
    parts += [f"[attachment: {a.url}]" for a in m.attachments]
    parts += [f"[embed: {e.title or ''} {e.url or ''}]".strip() for e in m.embeds if e.title or e.url]
    body = "\n".join(p for p in parts if p).strip() or "(no text)"
    return f"{m.author.display_name}: {body}"


def chunk(text: str, limit: int = 1900) -> list[str]:
    out = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        cut = cut if cut > 0 else limit
        out.append(text[:cut])
        text = text[cut:].lstrip("\n")
    out.append(text)
    return out


def thread_name(text: str) -> str:
    text = MESSAGE_LINK.sub("", text)
    text = re.sub(r"<(?:@[!&]?|#)\d+>", "", text)  # raw mention/channel tokens
    text = re.sub(r"\s+", " ", text).strip() or "issue"
    return ("Triage: " + text)[:95]


class IssueBot(discord.Client):
    def __init__(self, cfg: Config):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents, allowed_mentions=NO_PINGS)
        self.cfg = cfg
        self.claude = AsyncAnthropic(api_key=cfg.anthropic_api_key)
        self.gh = GitHub(cfg.github_token, dry_run=cfg.dry_run)
        self.sessions: dict[int, Session] = {}  # Discord thread id -> conversation

    async def close(self):
        await self.gh.close()
        await super().close()

    async def on_ready(self):
        mode = " (DRY RUN: nothing will be written to GitHub)" if self.cfg.dry_run else ""
        log.info("Logged in as %s; repos: %s%s", self.user, ", ".join(self.cfg.repos), mode)

    def _expire_sessions(self):
        cutoff = time.time() - self.cfg.session_ttl_hours * 3600
        for k in [k for k, s in self.sessions.items() if s.updated < cutoff]:
            del self.sessions[k]

    async def on_message(self, message: discord.Message):
        if message.author.bot or self.user is None:
            return
        if message.author.id not in self.cfg.allowed_user_ids:
            return
        self._expire_sessions()

        session = self.sessions.get(message.channel.id)
        if session is not None:
            await self._continue(message, session)
        elif self.user in message.mentions:
            await self._start(message)

    async def _linked_message(
        self, message: discord.Message, guild_id: int, channel_id: int, message_id: int
    ) -> tuple[discord.Message | None, str]:
        """Fetch a message from a pasted link. Returns (message, error)."""
        if message.guild is None or guild_id != message.guild.id:
            return None, "it's from a different server"
        channel = self.get_channel(channel_id)
        try:
            if channel is None:
                channel = await self.fetch_channel(channel_id)
            # Don't let a link pull a channel's contents somewhere its readers didn't choose:
            # the requester must be able to read that channel themselves.
            perms = channel.permissions_for(message.author)
            if not (perms.view_channel and perms.read_message_history):
                return None, "you can't read that channel"
            return await channel.fetch_message(message_id), ""
        except discord.Forbidden:
            return None, "I don't have access to that channel"
        except discord.NotFound:
            return None, "that message doesn't exist (deleted?)"
        except discord.HTTPException as e:
            return None, f"Discord error {e.status}"

    async def _gather_context(self, message: discord.Message) -> tuple[str, str]:
        """Returns (context for the agent, text of the reported message for the thread name)."""
        reported: list[discord.Message] = []
        instruction = strip_mentions(message.content, self.user.id) or "(no extra instructions)"
        sections = [f"Maintainer's request:\n{instruction}"]

        ref = message.reference
        if ref and ref.message_id:
            target = ref.resolved if isinstance(ref.resolved, discord.Message) else None
            if target is None:
                try:
                    target = await message.channel.fetch_message(ref.message_id)
                except discord.HTTPException:
                    target = None
            if target is not None:
                reported.append(target)
                sections.append(
                    f"The message being reported (link: {target.jump_url}):\n{describe(target)}"
                )

        for g, c, m in MESSAGE_LINK.findall(message.content)[:MAX_LINKS]:
            linked, err = await self._linked_message(message, int(g), int(c), int(m))
            if linked is None:
                sections.append(f"A linked message could not be read ({err}). Tell the maintainer.")
                continue
            reported.append(linked)
            section = f"Linked message being reported (link: {linked.jump_url}):\n{describe(linked)}"
            before = [
                describe(h)
                async for h in linked.channel.history(limit=LINK_CONTEXT_MESSAGES, before=linked)
            ]
            if before:
                section += "\n\nMessages just before it, oldest first:\n" + "\n".join(reversed(before))
            sections.append(section)

        history = [
            describe(m)
            async for m in message.channel.history(limit=self.cfg.context_messages, before=message)
        ]
        if history:
            sections.append("Recent channel messages, oldest first:\n" + "\n".join(reversed(history)))
        return "\n\n".join(sections), (reported[0].content if reported else "")

    async def _reply_target(self, message: discord.Message, name_hint: str = ""):
        """Use the current thread, or open one off the tagged message for the conversation."""
        if isinstance(message.channel, discord.Thread):
            return message.channel
        try:
            return await message.create_thread(
                name=thread_name(name_hint or strip_mentions(message.content, self.user.id)),
                auto_archive_duration=1440,
            )
        except (discord.HTTPException, ValueError) as e:
            log.warning("Could not create thread, replying inline: %s", e)
            return message.channel

    async def _start(self, message: discord.Message):
        await self._react(message, "👀")
        context_text, reported_text = await self._gather_context(message)
        req = Request(
            requester_name=message.author.display_name,
            requester_id=message.author.id,
            jump_url=message.jump_url,
            context_text=context_text,
        )
        target = await self._reply_target(message, reported_text)
        session = Session(req=req, transcript=[])
        async with session.lock:
            ok = await self._run(target, session)
        if ok and isinstance(target, discord.Thread):
            self.sessions[target.id] = session
        await self._react(message, "✅" if ok else "⚠️")

    async def _continue(self, message: discord.Message, session: Session):
        followup = strip_mentions(message.content, self.user.id)
        if not followup:
            return
        async with session.lock:
            await self._run(message.channel, session, followup=followup)

    async def _run(self, target, session: Session, followup: str | None = None) -> bool:
        try:
            async with target.typing():
                result = await run_triage(
                    self.claude,
                    self.cfg,
                    self.gh,
                    session.req,
                    history=session.transcript or None,
                    followup=followup,
                )
        except Exception:  # noqa: BLE001
            log.exception("Triage failed")
            await target.send("Something went wrong talking to Claude or GitHub. Check the bot logs.")
            return False
        session.transcript = result.transcript
        session.updated = time.time()
        for a in result.actions:
            log.info(a)
        log.info("usage: %s", result.usage)
        for part in chunk(result.reply):
            await target.send(part, suppress_embeds=True)
        return True

    async def _react(self, message: discord.Message, emoji: str):
        try:
            await message.add_reaction(emoji)
        except discord.HTTPException:
            pass


def main():
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    cfg = load_config(os.environ.get("CONFIG_PATH", "config.toml"))
    IssueBot(cfg).run(cfg.discord_token, log_handler=None)


if __name__ == "__main__":
    main()
