-- Entregas Automáticas • LinkRoubadão - esquema PostgreSQL/Supabase
-- Execute no SQL Editor do Supabase ou deixe AUTO_INIT_SCHEMA=true no Replit.

CREATE TABLE IF NOT EXISTS guild_config (
    guild_id BIGINT PRIMARY KEY,
    log_channel_id BIGINT, sales_channel_id BIGINT, review_channel_id BIGINT,
    support_category_id BIGINT, customer_role_id BIGINT,
    pix_key TEXT, pix_name TEXT, pix_city TEXT, webhook_url TEXT,
    mp_token TEXT, efi_client_id TEXT, efi_client_secret TEXT,
    store_name TEXT DEFAULT 'Entregas automática', color BIGINT DEFAULT 5793266,
    purchase_channel_id BIGINT, purchase_banner_url TEXT DEFAULT '',
    store_url TEXT DEFAULT '', feedback_url TEXT DEFAULT '', discord_url TEXT DEFAULT '',
    cart_category_id BIGINT, terms_url TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS products (
    id BIGSERIAL PRIMARY KEY,
    guild_id BIGINT, name TEXT, price DOUBLE PRECISION DEFAULT 0, stock INTEGER DEFAULT -1,
    description TEXT DEFAULT '', image_url TEXT DEFAULT '', banner_url TEXT DEFAULT '',
    delivery_text TEXT DEFAULT '', category TEXT DEFAULT 'Produtos', active INTEGER DEFAULT 1,
    created_at TIMESTAMPTZ DEFAULT NOW(), channel_id BIGINT, message_id BIGINT
);
CREATE INDEX IF NOT EXISTS idx_products_guild ON products(guild_id);

CREATE TABLE IF NOT EXISTS panels (
    id BIGSERIAL PRIMARY KEY,
    guild_id BIGINT, name TEXT, title TEXT, description TEXT,
    image_url TEXT DEFAULT '', banner_url TEXT DEFAULT '',
    channel_id BIGINT, message_id BIGINT, topic_id BIGINT,
    color BIGINT DEFAULT 5793266, created_at TIMESTAMPTZ DEFAULT NOW(),
    panel_type TEXT DEFAULT 'normal'
);
CREATE INDEX IF NOT EXISTS idx_panels_guild ON panels(guild_id);

CREATE TABLE IF NOT EXISTS panel_products (
    panel_id BIGINT NOT NULL,
    product_id BIGINT NOT NULL,
    UNIQUE(panel_id, product_id)
);

CREATE TABLE IF NOT EXISTS orders (
    id BIGSERIAL PRIMARY KEY,
    guild_id BIGINT, user_id BIGINT, product_id BIGINT, product_name TEXT,
    amount DOUBLE PRECISION, status TEXT DEFAULT 'pendente', code TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW(), approved_at TIMESTAMPTZ,
    rating INTEGER DEFAULT 0, review_text TEXT DEFAULT '',
    receipt_channel_id BIGINT, receipt_message_id BIGINT,
    mistic_transaction_id TEXT, mistic_state TEXT DEFAULT '',
    pix_copy_paste TEXT DEFAULT '', qr_code_url TEXT DEFAULT '',
    payer_name TEXT DEFAULT '', payer_document_last4 TEXT DEFAULT '',
    delivery_sent INTEGER DEFAULT 0, payment_checked_at TIMESTAMPTZ,
    payer_document TEXT DEFAULT '', payer_email TEXT DEFAULT '',
    transaction_id TEXT DEFAULT '', pix_code TEXT DEFAULT '', qr_url TEXT DEFAULT '',
    paid_at TIMESTAMPTZ, is_test INTEGER DEFAULT 0, delivered INTEGER DEFAULT 0,
    cart_channel_id BIGINT, updated_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_orders_guild_status ON orders(guild_id, status);
CREATE INDEX IF NOT EXISTS idx_orders_transaction ON orders(transaction_id);

CREATE TABLE IF NOT EXISTS reviews (
    id BIGSERIAL PRIMARY KEY,
    guild_id BIGINT, user_id BIGINT, stars INTEGER, text TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS guild_subscriptions (
    guild_id BIGINT PRIMARY KEY,
    active INTEGER DEFAULT 0, plan_name TEXT DEFAULT 'mensal', expires_at TIMESTAMPTZ,
    activated_by BIGINT, created_at TIMESTAMPTZ DEFAULT NOW(), updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS guild_customization (
    guild_id BIGINT PRIMARY KEY,
    store_name TEXT DEFAULT 'Entregas automática', color BIGINT DEFAULT 5793266,
    bot_nickname TEXT DEFAULT '', updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS premium_ticket_config (
    guild_id BIGINT PRIMARY KEY,
    title TEXT DEFAULT 'Central de Atendimento',
    description TEXT DEFAULT 'Escolha abaixo o setor desejado.',
    banner_url TEXT DEFAULT '', thumbnail_url TEXT DEFAULT '',
    category_name TEXT DEFAULT 'tickets-premium', color BIGINT DEFAULT 9055202,
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS guild_voice_config (
    guild_id BIGINT PRIMARY KEY, channel_id BIGINT,
    enabled INTEGER DEFAULT 1, updated_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS verification_config (
    guild_id BIGINT PRIMARY KEY,
    title TEXT DEFAULT 'Sistema de Verificação',
    description TEXT DEFAULT 'Verifique sua conta para receber seu produto grátis.',
    banner_url TEXT DEFAULT '', thumbnail_url TEXT DEFAULT '',
    button_label TEXT DEFAULT 'Verificar agora', free_delivery TEXT DEFAULT '',
    verified_role_id BIGINT, log_channel_id BIGINT, confirmation_channel_id BIGINT,
    recovery_guild_id BIGINT, recovery_guild_name TEXT DEFAULT '',
    terms_url TEXT DEFAULT '', privacy_url TEXT DEFAULT '', color BIGINT DEFAULT 8138466,
    updated_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS verification_panels (
    guild_id BIGINT, channel_id BIGINT, message_id BIGINT,
    created_at TIMESTAMPTZ,
    PRIMARY KEY(guild_id, channel_id, message_id)
);

CREATE TABLE IF NOT EXISTS verified_users (
    guild_id BIGINT, user_id BIGINT, username TEXT, global_name TEXT, avatar_url TEXT,
    access_token_enc TEXT, refresh_token_enc TEXT, token_expires_at TIMESTAMPTZ,
    recovery_guild_id BIGINT, consent_text TEXT, verified_at TIMESTAMPTZ,
    revoked_at TIMESTAMPTZ, last_join_at TIMESTAMPTZ, last_error TEXT,
    PRIMARY KEY(guild_id, user_id)
);

CREATE TABLE IF NOT EXISTS oauth_states (
    state TEXT PRIMARY KEY, guild_id BIGINT, expected_user_id BIGINT,
    recovery_guild_id BIGINT, expires_at TIMESTAMPTZ,
    used INTEGER DEFAULT 0, created_at TIMESTAMPTZ
);
