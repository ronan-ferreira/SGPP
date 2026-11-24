import os
from urllib.parse import urlparse
import psycopg2
from psycopg2 import pool
from dotenv import load_dotenv

# Carrega variáveis do arquivo .env (na raiz do projeto)
load_dotenv()


def _montar_parametros_conexao() -> dict:
    """
    Monta os parâmetros de conexão para PostgreSQL local ou Supabase (via DATABASE_URL).
    Aplica SSL automaticamente para conexões remotas (como Supabase).
    """
    database_url = os.getenv("DATABASE_URL", "").strip()
    sslmode_env = os.getenv("DB_SSLMODE", "").strip()

    params = {
        "connect_timeout": int(os.getenv("DB_CONNECT_TIMEOUT", "10")),
        "keepalives": 1,
        "keepalives_idle": 30,
        "keepalives_interval": 10,
        "keepalives_count": 5,
    }

    if database_url:
        # Normaliza prefixo legado postgres:// -> postgresql://
        if database_url.startswith("postgres://"):
            database_url = "postgresql://" + database_url[len("postgres://"):]

        params["dsn"] = database_url

        parsed = urlparse(database_url)
        host = (parsed.hostname or "").lower()
        eh_local = host in ("localhost", "127.0.0.1", "::1")

        # Só injeta sslmode se a URL ainda não trouxer esse parâmetro
        if "sslmode=" not in database_url.lower():
            params["sslmode"] = sslmode_env or ("prefer" if eh_local else "require")
    else:
        host = os.getenv("DB_HOST", "127.0.0.1").strip()
        eh_local = host.lower() in ("localhost", "127.0.0.1", "::1")
        params.update({
            "host": host,
            "port": os.getenv("DB_PORT", "5432"),
            "dbname": os.getenv("DB_NAME", "sgpp"),
            "user": os.getenv("DB_USER", "postgres"),
            "password": os.getenv("DB_PASSWORD", "123"),
            "sslmode": sslmode_env or ("prefer" if eh_local else "require"),
        })

    return params


_DB_PARAMS = _montar_parametros_conexao()
_POOL_MIN = int(os.getenv("DB_POOL_MIN", "1"))
_POOL_MAX = int(os.getenv("DB_POOL_MAX", "10"))

# Pool de conexões thread-safe (compatível com Gunicorn/Waitress + Supabase Pooler)
conexao_pool = psycopg2.pool.ThreadedConnectionPool(
    minconn=_POOL_MIN,
    maxconn=_POOL_MAX,
    **_DB_PARAMS
)


def _conexao_ativa(conn) -> bool:
    """Verifica rapidamente se a conexão obtida do pool ainda está aberta e utilizável."""
    if conn is None or getattr(conn, "closed", 1) != 0:
        return False
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
        return True
    except Exception:
        return False


def get_cursor():
    """Retorna uma conexão saudável e cursor do pool thread-safe (com reconexão automática)."""
    ultima_excecao = None
    for _ in range(3):
        conn = conexao_pool.getconn()
        try:
            if getattr(conn, "closed", 1) != 0:
                conexao_pool.putconn(conn, close=True)
                continue
            conn.autocommit = True
            if not _conexao_ativa(conn):
                conexao_pool.putconn(conn, close=True)
                continue
            cursor = conn.cursor()
            return conn, cursor
        except Exception as e:
            ultima_excecao = e
            try:
                conexao_pool.putconn(conn, close=True)
            except Exception:
                pass

    raise RuntimeError(f"Não foi possível obter uma conexão ativa com o PostgreSQL: {ultima_excecao}")


def release_cursor(conn, cursor):
    """Fecha o cursor e devolve a conexão ao pool (descartando-a se estiver corrompida)."""
    if cursor:
        try:
            cursor.close()
        except Exception:
            pass
    if conn:
        try:
            esta_fechada = getattr(conn, "closed", 1) != 0
            conexao_pool.putconn(conn, close=esta_fechada)
        except Exception:
            pass


def testar_conexao() -> tuple[bool, str]:
    """Testa a conectividade atual com o banco de dados (usado no /health)."""
    conn = None
    cursor = None
    try:
        conn, cursor = get_cursor()
        cursor.execute("SELECT 1")
        cursor.fetchone()
        return True, "ok"
    except Exception as e:
        return False, str(e)
    finally:
        release_cursor(conn, cursor)


def _executar_migracoes_iniciais():
    """Garante que todas as tabelas, colunas e índices existam no banco automaticamente."""
    conn = None
    cur = None
    try:
        conn, cur = get_cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS usuarios (
                id SERIAL PRIMARY KEY,
                nome VARCHAR(100) NOT NULL,
                email VARCHAR(150) NOT NULL UNIQUE,
                senha VARCHAR(255) NOT NULL,
                perfil VARCHAR(20) NOT NULL DEFAULT 'professor',
                tutorial_visto BOOLEAN NOT NULL DEFAULT FALSE,
                segmento VARCHAR(50) DEFAULT 'tecnico',
                instituicao VARCHAR(150),
                logo_instituicao VARCHAR(255),
                criado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                trial_fim TIMESTAMP DEFAULT (CURRENT_TIMESTAMP + INTERVAL '7 days'),
                plano_status VARCHAR(30) DEFAULT 'trial',
                plano_tipo VARCHAR(30),
                plano_expira_em TIMESTAMP,
                stripe_customer_id VARCHAR(255),
                stripe_subscription_id VARCHAR(255)
            );

            ALTER TABLE usuarios ADD COLUMN IF NOT EXISTS segmento VARCHAR(50) DEFAULT 'tecnico';
            ALTER TABLE usuarios ADD COLUMN IF NOT EXISTS instituicao VARCHAR(150);
            ALTER TABLE usuarios ADD COLUMN IF NOT EXISTS logo_instituicao VARCHAR(255);
            ALTER TABLE usuarios ADD COLUMN IF NOT EXISTS tutorial_visto BOOLEAN DEFAULT FALSE;
            ALTER TABLE usuarios ADD COLUMN IF NOT EXISTS criado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP;
            ALTER TABLE usuarios ADD COLUMN IF NOT EXISTS trial_fim TIMESTAMP DEFAULT (CURRENT_TIMESTAMP + INTERVAL '7 days');
            ALTER TABLE usuarios ADD COLUMN IF NOT EXISTS plano_status VARCHAR(30) DEFAULT 'trial';
            ALTER TABLE usuarios ADD COLUMN IF NOT EXISTS plano_tipo VARCHAR(30);
            ALTER TABLE usuarios ADD COLUMN IF NOT EXISTS plano_expira_em TIMESTAMP;
            ALTER TABLE usuarios ADD COLUMN IF NOT EXISTS stripe_customer_id VARCHAR(255);
            ALTER TABLE usuarios ADD COLUMN IF NOT EXISTS stripe_subscription_id VARCHAR(255);

            UPDATE usuarios SET trial_fim = CURRENT_TIMESTAMP + INTERVAL '7 days' WHERE trial_fim IS NULL;
            UPDATE usuarios SET plano_status = 'trial' WHERE plano_status IS NULL;
            UPDATE usuarios SET criado_em = CURRENT_TIMESTAMP WHERE criado_em IS NULL;
            UPDATE usuarios SET perfil = 'professor' WHERE perfil != 'professor';

            CREATE INDEX IF NOT EXISTS idx_usuarios_stripe_customer ON usuarios(stripe_customer_id);
            CREATE INDEX IF NOT EXISTS idx_usuarios_stripe_subscription ON usuarios(stripe_subscription_id);

            CREATE TABLE IF NOT EXISTS stripe_eventos (
                event_id VARCHAR(255) PRIMARY KEY,
                event_type VARCHAR(100) NOT NULL,
                processado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS codigos_recuperacao (
                id SERIAL PRIMARY KEY,
                usuario_id INTEGER NOT NULL REFERENCES usuarios(id) ON DELETE CASCADE,
                codigo VARCHAR(255) NOT NULL,
                tentativas INTEGER NOT NULL DEFAULT 0,
                usado BOOLEAN NOT NULL DEFAULT FALSE,
                criado_em TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                expira_em TIMESTAMP NOT NULL
            );

            ALTER TABLE codigos_recuperacao ALTER COLUMN codigo TYPE VARCHAR(255);
            CREATE INDEX IF NOT EXISTS idx_codigos_recuperacao_user ON codigos_recuperacao(usuario_id, usado, expira_em);

            CREATE TABLE IF NOT EXISTS atividades (
                id SERIAL PRIMARY KEY,
                curso VARCHAR(100) NOT NULL,
                disciplina VARCHAR(100) NOT NULL,
                conteudo TEXT NOT NULL,
                dificuldade VARCHAR(50) NOT NULL,
                atividade_gerada TEXT,
                origem VARCHAR(100) DEFAULT 'ia',
                data_criacao TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                usuario_id INTEGER REFERENCES usuarios(id) ON DELETE SET NULL
            );

            ALTER TABLE atividades ADD COLUMN IF NOT EXISTS origem VARCHAR(100) DEFAULT 'ia';
            ALTER TABLE atividades ALTER COLUMN conteudo TYPE TEXT;
            ALTER TABLE atividades ALTER COLUMN origem TYPE VARCHAR(100);

            CREATE INDEX IF NOT EXISTS idx_atividades_curso ON atividades(curso);
            CREATE INDEX IF NOT EXISTS idx_atividades_usuario ON atividades(usuario_id);
            CREATE INDEX IF NOT EXISTS idx_atividades_data ON atividades(data_criacao DESC);

            CREATE TABLE IF NOT EXISTS cursos_plano (
                id SERIAL PRIMARY KEY,
                nome_curso VARCHAR(200) NOT NULL,
                carga_horaria INTEGER NOT NULL,
                aulas_por_dia INTEGER NOT NULL,
                usuario_id INTEGER NOT NULL REFERENCES usuarios(id) ON DELETE CASCADE,
                data_criacao TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS idx_cursos_plano_usuario ON cursos_plano(usuario_id);
            CREATE INDEX IF NOT EXISTS idx_cursos_plano_data ON cursos_plano(data_criacao DESC);

            CREATE TABLE IF NOT EXISTS planejamentos_docente (
                id SERIAL PRIMARY KEY,
                nome_curso VARCHAR(255) NOT NULL,
                unidade_curricular VARCHAR(255) NOT NULL,
                turma VARCHAR(100),
                unidade_escolar VARCHAR(255),
                carga_horaria INTEGER NOT NULL,
                horas_por_dia INTEGER NOT NULL,
                data_inicio DATE,
                data_fim DATE,
                total_aulas INTEGER NOT NULL,
                arquivo_excel VARCHAR(255),
                curso_id INTEGER REFERENCES cursos_plano(id) ON DELETE SET NULL,
                modalidade VARCHAR(100),
                usuario_id INTEGER REFERENCES usuarios(id) ON DELETE CASCADE,
                data_criacao TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            ALTER TABLE planejamentos_docente ADD COLUMN IF NOT EXISTS modalidade VARCHAR(100);
            ALTER TABLE planejamentos_docente ADD COLUMN IF NOT EXISTS curso_id INTEGER REFERENCES cursos_plano(id) ON DELETE SET NULL;

            CREATE INDEX IF NOT EXISTS idx_planejamentos_usuario ON planejamentos_docente(usuario_id);
            CREATE INDEX IF NOT EXISTS idx_planejamentos_data ON planejamentos_docente(data_criacao DESC);

            CREATE TABLE IF NOT EXISTS planos_aula (
                id SERIAL PRIMARY KEY,
                curso_id INTEGER NOT NULL REFERENCES cursos_plano(id) ON DELETE CASCADE,
                dia INTEGER NOT NULL,
                tema VARCHAR(300),
                plano_gerado TEXT NOT NULL,
                usuario_id INTEGER NOT NULL REFERENCES usuarios(id) ON DELETE CASCADE,
                planejamento_id INTEGER REFERENCES planejamentos_docente(id) ON DELETE SET NULL,
                data_criacao TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            ALTER TABLE planos_aula ADD COLUMN IF NOT EXISTS planejamento_id INTEGER REFERENCES planejamentos_docente(id) ON DELETE SET NULL;

            CREATE INDEX IF NOT EXISTS idx_planos_aula_curso ON planos_aula(curso_id);
            CREATE INDEX IF NOT EXISTS idx_planos_aula_usuario ON planos_aula(usuario_id);
            CREATE INDEX IF NOT EXISTS idx_planos_aula_dia ON planos_aula(curso_id, dia);

            CREATE TABLE IF NOT EXISTS repositorio_atividades (
                id SERIAL PRIMARY KEY,
                curso VARCHAR(150) NOT NULL,
                disciplina VARCHAR(150) NOT NULL,
                conteudo TEXT NOT NULL,
                dificuldade VARCHAR(50) NOT NULL DEFAULT 'médio',
                tipo VARCHAR(50) NOT NULL DEFAULT 'objetiva',
                enunciado TEXT NOT NULL,
                alternativas TEXT,
                resposta_correta TEXT,
                origem VARCHAR(50) NOT NULL DEFAULT 'professor',
                usuario_id INTEGER REFERENCES usuarios(id) ON DELETE SET NULL,
                data_criacao TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            ALTER TABLE repositorio_atividades ALTER COLUMN conteudo TYPE TEXT;
            ALTER TABLE repositorio_atividades ALTER COLUMN origem TYPE VARCHAR(50);
            ALTER TABLE repositorio_atividades ALTER COLUMN tipo TYPE VARCHAR(50);

            CREATE INDEX IF NOT EXISTS idx_repositorio_curso ON repositorio_atividades(curso);
            CREATE INDEX IF NOT EXISTS idx_repositorio_disciplina ON repositorio_atividades(disciplina);
            CREATE INDEX IF NOT EXISTS idx_repositorio_usuario ON repositorio_atividades(usuario_id);
            CREATE INDEX IF NOT EXISTS idx_repositorio_origem ON repositorio_atividades(origem);
            CREATE INDEX IF NOT EXISTS idx_repositorio_dificuldade ON repositorio_atividades(dificuldade);
            CREATE INDEX IF NOT EXISTS idx_repositorio_tipo ON repositorio_atividades(tipo);
        """)
    except Exception as e:
        print(f"[BD] Aviso na verificação de migrações: {e}")
    finally:
        release_cursor(conn, cur)


# Executa checagem de tabelas e colunas na inicialização
_executar_migracoes_iniciais()
