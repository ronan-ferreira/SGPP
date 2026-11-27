"""
Testes automatizados das correções P0:
1. Isolamento multi-inquilino (anti-IDOR) nas rotas de planos e cursos:
   - /plano/<id>, /plano_pdf/<id>, /plano_word/<id>, /baixar_plano/<id>
   - /curso/<id>, /curso_pdf/<id>, /curso_word/<id>, /gerar_atividade_plano/<id>
2. Separação de arquivos de saída entre plano individual (outputs/plano_<id>.*)
   e curso completo (outputs/curso_<curso_id>.*) sem colisão de nomes.
"""

from datetime import datetime, timedelta
import json
import os
import tempfile
import unittest
from unittest.mock import patch
import uuid

from docx import Document

from app import app
from database.conexao import get_cursor, release_cursor
from utils.docx_atividade_generator import gerar_docx_atividade
from utils.docx_generator import gerar_docx_plano
from utils.pdf_generator import gerar_pdf_plano_reportlab


class TestSegurancaRotasPlanosCursos(unittest.TestCase):

    def setUp(self):
        self.client = app.test_client()
        self.app_context = app.app_context()
        self.app_context.push()
        self.emails_teste = []
        self.arquivos_teste = []

        self.uid_a, _ = self._criar_usuario("Docente A")
        self.uid_b, _ = self._criar_usuario("Docente B")
        self.curso_a, self.plano_a = self._criar_curso_e_plano(self.uid_a, "Curso A", "Conteúdo Plano A")

    def tearDown(self):
        for arq in self.arquivos_teste:
            if os.path.exists(arq):
                try:
                    os.remove(arq)
                except OSError:
                    pass

        if self.emails_teste:
            conn, cur = get_cursor()
            try:
                for email in self.emails_teste:
                    cur.execute("DELETE FROM usuarios WHERE LOWER(email) = LOWER(%s)", (email,))
                conn.commit()
            finally:
                release_cursor(conn, cur)
        self.app_context.pop()

    def _criar_usuario(self, nome):
        email = f"p0_test_{uuid.uuid4().hex[:8]}@exemplo.com"
        self.emails_teste.append(email)
        trial_fim = datetime.now() + timedelta(days=7)
        conn, cur = get_cursor()
        try:
            cur.execute(
                """
                INSERT INTO usuarios (nome, email, senha, perfil, tutorial_visto, trial_fim, plano_status)
                VALUES (%s, %s, %s, 'professor', TRUE, %s, 'trial')
                RETURNING id
                """,
                (nome, email, "HashTeste123!", trial_fim),
            )
            uid = cur.fetchone()[0]
            conn.commit()
            return uid, email
        finally:
            release_cursor(conn, cur)

    def _criar_curso_e_plano(self, usuario_id, nome_curso, texto_plano):
        conn, cur = get_cursor()
        try:
            cur.execute(
                """
                INSERT INTO cursos_plano (nome_curso, carga_horaria, aulas_por_dia, usuario_id)
                VALUES (%s, 40, 4, %s)
                RETURNING id
                """,
                (nome_curso, usuario_id),
            )
            curso_id = cur.fetchone()[0]
            cur.execute(
                """
                INSERT INTO planos_aula (curso_id, dia, tema, plano_gerado, usuario_id)
                VALUES (%s, 1, %s, %s, %s)
                RETURNING id
                """,
                (curso_id, f"Dia 1 - {nome_curso}", texto_plano, usuario_id),
            )
            plano_id = cur.fetchone()[0]
            conn.commit()
            return curso_id, plano_id
        finally:
            release_cursor(conn, cur)

    def _autenticar(self, uid, nome):
        with self.client.session_transaction() as sess:
            sess["usuario_id"] = uid
            sess["nome"] = nome
            sess["perfil"] = "professor"

    def test_01_acesso_anonimo_redireciona_para_login(self):
        rotas = [
            f"/plano/{self.plano_a}",
            f"/plano_pdf/{self.plano_a}",
            f"/plano_word/{self.plano_a}",
            f"/baixar_plano/{self.plano_a}",
            f"/curso/{self.curso_a}",
            f"/curso_pdf/{self.curso_a}",
            f"/curso_word/{self.curso_a}",
            f"/gerar_atividade_plano/{self.plano_a}",
        ]
        for rota in rotas:
            resp = self.client.get(rota)
            self.assertEqual(resp.status_code, 302, f"Esperado 302 em {rota}")
            self.assertIn("/login", resp.headers["Location"])

    def test_02_usuario_b_nao_acessa_recursos_de_usuario_a(self):
        self._autenticar(self.uid_b, "Docente B")
        rotas = [
            f"/plano/{self.plano_a}",
            f"/plano_pdf/{self.plano_a}",
            f"/plano_word/{self.plano_a}",
            f"/baixar_plano/{self.plano_a}",
            f"/curso/{self.curso_a}",
            f"/curso_pdf/{self.curso_a}",
            f"/curso_word/{self.curso_a}",
            f"/gerar_atividade_plano/{self.plano_a}",
        ]
        for rota in rotas:
            resp = self.client.get(rota)
            self.assertEqual(resp.status_code, 404, f"Esperado 404 em {rota} para usuário não proprietário")

    def test_03_proprietario_acessa_e_arquivos_nao_colidem(self):
        self._autenticar(self.uid_a, "Docente A")

        self.assertEqual(self.client.get(f"/plano/{self.plano_a}").status_code, 200)
        self.assertEqual(self.client.get(f"/curso/{self.curso_a}").status_code, 200)

        arq_plano = f"outputs/plano_{self.plano_a}.docx"
        arq_curso = f"outputs/curso_{self.curso_a}.docx"
        self.arquivos_teste.extend([arq_plano, arq_curso])

        resp_pw = self.client.get(f"/plano_word/{self.plano_a}")
        self.assertEqual(resp_pw.status_code, 200)
        resp_pw.close()

        resp_cw = self.client.get(f"/curso_word/{self.curso_a}")
        self.assertEqual(resp_cw.status_code, 200)
        resp_cw.close()

        self.assertTrue(os.path.exists(arq_plano))
        self.assertTrue(os.path.exists(arq_curso))
        self.assertNotEqual(os.path.abspath(arq_plano), os.path.abspath(arq_curso))

    def test_04_rodapes_e_instituicao_dinamicos_em_docx_e_pdf(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            docx_ativ = os.path.join(tmpdir, "ativ.docx")
            gerar_docx_atividade(
                titulo="ATIVIDADE",
                conteudo="1. Pergunta teste\nA) Opção A\nB) Opção B",
                caminho_saida=docx_ativ,
                dados_cabecalho={"curso": "Matemática", "instituicao": "Escola Estadual Modelo"},
            )
            doc_a = Document(docx_ativ)
            footer_a = doc_a.sections[0].footer.paragraphs[0].text
            self.assertIn("Escola Estadual Modelo", footer_a)
            self.assertNotIn("SENAI", footer_a)

            docx_plano = os.path.join(tmpdir, "plano.docx")
            gerar_docx_plano(
                titulo="PLANO DE AULA",
                subtitulo="Matemática",
                conteudo="1. Identificação\nConteúdo teste",
                caminho_saida=docx_plano,
                instituicao="Colégio Alfa",
            )
            doc_p = Document(docx_plano)
            footer_p = doc_p.sections[0].footer.paragraphs[0].text
            self.assertIn("Colégio Alfa", footer_p)
            self.assertNotIn("SENAI", footer_p)

            pdf_plano = os.path.join(tmpdir, "plano.pdf")
            ok = gerar_pdf_plano_reportlab(
                docx_path=None,
                pdf_path=pdf_plano,
                titulo="PLANO DE AULA",
                subtitulo="Matemática",
                conteudo="1. Identificação\nConteúdo teste",
                instituicao="Colégio Alfa",
            )
            self.assertTrue(ok)
            self.assertTrue(os.path.exists(pdf_plano))
            self.assertGreater(os.path.getsize(pdf_plano), 500)

    def test_05_exportar_cascata_e_pei_docx_pdf_e_isolamento(self):
        conn, cur = get_cursor()
        try:
            payload = json.dumps({
                "texto_anual": "1. IDENTIFICAÇÃO\nPlanejamento anual teste.",
                "bimestres": {"1": "1. OBJETIVO GERAL DO 1º BIMESTRE\nConteúdo bimestral teste."},
                "mensais": {"Fevereiro": "1. METAS\nMetas de Fevereiro."},
            }, ensure_ascii=False)
            cur.execute(
                """
                INSERT INTO planejamentos_docente (
                    nome_curso, unidade_curricular, unidade_escolar,
                    carga_horaria, horas_por_dia, total_aulas,
                    curso_id, usuario_id, modalidade, tipo_planejamento, dados_json
                )
                VALUES (%s, %s, %s, 160, 2, 80, %s, %s, 'fundamental', 'anual', %s)
                RETURNING id
                """,
                ("6º Ano", "Matemática", "Escola Municipal Centro", self.curso_a, self.uid_a, payload),
            )
            plan_id = cur.fetchone()[0]
            conn.commit()
        finally:
            release_cursor(conn, cur)

        # Usuário B não pode exportar planejamento do Usuário A
        self._autenticar(self.uid_b, "Docente B")
        self.assertEqual(self.client.get(f"/planejamento/{plan_id}/exportar/pdf").status_code, 404)

        # Usuário A exporta completo e bimestral em Word e PDF
        self._autenticar(self.uid_a, "Docente A")
        self.arquivos_teste.extend([
            f"outputs/planejamento_{plan_id}_completo.docx",
            f"outputs/planejamento_{plan_id}_completo.pdf",
            f"outputs/planejamento_{plan_id}_bimestral_b1.docx",
        ])

        r_pdf = self.client.get(f"/planejamento/{plan_id}/exportar/pdf?escopo=completo")
        self.assertEqual(r_pdf.status_code, 200)
        self.assertIn("application/pdf", r_pdf.content_type)
        r_pdf.close()

        r_word = self.client.get(f"/planejamento/{plan_id}/exportar/word?escopo=bimestral&bimestre=1")
        self.assertEqual(r_word.status_code, 200)
        r_word.close()

        self.assertEqual(self.client.get(f"/planejamento/{plan_id}/exportar/txt").status_code, 400)

    def test_06_regenerar_preserva_contexto_pei_e_formato(self):
        conn, cur = get_cursor()
        try:
            cur.execute(
                """
                INSERT INTO atividades (curso, disciplina, conteudo, dificuldade, atividade_gerada, usuario_id, origem)
                VALUES (%s, %s, %s, %s, %s, %s, 'ia')
                RETURNING id
                """,
                (
                    "PEI — Estudante Lucas",
                    "Ciências",
                    "- Curso: 5º Ano\n- Formato: Discursiva (Dissertativa)\n- Conteúdo: Sistema Solar",
                    "fácil",
                    "Questão 1: O que é o Sol?\nQuestão 2: Quantos planetas existem?",
                    self.uid_a,
                ),
            )
            ativ_id = cur.fetchone()[0]
            conn.commit()
        finally:
            release_cursor(conn, cur)

        self._autenticar(self.uid_a, "Docente A")
        with patch("app.gerar_atividade", return_value="Questão 1: Nova") as mock_gerar:
            resp = self.client.get(f"/regenerar/{ativ_id}")
            self.assertEqual(resp.status_code, 302)
            mock_gerar.assert_called_once()
            _, kwargs = mock_gerar.call_args
            args = mock_gerar.call_args[0]
            self.assertEqual(args[2], "discursiva")
            self.assertEqual(args[3], 2)
            self.assertEqual(kwargs["segmento"], "pei")
            self.assertEqual(kwargs["disciplina"], "Ciências")
            self.assertEqual(kwargs["curso"], "PEI — Estudante Lucas")


if __name__ == "__main__":
    unittest.main()
