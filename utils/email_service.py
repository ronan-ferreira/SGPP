"""
Serviço de envio de e-mails transacionais do SGPP.

Ordem de prioridade de envio:
1. Resend HTTP API (recomendado em produção no Render — sem bloqueio de portas SMTP)
2. SMTP tradicional (fallback compatível com configurações locais/legadas)
3. Modo de desenvolvimento (simulação no console quando nenhum provedor está configurado)
"""

import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
import requests


RESEND_API_URL = "https://api.resend.com/emails"


def resend_configurado() -> bool:
    """Verifica se a API key do Resend foi fornecida no ambiente."""
    return bool(os.getenv("RESEND_API_KEY", "").strip())


def smtp_configurado() -> bool:
    """Verifica se os parâmetros mínimos de SMTP foram fornecidos no .env."""
    servidor = os.getenv("SMTP_SERVER", "").strip()
    usuario = os.getenv("SMTP_USER", "").strip()
    senha = os.getenv("SMTP_PASSWORD", "").strip()
    return bool(servidor and usuario and senha)


def email_configurado() -> bool:
    """Retorna True se Resend ou SMTP estiverem configurados."""
    return resend_configurado() or smtp_configurado()


def _obter_remetente() -> tuple[str, str]:
    nome = (
        os.getenv("RESEND_FROM_NAME")
        or os.getenv("SMTP_FROM_NAME")
        or "SGPP - Gestão Pedagógica"
    ).strip()
    email = (
        os.getenv("RESEND_FROM_EMAIL")
        or os.getenv("SMTP_FROM_EMAIL")
        or os.getenv("SMTP_USER")
        or "onboarding@resend.dev"
    ).strip()
    return nome, email


def _enviar_via_resend(
    destinatario_email: str,
    assunto: str,
    texto_plano: str,
    corpo_html: str,
) -> tuple[bool, str]:
    api_key = os.getenv("RESEND_API_KEY", "").strip()
    remetente_nome, remetente_email = _obter_remetente()

    payload = {
        "from": f"{remetente_nome} <{remetente_email}>",
        "to": [destinatario_email],
        "subject": assunto,
        "html": corpo_html,
        "text": texto_plano,
    }
    reply_to = os.getenv("RESEND_REPLY_TO", "").strip()
    if reply_to:
        payload["reply_to"] = reply_to

    try:
        resp = requests.post(
            RESEND_API_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=15,
        )
        if resp.status_code in (200, 201):
            print(f"[EMAIL/RESEND] E-mail '{assunto}' enviado com sucesso para: {destinatario_email}")
            return True, "enviado"
        erro = f"HTTP {resp.status_code}: {resp.text[:250]}"
        print(f"[EMAIL/RESEND] Erro ao enviar para {destinatario_email}: {erro}")
        return False, erro
    except Exception as e:
        print(f"[EMAIL/RESEND] Exceção ao enviar para {destinatario_email}: {e}")
        return False, str(e)


def _enviar_via_smtp(
    destinatario_email: str,
    assunto: str,
    texto_plano: str,
    corpo_html: str,
) -> tuple[bool, str]:
    servidor_host = os.getenv("SMTP_SERVER", "").strip()
    servidor_porta = int(os.getenv("SMTP_PORT", "587"))
    usuario = os.getenv("SMTP_USER", "").strip()
    senha = os.getenv("SMTP_PASSWORD", "").strip()
    usar_tls = os.getenv("SMTP_USE_TLS", "true").lower() in ("1", "true", "yes")
    usar_ssl = os.getenv("SMTP_USE_SSL", "false").lower() in ("1", "true", "yes")
    remetente_nome, remetente_email = _obter_remetente()

    mensagem = MIMEMultipart("alternative")
    mensagem["Subject"] = assunto
    mensagem["From"] = f"{remetente_nome} <{remetente_email}>"
    mensagem["To"] = destinatario_email

    mensagem.attach(MIMEText(texto_plano, "plain", "utf-8"))
    mensagem.attach(MIMEText(corpo_html, "html", "utf-8"))

    try:
        if usar_ssl:
            with smtplib.SMTP_SSL(servidor_host, servidor_porta, timeout=12) as servidor:
                servidor.login(usuario, senha)
                servidor.sendmail(remetente_email, [destinatario_email], mensagem.as_string())
        else:
            with smtplib.SMTP(servidor_host, servidor_porta, timeout=12) as servidor:
                if usar_tls:
                    servidor.starttls()
                servidor.login(usuario, senha)
                servidor.sendmail(remetente_email, [destinatario_email], mensagem.as_string())

        print(f"[EMAIL/SMTP] E-mail '{assunto}' enviado com sucesso para: {destinatario_email}")
        return True, "enviado"
    except Exception as e:
        print(f"[EMAIL/SMTP] Erro ao enviar para {destinatario_email}: {e}")
        return False, str(e)


def enviar_email(
    destinatario_email: str,
    assunto: str,
    texto_plano: str,
    corpo_html: str,
) -> tuple[bool, str]:
    """Despacha um e-mail via Resend (prioritário) ou SMTP."""
    destinatario_email = (destinatario_email or "").strip()
    if not destinatario_email:
        return False, "Destinatário vazio"

    if resend_configurado():
        return _enviar_via_resend(destinatario_email, assunto, texto_plano, corpo_html)

    if smtp_configurado():
        return _enviar_via_smtp(destinatario_email, assunto, texto_plano, corpo_html)

    return True, "dev_mode"


def enviar_email_codigo_recuperacao(
    destinatario_email: str,
    destinatario_nome: str,
    codigo: str,
) -> tuple[bool, str]:
    """
    Envia o código OTP de 6 dígitos para o e-mail do usuário.
    Se nem Resend nem SMTP estiverem configurados, registra no console/log
    em modo de desenvolvimento sem quebrar o fluxo.
    """
    destinatario_email = destinatario_email.strip()
    primeiro_nome = (destinatario_nome.strip().split()[0] if destinatario_nome else "Professor(a)").title()

    if not email_configurado():
        print("\n" + "=" * 65)
        print("  [SGPP SEGURANÇA] SIMULAÇÃO DE E-MAIL (RESEND/SMTP NÃO CONFIGURADO)")
        print(f"  Para: {destinatario_email} ({primeiro_nome})")
        print(f"  CÓDIGO DE RECUPERAÇÃO OTP (6 DÍGITOS): >>>  {codigo}  <<<")
        print("  Validade: 15 minutos")
        print("  Para envio real, configure RESEND_API_KEY (ou SMTP_*) no .env")
        print("=" * 65 + "\n")
        return True, "dev_mode"

    assunto = f"{codigo} é o seu código de recuperação de senha • SGPP"

    texto_plano = (
        f"Olá, {primeiro_nome}!\n\n"
        f"Recebemos uma solicitação para redefinir a senha da sua conta no SGPP.\n\n"
        f"Seu código de verificação é: {codigo}\n\n"
        f"Este código é válido por 15 minutos. Caso não tenha feito esta solicitação, "
        f"ignore este e-mail por segurança.\n\n"
        f"— Equipe SGPP"
    )

    corpo_html = f"""<!DOCTYPE html>
<html lang="pt-br">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Código de Recuperação</title>
</head>
<body style="margin: 0; padding: 0; background-color: #f4f6f9; font-family: 'Segoe UI', -apple-system, BlinkMacSystemFont, Roboto, Helvetica, Arial, sans-serif; color: #212529;">
    <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background-color: #f4f6f9; padding: 30px 15px;">
        <tr>
            <td align="center">
                <table role="presentation" width="100%" style="max-width: 520px; background-color: #ffffff; border-radius: 12px; overflow: hidden; box-shadow: 0 4px 16px rgba(0,0,0,0.06); border: 1px solid #e2e8f0;">
                    <tr>
                        <td style="background: linear-gradient(135deg, #0a3871 0%, #0d6efd 100%); padding: 28px 24px; text-align: center;">
                            <h1 style="margin: 0; color: #ffffff; font-size: 24px; font-weight: 700; letter-spacing: 0.5px;">SGPP</h1>
                            <p style="margin: 4px 0 0 0; color: rgba(255,255,255,0.85); font-size: 13px;">Sistema de Gestão e Planejamento Pedagógico</p>
                        </td>
                    </tr>
                    <tr>
                        <td style="padding: 32px 28px;">
                            <h2 style="margin: 0 0 12px 0; color: #1e293b; font-size: 20px; font-weight: 600;">Olá, {primeiro_nome}!</h2>
                            <p style="margin: 0 0 18px 0; color: #475569; font-size: 15px; line-height: 1.5;">
                                Recebemos uma solicitação para redefinir a senha da sua conta de acesso ao SGPP.
                            </p>
                            <p style="margin: 0 0 10px 0; color: #475569; font-size: 14px; font-weight: 500;">
                                Utilize o código de verificação de 6 dígitos abaixo para continuar:
                            </p>
                            <div style="background-color: #f8fafc; border: 2px dashed #0d6efd; border-radius: 10px; padding: 20px; text-align: center; margin: 22px 0;">
                                <span style="font-family: 'Courier New', Courier, monospace; font-size: 36px; font-weight: 800; letter-spacing: 10px; color: #0a3871; display: inline-block;">{codigo}</span>
                                <div style="margin-top: 8px; color: #dc3545; font-size: 12px; font-weight: 600;">
                                    ⏱ Código válido por 15 minutos
                                </div>
                            </div>
                            <p style="margin: 0 0 14px 0; color: #64748b; font-size: 13px; line-height: 1.5;">
                                Digite este código no formulário de redefinição de senha na tela do sistema junto com sua nova senha.
                            </p>
                            <div style="background-color: #fff8e1; border-left: 4px solid #f59e0b; padding: 12px 14px; border-radius: 4px; margin-top: 24px;">
                                <p style="margin: 0; color: #92400e; font-size: 12.5px; line-height: 1.4;">
                                    <strong>Atenção de segurança:</strong> Se você não solicitou a redefinição de senha, desconsidere este e-mail. Sua senha permanecerá inalterada e ninguém terá acesso sem este código.
                                </p>
                            </div>
                        </td>
                    </tr>
                    <tr>
                        <td style="background-color: #f8fafc; padding: 18px 24px; text-align: center; border-top: 1px solid #e2e8f0;">
                            <p style="margin: 0; color: #94a3b8; font-size: 12px;">
                                SGPP &bull; Ambiente Docente Seguro
                            </p>
                        </td>
                    </tr>
                </table>
            </td>
        </tr>
    </table>
</body>
</html>"""

    return enviar_email(destinatario_email, assunto, texto_plano, corpo_html)


def enviar_email_boas_vindas(
    destinatario_email: str,
    destinatario_nome: str,
    dias_trial: int = 7,
    app_url: str = "",
) -> tuple[bool, str]:
    """Envia e-mail de boas-vindas ao novo professor informando os dias de teste gratuito."""
    if not email_configurado():
        return True, "dev_mode"

    primeiro_nome = (destinatario_nome.strip().split()[0] if destinatario_nome else "Professor(a)").title()
    link_painel = f"{app_url.rstrip('/')}/dashboard" if app_url else ""

    assunto = f"Bem-vindo(a) ao SGPP! Seus {dias_trial} dias grátis começaram"
    texto_plano = (
        f"Olá, {primeiro_nome}!\n\n"
        f"Sua conta no SGPP foi criada com sucesso.\n"
        f"Você tem {dias_trial} dias de acesso gratuito e completo a todos os módulos: "
        f"Planejamento Docente, Planos de Aula, Atividades Avaliativas e Repositório Pessoal.\n\n"
        + (f"Acesse seu painel: {link_painel}\n\n" if link_painel else "")
        + "Bons planejamentos!\n— Equipe SGPP"
    )

    botao_html = (
        f'<div style="text-align: center; margin: 26px 0;">'
        f'<a href="{link_painel}" style="display: inline-block; background: #0d6efd; color: #ffffff; '
        f'text-decoration: none; font-weight: 700; padding: 12px 26px; border-radius: 8px; font-size: 15px;">'
        f'Acessar meu Painel SGPP</a></div>'
        if link_painel else ""
    )

    corpo_html = f"""<!DOCTYPE html>
<html lang="pt-br">
<head><meta charset="UTF-8"><title>Bem-vindo ao SGPP</title></head>
<body style="margin: 0; padding: 0; background-color: #f4f6f9; font-family: 'Segoe UI', Roboto, Arial, sans-serif; color: #1e293b;">
    <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background-color: #f4f6f9; padding: 30px 15px;">
        <tr>
            <td align="center">
                <table role="presentation" width="100%" style="max-width: 540px; background-color: #ffffff; border-radius: 12px; overflow: hidden; border: 1px solid #e2e8f0;">
                    <tr>
                        <td style="background: linear-gradient(135deg, #0a3871 0%, #0d6efd 100%); padding: 28px 24px; text-align: center;">
                            <h1 style="margin: 0; color: #ffffff; font-size: 24px; font-weight: 700;">SGPP</h1>
                            <p style="margin: 4px 0 0 0; color: rgba(255,255,255,0.88); font-size: 13px;">Sistema de Gestão e Planejamento Pedagógico</p>
                        </td>
                    </tr>
                    <tr>
                        <td style="padding: 32px 28px;">
                            <h2 style="margin: 0 0 12px 0; font-size: 20px;">Bem-vindo(a), {primeiro_nome}!</h2>
                            <p style="margin: 0 0 16px 0; color: #475569; font-size: 15px; line-height: 1.6;">
                                Sua conta foi ativada com <strong>{dias_trial} dias de teste gratuito</strong> com acesso completo a todos os módulos do sistema.
                            </p>
                            <ul style="color: #475569; font-size: 14px; line-height: 1.8; padding-left: 18px;">
                                <li>Planejamento Docente com Taxonomia de Bloom (Excel e Word)</li>
                                <li>Planos de Aula estruturados e alinhados à BNCC ou Plano de Curso</li>
                                <li>Gerador híbrido de Atividades Avaliativas (Word e PDF)</li>
                                <li>Repositório pessoal e privado de questões</li>
                            </ul>
                            {botao_html}
                            <p style="margin: 0; color: #64748b; font-size: 13px;">
                                Ao final do período gratuito, você poderá escolher o plano ideal diretamente na plataforma.
                            </p>
                        </td>
                    </tr>
                    <tr>
                        <td style="background-color: #f8fafc; padding: 16px 24px; text-align: center; border-top: 1px solid #e2e8f0;">
                            <p style="margin: 0; color: #94a3b8; font-size: 12px;">SGPP &bull; Ambiente Docente Seguro</p>
                        </td>
                    </tr>
                </table>
            </td>
        </tr>
    </table>
</body>
</html>"""

    return enviar_email(destinatario_email, assunto, texto_plano, corpo_html)


def enviar_email_confirmacao_assinatura(
    destinatario_email: str,
    destinatario_nome: str,
    plano_nome: str,
    proxima_renovacao: str = "",
    app_url: str = "",
) -> tuple[bool, str]:
    """Envia confirmação quando a assinatura Stripe é ativada com sucesso."""
    if not email_configurado():
        return True, "dev_mode"

    primeiro_nome = (destinatario_nome.strip().split()[0] if destinatario_nome else "Professor(a)").title()
    link_perfil = f"{app_url.rstrip('/')}/perfil" if app_url else ""

    assunto = f"Assinatura confirmada: {plano_nome} • SGPP"
    texto_plano = (
        f"Olá, {primeiro_nome}!\n\n"
        f"Sua assinatura do {plano_nome} no SGPP foi confirmada e está ativa.\n"
        + (f"Próxima renovação prevista: {proxima_renovacao}\n" if proxima_renovacao else "")
        + (f"Você pode gerenciar ou cancelar sua assinatura a qualquer momento em: {link_perfil}\n\n" if link_perfil else "\n")
        + "Obrigado por confiar no SGPP!\n— Equipe SGPP"
    )

    bloco_renovacao = (
        f'<p style="margin: 8px 0 0 0; color: #475569; font-size: 14px;"><strong>Próxima renovação:</strong> {proxima_renovacao}</p>'
        if proxima_renovacao else ""
    )

    corpo_html = f"""<!DOCTYPE html>
<html lang="pt-br">
<head><meta charset="UTF-8"><title>Assinatura Confirmada</title></head>
<body style="margin: 0; padding: 0; background-color: #f4f6f9; font-family: 'Segoe UI', Roboto, Arial, sans-serif; color: #1e293b;">
    <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background-color: #f4f6f9; padding: 30px 15px;">
        <tr>
            <td align="center">
                <table role="presentation" width="100%" style="max-width: 540px; background-color: #ffffff; border-radius: 12px; overflow: hidden; border: 1px solid #e2e8f0;">
                    <tr>
                        <td style="background: linear-gradient(135deg, #059669 0%, #10b981 100%); padding: 28px 24px; text-align: center;">
                            <h1 style="margin: 0; color: #ffffff; font-size: 24px; font-weight: 700;">Assinatura Ativa!</h1>
                            <p style="margin: 4px 0 0 0; color: rgba(255,255,255,0.9); font-size: 13px;">SGPP &bull; Gestão e Planejamento Pedagógico</p>
                        </td>
                    </tr>
                    <tr>
                        <td style="padding: 32px 28px;">
                            <h2 style="margin: 0 0 12px 0; font-size: 20px;">Obrigado, {primeiro_nome}!</h2>
                            <p style="margin: 0 0 16px 0; color: #475569; font-size: 15px; line-height: 1.6;">
                                Seu pagamento foi confirmado e o seu acesso completo ao SGPP está liberado.
                            </p>
                            <div style="background: #f0fdf4; border: 1px solid #bbf7d0; border-radius: 10px; padding: 16px 18px; margin: 18px 0;">
                                <p style="margin: 0; color: #166534; font-size: 15px;"><strong>Plano:</strong> {plano_nome}</p>
                                {bloco_renovacao}
                            </div>
                            <p style="margin: 16px 0 0 0; color: #64748b; font-size: 13px;">
                                Você pode gerenciar faturas, atualizar o cartão ou cancelar a qualquer momento na página <strong>Meu Perfil</strong>.
                            </p>
                        </td>
                    </tr>
                </table>
            </td>
        </tr>
    </table>
</body>
</html>"""

    return enviar_email(destinatario_email, assunto, texto_plano, corpo_html)
