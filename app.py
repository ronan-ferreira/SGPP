from flask import (
    Flask,
    render_template,
    request,
    redirect,
    session,
    send_file,
    jsonify,
    flash,
    g,
    abort
)

from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.utils import secure_filename

from PyPDF2 import PdfReader

from database.conexao import get_cursor, release_cursor, testar_conexao
from utils.storage_service import (
    upload_arquivo_local,
    garantir_arquivo_local,
    deletar_arquivo,
)
from utils.email_service import enviar_email_boas_vindas
from utils.stripe_service import (
    PLANOS_STRIPE,
    dias_trial_padrao,
    stripe_configurado,
    obter_status_assinatura_usuario,
    criar_sessao_checkout,
    criar_sessao_checkout_pix,
    criar_sessao_portal_cliente,
    sincronizar_checkout_session,
    processar_webhook_stripe,
)

from ia.gerador import gerar_atividade
from utils.repositorio_service import (
    buscar_questoes_repositorio,
    contar_questoes_repositorio,
    formatar_questoes_para_atividade,
    compor_atividade_hibrida,
    salvar_questoes_ia_no_repositorio,
    adicionar_ao_repositorio,
    atualizar_no_repositorio,
    deletar_do_repositorio,
    obter_item_repositorio,
)
from ia.plano_aula import gerar_todos_planos, gerar_plano_aula, gerar_todos_planos_de_planejamento
from ia.planejamento_bloom import gerar_planejamento_ia, calcular_datas_aulas

from utils.docx_generator import gerar_docx_plano
from utils.pdf_generator import gerar_pdf_plano

from utils.docx_atividade_generator import gerar_docx_atividade
from utils.pdf_atividade_generator import gerar_pdf_atividade

from utils.excel_planejamento_generator import gerar_excel_planejamento
from utils.docx_planejamento_generator import gerar_docx_planejamento
from utils.pdf_plano_reader import estruturar_dados_plano_pdf
from utils.matriz_saep_reader import ler_matriz_saep
from utils.planejamento_docente_reader import ler_excel_planejamento_docente
from utils.imagem_utils import otimizar_imagem_upload, processar_logo_canvas_excel
from ia.planejamento_cascata import (
    gerar_planejamento_anual_ia,
    gerar_planejamento_semestral_ia,
    gerar_planejamento_bimestral_ia,
    gerar_planejamento_mensal_ia,
    gerar_planos_aula_do_mes_ia,
    gerar_planejamento_pei_semestral_ia,
    gerar_planejamento_pei_mensal_ia,
    obter_meses_por_semestre,
    obter_meses_por_bimestre,
    MESES_LETIVOS_SEMESTRE_1,
    MESES_LETIVOS_SEMESTRE_2,
    MESES_LETIVOS_POR_BIMESTRE,
    TODOS_MESES_LETIVOS,
)

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
import threading
from utils.excel_reader import extrair_dados_excel, formatar_dados_para_prompt, contar_itens
from utils.security import (
    gerar_hash_senha,
    verificar_senha,
    atualizar_senha_para_hash_se_necessario,
    redefinir_senha_usuario,
    redefinir_senha_por_email,
    solicitar_codigo_otp,
    redefinir_senha_com_otp,
    validar_forca_senha,
    avaliar_forca_senha,
    TAMANHO_MINIMO_SENHA,
)
import os
import re
import json
import traceback


IS_PRODUCTION = (
    os.environ.get("FLASK_ENV", "").strip().lower() == "production"
    or bool(os.environ.get("RENDER"))
)

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "sgpp_secret_key_2026")
app.config["TEMPLATES_AUTO_RELOAD"] = not IS_PRODUCTION
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 3600 if IS_PRODUCTION else 0
app.config["MAX_CONTENT_LENGTH"] = 25 * 1024 * 1024  # 25 MB para uploads (PDF/Excel/Logo)
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = IS_PRODUCTION
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=7)


def _obter_caminho_logo_usuario(nome_logo: str | None) -> str | None:
    """Resolve o caminho local da logo do professor, baixando do Supabase Storage se necessário."""
    if not nome_logo:
        return None
    nome_limpo = os.path.basename(nome_logo)
    caminho_local = os.path.join(app.root_path, "static", "uploads", "logos", nome_limpo)
    return garantir_arquivo_local(caminho_local, f"logos/{nome_limpo}")


def _obter_assinatura_requisicao() -> dict | None:
    """Carrega e faz cache do status de assinatura do usuário autenticado na requisição atual."""
    if "usuario_id" not in session:
        return None
    uid = session["usuario_id"]
    if getattr(g, "_assinatura_uid", None) != uid or not hasattr(g, "assinatura_atual"):
        g.assinatura_atual = obter_status_assinatura_usuario(uid)
        g._assinatura_uid = uid
    return g.assinatura_atual


@app.context_processor
def injetar_contexto_global():
    base_url = (os.environ.get("APP_BASE_URL", "").strip() or request.url_root).rstrip("/")
    return {
        "assinatura_atual": _obter_assinatura_requisicao(),
        "trial_dias_padrao": dias_trial_padrao(),
        "stripe_configurado": stripe_configurado(),
        "site_base_url": base_url,
        "google_site_verification": os.environ.get("GOOGLE_SITE_VERIFICATION", "").strip(),
    }


_ROTAS_LIVRES_ASSINATURA = (
    "/",
    "/inicio",
    "/precos",
    "/login",
    "/cadastro",
    "/logout",
    "/redefinir_senha",
    "/esqueci_senha",
    "/redefinir_senha/verificar",
    "/perfil",
    "/alterar_senha",
    "/health",
    "/robots.txt",
    "/sitemap.xml",
    "/webhook/stripe",
    "/api/validar_senha",
    "/api/concluir_tutorial",
    "/api/resetar_tutorial",
)

_PREFIXOS_LIVRES_ASSINATURA = (
    "/static/",
    "/media/",
    "/assinatura/",
)


@app.before_request
def verificar_acesso_assinatura():
    """Garante que usuários com período de teste vencido e sem assinatura ativa sejam redirecionados para /precos."""
    if "usuario_id" not in session:
        return None

    caminho = request.path or "/"
    if caminho in _ROTAS_LIVRES_ASSINATURA and not (caminho == "/" and request.method == "POST"):
        return None
    if any(caminho.startswith(p) for p in _PREFIXOS_LIVRES_ASSINATURA):
        return None

    assinatura = _obter_assinatura_requisicao()
    if assinatura and not assinatura.get("acesso_liberado", True):
        if caminho.startswith("/api/"):
            return jsonify({
                "erro": "Seu período de teste gratuito encerrou. Escolha um plano para continuar usando o SGPP.",
                "redirect": "/precos?trial_expirado=1",
            }), 402
        return redirect("/precos?trial_expirado=1")

    return None


@app.route("/health", methods=["GET"])
def health_check():
    """Endpoint de verificação de saúde para o Render e monitoramento."""
    db_ok, detalhe = testar_conexao()
    status_code = 200 if db_ok else 503
    return jsonify({
        "status": "ok" if db_ok else "degraded",
        "database": "connected" if db_ok else detalhe,
        "environment": "production" if IS_PRODUCTION else "development",
    }), status_code


@app.route("/robots.txt", methods=["GET"])
def robots_txt():
    """Instruções para mecanismos de busca (Google, Bing, etc.)."""
    base_url = (os.environ.get("APP_BASE_URL", "").strip() or request.url_root).rstrip("/")
    conteudo = (
        "User-agent: *\n"
        "Allow: /\n"
        "Allow: /precos\n"
        "Allow: /cadastro\n"
        "Allow: /login\n"
        "Disallow: /dashboard\n"
        "Disallow: /perfil\n"
        "Disallow: /atividades\n"
        "Disallow: /nova_atividade\n"
        "Disallow: /repositorio\n"
        "Disallow: /planejamento_docente\n"
        "Disallow: /planejamento/\n"
        "Disallow: /listar_planejamentos\n"
        "Disallow: /listar_cursos\n"
        "Disallow: /planos\n"
        "Disallow: /api/\n"
        "Disallow: /assinatura/\n"
        "Disallow: /webhook/\n"
        "Disallow: /media/\n"
        f"\nSitemap: {base_url}/sitemap.xml\n"
    )
    return app.response_class(conteudo, mimetype="text/plain; charset=utf-8")


@app.route("/sitemap.xml", methods=["GET"])
def sitemap_xml():
    """Sitemap XML oficial das páginas públicas do SGPP para indexação no Google."""
    base_url = (os.environ.get("APP_BASE_URL", "").strip() or request.url_root).rstrip("/")
    hoje = datetime.now().strftime("%Y-%m-%d")
    urls = [
        (f"{base_url}/", "weekly", "1.0"),
        (f"{base_url}/precos", "weekly", "0.9"),
        (f"{base_url}/cadastro", "monthly", "0.8"),
        (f"{base_url}/login", "monthly", "0.5"),
    ]
    itens = []
    for loc, freq, prio in urls:
        itens.append(
            f"  <url>\n"
            f"    <loc>{loc}</loc>\n"
            f"    <lastmod>{hoje}</lastmod>\n"
            f"    <changefreq>{freq}</changefreq>\n"
            f"    <priority>{prio}</priority>\n"
            f"  </url>"
        )
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        + "\n".join(itens)
        + "\n</urlset>\n"
    )
    return app.response_class(xml, mimetype="application/xml; charset=utf-8")


@app.route("/media/logo/<path:filename>")
def servir_logo_instituicao(filename):
    """Serve a logomarca institucional com recuperação automática do Supabase Storage."""
    nome_seguro = secure_filename(os.path.basename(filename))
    if not nome_seguro:
        abort(404)
    caminho = _obter_caminho_logo_usuario(nome_seguro)
    if not caminho or not os.path.exists(caminho):
        abort(404)
    return send_file(caminho, max_age=3600)


def pode_acessar_atividade(id, usuario_id, perfil=None):

    conn, cursor = get_cursor()
    try:
        cursor.execute(
            """
            SELECT usuario_id
            FROM atividades
            WHERE id = %s
        """,
            (id,),
        )

        atividade = cursor.fetchone()

        if not atividade:
            return False

        return atividade[0] == usuario_id
    finally:
        release_cursor(conn, cursor)


def _buscar_plano_usuario(plano_id, usuario_id, incluir_curso=False):
    """Busca um plano de aula garantindo que pertença ao usuário autenticado."""
    colunas_curso = ", cp.nome_curso, cp.carga_horaria" if incluir_curso else ""
    conn, cursor = get_cursor()
    try:
        cursor.execute(
            f"""
            SELECT pa.*{colunas_curso}
            FROM planos_aula pa
            LEFT JOIN cursos_plano cp ON cp.id = pa.curso_id
            WHERE pa.id = %s AND COALESCE(pa.usuario_id, cp.usuario_id) = %s
            """,
            (plano_id, usuario_id),
        )
        return cursor.fetchone()
    finally:
        release_cursor(conn, cursor)


def _buscar_curso_com_planos_usuario(curso_id, usuario_id):
    """Busca um curso e seus planos apenas se o curso pertencer ao usuário autenticado."""
    conn, cursor = get_cursor()
    try:
        cursor.execute(
            "SELECT * FROM cursos_plano WHERE id = %s AND usuario_id = %s",
            (curso_id, usuario_id),
        )
        curso = cursor.fetchone()
        if not curso:
            return None, []

        cursor.execute(
            "SELECT * FROM planos_aula WHERE curso_id = %s ORDER BY dia",
            (curso_id,),
        )
        planos = cursor.fetchall()
        return curso, planos
    finally:
        release_cursor(conn, cursor)


def _obter_identidade_institucional_sessao(instituicao_fallback=""):
    """Obtém caminho da logo e nome da instituição do docente logado."""
    logo_path = _obter_caminho_logo_usuario(session.get("logo_instituicao")) if session else None
    instituicao = (session.get("instituicao") or "").strip() if session else ""
    if not instituicao and instituicao_fallback:
        instituicao = str(instituicao_fallback).strip()
    return logo_path, instituicao


def _gerar_documentos_plano(
    arquivo_docx,
    arquivo_pdf,
    titulo,
    subtitulo,
    conteudo,
    gerar_pdf=True,
    instituicao_fallback="",
):
    """Gera DOCX e opcionalmente PDF de planos/planejamentos com identidade institucional."""
    os.makedirs(os.path.dirname(arquivo_docx) or "outputs", exist_ok=True)
    logo_path, instituicao = _obter_identidade_institucional_sessao(instituicao_fallback)

    gerar_docx_plano(
        titulo=titulo,
        subtitulo=subtitulo,
        conteudo=conteudo,
        caminho_saida=arquivo_docx,
        logo_path=logo_path,
        instituicao=instituicao,
    )

    if not gerar_pdf or not arquivo_pdf:
        return True

    resultado = gerar_pdf_plano(
        arquivo_docx,
        arquivo_pdf,
        titulo=titulo,
        subtitulo=subtitulo,
        conteudo=conteudo,
        logo_path=logo_path,
        instituicao=instituicao,
    )

    if not resultado or not os.path.exists(arquivo_pdf):
        from utils.pdf_generator import gerar_pdf_plano_reportlab
        gerar_pdf_plano_reportlab(
            docx_path=arquivo_docx,
            pdf_path=arquivo_pdf,
            titulo=titulo,
            subtitulo=subtitulo,
            conteudo=conteudo,
            logo_path=logo_path,
            instituicao=instituicao,
        )

    return os.path.exists(arquivo_pdf)


# =========================
# LOGIN & CADASTRO
# =========================
@app.route("/api/validar_senha", methods=["POST"])
def api_validar_senha():
    """Endpoint para avaliação em tempo real dos requisitos e força da senha."""
    dados = request.get_json(silent=True) or {}
    senha = dados.get("senha") if "senha" in dados else request.form.get("senha", "")
    return jsonify(avaliar_forca_senha(senha or ""))


@app.route("/cadastro", methods=["GET", "POST"])
def cadastro():

    if request.method == "POST":

        nome = request.form.get("nome", "").strip()
        email = request.form.get("email", "").strip().lower()
        segmento = request.form.get("segmento", "tecnico").strip()
        instituicao = request.form.get("instituicao", "").strip()
        senha = request.form.get("senha", "")
        confirmar_senha = request.form.get("confirmar_senha")

        if not nome or not email or not senha:
            return render_template(
                "cadastro.html",
                erro="Preencha todos os campos obrigatórios.",
                nome=nome,
                email=email,
                instituicao=instituicao,
            )

        if confirmar_senha is not None and senha != confirmar_senha:
            return render_template(
                "cadastro.html",
                erro="A confirmação de senha não confere com a senha digitada.",
                nome=nome,
                email=email,
                instituicao=instituicao,
            )

        senha_valida, msg_senha = validar_forca_senha(senha)
        if not senha_valida:
            return render_template(
                "cadastro.html",
                erro=msg_senha,
                nome=nome,
                email=email,
                instituicao=instituicao,
            )

        conn, cursor = get_cursor()
        try:
            # Verifica se o e-mail já está cadastrado
            cursor.execute("SELECT id FROM usuarios WHERE email=%s", (email,))
            if cursor.fetchone():
                return render_template(
                    "cadastro.html",
                    erro=f"O e-mail '{email}' já está cadastrado. Faça login ou use outro e-mail.",
                    nome=nome,
                    email=email,
                    instituicao=instituicao,
                )

            senha_hash = gerar_hash_senha(senha)
            trial_fim = datetime.now() + timedelta(days=dias_trial_padrao())

            cursor.execute(
                """
                INSERT INTO usuarios (
                    nome,
                    email,
                    senha,
                    perfil,
                    tutorial_visto,
                    segmento,
                    instituicao,
                    trial_fim,
                    plano_status
                )
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                RETURNING id
                """,
                (nome, email, senha_hash, "professor", False, segmento, instituicao or None, trial_fim, "trial"),
            )

            novo_usuario_id = cursor.fetchone()[0]
            conn.commit()

            # Envia e-mail de boas-vindas em background (sem atrasar o cadastro)
            app_url_atual = os.environ.get("APP_BASE_URL", "").strip() or request.url_root.rstrip("/")
            threading.Thread(
                target=enviar_email_boas_vindas,
                args=(email, nome, dias_trial_padrao(), app_url_atual),
                daemon=True,
            ).start()

            # Autentica e inicia a sessão automaticamente para exibir o tutorial no primeiro acesso
            session.permanent = True
            session["usuario_id"] = novo_usuario_id
            session["nome"] = nome
            session["email"] = email
            session["perfil"] = "professor"
            session["segmento"] = segmento
            session["instituicao"] = instituicao
            session["logo_instituicao"] = ""
            session["mostrar_tutorial"] = True

            return redirect("/dashboard?tutorial=1")
        except Exception as e:
            try:
                conn.rollback()
            except Exception:
                pass
            print(f"[CADASTRO] Erro ao cadastrar usuário: {e}")
            return render_template(
                "cadastro.html",
                erro="Ocorreu um erro ao cadastrar. Tente novamente.",
                nome=nome,
                email=email,
            )
        finally:
            release_cursor(conn, cursor)

    return render_template("cadastro.html")


@app.route("/login", methods=["GET", "POST"])
def login():

    if request.method == "POST":

        email = request.form["email"].strip().lower()
        senha = request.form["senha"]

        conn, cursor = get_cursor()
        try:
            cursor.execute(
                """
                SELECT id, nome, email, perfil, senha, COALESCE(tutorial_visto, FALSE),
                       COALESCE(segmento, 'tecnico'), instituicao, logo_instituicao
                FROM usuarios
                WHERE email=%s
                """,
                (email,),
            )

            usuario = cursor.fetchone()
        finally:
            release_cursor(conn, cursor)

        if usuario and verificar_senha(senha, usuario[4]):
            # Migra transparentemente se a senha ainda estava em texto puro
            atualizar_senha_para_hash_se_necessario(usuario[0], senha, usuario[4])

            session.permanent = True
            session["usuario_id"] = usuario[0]
            session["nome"] = usuario[1]
            session["email"] = usuario[2]
            session["perfil"] = "professor"
            session["segmento"] = usuario[6]
            session["instituicao"] = usuario[7] or ""
            session["logo_instituicao"] = usuario[8] or ""

            # Se for o primeiro acesso ou tutorial ainda não foi visto
            if not usuario[5]:
                session["mostrar_tutorial"] = True
                return redirect("/dashboard?tutorial=1")

            return redirect("/dashboard")

        return render_template("login.html", erro="Email ou senha incorretos.")

    return render_template("login.html")


@app.route("/logout")
def logout():

    session.clear()

    return redirect("/login")


# ============================================================
# PERFIL DOCENTE E IDENTIDADE INSTITUCIONAL (FASE 1)
# ============================================================
@app.route("/perfil", methods=["GET", "POST"])
def perfil():
    if "usuario_id" not in session:
        return redirect("/login")

    usuario_id = session["usuario_id"]
    conn, cursor = get_cursor()

    try:
        if request.method == "POST":
            nome = request.form.get("nome", "").strip()
            segmento = request.form.get("segmento", "tecnico").strip()
            instituicao = request.form.get("instituicao", "").strip()
            remover_logo = request.form.get("remover_logo") == "1"

            if not nome:
                cursor.execute("SELECT id, nome, email, segmento, instituicao, logo_instituicao FROM usuarios WHERE id=%s", (usuario_id,))
                row = cursor.fetchone()
                usr = {"id": row[0], "nome": row[1], "email": row[2], "segmento": row[3], "instituicao": row[4], "logo_instituicao": row[5]}
                return render_template("perfil.html", usuario=usr, erro="O nome não pode ficar em branco.")

            novo_nome_logo = None
            logo_anterior = session.get("logo_instituicao") or ""

            # Processamento do upload do logo da instituição (disco local + Supabase Storage)
            if "logo" in request.files and request.files["logo"].filename:
                arquivo_logo = request.files["logo"]
                extensao = os.path.splitext(arquivo_logo.filename)[1].lower()

                if extensao in [".png", ".jpg", ".jpeg", ".webp"]:
                    nome_seguro = f"logo_user_{usuario_id}_{int(datetime.now().timestamp() * 1000)}{extensao}"
                    pasta_logos = os.path.join(app.root_path, "static", "uploads", "logos")
                    os.makedirs(pasta_logos, exist_ok=True)
                    caminho_salvar = os.path.join(pasta_logos, nome_seguro)
                    arquivo_logo.save(caminho_salvar)
                    otimizar_imagem_upload(caminho_salvar)
                    upload_arquivo_local(
                        caminho_salvar,
                        f"logos/{nome_seguro}",
                        content_type=arquivo_logo.mimetype,
                    )
                    novo_nome_logo = nome_seguro

            if novo_nome_logo:
                cursor.execute(
                    """
                    UPDATE usuarios
                    SET nome = %s, segmento = %s, instituicao = %s, logo_instituicao = %s
                    WHERE id = %s
                    """,
                    (nome, segmento, instituicao or None, novo_nome_logo, usuario_id),
                )
                if logo_anterior and logo_anterior != novo_nome_logo:
                    deletar_arquivo(f"logos/{os.path.basename(logo_anterior)}")
                session["logo_instituicao"] = novo_nome_logo
            elif remover_logo:
                cursor.execute(
                    """
                    UPDATE usuarios
                    SET nome = %s, segmento = %s, instituicao = %s, logo_instituicao = NULL
                    WHERE id = %s
                    """,
                    (nome, segmento, instituicao or None, usuario_id),
                )
                if logo_anterior:
                    deletar_arquivo(f"logos/{os.path.basename(logo_anterior)}")
                    caminho_antigo = os.path.join(app.root_path, "static", "uploads", "logos", os.path.basename(logo_anterior))
                    if os.path.exists(caminho_antigo):
                        try:
                            os.remove(caminho_antigo)
                        except OSError:
                            pass
                session["logo_instituicao"] = ""
            else:
                cursor.execute(
                    """
                    UPDATE usuarios
                    SET nome = %s, segmento = %s, instituicao = %s
                    WHERE id = %s
                    """,
                    (nome, segmento, instituicao or None, usuario_id),
                )

            conn.commit()

            session["nome"] = nome
            session["segmento"] = segmento
            session["instituicao"] = instituicao

            cursor.execute("SELECT id, nome, email, segmento, instituicao, logo_instituicao FROM usuarios WHERE id=%s", (usuario_id,))
            row = cursor.fetchone()
            usr = {"id": row[0], "nome": row[1], "email": row[2], "segmento": row[3], "instituicao": row[4], "logo_instituicao": row[5]}
            return render_template("perfil.html", usuario=usr, sucesso="Perfil e identidade institucional atualizados com sucesso!")

        # GET
        cursor.execute("SELECT id, nome, email, segmento, instituicao, logo_instituicao FROM usuarios WHERE id=%s", (usuario_id,))
        row = cursor.fetchone()
        usr = {"id": row[0], "nome": row[1], "email": row[2], "segmento": row[3], "instituicao": row[4], "logo_instituicao": row[5]}
        return render_template("perfil.html", usuario=usr)

    finally:
        release_cursor(conn, cursor)



# ============================================================
# REDEFINIÇÃO DE SENHA COM OTP — ETAPA 1: solicitar código
# ============================================================
@app.route("/redefinir_senha", methods=["GET", "POST"])
@app.route("/esqueci_senha", methods=["GET", "POST"])
def redefinir_senha():
    """Etapa 1: o usuário informa apenas o e-mail e recebe o código OTP."""
    if "usuario_id" in session:
        return redirect("/alterar_senha")

    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        sucesso, mensagem, info = solicitar_codigo_otp(email)

        if not sucesso:
            return render_template("redefinir_senha.html", erro=mensagem, email=email)

        # Código enviado (ou simulado em dev): avança para etapa 2
        return render_template(
            "redefinir_senha.html",
            codigo_enviado=True,
            info_msg=mensagem,
            email=info.get("email", email),
            dev_mode=info.get("dev_mode", False),
            codigo_dev=info.get("codigo_dev"),
        )

    return render_template("redefinir_senha.html", email=request.args.get("email", ""))


# ============================================================
# REDEFINIÇÃO DE SENHA COM OTP — ETAPA 2: validar e redefinir
# ============================================================
@app.route("/redefinir_senha/verificar", methods=["POST"])
def redefinir_senha_verificar():
    """Etapa 2: valida o código OTP e define a nova senha."""
    if "usuario_id" in session:
        return redirect("/alterar_senha")

    email = request.form.get("email", "").strip().lower()
    codigo = request.form.get("codigo_otp", "").strip()
    nova_senha = request.form.get("nova_senha", "")
    confirmar_senha = request.form.get("confirmar_senha", "")

    sucesso, mensagem = redefinir_senha_com_otp(email, codigo, nova_senha, confirmar_senha)

    if not sucesso:
        return render_template(
            "redefinir_senha.html",
            codigo_enviado=True,
            erro=mensagem,
            email=email,
        )

    return redirect("/login?sucesso=senha_redefinida")


# ============================================================
# ALTERAR SENHA (USUÁRIO LOGADO)
# ============================================================
@app.route("/alterar_senha", methods=["GET", "POST"])
def alterar_senha():
    if "usuario_id" not in session:
        return redirect("/login")

    usuario_id = session["usuario_id"]

    if request.method == "POST":
        senha_atual = request.form.get("senha_atual", "")
        nova_senha = request.form.get("nova_senha", "")
        confirmar_senha = request.form.get("confirmar_senha", "")

        if not senha_atual or not nova_senha or not confirmar_senha:
            return render_template("alterar_senha.html", erro="Preencha todos os campos obrigatórios.")

        senha_valida, msg_senha = validar_forca_senha(nova_senha)
        if not senha_valida:
            return render_template("alterar_senha.html", erro=msg_senha)

        if nova_senha != confirmar_senha:
            return render_template("alterar_senha.html", erro="A nova senha e a confirmação não conferem.")

        conn, cursor = get_cursor()
        try:
            cursor.execute("SELECT senha FROM usuarios WHERE id = %s", (usuario_id,))
            res = cursor.fetchone()
            if not res or not verificar_senha(senha_atual, res[0]):
                return render_template("alterar_senha.html", erro="A senha atual informada está incorreta.")
        finally:
            release_cursor(conn, cursor)

        sucesso = redefinir_senha_usuario(usuario_id, nova_senha)
        if sucesso:
            return render_template("alterar_senha.html", sucesso="Sua senha foi alterada com sucesso!")
        else:
            return render_template("alterar_senha.html", erro="Erro ao atualizar a senha no banco de dados.")

    return render_template("alterar_senha.html")


# =========================
# LANDING PAGE (PÁGINA INICIAL DO SGPP)
# =========================
@app.route("/", methods=["GET"])
@app.route("/inicio", methods=["GET"])
def landing():
    # Sempre exibe a Landing Page ao abrir o site (mesmo logado, com botão 'Ir para o Painel')
    return render_template("landing.html")


# =========================
# PÁGINA DE PLANOS, PREÇOS E ASSINATURAS (STRIPE)
# =========================
@app.route("/precos", methods=["GET"])
def precos():
    erro_checkout = request.args.get("erro_checkout", "").strip()
    return render_template("precos.html", erro_checkout=erro_checkout)


@app.route("/assinatura/checkout/<plano>", methods=["POST"])
def assinatura_checkout(plano):
    if "usuario_id" not in session:
        return redirect(f"/login?next=/precos")

    usuario_id = session["usuario_id"]
    email = session.get("email", "")
    nome = session.get("nome", "")

    if not email:
        conn, cursor = get_cursor()
        try:
            cursor.execute("SELECT nome, email FROM usuarios WHERE id = %s", (usuario_id,))
            row = cursor.fetchone()
            if row:
                nome = nome or row[0]
                email = row[1]
                session["email"] = email
        finally:
            release_cursor(conn, cursor)

    url_checkout, erro = criar_sessao_checkout(
        usuario_id=usuario_id,
        email=email,
        nome=nome,
        plano_slug=plano,
        base_url=request.url_root.rstrip("/"),
    )
    if erro or not url_checkout:
        return render_template("precos.html", erro_checkout=erro or "Erro ao iniciar checkout.")

    return redirect(url_checkout, code=303)


@app.route("/assinatura/pix/<plano>", methods=["POST"])
def assinatura_checkout_pix(plano):
    if "usuario_id" not in session:
        return redirect("/login?next=/precos")

    usuario_id = session["usuario_id"]
    email = session.get("email", "")
    nome = session.get("nome", "")

    if not email:
        conn, cursor = get_cursor()
        try:
            cursor.execute("SELECT nome, email FROM usuarios WHERE id = %s", (usuario_id,))
            row = cursor.fetchone()
            if row:
                nome = nome or row[0]
                email = row[1]
                session["email"] = email
        finally:
            release_cursor(conn, cursor)

    url_checkout, erro = criar_sessao_checkout_pix(
        usuario_id=usuario_id,
        email=email,
        nome=nome,
        plano_slug=plano,
        base_url=request.url_root.rstrip("/"),
    )
    if erro or not url_checkout:
        return render_template("precos.html", erro_checkout=erro or "Erro ao iniciar pagamento Pix.")

    return redirect(url_checkout, code=303)


@app.route("/assinatura/sucesso", methods=["GET"])
def assinatura_sucesso():
    if "usuario_id" not in session:
        return redirect("/login")

    session_id = request.args.get("session_id", "").strip()
    info = {}
    if session_id:
        _ok, info = sincronizar_checkout_session(session_id, usuario_id_esperado=session["usuario_id"])
        # Atualiza cache da assinatura na requisição atual
        if hasattr(g, "assinatura_atual"):
            delattr(g, "assinatura_atual")

    return render_template("assinatura_sucesso.html", info=info)


@app.route("/assinatura/portal", methods=["POST"])
def assinatura_portal():
    if "usuario_id" not in session:
        return redirect("/login")

    url_portal, erro = criar_sessao_portal_cliente(
        usuario_id=session["usuario_id"],
        base_url=request.url_root.rstrip("/"),
    )
    if erro or not url_portal:
        return redirect(f"/perfil?erro_assinatura=1")

    return redirect(url_portal, code=303)


@app.route("/webhook/stripe", methods=["POST"])
def webhook_stripe():
    payload = request.get_data()
    sig_header = request.headers.get("Stripe-Signature", "")
    ok, mensagem, status_code = processar_webhook_stripe(payload, sig_header)
    return jsonify({"received": ok, "message": mensagem}), status_code


# =========================
# NOVA ATIVIDADE (GERAR ATIVIDADE - REPOSITÓRIO / IA)
# =========================
@app.route("/nova_atividade", methods=["GET", "POST"])
@app.route("/", methods=["POST"])
def index():

    if "usuario_id" not in session:
        return redirect("/login")

    if request.method == "POST":

        curso = request.form.get("curso", "").strip()
        disciplina = request.form.get("disciplina", "").strip()
        conteudo = request.form.get("conteudo", "").strip()
        dificuldade = request.form.get("dificuldade", "médio").strip()
        tipo = request.form.get("tipo", "mista").strip()
        quantidade_str = request.form.get("quantidade", "5").strip()
        fonte = request.form.get("fonte", "ia").strip()
        completar_com_ia = request.form.get("completar_com_ia", "").strip()

        try:
            quantidade = max(1, min(int(quantidade_str), 50))
        except ValueError:
            quantidade = 5

        # Se a fonte for Repositório do Professor
        if fonte == "repositorio":
            questoes_disp = buscar_questoes_repositorio(
                curso=curso,
                disciplina=disciplina,
                conteudo=conteudo,
                dificuldade=dificuldade,
                tipo=tipo,
                usuario_id=session["usuario_id"],
                limite=quantidade
            )
            qtd_disp = len(questoes_disp)

            # Se não tem o suficiente no repositório e ainda não autorizou completar com IA
            if qtd_disp < quantidade and completar_com_ia != "sim":
                # Se o usuário escolheu "Não, voltar para solicitação"
                if completar_com_ia == "nao":
                    return render_template(
                        "index.html",
                        dados_form={
                            "curso": curso,
                            "disciplina": disciplina,
                            "conteudo": conteudo,
                            "dificuldade": dificuldade,
                            "tipo": tipo,
                            "quantidade": quantidade,
                            "fonte": fonte
                        }
                    )

                # Fallback sem JavaScript ativo: renderiza o aviso com as opções Sim e Não
                return render_template(
                    "index.html",
                    confirmar_complemento=True,
                    disponiveis=qtd_disp,
                    solicitadas=quantidade,
                    faltantes=quantidade - qtd_disp,
                    dados_form={
                        "curso": curso,
                        "disciplina": disciplina,
                        "conteudo": conteudo,
                        "dificuldade": dificuldade,
                        "tipo": tipo,
                        "quantidade": quantidade,
                        "fonte": fonte
                    }
                )

            # Se atingiu a quantidade pelo repositório
            if qtd_disp >= quantidade:
                questoes_selecionadas = questoes_disp[:quantidade]
                atividade_gerada = formatar_questoes_para_atividade(questoes_selecionadas)
                origem_salva = "repositorio"
            else:
                # Completar com IA / banco externo (Usuário disse SIM!)
                faltantes = quantidade - qtd_disp
                segmento_form = request.form.get("segmento", "").strip()
                segmento_user = segmento_form or session.get("segmento", "")
                if "pei" in curso.lower() or "pei" in conteudo.lower() or "individualizado" in curso.lower():
                    segmento_user = "pei"

                texto_ia = gerar_atividade(
                    conteudo,
                    dificuldade,
                    tipo,
                    faltantes,
                    segmento=segmento_user,
                    disciplina=disciplina,
                    curso=curso
                )
                # Salva as novas questões geradas pela IA no repositório
                salvar_questoes_ia_no_repositorio(
                    texto_atividade=texto_ia,
                    curso=curso,
                    disciplina=disciplina,
                    conteudo=conteudo,
                    dificuldade=dificuldade,
                    tipo=tipo,
                    usuario_id=session["usuario_id"]
                )
                atividade_gerada = compor_atividade_hibrida(questoes_disp, texto_ia, quantidade)
                origem_salva = "misto" if qtd_disp > 0 else "ia"

        else:
            # Geração direta com IA / Banco Externo
            segmento_form = request.form.get("segmento", "").strip()
            segmento_user = segmento_form or session.get("segmento", "")
            if "pei" in curso.lower() or "pei" in conteudo.lower() or "individualizado" in curso.lower():
                segmento_user = "pei"

            atividade_gerada = gerar_atividade(
                conteudo,
                dificuldade,
                tipo,
                quantidade,
                segmento=segmento_user,
                disciplina=disciplina,
                curso=curso
            )
            salvar_questoes_ia_no_repositorio(
                texto_atividade=atividade_gerada,
                curso=curso,
                disciplina=disciplina,
                conteudo=conteudo,
                dificuldade=dificuldade,
                tipo=tipo,
                usuario_id=session["usuario_id"]
            )
            origem_salva = "ia"

        conn, cursor = get_cursor()
        try:
            cursor.execute(
                """
                INSERT INTO atividades (
                    curso,
                    disciplina,
                    conteudo,
                    dificuldade,
                    atividade_gerada,
                    usuario_id,
                    origem
                )
                VALUES (%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    curso,
                    disciplina,
                    conteudo,
                    dificuldade,
                    atividade_gerada,
                    session["usuario_id"],
                    origem_salva
                ),
            )

            conn.commit()
        finally:
            release_cursor(conn, cursor)

        return redirect("/atividades")

    return render_template("index.html")


# =========================
# API VERIFICAÇÃO REPOSITÓRIO
# =========================
@app.route("/api/repositorio/verificar", methods=["POST"])
def api_verificar_repositorio():
    if "usuario_id" not in session:
        return jsonify({"erro": "Não autorizado"}), 401

    dados = request.get_json() or {}
    curso = dados.get("curso", "").strip()
    disciplina = dados.get("disciplina", "").strip()
    conteudo = dados.get("conteudo", "").strip()
    dificuldade = dados.get("dificuldade", "").strip()
    tipo = dados.get("tipo", "").strip()
    fonte = dados.get("fonte", "repositorio").strip()

    try:
        quantidade = max(1, min(int(dados.get("quantidade", 5)), 50))
    except (ValueError, TypeError):
        quantidade = 5

    if fonte != "repositorio":
        return jsonify({
            "suficiente": True,
            "disponiveis": 0,
            "solicitadas": quantidade,
            "faltantes": 0
        })

    questoes = buscar_questoes_repositorio(
        curso=curso,
        disciplina=disciplina,
        conteudo=conteudo,
        dificuldade=dificuldade,
        tipo=tipo,
        usuario_id=session["usuario_id"],
        limite=quantidade
    )
    disponiveis = len(questoes)

    return jsonify({
        "suficiente": disponiveis >= quantidade,
        "disponiveis": disponiveis,
        "solicitadas": quantidade,
        "faltantes": max(0, quantidade - disponiveis)
    })


# =========================
# REPOSITÓRIO DE ATIVIDADES
# =========================
@app.route("/repositorio")
def listar_repositorio():
    if "usuario_id" not in session:
        return redirect("/login")

    busca = (request.args.get("busca") or "").strip()
    origem = (request.args.get("origem") or "").strip()
    tipo = (request.args.get("tipo") or "").strip()
    dificuldade = (request.args.get("dificuldade") or "").strip()

    try:
        pagina = max(1, int(request.args.get("pagina", 1)))
    except ValueError:
        pagina = 1

    por_pagina = 15
    offset = (pagina - 1) * por_pagina

    usuario_id = session["usuario_id"]

    conn, cursor = get_cursor()
    try:
        # Filtro base: somente itens do próprio usuário (repositório individual)
        condicoes = ["usuario_id = %s"]
        params = [usuario_id]

        if busca:
            condicoes.append("(enunciado ILIKE %s OR curso ILIKE %s OR disciplina ILIKE %s OR conteudo ILIKE %s)")
            termo = f"%{busca}%"
            params.extend([termo, termo, termo, termo])

        if origem:
            condicoes.append("origem = %s")
            params.append(origem)

        if tipo:
            condicoes.append("tipo = %s")
            params.append(tipo)

        if dificuldade:
            condicoes.append("dificuldade ILIKE %s")
            params.append(f"%{dificuldade}%")

        where_sql = "WHERE " + " AND ".join(condicoes)

        # Estatísticas do repositório (somente do usuário logado)
        cursor.execute("SELECT COUNT(*) FROM repositorio_atividades WHERE usuario_id = %s", (usuario_id,))
        total_itens = cursor.fetchone()[0]

        cursor.execute("SELECT COUNT(*) FROM repositorio_atividades WHERE usuario_id = %s AND origem = 'professor'", (usuario_id,))
        total_professor = cursor.fetchone()[0]

        cursor.execute("SELECT COUNT(*) FROM repositorio_atividades WHERE usuario_id = %s AND origem = 'ia'", (usuario_id,))
        total_ia = cursor.fetchone()[0]

        # Total filtrado para paginação
        cursor.execute(f"SELECT COUNT(*) FROM repositorio_atividades {where_sql}", tuple(params))
        total_filtrado = cursor.fetchone()[0]

        total_paginas = (total_filtrado + por_pagina - 1) // por_pagina

        query = f"""
            SELECT id, curso, disciplina, conteudo, dificuldade, tipo,
                   enunciado, alternativas, resposta_correta, origem, usuario_id, data_criacao
            FROM repositorio_atividades
            {where_sql}
            ORDER BY id DESC
            LIMIT %s OFFSET %s
        """
        cursor.execute(query, tuple(params + [por_pagina, offset]))
        rows = cursor.fetchall()

        questoes = []
        for r in rows:
            questoes.append({
                "id": r[0],
                "curso": r[1],
                "disciplina": r[2],
                "conteudo": r[3],
                "dificuldade": r[4],
                "tipo": r[5],
                "enunciado": r[6],
                "alternativas": r[7],
                "resposta_correta": r[8],
                "origem": r[9],
                "usuario_id": r[10],
                "data_criacao": r[11]
            })

    finally:
        release_cursor(conn, cursor)

    return render_template(
        "repositorio.html",
        questoes=questoes,
        total_itens=total_itens,
        total_professor=total_professor,
        total_ia=total_ia,
        total_paginas=total_paginas,
        pagina=pagina,
        busca=busca,
        origem=origem,
        tipo=tipo,
        dificuldade=dificuldade
    )


@app.route("/repositorio/nova", methods=["GET", "POST"])
def repositorio_nova():
    if "usuario_id" not in session:
        return redirect("/login")

    if request.method == "POST":
        curso = request.form.get("curso", "").strip()
        disciplina = request.form.get("disciplina", "").strip()
        conteudo = request.form.get("conteudo", "").strip()
        dificuldade = request.form.get("dificuldade", "médio").strip()
        tipo = request.form.get("tipo", "objetiva").strip()
        enunciado = request.form.get("enunciado", "").strip()
        alternativas = request.form.get("alternativas", "").strip()
        resposta_correta = request.form.get("resposta_correta", "").strip()

        if not curso or not disciplina or not conteudo or not enunciado:
            return render_template(
                "repositorio_form.html",
                item=request.form,
                erro="Preencha todos os campos obrigatórios."
            )

        adicionar_ao_repositorio(
            curso=curso,
            disciplina=disciplina,
            conteudo=conteudo,
            dificuldade=dificuldade,
            tipo=tipo,
            enunciado=enunciado,
            alternativas=alternativas,
            resposta_correta=resposta_correta,
            origem="professor",
            usuario_id=session["usuario_id"]
        )

        return redirect("/repositorio")

    return render_template("repositorio_form.html", item=None)


@app.route("/repositorio/editar/<int:id>", methods=["GET", "POST"])
def repositorio_editar(id):
    if "usuario_id" not in session:
        return redirect("/login")

    item = obter_item_repositorio(id)
    if not item or item.get("usuario_id") != session["usuario_id"]:
        return redirect("/repositorio")

    if request.method == "POST":
        curso = request.form.get("curso", "").strip()
        disciplina = request.form.get("disciplina", "").strip()
        conteudo = request.form.get("conteudo", "").strip()
        dificuldade = request.form.get("dificuldade", "médio").strip()
        tipo = request.form.get("tipo", "objetiva").strip()
        enunciado = request.form.get("enunciado", "").strip()
        alternativas = request.form.get("alternativas", "").strip()
        resposta_correta = request.form.get("resposta_correta", "").strip()

        atualizar_no_repositorio(
            item_id=id,
            curso=curso,
            disciplina=disciplina,
            conteudo=conteudo,
            dificuldade=dificuldade,
            tipo=tipo,
            enunciado=enunciado,
            alternativas=alternativas,
            resposta_correta=resposta_correta,
            usuario_id=session["usuario_id"]
        )

        return redirect("/repositorio")

    return render_template("repositorio_form.html", item=item)


@app.route("/repositorio/deletar/<int:id>", methods=["GET", "POST"])
def repositorio_deletar(id):
    if "usuario_id" not in session:
        return redirect("/login")

    deletar_do_repositorio(
        item_id=id,
        usuario_id=session["usuario_id"],
        perfil=session.get("perfil", "professor")
    )

    return redirect("/repositorio")


@app.route("/repositorio/visualizar/<int:id>")
def repositorio_visualizar(id):
    if "usuario_id" not in session:
        return redirect("/login")

    item = obter_item_repositorio(id)
    if not item or item.get("usuario_id") != session["usuario_id"]:
        return redirect("/repositorio")

    return render_template("visualizar_atividade_repositorio.html", item=item)


@app.route("/repositorio/pdf/<int:id>")
def repositorio_pdf(id):
    if "usuario_id" not in session:
        return redirect("/login")

    item = obter_item_repositorio(id)
    if not item or item.get("usuario_id") != session["usuario_id"]:
        return redirect("/repositorio")

    dados_cab = {
        "curso": item.get("curso") or "—",
        "unidade_curricular": item.get("disciplina") or "—",
        "aula_encontro": "____________________________",
        "nivel_bloom": (item.get("dificuldade") or "Médio").capitalize(),
        "instituicao": session.get("instituicao", ""),
        "logo_path": _obter_caminho_logo_usuario(session.get("logo_instituicao")),
    }

    enunciado = (item.get("enunciado") or "").strip()
    alternativas = (item.get("alternativas") or "").strip()
    resposta = (item.get("resposta_correta") or "").strip()

    conteudo_formatado = f"Questão 1: {enunciado}"
    if alternativas:
        conteudo_formatado += f"\n\n{alternativas}"
    if resposta:
        if not resposta.lower().startswith("resposta"):
            conteudo_formatado += f"\n\nResposta correta: {resposta}"
        else:
            conteudo_formatado += f"\n\n{resposta}"

    os.makedirs("outputs", exist_ok=True)
    arquivo_docx = f"outputs/repositorio_{id}.docx"
    arquivo_pdf = f"outputs/repositorio_{id}.pdf"

    gerar_docx_atividade(
        titulo="ATIVIDADE",
        subtitulo=dados_cab["curso"],
        conteudo=conteudo_formatado,
        caminho_saida=arquivo_docx,
        dados_cabecalho=dados_cab
    )

    resultado = gerar_pdf_atividade(
        arquivo_docx=arquivo_docx,
        arquivo_pdf=arquivo_pdf,
        titulo="ATIVIDADE",
        subtitulo=dados_cab["curso"],
        dados_cabecalho=dados_cab,
        conteudo=conteudo_formatado
    )

    if not resultado or not os.path.exists(arquivo_pdf):
        from utils.pdf_atividade_generator import gerar_pdf_atividade_reportlab
        gerar_pdf_atividade_reportlab(
            arquivo_pdf=arquivo_pdf,
            titulo="ATIVIDADE",
            subtitulo=dados_cab["curso"],
            dados_cabecalho=dados_cab,
            conteudo=conteudo_formatado,
            arquivo_docx=arquivo_docx
        )

    if os.path.exists(arquivo_pdf):
        return send_file(
            arquivo_pdf,
            as_attachment=True,
            download_name=f"atividade_repositorio_{id}.pdf",
            mimetype="application/pdf"
        )

    return "Erro ao gerar arquivo PDF da atividade do repositório.", 500


@app.route("/repositorio/word/<int:id>")
def repositorio_word(id):
    if "usuario_id" not in session:
        return redirect("/login")

    item = obter_item_repositorio(id)
    if not item or item.get("usuario_id") != session["usuario_id"]:
        return redirect("/repositorio")

    dados_cab = {
        "curso": item.get("curso") or "—",
        "unidade_curricular": item.get("disciplina") or "—",
        "aula_encontro": "____________________________",
        "nivel_bloom": (item.get("dificuldade") or "Médio").capitalize(),
        "instituicao": session.get("instituicao", ""),
        "logo_path": _obter_caminho_logo_usuario(session.get("logo_instituicao")),
    }

    enunciado = (item.get("enunciado") or "").strip()
    alternativas = (item.get("alternativas") or "").strip()
    resposta = (item.get("resposta_correta") or "").strip()

    conteudo_formatado = f"Questão 1: {enunciado}"
    if alternativas:
        conteudo_formatado += f"\n\n{alternativas}"
    if resposta:
        if not resposta.lower().startswith("resposta"):
            conteudo_formatado += f"\n\nResposta correta: {resposta}"
        else:
            conteudo_formatado += f"\n\n{resposta}"

    os.makedirs("outputs", exist_ok=True)
    arquivo_docx = f"outputs/repositorio_{id}.docx"

    gerar_docx_atividade(
        titulo="ATIVIDADE",
        subtitulo=dados_cab["curso"],
        conteudo=conteudo_formatado,
        caminho_saida=arquivo_docx,
        dados_cabecalho=dados_cab
    )

    return send_file(
        arquivo_docx,
        as_attachment=True,
        download_name=f"atividade_repositorio_{id}.docx",
        mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )


# =========================
# LISTAR + PESQUISA COM PAGINAÇÃO
# =========================
@app.route("/atividades")
def atividades():

    if "usuario_id" not in session:
        return redirect("/login")

    busca = (request.args.get("busca") or "").strip()
    try:
        pagina = max(1, int(request.args.get("pagina", 1)))
    except ValueError:
        pagina = 1

    por_pagina = 20
    offset = (pagina - 1) * por_pagina

    conn, cursor = get_cursor()
    try:
        usuario_id = session["usuario_id"]
        if busca:
            cursor.execute(
                """
                SELECT COUNT(*)
                FROM atividades
                WHERE usuario_id = %s
                  AND (
                        curso ILIKE %s
                     OR disciplina ILIKE %s
                     OR conteudo ILIKE %s
                  )
                """,
                (usuario_id, f"%{busca}%", f"%{busca}%", f"%{busca}%"),
            )
            total_itens = cursor.fetchone()[0]

            cursor.execute(
                """
                SELECT *
                FROM atividades
                WHERE usuario_id = %s
                  AND (
                        curso ILIKE %s
                     OR disciplina ILIKE %s
                     OR conteudo ILIKE %s
                  )
                ORDER BY id DESC
                LIMIT %s OFFSET %s
                """,
                (
                    usuario_id,
                    f"%{busca}%",
                    f"%{busca}%",
                    f"%{busca}%",
                    por_pagina,
                    offset
                ),
            )
        else:
            cursor.execute("SELECT COUNT(*) FROM atividades WHERE usuario_id = %s", (usuario_id,))
            total_itens = cursor.fetchone()[0]

            cursor.execute(
                """
                SELECT *
                FROM atividades
                WHERE usuario_id = %s
                ORDER BY id DESC
                LIMIT %s OFFSET %s
                """,
                (usuario_id, por_pagina, offset),
            )

        dados = cursor.fetchall()
        total_paginas = max(1, -(-total_itens // por_pagina))
    finally:
        release_cursor(conn, cursor)

    return render_template(
        "atividades.html",
        atividades=dados,
        pagina=pagina,
        total_paginas=total_paginas,
        total_itens=total_itens,
        busca=busca
    )


# =========================
# DELETE
# =========================
@app.route("/deletar/<int:id>")
def deletar(id):

    if "usuario_id" not in session:
        return redirect("/login")

    if not pode_acessar_atividade(id, session["usuario_id"], session.get("perfil")):
        return "Acesso negado"

    conn, cursor = get_cursor()
    try:
        cursor.execute(
            "DELETE FROM atividades WHERE id=%s",
            (id,)
        )

        conn.commit()
    finally:
        release_cursor(conn, cursor)

    return redirect("/atividades")


# =========================
# EDITAR
# =========================
@app.route("/editar/<int:id>", methods=["GET", "POST"])
def editar(id):

    if "usuario_id" not in session:
        return redirect("/login")

    if not pode_acessar_atividade(id, session["usuario_id"], session.get("perfil")):
        return "Acesso negado"

    if request.method == "POST":

        curso = request.form["curso"]
        disciplina = request.form["disciplina"]
        conteudo = request.form["conteudo"]
        dificuldade = request.form["dificuldade"]
        atividade_gerada = request.form["atividade_gerada"]

        conn, cursor = get_cursor()
        try:
            cursor.execute(
                """
                UPDATE atividades
                SET curso=%s,
                    disciplina=%s,
                    conteudo=%s,
                    dificuldade=%s,
                    atividade_gerada=%s
                WHERE id=%s
                """,
                (
                    curso,
                    disciplina,
                    conteudo,
                    dificuldade,
                    atividade_gerada,
                    id
                ),
            )

            conn.commit()
        finally:
            release_cursor(conn, cursor)

        return redirect("/atividades")

    conn, cursor = get_cursor()
    try:
        cursor.execute(
            "SELECT * FROM atividades WHERE id=%s",
            (id,)
        )

        atividade = cursor.fetchone()
    finally:
        release_cursor(conn, cursor)

    if not atividade:
        return "Atividade nÃ£o encontrada"

    return render_template(
        "editar.html",
        atividade=atividade
    )


# =========================
# CABEÇALHO DA ATIVIDADE
# =========================
def extrair_cabecalho_atividade(atividade):
    curso_raw = (atividade[1] or "").strip()
    disc_raw = (atividade[2] or "").strip()
    cont_raw = (atividade[3] or "").strip()
    dif_raw = (atividade[4] or "").strip()

    curso = curso_raw
    uc = disc_raw
    aula = "Encontro Regular"
    bloom = dif_raw.capitalize() if dif_raw else "Lembrar"

    if "—" in curso_raw:
        partes = [p.strip() for p in curso_raw.split("—")]
        curso = partes[0]
        if len(partes) > 1 and disc_raw in ("Plano de Aula", "", None):
            uc = partes[1]
    elif " - " in curso_raw and disc_raw in ("Plano de Aula", "", None):
        partes = [p.strip() for p in curso_raw.split(" - ")]
        curso = partes[0]
        uc = partes[1]

    m_c = re.search(r"Curso:\s*([^\n\r]+)", cont_raw, re.IGNORECASE)
    if m_c:
        curso = m_c.group(1).strip()

    m_uc = re.search(r"Unidade Curricular:\s*([^\n\r]+)", cont_raw, re.IGNORECASE)
    if m_uc:
        uc = m_uc.group(1).strip()

    m_a = re.search(r"Aula\s*/\s*Encontro:\s*([^\n\r]+)", cont_raw, re.IGNORECASE)
    if m_a:
        aula = m_a.group(1).strip()
    else:
        m_a2 = re.search(r"Aula:\s*([^\n\r]+)", cont_raw, re.IGNORECASE)
        if m_a2:
            aula = m_a2.group(1).strip()

    m_b = re.search(r"N[i\xed]vel Cognitivo(?:\s*\(Taxonomia de Bloom\))?:\s*([^\n\r]+)", cont_raw, re.IGNORECASE)
    if m_b:
        bloom = m_b.group(1).strip()

    logo_path = _obter_caminho_logo_usuario(session.get("logo_instituicao")) if session else None

    return {
        "curso": curso,
        "unidade_curricular": uc,
        "aula_encontro": aula,
        "nivel_bloom": bloom,
        "instituicao": session.get("instituicao", "") if session else "",
        "logo_path": logo_path,
    }

# =========================
# PDF
# =========================
@app.route("/pdf/<int:id>")
def gerar_pdf(id):

    if "usuario_id" not in session:
        return redirect("/login")

    if not pode_acessar_atividade(id, session["usuario_id"], session.get("perfil")):
        return "Acesso negado"

    conn, cursor = get_cursor()
    try:
        cursor.execute(
            """
            SELECT *
            FROM atividades
            WHERE id=%s
            """,
            (id,)
        )

        atividade = cursor.fetchone()
    finally:
        release_cursor(conn, cursor)

    if not atividade:
        return "Atividade nÃ£o encontrada"

    arquivo_docx = f"outputs/atividade_{id}.docx"
    arquivo_pdf = f"outputs/atividade_{id}.pdf"

    dados_cab = extrair_cabecalho_atividade(atividade)

    os.makedirs("outputs", exist_ok=True)

    gerar_docx_atividade(
        titulo="ATIVIDADE",
        subtitulo=dados_cab["curso"],
        conteudo=atividade[5],
        caminho_saida=arquivo_docx,
        dados_cabecalho=dados_cab
    )

    resultado = gerar_pdf_atividade(
        arquivo_docx=arquivo_docx,
        arquivo_pdf=arquivo_pdf,
        titulo="ATIVIDADE",
        subtitulo=dados_cab["curso"],
        dados_cabecalho=dados_cab,
        conteudo=atividade[5]
    )

    if not resultado or not os.path.exists(arquivo_pdf):
        from utils.pdf_atividade_generator import gerar_pdf_atividade_reportlab
        gerar_pdf_atividade_reportlab(
            arquivo_pdf=arquivo_pdf,
            titulo="ATIVIDADE",
            subtitulo=dados_cab["curso"],
            dados_cabecalho=dados_cab,
            conteudo=atividade[5],
            arquivo_docx=arquivo_docx
        )

    if os.path.exists(arquivo_pdf):
        return send_file(
            arquivo_pdf,
            as_attachment=True,
            download_name=f"atividade_{id}.pdf",
            mimetype="application/pdf"
        )

    return "Erro ao gerar arquivo PDF da atividade. Por favor, tente novamente.", 500


# =========================
# WORD
# =========================
@app.route("/word/<int:id>")
def gerar_word(id):

    if "usuario_id" not in session:
        return redirect("/login")

    if not pode_acessar_atividade(id, session["usuario_id"], session.get("perfil")):
        return "Acesso negado"

    conn, cursor = get_cursor()
    try:
        cursor.execute(
            """
            SELECT *
            FROM atividades
            WHERE id=%s
            """,
            (id,)
        )

        atividade = cursor.fetchone()
    finally:
        release_cursor(conn, cursor)

    if not atividade:
        return "Atividade não encontrada"

    arquivo = f"outputs/atividade_{id}.docx"

    dados_cab = extrair_cabecalho_atividade(atividade)

    gerar_docx_atividade(
        titulo="ATIVIDADE",
        subtitulo=dados_cab["curso"],
        conteudo=atividade[5],
        caminho_saida=arquivo,
        dados_cabecalho=dados_cab
    )

    return send_file(
        arquivo,
        as_attachment=True,
        download_name=f"atividade_{id}.docx",
        mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )


# =========================
# REGENERAR IA
# =========================
@app.route("/regenerar/<int:id>")
def regenerar(id):

    if "usuario_id" not in session:
        return redirect("/login")

    if not pode_acessar_atividade(id, session["usuario_id"], session.get("perfil")):
        return "Acesso negado"

    conn, cursor = get_cursor()
    try:
        cursor.execute(
            """
            SELECT curso, disciplina, conteudo, dificuldade, atividade_gerada
            FROM atividades
            WHERE id=%s
            """,
            (id,)
        )

        atividade = cursor.fetchone()
    finally:
        release_cursor(conn, cursor)

    if not atividade:
        return "Atividade não encontrada", 404

    curso, disciplina, conteudo, dificuldade, texto_anterior = atividade
    curso = curso or ""
    disciplina = disciplina or ""
    conteudo = conteudo or ""
    dificuldade = dificuldade or "médio"

    # Preserva segmento (incluindo PEI) e formato/quantidade originais
    segmento = session.get("segmento", "")
    if (
        "pei" in curso.lower()
        or "pei" in conteudo.lower()
        or "individualizado" in curso.lower()
        or "individualizado" in conteudo.lower()
    ):
        segmento = "pei"

    tipo = "mista"
    m_fmt = re.search(r"Formato:\s*([^\n\r]+)", conteudo, re.IGNORECASE)
    if m_fmt:
        fmt_txt = m_fmt.group(1).lower()
        if "objetiva" in fmt_txt:
            tipo = "objetiva"
        elif "discursiva" in fmt_txt:
            tipo = "discursiva"
        elif "prática" in fmt_txt or "pratica" in fmt_txt:
            tipo = "pratica"

    qtd_encontrada = len(re.findall(r"(?im)^(?:Quest[aã]o\s*\d+|Exerc[ií]cio\s*\d+|\d+[\.\)])", texto_anterior or ""))
    quantidade = max(1, min(qtd_encontrada, 50)) if qtd_encontrada > 0 else 5

    nova = gerar_atividade(
        conteudo,
        dificuldade,
        tipo,
        quantidade,
        segmento=segmento,
        disciplina=disciplina,
        curso=curso,
    )

    conn, cursor = get_cursor()
    try:
        cursor.execute(
            """
            UPDATE atividades
            SET atividade_gerada=%s
            WHERE id=%s
            """,
            (nova, id)
        )

        conn.commit()
    finally:
        release_cursor(conn, cursor)

    return redirect("/atividades")


# =========================
# DASHBOARD
# =========================
@app.route("/dashboard")
def dashboard():

    if "usuario_id" not in session:
        return redirect("/login")

    conn, cursor = get_cursor()
    try:
        usuario_id = session["usuario_id"]

        # Checa status do tutorial
        cursor.execute("SELECT COALESCE(tutorial_visto, FALSE) FROM usuarios WHERE id = %s", (usuario_id,))
        res_tutorial = cursor.fetchone()
        tutorial_ja_visto = res_tutorial[0] if res_tutorial else False

        cursor.execute(
            """
            SELECT
                curso,
                COUNT(*)
            FROM atividades
            WHERE usuario_id = %s
            GROUP BY curso
            ORDER BY curso
            """,
            (usuario_id,),
        )
        cursos = cursor.fetchall()

        cursor.execute("SELECT COUNT(*) FROM atividades WHERE usuario_id = %s", (usuario_id,))
        total_atividades = cursor.fetchone()[0]

        cursor.execute("SELECT COUNT(*) FROM planejamentos_docente WHERE usuario_id = %s", (usuario_id,))
        total_planejamentos = cursor.fetchone()[0]

        cursor.execute("SELECT COUNT(*) FROM planos_aula WHERE usuario_id = %s", (usuario_id,))
        total_planos = cursor.fetchone()[0]

        cursor.execute("SELECT COUNT(*) FROM cursos_plano WHERE usuario_id = %s", (usuario_id,))
        total_cursos_plano = cursor.fetchone()[0]

    finally:
        release_cursor(conn, cursor)

    # Determina se deve disparar o tutorial automaticamente
    abrir_tutorial = False
    param_tutorial = request.args.get("tutorial")
    if param_tutorial in ("1", "true", "True") or session.pop("mostrar_tutorial", False) or not tutorial_ja_visto:
        abrir_tutorial = True

    return render_template(
        "dashboard.html",
        cursos=cursos,
        total_atividades=total_atividades,
        total_planejamentos=total_planejamentos,
        total_planos=total_planos,
        total_cursos_plano=total_cursos_plano,
        abrir_tutorial=abrir_tutorial,
        tutorial_ja_visto=tutorial_ja_visto
    )


# =========================
# ROTAS DO TUTORIAL
# =========================
@app.route("/api/concluir_tutorial", methods=["POST"])
def concluir_tutorial():
    if "usuario_id" not in session:
        return jsonify({"sucesso": False, "erro": "Usuário não autenticado."}), 401

    conn, cursor = get_cursor()
    try:
        cursor.execute(
            "UPDATE usuarios SET tutorial_visto = TRUE WHERE id = %s",
            (session["usuario_id"],),
        )
        conn.commit()
    except Exception as e:
        print(f"[TUTORIAL] Erro ao concluir tutorial: {e}")
        return jsonify({"sucesso": False, "erro": str(e)}), 500
    finally:
        release_cursor(conn, cursor)

    session.pop("mostrar_tutorial", None)
    return jsonify({"sucesso": True})


@app.route("/api/resetar_tutorial", methods=["POST"])
def resetar_tutorial():
    if "usuario_id" not in session:
        return jsonify({"sucesso": False, "erro": "Usuário não autenticado."}), 401

    conn, cursor = get_cursor()
    try:
        cursor.execute(
            "UPDATE usuarios SET tutorial_visto = FALSE WHERE id = %s",
            (session["usuario_id"],),
        )
        conn.commit()
    except Exception as e:
        return jsonify({"sucesso": False, "erro": str(e)}), 500
    finally:
        release_cursor(conn, cursor)

    return jsonify({"sucesso": True})

# =========================
# PLANOS DE AULA
# =========================
@app.route("/planos", methods=["GET", "POST"])
def planos():

    if "usuario_id" not in session:
        return redirect("/login")

    if request.method == "POST":

        nome_curso = request.form.get("nome_curso", "").strip()
        carga_horaria_form = request.form.get("carga_horaria")
        aulas_por_dia_form = request.form.get("aulas_por_dia")

        carga_horaria = int(carga_horaria_form) if carga_horaria_form else 0
        aulas_por_dia = int(aulas_por_dia_form) if aulas_por_dia_form else 4

        arquivo = request.files["arquivo_pdf"]
        nome_arquivo = secure_filename(arquivo.filename)

        pasta_upload = "uploads"
        os.makedirs(pasta_upload, exist_ok=True)

        caminho = os.path.join(pasta_upload, nome_arquivo)
        arquivo.save(caminho)

        is_excel = nome_arquivo.lower().endswith((".xlsx", ".xls"))

        print("=" * 60)
        print(f"[INFO] PROCESSANDO ARQUIVO PARA GERAR PLANOS DE AULA")
        print(f"   Arquivo: {nome_arquivo} (Excel: {is_excel})")
        print("=" * 60)

        planos = []
        curso_id = None
        total_dias = 0

        # Verifica se é um arquivo Excel de Planejamento Docente
        if is_excel:
            try:
                dados_cabecalho, aulas_excel = ler_excel_planejamento_docente(caminho)
                if aulas_excel and len(aulas_excel) > 0:
                    print(f"[INFO] Arquivo reconhecido como Planejamento Docente ({len(aulas_excel)} aulas identificadas)!")
                    if not nome_curso and dados_cabecalho.get("nome_curso"):
                        nome_curso = dados_cabecalho["nome_curso"]
                    if dados_cabecalho.get("unidade_curricular"):
                        if nome_curso:
                            nome_curso = f"{nome_curso} — {dados_cabecalho['unidade_curricular']}"
                        else:
                            nome_curso = dados_cabecalho["unidade_curricular"]

                    if carga_horaria <= 0:
                        carga_horaria = dados_cabecalho.get("carga_horaria", 40)
                    if aulas_por_dia <= 0:
                        aulas_por_dia = dados_cabecalho.get("horas_por_dia", 4)

                    total_dias = len(aulas_excel)

                    # Cria o curso
                    conn, cursor = get_cursor()
                    try:
                        cursor.execute(
                            """
                            INSERT INTO cursos_plano (nome_curso, carga_horaria, aulas_por_dia, usuario_id)
                            VALUES (%s, %s, %s, %s)
                            RETURNING id
                            """,
                            (nome_curso or "Curso Planejado", carga_horaria, aulas_por_dia, session["usuario_id"])
                        )
                        curso_id = cursor.fetchone()[0]
                        conn.commit()
                    finally:
                        release_cursor(conn, cursor)

                    # Gera planos de aula a partir das aulas estruturadas no Excel do planejamento
                    planos = gerar_todos_planos_de_planejamento(
                        aulas_excel,
                        nome_curso,
                        dados_cabecalho.get("unidade_curricular", ""),
                        aulas_por_dia,
                        max_workers=2
                    )

            except Exception as e:
                print(f"Aviso ao ler como Planejamento Docente: {e}. Tentando fluxo padrão...")

        # Fluxo alternativo: Matriz SAEP ou PDF de Plano de Curso
        if not planos:
            if is_excel:
                print("[INFO] Extraindo dados de Matriz de Referência...")
                dados_excel = extrair_dados_excel(caminho)
                texto = formatar_dados_para_prompt(dados_excel)
            else:
                print("[INFO] Extraindo texto do PDF...")
                reader = PdfReader(caminho)
                texto = ""
                for pagina in reader.pages:
                    conteudo = pagina.extract_text()
                    if conteudo:
                        texto += conteudo + "\n"

            if carga_horaria <= 0:
                carga_horaria = 40
            if aulas_por_dia <= 0:
                aulas_por_dia = 4

            total_dias = max(1, carga_horaria // aulas_por_dia)

            conn, cursor = get_cursor()
            try:
                cursor.execute(
                    """
                    INSERT INTO cursos_plano (nome_curso, carga_horaria, aulas_por_dia, usuario_id)
                    VALUES (%s, %s, %s, %s)
                    RETURNING id
                    """,
                    (nome_curso or "Curso Sem Nome", carga_horaria, aulas_por_dia, session["usuario_id"])
                )
                curso_id = cursor.fetchone()[0]
                conn.commit()
            finally:
                release_cursor(conn, cursor)

            planos = gerar_todos_planos(texto, carga_horaria, aulas_por_dia, total_dias, max_workers=2)

        # Salva os planos no banco
        planos_completos = ""
        conn, cursor = get_cursor()
        try:
            for dia, plano in enumerate(planos, start=1):
                planos_completos += f"\n\nDIA {dia}\n\n{plano}"
                cursor.execute(
                    """
                    INSERT INTO planos_aula (curso_id, dia, tema, plano_gerado, usuario_id)
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    (curso_id, dia, f"Plano Dia {dia}", plano, session["usuario_id"])
                )
            conn.commit()
        finally:
            release_cursor(conn, cursor)

        # Gera o DOCX consolidado do curso
        os.makedirs("outputs", exist_ok=True)
        arquivo_docx = f"outputs/curso_{curso_id}.docx"
        arquivo_pdf = f"outputs/curso_{curso_id}.pdf"

        _gerar_documentos_plano(
            arquivo_docx=arquivo_docx,
            arquivo_pdf=arquivo_pdf,
            titulo="PLANO DE AULA",
            subtitulo=f"Curso: {nome_curso}",
            conteudo=planos_completos,
            gerar_pdf=True,
        )

        print(f"[OK] {len(planos)} Planos de aula gerados para o Curso #{curso_id}!")
        print("=" * 60)

        # Redireciona para o curso onde cada plano tem o botão individual para gerar atividade
        return redirect(f"/curso/{curso_id}")

    return render_template("planos.html")


# =========================
# LISTAR PLANOS
# =========================
@app.route("/listar_planos")
def listar_planos():
    return redirect("/listar_cursos")


# =========================
# VISUALIZAR PLANO
# =========================
@app.route("/plano/<int:plano_id>")
def visualizar_plano(plano_id):

    if "usuario_id" not in session:
        return redirect("/login")

    plano = _buscar_plano_usuario(plano_id, session["usuario_id"], incluir_curso=True)

    if not plano:
        return "Plano não encontrado", 404

    return render_template(
        "visualizar_plano.html",
        plano=plano
    )


# =========================
# LISTAR CURSOS
# =========================
@app.route("/listar_cursos")
def listar_cursos():

    if "usuario_id" not in session:
        return redirect("/login")

    conn, cursor = get_cursor()
    try:

        cursor.execute(
            """
            SELECT *
            FROM cursos_plano
            WHERE usuario_id = %s
            ORDER BY id DESC
            """,
            (session["usuario_id"],),
        )

        cursos = cursor.fetchall()
    finally:
        release_cursor(conn, cursor)

    return render_template(
        "listar_cursos.html",
        cursos=cursos
    )


# =========================
# DELETAR CURSO
# =========================
@app.route("/deletar_curso/<int:curso_id>")
def deletar_curso(curso_id):

    if "usuario_id" not in session:
        return redirect("/login")

    conn, cursor = get_cursor()
    try:
        # Verifica se o curso pertence ao usuario (ou se Ã© admin)
        cursor.execute(
            "SELECT id FROM cursos_plano WHERE id = %s AND usuario_id = %s",
            (curso_id, session["usuario_id"])
        )

        curso = cursor.fetchone()
        if not curso:
            return "Curso nÃ£o encontrado", 404

        # Deleta os planos de aula associados primeiro
        cursor.execute(
            "DELETE FROM planos_aula WHERE curso_id = %s",
            (curso_id,)
        )

        # Deleta o curso
        cursor.execute(
            "DELETE FROM cursos_plano WHERE id = %s",
            (curso_id,)
        )

        conn.commit()
        print(f"ðŸ—‘ï¸ Curso #{curso_id} e seus planos deletados")
    finally:
        release_cursor(conn, cursor)

    return redirect("/listar_cursos")


# =========================
# VISUALIZAR CURSO
# =========================
@app.route("/curso/<int:curso_id>")
def visualizar_curso(curso_id):

    if "usuario_id" not in session:
        return redirect("/login")

    curso, planos = _buscar_curso_com_planos_usuario(curso_id, session["usuario_id"])
    if not curso:
        return "Curso não encontrado", 404

    return render_template(
        "planos_curso.html",
        planos=planos,
        curso_id=curso_id,
        curso=curso
    )


# =========================
# PLANO PDF
# =========================
@app.route("/plano_pdf/<int:id>")
def plano_pdf(id):

    if "usuario_id" not in session:
        return redirect("/login")

    plano = _buscar_plano_usuario(id, session["usuario_id"])
    if not plano:
        return "Plano não encontrado", 404

    arquivo_docx = f"outputs/plano_{id}.docx"
    arquivo_pdf = f"outputs/plano_{id}.pdf"

    if _gerar_documentos_plano(
        arquivo_docx=arquivo_docx,
        arquivo_pdf=arquivo_pdf,
        titulo="PLANO DE AULA",
        subtitulo=plano[3],
        conteudo=plano[4],
        gerar_pdf=True,
    ):
        return send_file(
            arquivo_pdf,
            as_attachment=True,
            download_name=f"plano_{id}.pdf",
            mimetype="application/pdf"
        )

    return "Erro ao gerar arquivo PDF do plano. Por favor, tente novamente.", 500


# =========================
# PLANO WORD
# =========================
@app.route("/plano_word/<int:id>")
def plano_word(id):

    if "usuario_id" not in session:
        return redirect("/login")

    plano = _buscar_plano_usuario(id, session["usuario_id"])
    if not plano:
        return "Plano não encontrado", 404

    arquivo = f"outputs/plano_{id}.docx"

    _gerar_documentos_plano(
        arquivo_docx=arquivo,
        arquivo_pdf=None,
        titulo="PLANO DE AULA",
        subtitulo=plano[3],
        conteudo=plano[4],
        gerar_pdf=False,
    )

    return send_file(
        arquivo,
        as_attachment=True,
        download_name=f"plano_{id}.docx",
        mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )


# =========================
# CURSO COMPLETO PDF & WORD
# =========================
def _montar_conteudo_curso(planos):
    return "\n\n".join(f"DIA {p[2]}\n{p[4]}" for p in planos)


@app.route("/curso_pdf/<int:curso_id>")
def curso_pdf(curso_id):

    if "usuario_id" not in session:
        return redirect("/login")

    curso, planos = _buscar_curso_com_planos_usuario(curso_id, session["usuario_id"])
    if not curso:
        return "Curso não encontrado", 404

    arquivo_docx = f"outputs/curso_{curso_id}.docx"
    arquivo_pdf = f"outputs/curso_{curso_id}.pdf"

    nome_curso = curso[1]
    conteudo_total = _montar_conteudo_curso(planos)

    if _gerar_documentos_plano(
        arquivo_docx=arquivo_docx,
        arquivo_pdf=arquivo_pdf,
        titulo="PLANO DE AULA",
        subtitulo=f"Curso: {nome_curso}",
        conteudo=conteudo_total,
        gerar_pdf=True,
    ):
        return send_file(
            arquivo_pdf,
            as_attachment=True,
            download_name=f"curso_{curso_id}_completo.pdf",
            mimetype="application/pdf"
        )

    return "Erro ao gerar arquivo PDF do curso completo.", 500


@app.route("/curso_word/<int:curso_id>")
def curso_word(curso_id):

    if "usuario_id" not in session:
        return redirect("/login")

    curso, planos = _buscar_curso_com_planos_usuario(curso_id, session["usuario_id"])
    if not curso:
        return "Curso não encontrado", 404

    arquivo_docx = f"outputs/curso_{curso_id}.docx"

    nome_curso = curso[1]
    conteudo_total = _montar_conteudo_curso(planos)

    _gerar_documentos_plano(
        arquivo_docx=arquivo_docx,
        arquivo_pdf=None,
        titulo="PLANO DE AULA",
        subtitulo=f"Curso: {nome_curso}",
        conteudo=conteudo_total,
        gerar_pdf=False,
    )

    return send_file(
        arquivo_docx,
        as_attachment=True,
        download_name=f"curso_{curso_id}_completo.docx",
        mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )


# =========================
# GERAR ATIVIDADE DO PLANO
# =========================
@app.route("/gerar_atividade_plano/<int:id>", methods=["GET", "POST"])
def gerar_atividade_plano(id):

    if "usuario_id" not in session:
        return redirect("/login")

    tipo = request.values.get("tipo", "mista").strip().lower()
    fonte = request.values.get("fonte", "mista").strip().lower()
    autoria = request.values.get("autoria", "mista").strip().lower()
    dificuldade = request.values.get("dificuldade", "médio").strip().lower()
    quantidade_str = request.values.get("quantidade", "5").strip()

    # Validar quantidade
    try:
        quantidade = int(quantidade_str)
        if quantidade < 1:
            quantidade = 5
        if quantidade > 50:
            quantidade = 50
    except ValueError:
        quantidade = 5

    # =========================
    # BUSCAR PLANO
    # =========================
    plano = _buscar_plano_usuario(id, session["usuario_id"])

    if not plano:
        return "Plano não encontrado", 404

    conteudo = plano[4]
    curso_id = plano[1]

    # =========================
    # BUSCAR NOME DO CURSO
    # =========================
    conn, cursor = get_cursor()
    try:
        cursor.execute(
            """
            SELECT nome_curso
            FROM cursos_plano
            WHERE id = %s
            """,
            (curso_id,),
        )

        curso = cursor.fetchone()
    finally:
        release_cursor(conn, cursor)

    nome_curso = curso[0] if curso else "Curso Técnico"

    # Extrai metadados estruturados para o cabeçalho e busca contextual
    curso_ident = nome_curso
    uc_ident = "Plano de Aula"
    aula_ident = f"Dia {plano[2]}"
    bloom_ident = dificuldade.capitalize()

    m_c = re.search(r"Curso:\s*([^\n\r]+)", conteudo, re.IGNORECASE)
    if m_c:
        curso_ident = m_c.group(1).strip()
    elif "—" in nome_curso:
        curso_ident = nome_curso.split("—")[0].strip()

    m_uc = re.search(r"Unidade Curricular:\s*([^\n\r]+)", conteudo, re.IGNORECASE)
    if m_uc:
        uc_ident = m_uc.group(1).strip()
    elif "—" in nome_curso:
        uc_ident = nome_curso.split("—")[1].strip()

    m_a = re.search(r"Aula\s*/\s*Encontro:\s*([^\n\r]+)", conteudo, re.IGNORECASE)
    if m_a:
        aula_ident = m_a.group(1).strip()

    m_b = re.search(r"N[ií]vel Cognitivo(?:\s*\(Taxonomia de Bloom\))?:\s*([^\n\r]+)", conteudo, re.IGNORECASE)
    if m_b:
        bloom_ident = m_b.group(1).strip()

    print(f"🤖 Gerando atividade a partir do Plano #{id}")
    print(f"   Curso: {curso_ident} | UC: {uc_ident} | Formato: {tipo} | Fonte: {fonte} | Autoria: {autoria} | Dificuldade: {dificuldade} | Qtd: {quantidade}")

    # Detecção contextual se é PEI / Educação Especial
    eh_pei_plano = (
        "pei" in (plano[3] or "").lower()
        or "pei" in (nome_curso or "").lower()
        or "individualizado" in str(conteudo).lower()
        or any("dua" in str(col or "").lower() for col in plano[8:])
    )
    segmento_plano = "pei" if eh_pei_plano else session.get("segmento", "fundamental")

    # ========================================================
    # RESOLUÇÃO DAS QUESTÕES DE ACORDO COM FONTE E AUTORIA
    # ========================================================
    atividade = ""
    origem_salva = "ia"

    if fonte == "repositorio":
        # Busca no Repositório Interno com o filtro de autoria selecionado
        questoes_disp = buscar_questoes_repositorio(
            curso=curso_ident,
            disciplina=uc_ident,
            conteudo=conteudo,
            dificuldade=dificuldade,
            tipo=tipo,
            usuario_id=session["usuario_id"],
            limite=quantidade,
            autoria=autoria
        )
        qtd_encontrada = len(questoes_disp)
        if qtd_encontrada >= quantidade:
            atividade = formatar_questoes_para_atividade(questoes_disp[:quantidade])
            origem_salva = "repositorio"
        else:
            # Completa as faltantes via IA
            faltantes = quantidade - qtd_encontrada
            texto_ia = gerar_atividade(conteudo, dificuldade, tipo, faltantes, segmento=segmento_plano, disciplina=uc_ident, curso=curso_ident)
            salvar_questoes_ia_no_repositorio(
                texto_atividade=texto_ia,
                curso=curso_ident,
                disciplina=uc_ident,
                conteudo=conteudo,
                dificuldade=dificuldade,
                tipo=tipo,
                usuario_id=session["usuario_id"]
            )
            atividade = compor_atividade_hibrida(questoes_disp, texto_ia, quantidade)
            origem_salva = "misto" if qtd_encontrada > 0 else "ia"

    elif fonte == "externo":
        # Geração externa via IA
        atividade = gerar_atividade(conteudo, dificuldade, tipo, quantidade, segmento=segmento_plano, disciplina=uc_ident, curso=curso_ident)
        salvar_questoes_ia_no_repositorio(
            texto_atividade=atividade,
            curso=curso_ident,
            disciplina=uc_ident,
            conteudo=conteudo,
            dificuldade=dificuldade,
            tipo=tipo,
            usuario_id=session["usuario_id"]
        )
        origem_salva = "ia"

    else:
        # Fonte Mista (Repositório + Externo/IA)
        qtd_repo_alvo = max(1, quantidade // 2)
        questoes_disp = buscar_questoes_repositorio(
            curso=curso_ident,
            disciplina=uc_ident,
            conteudo=conteudo,
            dificuldade=dificuldade,
            tipo=tipo,
            usuario_id=session["usuario_id"],
            limite=qtd_repo_alvo,
            autoria=autoria
        )
        qtd_repo = len(questoes_disp)
        qtd_ia_necessaria = quantidade - qtd_repo
        texto_ia = ""
        if qtd_ia_necessaria > 0:
            texto_ia = gerar_atividade(conteudo, dificuldade, tipo, qtd_ia_necessaria, segmento=segmento_plano, disciplina=uc_ident, curso=curso_ident)
            salvar_questoes_ia_no_repositorio(
                texto_atividade=texto_ia,
                curso=curso_ident,
                disciplina=uc_ident,
                conteudo=conteudo,
                dificuldade=dificuldade,
                tipo=tipo,
                usuario_id=session["usuario_id"]
            )
        atividade = compor_atividade_hibrida(questoes_disp, texto_ia, quantidade)
        origem_salva = "misto"

    # Monta rótulos claros para o cabeçalho oficial
    fonte_label = {
        "repositorio": "Repositório Interno",
        "externo": "Externo (Geração sob Demanda)",
        "mista": "Mista (Repositório + Externo)"
    }.get(fonte, fonte.capitalize())

    autoria_label = {
        "professor": "Professor (Docente)",
        "ia": "Inteligência Artificial (IA)",
        "mista": "Mista (Professor + IA)"
    }.get(autoria, autoria.capitalize())

    formato_label = {
        "objetiva": "Objetiva (Múltipla Escolha)",
        "discursiva": "Discursiva (Dissertativa)",
        "pratica": "Prática (Laboratório / Oficina / Desafio)",
        "mista": "Mista (Teórica + Prática)"
    }.get(tipo, tipo.capitalize())

    cabecalho_ident = (
        f"- Curso: {curso_ident}\n"
        f"- Unidade Curricular: {uc_ident}\n"
        f"- Aula / Encontro: {aula_ident}\n"
        f"- Formato: {formato_label}\n"
        f"- Fonte das Questões: {fonte_label}\n"
        f"- Autoria das Questões: {autoria_label}\n"
        f"- Nível Cognitivo (Taxonomia de Bloom): {bloom_ident}"
    )

    # =========================
    # SALVAR NO BANCO
    # =========================
    conn, cursor = get_cursor()
    try:
        cursor.execute(
            """
            INSERT INTO atividades (
                curso,
                disciplina,
                conteudo,
                dificuldade,
                atividade_gerada,
                usuario_id,
                origem
            )
            VALUES (%s,%s,%s,%s,%s,%s,%s)
            """,
            (
                curso_ident,
                uc_ident,
                cabecalho_ident,
                bloom_ident,
                atividade,
                session["usuario_id"],
                origem_salva
            ),
        )

        conn.commit()
    finally:
        release_cursor(conn, cursor)

    return redirect("/atividades")


# =========================
# DELETAR PLANO INDIVIDUAL
# =========================
@app.route("/deletar_plano/<int:plano_id>")
def deletar_plano(plano_id):

    if "usuario_id" not in session:
        return redirect("/login")

    conn, cursor = get_cursor()
    try:
        # Busca o plano para saber o curso_id (para redirecionar)
        cursor.execute(
            "SELECT curso_id, usuario_id FROM planos_aula WHERE id = %s",
            (plano_id,)
        )
        plano = cursor.fetchone()

        if not plano:
            return "Plano nÃ£o encontrado", 404

        curso_id = plano[0]
        dono_id = plano[1]

        # Verifica permissÃ£o: admin ou dono
        if dono_id != session["usuario_id"]:
            return "Acesso negado", 403

        # Deleta o plano
        cursor.execute(
            "DELETE FROM planos_aula WHERE id = %s",
            (plano_id,)
        )
        conn.commit()
        print(f"ðŸ—‘ï¸ Plano #{plano_id} deletado")
    finally:
        release_cursor(conn, cursor)

    return redirect(f"/curso/{curso_id}")


# =========================
# DOWNLOAD PLANO
# =========================
@app.route("/baixar_plano/<int:plano_id>")
def baixar_plano(plano_id):

    if "usuario_id" not in session:
        return redirect("/login")

    plano = _buscar_plano_usuario(plano_id, session["usuario_id"])
    if not plano:
        return "Plano não encontrado", 404

    arquivo = f"outputs/plano_{plano_id}.docx"

    _gerar_documentos_plano(
        arquivo_docx=arquivo,
        arquivo_pdf=None,
        titulo="PLANO DE AULA",
        subtitulo=f"Dia {plano[2]}",
        conteudo=plano[4],
        gerar_pdf=False,
    )

    return send_file(
        arquivo,
        as_attachment=True,
        download_name=f"plano_{plano_id}.docx",
        mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )




# =========================
# PLANEJAMENTO DOCENTE
# =========================
def _para_int_seguro(valor, padrao, minimo=1):
    """Converte valor de formulário para inteiro positivo sem lançar ValueError."""
    if valor is None:
        return padrao
    texto = str(valor).strip().replace(",", ".")
    m = re.search(r"-?\d+(?:\.\d+)?", texto)
    if not m:
        return padrao
    try:
        num = int(round(float(m.group(0))))
        return max(minimo, num) if minimo is not None else num
    except (ValueError, TypeError):
        return padrao


def _normalizar_data_sql(data_str):
    """Normaliza data (YYYY-MM-DD ou DD/MM/YYYY) para formato ISO aceito por coluna DATE do PostgreSQL."""
    if not data_str:
        return None
    s = str(data_str).strip()[:10]
    for fmt in ("%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


@app.route("/planejamento_docente", methods=["GET", "POST"])
def planejamento_docente():

    if "usuario_id" not in session:
        return redirect("/login")

    if request.method == "POST":

        nome_curso         = request.form.get("nome_curso", "").strip()
        unidade_curricular = request.form.get("unidade_curricular", "").strip()
        if not nome_curso or not unidade_curricular:
            flash("Informe o nome do curso/série e o componente curricular (disciplina).", "warning")
            return redirect("/planejamento_docente")

        codigo_turma       = request.form.get("codigo_turma", "TEC.2026.1").strip() or "TEC.2026.1"
        modalidade         = request.form.get("modalidade", "HABILITAÇÃO TÉCNICA").strip() or "HABILITAÇÃO TÉCNICA"
        unidade_escolar    = request.form.get("unidade_escolar", "").strip()
        tipo_cronograma    = request.form.get("tipo_cronograma", "horas").strip()
        duracao_aula       = _para_int_seguro(request.form.get("duracao_aula"), 50, minimo=10)
        data_inicio_str    = request.form.get("data_inicio", "").strip()
        data_fim_str       = request.form.get("data_fim", "").strip()

        if tipo_cronograma == "aulas":
            aulas_bimestre = _para_int_seguro(request.form.get("aulas_bimestre"), 40, minimo=1)
            aulas_por_dia_raw = request.form.get("aulas_por_dia")
            if aulas_por_dia_raw and str(aulas_por_dia_raw).strip():
                aulas_por_dia = _para_int_seguro(aulas_por_dia_raw, 2, minimo=1)
                total_aulas = -(-aulas_bimestre // aulas_por_dia)  # Total de encontros
            else:
                aulas_por_dia = 1
                total_aulas = aulas_bimestre
            carga_horaria = int(round((aulas_bimestre * duracao_aula) / 60)) or aulas_bimestre
            horas_por_dia = max(1, int(round((aulas_por_dia * duracao_aula) / 60))) or 1
        else:
            carga_horaria      = _para_int_seguro(request.form.get("carga_horaria"), 80, minimo=1)
            horas_por_dia      = _para_int_seguro(request.form.get("horas_por_dia"), 4, minimo=1)
            total_aulas = -(-carga_horaria // horas_por_dia)  # Divisão com teto

        print("=" * 60)
        print(f"[INFO] GERANDO PLANEJAMENTO DOCENTE")
        print(f"   Curso: {nome_curso} | UC: {unidade_curricular}")
        print(f"   CH: {carga_horaria}h | {horas_por_dia}h/dia | {total_aulas} aulas")
        print("=" * 60)

        pasta_upload = "uploads"
        os.makedirs(pasta_upload, exist_ok=True)

        # ----------------------------------------------------------
        # 0. Modelo de Planejamento Personalizado (Word ou Excel) - Opcional
        # ----------------------------------------------------------
        arquivo_modelo = request.files.get("arquivo_modelo") or request.files.get("arquivo_modelo_excel")
        caminho_modelo_custom = None

        # Define formato padrão conforme a modalidade escolhida:
        # Técnico / Qualificação / Profissionalizante -> Excel (.xlsx) oficial
        # Educação Básica (Infantil, Fundamental, Médio) e Ensino Superior / Graduação -> Word (.docx)
        modalidade_upper = (modalidade or "").upper()
        eh_tecnico_ou_qualif = any(k in modalidade_upper for k in ["TÉCNIC", "TECNIC", "HABILITA", "QUALIF", "APERFEIÇ", "APERFEIC"])
        formato_modelo = "excel" if eh_tecnico_ou_qualif else "word"

        if arquivo_modelo and arquivo_modelo.filename:
            ext_original = os.path.splitext(arquivo_modelo.filename)[1].lower()
            nome_modelo = secure_filename(arquivo_modelo.filename) or f"modelo{ext_original}"
            ext_modelo = os.path.splitext(nome_modelo)[1].lower() or ext_original
            prefixo_unico = f"modelo_custom_{session['usuario_id']}_{int(datetime.now().timestamp() * 1000)}"
            if ext_modelo in (".xlsx", ".xls", ".xlsm"):
                caminho_modelo_custom = os.path.join(pasta_upload, f"{prefixo_unico}_{nome_modelo}")
                arquivo_modelo.save(caminho_modelo_custom)
                formato_modelo = "excel"
                print(f"[INFO] Modelo de planejamento Excel enviado: {caminho_modelo_custom}")
            elif ext_modelo in (".docx", ".doc"):
                caminho_modelo_custom = os.path.join(pasta_upload, f"{prefixo_unico}_{nome_modelo}")
                arquivo_modelo.save(caminho_modelo_custom)
                formato_modelo = "word"
                print(f"[INFO] Modelo de planejamento Word ({ext_modelo}) enviado: {caminho_modelo_custom}")

        # ----------------------------------------------------------
        # 1. Obtenção dos Dados Pedagógicos (Digitados ou via PDF)
        # ----------------------------------------------------------
        tipo_entrada = request.form.get("tipo_entrada_conteudo", "texto")
        conteudo_digitado = request.form.get("conteudo_digitado", "").strip()

        dados_plano = {
            "unidade_curricular": unidade_curricular,
            "objetivo_geral": f"Desenvolver competências e conteúdos fundamentais de {unidade_curricular}",
            "capacidades_tecnicas": [],
            "conhecimentos": [],
            "grupos_conhecimentos": [],
            "texto_bruto": ""
        }

        arquivo_plano = request.files.get("arquivo_plano_pdf")

        if tipo_entrada == "pdf" and arquivo_plano and arquivo_plano.filename:
            nome_plano = secure_filename(arquivo_plano.filename)
            caminho_plano = os.path.join(pasta_upload, nome_plano)
            arquivo_plano.save(caminho_plano)
            print(f"[INFO] Plano de Curso salvo: {caminho_plano}")
            try:
                dados_plano = estruturar_dados_plano_pdf(caminho_plano, unidade_curricular_alvo=unidade_curricular)
                dados_plano["unidade_curricular"] = dados_plano.get("unidade_curricular") or unidade_curricular
                print(f"[OK] PDF lido: {len(dados_plano.get('conhecimentos', []))} conhecimentos extraídos")
            except Exception as e:
                print(f"[AVISO] Erro ao ler PDF do plano: {e}")
        elif conteudo_digitado:
            # Processa ementa / tópicos digitados diretamente pelo professor
            linhas_conteudo = [l.strip() for l in conteudo_digitado.splitlines() if l.strip()]
            dados_plano["conhecimentos"] = linhas_conteudo
            dados_plano["texto_bruto"] = conteudo_digitado
            print(f"[OK] Entrada por tópicos digitados: {len(linhas_conteudo)} tópicos informados.")
        else:
            # Caso não tenha subido PDF e não tenha digitado nada, o fallback inteligente formula os tópicos
            dados_plano["conhecimentos"] = [
                f"Introdução e contextualização de {unidade_curricular}",
                f"Conceitos e princípios essenciais de {unidade_curricular}",
                f"Procedimentos fundamentais e aplicações de {unidade_curricular}",
                f"Exercícios e consolidação prática em {unidade_curricular}",
                f"Avaliação e síntese dos conhecimentos em {unidade_curricular}"
            ]
            print("[INFO] Nenhum arquivo ou ementa fornecida. A IA preencherá automaticamente.")

        # ----------------------------------------------------------
        # 1.1 ROTA ESPECIALIZADA: PEI (Educação Especial Inclusiva)
        # ----------------------------------------------------------
        eh_pei = "PEI" in modalidade_upper or "INDIVIDUALIZADO" in modalidade_upper
        eh_cascata_escolar = any(k in modalidade_upper for k in ["FUNDAMENTAL", "MÉDIO", "MEDIO", "INFANTIL"])

        if eh_pei:
            estudante_nome = request.form.get("estudante_nome", "").strip() or "Estudante"
            estudante_necessidades = request.form.get("estudante_necessidades", "").strip() or "Educação Especial Inclusiva"
            recursos_tecnologicos_assistivos = request.form.get("recursos_tecnologicos_assistivos", "").strip() or "DUA, CAA (ARASAAC), Apoios Visuais TEACCH"
            semestre_pei = int(request.form.get("semestre_pei") or 1)

            print(f"[PEI] Gerando PEI Semestral para {estudante_nome} ({estudante_necessidades})...")
            texto_pei, dados_pei = gerar_planejamento_pei_semestral_ia(
                aluno_nome=estudante_nome,
                necessidades=estudante_necessidades,
                segmento=modalidade,
                componente=unidade_curricular,
                semestre=semestre_pei,
                tecnologias_assistivas=recursos_tecnologicos_assistivos,
                serie=nome_curso
            )

            conn, cursor = get_cursor()
            try:
                cursor.execute(
                    """
                    INSERT INTO cursos_plano (nome_curso, carga_horaria, aulas_por_dia, usuario_id)
                    VALUES (%s, %s, %s, %s)
                    RETURNING id
                    """,
                    (f"PEI — {estudante_nome} ({unidade_curricular})"[:200], carga_horaria, horas_por_dia, session["usuario_id"])
                )
                curso_id = cursor.fetchone()[0]

                payload_json = json.dumps({
                    "texto_pei_semestral": texto_pei,
                    "dados": dados_pei,
                    "semestres": {str(semestre_pei): texto_pei},
                    "mensais": {}
                }, ensure_ascii=False)

                cursor.execute(
                    """
                    INSERT INTO planejamentos_docente (
                        nome_curso, unidade_curricular, turma, unidade_escolar,
                        carga_horaria, horas_por_dia, data_inicio, data_fim, total_aulas,
                        arquivo_excel, curso_id, usuario_id, modalidade,
                        tipo_planejamento, semestre, estudante_nome, estudante_necessidades,
                        recursos_tecnologicos_assistivos, dados_json
                    )
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    RETURNING id
                    """,
                    (
                        nome_curso[:255], unidade_curricular[:255], codigo_turma[:100], unidade_escolar[:255],
                        carga_horaria, horas_por_dia, _normalizar_data_sql(data_inicio_str), _normalizar_data_sql(data_fim_str), total_aulas,
                        None, curso_id, session["usuario_id"], modalidade[:100],
                        "pei_semestral", semestre_pei, estudante_nome[:150], estudante_necessidades,
                        recursos_tecnologicos_assistivos, payload_json
                    )
                )
                planejamento_id = cursor.fetchone()[0]
                conn.commit()
                print(f"[OK] PEI Semestral #{planejamento_id} criado com sucesso!")
            finally:
                release_cursor(conn, cursor)

            return redirect(f"/planejamento/{planejamento_id}")

        elif eh_cascata_escolar and not caminho_modelo_custom:
            ano_letivo = str(datetime.now().year)
            if data_inicio_str and len(data_inicio_str) >= 4 and data_inicio_str[:4].isdigit():
                ano_letivo = data_inicio_str[:4]
            print(f"[CASCATA] Gerando Planejamento Anual (Educação Básica) para {nome_curso} - {unidade_curricular} (Ano Letivo: {ano_letivo})...")
            texto_anual, dados_anual = gerar_planejamento_anual_ia(
                nome_curso=nome_curso,
                componente=unidade_curricular,
                segmento=modalidade,
                ementa_digitada=conteudo_digitado,
                ano_letivo=ano_letivo,
                docente_responsavel=session.get("nome", "").strip()
            )

            conn, cursor = get_cursor()
            try:
                cursor.execute(
                    """
                    INSERT INTO cursos_plano (nome_curso, carga_horaria, aulas_por_dia, usuario_id)
                    VALUES (%s, %s, %s, %s)
                    RETURNING id
                    """,
                    (f"{nome_curso} — {unidade_curricular}"[:200], carga_horaria, horas_por_dia, session["usuario_id"])
                )
                curso_id = cursor.fetchone()[0]

                payload_json = json.dumps({
                    "texto_anual": texto_anual,
                    "dados": dados_anual,
                    "bimestres": {},
                    "semestres": {},
                    "mensais": {}
                }, ensure_ascii=False)

                cursor.execute(
                    """
                    INSERT INTO planejamentos_docente (
                        nome_curso, unidade_curricular, turma, unidade_escolar,
                        carga_horaria, horas_por_dia, data_inicio, data_fim, total_aulas,
                        arquivo_excel, curso_id, usuario_id, modalidade,
                        tipo_planejamento, dados_json
                    )
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    RETURNING id
                    """,
                    (
                        nome_curso[:255], unidade_curricular[:255], codigo_turma[:100], unidade_escolar[:255],
                        carga_horaria, horas_por_dia, _normalizar_data_sql(data_inicio_str), _normalizar_data_sql(data_fim_str), total_aulas,
                        None, curso_id, session["usuario_id"], modalidade[:100],
                        "anual", payload_json
                    )
                )
                planejamento_id = cursor.fetchone()[0]
                conn.commit()
                print(f"[OK] Planejamento Anual em Cascata #{planejamento_id} criado com sucesso!")
            finally:
                release_cursor(conn, cursor)

            return redirect(f"/planejamento/{planejamento_id}")

        # ----------------------------------------------------------
        # 2. Lê a Matriz SAEP (opcional)
        # ----------------------------------------------------------
        arquivo_matriz = request.files.get("arquivo_matriz")
        dados_matriz = []
        if arquivo_matriz and arquivo_matriz.filename:
            nome_matriz = secure_filename(arquivo_matriz.filename)
            caminho_matriz = os.path.join(pasta_upload, nome_matriz)
            arquivo_matriz.save(caminho_matriz)
            print(f"[INFO] Matriz SAEP salva: {caminho_matriz}")
            try:
                dados_matriz = ler_matriz_saep(caminho_matriz)
                print(f"[OK] Matriz lida: {len(dados_matriz)} capacidades")
            except Exception as e:
                print(f"[AVISO] Erro ao ler Matriz SAEP: {e}")

        # Dias da semana de aula selecionados pelo professor (0=Seg, ..., 6=Dom)
        dias_semana_raw = request.form.getlist("dias_semana")
        if dias_semana_raw:
            dias_semana = [int(x) for x in dias_semana_raw if str(x).isdigit()]
        else:
            dias_semana = [0, 1, 2, 3, 4]

        # Feriados e recessos municipais / regionais informados
        feriados_json = request.form.get("feriados_json", "[]")
        feriados = []
        try:
            itens_feriados = json.loads(feriados_json) if feriados_json else []
            for item in itens_feriados:
                if isinstance(item, dict) and "data" in item:
                    feriados.append(item["data"])
                elif isinstance(item, str) and item.strip():
                    feriados.append(item.strip())
        except Exception as e:
            print(f"[AVISO] Erro ao decodificar feriados_json: {e}")

        feriados_form = request.form.getlist("feriados")
        if feriados_form:
            feriados.extend([f.strip() for f in feriados_form if f.strip()])

        # Frequência / Periodicidade (semanal ou quinzenal / 15 em 15 dias)
        periodicidade = request.form.get("periodicidade", "semanal")

        # ----------------------------------------------------------
        # 3. Gera o Planejamento Docente (Taxonomia de Bloom)
        # ----------------------------------------------------------
        datas = calcular_datas_aulas(
            data_inicio_str,
            total_aulas,
            dias_semana=dias_semana,
            feriados=feriados,
            periodicidade=periodicidade
        )

        data_fim_calculada = datas[-1].strftime("%Y-%m-%d") if datas else data_inicio_str
        if not data_fim_str:
            data_fim_str = data_fim_calculada

        aulas = gerar_planejamento_ia(
            dados_plano,
            dados_matriz,
            total_aulas,
            horas_por_dia,
            datas,
            modalidade=modalidade
        )

        logo_path = _obter_caminho_logo_usuario(session.get("logo_instituicao"))

        dados_cabecalho = {
            "nome_curso":         nome_curso,
            "unidade_curricular": unidade_curricular,
            "codigo_turma":       codigo_turma,
            "modalidade":         modalidade,
            "unidade_escolar":    unidade_escolar or session.get("instituicao", ""),
            "carga_horaria":      carga_horaria,
            "horas_por_dia":      horas_por_dia,
            "data_inicio":        data_inicio_str,
            "data_fim":           data_fim_str,
            "instrutor":          session.get("nome", ""),
            "logo_path":          logo_path
        }

        os.makedirs("outputs", exist_ok=True)
        if formato_modelo == "word":
            try:
                caminho_gerado = gerar_docx_planejamento(dados_cabecalho, aulas, caminho_modelo=caminho_modelo_custom)
            except Exception as e_modelo_word:
                print(f"[AVISO] Falha ao preencher modelo Word customizado ({e_modelo_word}). Usando modelo padrão.")
                traceback.print_exc()
                caminho_gerado = gerar_docx_planejamento(dados_cabecalho, aulas, caminho_modelo=None)
                if caminho_modelo_custom:
                    flash(
                        "O modelo Word enviado possui formato incompatível; o planejamento foi gerado automaticamente no modelo padrão do SGPP.",
                        "warning",
                    )
            nome_arquivo_gerado = os.path.basename(caminho_gerado)
            upload_arquivo_local(
                caminho_gerado,
                f"planejamentos/{nome_arquivo_gerado}",
                content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
            print(f"[OK] Planejamento Docente (Word) gerado com sucesso: {caminho_gerado}")
        else:
            # Excel: usa o modelo customizado (se enviado) ou os modelos oficiais padrão (modelo_tecnico.xlsx / modelo_qualificacao.xlsx)
            try:
                caminho_gerado = gerar_excel_planejamento(dados_cabecalho, aulas, caminho_modelo=caminho_modelo_custom)
            except Exception as e_modelo_excel:
                print(f"[AVISO] Falha ao preencher modelo Excel customizado ({e_modelo_excel}). Usando modelo padrão.")
                traceback.print_exc()
                caminho_gerado = gerar_excel_planejamento(dados_cabecalho, aulas, caminho_modelo=None)
                if caminho_modelo_custom:
                    flash(
                        "O modelo Excel enviado possui formato incompatível ou protegido; o planejamento foi gerado automaticamente no modelo padrão do SGPP.",
                        "warning",
                    )
            nome_arquivo_gerado = os.path.basename(caminho_gerado)
            upload_arquivo_local(
                caminho_gerado,
                f"planejamentos/{nome_arquivo_gerado}",
                content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
            print(f"[OK] Planejamento Docente (Excel) gerado com sucesso: {caminho_gerado}")

        # ----------------------------------------------------------
        # 4. Salva o Planejamento Docente e gera automaticamente os Planos de Aula
        # ----------------------------------------------------------
        data_inicio_sql = _normalizar_data_sql(data_inicio_str)
        data_fim_sql = _normalizar_data_sql(data_fim_str)
        titulo_curso_bd = f"{nome_curso} — {unidade_curricular}"[:200]

        conn, cursor = get_cursor()
        try:
            # 4.1 Cria o registro do curso em cursos_plano
            cursor.execute(
                """
                INSERT INTO cursos_plano (nome_curso, carga_horaria, aulas_por_dia, usuario_id)
                VALUES (%s, %s, %s, %s)
                RETURNING id
                """,
                (titulo_curso_bd, carga_horaria, horas_por_dia, session["usuario_id"])
            )
            curso_id = cursor.fetchone()[0]

            # 4.2 Salva o Planejamento Docente com curso_id vinculado
            cursor.execute(
                """
                INSERT INTO planejamentos_docente (
                    nome_curso,
                    unidade_curricular,
                    turma,
                    unidade_escolar,
                    carga_horaria,
                    horas_por_dia,
                    data_inicio,
                    data_fim,
                    total_aulas,
                    arquivo_excel,
                    curso_id,
                    usuario_id,
                    modalidade
                )
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                RETURNING id
                """,
                (
                    nome_curso[:255],
                    unidade_curricular[:255],
                    codigo_turma[:100],
                    unidade_escolar[:255],
                    carga_horaria,
                    horas_por_dia,
                    data_inicio_sql,
                    data_fim_sql,
                    total_aulas,
                    nome_arquivo_gerado[:255],
                    curso_id,
                    session["usuario_id"],
                    modalidade[:100]
                )
            )
            planejamento_id = cursor.fetchone()[0]
            conn.commit()
            print(f"[OK] Planejamento Docente #{planejamento_id} (Curso #{curso_id}) salvo com sucesso!")
        finally:
            release_cursor(conn, cursor)

        print(f"[OK] Planejamento Docente pronto!")
        print(f"[INFO] Disponibilizando arquivo Excel para download imediato...")
        print("=" * 60)

        # Redireciona para a lista de planejamentos com download automático
        return redirect(f"/listar_planejamentos?sucesso=1&download={planejamento_id}")

    return render_template("planejamento_docente.html")


# =========================
# LISTAR PLANEJAMENTOS DOCENTES
# =========================
@app.route("/listar_planejamentos")
def listar_planejamentos():

    if "usuario_id" not in session:
        return redirect("/login")

    conn, cursor = get_cursor()
    try:
        sql_select = """
            SELECT 
                p.id,
                p.nome_curso,
                p.unidade_curricular,
                p.turma,
                p.unidade_escolar,
                p.carga_horaria,
                p.horas_por_dia,
                p.data_inicio,
                p.data_fim,
                p.total_aulas,
                p.arquivo_excel,
                p.usuario_id,
                p.data_criacao,
                p.curso_id,
                p.modalidade,
                COALESCE(
                    (SELECT COUNT(*) FROM planos_aula pa WHERE pa.planejamento_id = p.id),
                    (SELECT COUNT(*) FROM planos_aula pa WHERE p.curso_id IS NOT NULL AND pa.curso_id = p.curso_id),
                    0
                ) AS qtd_planos
            FROM planejamentos_docente p
        """

        cursor.execute(sql_select + " WHERE p.usuario_id = %s ORDER BY p.id DESC", (session["usuario_id"],))

        planejamentos = cursor.fetchall()
    finally:
        release_cursor(conn, cursor)

    return render_template(
        "listar_planejamentos.html",
        planejamentos=planejamentos
    )


# =========================
# BAIXAR PLANEJAMENTO DOCENTE (EXCEL)
# =========================
@app.route("/baixar_planejamento/<int:planejamento_id>")
def baixar_planejamento(planejamento_id):

    if "usuario_id" not in session:
        return redirect("/login")

    conn, cursor = get_cursor()
    try:
        cursor.execute(
            "SELECT id, arquivo_excel FROM planejamentos_docente WHERE id = %s AND usuario_id = %s",
            (planejamento_id, session["usuario_id"])
        )
        planejamento = cursor.fetchone()
    finally:
        release_cursor(conn, cursor)

    if not planejamento:
        return "Planejamento não encontrado", 404

    nome_arquivo = planejamento[1]  # coluna arquivo_excel
    if not nome_arquivo:
        flash("Este planejamento em cascata/PEI não possui arquivo Excel/Word associado.", "warning")
        return redirect(f"/planejamento/{planejamento_id}")

    caminho = os.path.join("outputs", nome_arquivo)
    garantir_arquivo_local(caminho, f"planejamentos/{nome_arquivo}")

    if not os.path.exists(caminho):
        return "Arquivo Excel não encontrado no servidor. Por favor, gere novamente.", 404

    return send_file(
        caminho,
        as_attachment=True,
        download_name=nome_arquivo
    )


# =========================
# DELETAR PLANEJAMENTO DOCENTE
# =========================
@app.route("/deletar_planejamento/<int:planejamento_id>")
def deletar_planejamento(planejamento_id):

    if "usuario_id" not in session:
        return redirect("/login")

    conn, cursor = get_cursor()
    try:
        cursor.execute(
            "SELECT id, arquivo_excel FROM planejamentos_docente WHERE id = %s AND usuario_id = %s",
            (planejamento_id, session["usuario_id"])
        )
        planejamento = cursor.fetchone()

        if not planejamento:
            return "Planejamento não encontrado", 404

        # Remove o arquivo Excel do disco e do Supabase Storage, se existir
        nome_arquivo = planejamento[1]
        if nome_arquivo:
            deletar_arquivo(f"planejamentos/{nome_arquivo}")
            caminho = os.path.join("outputs", nome_arquivo)
            if os.path.exists(caminho):
                os.remove(caminho)
                print(f"[INFO] Arquivo Excel removido: {caminho}")

        cursor.execute(
            "DELETE FROM planejamentos_docente WHERE id = %s",
            (planejamento_id,)
        )
        conn.commit()
        print(f"[INFO] Planejamento Docente #{planejamento_id} deletado")
    finally:
        release_cursor(conn, cursor)

    return redirect("/listar_planejamentos")



# =========================
# GERAR PLANOS DE UM PLANEJAMENTO EXISTENTE
# =========================
@app.route("/gerar_planos_planejamento/<int:planejamento_id>")
def gerar_planos_planejamento(planejamento_id):

    if "usuario_id" not in session:
        return redirect("/login")

    conn, cursor = get_cursor()
    try:
        cursor.execute(
            """
            SELECT id, nome_curso, unidade_curricular, turma, unidade_escolar,
                   carga_horaria, horas_por_dia, data_inicio, data_fim, total_aulas,
                   arquivo_excel, usuario_id, data_criacao, curso_id, modalidade
            FROM planejamentos_docente
            WHERE id = %s
            """,
            (planejamento_id,)
        )
        plan = cursor.fetchone()
    finally:
        release_cursor(conn, cursor)

    if not plan:
        return "Planejamento não encontrado", 404

    # Verifica permissão: dono ou admin
    if plan[11] != session["usuario_id"]:
        return "Acesso não autorizado", 403

    p_id = plan[0]
    nome_curso = plan[1]
    uc = plan[2]
    turma = plan[3]
    unidade_escolar = plan[4]
    ch = plan[5]
    hd = plan[6]
    data_inicio = plan[7]
    data_fim = plan[8]
    total_aulas = plan[9]
    nome_arquivo_excel = plan[10]
    dono_id = plan[11]
    curso_id = plan[13]

    # 1. Se já tem planos gerados e curso_id vinculado, vai direto para o curso
    conn, cursor = get_cursor()
    try:
        qtd = 0
        if curso_id:
            cursor.execute("SELECT COUNT(*) FROM planos_aula WHERE curso_id = %s", (curso_id,))
            qtd = cursor.fetchone()[0]
        if qtd == 0:
            cursor.execute("SELECT COUNT(*) FROM planos_aula WHERE planejamento_id = %s", (p_id,))
            qtd = cursor.fetchone()[0]

        if qtd > 0:
            if not curso_id:
                cursor.execute("SELECT curso_id FROM planos_aula WHERE planejamento_id = %s LIMIT 1", (p_id,))
                c_row = cursor.fetchone()
                if c_row and c_row[0]:
                    curso_id = c_row[0]
                    cursor.execute("UPDATE planejamentos_docente SET curso_id = %s WHERE id = %s", (curso_id, p_id))
                    conn.commit()
            if curso_id:
                return redirect(f"/curso/{curso_id}")
    finally:
        release_cursor(conn, cursor)

    # 2. Lê as aulas estruturadas da planilha Excel do planejamento (com fallback do Supabase Storage)
    caminho_excel = os.path.join("outputs", nome_arquivo_excel) if nome_arquivo_excel else ""
    if caminho_excel and nome_arquivo_excel:
        garantir_arquivo_local(caminho_excel, f"planejamentos/{nome_arquivo_excel}")
    aulas_excel = []
    if caminho_excel and os.path.exists(caminho_excel):
        try:
            _, aulas_excel = ler_excel_planejamento_docente(caminho_excel)
        except Exception as e:
            print(f"[AVISO] Erro ao ler Excel do planejamento #{p_id}: {e}")

    # 3. Cria ou vincula o curso na tabela cursos_plano
    conn, cursor = get_cursor()
    try:
        if not curso_id:
            cursor.execute(
                """
                INSERT INTO cursos_plano (nome_curso, carga_horaria, aulas_por_dia, usuario_id)
                VALUES (%s, %s, %s, %s)
                RETURNING id
                """,
                (f"{nome_curso} — {uc}", ch, hd, dono_id)
            )
            curso_id = cursor.fetchone()[0]

            cursor.execute(
                "UPDATE planejamentos_docente SET curso_id = %s WHERE id = %s",
                (curso_id, p_id)
            )
            conn.commit()
    finally:
        release_cursor(conn, cursor)

    # 4. Fallback caso não tenha conseguido ler do Excel
    if not aulas_excel:
        datas = calcular_datas_aulas(str(data_inicio) if data_inicio else None, total_aulas)
        dados_plano = {"unidade_curricular": uc, "conhecimentos": []}
        aulas_excel = gerar_planejamento_ia(dados_plano, [], total_aulas, hd, datas)

    # 5. Gera os planos de aula detalhados
    planos_gerados = gerar_todos_planos_de_planejamento(
        aulas_excel,
        nome_curso,
        uc,
        hd,
        max_workers=2
    )

    # 6. Salva no banco de dados vinculando curso_id e planejamento_id
    planos_completos = ""
    conn, cursor = get_cursor()
    try:
        cursor.execute("DELETE FROM planos_aula WHERE curso_id = %s OR planejamento_id = %s", (curso_id, p_id))
        for dia, plano_texto in enumerate(planos_gerados, start=1):
            planos_completos += f"\n\nDIA {dia}\n\n{plano_texto}"
            cursor.execute(
                """
                INSERT INTO planos_aula (curso_id, dia, tema, plano_gerado, usuario_id, planejamento_id)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (curso_id, dia, f"Plano Dia {dia} — {uc}", plano_texto, dono_id, p_id)
            )
        conn.commit()
        print(f"[OK] {len(planos_gerados)} Planos de Aula gerados e salvos para o Planejamento #{p_id} (Curso #{curso_id})!")
    finally:
        release_cursor(conn, cursor)

    # 7. Gera DOCX e PDF consolidados do curso
    try:
        arquivo_docx = f"outputs/curso_{curso_id}.docx"
        arquivo_pdf = f"outputs/curso_{curso_id}.pdf"
        _gerar_documentos_plano(
            arquivo_docx=arquivo_docx,
            arquivo_pdf=arquivo_pdf,
            titulo="PLANO DE AULA",
            subtitulo=f"Curso: {nome_curso} — {uc}",
            conteudo=planos_completos,
            gerar_pdf=True,
            instituicao_fallback=unidade_escolar or "",
        )
    except Exception as e:
        print(f"[AVISO] Erro ao consolidar DOCX/PDF: {e}")

    return redirect(f"/curso/{curso_id}")


# ==============================================================================
# ROTAS DE PLANEJAMENTO EM CASCATA & MÓDULO PEI INCLUSIVO (FASE 6)
# ==============================================================================

@app.route("/planejamento/<int:planejamento_id>")
def visualizar_planejamento_cascata(planejamento_id):
    if "usuario_id" not in session:
        return redirect("/login")

    conn, cursor = get_cursor()
    try:
        cursor.execute(
            """
            SELECT id, nome_curso, unidade_curricular, turma, unidade_escolar,
                   carga_horaria, horas_por_dia, data_inicio, data_fim, total_aulas,
                   arquivo_excel, usuario_id, data_criacao, curso_id, modalidade,
                   tipo_planejamento, planejamento_pai_id, semestre, mes,
                   dados_json, estudante_nome, estudante_necessidades, recursos_tecnologicos_assistivos
            FROM planejamentos_docente
            WHERE id = %s AND usuario_id = %s
            """,
            (planejamento_id, session["usuario_id"])
        )
        plan = cursor.fetchone()
    finally:
        release_cursor(conn, cursor)

    if not plan:
        flash("Planejamento não encontrado.", "warning")
        return redirect("/listar_planejamentos")

    modalidade = plan[14] or ""
    tipo_planejamento = plan[15] or "anual"
    eh_pei = "PEI" in (modalidade or "").upper() or "INDIVIDUALIZADO" in (modalidade or "").upper() or tipo_planejamento == "pei_semestral"

    # Carrega dados_json
    dados_dict = {}
    if plan[19]:
        try:
            dados_dict = json.loads(plan[19])
        except Exception:
            dados_dict = {}

    semestre_selecionado = int(request.args.get("semestre") or plan[17] or 1)
    bimestre_selecionado = int(request.args.get("bimestre") or 1)
    if bimestre_selecionado not in (1, 2, 3, 4):
        bimestre_selecionado = 1
    aba_ativa = request.args.get("aba", "")
    if aba_ativa == "semestral":
        aba_ativa = "bimestral"

    meses_semestre_1 = MESES_LETIVOS_SEMESTRE_1
    meses_semestre_2 = MESES_LETIVOS_SEMESTRE_2
    bimestres_meses = MESES_LETIVOS_POR_BIMESTRE
    meses_letivos_todos = TODOS_MESES_LETIVOS

    mes_ativo = request.args.get("mes_nome")
    mes_numero_ativo = request.args.get("mes")
    if not mes_ativo:
        if eh_pei:
            mes_ativo = "Fevereiro" if semestre_selecionado == 1 else "Agosto"
            mes_numero_ativo = 2 if semestre_selecionado == 1 else 8
        else:
            primeiro_mes_bim = obter_meses_por_bimestre(bimestre_selecionado)[0]
            mes_ativo = primeiro_mes_bim["nome"]
            mes_numero_ativo = primeiro_mes_bim["numero"]
    else:
        mes_numero_ativo = int(mes_numero_ativo or 2)

    texto_anual = dados_dict.get("texto_anual") or dados_dict.get("texto") or "Planejamento Curricular Anual estruturado conforme a BNCC."
    texto_pei_semestral = dados_dict.get("texto_pei_semestral") or dados_dict.get("texto") or texto_anual

    bimestres_salvos = dados_dict.get("bimestres", {})
    texto_bimestral = bimestres_salvos.get(str(bimestre_selecionado), "")
    if not texto_bimestral:
        # Fallback somente leitura caso exista conteúdo legado semestral
        semestres_salvos = dados_dict.get("semestres", {})
        sem_eq = "1" if bimestre_selecionado in (1, 2) else "2"
        texto_bimestral = semestres_salvos.get(sem_eq, "")

    mensais_salvos = dados_dict.get("mensais", {})
    texto_mensal = mensais_salvos.get(mes_ativo, "")
    texto_pei_mensal = mensais_salvos.get(mes_ativo, "")

    conn, cursor = get_cursor()
    planos_mes = []
    contagem_planos_por_mes = {}
    try:
        # Busca contagem de planos por mês
        cursor.execute(
            """
            SELECT pa.mes, pa.tema, COUNT(*)
            FROM planos_aula pa
            WHERE pa.planejamento_id = %s OR (pa.curso_id = %s AND %s IS NOT NULL)
            GROUP BY pa.mes, pa.tema
            """,
            (planejamento_id, plan[13], plan[13])
        )
        for row in cursor.fetchall():
            m_num = row[0]
            t_tema = row[1] or ""
            for m in TODOS_MESES_LETIVOS:
                if m["numero"] == m_num or m["nome"].lower() in t_tema.lower():
                    contagem_planos_por_mes[m["nome"]] = contagem_planos_por_mes.get(m["nome"], 0) + row[2]

        cursor.execute(
            """
            SELECT id, curso_id, dia, tema, plano_gerado, usuario_id, semana, mes
            FROM planos_aula
            WHERE (planejamento_id = %s OR (curso_id = %s AND %s IS NOT NULL))
              AND (mes = %s OR tema ILIKE %s)
            ORDER BY dia ASC
            """,
            (planejamento_id, plan[13], plan[13], mes_numero_ativo, f"%{mes_ativo}%")
        )
        planos_mes = cursor.fetchall()
    finally:
        release_cursor(conn, cursor)

    return render_template(
        "visualizar_planejamento_cascata.html",
        planejamento=plan,
        modalidade=modalidade,
        eh_pei=eh_pei,
        estudante_nome=plan[20],
        estudante_necessidades=plan[21],
        recursos_tecnologicos_assistivos=plan[22],
        semestre_selecionado=semestre_selecionado,
        bimestre_selecionado=bimestre_selecionado,
        mes_ativo=mes_ativo,
        mes_numero_ativo=mes_numero_ativo,
        meses_semestre_1=meses_semestre_1,
        meses_semestre_2=meses_semestre_2,
        bimestres_meses=bimestres_meses,
        meses_letivos_todos=meses_letivos_todos,
        texto_anual=texto_anual,
        texto_bimestral=texto_bimestral,
        texto_mensal=texto_mensal,
        texto_pei_semestral=texto_pei_semestral,
        texto_pei_mensal=texto_pei_mensal,
        planos_mes=planos_mes,
        contagem_planos_por_mes=contagem_planos_por_mes,
        aba_ativa=aba_ativa
    )


def _executar_geracao_bimestral(planejamento_id, bimestre):
    """Desdobra o Planejamento Anual no bimestre informado (1 a 4) e persiste no banco."""
    if bimestre not in (1, 2, 3, 4):
        bimestre = 1

    conn, cursor = get_cursor()
    try:
        cursor.execute(
            "SELECT id, nome_curso, unidade_curricular, modalidade, dados_json FROM planejamentos_docente WHERE id = %s AND usuario_id = %s",
            (planejamento_id, session["usuario_id"])
        )
        plan = cursor.fetchone()
    finally:
        release_cursor(conn, cursor)

    if not plan:
        return None

    dados_dict = {}
    if plan[4]:
        try:
            dados_dict = json.loads(plan[4])
        except Exception:
            dados_dict = {}

    texto_anual = dados_dict.get("texto_anual") or dados_dict.get("texto") or ""
    texto_bim, _ = gerar_planejamento_bimestral_ia(
        planejamento_anual_texto=texto_anual,
        componente=plan[2],
        nome_curso=plan[1],
        bimestre=bimestre
    )

    dados_dict.setdefault("bimestres", {})[str(bimestre)] = texto_bim

    conn, cursor = get_cursor()
    try:
        cursor.execute(
            "UPDATE planejamentos_docente SET dados_json = %s WHERE id = %s",
            (json.dumps(dados_dict, ensure_ascii=False), planejamento_id)
        )
        conn.commit()
    finally:
        release_cursor(conn, cursor)

    return redirect(f"/planejamento/{planejamento_id}?bimestre={bimestre}&aba=bimestral")


@app.route("/planejamento/<int:planejamento_id>/gerar_semestral", methods=["POST"])
def gerar_semestral_planejamento(planejamento_id):
    """Rota legada mantida por compatibilidade, redirecionando para a estrutura bimestral."""
    if "usuario_id" not in session:
        return redirect("/login")

    semestre = int(request.form.get("semestre") or 1)
    bimestre = 1 if semestre == 1 else 3
    resposta = _executar_geracao_bimestral(planejamento_id, bimestre)
    return resposta if resposta is not None else ("Planejamento não encontrado", 404)


@app.route("/planejamento/<int:planejamento_id>/gerar_bimestral", methods=["POST"])
def gerar_bimestral_planejamento(planejamento_id):
    if "usuario_id" not in session:
        return redirect("/login")

    bimestre = int(request.form.get("bimestre") or 1)
    resposta = _executar_geracao_bimestral(planejamento_id, bimestre)
    return resposta if resposta is not None else ("Planejamento não encontrado", 404)


def _montar_conteudo_cascata_para_exportacao(plan, dados_dict, escopo, bimestre=1, mes_nome=""):
    """Monta título, subtítulo e conteúdo formatado para exportação DOCX/PDF de Cascata ou PEI."""
    nome_curso = plan[1] or "Série/Turma"
    componente = plan[2] or "Componente Curricular"
    modalidade = plan[14] or ""
    tipo_planejamento = plan[15] or "anual"
    eh_pei = "PEI" in modalidade.upper() or "INDIVIDUALIZADO" in modalidade.upper() or tipo_planejamento == "pei_semestral"

    cabecalho_pei = ""
    if eh_pei:
        aluno = plan[20] or "Não informado"
        necessidades = plan[21] or "Educação Especial Inclusiva"
        recursos = plan[22] or "DUA / CAA / TEACCH"
        cabecalho_pei = (
            f"PERFIL DO ESTUDANTE (PEI)\n"
            f"- Estudante: {aluno}\n"
            f"- Necessidades Específicas / Diagnóstico: {necessidades}\n"
            f"- Tecnologias Assistivas: {recursos}\n\n"
        )

    bimestres_salvos = dados_dict.get("bimestres", {})
    mensais_salvos = dados_dict.get("mensais", {})
    texto_anual = dados_dict.get("texto_anual") or dados_dict.get("texto") or ""
    texto_pei = dados_dict.get("texto_pei_semestral") or dados_dict.get("texto") or texto_anual

    if escopo == "anual":
        return (
            "PLANEJAMENTO ANUAL",
            f"{nome_curso} — {componente}",
            texto_anual or "Planejamento Anual ainda não gerado.",
        )

    if escopo == "bimestral":
        txt_bim = bimestres_salvos.get(str(bimestre), "")
        return (
            f"PLANEJAMENTO DO {bimestre}º BIMESTRE",
            f"{nome_curso} — {componente}",
            txt_bim or f"Planejamento do {bimestre}º Bimestre ainda não gerado.",
        )

    if escopo in ("mensal", "pei_mensal"):
        txt_mes = mensais_salvos.get(mes_nome, "")
        prefixo = "PEI — INTERVENÇÃO MENSAL" if eh_pei else "PLANEJAMENTO MENSAL"
        return (
            f"{prefixo} ({mes_nome.upper()})",
            f"{nome_curso} — {componente}",
            f"{cabecalho_pei}{txt_mes or f'Planejamento mensal de {mes_nome} ainda não gerado.'}",
        )

    if escopo == "pei_semestral":
        return (
            "PEI — PLANO EDUCACIONAL INDIVIDUALIZADO",
            f"{nome_curso} — {componente}",
            f"{cabecalho_pei}{texto_pei or 'PEI Semestral ainda não gerado.'}",
        )

    # Escopo completo
    blocos = []
    if eh_pei:
        titulo_doc = "PEI — PLANO EDUCACIONAL INDIVIDUALIZADO"
        if cabecalho_pei:
            blocos.append(cabecalho_pei.strip())
        if texto_pei:
            blocos.append(f"PLANEJAMENTO PEI SEMESTRAL\n\n{texto_pei}")
    else:
        titulo_doc = "PLANEJAMENTO PEDAGÓGICO EM CASCATA"
        if texto_anual:
            blocos.append(f"PLANEJAMENTO ANUAL\n\n{texto_anual}")
        for b in ("1", "2", "3", "4"):
            if bimestres_salvos.get(b):
                blocos.append(f"PLANEJAMENTO BIMESTRAL — {b}º BIMESTRE\n\n{bimestres_salvos[b]}")

    for m in TODOS_MESES_LETIVOS:
        if mensais_salvos.get(m["nome"]):
            blocos.append(f"PLANEJAMENTO MENSAL — {m['nome'].upper()}\n\n{mensais_salvos[m['nome']]}")

    conteudo_final = "\n\n".join(blocos) if blocos else "Nenhum conteúdo de planejamento disponível."
    return titulo_doc, f"{nome_curso} — {componente}", conteudo_final


@app.route("/planejamento/<int:planejamento_id>/exportar/<formato>")
def exportar_planejamento_cascata(planejamento_id, formato):
    if "usuario_id" not in session:
        return redirect("/login")

    formato = (formato or "").lower()
    if formato not in ("word", "pdf"):
        return "Formato inválido", 400

    conn, cursor = get_cursor()
    try:
        cursor.execute(
            """
            SELECT id, nome_curso, unidade_curricular, turma, unidade_escolar,
                   carga_horaria, horas_por_dia, data_inicio, data_fim, total_aulas,
                   arquivo_excel, usuario_id, data_criacao, curso_id, modalidade,
                   tipo_planejamento, planejamento_pai_id, semestre, mes,
                   dados_json, estudante_nome, estudante_necessidades, recursos_tecnologicos_assistivos
            FROM planejamentos_docente
            WHERE id = %s AND usuario_id = %s
            """,
            (planejamento_id, session["usuario_id"])
        )
        plan = cursor.fetchone()
    finally:
        release_cursor(conn, cursor)

    if not plan:
        return "Planejamento não encontrado", 404

    dados_dict = {}
    if plan[19]:
        try:
            dados_dict = json.loads(plan[19])
        except Exception:
            dados_dict = {}

    escopo = request.args.get("escopo", "completo").strip().lower()
    if escopo not in ("completo", "anual", "bimestral", "mensal", "pei_semestral", "pei_mensal"):
        escopo = "completo"
    bimestre = int(request.args.get("bimestre") or 1)
    if bimestre not in (1, 2, 3, 4):
        bimestre = 1
    mes_nome = request.args.get("mes_nome", "").strip()

    titulo_doc, subtitulo_doc, conteudo_doc = _montar_conteudo_cascata_para_exportacao(
        plan, dados_dict, escopo, bimestre=bimestre, mes_nome=mes_nome
    )

    sufixo = f"{escopo}_b{bimestre}" if escopo == "bimestral" else (f"{escopo}_{mes_nome.lower()}" if mes_nome and escopo in ("mensal", "pei_mensal") else escopo)
    sufixo_seguro = re.sub(r"[^a-z0-9_-]", "_", sufixo)
    arquivo_docx = f"outputs/planejamento_{planejamento_id}_{sufixo_seguro}.docx"
    arquivo_pdf = f"outputs/planejamento_{planejamento_id}_{sufixo_seguro}.pdf"

    ok = _gerar_documentos_plano(
        arquivo_docx=arquivo_docx,
        arquivo_pdf=arquivo_pdf if formato == "pdf" else None,
        titulo=titulo_doc,
        subtitulo=subtitulo_doc,
        conteudo=conteudo_doc,
        gerar_pdf=(formato == "pdf"),
        instituicao_fallback=plan[4] or "",
    )

    if formato == "pdf":
        if ok and os.path.exists(arquivo_pdf):
            return send_file(
                arquivo_pdf,
                as_attachment=True,
                download_name=f"planejamento_{planejamento_id}_{sufixo_seguro}.pdf",
                mimetype="application/pdf",
            )
        return "Erro ao gerar PDF do planejamento.", 500

    return send_file(
        arquivo_docx,
        as_attachment=True,
        download_name=f"planejamento_{planejamento_id}_{sufixo_seguro}.docx",
        mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )


@app.route("/planejamento/<int:planejamento_id>/gerar_planos_mes", methods=["POST"])
def gerar_planos_mes_planejamento(planejamento_id):
    if "usuario_id" not in session:
        return redirect("/login")

    mes_nome = request.form.get("mes_nome", "Fevereiro")
    mes_numero = int(request.form.get("mes_numero") or 2)
    semestre = int(request.form.get("semestre") or 1)
    bimestre = int(request.form.get("bimestre") or 1)
    dias_semana = request.form.get("dias_semana", "Segunda, Quarta")
    aulas_por_dia = int(request.form.get("aulas_por_dia") or 2)
    duracao_aula = int(request.form.get("duracao_aula") or 50)

    conn, cursor = get_cursor()
    try:
        cursor.execute(
            """
            SELECT id, nome_curso, unidade_curricular, modalidade, curso_id,
                   dados_json, estudante_nome, estudante_necessidades, recursos_tecnologicos_assistivos
            FROM planejamentos_docente
            WHERE id = %s AND usuario_id = %s
            """,
            (planejamento_id, session["usuario_id"])
        )
        plan = cursor.fetchone()
    finally:
        release_cursor(conn, cursor)

    if not plan:
        return "Planejamento não encontrado", 404

    modalidade = plan[3] or ""
    curso_id = plan[4]
    dados_dict = {}
    if plan[5]:
        try:
            dados_dict = json.loads(plan[5])
        except Exception:
            dados_dict = {}

    eh_pei = "PEI" in modalidade.upper() or "INDIVIDUALIZADO" in modalidade.upper()
    aluno_nome = plan[6] or "Estudante"
    necessidades = plan[7] or "Educação Especial Inclusiva"

    # Garante que o curso_id exista
    if not curso_id:
        conn, cursor = get_cursor()
        try:
            nome_c = f"PEI — {aluno_nome} ({plan[2]})" if eh_pei else f"{plan[1]} — {plan[2]}"
            cursor.execute(
                """
                INSERT INTO cursos_plano (nome_curso, carga_horaria, aulas_por_dia, usuario_id)
                VALUES (%s, %s, %s, %s)
                RETURNING id
                """,
                (nome_c, 80, aulas_por_dia, session["usuario_id"])
            )
            curso_id = cursor.fetchone()[0]
            cursor.execute("UPDATE planejamentos_docente SET curso_id = %s WHERE id = %s", (curso_id, planejamento_id))
            conn.commit()
        finally:
            release_cursor(conn, cursor)

    # 1. Gera ou recupera o planejamento mensal
    mensais_dict = dados_dict.setdefault("mensais", {})
    if mes_nome not in mensais_dict:
        if eh_pei:
            txt_mensal, _ = gerar_planejamento_pei_mensal_ia(
                pei_semestral_texto=dados_dict.get("texto_pei_semestral", ""),
                aluno_nome=aluno_nome,
                necessidades=necessidades,
                componente=plan[2],
                mes_nome=mes_nome,
                numero_mes=mes_numero
            )
        else:
            ref_bimestral = (
                dados_dict.get("bimestres", {}).get(str(bimestre))
                or dados_dict.get("semestres", {}).get(str(semestre))
                or dados_dict.get("texto_anual", "")
            )
            txt_mensal, _ = gerar_planejamento_mensal_ia(
                planejamento_semestral_texto=ref_bimestral,
                componente=plan[2],
                nome_curso=plan[1],
                mes_nome=mes_nome,
                numero_mes=mes_numero
            )
        mensais_dict[mes_nome] = txt_mensal

    # 2. Gera os planos de aula daquele mês
    planos_gerados, total_encontros, total_aulas = gerar_planos_aula_do_mes_ia(
        planejamento_mensal_texto=mensais_dict[mes_nome],
        componente=plan[2],
        nome_curso=plan[1],
        mes_nome=mes_nome,
        dias_semana=dias_semana,
        aulas_por_dia=aulas_por_dia,
        duracao_aula=duracao_aula,
        eh_pei=eh_pei,
        dados_pei={"nome": aluno_nome, "necessidades": necessidades}
    )

    # 3. Salva os planos de aula vinculados no banco de dados
    conn, cursor = get_cursor()
    try:
        cursor.execute(
            """
            DELETE FROM planos_aula
            WHERE (planejamento_id = %s OR curso_id = %s)
              AND (mes = %s OR tema ILIKE %s)
            """,
            (planejamento_id, curso_id, mes_numero, f"%{mes_nome}%")
        )

        for p in planos_gerados:
            cursor.execute(
                """
                INSERT INTO planos_aula (
                    curso_id, dia, tema, plano_gerado, usuario_id,
                    planejamento_id, mes, semana, metodologia_inclusiva
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    curso_id,
                    p["dia"],
                    p["tema"],
                    p["conteudo"],
                    session["usuario_id"],
                    planejamento_id,
                    mes_numero,
                    p.get("semana", 1),
                    "DUA/CAA/TEACCH" if eh_pei else None
                )
            )

        cursor.execute(
            "UPDATE planejamentos_docente SET dados_json = %s, dias_semana = %s, aulas_semana = %s WHERE id = %s",
            (json.dumps(dados_dict, ensure_ascii=False), dias_semana, aulas_por_dia, planejamento_id)
        )
        conn.commit()
    finally:
        release_cursor(conn, cursor)

    aba_destino = "pei-aulas" if eh_pei else "mensal"
    return redirect(f"/planejamento/{planejamento_id}?mes={mes_numero}&mes_nome={mes_nome}&semestre={semestre}&bimestre={bimestre}&aba={aba_destino}")


# =========================
# START
# =========================
if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", os.environ.get("SERVER_PORT", 5000))),
        debug=False,
        threaded=True
    )