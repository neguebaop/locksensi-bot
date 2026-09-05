import asyncio
import io
from datetime import datetime, timezone
from typing import Optional

import discord
from discord import app_commands

from database import db, ensure_schema


BOT = None
ADMIN_CHECK = None


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def init_db():
    ensure_schema()


def resolve_channel(interaction, selected):
    if interaction.guild is None:
        return None
    return (
        interaction.guild.get_channel(selected.id)
        or interaction.guild.get_thread(selected.id)
    )


class MessageCommands(app_commands.Group):
    def __init__(self):
        super().__init__(
            name="mensagem",
            description="Mensagens premium em canal ou privado",
        )

    @app_commands.command(
        name="canal",
        description="Envia embed com imagem ou arquivo em um canal",
    )
    async def channel(
        self,
        i: discord.Interaction,
        canal: app_commands.AppCommandChannel,
        titulo: str,
        mensagem: str,
        imagem_url: str = "",
        thumbnail_url: str = "",
        arquivo: Optional[discord.Attachment] = None,
        cor_hex: str = "7c2de2",
    ):
        if ADMIN_CHECK and not await ADMIN_CHECK(i):
            return

        channel = resolve_channel(i, canal)

        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            await i.response.send_message(
                "❌ Selecione um canal de texto comum ou um tópico.",
                ephemeral=True,
            )
            return

        permissions = channel.permissions_for(i.guild.me)

        if not permissions.view_channel or not permissions.send_messages:
            await i.response.send_message(
                "❌ O bot não possui permissão para visualizar ou enviar mensagens nesse canal.",
                ephemeral=True,
            )
            return

        try:
            color = int(cor_hex.replace("#", ""), 16)
        except (TypeError, ValueError):
            color = 0x7C2DE2

        embed = discord.Embed(
            title=titulo,
            description=mensagem[:4096],
            color=color,
        )

        if imagem_url:
            embed.set_image(url=imagem_url)

        if thumbnail_url:
            embed.set_thumbnail(url=thumbnail_url)

        file = None

        if arquivo:
            data = await arquivo.read()
            file = discord.File(io.BytesIO(data), filename=arquivo.filename)

            if (
                arquivo.content_type
                and arquivo.content_type.startswith("image/")
                and not imagem_url
            ):
                embed.set_image(url=f"attachment://{arquivo.filename}")

        await i.response.defer(ephemeral=True, thinking=True)

        try:
            await channel.send(embed=embed, file=file)
            await i.followup.send(
                f"✅ Mensagem enviada em {channel.mention}.",
                ephemeral=True,
            )
        except Exception as error:
            await i.followup.send(
                f"❌ Falha ao enviar: `{str(error)[:500]}`",
                ephemeral=True,
            )

    @app_commands.command(
        name="pv",
        description="Envia mensagem, imagem ou arquivo no privado",
    )
    async def dm(
        self,
        i: discord.Interaction,
        usuario: discord.User,
        titulo: str,
        mensagem: str,
        imagem_url: str = "",
        arquivo: Optional[discord.Attachment] = None,
    ):
        if ADMIN_CHECK and not await ADMIN_CHECK(i):
            return

        embed = discord.Embed(
            title=titulo,
            description=mensagem[:4096],
            color=0x7C2DE2,
        )

        if imagem_url:
            embed.set_image(url=imagem_url)

        file = None

        if arquivo:
            data = await arquivo.read()
            file = discord.File(io.BytesIO(data), filename=arquivo.filename)

            if (
                arquivo.content_type
                and arquivo.content_type.startswith("image/")
                and not imagem_url
            ):
                embed.set_image(url=f"attachment://{arquivo.filename}")

        await i.response.defer(ephemeral=True, thinking=True)

        try:
            await usuario.send(embed=embed, file=file)
            await i.followup.send("✅ Enviado no privado.", ephemeral=True)
        except Exception as error:
            await i.followup.send(
                "❌ Não consegui enviar. O usuário pode estar com a DM fechada. "
                f"`{str(error)[:300]}`",
                ephemeral=True,
            )


class VoiceCommands(app_commands.Group):
    def __init__(self):
        super().__init__(
            name="voz",
            description="Conexão persistente do bot em canal de voz",
        )

    @app_commands.command(
        name="configurar",
        description="Salva o canal e conecta automaticamente após reinícios",
    )
    async def configure(
        self,
        i: discord.Interaction,
        canal: app_commands.AppCommandChannel,
    ):
        if ADMIN_CHECK and not await ADMIN_CHECK(i):
            return

        channel = resolve_channel(i, canal)

        if not isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
            await i.response.send_message(
                "❌ Selecione uma call ou um canal de palco.",
                ephemeral=True,
            )
            return

        con = db()
        con.execute(
            """
            INSERT INTO guild_voice_config(
                guild_id, channel_id, enabled, updated_at
            )
            VALUES(?,?,1,?)
            ON CONFLICT(guild_id) DO UPDATE SET
                channel_id=excluded.channel_id,
                enabled=1,
                updated_at=excluded.updated_at
            """,
            (i.guild.id, channel.id, now_iso()),
        )
        con.commit()
        con.close()

        await i.response.defer(ephemeral=True, thinking=True)

        try:
            vc = i.guild.voice_client

            if vc and vc.is_connected():
                await vc.move_to(channel)
            else:
                await channel.connect(
                    reconnect=True,
                    self_deaf=True,
                    timeout=30,
                )

            await i.followup.send(
                f"✅ Canal salvo. Vou reconectar automaticamente em {channel.mention}.",
                ephemeral=True,
            )
        except Exception as error:
            await i.followup.send(
                "⚠️ Configuração salva, mas a conexão falhou agora: "
                f"`{str(error)[:400]}`",
                ephemeral=True,
            )

    @app_commands.command(
        name="desativar",
        description="Desativa a reconexão automática e sai da call",
    )
    async def disable(self, i: discord.Interaction):
        if ADMIN_CHECK and not await ADMIN_CHECK(i):
            return

        con = db()
        con.execute(
            "UPDATE guild_voice_config SET enabled=0,updated_at=? WHERE guild_id=?",
            (now_iso(), i.guild.id),
        )
        con.commit()
        con.close()

        if i.guild.voice_client:
            await i.guild.voice_client.disconnect(force=True)

        await i.response.send_message(
            "✅ Reconexão automática desativada.",
            ephemeral=True,
        )

    @app_commands.command(
        name="status",
        description="Mostra o canal de voz salvo",
    )
    async def status(self, i: discord.Interaction):
        con = db()
        row = con.execute(
            "SELECT * FROM guild_voice_config WHERE guild_id=?",
            (i.guild.id,),
        ).fetchone()
        con.close()

        text = (
            f"Canal salvo: <#{row['channel_id']}> • Ativo: `{bool(row['enabled'])}`"
            if row
            else "Nenhum canal salvo."
        )

        await i.response.send_message(text, ephemeral=True)


async def ensure_voice(guild):
    con = db()
    row = con.execute(
        "SELECT * FROM guild_voice_config WHERE guild_id=? AND enabled=1",
        (guild.id,),
    ).fetchone()
    con.close()

    if not row:
        return

    channel = guild.get_channel(row["channel_id"])

    if not isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
        return

    try:
        vc = guild.voice_client

        if vc and vc.is_connected():
            if vc.channel.id != channel.id:
                await vc.move_to(channel)
        else:
            await channel.connect(
                reconnect=True,
                self_deaf=True,
                timeout=30,
            )
    except Exception as error:
        print(f"Voz {guild.id}: {error}")


async def voice_monitor():
    await BOT.wait_until_ready()

    while not BOT.is_closed():
        for guild in BOT.guilds:
            await ensure_voice(guild)
            await asyncio.sleep(1)

        await asyncio.sleep(25)


async def setup(bot, admin_check=None):
    global BOT, ADMIN_CHECK

    BOT = bot
    ADMIN_CHECK = admin_check
    init_db()

    for command in (MessageCommands(), VoiceCommands()):
        try:
            bot.tree.add_command(command)
        except app_commands.CommandAlreadyRegistered:
            pass

    if not getattr(bot, "_voice_monitor_started", False):
        bot._voice_monitor_started = True
        asyncio.create_task(voice_monitor())