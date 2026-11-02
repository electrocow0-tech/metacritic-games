-- Базовая схема сервиса. Оценка NULL означает «нет оценки» (на Metacritic tbd), а не ноль.
CREATE TABLE games (
    id                serial PRIMARY KEY,
    slug              text NOT NULL UNIQUE,
    title             text NOT NULL,
    release_date      date,
    description       text,
    genres            text[] NOT NULL DEFAULT '{}',
    developer         text[] NOT NULL DEFAULT '{}',
    publisher         text[] NOT NULL DEFAULT '{}',
    rating            text,
    cover_url         text,
    video_title       text,
    video_url         text,
    video_duration    int,
    metascore         int,
    critic_reviews    int,
    critic_sentiment  text,
    userscore         numeric(3, 1),
    user_reviews      int,
    user_sentiment    text,
    tagline           text,
    embedding         real[],                -- семантический эмбеддинг (опционально, если задан EMBEDDING_MODEL)
    embedding_model   text,
    embedding_hash    text,
    first_seen_at     timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now(),
    last_crawled_at   timestamptz
);
CREATE INDEX games_title_idx ON games (lower(title));
CREATE INDEX games_metascore_idx ON games (metascore DESC NULLS LAST);

CREATE TABLE game_platforms (
    game_id         int NOT NULL REFERENCES games (id) ON DELETE CASCADE,
    platform_slug   text NOT NULL,
    platform        text NOT NULL,
    metascore       int,
    critic_reviews  int,
    userscore       numeric(3, 1),
    user_reviews    int,
    PRIMARY KEY (game_id, platform_slug)
);
CREATE INDEX game_platforms_platform_idx ON game_platforms (platform);

CREATE TABLE reviews (
    id           bigserial PRIMARY KEY,
    game_id      int NOT NULL REFERENCES games (id) ON DELETE CASCADE,
    kind         text NOT NULL CHECK (kind IN ('critic', 'user')),
    ext_key      text NOT NULL,              -- хеш автора, даты и текста: идемпотентный upsert
    source       text,                       -- издание или ник игрока
    score        numeric(4, 1),
    quote        text NOT NULL,
    platform     text,
    review_date  date,
    url          text,
    fetched_at   timestamptz NOT NULL DEFAULT now(),
    UNIQUE (game_id, kind, ext_key)
);

CREATE TABLE summaries (
    game_id        int NOT NULL REFERENCES games (id) ON DELETE CASCADE,
    kind           text NOT NULL CHECK (kind IN ('critic', 'user')),
    verdict        text NOT NULL,
    likes          jsonb NOT NULL DEFAULT '[]',   -- [{text, quote, src}], quote дословно из отзыва
    dislikes       jsonb NOT NULL DEFAULT '[]',
    reviews_used   int NOT NULL,
    reviews_total  int,
    reviews_hash   text NOT NULL,                 -- пересчёт резюме только при изменении набора отзывов
    model          text NOT NULL,
    tokens_in      int NOT NULL DEFAULT 0,
    tokens_out     int NOT NULL DEFAULT 0,
    cost_usd       numeric(10, 6) NOT NULL DEFAULT 0,
    updated_at     timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (game_id, kind)
);

CREATE TABLE similar_games (
    game_id     int NOT NULL REFERENCES games (id) ON DELETE CASCADE,
    similar_id  int NOT NULL REFERENCES games (id) ON DELETE CASCADE,
    rank        int NOT NULL,
    score       real NOT NULL,
    reasons     jsonb NOT NULL DEFAULT '[]',
    PRIMARY KEY (game_id, similar_id)
);

CREATE TABLE letsplays (
    game_id          int PRIMARY KEY REFERENCES games (id) ON DELETE CASCADE,
    video_id         text NOT NULL,
    title            text NOT NULL,
    channel          text,
    views            bigint,
    duration_sec     int,
    summary          text,
    moments          jsonb NOT NULL DEFAULT '[]',   -- [{t, text}]
    transcript_chars int,
    updated_at       timestamptz NOT NULL DEFAULT now()
);

-- Состояние обхода за день: первый заход берёт New Releases, дальше курсор по страницам SEE ALL
CREATE TABLE crawl_days (
    day                date PRIMARY KEY,
    new_releases_done  boolean NOT NULL DEFAULT false,
    see_all_page       int NOT NULL DEFAULT 1,
    updated_at         timestamptz NOT NULL DEFAULT now()
);

-- Какие игры уже обработаны сегодня: признак «сегодня уже обрабатывали»
CREATE TABLE crawl_processed (
    day     date NOT NULL,
    slug    text NOT NULL,
    run_id  int,
    source  text,
    ts      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (day, slug)
);

CREATE TABLE runs (
    id             serial PRIMARY KEY,
    trigger        text NOT NULL CHECK (trigger IN ('scheduled', 'manual')),
    status         text NOT NULL CHECK (status IN ('queued', 'running', 'done', 'failed')),
    source         text,
    games_total    int NOT NULL DEFAULT 0,
    games_new      int NOT NULL DEFAULT 0,
    games_updated  int NOT NULL DEFAULT 0,
    errors         int NOT NULL DEFAULT 0,
    tokens_in      int NOT NULL DEFAULT 0,
    tokens_out     int NOT NULL DEFAULT 0,
    cost_usd       numeric(10, 6) NOT NULL DEFAULT 0,
    created_at     timestamptz NOT NULL DEFAULT now(),
    started_at     timestamptz,
    finished_at    timestamptz,
    error          text
);
-- Защита от двойного старта: одновременно может существовать только один queued/running запуск
CREATE UNIQUE INDEX runs_single_active ON runs ((true)) WHERE status IN ('queued', 'running');

CREATE TABLE run_events (
    id       bigserial PRIMARY KEY,
    run_id   int REFERENCES runs (id) ON DELETE CASCADE,
    ts       timestamptz NOT NULL DEFAULT now(),
    worker   text NOT NULL,
    stage    text,
    level    text NOT NULL DEFAULT 'info',
    slug     text,
    message  text NOT NULL,
    data     jsonb
);
CREATE INDEX run_events_run_idx ON run_events (run_id, id);

CREATE TABLE workers (
    name          text PRIMARY KEY,
    stage         text NOT NULL,
    status        text NOT NULL DEFAULT 'idle',
    job           text,
    processed     int NOT NULL DEFAULT 0,
    heartbeat_at  timestamptz NOT NULL DEFAULT now()
);
