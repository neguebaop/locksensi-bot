import os, re, json, time, base64, hashlib, secrets, asyncio
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import aiohttp
import discord
from discord import app_commands
from cryptography.fernet import Fernet, InvalidToken
from flask import request, redirect, jsonify, render_template_string

DISCORD_API = 'https://discord.com/api/v10'
CLIENT_ID = os.getenv('DISCORD_CLIENT_ID', '').strip()
CLIENT_SECRET = os.getenv('DISCORD_CLIENT_SECRET', '').strip()
PUBLIC_BASE_URL = os.getenv('PUBLIC_BASE_URL', '').strip().rstrip('/')
TOKEN_KEY = os.getenv('OAUTH_TOKEN_ENCRYPTION_KEY', '').strip()

BOT = None
ADMIN_CHECK = None
APP = None

SUCCESS_HTML = '''<!doctype html><html lang="pt-BR"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Entregas Automáticas | Verificação</title><style>body{margin:0;background:#09090d;color:#fff;font-family:Arial;display:grid;place-items:center;min-height:100vh}.box{width:min(520px,90vw);background:#14141b;border:1px solid #39205f;border-radius:22px;padding:32px;text-align:center;box-shadow:0 0 50px #6f23b833}.ok{font-size:56px}.btn{display:inline-block;margin-top:18px;padding:13px 20px;background:#7c2de2;color:#fff;text-decoration:none;border-radius:10px}</style></head><body><div class="box"><div class="ok">✅</div><h1>Verificação concluída</h1><p>{{ message }}</p><p>Você já pode voltar ao Discord.</p><a class="btn" href="https://discord.com/app">Voltar ao Discord</a></div></body></html>'''
ERROR_HTML = '''<!doctype html><html lang="pt-BR"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Erro</title><style>body{margin:0;background:#09090d;color:#fff;font-family:Arial;display:grid;place-items:center;min-height:100vh}.box{width:min(520px,90vw);background:#14141b;border:1px solid #6a2531;border-radius:22px;padding:32px;text-align:center}</style></head><body><div class="box"><h1>❌ Não foi possível verificar</h1><p>{{ message }}</p></div></body></html>'''

from database import db as shared_db, ensure_schema

def db():
    # Render Free usa filesystem efêmero. Persistimos a verificação
    # no mesmo PostgreSQL/Supabase do restante do bot.
    return shared_db()

def now_iso():
    return datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')

def _fernet():
    if not TOKEN_KEY:
        return None
    key = TOKEN_KEY.encode()
    try:
        return Fernet(key)
    except Exception:
        key = base64.urlsafe_b64encode(hashlib.sha256(key).digest())
        return Fernet(key)

def encrypt(text):
    f = _fernet()
    if not f:
        raise RuntimeError('OAUTH_TOKEN_ENCRYPTION_KEY não configurada.')
    return f.encrypt(text.encode()).decode()

def decrypt(text):
    f = _fernet()
    if not f:
        raise RuntimeError('OAUTH_TOKEN_ENCRYPTION_KEY não configurada.')
    return f.decrypt(text.encode()).decode()

def init_db():
    ensure_schema()

def get_config(guild_id):
    con = db(); con.execute('INSERT OR IGNORE INTO verification_config(guild_id,updated_at) VALUES(?,?)',(guild_id,now_iso())); con.commit()
    row = con.execute('SELECT * FROM verification_config WHERE guild_id=?',(guild_id,)).fetchone(); con.close(); return row

def oauth_ready():
    return bool(CLIENT_ID and CLIENT_SECRET and PUBLIC_BASE_URL and TOKEN_KEY)

def oauth_redirect_uri():
    return f'{PUBLIC_BASE_URL}/oauth/discord/callback'

def build_auth_url(state):
    params = {
        'client_id': CLIENT_ID,
        'redirect_uri': oauth_redirect_uri(),
        'response_type': 'code',
        'scope': 'identify guilds.join',
        'state': state,
        'prompt': 'consent'
    }
    return 'https://discord.com/oauth2/authorize?' + urlencode(params)

def create_state(guild_id, user_id, recovery_guild_id):
    state = secrets.token_urlsafe(36)
    exp = (datetime.now(timezone.utc)+timedelta(minutes=10)).strftime('%Y-%m-%d %H:%M:%S')
    con=db(); con.execute('INSERT INTO oauth_states(state,guild_id,expected_user_id,recovery_guild_id,expires_at,created_at) VALUES(?,?,?,?,?,?)',(state,guild_id,user_id,recovery_guild_id,exp,now_iso())); con.commit(); con.close()
    return state

async def discord_request(method, endpoint, *, token=None, bot_token=None, data=None, json_data=None):
    headers={}
    if token: headers['Authorization']=f'Bearer {token}'
    if bot_token: headers['Authorization']=f'Bot {bot_token}'
    async with aiohttp.ClientSession() as sess:
        async with sess.request(method, DISCORD_API+endpoint, headers=headers, data=data, json=json_data) as resp:
            txt=await resp.text()
            try: body=json.loads(txt) if txt else {}
            except Exception: body={'raw':txt}
            if resp.status >= 400:
                raise RuntimeError(f'Discord API {resp.status}: {str(body)[:500]}')
            return body, resp.status

async def exchange_code(code):
    data={'client_id':CLIENT_ID,'client_secret':CLIENT_SECRET,'grant_type':'authorization_code','code':code,'redirect_uri':oauth_redirect_uri()}
    async with aiohttp.ClientSession() as sess:
        async with sess.post('https://discord.com/api/v10/oauth2/token',data=data) as resp:
            body=await resp.json(content_type=None)
            if resp.status>=400: raise RuntimeError(f'OAuth token {resp.status}: {body}')
            return body

async def refresh_token(refresh):
    data={'client_id':CLIENT_ID,'client_secret':CLIENT_SECRET,'grant_type':'refresh_token','refresh_token':refresh}
    async with aiohttp.ClientSession() as sess:
        async with sess.post('https://discord.com/api/v10/oauth2/token',data=data) as resp:
            body=await resp.json(content_type=None)
            if resp.status>=400: raise RuntimeError(f'OAuth refresh {resp.status}: {body}')
            return body

async def add_to_recovery_guild(guild_id, user_id, access_token):
    token = os.getenv('DISCORD_TOKEN','').strip()
    _, status = await discord_request('PUT',f'/guilds/{guild_id}/members/{user_id}',bot_token=token,json_data={'access_token':access_token})
    return status in (201,204)

async def finalize_verification(state, code):
    con=db(); row=con.execute('SELECT * FROM oauth_states WHERE state=?',(state,)).fetchone()
    if not row: con.close(); raise RuntimeError('Solicitação inválida ou expirada.')
    if row['used']: con.close(); raise RuntimeError('Esta solicitação já foi utilizada.')
    exp=datetime.strptime(row['expires_at'],'%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc)
    if exp < datetime.now(timezone.utc): con.close(); raise RuntimeError('O link de verificação expirou. Clique novamente no Discord.')
    con.execute('UPDATE oauth_states SET used=1 WHERE state=?',(state,)); con.commit(); con.close()
    tok=await exchange_code(code)
    access=tok['access_token']; refresh=tok.get('refresh_token',''); expires=int(tok.get('expires_in',604800))
    user,_=await discord_request('GET','/users/@me',token=access)
    uid=int(user['id'])
    if uid != int(row['expected_user_id']):
        raise RuntimeError('A conta autorizada não é a mesma que clicou em verificar.')
    cfg=get_config(int(row['guild_id']))
    recovery_id=int(row['recovery_guild_id'] or 0)
    consent=f'Autorizou identify + guilds.join exclusivamente para o servidor de recuperação {recovery_id} em {now_iso()}.'
    avatar=''
    if user.get('avatar'): avatar=f'https://cdn.discordapp.com/avatars/{uid}/{user["avatar"]}.png?size=256'
    token_exp=(datetime.now(timezone.utc)+timedelta(seconds=expires)).strftime('%Y-%m-%d %H:%M:%S')
    con=db(); con.execute('''INSERT INTO verified_users(guild_id,user_id,username,global_name,avatar_url,access_token_enc,refresh_token_enc,token_expires_at,recovery_guild_id,consent_text,verified_at,revoked_at,last_error)
    VALUES(?,?,?,?,?,?,?,?,?,?,?,NULL,'') ON CONFLICT(guild_id,user_id) DO UPDATE SET username=excluded.username,global_name=excluded.global_name,avatar_url=excluded.avatar_url,access_token_enc=excluded.access_token_enc,refresh_token_enc=excluded.refresh_token_enc,token_expires_at=excluded.token_expires_at,recovery_guild_id=excluded.recovery_guild_id,consent_text=excluded.consent_text,verified_at=excluded.verified_at,revoked_at=NULL,last_error='' ''',
    (row['guild_id'],uid,user.get('username',''),user.get('global_name') or '',avatar,encrypt(access),encrypt(refresh) if refresh else '',token_exp,recovery_id,consent,now_iso())); con.commit(); con.close()
    await after_verified(int(row['guild_id']),uid,recovery_id)
    return user

async def after_verified(source_guild_id,user_id,recovery_id):
    guild=BOT.get_guild(source_guild_id); cfg=get_config(source_guild_id)
    member=guild.get_member(user_id) if guild else None
    if guild and not member:
        try: member=await guild.fetch_member(user_id)
        except Exception: member=None
    if member and cfg['verified_role_id']:
        role=guild.get_role(int(cfg['verified_role_id']))
        if role:
            try: await member.add_roles(role,reason='Verificação OAuth autorizada pelo usuário')
            except Exception: pass
    user=member
    if not user:
        try: user=await BOT.fetch_user(user_id)
        except Exception: user=None
    if user and cfg['free_delivery']:
        e=discord.Embed(title='🎁 Seu produto grátis chegou!',description=cfg['free_delivery'][:4000],color=int(cfg['color'] or 0x7c2de2))
        if cfg['banner_url']: e.set_image(url=cfg['banner_url'])
        try: await user.send(embed=e)
        except Exception: pass
    ch=guild.get_channel(cfg['confirmation_channel_id']) if guild and cfg['confirmation_channel_id'] else None
    if ch and user:
        e=discord.Embed(title='✅ Verificação concluída',description=f'{user.mention} verificou a conta e autorizou a recuperação **somente** para o servidor configurado (`{recovery_id}`).',color=0x19c37d)
        if cfg['banner_url']: e.set_image(url=cfg['banner_url'])
        await ch.send(embed=e)
    logch=guild.get_channel(cfg['log_channel_id']) if guild and cfg['log_channel_id'] else None
    if logch and user:
        await logch.send(embed=discord.Embed(title='🔐 Nova verificação OAuth',description=f'Usuário: {user.mention}\nID: `{user_id}`\nServidor autorizado: `{recovery_id}`',color=0x7c2de2))

async def get_valid_access(row):
    access=decrypt(row['access_token_enc']); exp=datetime.strptime(row['token_expires_at'],'%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc)
    if exp > datetime.now(timezone.utc)+timedelta(minutes=2): return access
    if not row['refresh_token_enc']: raise RuntimeError('Token expirado e sem refresh token.')
    tok=await refresh_token(decrypt(row['refresh_token_enc']))
    access=tok['access_token']; refresh=tok.get('refresh_token',''); expires=int(tok.get('expires_in',604800))
    token_exp=(datetime.now(timezone.utc)+timedelta(seconds=expires)).strftime('%Y-%m-%d %H:%M:%S')
    con=db(); con.execute('UPDATE verified_users SET access_token_enc=?,refresh_token_enc=?,token_expires_at=? WHERE guild_id=? AND user_id=?',(encrypt(access),encrypt(refresh) if refresh else row['refresh_token_enc'],token_exp,row['guild_id'],row['user_id'])); con.commit(); con.close()
    return access

class VerifyPanelView(discord.ui.View):
    def __init__(self,guild_id):
        super().__init__(timeout=None); self.guild_id=int(guild_id)
        self.add_item(VerifyButton(self.guild_id))

class VerifyButton(discord.ui.Button):
    def __init__(self,guild_id):
        cfg=get_config(guild_id)
        super().__init__(label=(cfg['button_label'] or 'Verificar agora')[:80],style=discord.ButtonStyle.success,emoji='✅',custom_id=f'renegade_verify_{guild_id}')
        self.guild_id=guild_id
    async def callback(self,interaction):
        cfg=get_config(self.guild_id)
        if not oauth_ready():
            await interaction.response.send_message('❌ OAuth não configurado. O administrador precisa preencher os Secrets.',ephemeral=True); return
        recovery=int(cfg['recovery_guild_id'] or 0)
        if not recovery:
            await interaction.response.send_message('❌ Servidor de recuperação ainda não configurado.',ephemeral=True); return
        state=create_state(self.guild_id,interaction.user.id,recovery)
        url=build_auth_url(state)
        e=discord.Embed(title='🔐 Autorizar verificação',description=f'Você autorizará **identify** e **guilds.join** exclusivamente para o servidor de recuperação configurado: `{recovery}`.\n\nVocê pode remover essa autorização depois com `/verificacao revogar`.',color=int(cfg['color'] or 0x7c2de2))
        v=discord.ui.View(); v.add_item(discord.ui.Button(label='Continuar no Discord',url=url,emoji='🔗'))
        await interaction.response.send_message(embed=e,view=v,ephemeral=True)

def panel_embed(cfg):
    desc=(cfg['description'] or '')+'\n\n**Como funciona:**\n• Clique em verificar\n• Autorize no Discord\n• Receba o produto grátis no privado\n• A autorização vale somente para o servidor de recuperação informado\n• Você pode revogar quando quiser'
    e=discord.Embed(title=cfg['title'] or 'Sistema de Verificação',description=desc[:4096],color=int(cfg['color'] or 0x7c2de2))
    if cfg['thumbnail_url']: e.set_thumbnail(url=cfg['thumbnail_url'])
    if cfg['banner_url']: e.set_image(url=cfg['banner_url'])
    e.set_footer(text=f'Servidor de recuperação autorizado: {cfg["recovery_guild_id"] or "não configurado"}')
    return e

class VerificationCommands(app_commands.Group):
    def __init__(self): super().__init__(name='verificacao',description='Sistema premium de verificação e recuperação autorizada')

    @app_commands.command(name='configurar',description='Configura painel, banner, entrega e servidor autorizado')
    async def configure(self,i:discord.Interaction,titulo:str,descricao:str,servidor_recuperacao_id:str,banner_url:str='',produto_gratis:str='',cargo:discord.Role=None,canal_logs:discord.TextChannel=None,canal_confirmacao:discord.TextChannel=None,thumbnail_url:str='',texto_botao:str='Verificar agora'):
        if ADMIN_CHECK and not await ADMIN_CHECK(i): return
        try: rid=int(servidor_recuperacao_id)
        except Exception: await i.response.send_message('❌ ID do servidor inválido.',ephemeral=True); return
        rg=BOT.get_guild(rid)
        if not rg:
            await i.response.send_message('❌ O bot precisa estar dentro do servidor de recuperação antes de configurá-lo.',ephemeral=True); return
        con=db(); con.execute('''INSERT INTO verification_config(guild_id,title,description,banner_url,thumbnail_url,button_label,free_delivery,verified_role_id,log_channel_id,confirmation_channel_id,recovery_guild_id,recovery_guild_name,updated_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(guild_id) DO UPDATE SET title=excluded.title,description=excluded.description,banner_url=excluded.banner_url,thumbnail_url=excluded.thumbnail_url,button_label=excluded.button_label,free_delivery=excluded.free_delivery,verified_role_id=excluded.verified_role_id,log_channel_id=excluded.log_channel_id,confirmation_channel_id=excluded.confirmation_channel_id,recovery_guild_id=excluded.recovery_guild_id,recovery_guild_name=excluded.recovery_guild_name,updated_at=excluded.updated_at''',(i.guild.id,titulo,descricao,banner_url,thumbnail_url,texto_botao,produto_gratis,cargo.id if cargo else None,canal_logs.id if canal_logs else None,canal_confirmacao.id if canal_confirmacao else i.channel.id,rid,rg.name,now_iso())); con.commit(); con.close()
        await i.response.send_message(f'✅ Configurado para **{rg.name}** (`{rid}`).\n⚠️ Autorizações antigas para outro servidor não serão usadas; os usuários precisam verificar novamente.',ephemeral=True)

    @app_commands.command(name='publicar',description='Publica o painel de verificação no canal')
    async def publish(self,i:discord.Interaction,canal:discord.TextChannel=None):
        if ADMIN_CHECK and not await ADMIN_CHECK(i): return
        canal=canal or i.channel; cfg=get_config(i.guild.id)
        msg=await canal.send(embed=panel_embed(cfg),view=VerifyPanelView(i.guild.id))
        con=db(); con.execute('''INSERT INTO verification_panels(guild_id,channel_id,message_id,created_at) VALUES(?,?,?,?) ON CONFLICT(guild_id,channel_id,message_id) DO UPDATE SET created_at=excluded.created_at''',(i.guild.id,canal.id,msg.id,now_iso())); con.commit(); con.close()
        BOT.add_view(VerifyPanelView(i.guild.id)); await i.response.send_message(f'✅ Painel publicado em {canal.mention}.',ephemeral=True)

    @app_commands.command(name='lista',description='Mostra usuários verificados e status')
    async def listing(self,i:discord.Interaction,pagina:int=1):
        if ADMIN_CHECK and not await ADMIN_CHECK(i): return
        pagina=max(1,pagina); con=db(); total=con.execute('SELECT COUNT(*) c FROM verified_users WHERE guild_id=?',(i.guild.id,)).fetchone()['c']; rows=con.execute('SELECT * FROM verified_users WHERE guild_id=? ORDER BY verified_at DESC LIMIT 15 OFFSET ?',(i.guild.id,(pagina-1)*15)).fetchall(); con.close()
        lines=[]
        for r in rows:
            status='❌ revogado' if r['revoked_at'] else '✅ ativo'
            lines.append(f'{status} <@{r["user_id"]}> (`{r["user_id"]}`) • destino `{r["recovery_guild_id"]}` • {r["verified_at"]}')
        e=discord.Embed(title=f'👥 Verificados • {total}',description='\n'.join(lines)[:4000] or 'Nenhum usuário.',color=0x7c2de2)
        await i.response.send_message(embed=e,ephemeral=True)

    @app_commands.command(name='revogar',description='Remove sua autorização e apaga os tokens salvos')
    async def revoke(self,i:discord.Interaction):
        con=db(); row=con.execute('SELECT * FROM verified_users WHERE guild_id=? AND user_id=?',(i.guild.id,i.user.id)).fetchone()
        if not row: con.close(); await i.response.send_message('Você não possui autorização ativa neste servidor.',ephemeral=True); return
        con.execute("UPDATE verified_users SET access_token_enc='',refresh_token_enc='',revoked_at=?,last_error='' WHERE guild_id=? AND user_id=?",(now_iso(),i.guild.id,i.user.id)); con.commit(); con.close()
        await i.response.send_message('✅ Autorização removida do banco. Para invalidar também no Discord, remova o aplicativo em Configurações → Aplicativos autorizados.',ephemeral=True)

    @app_commands.command(name='recuperar',description='Adiciona autorizados somente ao servidor configurado e consentido')
    async def recover(self,i:discord.Interaction,limite:int=100):
        if ADMIN_CHECK and not await ADMIN_CHECK(i): return
        cfg=get_config(i.guild.id); rid=int(cfg['recovery_guild_id'] or 0)
        if not rid: await i.response.send_message('❌ Configure o servidor de recuperação.',ephemeral=True); return
        target=BOT.get_guild(rid)
        if not target: await i.response.send_message('❌ O bot não está no servidor de recuperação.',ephemeral=True); return
        await i.response.defer(ephemeral=True,thinking=True)
        con=db(); rows=con.execute('SELECT * FROM verified_users WHERE guild_id=? AND recovery_guild_id=? AND revoked_at IS NULL LIMIT ?',(i.guild.id,rid,max(1,min(limite,500)))).fetchall(); con.close()
        added=already=failed=0
        for r in rows:
            if target.get_member(r['user_id']): already+=1; continue
            try:
                access=await get_valid_access(r); await add_to_recovery_guild(rid,r['user_id'],access); added+=1
                con=db(); con.execute('UPDATE verified_users SET last_join_at=?,last_error='' WHERE guild_id=? AND user_id=?',(now_iso(),r['guild_id'],r['user_id'])); con.commit(); con.close()
            except Exception as e:
                failed+=1; con=db(); con.execute('UPDATE verified_users SET last_error=? WHERE guild_id=? AND user_id=?',(str(e)[:500],r['guild_id'],r['user_id'])); con.commit(); con.close()
            await asyncio.sleep(0.35)
        await i.followup.send(f'✅ Recuperação concluída para **{target.name}**.\nAdicionados: **{added}**\nJá estavam: **{already}**\nFalhas/revogados: **{failed}**',ephemeral=True)


def register_routes(app):
    @app.route('/oauth/discord/callback')
    def oauth_callback():
        error=request.args.get('error'); state=request.args.get('state',''); code=request.args.get('code','')
        if error: return render_template_string(ERROR_HTML,message='A autorização foi cancelada.'),400
        if not state or not code: return render_template_string(ERROR_HTML,message='Resposta OAuth inválida.'),400
        try:
            fut=asyncio.run_coroutine_threadsafe(finalize_verification(state,code),BOT.loop)
            user=fut.result(timeout=30)
            return render_template_string(SUCCESS_HTML,message=f'Conta {user.get("username","Discord")} verificada e produto enviado no privado.'),200
        except Exception as e:
            return render_template_string(ERROR_HTML,message=str(e)),400

    @app.route('/oauth/discord/status')
    def oauth_status():
        return jsonify({'ok':oauth_ready(),'redirect_uri':oauth_redirect_uri() if PUBLIC_BASE_URL else ''})

async def setup(bot,app,admin_check=None):
    global BOT,APP,ADMIN_CHECK
    BOT=bot; APP=app; ADMIN_CHECK=admin_check; init_db()
    try: bot.tree.add_command(VerificationCommands())
    except app_commands.CommandAlreadyRegistered: pass
    if not getattr(app,'_verification_routes',False): register_routes(app); app._verification_routes=True
    con=db(); gids=[r['guild_id'] for r in con.execute('SELECT DISTINCT guild_id FROM verification_panels').fetchall()]; con.close()
    for gid in gids:
        try: bot.add_view(VerifyPanelView(gid))
        except Exception: pass
