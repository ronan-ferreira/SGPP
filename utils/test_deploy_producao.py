"""
Testes automatizados das adaptações de produção:
- Conexão PostgreSQL / Supabase (DATABASE_URL + SSL)
- Health check (/health)
- Fluxo de Trial de 7 dias (ativo vs expirado -> redireciona para /precos)
- Integração Stripe (checkout, portal, webhook sem assinatura válida, página /precos)
- Serviço de e-mail Resend (detecção e payload) e Storage Supabase (fallback local)
"""

from datetime import datetime, timedelta
import os
import unittest
from unittest.mock import patch, MagicMock
import uuid

from app import app
from database.conexao import get_cursor, release_cursor, _montar_parametros_conexao
from utils.email_service import enviar_email_codigo_recuperacao, resend_configurado
from utils.storage_service import garantir_arquivo_local, storage_configurado
from utils.stripe_service import obter_status_assinatura_usuario, processar_webhook_stripe


class TestDeployProducao(unittest.TestCase):

    def setUp(self):
        self.client = app.test_client()
        self.app_context = app.app_context()
        self.app_context.push()
        self.emails_teste = []

    def tearDown(self):
        if self.emails_teste:
            conn, cur = get_cursor()
            try:
                for email in self.emails_teste:
                    cur.execute("DELETE FROM usuarios WHERE LOWER(email) = LOWER(%s)", (email,))
                conn.commit()
            finally:
                release_cursor(conn, cur)
        self.app_context.pop()

    def _criar_usuario_teste(self, trial_delta_days=7, plano_status="trial"):
        email = f"deploy_test_{uuid.uuid4().hex[:8]}@exemplo.com"
        self.emails_teste.append(email)
        trial_fim = datetime.now() + timedelta(days=trial_delta_days)
        conn, cur = get_cursor()
        try:
            cur.execute(
                """
                INSERT INTO usuarios (nome, email, senha, perfil, tutorial_visto, trial_fim, plano_status)
                VALUES (%s, %s, %s, 'professor', TRUE, %s, %s)
                RETURNING id
                """,
                ("Docente Deploy", email, "SenhaHashTeste", trial_fim, plano_status),
            )
            uid = cur.fetchone()[0]
            conn.commit()
            return uid, email
        finally:
            release_cursor(conn, cur)

    def test_01_database_url_supabase_aplica_ssl_require(self):
        with patch.dict(
            os.environ,
            {"DATABASE_URL": "postgres://postgres.abc:secret@aws-0-sa-east-1.pooler.supabase.com:6543/postgres"},
            clear=False,
        ):
            params = _montar_parametros_conexao()
            self.assertTrue(params["dsn"].startswith("postgresql://"))
            self.assertEqual(params["sslmode"], "require")

    def test_02_health_endpoint_ok(self):
        resp = self.client.get("/health")
        self.assertEqual(resp.status_code, 200)
        dados = resp.get_json()
        self.assertEqual(dados["status"], "ok")
        self.assertEqual(dados["database"], "connected")

    def test_03_trial_ativo_libera_acesso_e_trial_expirado_bloqueia(self):
        uid_ativo, _ = self._criar_usuario_teste(trial_delta_days=5, plano_status="trial")
        status_ativo = obter_status_assinatura_usuario(uid_ativo)
        self.assertTrue(status_ativo["acesso_liberado"])
        self.assertTrue(status_ativo["em_trial"])
        self.assertGreaterEqual(status_ativo["dias_restantes_trial"], 5)

        with self.client.session_transaction() as sess:
            sess["usuario_id"] = uid_ativo
            sess["nome"] = "Docente Deploy"
            sess["perfil"] = "professor"

        r_dash = self.client.get("/dashboard")
        self.assertEqual(r_dash.status_code, 200)

        # Usuário com trial vencido há 2 dias
        uid_vencido, _ = self._criar_usuario_teste(trial_delta_days=-2, plano_status="trial")
        status_vencido = obter_status_assinatura_usuario(uid_vencido)
        self.assertFalse(status_vencido["acesso_liberado"])
        self.assertEqual(status_vencido["status"], "expired")

        with self.client.session_transaction() as sess:
            sess["usuario_id"] = uid_vencido
            sess["nome"] = "Docente Deploy"
            sess["perfil"] = "professor"

        # Dashboard redireciona para /precos?trial_expirado=1
        r_bloq = self.client.get("/dashboard")
        self.assertEqual(r_bloq.status_code, 302)
        self.assertIn("/precos?trial_expirado=1", r_bloq.headers["Location"])

        # API retorna 402 Payment Required
        r_api = self.client.post("/api/repositorio/verificar", json={})
        self.assertEqual(r_api.status_code, 402)

        # Mas /perfil e /precos continuam acessíveis para assinar/gerenciar conta
        self.assertEqual(self.client.get("/perfil").status_code, 200)
        self.assertEqual(self.client.get("/precos?trial_expirado=1").status_code, 200)

    def test_04_assinatura_ativa_libera_acesso_mesmo_com_trial_vencido(self):
        uid, _ = self._criar_usuario_teste(trial_delta_days=-10, plano_status="active")
        status = obter_status_assinatura_usuario(uid)
        self.assertTrue(status["acesso_liberado"])
        self.assertEqual(status["status"], "active")

        with self.client.session_transaction() as sess:
            sess["usuario_id"] = uid
            sess["nome"] = "Docente Ativo"
            sess["perfil"] = "professor"

        self.assertEqual(self.client.get("/dashboard").status_code, 200)

    def test_05_resend_envio_http_mockado(self):
        fake_resp = MagicMock(status_code=200, text='{"id":"email_123"}')
        with patch.dict(
            os.environ,
            {
                "RESEND_API_KEY": "re_test_123",
                "RESEND_FROM_EMAIL": "noreply@sgpp.com.br",
                "RESEND_FROM_NAME": "SGPP",
            },
            clear=False,
        ):
            self.assertTrue(resend_configurado())
            with patch("utils.email_service.requests.post", return_value=fake_resp) as mock_post:
                ok, status = enviar_email_codigo_recuperacao("prof@escola.com", "Maria Silva", "123456")
                self.assertTrue(ok)
                self.assertEqual(status, "enviado")
                mock_post.assert_called_once()
                _, kwargs = mock_post.call_args
                self.assertEqual(kwargs["json"]["to"], ["prof@escola.com"])
                self.assertIn("123456", kwargs["json"]["subject"])
                self.assertNotIn("&bull;", kwargs["json"]["subject"])

    def test_06_webhook_stripe_rejeita_sem_configuracao_ou_assinatura_invalida(self):
        with patch.dict(os.environ, {"STRIPE_SECRET_KEY": "", "STRIPE_WEBHOOK_SECRET": ""}, clear=False):
            ok, _msg, code = processar_webhook_stripe(b"{}", "")
            self.assertFalse(ok)
            self.assertEqual(code, 503)

    def test_07_storage_fallback_local_sem_supabase(self):
        with patch.dict(os.environ, {"SUPABASE_URL": "", "SUPABASE_SERVICE_ROLE_KEY": ""}, clear=False):
            self.assertFalse(storage_configurado())
            self.assertIsNone(garantir_arquivo_local("/tmp/nao_existe_sgpp_999.png", "logos/nao_existe.png"))

    def test_08_stripe_v17_webhook_event_object_compatibilidade(self):
        """Garante compatibilidade com objetos StripeObject do stripe-python v17 (sem .get())."""
        import stripe as stripe_sdk
        from utils.stripe_service import _para_dict, _extrair_subscription_id_invoice

        uid, _ = self._criar_usuario_teste(trial_delta_days=-5, plano_status="trial")
        evento_id = f"evt_test_{uuid.uuid4().hex[:12]}"
        evento_obj = stripe_sdk.Event.construct_from(
            {
                "id": evento_id,
                "object": "event",
                "type": "customer.subscription.updated",
                "data": {
                    "object": {
                        "id": "sub_test_v17",
                        "object": "subscription",
                        "customer": "cus_test_v17",
                        "status": "active",
                        "metadata": {"usuario_id": str(uid), "plano_tipo": "trimestral"},
                        "items": {"data": [{"current_period_end": 1893456000, "price": {"id": "price_x"}}]},
                    }
                },
            },
            key="rk_test",
        )
        # Confirma que _para_dict converte recursivamente StripeObject sem quebrar
        d = _para_dict(evento_obj)
        self.assertEqual(d["id"], evento_id)
        self.assertEqual(d["data"]["object"]["status"], "active")
        self.assertEqual(
            _extrair_subscription_id_invoice({"parent": {"subscription_details": {"subscription": "sub_2026"}}}),
            "sub_2026",
        )

        with patch.dict(
            os.environ,
            {"STRIPE_SECRET_KEY": "rk_test_123", "STRIPE_WEBHOOK_SECRET": "whsec_test_123"},
            clear=False,
        ):
            with patch("utils.stripe_service.stripe.Webhook.construct_event", return_value=evento_obj):
                ok, msg, code = processar_webhook_stripe(b"{}", "t=1,v1=abc")
                self.assertTrue(ok, msg)
                self.assertEqual(code, 200)

        status = obter_status_assinatura_usuario(uid)
        self.assertTrue(status["acesso_liberado"])
        self.assertEqual(status["status"], "active")
        self.assertEqual(status["plano_tipo"], "trimestral")

        # Limpeza do evento de teste
        conn, cur = get_cursor()
        try:
            cur.execute("DELETE FROM stripe_eventos WHERE event_id = %s", (evento_id,))
            conn.commit()
        finally:
            release_cursor(conn, cur)


if __name__ == "__main__":
    unittest.main()
