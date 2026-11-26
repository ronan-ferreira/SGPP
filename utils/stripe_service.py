"""
Serviço de assinaturas, período de teste gratuito (7 dias) e integração Stripe.

Suporta:
- Período de teste gratuito automático ao criar conta (TRIAL_DIAS, padrão 7 dias)
- 4 planos recorrentes via Stripe Checkout (mensal, trimestral, semestral, anual)
- Portal de Cobrança do Cliente Stripe (gerenciar cartão, faturas e cancelamento)
- Webhooks assinados com idempotência persistida no PostgreSQL (tabela stripe_eventos)
"""

from datetime import datetime, timedelta
import math
import os

try:
    import stripe
except ImportError:
    stripe = None

from database.conexao import get_cursor, release_cursor
from utils.email_service import enviar_email_confirmacao_assinatura


PLANOS_STRIPE = {
    "mensal": {
        "slug": "mensal",
        "nome": "Plano Mensal",
        "preco_texto": "R$ 19,90/mês",
        "env_price_id": "STRIPE_PRICE_MENSAL",
    },
    "trimestral": {
        "slug": "trimestral",
        "nome": "Plano Trimestral",
        "preco_texto": "R$ 39,90 a cada 3 meses",
        "env_price_id": "STRIPE_PRICE_TRIMESTRAL",
    },
    "semestral": {
        "slug": "semestral",
        "nome": "Plano Semestral",
        "preco_texto": "R$ 69,90 a cada 6 meses",
        "env_price_id": "STRIPE_PRICE_SEMESTRAL",
    },
    "anual": {
        "slug": "anual",
        "nome": "Plano Anual",
        "preco_texto": "R$ 129,90/ano",
        "env_price_id": "STRIPE_PRICE_ANUAL",
    },
}


def dias_trial_padrao() -> int:
    try:
        return max(1, int(os.getenv("TRIAL_DIAS", "7")))
    except ValueError:
        return 7


def _configurar_stripe() -> bool:
    api_key = os.getenv("STRIPE_SECRET_KEY", "").strip()
    if stripe is None or not api_key:
        return False
    stripe.api_key = api_key
    return True


def stripe_configurado() -> bool:
    """Retorna True se o SDK stripe e STRIPE_SECRET_KEY estiverem disponíveis."""
    return _configurar_stripe()


def obter_price_id(plano_slug: str) -> str:
    info = PLANOS_STRIPE.get((plano_slug or "").strip().lower())
    if not info:
        return ""
    return os.getenv(info["env_price_id"], "").strip()


def identificar_plano_por_price_id(price_id: str | None) -> str | None:
    if not price_id:
        return None
    for slug, info in PLANOS_STRIPE.items():
        if os.getenv(info["env_price_id"], "").strip() == price_id:
            return slug
    return None


def _nome_plano(plano_slug: str | None) -> str:
    if not plano_slug:
        return "Teste Gratuito"
    info = PLANOS_STRIPE.get(plano_slug.lower())
    return info["nome"] if info else plano_slug.capitalize()


def _timestamp_para_datetime(ts) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.utcfromtimestamp(int(ts))
    except Exception:
        return None


def _para_dict(obj):
    """Converte objetos StripeObject (stripe-python v5..v17+) em dict puro recursivamente."""
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, list):
        return [_para_dict(i) for i in obj]
    if hasattr(obj, "to_dict"):
        try:
            obj = obj.to_dict()
        except Exception:
            pass
    if isinstance(obj, dict):
        return {k: _para_dict(v) for k, v in obj.items()}
    return obj


def _extrair_subscription_id_invoice(inv: dict) -> str | None:
    """Extrai o ID da assinatura de uma Invoice (compatível com Stripe API clássica e 2025+)."""
    if not isinstance(inv, dict):
        return None
    sub_id = inv.get("subscription")
    if not sub_id:
        parent = inv.get("parent") or {}
        sub_details = parent.get("subscription_details") or {}
        sub_id = sub_details.get("subscription")
    return sub_id if isinstance(sub_id, str) else (sub_id.get("id") if isinstance(sub_id, dict) else None)


def _extrair_period_end_subscription(sub) -> datetime | None:
    """Extrai current_period_end compatível com versões antigas e novas da API Stripe."""
    sub_dict = _para_dict(sub)
    if not isinstance(sub_dict, dict):
        return None
    val = sub_dict.get("current_period_end")
    if not val:
        itens = (sub_dict.get("items") or {}).get("data") or []
        if itens and isinstance(itens[0], dict):
            val = itens[0].get("current_period_end")
    return _timestamp_para_datetime(val)


def _extrair_price_id_subscription(sub) -> str | None:
    sub_dict = _para_dict(sub)
    if not isinstance(sub_dict, dict):
        return None
    try:
        itens = (sub_dict.get("items") or {}).get("data") or []
        if itens and isinstance(itens[0], dict):
            price = itens[0].get("price")
            if isinstance(price, dict):
                return price.get("id")
            if isinstance(price, str):
                return price
    except Exception:
        pass
    return None


def obter_status_assinatura_usuario(usuario_id: int) -> dict:
    """
    Calcula o status completo de acesso/assinatura do usuário.
    Sempre retorna um dicionário seguro mesmo em caso de dados antigos/parciais.
    """
    padrao_liberado = {
        "acesso_liberado": True,
        "status": "trial",
        "status_label": "Teste Gratuito",
        "plano_tipo": None,
        "plano_nome": "Teste Gratuito (7 dias)",
        "em_trial": True,
        "dias_restantes_trial": dias_trial_padrao(),
        "trial_fim": None,
        "plano_expira_em": None,
        "stripe_customer_id": None,
        "stripe_subscription_id": None,
        "stripe_ativo": stripe_configurado(),
    }

    if not usuario_id:
        return padrao_liberado

    conn, cursor = get_cursor()
    try:
        cursor.execute(
            """
            SELECT
                COALESCE(plano_status, 'trial'),
                plano_tipo,
                trial_fim,
                plano_expira_em,
                stripe_customer_id,
                stripe_subscription_id,
                criado_em
            FROM usuarios
            WHERE id = %s
            """,
            (usuario_id,),
        )
        row = cursor.fetchone()
    finally:
        release_cursor(conn, cursor)

    if not row:
        return padrao_liberado

    plano_status, plano_tipo, trial_fim, plano_expira_em, customer_id, subscription_id, criado_em = row
    status_norm = (plano_status or "trial").strip().lower()
    agora = datetime.now()

    if trial_fim and getattr(trial_fim, "tzinfo", None) is not None:
        trial_fim = trial_fim.replace(tzinfo=None)
    if plano_expira_em and getattr(plano_expira_em, "tzinfo", None) is not None:
        plano_expira_em = plano_expira_em.replace(tzinfo=None)

    if status_norm in ("active", "trialing"):
        return {
            "acesso_liberado": True,
            "status": status_norm,
            "status_label": "Assinatura Ativa" if status_norm == "active" else "Período de Teste (Stripe)",
            "plano_tipo": plano_tipo,
            "plano_nome": _nome_plano(plano_tipo),
            "em_trial": status_norm == "trialing",
            "dias_restantes_trial": 0,
            "trial_fim": trial_fim,
            "plano_expira_em": plano_expira_em,
            "stripe_customer_id": customer_id,
            "stripe_subscription_id": subscription_id,
            "stripe_ativo": stripe_configurado(),
        }

    if status_norm == "trial":
        limite_trial = trial_fim
        if not limite_trial:
            base = criado_em.replace(tzinfo=None) if (criado_em and getattr(criado_em, "tzinfo", None)) else (criado_em or agora)
            limite_trial = base + timedelta(days=dias_trial_padrao())

        if agora < limite_trial:
            segundos = (limite_trial - agora).total_seconds()
            dias_restantes = max(1, int(math.ceil(segundos / 86400.0)))
            return {
                "acesso_liberado": True,
                "status": "trial",
                "status_label": f"Teste Gratuito ({dias_restantes} dia{'s' if dias_restantes != 1 else ''} restante{'s' if dias_restantes != 1 else ''})",
                "plano_tipo": None,
                "plano_nome": f"Teste Gratuito ({dias_trial_padrao()} dias)",
                "em_trial": True,
                "dias_restantes_trial": dias_restantes,
                "trial_fim": limite_trial,
                "plano_expira_em": limite_trial,
                "stripe_customer_id": customer_id,
                "stripe_subscription_id": subscription_id,
                "stripe_ativo": stripe_configurado(),
            }

        # Trial vencido
        status_norm = "expired"

    labels = {
        "expired": "Período de teste encerrado",
        "past_due": "Pagamento pendente",
        "canceled": "Assinatura cancelada",
        "unpaid": "Pagamento não concluído",
        "incomplete_expired": "Checkout expirado",
    }

    return {
        "acesso_liberado": False,
        "status": status_norm,
        "status_label": labels.get(status_norm, "Assinatura inativa"),
        "plano_tipo": plano_tipo,
        "plano_nome": _nome_plano(plano_tipo) if plano_tipo else "Sem plano ativo",
        "em_trial": False,
        "dias_restantes_trial": 0,
        "trial_fim": trial_fim,
        "plano_expira_em": plano_expira_em,
        "stripe_customer_id": customer_id,
        "stripe_subscription_id": subscription_id,
        "stripe_ativo": stripe_configurado(),
    }


def _garantir_customer_stripe(usuario_id: int, email: str, nome: str) -> str:
    """Retorna o stripe_customer_id do usuário, criando-o na Stripe caso não exista."""
    conn, cursor = get_cursor()
    try:
        cursor.execute("SELECT stripe_customer_id FROM usuarios WHERE id = %s", (usuario_id,))
        row = cursor.fetchone()
        if row and row[0]:
            return row[0]

        customer = _para_dict(stripe.Customer.create(
            email=email,
            name=nome,
            metadata={"usuario_id": str(usuario_id)},
        ))
        customer_id = customer["id"]

        cursor.execute(
            "UPDATE usuarios SET stripe_customer_id = %s WHERE id = %s",
            (customer_id, usuario_id),
        )
        conn.commit()
        return customer_id
    finally:
        release_cursor(conn, cursor)


def criar_sessao_checkout(
    usuario_id: int,
    email: str,
    nome: str,
    plano_slug: str,
    base_url: str,
) -> tuple[str | None, str | None]:
    """Cria uma Stripe Checkout Session para assinatura do plano escolhido."""
    if not _configurar_stripe():
        return None, "O pagamento online (Stripe) ainda não foi configurado no servidor."

    slug = (plano_slug or "").strip().lower()
    if slug not in PLANOS_STRIPE:
        return None, "Plano selecionado inválido."

    price_id = obter_price_id(slug)
    if not price_id:
        return None, f"O preço do {PLANOS_STRIPE[slug]['nome']} ainda não foi configurado na Stripe."

    url_base = (os.getenv("APP_BASE_URL", "").strip() or base_url).rstrip("/")

    try:
        customer_id = _garantir_customer_stripe(usuario_id, email, nome)

        checkout_session = _para_dict(stripe.checkout.Session.create(
            mode="subscription",
            customer=customer_id,
            line_items=[{"price": price_id, "quantity": 1}],
            allow_promotion_codes=True,
            locale="pt-BR",
            client_reference_id=str(usuario_id),
            metadata={
                "usuario_id": str(usuario_id),
                "plano_tipo": slug,
            },
            subscription_data={
                "metadata": {
                    "usuario_id": str(usuario_id),
                    "plano_tipo": slug,
                }
            },
            success_url=f"{url_base}/assinatura/sucesso?session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{url_base}/precos?checkout_cancelado=1",
        ))
        return checkout_session["url"], None
    except Exception as e:
        print(f"[STRIPE] Erro ao criar Checkout Session (user #{usuario_id}, plano={slug}): {e}")
        return None, "Não foi possível iniciar o pagamento na Stripe. Tente novamente em instantes."


def criar_sessao_portal_cliente(usuario_id: int, base_url: str) -> tuple[str | None, str | None]:
    """Abre o Portal do Cliente da Stripe (trocar cartão, baixar faturas, cancelar)."""
    if not _configurar_stripe():
        return None, "A integração com a Stripe não está configurada."

    conn, cursor = get_cursor()
    try:
        cursor.execute("SELECT stripe_customer_id FROM usuarios WHERE id = %s", (usuario_id,))
        row = cursor.fetchone()
    finally:
        release_cursor(conn, cursor)

    customer_id = row[0] if row else None
    if not customer_id:
        return None, "Você ainda não possui uma assinatura vinculada na Stripe."

    url_base = (os.getenv("APP_BASE_URL", "").strip() or base_url).rstrip("/")

    try:
        portal = _para_dict(stripe.billing_portal.Session.create(
            customer=customer_id,
            return_url=f"{url_base}/perfil",
        ))
        return portal["url"], None
    except Exception as e:
        print(f"[STRIPE] Erro ao criar Billing Portal (user #{usuario_id}): {e}")
        return None, "Não foi possível abrir o portal de assinatura. Verifique se o Customer Portal está ativado na Stripe."


def _atualizar_assinatura_bd(
    usuario_id: int | None,
    customer_id: str | None,
    subscription_id: str | None,
    status: str | None,
    plano_tipo: str | None = None,
    expira_em: datetime | None = None,
    enviar_email: bool = False,
) -> bool:
    """Atualiza os campos de assinatura na tabela usuarios a partir de usuario_id ou stripe_customer_id."""
    conn, cursor = get_cursor()
    try:
        alvo_id = usuario_id
        email_user = None
        nome_user = None

        if alvo_id:
            cursor.execute("SELECT id, nome, email FROM usuarios WHERE id = %s", (alvo_id,))
            row = cursor.fetchone()
        elif customer_id:
            cursor.execute("SELECT id, nome, email FROM usuarios WHERE stripe_customer_id = %s", (customer_id,))
            row = cursor.fetchone()
        elif subscription_id:
            cursor.execute("SELECT id, nome, email FROM usuarios WHERE stripe_subscription_id = %s", (subscription_id,))
            row = cursor.fetchone()
        else:
            row = None

        if not row:
            print(f"[STRIPE] Usuário não encontrado para sync (uid={usuario_id}, cus={customer_id}, sub={subscription_id})")
            return False

        alvo_id, nome_user, email_user = row

        campos = []
        valores = []
        if status:
            campos.append("plano_status = %s")
            valores.append(status)
        if plano_tipo:
            campos.append("plano_tipo = %s")
            valores.append(plano_tipo)
        if expira_em is not None:
            campos.append("plano_expira_em = %s")
            valores.append(expira_em)
        if customer_id:
            campos.append("stripe_customer_id = %s")
            valores.append(customer_id)
        if subscription_id:
            campos.append("stripe_subscription_id = %s")
            valores.append(subscription_id)

        if not campos:
            return False

        valores.append(alvo_id)
        cursor.execute(f"UPDATE usuarios SET {', '.join(campos)} WHERE id = %s", tuple(valores))
        conn.commit()
        print(f"[STRIPE] Assinatura atualizada: usuario #{alvo_id} -> status={status}, plano={plano_tipo}")

        if enviar_email and email_user and status in ("active", "trialing"):
            try:
                renovacao_str = expira_em.strftime("%d/%m/%Y") if expira_em else ""
                enviar_email_confirmacao_assinatura(
                    email_user,
                    nome_user or "",
                    _nome_plano(plano_tipo),
                    proxima_renovacao=renovacao_str,
                    app_url=os.getenv("APP_BASE_URL", ""),
                )
            except Exception as e_mail:
                print(f"[STRIPE] Aviso ao enviar e-mail de confirmação: {e_mail}")

        return True
    finally:
        release_cursor(conn, cursor)


def sincronizar_checkout_session(session_id: str, usuario_id_esperado: int | None = None) -> tuple[bool, dict]:
    """
    Confirma diretamente na Stripe uma sessão de checkout concluída ao retornar
    em /assinatura/sucesso, liberando o acesso imediatamente mesmo antes do webhook.
    """
    if not _configurar_stripe() or not session_id:
        return False, {}

    try:
        sessao = _para_dict(stripe.checkout.Session.retrieve(session_id, expand=["subscription"]))
        status_pagamento = sessao.get("payment_status")
        status_sessao = sessao.get("status")

        metadata = sessao.get("metadata") or {}
        uid_meta = metadata.get("usuario_id") or sessao.get("client_reference_id")
        uid = int(uid_meta) if uid_meta and str(uid_meta).isdigit() else usuario_id_esperado

        if usuario_id_esperado and uid and uid != usuario_id_esperado:
            return False, {"erro": "Sessão pertence a outro usuário."}

        if status_sessao != "complete" and status_pagamento not in ("paid", "no_payment_required"):
            return False, {"status": status_sessao, "payment_status": status_pagamento}

        customer_id = sessao.get("customer")
        sub = sessao.get("subscription")
        sub_id = sub.get("id") if isinstance(sub, dict) else (sub if isinstance(sub, str) else None)
        sub_status = (sub.get("status") if isinstance(sub, dict) else None) or "active"
        expira_em = _extrair_period_end_subscription(sub)
        plano_tipo = metadata.get("plano_tipo") or identificar_plano_por_price_id(_extrair_price_id_subscription(sub))

        _atualizar_assinatura_bd(
            usuario_id=uid,
            customer_id=customer_id,
            subscription_id=sub_id,
            status=sub_status,
            plano_tipo=plano_tipo,
            expira_em=expira_em,
            enviar_email=False,  # O webhook cuida do envio único de e-mail para evitar duplicidade
        )

        return True, {
            "plano_tipo": plano_tipo,
            "plano_nome": _nome_plano(plano_tipo),
            "status": sub_status,
            "expira_em": expira_em,
        }
    except Exception as e:
        print(f"[STRIPE] Erro ao sincronizar checkout session '{session_id}': {e}")
        return False, {"erro": str(e)}


def _registrar_evento_idempotente(event_id: str, event_type: str) -> bool:
    """Registra o event_id no banco. Retorna False se já tiver sido processado anteriormente."""
    conn, cursor = get_cursor()
    try:
        cursor.execute(
            """
            INSERT INTO stripe_eventos (event_id, event_type)
            VALUES (%s, %s)
            ON CONFLICT (event_id) DO NOTHING
            RETURNING event_id
            """,
            (event_id, event_type),
        )
        inserido = cursor.fetchone() is not None
        conn.commit()
        return inserido
    except Exception as e:
        print(f"[STRIPE] Aviso no registro de idempotência ({event_id}): {e}")
        return True
    finally:
        release_cursor(conn, cursor)


def processar_webhook_stripe(payload_bytes: bytes, sig_header: str) -> tuple[bool, str, int]:
    """Valida a assinatura do webhook da Stripe e sincroniza o estado da assinatura no banco."""
    if not _configurar_stripe():
        return False, "Stripe não configurada", 503

    webhook_secret = os.getenv("STRIPE_WEBHOOK_SECRET", "").strip()
    if not webhook_secret:
        return False, "STRIPE_WEBHOOK_SECRET não configurada", 500

    try:
        evento = _para_dict(stripe.Webhook.construct_event(payload_bytes, sig_header, webhook_secret))
    except ValueError:
        return False, "Payload inválido", 400
    except Exception as e:
        print(f"[STRIPE] Assinatura de webhook inválida: {e}")
        return False, "Assinatura inválida", 400

    event_id = evento.get("id")
    event_type = evento.get("type")
    obj = (evento.get("data") or {}).get("object") or {}

    if event_id and not _registrar_evento_idempotente(event_id, event_type or "unknown"):
        return True, "Evento já processado", 200

    try:
        if event_type == "checkout.session.completed":
            metadata = obj.get("metadata") or {}
            uid_raw = metadata.get("usuario_id") or obj.get("client_reference_id")
            uid = int(uid_raw) if uid_raw and str(uid_raw).isdigit() else None
            customer_id = obj.get("customer")
            sub_id = obj.get("subscription")
            plano_tipo = metadata.get("plano_tipo")
            expira_em = None
            sub_status = "active"

            if sub_id:
                try:
                    sub = _para_dict(stripe.Subscription.retrieve(sub_id))
                    sub_status = sub.get("status") or "active"
                    expira_em = _extrair_period_end_subscription(sub)
                    if not plano_tipo:
                        plano_tipo = identificar_plano_por_price_id(_extrair_price_id_subscription(sub))
                except Exception as e_sub:
                    print(f"[STRIPE] Aviso ao consultar subscription {sub_id}: {e_sub}")

            _atualizar_assinatura_bd(
                usuario_id=uid,
                customer_id=customer_id,
                subscription_id=sub_id,
                status=sub_status,
                plano_tipo=plano_tipo,
                expira_em=expira_em,
                enviar_email=True,
            )

        elif event_type in ("customer.subscription.created", "customer.subscription.updated"):
            metadata = obj.get("metadata") or {}
            uid_raw = metadata.get("usuario_id")
            uid = int(uid_raw) if uid_raw and str(uid_raw).isdigit() else None
            customer_id = obj.get("customer")
            sub_id = obj.get("id")
            sub_status = obj.get("status")
            expira_em = _extrair_period_end_subscription(obj)
            plano_tipo = metadata.get("plano_tipo") or identificar_plano_por_price_id(_extrair_price_id_subscription(obj))

            _atualizar_assinatura_bd(
                usuario_id=uid,
                customer_id=customer_id,
                subscription_id=sub_id,
                status=sub_status,
                plano_tipo=plano_tipo,
                expira_em=expira_em,
                enviar_email=False,
            )

        elif event_type == "customer.subscription.deleted":
            metadata = obj.get("metadata") or {}
            uid_raw = metadata.get("usuario_id")
            uid = int(uid_raw) if uid_raw and str(uid_raw).isdigit() else None
            _atualizar_assinatura_bd(
                usuario_id=uid,
                customer_id=obj.get("customer"),
                subscription_id=obj.get("id"),
                status="canceled",
                expira_em=_extrair_period_end_subscription(obj),
            )

        elif event_type == "invoice.payment_failed":
            _atualizar_assinatura_bd(
                usuario_id=None,
                customer_id=obj.get("customer"),
                subscription_id=_extrair_subscription_id_invoice(obj),
                status="past_due",
            )

        elif event_type == "invoice.payment_succeeded":
            sub_id = _extrair_subscription_id_invoice(obj)
            expira_em = None
            plano_tipo = None
            if sub_id:
                try:
                    sub = _para_dict(stripe.Subscription.retrieve(sub_id))
                    expira_em = _extrair_period_end_subscription(sub)
                    plano_tipo = (sub.get("metadata") or {}).get("plano_tipo") or identificar_plano_por_price_id(_extrair_price_id_subscription(sub))
                except Exception:
                    pass
            _atualizar_assinatura_bd(
                usuario_id=None,
                customer_id=obj.get("customer"),
                subscription_id=sub_id,
                status="active",
                plano_tipo=plano_tipo,
                expira_em=expira_em,
            )

        return True, "ok", 200
    except Exception as e:
        print(f"[STRIPE] Erro ao processar webhook '{event_type}': {e}")
        return False, "Erro interno ao processar webhook", 500
