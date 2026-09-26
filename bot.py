import os, asyncio, json, io, random, string, traceback, re
from datetime import datetime, timedelta
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv
import qrcode
from flask import Flask, request, jsonify
from threading import Thread
from database import db, ensure_schema
import checkout_system
import verification_system
import utility_system

app = Flask("")


@app.route("/")
def home():
    try:
        if "bot" in globals() and bot.is_ready():
            return f"Bot online! Discord conectado como {bot.user}", 200
    except Exception:
        pass
    return "Servidor web online. Discord ainda conectando...", 200


@app.route("/webhooks/misticpay/<token>", methods=["POST"])
def misticpay_webhook(token):
    if not os.getenv("MISTICPAY_WEBHOOK_TOKEN", "") or token != os.getenv(
        "MISTICPAY_WEBHOOK_TOKEN", ""
    ):
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    payload = request.get_json(silent=True) or {}
    try:
        if bot.loop and bot.loop.is_running():
            asyncio.run_coroutine_threadsafe(
                checkout_system.webhook_process(payload), bot.loop
            )
    except Exception as exc:
        print("Webhook MisticPay:", exc)
    return jsonify({"ok": True}), 200


def run_web():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)


def keep_alive():
    # Se o Discord falhar, o Flask não mantém o processo falsamente "Live".
    t = Thread(target=run_web, daemon=True, name="LockSensi-Web")
    t.start()


load_dotenv()
TOKEN = (os.getenv("DISCORD_TOKEN") or "").strip()
PIX_KEY = os.getenv("PIX_KEY", "")
PIX_NOME = os.getenv("PIX_NOME", "LOJA")[:25]
PIX_CIDADE = os.getenv("PIX_CIDADE", "SAO PAULO")[:15]
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "")
OWNER_IDS = [
    int(x)
    for x in os.getenv("OWNER_IDS", "").replace(";", ",").split(",")
    if x.strip().isdigit()
]
DEFAULT_TRIAL_DAYS = int(os.getenv("DEFAULT_TRIAL_DAYS", "0"))
TICKET_IMAGE_URL = os.getenv("TICKET_IMAGE_URL", "")
TICKET_THUMB_URL = os.getenv("TICKET_THUMB_URL", "")
TICKET_CATEGORY_NAME = os.getenv("TICKET_CATEGORY_NAME", "tickets")
TICKET_PANEL_TITLE = os.getenv("TICKET_PANEL_TITLE", "🤖 Central de Suporte")
TICKET_PANEL_DESC = os.getenv(
    "TICKET_PANEL_DESC",
    "Olá! Bem-vindo ao nosso sistema de suporte.\n\n• Abra um ticket apenas se necessário\n• Respeite as regras do servidor\n• Nossa equipe responderá o mais rápido possível\n\nEntregas Automáticas • LinkRoubadão",
)

if not TOKEN or TOKEN == "COLE_SEU_TOKEN_AQUI":
    print("ERROR: coloque DISCORD_TOKEN no arquivo .env")
    raise SystemExit

intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.message_content = True
intents.voice_states = True
bot = commands.Bot(command_prefix="!", intents=intents)
RANKING_REFRESH_LOCKS = {}
RANKING_REFRESH_TIMES = {}


# ================= HELPERS =================
def valid_url(url: Optional[str]) -> bool:
    if not url:
        return False
    url = str(url).strip()
    return bool(re.match(r"^https?://[^\s]+\.[^\s]+", url))


def add_column_if_missing(cur, table, col, typ):
    # O esquema PostgreSQL completo é criado por supabase_schema.sql.
    return None


def init_db():
    ensure_schema()


init_db()


def ensure_config(guild_id: int):
    con = db()
    cur = con.cursor()
    cur.execute(
        "INSERT OR IGNORE INTO guild_config(guild_id,pix_key,pix_name,pix_city,webhook_url) VALUES(?,?,?,?,?)",
        (guild_id, PIX_KEY, PIX_NOME, PIX_CIDADE, WEBHOOK_URL),
    )
    con.commit()
    con.close()


def get_config(guild_id: int):
    ensure_config(guild_id)
    con = db()
    row = con.execute(
        "SELECT * FROM guild_config WHERE guild_id=?", (guild_id,)
    ).fetchone()
    con.close()
    return row


def now_iso():
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")


def parse_iso(dt):
    if not dt:
        return None
    try:
        return datetime.strptime(
            str(dt).split(".")[0].replace("T", " "), "%Y-%m-%d %H:%M:%S"
        )
    except Exception:
        return None


def is_owner_user(user_id: int):
    return int(user_id) in OWNER_IDS


def get_subscription(guild_id: int):
    con = db()
    row = con.execute(
        "SELECT * FROM guild_subscriptions WHERE guild_id=?", (guild_id,)
    ).fetchone()
    con.close()
    return row


def guild_has_plan(guild_id: int):
    row = get_subscription(guild_id)
    if not row or int(row["active"] or 0) != 1:
        return False
    exp = parse_iso(row["expires_at"])
    return exp is None or exp >= datetime.utcnow()


def plan_text(guild_id: int):
    row = get_subscription(guild_id)
    if not row:
        return "❌ Sem plano ativo"
    exp = row["expires_at"] or "sem vencimento"
    return (
        f"✅ Plano **{row['plan_name']}** ativo até `{exp}`"
        if guild_has_plan(guild_id)
        else f"❌ Plano expirado em `{exp}`"
    )


def get_customization(guild_id: int):
    ensure_config(guild_id)
    con = db()
    cur = con.cursor()
    cur.execute(
        "INSERT OR IGNORE INTO guild_customization(guild_id,store_name,color) SELECT guild_id,store_name,color FROM guild_config WHERE guild_id=?",
        (guild_id,),
    )
    con.commit()
    row = cur.execute(
        "SELECT * FROM guild_customization WHERE guild_id=?", (guild_id,)
    ).fetchone()
    con.close()
    return row


def parse_color(value, default=0x5865F2):
    if value is None:
        return default
    try:
        return int(str(value).strip().replace("#", "").replace("0x", ""), 16)
    except Exception:
        return default


async def require_active_plan(inter):
    if not inter.guild:
        return True
    if is_owner_user(inter.user.id):
        return True
    if guild_has_plan(inter.guild.id):
        return True
    await inter.response.send_message(
        f"🔒 Este servidor ainda não tem plano ativo.\n\nID do servidor: `{inter.guild.id}`\nPeça ao dono do bot para ativar com `/ativar-servidor`.",
        ephemeral=True,
    )
    return False


async def protected_admin_only(inter):
    if not await admin_only(inter):
        return False
    return await require_active_plan(inter)


async def owner_only(inter):
    if not is_owner_user(inter.user.id):
        await inter.response.send_message(
            "❌ Apenas o dono do bot pode usar isso.", ephemeral=True
        )
        return False
    return True


def is_admin(inter: discord.Interaction):
    return inter.user.guild_permissions.administrator or inter.user.id in OWNER_IDS


async def admin_only(inter):
    if not is_admin(inter):
        await inter.response.send_message(
            "❌ Apenas administradores podem usar isso.", ephemeral=True
        )
        return False
    return True


def money(v):
    return f"R${float(v):.2f}".replace(".", ",")


def parse_price_input(value):
    """Converte preço digitado em PT-BR ou padrão decimal sem confundir 19,99 com 1999."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return round(float(value), 2)

    raw = str(value).strip().upper().replace("R$", "").replace(" ", "")
    raw = re.sub(r"[^0-9,.-]", "", raw)
    if not raw:
        raise ValueError("Preço vazio")

    if "," in raw and "." in raw:
        # O último separador é o decimal; o outro é tratado como milhar.
        if raw.rfind(",") > raw.rfind("."):
            raw = raw.replace(".", "").replace(",", ".")
        else:
            raw = raw.replace(",", "")
    elif "," in raw:
        raw = raw.replace(".", "").replace(",", ".")
    elif raw.count(".") > 1:
        parts = raw.split(".")
        raw = "".join(parts[:-1]) + "." + parts[-1]

    price = round(float(raw), 2)
    if price < 0:
        raise ValueError("Preço não pode ser negativo")
    return price


def random_code(n=8):
    return "".join(
        random.choice(string.ascii_uppercase + string.digits) for _ in range(n)
    )


def pix_payload(key, name, city, amount, txid):
    return f"PIX|CHAVE:{key}|NOME:{name}|CIDADE:{city}|VALOR:{amount:.2f}|TXID:{txid}"


def make_qr_bytes(text):
    img = qrcode.make(text)
    bio = io.BytesIO()
    img.save(bio, format="PNG")
    bio.seek(0)
    return bio


async def log(guild: discord.Guild, msg: str):
    cfg = get_config(guild.id)
    ch = (
        guild.get_channel(cfg["log_channel_id"])
        if cfg and cfg["log_channel_id"]
        else None
    )
    if ch:
        try:
            await ch.send(msg)
        except Exception:
            pass


# ================= UI VENDAS =================
class ProductBuyButton(discord.ui.Button):
    def __init__(self, product_id: int):
        super().__init__(
            label="🛒 Comprar agora",
            style=discord.ButtonStyle.green,
            custom_id=f"buy_product_{int(product_id)}",
        )
        self.product_id = int(product_id)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        await start_order(interaction, self.product_id)


class BuyView(discord.ui.View):
    def __init__(self, product_id: int = None, panel_id: int = None):
        super().__init__(timeout=None)
        self.product_id = product_id
        self.panel_id = panel_id
        if product_id:
            self.add_item(ProductBuyButton(int(product_id)))
        if panel_id:
            self.add_item(PanelSelect(panel_id))


class PanelOnlyView(discord.ui.View):
    def __init__(self, panel_id: int):
        super().__init__(timeout=None)
        self.add_item(PanelSelect(panel_id))


def get_panel_mode(panel_id: int) -> str:
    con = db()
    try:
        row = con.execute(
            "SELECT display_mode FROM panels WHERE id=?", (int(panel_id),)
        ).fetchone()
    finally:
        con.close()
    mode = str(row["display_mode"] or "select").lower() if row else "select"
    return "button" if mode == "button" else "select"


class PanelOptionsButton(discord.ui.Button):
    def __init__(self, panel_id: int):
        super().__init__(
            label="Opções",
            emoji="🛒",
            style=discord.ButtonStyle.secondary,
            custom_id=f"panel_options_{int(panel_id)}",
            row=0,
        )
        self.panel_id = int(panel_id)

    async def callback(self, interaction: discord.Interaction):
        # Confirma o clique imediatamente. Carregar os produtos consulta o banco
        # e, em hospedagens como Render, pode levar mais de 3 segundos.
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            view = PanelOnlyView(self.panel_id)
            await interaction.followup.send(
                "**Selecione um Produto**",
                view=view,
                ephemeral=True,
            )
        except Exception as exc:
            print(f"Erro ao abrir opções do painel {self.panel_id}: {exc}")
            await interaction.followup.send(
                "Não consegui carregar os produtos deste painel agora. Tente novamente em alguns segundos.",
                ephemeral=True,
            )


class PanelOptionsView(discord.ui.LayoutView):
    def __init__(self, panel_id: int):
        super().__init__(timeout=None)
        self.panel_id = int(panel_id)

        con = db()
        try:
            panel = con.execute(
                "SELECT * FROM panels WHERE id=?", (self.panel_id,)
            ).fetchone()
        finally:
            con.close()

        if not panel:
            self.add_item(discord.ui.TextDisplay("## Painel não encontrado"))
            return

        try:
            custom = get_customization(int(panel["guild_id"]))
            store_name = custom["store_name"] or "Entregas automática"
            custom_color = custom["color"] if custom else None
        except Exception:
            store_name = "Entregas automática"
            custom_color = None

        # Cor individual do painel tem prioridade sobre a cor global da loja.
        # -1 é usado como sentinela para "sem barra / transparente".
        panel_color = panel["color"]
        try:
            panel_color = int(panel_color) if panel_color is not None else None
        except Exception:
            panel_color = None

        if panel_color == -1:
            color = None
        elif panel_color is not None:
            color = panel_color
        else:
            color = int(custom_color or 0x5865F2)

        title = str(panel["title"] or panel["name"] or "Produtos").strip()
        description = str(panel["description"] or "Confira as opções disponíveis.").strip()
        large_image = str(panel["banner_url"] or panel["image_url"] or "").strip()

        # Sem accent_color = sem a barra colorida lateral.
        container = (
            discord.ui.Container()
            if color is None
            else discord.ui.Container(accent_color=int(color))
        )
        if valid_url(large_image):
            gallery = discord.ui.MediaGallery()
            gallery.add_item(media=large_image, description=title[:256])
            container.add_item(gallery)

        container.add_item(discord.ui.Separator(visible=True))
        container.add_item(discord.ui.TextDisplay(f"## {title}"))
        container.add_item(discord.ui.Separator(visible=True))
        container.add_item(discord.ui.TextDisplay(description[:3900]))
        container.add_item(discord.ui.Separator(visible=True))
        container.add_item(
            discord.ui.TextDisplay(f"-# {store_name} • Painel de vendas")
        )

        self.add_item(container)
        self.add_item(discord.ui.ActionRow(PanelOptionsButton(self.panel_id)))


def panel_view(panel_id: int):
    if get_panel_mode(panel_id) == "button":
        return PanelOptionsView(panel_id)
    return PanelOnlyView(panel_id)


def panel_send_kwargs(panel_id: int):
    """Monta a mensagem correta para o modo clássico ou Components V2."""
    view = panel_view(panel_id)
    if isinstance(view, discord.ui.LayoutView):
        return {"view": view}
    return {"embed": panel_embed(panel_id), "view": view}


async def refresh_published_panel(guild: discord.Guild, panel_id: int) -> bool:
    """Atualiza a última mensagem publicada do painel, quando ela ainda existe."""
    if not guild:
        return False

    con = db()
    try:
        panel = con.execute(
            "SELECT * FROM panels WHERE id=? AND guild_id=?",
            (int(panel_id), int(guild.id)),
        ).fetchone()
    finally:
        con.close()

    if not panel or not panel["channel_id"] or not panel["message_id"]:
        return False

    try:
        channel = guild.get_channel(int(panel["channel_id"]))
        if channel is None:
            channel = await guild.fetch_channel(int(panel["channel_id"]))
        message = await channel.fetch_message(int(panel["message_id"]))
        view = panel_view(int(panel_id))
        if isinstance(view, discord.ui.LayoutView):
            await message.edit(content=None, embed=None, attachments=[], view=view)
        else:
            await message.edit(embed=panel_embed(int(panel_id)), view=view)
        return True
    except (discord.NotFound, discord.Forbidden, discord.HTTPException, AttributeError):
        return False


def parse_panel_color(value: str) -> tuple[int, str]:
    """Converte nome/HEX para inteiro. -1 significa sem barra (transparente)."""
    raw = str(value or "").strip().lower()
    aliases = {
        "transparente": (-1, "transparente / sem barra"),
        "transparent": (-1, "transparente / sem barra"),
        "sem barra": (-1, "transparente / sem barra"),
        "sem-barra": (-1, "transparente / sem barra"),
        "none": (-1, "transparente / sem barra"),
        "preto": (0x000000, "preto"),
        "black": (0x000000, "preto"),
        "vermelho": (0xED4245, "vermelho"),
        "red": (0xED4245, "vermelho"),
        "azul": (0x5865F2, "azul"),
        "blue": (0x5865F2, "azul"),
        "roxo": (0x8B2CF5, "roxo"),
        "purple": (0x8B2CF5, "roxo"),
        "verde": (0x57F287, "verde"),
        "green": (0x57F287, "verde"),
        "amarelo": (0xFEE75C, "amarelo"),
        "yellow": (0xFEE75C, "amarelo"),
        "branco": (0xFFFFFF, "branco"),
        "white": (0xFFFFFF, "branco"),
    }
    if raw in aliases:
        return aliases[raw]

    clean = raw.replace("#", "").replace("0x", "")
    if re.fullmatch(r"[0-9a-f]{6}", clean):
        value_int = int(clean, 16)
        return value_int, f"#{clean.upper()}"

    raise ValueError(
        "Cor inválida. Use transparente, preto, vermelho, azul, roxo, verde ou HEX como #FF0000."
    )


class PanelSelect(discord.ui.Select):
    def __init__(self, panel_id: int):
        self.panel_id = panel_id
        options = []
        con = db()
        try:
            # Mostra no menu SOMENTE os produtos principais.
            # Produtos usados como variantes (ex.: Mensal/Permanente) continuam
            # vinculados ao painel e ao banco, mas ficam escondidos desta lista.
            try:
                rows = con.execute(
                    """
                    SELECT p.*
                    FROM products p
                    JOIN panel_products pp ON p.id=pp.product_id
                    WHERE pp.panel_id=?
                      AND p.active=1
                      AND NOT EXISTS (
                          SELECT 1
                          FROM checkout_variants cv
                          WHERE cv.variant_product_id=p.id
                            AND cv.guild_id=p.guild_id
                      )
                    ORDER BY p.price ASC
                    """,
                    (panel_id,),
                ).fetchall()
            except Exception as exc:
                # Compatibilidade com instalações antigas antes da tabela
                # checkout_variants existir.
                print(f"[MENU PRODUTOS] fallback sem variantes: {exc}")
                rows = con.execute(
                    """
                    SELECT p.*
                    FROM products p
                    JOIN panel_products pp ON p.id=pp.product_id
                    WHERE pp.panel_id=? AND p.active=1
                    ORDER BY p.price ASC
                    """,
                    (panel_id,),
                ).fetchall()
        finally:
            con.close()
        for p in rows[:25]:
            stock = "∞" if p["stock"] < 0 else str(p["stock"])
            options.append(
                discord.SelectOption(
                    label=p["name"][:100],
                    description=f"{money(p['price'])} | Estoque: {stock}",
                    emoji="🛒",
                    value=str(p["id"]),
                )
            )
        if not options:
            options = [
                discord.SelectOption(
                    label="Nenhum produto configurado", value="none", emoji="❌"
                )
            ]
        super().__init__(
            placeholder="Selecione um Produto",
            min_values=1,
            max_values=1,
            options=options,
            custom_id=f"panel_select_{panel_id}",
        )

    async def callback(self, interaction: discord.Interaction):
        if self.values[0] == "none":
            await interaction.response.send_message(
                "Nenhum produto neste painel ainda.", ephemeral=True
            )
            return
        # Responde rápido para o Discord não derrubar a interação em hospedagem lenta
        await interaction.response.defer(ephemeral=True, thinking=True)
        await start_order(interaction, int(self.values[0]))


async def smart_send(interaction: discord.Interaction, *args, **kwargs):
    # Se a interação já recebeu defer(), usa followup. Senão, responde normal.
    if interaction.response.is_done():
        return await interaction.followup.send(*args, **kwargs)
    return await interaction.response.send_message(*args, **kwargs)


async def start_order(interaction: discord.Interaction, product_id: int):
    if not interaction.response.is_done():
        await interaction.response.defer(ephemeral=True, thinking=True)
    await checkout_system.open_cart(interaction, product_id)


# ================= EMBEDS =================
def product_embed(p):
    stock = "∞" if p["stock"] < 0 else str(p["stock"])
    embed = discord.Embed(
        title=f"💎 {p['name']}",
        description=p["description"] or "Produto sem descrição.",
        color=0x5865F2,
    )
    embed.add_field(name="💰 Preço", value=money(p["price"]), inline=True)
    embed.add_field(name="📦 Estoque", value=stock, inline=True)
    embed.add_field(name="⚡ Entrega", value="Automática após aprovação", inline=False)
    if valid_url(p["image_url"]):
        embed.set_thumbnail(url=p["image_url"])
    if valid_url(p["banner_url"]):
        embed.set_image(url=p["banner_url"])

    try:
        cfg = get_customization(int(p["guild_id"]))
        loja = cfg["store_name"] or "Entregas automática"
    except Exception:
        loja = "Entregas automática"
    embed.set_footer(text=f"{loja} • Hoje")
    return embed


def panel_embed(panel_id: int):
    con = db()
    panel = con.execute("SELECT * FROM panels WHERE id=?", (panel_id,)).fetchone()
    con.close()
    if not panel:
        return discord.Embed(title="Painel não encontrado", color=0xFF0000)
    # CORREÇÃO PEDIDA: NÃO mostra valores/planos dentro da descrição.

    try:
        custom = get_customization(int(panel["guild_id"]))
        loja = custom["store_name"] or "Entregas automática"
        custom_color = custom["color"] if custom else None
    except Exception:
        loja = "Entregas automática"
        custom_color = None

    # Embeds clássicos não possuem barra "transparente"; quando o painel está
    # com -1 usamos a cor global apenas como fallback visual.
    panel_color = panel["color"]
    try:
        panel_color = int(panel_color) if panel_color is not None else None
    except Exception:
        panel_color = None
    cor = (
        panel_color
        if panel_color is not None and panel_color >= 0
        else int(custom_color or 0x5865F2)
    )
    embed = discord.Embed(
        title=panel["title"] or panel["name"],
        description=(panel["description"] or "")[:4096],
        color=cor,
    )
    if valid_url(panel["image_url"]):
        embed.set_thumbnail(url=panel["image_url"])
    if valid_url(panel["banner_url"]):
        embed.set_image(url=panel["banner_url"])
    embed.set_footer(text=f"{loja} • Painel de vendas")
    return embed


# ================= RANKING DE COMPRADORES =================
def get_ranking_rows(guild_id: int, limit: int = 10):
    con = db()
    try:
        return con.execute(
            """
            SELECT user_id,COUNT(*) AS purchases,COALESCE(SUM(amount),0) AS total
            FROM orders
            WHERE guild_id=? AND status='aprovado' AND COALESCE(is_test,0)=0
            GROUP BY user_id
            ORDER BY total DESC,purchases DESC,user_id ASC
            LIMIT ?
            """,
            (int(guild_id), max(1, min(int(limit), 25))),
        ).fetchall()
    finally:
        con.close()


def get_ranking_config(guild_id: int):
    con = db()
    try:
        return con.execute(
            "SELECT * FROM ranking_config WHERE guild_id=?", (int(guild_id),)
        ).fetchone()
    finally:
        con.close()


def ranking_embed(guild: discord.Guild, rows=None):
    rows = rows if rows is not None else get_ranking_rows(guild.id, 10)
    medals = ("🥇", "🥈", "🥉")
    lines = []
    for position, row in enumerate(rows, start=1):
        badge = medals[position - 1] if position <= 3 else f"`#{position:02d}`"
        lines.append(
            f"{badge} <@{int(row['user_id'])}>\n"
            f"      **{money(row['total'])}** • {int(row['purchases'])} compra(s)"
        )

    cfg = get_ranking_config(guild.id)
    role_text = "Não configurado"
    winners = 0
    if cfg and cfg["role_id"]:
        role_text = f"<@&{int(cfg['role_id'])}>"
        winners = max(1, min(int(cfg["top_count"] or 1), 10))

    embed = discord.Embed(
        title="🏆 RANKING DOS MAIORES COMPRADORES",
        description=(
            "Os clientes que mais investiram na loja aparecem aqui.\n\n"
            + ("\n\n".join(lines) if lines else "*Ainda não há compras aprovadas.*")
        ),
        color=0xD4AF37,
    )
    embed.add_field(
        name="💎 Recompensa",
        value=(
            f"{role_text} para o **Top {winners}**"
            if winners
            else role_text
        ),
        inline=False,
    )
    embed.set_footer(text="Ranking considera somente pagamentos reais aprovados")
    embed.timestamp = discord.utils.utcnow()
    return embed


async def sync_ranking_role(guild: discord.Guild, rows=None):
    cfg = get_ranking_config(guild.id)
    if not cfg or not cfg["role_id"]:
        return "Nenhum cargo de premiação configurado."

    role = guild.get_role(int(cfg["role_id"]))
    if not role:
        return "O cargo configurado não existe mais."
    if not guild.me or role >= guild.me.top_role:
        return "Coloque o cargo do bot acima do cargo de rico."

    rows = rows if rows is not None else get_ranking_rows(guild.id, 10)
    top_count = max(1, min(int(cfg["top_count"] or 1), 10))
    winners = {int(row["user_id"]) for row in rows[:top_count]}

    con = db()
    try:
        previous = {
            int(row["user_id"])
            for row in con.execute(
                "SELECT user_id FROM ranking_awards WHERE guild_id=? AND role_id=?",
                (guild.id, role.id),
            ).fetchall()
        }
    finally:
        con.close()

    added = removed = 0
    for user_id in previous - winners:
        member = guild.get_member(user_id)
        try:
            if member and role in member.roles:
                await member.remove_roles(role, reason="Saiu da faixa premiada do ranking")
            con = db()
            con.execute(
                "DELETE FROM ranking_awards WHERE guild_id=? AND user_id=? AND role_id=?",
                (guild.id, user_id, role.id),
            )
            con.commit()
            con.close()
            removed += 1
        except (discord.Forbidden, discord.HTTPException):
            continue

    for user_id in winners:
        member = guild.get_member(user_id)
        if not member:
            continue
        already_tracked = user_id in previous
        had_role = role in member.roles
        try:
            if not had_role:
                await member.add_roles(role, reason="Premiação do ranking de compradores")
                added += 1
            # Só rastreia para remoção futura quem o ranking realmente administra.
            if already_tracked or not had_role:
                con = db()
                con.execute(
                    """
                    INSERT INTO ranking_awards(guild_id,user_id,role_id,awarded_at)
                    VALUES(?,?,?,NOW())
                    ON CONFLICT(guild_id,user_id,role_id)
                    DO UPDATE SET awarded_at=excluded.awarded_at
                    """,
                    (guild.id, user_id, role.id),
                )
                con.commit()
                con.close()
        except (discord.Forbidden, discord.HTTPException):
            continue

    return f"Cargo sincronizado: {added} adicionado(s), {removed} removido(s)."


async def clear_tracked_ranking_role(guild: discord.Guild, role_id: int):
    """Remove somente o cargo antigo que foi registrado como entregue pelo ranking."""
    role = guild.get_role(int(role_id))
    con = db()
    try:
        tracked = con.execute(
            "SELECT user_id FROM ranking_awards WHERE guild_id=? AND role_id=?",
            (guild.id, int(role_id)),
        ).fetchall()
    finally:
        con.close()

    for row in tracked:
        user_id = int(row["user_id"])
        member = guild.get_member(user_id)
        try:
            if role and member and role in member.roles:
                await member.remove_roles(
                    role, reason="Cargo de premiação do ranking foi alterado"
                )
            con = db()
            con.execute(
                "DELETE FROM ranking_awards WHERE guild_id=? AND user_id=? AND role_id=?",
                (guild.id, user_id, int(role_id)),
            )
            con.commit()
            con.close()
        except (discord.Forbidden, discord.HTTPException):
            continue


class RankingView(discord.ui.View):
    def __init__(self, guild_id: int):
        super().__init__(timeout=None)
        self.guild_id = int(guild_id)
        self.add_item(RankingRefreshButton(self.guild_id))


class RankingRefreshButton(discord.ui.Button):
    def __init__(self, guild_id: int):
        super().__init__(
            label="Atualizar ranking",
            emoji="🔄",
            style=discord.ButtonStyle.secondary,
            custom_id=f"ranking_refresh_{int(guild_id)}",
        )
        self.guild_id = int(guild_id)

    async def callback(self, interaction: discord.Interaction):
        if not interaction.guild or interaction.guild.id != self.guild_id:
            await interaction.response.send_message(
                "Este ranking pertence a outro servidor.", ephemeral=True
            )
            return

        now = asyncio.get_running_loop().time()
        last = RANKING_REFRESH_TIMES.get(self.guild_id, 0)
        if now - last < 10:
            await interaction.response.send_message(
                "⏳ Aguarde alguns segundos antes de atualizar novamente.", ephemeral=True
            )
            return

        lock = RANKING_REFRESH_LOCKS.setdefault(self.guild_id, asyncio.Lock())
        await interaction.response.defer(ephemeral=True, thinking=True)
        async with lock:
            rows = get_ranking_rows(self.guild_id, 10)
            role_result = await sync_ranking_role(interaction.guild, rows)
            await interaction.message.edit(
                embed=ranking_embed(interaction.guild, rows),
                view=RankingView(self.guild_id),
            )
            RANKING_REFRESH_TIMES[self.guild_id] = now
        await interaction.followup.send(
            f"✅ Ranking atualizado. {role_result}", ephemeral=True
        )


# ================= MODALS PAINEL =================
class PanelTextModal(discord.ui.Modal, title="Configurar painel"):
    def __init__(self, panel_id: int):
        super().__init__()
        self.panel_id = panel_id
        con = db()
        p = con.execute("SELECT * FROM panels WHERE id=?", (panel_id,)).fetchone()
        con.close()
        self.titulo = discord.ui.TextInput(
            label="Título", default=p["title"] or p["name"], max_length=100
        )
        self.desc = discord.ui.TextInput(
            label="Descrição",
            default=p["description"] or "",
            style=discord.TextStyle.paragraph,
            required=False,
            max_length=3000,
        )
        self.img = discord.ui.TextInput(
            label="Thumbnail/Imagem pequena URL",
            default=p["image_url"] or "",
            required=False,
        )
        self.banner = discord.ui.TextInput(
            label="Banner/Imagem grande URL",
            default=p["banner_url"] or "",
            required=False,
        )
        self.add_item(self.titulo)
        self.add_item(self.desc)
        self.add_item(self.img)
        self.add_item(self.banner)

    async def on_submit(self, interaction):
        if not interaction.guild:
            await interaction.response.send_message(
                "❌ Use este painel dentro do servidor.", ephemeral=True
            )
            return

        con = db()
        try:
            con.execute(
                """
                UPDATE panels
                SET title=?,description=?,image_url=?,banner_url=?
                WHERE id=? AND guild_id=?
                """,
                (
                    str(self.titulo),
                    str(self.desc),
                    str(self.img),
                    str(self.banner),
                    self.panel_id,
                    interaction.guild.id,
                ),
            )
            con.commit()
        finally:
            con.close()

        atualizado = await refresh_published_panel(interaction.guild, self.panel_id)
        extra = " A mensagem publicada também foi atualizada." if atualizado else ""
        await interaction.response.send_message(
            "✅ Painel atualizado." + extra, ephemeral=True
        )


class PlanModal(discord.ui.Modal, title="Adicionar plano/produto"):
    def __init__(self, panel_id: int):
        super().__init__()
        self.panel_id = panel_id
        self.nome = discord.ui.TextInput(
            label="Nome do plano", placeholder="Bypass Mensal"
        )
        self.preco = discord.ui.TextInput(label="Preço", placeholder="45.00")
        self.estoque = discord.ui.TextInput(
            label="Estoque (-1 para infinito)", default="-1"
        )
        self.desc = discord.ui.TextInput(
            label="Descrição/entrega", required=False, style=discord.TextStyle.paragraph
        )
        self.add_item(self.nome)
        self.add_item(self.preco)
        self.add_item(self.estoque)
        self.add_item(self.desc)

    async def on_submit(self, interaction):
        try:
            price = parse_price_input(str(self.preco))
            stock = int(str(self.estoque))
        except Exception:
            await interaction.response.send_message(
                "❌ Preço ou estoque inválido.", ephemeral=True
            )
            return
        con = db()
        cur = con.cursor()
        cur.execute(
            "INSERT INTO products(guild_id,name,price,stock,description,category,delivery_text) VALUES(?,?,?,?,?,?,?)",
            (
                interaction.guild.id,
                str(self.nome),
                price,
                stock,
                str(self.desc),
                "Painel",
                str(self.desc),
            ),
        )
        pid = cur.lastrowid
        cur.execute(
            "INSERT OR IGNORE INTO panel_products(panel_id,product_id) VALUES(?,?)",
            (self.panel_id, pid),
        )
        con.commit()
        con.close()
        await interaction.response.send_message(
            f"✅ Plano **{self.nome}** adicionado ao painel.", ephemeral=True
        )


class ConfigPanelView(discord.ui.View):
    def __init__(self, panel_id: int):
        super().__init__(timeout=None)
        self.panel_id = panel_id

    @discord.ui.button(label="✏️ Texto/Imagem", style=discord.ButtonStyle.blurple)
    async def text_img(self, interaction, button):
        await interaction.response.send_modal(PanelTextModal(self.panel_id))

    @discord.ui.button(label="➕ Adicionar plano", style=discord.ButtonStyle.green)
    async def add_plan(self, interaction, button):
        await interaction.response.send_modal(PlanModal(self.panel_id))

    @discord.ui.button(label="👁️ Prévia", style=discord.ButtonStyle.gray)
    async def preview(self, interaction, button):
        await interaction.response.send_message(
            **panel_send_kwargs(self.panel_id), ephemeral=True
        )

    @discord.ui.button(label="🚀 Publicar aqui", style=discord.ButtonStyle.red)
    async def publish_here(self, interaction, button):
        await interaction.channel.send(**panel_send_kwargs(self.panel_id))
        await interaction.response.send_message(
            "✅ Painel publicado neste canal/tópico.", ephemeral=True
        )

    @discord.ui.button(
        label="🛒 Usar botão Opções", style=discord.ButtonStyle.secondary, row=1
    )
    async def options_mode(self, interaction, button):
        con = db()
        con.execute(
            "UPDATE panels SET display_mode='button' WHERE id=?", (self.panel_id,)
        )
        con.commit()
        con.close()
        await interaction.response.send_message(
            "✅ Este painel agora usa o botão **Opções**.", ephemeral=True
        )


# ================= TICKET =================
def ticket_panel_embed(titulo=None, descricao=None, imagem=None, thumb=None):
    embed = discord.Embed(
        title=titulo or TICKET_PANEL_TITLE,
        description=descricao or TICKET_PANEL_DESC,
        color=0x5865F2,
    )
    # CORREÇÃO: só aplica URL se for válida. Evita Invalid Form Body.
    img = imagem or TICKET_IMAGE_URL
    th = thumb or TICKET_THUMB_URL
    if valid_url(th):
        embed.set_thumbnail(url=th)
    if valid_url(img):
        embed.set_image(url=img)
    embed.set_footer(text="Entregas Automáticas • LinkRoubadão")
    return embed


class CloseTicketView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="🔒 Fechar Ticket",
        style=discord.ButtonStyle.danger,
        custom_id="close_ticket_v13",
    )
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            "🔒 Ticket será fechado em 5 segundos...", ephemeral=True
        )
        await asyncio.sleep(5)
        try:
            await interaction.channel.delete()
        except Exception:
            pass


class TicketPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="📩 Suporte",
        style=discord.ButtonStyle.primary,
        custom_id="ticket_suporte_v13",
    )
    async def suporte(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        await criar_ticket(interaction, "suporte")

    @discord.ui.button(
        label="❓ Dúvidas",
        style=discord.ButtonStyle.secondary,
        custom_id="ticket_duvidas_v13",
    )
    async def duvidas(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        await criar_ticket(interaction, "duvidas")

    @discord.ui.button(
        label="💰 Financeiro",
        style=discord.ButtonStyle.success,
        custom_id="ticket_financeiro_v13",
    )
    async def financeiro(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        await criar_ticket(interaction, "financeiro")


async def criar_ticket(interaction: discord.Interaction, tipo: str):
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild
    user = interaction.user
    category = discord.utils.get(guild.categories, name=TICKET_CATEGORY_NAME)
    if not category:
        category = await guild.create_category(TICKET_CATEGORY_NAME)
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        user: discord.PermissionOverwrite(
            view_channel=True, send_messages=True, read_message_history=True
        ),
        guild.me: discord.PermissionOverwrite(
            view_channel=True, send_messages=True, manage_channels=True
        ),
    }
    ch = await guild.create_text_channel(
        f"{tipo}-{user.name}"[:90], category=category, overwrites=overwrites
    )
    embed = discord.Embed(
        title="📁 Ticket aberto",
        description=f"👋 {user.mention}, bem-vindo ao **Entregas Automáticas**\n\n📌 **Tipo:** {tipo.upper()}\n\nExplique seu problema abaixo.",
        color=0x2B2D31,
    )
    await ch.send(embed=embed, view=CloseTicketView())
    await interaction.followup.send(f"✅ Ticket criado: {ch.mention}", ephemeral=True)


@bot.event
async def on_guild_join(guild):
    ensure_config(guild.id)
    if DEFAULT_TRIAL_DAYS > 0:
        expires = datetime.utcnow() + timedelta(days=DEFAULT_TRIAL_DAYS)
        con = db()
        con.execute(
            """INSERT INTO guild_subscriptions(guild_id,active,plan_name,expires_at,activated_by,updated_at) VALUES(?,?,?,?,?,?) ON CONFLICT(guild_id) DO UPDATE SET active=1, plan_name=excluded.plan_name, expires_at=excluded.expires_at, updated_at=excluded.updated_at""",
            (guild.id, 1, "trial", expires.strftime("%Y-%m-%d %H:%M:%S"), 0, now_iso()),
        )
        con.commit()
        con.close()


# ================= COMMANDS =================
@bot.event
async def on_ready():
    init_db()

    # Persistent views: mantém botões/dropdowns funcionando após reiniciar o bot.
    # Isso restaura todos os painéis salvos no vendas.db.
    bot.add_view(TicketPanelView())
    bot.add_view(CloseTicketView())
    try:
        con = db()
        paineis = con.execute("SELECT * FROM panels").fetchall()
        produtos = con.execute("SELECT * FROM products WHERE active=1").fetchall()
        rankings = con.execute("SELECT guild_id FROM ranking_config").fetchall()
        con.close()
        for painel in paineis:
            try:
                bot.add_view(panel_view(int(painel["id"])))
            except Exception as e:
                print("erro restaurando painel", painel["id"], e)
        for ranking in rankings:
            try:
                bot.add_view(RankingView(int(ranking["guild_id"])))
            except Exception as e:
                print("erro restaurando ranking", ranking["guild_id"], e)
        for produto in produtos:
            try:
                bot.add_view(BuyView(product_id=int(produto["id"])))
            except Exception as e:
                print("erro restaurando produto", produto["id"], e)
        # Os painéis antigos e produtos continuam persistentes pelas Views acima.
        # As classes PremiumPanelView/PurchaseReceiptView pertenciam a uma versão
        # anterior e foram removidas para evitar NameError na inicialização.
        print(
            f"Views persistentes restauradas: {len(paineis)} painel(is), "
            f"{len(produtos)} produto(s) e {len(rankings)} ranking(s)"
        )
    except Exception as e:
        print("Erro restaurando views persistentes:", e)

    for g in bot.guilds:
        ensure_config(g.id)
    try:
        if not getattr(bot, "_checkout_setup_done", False):
            await checkout_system.setup(bot, protected_admin_only)
            bot._checkout_setup_done = True
        if not getattr(bot, "_verification_setup_done", False):
            await verification_system.setup(bot, app, protected_admin_only)
            bot._verification_setup_done = True
        if not getattr(bot, "_utility_setup_done", False):
            await utility_system.setup(bot, protected_admin_only)
            bot._utility_setup_done = True
        if not getattr(bot, "_locksensi_integrated_setup_done", False):
            await setup_locksensi_integrated(bot, protected_admin_only)
            bot._locksensi_integrated_setup_done = True
        # Faz a limpeza dos comandos locais antigos apenas uma vez por inicialização.
        # Isso evita que versões antigas dos slash commands continuem salvas
        # em servidores específicos e causem TransformerError.
        if not getattr(bot, "_initial_sync_done", False):
            for guild in bot.guilds:
                try:
                    guild_obj = discord.Object(id=guild.id)
                    bot.tree.clear_commands(guild=guild_obj)
                    await bot.tree.sync(guild=guild_obj)
                    print(
                        f"[SYNC] Comandos locais antigos removidos de "
                        f"{guild.name} ({guild.id})"
                    )
                except Exception as sync_guild_error:
                    print(
                        f"[SYNC] Erro limpando comandos locais de "
                        f"{guild.name} ({guild.id}): {sync_guild_error}"
                    )

            global_synced = await bot.tree.sync()
            bot._initial_sync_done = True
        else:
            global_synced = bot.tree.get_commands()

        print(f"Online como {bot.user}")
        print(f"Comandos globais sincronizados: {len(global_synced)}")
    except Exception as e:
        print("Erro sync:", e)


@bot.tree.command(name="meu-plano", description="Mostra o plano deste servidor")
async def meu_plano(interaction: discord.Interaction):
    if not interaction.guild:
        await interaction.response.send_message(
            "Use este comando dentro de um servidor.", ephemeral=True
        )
        return
    await interaction.response.send_message(
        f"📌 Servidor: **{interaction.guild.name}**\n🆔 ID: `{interaction.guild.id}`\n{plan_text(interaction.guild.id)}",
        ephemeral=True,
    )


@bot.tree.command(
    name="ativar-servidor", description="Ativa plano para um servidor pelo ID"
)
@app_commands.describe(
    servidor_id="ID do servidor", dias="Dias de acesso", plano="Nome do plano"
)
async def ativar_servidor(
    interaction: discord.Interaction,
    servidor_id: str,
    dias: int = 30,
    plano: str = "mensal",
):
    if not await owner_only(interaction):
        return
    try:
        gid = int(servidor_id)
    except Exception:
        await interaction.response.send_message("❌ ID inválido.", ephemeral=True)
        return
    expires = datetime.utcnow() + timedelta(days=max(1, dias))
    con = db()
    con.execute(
        """INSERT INTO guild_subscriptions(guild_id,active,plan_name,expires_at,activated_by,updated_at) VALUES(?,?,?,?,?,?) ON CONFLICT(guild_id) DO UPDATE SET active=1, plan_name=excluded.plan_name, expires_at=excluded.expires_at, activated_by=excluded.activated_by, updated_at=excluded.updated_at""",
        (
            gid,
            1,
            plano,
            expires.strftime("%Y-%m-%d %H:%M:%S"),
            interaction.user.id,
            now_iso(),
        ),
    )
    con.commit()
    con.close()
    await interaction.response.send_message(
        f"✅ Servidor `{gid}` ativado por **{dias} dias** no plano **{plano}**.\nVence em `{expires.strftime('%Y-%m-%d %H:%M:%S')}`.",
        ephemeral=True,
    )


@bot.tree.command(
    name="desativar-servidor", description="Desativa plano de um servidor pelo ID"
)
async def desativar_servidor(interaction: discord.Interaction, servidor_id: str):
    if not await owner_only(interaction):
        return
    try:
        gid = int(servidor_id)
    except Exception:
        await interaction.response.send_message("❌ ID inválido.", ephemeral=True)
        return
    con = db()
    con.execute(
        "UPDATE guild_subscriptions SET active=0, updated_at=? WHERE guild_id=?",
        (now_iso(), gid),
    )
    con.commit()
    con.close()
    await interaction.response.send_message(
        f"✅ Servidor `{gid}` desativado.", ephemeral=True
    )


@bot.tree.command(
    name="personalizar-loja",
    description="Personaliza loja, cor e nickname do bot neste servidor",
)
@app_commands.describe(
    nome="Nome da loja",
    cor_hex="Cor HEX. Ex: ff00aa",
    nickname_bot="Nome do bot neste servidor",
)
async def personalizar_loja(
    interaction: discord.Interaction,
    nome: Optional[str] = None,
    cor_hex: Optional[str] = None,
    nickname_bot: Optional[str] = None,
):
    if not await protected_admin_only(interaction):
        return
    cfg = get_customization(interaction.guild.id)
    nome_final = nome or cfg["store_name"] or "Entregas automática"
    cor_final = parse_color(cor_hex, cfg["color"] or 0x5865F2)
    nick_final = (
        nickname_bot if nickname_bot is not None else (cfg["bot_nickname"] or "")
    )
    con = db()
    con.execute(
        """INSERT INTO guild_customization(guild_id,store_name,color,bot_nickname,updated_at) VALUES(?,?,?,?,?) ON CONFLICT(guild_id) DO UPDATE SET store_name=excluded.store_name, color=excluded.color, bot_nickname=excluded.bot_nickname, updated_at=excluded.updated_at""",
        (interaction.guild.id, nome_final, cor_final, nick_final, now_iso()),
    )
    con.execute(
        "UPDATE guild_config SET store_name=?, color=? WHERE guild_id=?",
        (nome_final, cor_final, interaction.guild.id),
    )
    con.commit()
    con.close()
    msg = f"✅ Loja personalizada.\nNome: **{nome_final}**\nCor: `#{cor_final:06x}`"
    if nickname_bot:
        try:
            await interaction.guild.me.edit(nick=nickname_bot[:32])
            msg += f"\nNickname do bot alterado para **{nickname_bot[:32]}**."
        except Exception as e:
            msg += f"\n⚠️ Não consegui alterar o nickname: `{e}`"
    await interaction.response.send_message(msg, ephemeral=True)


@bot.tree.command(
    name="listar-servidores",
    description="Lista servidores onde o bot está e status do plano",
)
async def listar_servidores(interaction: discord.Interaction):
    if not await owner_only(interaction):
        return
    linhas = []
    for g in bot.guilds[:25]:
        status = "ativo" if guild_has_plan(g.id) else "bloqueado/expirado"
        linhas.append(f"`{g.id}` • **{g.name}** • {status}")
    await interaction.response.send_message(
        "\n".join(linhas) if linhas else "Nenhum servidor encontrado.", ephemeral=True
    )


@bot.tree.command(
    name="sincronizar",
    description="Remove comandos antigos e sincroniza os comandos atuais",
)
async def sincronizar(interaction: discord.Interaction):
    if not await protected_admin_only(interaction):
        return

    await interaction.response.defer(ephemeral=True, thinking=True)

    try:
        guild_obj = discord.Object(id=interaction.guild.id)

        # Remove comandos locais antigos deste servidor.
        bot.tree.clear_commands(guild=guild_obj)
        comandos_locais = await bot.tree.sync(guild=guild_obj)

        # Atualiza os comandos globais atuais.
        comandos_globais = await bot.tree.sync()

        mensagem_sucesso = (
            "✅ Sincronização concluída.\n\n"
            f"🧹 Comandos locais restantes: `{len(comandos_locais)}`\n"
            f"🌐 Comandos globais atuais: `{len(comandos_globais)}`\n\n"
            "Feche e abra o Discord novamente antes de testar."
        )

        await interaction.followup.send(
            mensagem_sucesso,
            ephemeral=True,
        )

    except Exception as erro:
        print(f"[ERRO SINCRONIZAR] {type(erro).__name__}: {erro}")
        traceback.print_exc()

        mensagem_erro = (
            "❌ Erro durante a sincronização:\n"
            f"```{str(erro)[:1000]}```"
        )

        await interaction.followup.send(
            mensagem_erro,
            ephemeral=True,
        )


@bot.tree.command(name="ping", description="Mostra o ping do bot")
async def ping(interaction):
    await interaction.response.send_message(
        f"🏓 Pong! `{round(bot.latency * 1000)}ms`", ephemeral=True
    )


@bot.tree.command(name="configurar", description="Cria estrutura inicial do bot")
async def configurar(interaction):
    if not await protected_admin_only(interaction):
        return
    guild = interaction.guild
    ensure_config(guild.id)
    cat = await guild.create_category("🛒 Loja / Vendas")
    painel = await guild.create_text_channel("🛒・seu-painel", category=cat)
    logs = await guild.create_text_channel("📋・logs-vendas", category=cat)
    aval = await guild.create_text_channel("⭐・avaliações", category=cat)
    role = discord.utils.get(guild.roles, name="Cliente") or await guild.create_role(
        name="Cliente"
    )
    con = db()
    con.execute(
        "UPDATE guild_config SET sales_channel_id=?,log_channel_id=?,review_channel_id=?,support_category_id=?,customer_role_id=? WHERE guild_id=?",
        (painel.id, logs.id, aval.id, cat.id, role.id, guild.id),
    )
    con.commit()
    con.close()
    await interaction.response.send_message(
        f"✅ Configurado: {painel.mention}, {logs.mention}, {aval.mention}",
        ephemeral=True,
    )


@bot.tree.command(
    name="autenticacao", description="Configura Pix, Mercado Pago, EFI e webhook"
)
@app_commands.describe(
    pix_key="Chave PIX",
    pix_nome="Nome recebedor",
    pix_cidade="Cidade",
    mercado_pago_token="Access token MP",
    webhook="Webhook de logs",
)
async def autenticacao(
    interaction,
    pix_key: Optional[str] = None,
    pix_nome: Optional[str] = None,
    pix_cidade: Optional[str] = None,
    mercado_pago_token: Optional[str] = None,
    webhook: Optional[str] = None,
):
    if not await protected_admin_only(interaction):
        return
    ensure_config(interaction.guild.id)
    con = db()
    cfg = get_config(interaction.guild.id)
    con.execute(
        "UPDATE guild_config SET pix_key=?, pix_name=?, pix_city=?, mp_token=?, webhook_url=? WHERE guild_id=?",
        (
            pix_key or cfg["pix_key"],
            pix_nome or cfg["pix_name"],
            pix_cidade or cfg["pix_city"],
            mercado_pago_token or cfg["mp_token"],
            webhook or cfg["webhook_url"],
            interaction.guild.id,
        ),
    )
    con.commit()
    con.close()
    await interaction.response.send_message(
        "✅ Autenticação/PIX atualizados.", ephemeral=True
    )


@bot.tree.command(name="webhook", description="Cadastra webhook para logs")
async def webhook(interaction, url: str):
    if not await protected_admin_only(interaction):
        return
    con = db()
    ensure_config(interaction.guild.id)
    con.execute(
        "UPDATE guild_config SET webhook_url=? WHERE guild_id=?",
        (url, interaction.guild.id),
    )
    con.commit()
    con.close()
    await interaction.response.send_message("✅ Webhook salvo.", ephemeral=True)


@bot.tree.command(name="saldo", description="Exibe saldo/configuração de pagamento")
async def saldo(interaction):
    cfg = get_config(interaction.guild.id)
    await interaction.response.send_message(
        f"💰 PIX configurado: `{bool(cfg['pix_key'])}`\nMercado Pago: `{bool(cfg['mp_token'])}`",
        ephemeral=True,
    )


@bot.tree.command(name="cargo_id", description="Mostra o ID de um cargo")
async def cargo_id(interaction, cargo: discord.Role):
    await interaction.response.send_message(
        f"Cargo {cargo.mention} ID: `{cargo.id}`", ephemeral=True
    )


@bot.tree.command(
    name="canal-de-avaliacoes", description="Seleciona canal para avaliações"
)
async def canal_de_avaliacoes(interaction, canal: discord.TextChannel):
    if not await protected_admin_only(interaction):
        return
    ensure_config(interaction.guild.id)
    con = db()
    con.execute(
        "UPDATE guild_config SET review_channel_id=? WHERE guild_id=?",
        (canal.id, interaction.guild.id),
    )
    con.commit()
    con.close()
    await interaction.response.send_message(
        f"✅ Canal de avaliações: {canal.mention}", ephemeral=True
    )


@bot.tree.command(name="avaliacoes-servidor", description="Mostra média de avaliações")
async def avaliacoes_servidor(interaction):
    con = db()
    rows = con.execute(
        "SELECT stars FROM reviews WHERE guild_id=?", (interaction.guild.id,)
    ).fetchall()
    con.close()
    if not rows:
        await interaction.response.send_message(
            "⭐ Ainda não há avaliações.", ephemeral=True
        )
        return
    avg = sum(r["stars"] for r in rows) / len(rows)
    await interaction.response.send_message(
        f"⭐ Média: **{avg:.1f}/5** em **{len(rows)}** avaliações.", ephemeral=True
    )


@bot.tree.command(
    name="criar-painel-config",
    description="Abre tópico/painel para configurar produto por botões",
)
@app_commands.describe(nome="Nome interno do painel")
async def criar_painel_config(interaction, nome: str):
    if not await protected_admin_only(interaction):
        return
    con = db()
    cur = con.cursor()
    cur.execute(
        "INSERT INTO panels(guild_id,name,title,description,channel_id,display_mode) VALUES(?,?,?,?,?,?)",
        (
            interaction.guild.id,
            nome,
            nome,
            "Configure a descrição deste painel clicando nos botões abaixo.",
            interaction.channel.id,
            "button",
        ),
    )
    panel_id = cur.lastrowid
    con.commit()
    con.close()
    await interaction.response.send_message(
        f"✅ Painel criado. ID `{panel_id}`", ephemeral=True
    )
    msg = await interaction.channel.send(
        f"⚙️ Configuração do painel **{nome}**\nID: `{panel_id}`",
        view=ConfigPanelView(panel_id),
    )
    try:
        thread = await msg.create_thread(name=f"config-{nome}"[:90])
        await thread.send(
            "Use os botões acima para configurar texto, imagem, banner e planos. Depois clique em publicar aqui."
        )
        con = db()
        con.execute("UPDATE panels SET topic_id=? WHERE id=?", (thread.id, panel_id))
        con.commit()
        con.close()
    except Exception:
        pass


@bot.tree.command(
    name="editar-painel",
    description="Edita título, descrição e imagens de um painel existente",
)
@app_commands.describe(painel_id="ID do painel, ex.: 137")
async def editar_painel(interaction: discord.Interaction, painel_id: int):
    if not await protected_admin_only(interaction):
        return
    if not interaction.guild:
        await interaction.response.send_message(
            "❌ Use este comando dentro do servidor.", ephemeral=True
        )
        return

    con = db()
    try:
        panel = con.execute(
            "SELECT id FROM panels WHERE id=? AND guild_id=?",
            (int(painel_id), int(interaction.guild.id)),
        ).fetchone()
    finally:
        con.close()

    if not panel:
        await interaction.response.send_message(
            f"❌ Painel `{painel_id}` não encontrado neste servidor.", ephemeral=True
        )
        return

    await interaction.response.send_modal(PanelTextModal(int(painel_id)))


@bot.tree.command(
    name="reabrir-config",
    description="Recria a mensagem com os botões de configuração de um painel",
)
@app_commands.describe(painel_id="ID do painel, ex.: 137")
async def reabrir_config(interaction: discord.Interaction, painel_id: int):
    if not await protected_admin_only(interaction):
        return
    if not interaction.guild:
        await interaction.response.send_message(
            "❌ Use este comando dentro do servidor.", ephemeral=True
        )
        return

    con = db()
    try:
        panel = con.execute(
            "SELECT * FROM panels WHERE id=? AND guild_id=?",
            (int(painel_id), int(interaction.guild.id)),
        ).fetchone()
    finally:
        con.close()

    if not panel:
        await interaction.response.send_message(
            f"❌ Painel `{painel_id}` não encontrado neste servidor.", ephemeral=True
        )
        return

    nome = str(panel["title"] or panel["name"] or f"Painel {painel_id}")
    await interaction.response.send_message(
        "✅ Configuração reaberta abaixo.", ephemeral=True
    )
    await interaction.channel.send(
        f"⚙️ Configuração do painel **{nome}**\nID: `{painel_id}`",
        view=ConfigPanelView(int(painel_id)),
    )


@bot.tree.command(
    name="cor-painel",
    description="Muda a barra lateral do painel (ou remove a barra)",
)
@app_commands.describe(
    painel_id="ID do painel, ex.: 137",
    cor="transparente, preto, vermelho, azul, roxo, verde ou HEX (#FF0000)",
)
async def cor_painel(
    interaction: discord.Interaction, painel_id: int, cor: str
):
    if not await protected_admin_only(interaction):
        return
    if not interaction.guild:
        await interaction.response.send_message(
            "❌ Use este comando dentro do servidor.", ephemeral=True
        )
        return

    try:
        cor_valor, cor_nome = parse_panel_color(cor)
    except ValueError as exc:
        await interaction.response.send_message(f"❌ {exc}", ephemeral=True)
        return

    con = db()
    try:
        panel = con.execute(
            "SELECT * FROM panels WHERE id=? AND guild_id=?",
            (int(painel_id), int(interaction.guild.id)),
        ).fetchone()
        if not panel:
            await interaction.response.send_message(
                f"❌ Painel `{painel_id}` não encontrado neste servidor.", ephemeral=True
            )
            return
        con.execute(
            "UPDATE panels SET color=? WHERE id=? AND guild_id=?",
            (int(cor_valor), int(painel_id), int(interaction.guild.id)),
        )
        con.commit()
    finally:
        con.close()

    atualizado = await refresh_published_panel(interaction.guild, int(painel_id))
    extra = " O painel publicado também foi atualizado." if atualizado else ""
    await interaction.response.send_message(
        f"✅ Barra do painel `{painel_id}` alterada para **{cor_nome}**.{extra}",
        ephemeral=True,
    )


@bot.tree.command(
    name="adicionar-plano", description="Adiciona plano/produto a um painel existente"
)
async def adicionar_plano(
    interaction,
    painel_id: int,
    nome: str,
    preco: str,
    estoque: int = -1,
    descricao: str = "",
):
    if not await protected_admin_only(interaction):
        return
    try:
        preco_final = parse_price_input(preco)
    except Exception:
        await interaction.response.send_message(
            "❌ Preço inválido. Use `19,99` ou `19.99`.", ephemeral=True
        )
        return
    con = db()
    cur = con.cursor()
    cur.execute(
        "INSERT INTO products(guild_id,name,price,stock,description,category,delivery_text) VALUES(?,?,?,?,?,?,?)",
        (interaction.guild.id, nome, preco_final, estoque, descricao, "Painel", descricao),
    )
    pid = cur.lastrowid
    cur.execute(
        "INSERT OR IGNORE INTO panel_products(panel_id,product_id) VALUES(?,?)",
        (painel_id, pid),
    )
    con.commit()
    con.close()
    await interaction.response.send_message(
        f"✅ Plano `{nome}` adicionado ao painel `{painel_id}`.", ephemeral=True
    )


@bot.tree.command(
    name="publicar-painel", description="Publica painel de produtos no canal"
)
async def publicar_painel(
    interaction, painel_id: int, canal: Optional[discord.TextChannel] = None
):
    if not await protected_admin_only(interaction):
        return
    canal = canal or interaction.channel
    msg = await canal.send(**panel_send_kwargs(painel_id))
    con = db()
    con.execute(
        "UPDATE panels SET channel_id=?,message_id=? WHERE id=?",
        (canal.id, msg.id, painel_id),
    )
    con.commit()
    con.close()
    await interaction.response.send_message(
        f"✅ Painel publicado em {canal.mention}", ephemeral=True
    )


_REPUBLICAR_PAINEIS_GUILDS: set[int] = set()


@bot.tree.command(
    name="republicar-paineis",
    description="Reenvia todos os painéis salvos nos canais originais",
)
@app_commands.describe(
    confirmar="Use verdadeiro somente depois de conferir a quantidade mostrada"
)
async def republicar_paineis(interaction: discord.Interaction, confirmar: bool = False):
    """Migra as mensagens dos painéis para uma nova aplicação do bot sem recriar produtos."""
    if not await protected_admin_only(interaction):
        return
    if not interaction.guild:
        await interaction.response.send_message(
            "Use este comando dentro do servidor.", ephemeral=True
        )
        return

    guild_id = interaction.guild.id
    con = db()
    try:
        paineis = con.execute(
            "SELECT id, name, channel_id FROM panels WHERE guild_id=? ORDER BY id ASC",
            (guild_id,),
        ).fetchall()
    finally:
        con.close()

    if not paineis:
        await interaction.response.send_message(
            "Não encontrei painéis salvos neste servidor.", ephemeral=True
        )
        return

    if not confirmar:
        await interaction.response.send_message(
            "🔎 Encontrei **{} painel(is)** salvo(s). Nada foi enviado ainda. "
            "Execute `/republicar-paineis confirmar:True` para reenviar todos "
            "nos canais originais, sem recriar produtos.".format(len(paineis)),
            ephemeral=True,
        )
        return

    if guild_id in _REPUBLICAR_PAINEIS_GUILDS:
        await interaction.response.send_message(
            "Já existe uma republicação em andamento neste servidor.", ephemeral=True
        )
        return

    _REPUBLICAR_PAINEIS_GUILDS.add(guild_id)
    await interaction.response.defer(ephemeral=True, thinking=True)
    enviados: list[str] = []
    falhas: list[str] = []
    try:
        for painel in paineis:
            painel_id = int(painel["id"])
            canal_id = painel["channel_id"]
            nome = str(painel["name"] or f"Painel {painel_id}")[:80]

            if not canal_id:
                falhas.append(f"`{painel_id}` {nome} — sem canal salvo")
                continue

            canal = interaction.guild.get_channel(int(canal_id))
            if canal is None:
                try:
                    canal = await bot.fetch_channel(int(canal_id))
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    canal = None

            if not canal or not hasattr(canal, "send"):
                falhas.append(f"`{painel_id}` {nome} — canal não encontrado/sem acesso")
                continue

            try:
                mensagem = await canal.send(**panel_send_kwargs(painel_id))
                con = db()
                try:
                    con.execute(
                        "UPDATE panels SET channel_id=?, message_id=? WHERE id=?",
                        (int(canal.id), int(mensagem.id), painel_id),
                    )
                    con.commit()
                finally:
                    con.close()
                enviados.append(f"`{painel_id}` {nome}")
                # Evita disparar o limite de mensagens ao migrar muitos painéis.
                await asyncio.sleep(1.2)
            except Exception as exc:
                print(f"Erro ao republicar painel {painel_id}: {exc}")
                falhas.append(f"`{painel_id}` {nome} — falha ao enviar")
    finally:
        _REPUBLICAR_PAINEIS_GUILDS.discard(guild_id)

    linhas = [f"✅ **Republicação concluída:** {len(enviados)} enviado(s)."]
    if falhas:
        linhas.append(f"⚠️ **{len(falhas)} pendente(s):**")
        linhas.extend(falhas[:20])
        if len(falhas) > 20:
            linhas.append(f"… e mais {len(falhas) - 20}.")
    else:
        linhas.append("Todos foram reenviados nos canais salvos.")
    await interaction.followup.send("\n".join(linhas)[:1900], ephemeral=True)


@bot.tree.command(
    name="painel-modo",
    description="Escolhe se o painel mostra botão Opções ou a lista direta",
)
@app_commands.describe(
    painel_id="ID do painel",
    modo="Visual do painel de vendas",
)
@app_commands.choices(
    modo=[
        app_commands.Choice(name="Botão Opções (recomendado)", value="button"),
        app_commands.Choice(name="Lista direta", value="select"),
    ]
)
async def painel_modo(
    interaction: discord.Interaction,
    painel_id: int,
    modo: app_commands.Choice[str],
):
    if not await protected_admin_only(interaction):
        return

    con = db()
    panel = con.execute(
        "SELECT * FROM panels WHERE id=? AND guild_id=?",
        (painel_id, interaction.guild.id),
    ).fetchone()
    if not panel:
        con.close()
        await interaction.response.send_message(
            "❌ Painel não encontrado neste servidor.", ephemeral=True
        )
        return
    con.execute(
        "UPDATE panels SET display_mode=? WHERE id=?",
        (modo.value, painel_id),
    )
    con.commit()
    con.close()

    await interaction.response.defer(ephemeral=True, thinking=True)
    updated = False
    if panel["channel_id"] and panel["message_id"]:
        try:
            channel = interaction.guild.get_channel(int(panel["channel_id"]))
            if channel:
                message = await channel.fetch_message(int(panel["message_id"]))
                if modo.value == "button":
                    await message.edit(
                        content=None,
                        embed=None,
                        attachments=[],
                        view=PanelOptionsView(painel_id),
                    )
                else:
                    await message.edit(
                        embed=panel_embed(painel_id), view=PanelOnlyView(painel_id)
                    )
                updated = True
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            pass

    label = "botão **🛒 Opções**" if modo.value == "button" else "lista direta"
    suffix = " O painel publicado também foi atualizado." if updated else ""
    await interaction.followup.send(
        f"✅ Painel configurado com {label}.{suffix}", ephemeral=True
    )


@bot.tree.command(
    name="ranking",
    description="Publica o ranking dos maiores compradores com cargo de premiação",
)
@app_commands.describe(
    cargo="Cargo de rico entregue aos vencedores",
    premiados="Quantidade de compradores que receberão o cargo (1 a 10)",
    canal="Canal onde o ranking será publicado",
)
async def ranking(
    interaction: discord.Interaction,
    cargo: discord.Role,
    premiados: int = 1,
    canal: Optional[discord.TextChannel] = None,
):
    if not await protected_admin_only(interaction):
        return
    if cargo.is_default():
        await interaction.response.send_message(
            "❌ Escolha um cargo específico, não o @everyone.", ephemeral=True
        )
        return
    if not interaction.guild.me or cargo >= interaction.guild.me.top_role:
        await interaction.response.send_message(
            "❌ O cargo do bot precisa ficar acima do cargo escolhido.", ephemeral=True
        )
        return

    premiados = max(1, min(int(premiados or 1), 10))
    canal = canal or interaction.channel
    await interaction.response.defer(ephemeral=True, thinking=True)

    previous_cfg = get_ranking_config(interaction.guild.id)
    if (
        previous_cfg
        and previous_cfg["role_id"]
        and int(previous_cfg["role_id"]) != cargo.id
    ):
        await clear_tracked_ranking_role(
            interaction.guild, int(previous_cfg["role_id"])
        )

    con = db()
    con.execute(
        """
        INSERT INTO ranking_config(guild_id,role_id,top_count,channel_id,updated_at)
        VALUES(?,?,?,?,NOW())
        ON CONFLICT(guild_id) DO UPDATE SET
            role_id=excluded.role_id,
            top_count=excluded.top_count,
            channel_id=excluded.channel_id,
            updated_at=excluded.updated_at
        """,
        (interaction.guild.id, cargo.id, premiados, canal.id),
    )
    con.commit()
    con.close()

    rows = get_ranking_rows(interaction.guild.id, 10)
    role_result = await sync_ranking_role(interaction.guild, rows)
    message = await canal.send(
        embed=ranking_embed(interaction.guild, rows),
        view=RankingView(interaction.guild.id),
    )
    con = db()
    con.execute(
        "UPDATE ranking_config SET channel_id=?,message_id=?,updated_at=NOW() WHERE guild_id=?",
        (canal.id, message.id, interaction.guild.id),
    )
    con.commit()
    con.close()

    await interaction.followup.send(
        f"✅ Ranking publicado em {canal.mention}. {role_result}", ephemeral=True
    )


@bot.tree.command(
    name="criar-produto-canal-atual", description="Cria produto único no canal atual"
)
async def criar_produto_canal_atual(
    interaction,
    nome: str,
    preco: str,
    estoque: int,
    descricao: str,
    imagem: Optional[str] = None,
    banner: Optional[str] = None,
):
    if not await protected_admin_only(interaction):
        return
    try:
        preco_final = parse_price_input(preco)
    except Exception:
        await interaction.response.send_message(
            "❌ Preço inválido. Use `19,99` ou `19.99`.", ephemeral=True
        )
        return
    con = db()
    cur = con.cursor()
    cur.execute(
        "INSERT INTO products(guild_id,name,price,stock,description,image_url,banner_url,channel_id) VALUES(?,?,?,?,?,?,?,?)",
        (
            interaction.guild.id,
            nome,
            preco_final,
            estoque,
            descricao,
            imagem or "",
            banner or "",
            interaction.channel.id,
        ),
    )
    pid = cur.lastrowid
    p = con.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
    con.commit()
    con.close()
    msg = await interaction.channel.send(
        embed=product_embed(p), view=BuyView(product_id=pid)
    )
    con = db()
    con.execute("UPDATE products SET message_id=? WHERE id=?", (msg.id, pid))
    con.commit()
    con.close()
    await interaction.response.send_message(
        "✅ Produto criado no canal atual.", ephemeral=True
    )


@bot.tree.command(
    name="criar-produto-lista", description="Cria produto e adiciona em painel/lista"
)
async def criar_produto_lista(
    interaction,
    painel_id: int,
    nome: str,
    preco: str,
    estoque: int = -1,
    descricao: str = "",
    imagem: Optional[str] = None,
    banner: Optional[str] = None,
):
    if not await protected_admin_only(interaction):
        return
    try:
        preco_final = parse_price_input(preco)
    except Exception:
        await interaction.response.send_message(
            "❌ Preço inválido. Use `19,99` ou `19.99`.", ephemeral=True
        )
        return
    con = db()
    cur = con.cursor()
    cur.execute(
        "INSERT INTO products(guild_id,name,price,stock,description,image_url,banner_url) VALUES(?,?,?,?,?,?,?)",
        (
            interaction.guild.id,
            nome,
            preco_final,
            estoque,
            descricao,
            imagem or "",
            banner or "",
        ),
    )
    pid = cur.lastrowid
    cur.execute(
        "INSERT OR IGNORE INTO panel_products(panel_id,product_id) VALUES(?,?)",
        (painel_id, pid),
    )
    con.commit()
    con.close()
    await interaction.response.send_message(
        f"✅ Produto `{nome}` adicionado ao painel `{painel_id}`.", ephemeral=True
    )



class AjustarPrecoModal(discord.ui.Modal, title="Ajustar preço do produto"):
    def __init__(self, produto_local_id: int, guild_id: int):
        super().__init__()
        self.produto_local_id = int(produto_local_id)
        self.guild_id = int(guild_id)
        self.preco = discord.ui.TextInput(
            label="Novo preço",
            placeholder="Ex.: 19,99",
            required=True,
            max_length=20,
        )
        self.add_item(self.preco)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            novo_preco = parse_price_input(str(self.preco))
        except Exception:
            await interaction.response.send_message(
                "❌ Preço inválido. Digite como `19,99` ou `19.99`.",
                ephemeral=True,
            )
            return

        con = db()
        try:
            produto = con.execute(
                "SELECT * FROM products WHERE guild_id=? AND local_id=?",
                (self.guild_id, self.produto_local_id),
            ).fetchone()
            if not produto:
                await interaction.response.send_message(
                    f"❌ Produto `#{self.produto_local_id}` não encontrado neste servidor.",
                    ephemeral=True,
                )
                return
            con.execute(
                "UPDATE products SET price=? WHERE id=? AND guild_id=?",
                (novo_preco, int(produto["id"]), self.guild_id),
            )
            con.commit()
            nome = str(produto["name"])
        finally:
            con.close()

        await interaction.response.send_message(
            f"✅ Preço de **{nome}** (`#{self.produto_local_id}`) alterado para **{money(novo_preco)}**.",
            ephemeral=True,
        )


@bot.tree.command(
    name="ajustar-preco",
    description="Altera o preço pelo ID visível usando um campo de texto seguro",
)
@app_commands.describe(produto_id="ID visível mostrado em /loja produtos, ex.: 131")
async def ajustar_preco(interaction: discord.Interaction, produto_id: int):
    if not await protected_admin_only(interaction):
        return
    if not interaction.guild:
        await interaction.response.send_message(
            "❌ Use este comando dentro do servidor.", ephemeral=True
        )
        return

    con = db()
    try:
        produto = con.execute(
            "SELECT id,name FROM products WHERE guild_id=? AND local_id=?",
            (interaction.guild.id, int(produto_id)),
        ).fetchone()
    finally:
        con.close()

    if not produto:
        await interaction.response.send_message(
            f"❌ Produto `#{produto_id}` não encontrado neste servidor.",
            ephemeral=True,
        )
        return

    await interaction.response.send_modal(
        AjustarPrecoModal(produto_id, interaction.guild.id)
    )


@bot.tree.command(name="editar-produto", description="Edita produto pelo ID visível da loja")
@app_commands.describe(produto_id="ID visível mostrado em /loja produtos (ex.: 122)")
async def editar_produto(
    interaction,
    produto_id: int,
    nome: Optional[str] = None,
    preco: Optional[str] = None,
    estoque: Optional[int] = None,
    descricao: Optional[str] = None,
    imagem: Optional[str] = None,
    banner: Optional[str] = None,
):
    if not await protected_admin_only(interaction):
        return
    try:
        preco_final = parse_price_input(preco) if preco is not None else None
    except Exception:
        await interaction.response.send_message(
            "❌ Preço inválido. Use `19,99` ou `19.99`.", ephemeral=True
        )
        return
    con = db()
    try:
        # O número exibido ao administrador é products.local_id, que é separado
        # para cada servidor. Primeiro resolvemos por ele. O fallback em products.id
        # mantém compatibilidade com instalações antigas.
        p = con.execute(
            "SELECT * FROM products WHERE guild_id=? AND local_id=?",
            (interaction.guild.id, int(produto_id)),
        ).fetchone()
        if not p:
            p = con.execute(
                "SELECT * FROM products WHERE guild_id=? AND id=?",
                (interaction.guild.id, int(produto_id)),
            ).fetchone()
        if not p:
            await interaction.response.send_message(
                f"❌ Produto `#{produto_id}` não encontrado neste servidor.",
                ephemeral=True,
            )
            return

        global_id = int(p["id"])
        con.execute(
            """
            UPDATE products
            SET name=?,price=?,stock=?,description=?,image_url=?,banner_url=?
            WHERE id=? AND guild_id=?
            """,
            (
                nome or p["name"],
                preco_final if preco_final is not None else p["price"],
                estoque if estoque is not None else p["stock"],
                descricao if descricao is not None else p["description"],
                imagem if imagem is not None else p["image_url"],
                banner if banner is not None else p["banner_url"],
                global_id,
                interaction.guild.id,
            ),
        )
        con.commit()
        nome_final = nome or p["name"]
    finally:
        con.close()

    await interaction.response.send_message(
        f"✅ Produto **{nome_final}** (`#{produto_id}`) editado.", ephemeral=True
    )


@bot.tree.command(name="remover-produto", description="Desativa produto pelo ID visível da loja")
@app_commands.describe(produto_id="ID visível mostrado em /loja produtos (ex.: 122)")
async def remover_produto(interaction, produto_id: int):
    if not await protected_admin_only(interaction):
        return

    con = db()
    try:
        # IMPORTANTE: /loja produtos exibe local_id, não o products.id global.
        # Isso evita o bug em que o comando dizia que removeu, mas o produto
        # continuava aparecendo em Opções.
        p = con.execute(
            "SELECT * FROM products WHERE guild_id=? AND local_id=?",
            (interaction.guild.id, int(produto_id)),
        ).fetchone()
        if not p:
            # Compatibilidade com produtos/instalações antigas.
            p = con.execute(
                "SELECT * FROM products WHERE guild_id=? AND id=?",
                (interaction.guild.id, int(produto_id)),
            ).fetchone()

        if not p:
            await interaction.response.send_message(
                f"❌ Produto `#{produto_id}` não encontrado neste servidor.",
                ephemeral=True,
            )
            return

        global_id = int(p["id"])
        visible_id = p["local_id"] if p["local_id"] is not None else produto_id
        nome_produto = str(p["name"] or f"Produto #{visible_id}")

        con.execute(
            "UPDATE products SET active=0 WHERE id=? AND guild_id=?",
            (global_id, interaction.guild.id),
        )
        con.commit()
    finally:
        con.close()

    await interaction.response.send_message(
        f"✅ Produto **{nome_produto}** (`#{visible_id}`) desativado e removido de **Opções**.",
        ephemeral=True,
    )


@bot.tree.command(name="cobrar", description="Cria cobrança PIX personalizada")
async def cobrar(interaction, valor: float, descricao: str = "Cobrança personalizada"):
    if not await protected_admin_only(interaction):
        return
    code = random_code()
    cfg = get_config(interaction.guild.id)
    key = cfg["pix_key"] or PIX_KEY
    if not key:
        await interaction.response.send_message(
            "❌ Configure PIX primeiro.", ephemeral=True
        )
        return
    payload = pix_payload(
        key, cfg["pix_name"] or PIX_NOME, cfg["pix_city"] or PIX_CIDADE, valor, code
    )
    file = discord.File(make_qr_bytes(payload), filename="pix.png")
    embed = discord.Embed(
        title="💰 Cobrança PIX", description=descricao, color=0x00A86B
    )
    embed.add_field(name="Valor", value=money(valor))
    embed.add_field(name="Código", value=code)
    embed.add_field(name="Copia e cola", value=f"```{payload[:900]}```", inline=False)
    embed.set_image(url="attachment://pix.png")
    await interaction.response.send_message(embed=embed, file=file, ephemeral=True)


@bot.tree.command(name="estatisticas", description="Mostra estatísticas de vendas")
async def estatisticas(interaction):
    con = db()
    rows = con.execute(
        "SELECT * FROM orders WHERE guild_id=?", (interaction.guild.id,)
    ).fetchall()
    con.close()
    total = sum(r["amount"] for r in rows)
    pend = sum(1 for r in rows if r["status"] == "pendente")
    ok = sum(1 for r in rows if r["status"] == "aprovado")
    embed = discord.Embed(title="💫 Seus rendimentos durante:", color=0x2B2D31)
    embed.add_field(name="💎 Pedidos", value=str(len(rows)), inline=True)
    embed.add_field(name="💰 Recebimentos", value=money(total), inline=True)
    embed.add_field(name="⏳ Pendentes", value=str(pend), inline=True)
    embed.add_field(name="✅ Aprovados", value=str(ok), inline=True)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(
    name="conectar",
    description="Conecta e salva o canal de voz para reconexão automática",
)
async def conectar(
    interaction: discord.Interaction,
    canal: Optional[app_commands.AppCommandChannel] = None,
):
    if not await protected_admin_only(interaction):
        return

    channel = None

    if canal is not None:
        channel = interaction.guild.get_channel(canal.id)

    if channel is None and interaction.user.voice:
        channel = interaction.user.voice.channel

    if not isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
        await interaction.response.send_message(
            "❌ Entre em uma call ou selecione um canal de voz válido.",
            ephemeral=True,
        )
        return

    con = db()
    con.execute(
        "CREATE TABLE IF NOT EXISTS guild_voice_config(guild_id INTEGER PRIMARY KEY,channel_id INTEGER,enabled INTEGER DEFAULT 1,updated_at TEXT)"
    )
    con.execute(
        "INSERT INTO guild_voice_config(guild_id,channel_id,enabled,updated_at) VALUES(?,?,1,?) ON CONFLICT(guild_id) DO UPDATE SET channel_id=excluded.channel_id,enabled=1,updated_at=excluded.updated_at",
        (interaction.guild.id, channel.id, now_iso()),
    )
    con.commit()
    con.close()

    await interaction.response.defer(ephemeral=True, thinking=True)

    try:
        vc = interaction.guild.voice_client

        if vc and vc.is_connected():
            await vc.move_to(channel)
        else:
            await channel.connect(
                timeout=30,
                reconnect=True,
                self_deaf=True,
            )

        await interaction.followup.send(
            f"✅ Conectado e canal salvo: {channel.mention}. Vou voltar automaticamente após reinícios.",
            ephemeral=True,
        )
    except Exception as error:
        await interaction.followup.send(
            f"⚠️ Canal salvo, mas falhou ao conectar agora: `{str(error)[:400]}`",
            ephemeral=True,
        )


@bot.tree.command(
    name="desconectar", description="Sai da call e desativa reconexão automática"
)
async def desconectar(interaction):
    if not await protected_admin_only(interaction):
        return
    con = db()
    con.execute(
        "CREATE TABLE IF NOT EXISTS guild_voice_config(guild_id INTEGER PRIMARY KEY,channel_id INTEGER,enabled INTEGER DEFAULT 1,updated_at TEXT)"
    )
    con.execute(
        "UPDATE guild_voice_config SET enabled=0,updated_at=? WHERE guild_id=?",
        (now_iso(), interaction.guild.id),
    )
    con.commit()
    con.close()
    if interaction.guild.voice_client:
        await interaction.guild.voice_client.disconnect(force=True)
    await interaction.response.send_message(
        "✅ Desconectado e reconexão automática desativada.", ephemeral=True
    )


@bot.tree.command(
    name="painel-ticket", description="Envia painel de ticket com imagem opcional"
)
@app_commands.describe(
    titulo="Título do painel",
    descricao="Descrição do painel",
    imagem="URL da imagem/banner",
)
async def painel_ticket(
    interaction,
    titulo: Optional[str] = None,
    descricao: Optional[str] = None,
    imagem: Optional[str] = None,
):
    if not await protected_admin_only(interaction):
        return
    await interaction.response.defer(ephemeral=True)
    embed = ticket_panel_embed(titulo, descricao, imagem)
    await interaction.channel.send(embed=embed, view=TicketPanelView())
    await interaction.followup.send("✅ Painel de ticket enviado.", ephemeral=True)


@bot.tree.command(
    name="criar-tickets-modo-canais", description="Cria painel de ticket por canais"
)
async def criar_tickets_modo_canais(interaction):
    if not await protected_admin_only(interaction):
        return
    await interaction.response.defer(ephemeral=True)
    await interaction.channel.send(embed=ticket_panel_embed(), view=TicketPanelView())
    await interaction.followup.send("✅ Painel de ticket enviado.", ephemeral=True)


@bot.tree.command(
    name="criar-tickets-modo-topico", description="Cria painel de ticket por tópico"
)
async def criar_tickets_modo_topico(interaction):
    if not await protected_admin_only(interaction):
        return
    await interaction.response.defer(ephemeral=True)
    await interaction.channel.send(embed=ticket_panel_embed(), view=TicketPanelView())
    await interaction.followup.send("✅ Painel de ticket enviado.", ephemeral=True)


@bot.tree.command(name="criar-categoria", description="Cria categoria da loja")
async def criar_categoria(interaction, nome: str):
    if not await protected_admin_only(interaction):
        return
    cat = await interaction.guild.create_category(nome)
    await interaction.response.send_message(
        f"✅ Categoria criada: `{cat.name}`", ephemeral=True
    )


@bot.tree.command(
    name="criar-painel-captcha",
    description="Cria painel simples de captcha/verificação",
)
async def criar_painel_captcha(interaction):
    if not await protected_admin_only(interaction):
        return
    await interaction.channel.send(
        embed=discord.Embed(
            title="✅ Verificação",
            description="Clique para liberar acesso.",
            color=0x00FF99,
        ),
        view=CaptchaView(),
    )
    await interaction.response.send_message(
        "✅ Painel captcha enviado.", ephemeral=True
    )


class CaptchaView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="✅ Verificar", style=discord.ButtonStyle.green)
    async def verify(self, interaction, button):
        await interaction.response.send_message("✅ Verificado.", ephemeral=True)


@bot.tree.command(name="limpar", description="Apaga mensagens")
async def limpar(interaction, quantidade: int):
    if not await protected_admin_only(interaction):
        return
    await interaction.response.defer(ephemeral=True)
    deleted = await interaction.channel.purge(limit=min(quantidade, 100))
    await interaction.followup.send(
        f"✅ Apaguei {len(deleted)} mensagens.", ephemeral=True
    )


@bot.tree.command(
    name="enviar-mensagem-servidor",
    description="Envia mensagem em canal selecionado",
)
async def enviar_mensagem_servidor(
    interaction: discord.Interaction,
    canal: discord.TextChannel,
    mensagem: str,
):
    if not await protected_admin_only(interaction):
        return

    await interaction.response.defer(ephemeral=True, thinking=True)

    try:
        bot_member = interaction.guild.me
        if bot_member is None:
            await interaction.followup.send(
                "❌ Não consegui localizar o bot neste servidor.",
                ephemeral=True,
            )
            return

        permissoes = canal.permissions_for(bot_member)

        if not permissoes.view_channel:
            await interaction.followup.send(
                "❌ Não tenho permissão para visualizar esse canal.",
                ephemeral=True,
            )
            return

        if not permissoes.send_messages:
            await interaction.followup.send(
                "❌ Não tenho permissão para enviar mensagens nesse canal.",
                ephemeral=True,
            )
            return

        await canal.send(mensagem)
        await interaction.followup.send(
            f"✅ Mensagem enviada em {canal.mention}.",
            ephemeral=True,
        )

    except discord.Forbidden:
        await interaction.followup.send(
            "❌ O Discord bloqueou o envio. Confira as permissões do bot nesse canal.",
            ephemeral=True,
        )
    except discord.HTTPException as erro:
        await interaction.followup.send(
            f"❌ O Discord recusou a mensagem: `{str(erro)[:700]}`",
            ephemeral=True,
        )
    except Exception as erro:
        print(
            f"[ERRO enviar-mensagem-servidor] "
            f"{type(erro).__name__}: {erro}"
        )
        traceback.print_exc()
        await interaction.followup.send(
            "❌ Ocorreu um erro inesperado ao enviar a mensagem.",
            ephemeral=True,
        )


@bot.tree.command(name="enviar-mensagem-dm", description="Envia DM para usuário")
async def enviar_mensagem_dm(interaction, usuario: discord.Member, mensagem: str):
    if not await protected_admin_only(interaction):
        return
    try:
        await usuario.send(mensagem)
        await interaction.response.send_message("✅ DM enviada.", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"❌ Erro: `{e}`", ephemeral=True)


@bot.tree.command(
    name="status-adicionar", description="Adiciona status/atividade no bot"
)
async def status_adicionar(interaction, texto: str):
    if not await protected_admin_only(interaction):
        return
    await bot.change_presence(activity=discord.Game(name=texto))
    await interaction.response.send_message("✅ Status alterado.", ephemeral=True)


@bot.tree.command(name="status-remover", description="Remove status do bot")
async def status_remover(interaction):
    if not await protected_admin_only(interaction):
        return
    await bot.change_presence(activity=None)
    await interaction.response.send_message("✅ Status removido.", ephemeral=True)


@bot.tree.command(name="desbloquear", description="Desbloqueia comandos no canal atual")
async def desbloquear(interaction):
    if not await protected_admin_only(interaction):
        return
    await interaction.channel.set_permissions(
        interaction.guild.default_role, send_messages=True
    )
    await interaction.response.send_message("✅ Canal desbloqueado.", ephemeral=True)


@bot.tree.command(
    name="restaurar-servidor", description="Restaura dados básicos do servidor no banco"
)
async def restaurar_servidor(interaction):
    if not await protected_admin_only(interaction):
        return
    ensure_config(interaction.guild.id)
    await interaction.response.send_message(
        "✅ Dados restaurados/sincronizados no banco.", ephemeral=True
    )


# ================= TRATAMENTO GLOBAL DE ERROS =================
@bot.tree.error
async def on_app_command_error(
    interaction: discord.Interaction,
    error: app_commands.AppCommandError,
):
    comando = interaction.command.name if interaction.command else "desconhecido"
    servidor = interaction.guild.name if interaction.guild else "DM"
    servidor_id = interaction.guild.id if interaction.guild else "sem ID"
    erro_original = getattr(error, "original", error)

    print(
        "\n[ERRO SLASH COMMAND]"
        f"\nComando: {comando}"
        f"\nServidor: {servidor}"
        f"\nServidor ID: {servidor_id}"
        f"\nUsuário: {interaction.user} ({interaction.user.id})"
        f"\nTipo: {type(erro_original).__name__}"
        f"\nErro: {erro_original}"
    )
    traceback.print_exception(
        type(erro_original),
        erro_original,
        erro_original.__traceback__,
    )

    if isinstance(error, app_commands.TransformerError):
        mensagem = (
            "❌ O Discord não conseguiu interpretar uma das opções do comando.\n\n"
            "Os slash commands deste servidor podem estar desatualizados. "
            "Feche e abra o Discord e tente novamente após a sincronização."
        )
    elif isinstance(error, app_commands.MissingPermissions):
        mensagem = "❌ Você não possui permissão para usar este comando."
    elif isinstance(erro_original, discord.Forbidden):
        mensagem = (
            "❌ O bot não possui permissão suficiente neste servidor "
            "ou neste canal."
        )
    elif isinstance(erro_original, discord.NotFound):
        mensagem = (
            "❌ A interação expirou ou o canal/mensagem não foi encontrado. "
            "Execute o comando novamente."
        )
    else:
        mensagem = (
            "❌ Não consegui executar este comando. "
            "O erro completo foi registrado no console do bot."
        )

    try:
        if interaction.response.is_done():
            await interaction.followup.send(mensagem, ephemeral=True)
        else:
            await interaction.response.send_message(mensagem, ephemeral=True)
    except discord.HTTPException as response_error:
        print(
            "[ERRO AO RESPONDER INTERAÇÃO] "
            f"{type(response_error).__name__}: {response_error}"
        )




# ============================================================================
# LOCK SENSI INTEGRADO — TICKET PREMIUM + GERADOR DE SENSI + LISTAR PAINÉIS
# Não precisa de locksensi_extras.py. Tudo abaixo faz parte deste bot.py.
# ============================================================================
from datetime import timezone as _lsx_timezone

LSX_BOT = None
LSX_ADMIN_CHECK = None
LSX_BLACK = 0x111111
LSX_TICKET_BANNER = os.getenv("TICKET_IMAGE_URL", "").strip()
LSX_SENSI_BANNER = os.getenv("SENSI_BANNER_URL", "").strip()


def lsx_now_iso():
    return datetime.now(_lsx_timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def lsx_valid_url(v):
    return bool(v and re.match(r"^https?://", str(v).strip(), re.I))


def lsx_platform_label(v):
    v = str(v or "").lower().strip()
    return {"tiktok": "TikTok", "instagram": "Instagram", "discord": "Discord"}.get(v, v.title() or "rede social")


def lsx_ensure_schema():
    con = db()
    try:
        con._conn.execute("""
            CREATE TABLE IF NOT EXISTS sensi_config(
                guild_id BIGINT PRIMARY KEY,
                access_role_id BIGINT NULL,
                proof_channel_id BIGINT NULL,
                verification_platform TEXT NOT NULL DEFAULT 'tiktok',
                verification_url TEXT NULL,
                banner_url TEXT NULL,
                cooldown_seconds INTEGER NOT NULL DEFAULT 0,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        con._conn.execute("""
            CREATE TABLE IF NOT EXISTS sensi_requests(
                id BIGSERIAL PRIMARY KEY,
                guild_id BIGINT NOT NULL,
                user_id BIGINT NOT NULL,
                profile_text TEXT NULL,
                proof_text TEXT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                reviewed_by BIGINT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        con._conn.execute("""
            CREATE TABLE IF NOT EXISTS sensi_generations(
                id BIGSERIAL PRIMARY KEY,
                guild_id BIGINT NOT NULL,
                user_id BIGINT NOT NULL,
                platform TEXT NOT NULL,
                device_name TEXT NULL,
                style_name TEXT NULL,
                payload TEXT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        con.commit()
    finally:
        con.close()


def lsx_get_cfg(guild_id):
    lsx_ensure_schema()
    con = db()
    try:
        return con.execute("SELECT * FROM sensi_config WHERE guild_id=?", (int(guild_id),)).fetchone()
    finally:
        con.close()


def lsx_save_cfg(guild_id, role_id, channel_id, platform, url, banner, cooldown):
    con = db()
    try:
        con.execute("""
            INSERT INTO sensi_config(
                guild_id,access_role_id,proof_channel_id,verification_platform,
                verification_url,banner_url,cooldown_seconds,updated_at
            ) VALUES(?,?,?,?,?,?,?,?)
            ON CONFLICT(guild_id) DO UPDATE SET
                access_role_id=excluded.access_role_id,
                proof_channel_id=excluded.proof_channel_id,
                verification_platform=excluded.verification_platform,
                verification_url=excluded.verification_url,
                banner_url=excluded.banner_url,
                cooldown_seconds=excluded.cooldown_seconds,
                updated_at=excluded.updated_at
        """, (int(guild_id), int(role_id), int(channel_id), platform, url or "", banner or "", max(0, int(cooldown)), lsx_now_iso()))
        con.commit()
    finally:
        con.close()


def lsx_is_staff(interaction):
    p = getattr(interaction.user, "guild_permissions", None)
    return bool(p and (p.administrator or p.manage_guild or p.manage_channels))


async def lsx_has_access(interaction, notify=True):
    if not interaction.guild:
        if notify:
            await interaction.response.send_message("❌ Use este recurso dentro do servidor.", ephemeral=True)
        return False
    if lsx_is_staff(interaction):
        return True
    cfg = lsx_get_cfg(interaction.guild.id)
    if not cfg or not cfg["access_role_id"]:
        if notify:
            await interaction.response.send_message("🔒 O gerador ainda não foi configurado.", ephemeral=True)
        return False
    role = interaction.guild.get_role(int(cfg["access_role_id"]))
    if role and role in getattr(interaction.user, "roles", []):
        return True
    if notify:
        await interaction.response.send_message(
            f"🔒 Você ainda não tem acesso. Use **✅ Validar Acesso** e envie a prova de que seguiu a Lock Sensi no **{lsx_platform_label(cfg['verification_platform'])}**.",
            ephemeral=True,
        )
    return False


# ---------------- TICKETS ----------------
LSX_TICKET_DESC = (
    "Precisa de ajuda com **compra, acesso, instalação** ou tem alguma dúvida sobre nossos produtos?\n\n"
    "Abra um ticket e fale com a equipe da **Lock Sensi**.\n"
    "Selecione abaixo o tipo de atendimento desejado."
)


class LSXTicketOpenButton(discord.ui.Button):
    def __init__(self, tipo, label, style, custom_id):
        super().__init__(label=label, style=style, custom_id=custom_id)
        self.tipo = tipo

    async def callback(self, interaction):
        await lsx_create_ticket(interaction, self.tipo)


class LSXPremiumTicketPanel(discord.ui.LayoutView):
    def __init__(self, titulo="PAINEL DE ATENDIMENTO LOCK SENSI", descricao=LSX_TICKET_DESC, imagem=None):
        super().__init__(timeout=None)
        c = discord.ui.Container(accent_color=LSX_BLACK)
        img = str(imagem or LSX_TICKET_BANNER or "").strip()
        if lsx_valid_url(img):
            g = discord.ui.MediaGallery()
            g.add_item(media=img, description="Lock Sensi • Atendimento")
            c.add_item(g)
            c.add_item(discord.ui.Separator(visible=True))
        c.add_item(discord.ui.TextDisplay(f"## {titulo}"))
        c.add_item(discord.ui.Separator(visible=True))
        c.add_item(discord.ui.TextDisplay(descricao[:3900]))
        c.add_item(discord.ui.Separator(visible=True))
        c.add_item(discord.ui.TextDisplay("-# Lock Sensi • Atendimento rápido e organizado"))
        self.add_item(c)
        self.add_item(discord.ui.ActionRow(
            LSXTicketOpenButton("suporte", "🎧 Suporte", discord.ButtonStyle.primary, "ls_ticket_suporte_v1"),
            LSXTicketOpenButton("duvidas", "❓ Dúvidas", discord.ButtonStyle.secondary, "ls_ticket_duvidas_v1"),
            LSXTicketOpenButton("streamers", "🎥 Streamers", discord.ButtonStyle.success, "ls_ticket_streamers_v1"),
        ))


class LSXTicketControls(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="📌 Assumir", style=discord.ButtonStyle.secondary, custom_id="ls_ticket_take_v1")
    async def take(self, interaction, button):
        if not lsx_is_staff(interaction):
            await interaction.response.send_message("❌ Apenas a equipe pode assumir o ticket.", ephemeral=True)
            return
        await interaction.response.send_message(f"📌 Atendimento assumido por {interaction.user.mention}.")

    @discord.ui.button(label="✅ Resolver", style=discord.ButtonStyle.success, custom_id="ls_ticket_resolve_v1")
    async def resolve(self, interaction, button):
        if not lsx_is_staff(interaction):
            await interaction.response.send_message("❌ Apenas a equipe pode finalizar o ticket.", ephemeral=True)
            return
        await interaction.response.send_message("✅ Atendimento concluído. Fechando em 5 segundos...")
        import asyncio
        await asyncio.sleep(5)
        try:
            await interaction.channel.delete(reason=f"Ticket resolvido por {interaction.user}")
        except Exception:
            pass

    @discord.ui.button(label="🔒 Fechar", style=discord.ButtonStyle.danger, custom_id="ls_ticket_close_v1")
    async def close(self, interaction, button):
        await interaction.response.send_message("🔒 Fechando em 5 segundos...", ephemeral=True)
        import asyncio
        await asyncio.sleep(5)
        try:
            await interaction.channel.delete(reason=f"Ticket fechado por {interaction.user}")
        except Exception:
            pass


async def lsx_create_ticket(interaction, tipo):
    await interaction.response.defer(ephemeral=True)
    guild, user = interaction.guild, interaction.user
    category_name = os.getenv("TICKET_CATEGORY_NAME", "tickets")
    category = discord.utils.get(guild.categories, name=category_name)
    if not category:
        category = await guild.create_category(category_name)

    for ch in category.text_channels:
        if ch.name.startswith(f"{tipo}-") and f"user_id:{user.id}" in (ch.topic or ""):
            await interaction.followup.send(f"⚠️ Você já tem um ticket aberto: {ch.mention}", ephemeral=True)
            return

    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        user: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, attach_files=True, embed_links=True),
        guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, manage_channels=True),
    }
    safe = re.sub(r"[^a-zA-Z0-9_-]+", "-", user.name).strip("-").lower()
    ch = await guild.create_text_channel(
        f"{tipo}-{safe}"[:90], category=category, overwrites=overwrites,
        topic=f"Lock Sensi | tipo:{tipo} | user_id:{user.id}"
    )

    if tipo == "streamers":
        body = "**Para agilizar, envie:**\n• Link do TikTok/Instagram\n• Seguidores\n• Média de views ou público da live\n• Como pretende divulgar a Lock Sensi"
        label = "🎥 STREAMERS"
    elif tipo == "suporte":
        body = "**Para agilizar, envie:**\n• Produto utilizado\n• O que aconteceu\n• Print/vídeo do problema\n• Informações do PC ou celular"
        label = "🎧 SUPORTE"
    else:
        body = "Envie sua dúvida com detalhes. Se for sobre um produto, informe também o nome dele."
        label = "❓ DÚVIDAS"

    embed = discord.Embed(
        title="LOCK SENSI • ATENDIMENTO",
        description=f"Olá, {user.mention}. Seu atendimento foi aberto com sucesso.\n\n**Categoria:** {label}\n\n{body}\n\nNossa equipe responderá assim que possível.",
        color=LSX_BLACK,
    )
    if guild.icon:
        embed.set_thumbnail(url=guild.icon.url)
    if lsx_valid_url(LSX_TICKET_BANNER):
        embed.set_image(url=LSX_TICKET_BANNER)
    embed.set_footer(text="Lock Sensi • Atendimento Premium")
    await ch.send(content=user.mention, embed=embed, view=LSXTicketControls())
    await interaction.followup.send(f"✅ Ticket criado: {ch.mention}", ephemeral=True)


# ---------------- SENSI ----------------
def lsx_style_key(v):
    v = str(v or "").lower()
    if any(x in v for x in ("control", "pesad", "precis")):
        return "controle"
    if any(x in v for x in ("rapid", "alta", "leve")):
        return "rapida"
    return "equilibrada"


def lsx_pick(rng, a, b):
    return rng.randint(a, b)


def lsx_generate_sensi(platform, device, style):
    rng = random.SystemRandom()
    style = lsx_style_key(style)
    platform = platform.lower()

    if platform == "android":
        sets = {
            "controle": [(150,175),(140,165),(125,150),(110,140),(45,70),(145,175),(440,580),(48,55)],
            "equilibrada": [(170,190),(155,180),(140,170),(125,155),(55,80),(160,190),(520,680),(50,57)],
            "rapida": [(185,200),(175,195),(160,190),(145,175),(65,90),(180,200),(620,800),(52,60)],
        }[style]
        v = [lsx_pick(rng,*r) for r in sets]
        return {"Plataforma":"Android","Aparelho":device,"Estilo":style.title(),"Geral":v[0],"Red Dot":v[1],"Mira 2x":v[2],"Mira 4x":v[3],"AWM":v[4],"Olhadinha":v[5],"DPI base":v[6],"Botão de atirar":f"{v[7]}%","Ajuste fino":"Se passar da cabeça, reduza Geral/Red Dot em 3–5 pontos."}

    if platform == "ios":
        sets = {
            "controle": [(150,175),(140,165),(125,150),(110,140),(45,70),(145,175),(48,54),(52,65)],
            "equilibrada": [(170,190),(155,180),(140,170),(125,155),(55,80),(160,190),(50,57),(60,75)],
            "rapida": [(185,200),(175,195),(160,190),(145,175),(65,90),(180,200),(52,60),(68,82)],
        }[style]
        v = [lsx_pick(rng,*r) for r in sets]
        return {"Plataforma":"iOS","Aparelho":device,"Estilo":style.title(),"Geral":v[0],"Red Dot":v[1],"Mira 2x":v[2],"Mira 4x":v[3],"AWM":v[4],"Olhadinha":v[5],"Botão de atirar":f"{v[6]}%","Rastreamento base":v[7],"Ciclos":3,"DPI":"Não se aplica ao iOS; use o ajuste de rastreamento.","Ajuste fino":"Teste no treino e altere 3–5 pontos por vez."}

    sets = {
        "controle": [(135,160),(125,150),(110,140),(95,125),(35,60),(135,165)],
        "equilibrada": [(150,175),(140,165),(125,155),(110,140),(45,70),(150,180)],
        "rapida": [(170,195),(160,185),(145,175),(130,160),(55,80),(170,195)],
    }[style]
    v = [lsx_pick(rng,*r) for r in sets]
    return {"Plataforma":"Emulador / PC","Setup":device,"Estilo":style.title(),"Geral":v[0],"Red Dot":v[1],"Mira 2x":v[2],"Mira 4x":v[3],"AWM":v[4],"Olhadinha":v[5],"DPI do mouse":rng.choice([800,1000,1200,1600]),"Eixo X base":round(rng.uniform(.72,.96),2),"Eixo Y base":round(rng.uniform(.66,.92),2),"Windows":"6/11","Polling rate":"1000 Hz (se suportado)","Ajuste fino":"Se ficar rápida demais, reduza X/Y ou Geral em pequenos passos."}


def lsx_sensi_embed(user, payload):
    device = payload.get("Aparelho") or payload.get("Setup") or "Não informado"
    lines = []
    for k,v in payload.items():
        if k not in ("Plataforma","Aparelho","Setup","Estilo","Ajuste fino"):
            lines.append(f"**{k}:** `{v}`")
    e = discord.Embed(
        title=f"🎯 SENSI LOCK SENSI • {payload['Plataforma'].upper()}",
        description=f"**Dispositivo:** `{device}`\n**Perfil:** `{payload['Estilo']}`\n\n" + "\n".join(lines) + f"\n\n**Ajuste recomendado:** {payload['Ajuste fino']}\n\n-# Base para teste; não garante resultado automático.",
        color=LSX_BLACK,
    )
    e.set_thumbnail(url=user.display_avatar.url)
    e.set_footer(text="Lock Sensi • Gerador de Sensibilidade")
    return e


class LSXDeviceModal(discord.ui.Modal):
    def __init__(self, platform):
        names = {"android":"Android","ios":"iOS","emulador":"Emulador"}
        super().__init__(title=f"Gerar Sensi • {names.get(platform, platform)}")
        self.platform = platform
        self.device = discord.ui.TextInput(label="Celular / setup", placeholder="Ex: iPhone 13, Redmi Note 13 ou MSI + G203", max_length=120)
        self.style = discord.ui.TextInput(label="Estilo", placeholder="controle, equilibrada ou rápida", default="equilibrada", max_length=30)
        self.add_item(self.device)
        self.add_item(self.style)

    async def on_submit(self, interaction):
        if not await lsx_has_access(interaction):
            return

        cfg = lsx_get_cfg(interaction.guild.id)
        cooldown_seconds = int(cfg["cooldown_seconds"] or 0) if cfg else 0
        if cooldown_seconds > 0 and not lsx_is_staff(interaction):
            con = db()
            try:
                last = con.execute(
                    "SELECT created_at FROM sensi_generations WHERE guild_id=? AND user_id=? ORDER BY id DESC LIMIT 1",
                    (int(interaction.guild.id), int(interaction.user.id)),
                ).fetchone()
            finally:
                con.close()
            if last and last["created_at"]:
                created = last["created_at"]
                if isinstance(created, str):
                    try:
                        created = datetime.fromisoformat(created.replace("Z", "+00:00"))
                    except Exception:
                        created = None
                if created is not None:
                    if created.tzinfo is None:
                        created = created.replace(tzinfo=_lsx_timezone.utc)
                    elapsed = (datetime.now(_lsx_timezone.utc) - created).total_seconds()
                    remaining = int(cooldown_seconds - elapsed)
                    if remaining > 0:
                        await interaction.response.send_message(
                            f"⏳ Aguarde **{remaining}s** para gerar outra sensi.",
                            ephemeral=True,
                        )
                        return

        payload = lsx_generate_sensi(self.platform, str(self.device).strip(), str(self.style).strip())
        con = db()
        try:
            con.execute("INSERT INTO sensi_generations(guild_id,user_id,platform,device_name,style_name,payload,created_at) VALUES(?,?,?,?,?,?,?)", (interaction.guild.id, interaction.user.id, self.platform, str(self.device), str(self.style), str(payload), lsx_now_iso()))
            con.commit()
        finally:
            con.close()
        await interaction.response.send_message(embed=lsx_sensi_embed(interaction.user, payload), ephemeral=True)


class LSXPlatformView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=300)

    @discord.ui.button(label="📱 Android", style=discord.ButtonStyle.success)
    async def android(self, interaction, button):
        await interaction.response.send_modal(LSXDeviceModal("android"))

    @discord.ui.button(label="🍎 iOS", style=discord.ButtonStyle.primary)
    async def ios(self, interaction, button):
        await interaction.response.send_modal(LSXDeviceModal("ios"))

    @discord.ui.button(label="🖥️ Emulador", style=discord.ButtonStyle.secondary)
    async def emu(self, interaction, button):
        await interaction.response.send_modal(LSXDeviceModal("emulador"))


class LSXProofModal(discord.ui.Modal, title="Validar acesso • Lock Sensi"):
    perfil = discord.ui.TextInput(label="@ ou link do seu perfil", placeholder="@usuario ou https://...", max_length=200)
    prova = discord.ui.TextInput(label="Comprovação", placeholder="Cole o link do print ou descreva a prova", style=discord.TextStyle.paragraph, max_length=1000)

    async def on_submit(self, interaction):
        cfg = lsx_get_cfg(interaction.guild.id)
        if not cfg or not cfg["proof_channel_id"] or not cfg["access_role_id"]:
            await interaction.response.send_message("❌ A validação ainda não foi configurada.", ephemeral=True)
            return
        role = interaction.guild.get_role(int(cfg["access_role_id"]))
        if role and role in interaction.user.roles:
            await interaction.response.send_message("✅ Seu acesso já está liberado.", ephemeral=True)
            return

        con = db()
        try:
            pending = con.execute(
                "SELECT id FROM sensi_requests WHERE guild_id=? AND user_id=? AND status='pending' ORDER BY id DESC LIMIT 1",
                (int(interaction.guild.id), int(interaction.user.id)),
            ).fetchone()
        finally:
            con.close()
        if pending:
            await interaction.response.send_message(
                "⏳ Você já possui uma validação aguardando análise da equipe.",
                ephemeral=True,
            )
            return

        channel = interaction.guild.get_channel(int(cfg["proof_channel_id"]))
        if not channel:
            await interaction.response.send_message("❌ Canal de validação não encontrado.", ephemeral=True)
            return
        con = db()
        try:
            cur = con.cursor()
            cur.execute("INSERT INTO sensi_requests(guild_id,user_id,profile_text,proof_text,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?)", (interaction.guild.id, interaction.user.id, str(self.perfil), str(self.prova), "pending", lsx_now_iso(), lsx_now_iso()))
            req_id = cur.lastrowid
            con.commit()
        finally:
            con.close()
        e = discord.Embed(title="✅ NOVA VALIDAÇÃO • SENSI", description=f"**Usuário:** {interaction.user.mention}\n**Plataforma:** {lsx_platform_label(cfg['verification_platform'])}\n**Perfil:** {str(self.perfil)[:400]}\n\n**Comprovação:**\n{str(self.prova)[:1400]}", color=0xF1C40F)
        e.set_thumbnail(url=interaction.user.display_avatar.url)
        e.set_footer(text=f"request_id:{req_id}")
        await channel.send(embed=e, view=LSXApprovalView())
        await interaction.response.send_message("📨 Comprovação enviada. Quando a staff aprovar, o cargo será entregue automaticamente.", ephemeral=True)


def lsx_request_id_from_message(message):
    try:
        m = re.search(r"request_id:(\d+)", message.embeds[0].footer.text or "")
        return int(m.group(1)) if m else None
    except Exception:
        return None


class LSXApprovalView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    async def get_request(self, interaction):
        if not lsx_is_staff(interaction):
            await interaction.response.send_message("❌ Apenas a equipe pode revisar.", ephemeral=True)
            return None
        rid = lsx_request_id_from_message(interaction.message)
        if not rid:
            await interaction.response.send_message("❌ Solicitação sem ID.", ephemeral=True)
            return None
        con = db()
        try:
            row = con.execute("SELECT * FROM sensi_requests WHERE id=?", (rid,)).fetchone()
        finally:
            con.close()
        if not row or row["status"] != "pending":
            await interaction.response.send_message("⚠️ Essa solicitação já foi finalizada ou não existe.", ephemeral=True)
            return None
        return row

    @discord.ui.button(label="✅ Aprovar", style=discord.ButtonStyle.success, custom_id="ls_sensi_approve_v1")
    async def approve(self, interaction, button):
        row = await self.get_request(interaction)
        if not row:
            return
        cfg = lsx_get_cfg(interaction.guild.id)
        role = interaction.guild.get_role(int(cfg["access_role_id"])) if cfg and cfg["access_role_id"] else None
        if not role:
            await interaction.response.send_message("❌ Cargo de acesso não encontrado.", ephemeral=True)
            return
        member = interaction.guild.get_member(int(row["user_id"]))
        if member is None:
            try:
                member = await interaction.guild.fetch_member(int(row["user_id"]))
            except Exception:
                member = None
        if not member:
            await interaction.response.send_message("❌ Usuário não encontrado no servidor.", ephemeral=True)
            return
        try:
            await member.add_roles(role, reason=f"Sensi aprovada por {interaction.user}")
        except Exception as exc:
            await interaction.response.send_message(f"❌ Não consegui dar o cargo: `{exc}`", ephemeral=True)
            return
        con = db()
        try:
            con.execute("UPDATE sensi_requests SET status='approved',reviewed_by=?,updated_at=? WHERE id=?", (interaction.user.id, lsx_now_iso(), row["id"]))
            con.commit()
        finally:
            con.close()
        e = interaction.message.embeds[0]
        e.color = 0x2ECC71
        e.add_field(name="Status", value=f"✅ Aprovado por {interaction.user.mention}", inline=False)
        await interaction.response.edit_message(embed=e, view=None)
        try:
            await member.send(f"✅ Seu acesso ao **Gerador de Sensi Lock Sensi** foi aprovado em **{interaction.guild.name}**.")
        except Exception:
            pass

    @discord.ui.button(label="❌ Recusar", style=discord.ButtonStyle.danger, custom_id="ls_sensi_reject_v1")
    async def reject(self, interaction, button):
        row = await self.get_request(interaction)
        if not row:
            return
        con = db()
        try:
            con.execute("UPDATE sensi_requests SET status='rejected',reviewed_by=?,updated_at=? WHERE id=?", (interaction.user.id, lsx_now_iso(), row["id"]))
            con.commit()
        finally:
            con.close()
        e = interaction.message.embeds[0]
        e.color = 0xE74C3C
        e.add_field(name="Status", value=f"❌ Recusado por {interaction.user.mention}", inline=False)
        await interaction.response.edit_message(embed=e, view=None)


class LSXGenerateButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="🎯 Gerar Sensi", style=discord.ButtonStyle.success, custom_id="ls_sensi_generate_v1")

    async def callback(self, interaction):
        if not await lsx_has_access(interaction):
            return
        await interaction.response.send_message("Escolha a plataforma:", view=LSXPlatformView(), ephemeral=True)


class LSXValidateButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="✅ Validar Acesso", style=discord.ButtonStyle.primary, custom_id="ls_sensi_validate_v1")

    async def callback(self, interaction):
        if await lsx_has_access(interaction, notify=False):
            await interaction.response.send_message("✅ Você já possui acesso ao gerador.", ephemeral=True)
            return
        await interaction.response.send_modal(LSXProofModal())


class LSXSensiPanel(discord.ui.LayoutView):
    def __init__(self, guild_id=None):
        super().__init__(timeout=None)
        cfg = lsx_get_cfg(guild_id) if guild_id else None
        banner = str(cfg["banner_url"] or "") if cfg else LSX_SENSI_BANNER
        platform = lsx_platform_label(cfg["verification_platform"] if cfg else "tiktok")
        url = str(cfg["verification_url"] or "") if cfg else ""
        c = discord.ui.Container(accent_color=LSX_BLACK)
        if lsx_valid_url(banner):
            g = discord.ui.MediaGallery()
            g.add_item(media=banner, description="Gerador de Sensi Lock Sensi")
            c.add_item(g)
            c.add_item(discord.ui.Separator(visible=True))
        c.add_item(discord.ui.TextDisplay("## GERADOR DE SENSI LOCK SENSI"))
        c.add_item(discord.ui.Separator(visible=True))
        c.add_item(discord.ui.TextDisplay("Gere uma configuração personalizada para **Android, iOS ou Emulador**.\n\nInforme seu dispositivo/setup e receba uma base pronta para testar.\n**Acesso exclusivo para membros liberados pela Lock Sensi.**"))
        c.add_item(discord.ui.Separator(visible=True))
        c.add_item(discord.ui.TextDisplay(f"**🔓 Como liberar:** siga a Lock Sensi no **{platform}** e envie sua comprovação."))
        c.add_item(discord.ui.Separator(visible=True))
        c.add_item(discord.ui.TextDisplay("-# Lock Sensi • Sensibilidade personalizada para teste"))
        self.add_item(c)
        buttons = [LSXGenerateButton()]
        if lsx_valid_url(url):
            buttons.append(discord.ui.Button(label=f"🔗 Abrir {platform}", style=discord.ButtonStyle.link, url=url))
        buttons.append(LSXValidateButton())
        self.add_item(discord.ui.ActionRow(*buttons))


async def setup_locksensi_integrated(bot, admin_check):
    global LSX_BOT, LSX_ADMIN_CHECK
    LSX_BOT = bot
    LSX_ADMIN_CHECK = admin_check
    lsx_ensure_schema()

    bot.add_view(LSXPremiumTicketPanel())
    bot.add_view(LSXTicketControls())
    bot.add_view(LSXSensiPanel())
    bot.add_view(LSXApprovalView())

    # Substitui os comandos antigos de ticket pelo painel premium.
    for _ticket_command in ("painel-ticket", "criar-tickets-modo-canais", "criar-tickets-modo-topico"):
        try:
            bot.tree.remove_command(_ticket_command)
        except Exception:
            pass

    @bot.tree.command(name="painel-ticket", description="Publica o painel premium de atendimento Lock Sensi")
    @app_commands.describe(imagem="URL do banner", titulo="Título opcional", descricao="Descrição opcional")
    async def painel_ticket(interaction: discord.Interaction, imagem: Optional[str]=None, titulo: Optional[str]=None, descricao: Optional[str]=None):
        if not await LSX_ADMIN_CHECK(interaction):
            return
        await interaction.channel.send(view=LSXPremiumTicketPanel(titulo or "PAINEL DE ATENDIMENTO LOCK SENSI", descricao or LSX_TICKET_DESC, imagem))
        await interaction.response.send_message("✅ Painel premium publicado.", ephemeral=True)

    @bot.tree.command(name="criar-tickets-modo-canais", description="Publica o painel premium de ticket")
    async def criar_tickets_modo_canais_premium(interaction: discord.Interaction):
        if not await LSX_ADMIN_CHECK(interaction):
            return
        await interaction.channel.send(view=LSXPremiumTicketPanel())
        await interaction.response.send_message("✅ Painel premium publicado.", ephemeral=True)

    @bot.tree.command(name="criar-tickets-modo-topico", description="Publica o painel premium de ticket")
    async def criar_tickets_modo_topico_premium(interaction: discord.Interaction):
        if not await LSX_ADMIN_CHECK(interaction):
            return
        await interaction.channel.send(view=LSXPremiumTicketPanel())
        await interaction.response.send_message("✅ Painel premium publicado.", ephemeral=True)

    for name in ("sensi-config", "publicar-sensi", "gerar-sensi", "listar-paineis"):
        try:
            bot.tree.remove_command(name)
        except Exception:
            pass

    @bot.tree.command(name="sensi-config", description="Configura o painel e a validação do Gerador de Sensi")
    @app_commands.describe(plataforma="Rede exigida", link="Link oficial", cargo="Cargo liberado", canal_validacao="Canal privado da staff", banner="URL do banner", cooldown="0 = ilimitado")
    @app_commands.choices(plataforma=[
        app_commands.Choice(name="TikTok", value="tiktok"),
        app_commands.Choice(name="Instagram", value="instagram"),
        app_commands.Choice(name="Discord", value="discord"),
    ])
    async def sensi_config(interaction: discord.Interaction, plataforma: app_commands.Choice[str], link: Optional[str]=None, cargo: Optional[discord.Role]=None, canal_validacao: Optional[discord.TextChannel]=None, banner: Optional[str]=None, cooldown: int=0):
        if not await LSX_ADMIN_CHECK(interaction):
            return
        old = lsx_get_cfg(interaction.guild.id)
        if cargo is None and old and old["access_role_id"]:
            cargo = interaction.guild.get_role(int(old["access_role_id"]))
        if cargo is None:
            cargo = discord.utils.get(interaction.guild.roles, name="Sensi Liberada") or await interaction.guild.create_role(name="Sensi Liberada", reason="Acesso ao Gerador Lock Sensi")
        if canal_validacao is None and old and old["proof_channel_id"]:
            canal_validacao = interaction.guild.get_channel(int(old["proof_channel_id"]))
        canal_validacao = canal_validacao or interaction.channel
        if link is None and old:
            link = old["verification_url"] or ""
        if banner is None and old:
            banner = old["banner_url"] or ""
        lsx_save_cfg(interaction.guild.id, cargo.id, canal_validacao.id, plataforma.value, link, banner, cooldown)
        await interaction.response.send_message(
            f"✅ **Sensi Config atualizado**\nRede: **{lsx_platform_label(plataforma.value)}**\nCargo: {cargo.mention}\nCanal de aprovação: {canal_validacao.mention}\nGerações: **{'ilimitadas' if cooldown <= 0 else f'1 a cada {cooldown}s'}**\n\nA validação usa comprovante + aprovação da staff; o cargo é entregue automaticamente após aprovar.",
            ephemeral=True,
        )

    @bot.tree.command(name="publicar-sensi", description="Publica o painel premium do Gerador de Sensi")
    async def publicar_sensi(interaction: discord.Interaction, canal: Optional[discord.TextChannel]=None):
        if not await LSX_ADMIN_CHECK(interaction):
            return
        if not lsx_get_cfg(interaction.guild.id):
            await interaction.response.send_message("❌ Primeiro use `/sensi-config`.", ephemeral=True)
            return
        target = canal or interaction.channel
        await target.send(view=LSXSensiPanel(interaction.guild.id))
        await interaction.response.send_message(f"✅ Painel publicado em {target.mention}.", ephemeral=True)

    @bot.tree.command(
        name="listar-paineis",
        description="Lista os painéis de vendas deste servidor com seus IDs",
    )
    async def listar_paineis(interaction: discord.Interaction):
        if not await LSX_ADMIN_CHECK(interaction):
            return

        con = db()
        try:
            paineis = con.execute(
                """
                SELECT id, name, title, channel_id, message_id, display_mode
                FROM panels
                WHERE guild_id=?
                ORDER BY id ASC
                """,
                (int(interaction.guild.id),),
            ).fetchall()
        finally:
            con.close()

        if not paineis:
            await interaction.response.send_message(
                "📭 Nenhum painel de vendas foi encontrado neste servidor.",
                ephemeral=True,
            )
            return

        linhas = []
        for p in paineis:
            painel_id = int(p["id"])
            nome = str(p["name"] or p["title"] or f"Painel {painel_id}")
            titulo = str(p["title"] or "").strip()
            canal_id = p["channel_id"]
            modo = str(p["display_mode"] or "lista")

            canal_txt = f"<#{int(canal_id)}>" if canal_id else "sem canal salvo"

            extra = ""
            if titulo and titulo.lower() != nome.lower():
                extra = f" • **{titulo[:70]}**"

            linhas.append(
                f"**#{painel_id}** — {nome[:80]}{extra}\n"
                f"-# Canal: {canal_txt} • modo: {modo}"
            )

        # Divide para nunca estourar o limite do Discord.
        blocos = []
        atual = ""
        for linha in linhas:
            candidato = f"{atual}\n\n{linha}".strip()
            if len(candidato) > 3500:
                blocos.append(atual)
                atual = linha
            else:
                atual = candidato
        if atual:
            blocos.append(atual)

        await interaction.response.defer(ephemeral=True)

        total = len(paineis)
        for i, bloco in enumerate(blocos, start=1):
            embed = discord.Embed(
                title="📋 PAINÉIS LOCK SENSI",
                description=bloco,
                color=LSX_BLACK,
            )
            embed.set_footer(
                text=f"{total} painel(is) • Página {i}/{len(blocos)}"
            )
            await interaction.followup.send(embed=embed, ephemeral=True)

    @bot.tree.command(name="gerar-sensi", description="Gera uma sensibilidade personalizada Lock Sensi")
    async def gerar_sensi(interaction: discord.Interaction):
        if not await lsx_has_access(interaction):
            return
        await interaction.response.send_message("🎯 Escolha sua plataforma:", view=LSXPlatformView(), ephemeral=True)


# Registra todas as rotas Flask antes de iniciar o servidor web.
# Isso evita:
# "The setup method 'route' can no longer be called..."
if not getattr(app, "_verification_routes", False):
    verification_system.register_routes(app)
    app._verification_routes = True

if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN não foi carregado no Render.")

print("[STARTUP] DISCORD_TOKEN carregado: SIM", flush=True)
keep_alive()
print("[STARTUP] Conectando ao Discord com discord.py...", flush=True)

try:
    bot.run(TOKEN)
except discord.LoginFailure:
    print(
        "[FATAL] Discord recusou o token. Revise DISCORD_TOKEN no Render.",
        flush=True,
    )
    raise
except discord.PrivilegedIntentsRequired:
    print(
        "[FATAL] Privileged Intents desabilitadas. "
        "Ative SERVER MEMBERS INTENT e MESSAGE CONTENT INTENT no Developer Portal.",
        flush=True,
    )
    raise
except discord.HTTPException as exc:
    print(
        "[FATAL] Erro HTTP do Discord:",
        getattr(exc, "status", "?"),
        str(exc),
        flush=True,
    )
    raise
except Exception as exc:
    print(
        "[FATAL] Bot Discord encerrou:",
        type(exc).__name__,
        str(exc),
        flush=True,
    )
    traceback.print_exc()
    raise
