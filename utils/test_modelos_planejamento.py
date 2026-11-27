"""
Testes automatizados de robustez para geração de Planejamento Docente com modelos próprios:
- Planilha Excel (.xlsx) personalizada com células mescladas no cabeçalho, nas linhas de aulas e no rodapé
- Planilha Excel (.xlsx) estilo Técnico/Qualificação com merges deslocados no cabeçalho (ex: B5:F5)
- Planilha Excel (.xls) legada (97-2003) via conversão xlrd
- Documento Word (.docx) com tabela horizontal com células mescladas e cabeçalho grade mesclado
- Documento Word (.docx) com cartão vertical único clonado para múltiplas aulas
- Fluxo HTTP POST /planejamento_docente para 'Gestão de Suprimentos' (acentos, modelo customizado,
  arquivo corrompido com fallback automático sem 500, datas DD/MM/YYYY e campos longos)
"""

from datetime import datetime, timedelta
import io
import os
import tempfile
import unittest
from unittest.mock import patch
import uuid

import openpyxl
from openpyxl.styles import Border
from docx import Document

from app import app
from database.conexao import get_cursor, release_cursor
from utils.docx_planejamento_generator import gerar_docx_planejamento
from utils.excel_planejamento_generator import gerar_excel_planejamento
from utils.matriz_saep_reader import ler_matriz_saep


def _aulas_exemplo(qtd=4):
    base = datetime(2026, 3, 2)
    return [
        {
            "dia": i + 1,
            "nivel_bloom": "Aplicar",
            "capacidade": f"Aplicar técnicas de gestão de suprimentos na etapa {i + 1}",
            "conhecimento": f"Controle de estoques {i + 1}\nCurva ABC",
            "saep": "C1 - Gerenciar compras e estoques",
            "estrategia": "Estudo de caso em logística",
            "ambiente": "Sala de Aula",
            "recursos": "Planilhas, Projetor",
            "carga_horaria_str": "4 HS",
            "criterios": "Precisão nos cálculos de reposição",
            "instrumentos": "Lista prática",
            "situacao_aprendizagem": "Simulação de centro de distribuição",
            "data_inicio": base + timedelta(days=i * 2),
            "data_fim": base + timedelta(days=i * 2),
        }
        for i in range(qtd)
    ]


class TestModelosPlanejamento(unittest.TestCase):

    def setUp(self):
        self.client = app.test_client()
        self.app_context = app.app_context()
        self.app_context.push()
        self.emails_teste = []
        self.arquivos_limpar = []

    def tearDown(self):
        for arq in self.arquivos_limpar:
            if arq and os.path.exists(arq):
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

    def _criar_usuario_autenticado(self):
        email = f"modelo_test_{uuid.uuid4().hex[:8]}@exemplo.com"
        self.emails_teste.append(email)
        conn, cur = get_cursor()
        try:
            cur.execute(
                """
                INSERT INTO usuarios (nome, email, senha, perfil, tutorial_visto, trial_fim, plano_status, segmento)
                VALUES (%s, %s, %s, 'professor', TRUE, %s, 'trial', 'tecnico')
                RETURNING id
                """,
                ("Docente Suprimentos", email, "HashTeste123!", datetime.now() + timedelta(days=7)),
            )
            uid = cur.fetchone()[0]
            conn.commit()
        finally:
            release_cursor(conn, cur)

        with self.client.session_transaction() as sess:
            sess["usuario_id"] = uid
            sess["nome"] = "Docente Suprimentos"
            sess["email"] = email
            sess["perfil"] = "professor"
            sess["segmento"] = "tecnico"
        return uid

    def test_01_excel_customizado_com_celulas_mescladas_e_bordas_vazias(self):
        """Reproduz o crash clássico MergedCell read-only em planilhas personalizadas reais."""
        with tempfile.TemporaryDirectory() as tmp:
            modelo = os.path.join(tmp, "modelo_suprimentos.xlsx")
            saida = os.path.join(tmp, "saida_suprimentos.xlsx")

            wb = openpyxl.Workbook()
            ws = wb.active
            ws.title = "Planejamento"

            # Cabeçalho com rótulos e campos mesclados (A2:B2 rótulo, C2:F2 valor)
            ws["A2"] = "Curso:"
            ws.merge_cells("A2:B2")
            ws.merge_cells("C2:F2")

            ws["A3"] = "Unidade Curricular:"
            ws.merge_cells("A3:B3")
            ws.merge_cells("C3:F3")

            ws["A4"] = "Professor:"
            ws.merge_cells("A4:B4")
            ws.merge_cells("C4:F4")

            # Cabeçalho de tabela em 2 linhas mescladas verticalmente (linhas 6 e 7)
            ws["A6"] = "Aula"
            ws["B6"] = "Conteúdos / Capacidades"
            ws["D6"] = "Estratégia"
            ws["E6"] = "Recursos"
            ws["F6"] = "Data"
            ws.merge_cells("B6:C7")
            ws.merge_cells("A6:A7")
            ws.merge_cells("D6:D7")
            ws.merge_cells("E6:E7")
            ws.merge_cells("F6:F7")

            # Linha de exemplo mesclada (linha 8) com borda left=None + rodapé mesclado (linha 10)
            ws["B8"] = "Exemplo antigo"
            ws.merge_cells("B8:C8")
            ws["A8"].border = Border(left=None)
            ws["A10"] = "Assinatura Coordenação"
            ws.merge_cells("A10:F10")
            wb.save(modelo)

            cab = {
                "nome_curso": "Técnico em Logística",
                "unidade_curricular": "Gestão de Suprimentos",
                "codigo_turma": "LOG.2026.1",
                "modalidade": "HABILITAÇÃO TÉCNICA",
                "unidade_escolar": "Escola Técnica Central",
                "carga_horaria": 60,
                "horas_por_dia": 4,
                "data_inicio": "2026-03-02",
                "data_fim": "2026-03-30",
                "instrutor": "Maria Oliveira",
            }
            caminho = gerar_excel_planejamento(cab, _aulas_exemplo(4), caminho_modelo=modelo, caminho_saida=saida)
            self.assertTrue(os.path.exists(caminho))

            wb_out = openpyxl.load_workbook(caminho)
            ws_out = wb_out.active
            self.assertEqual(ws_out["C2"].value, "TÉCNICO EM LOGÍSTICA")
            self.assertEqual(ws_out["C3"].value, "Gestão de Suprimentos")
            self.assertEqual(ws_out["C4"].value, "MARIA OLIVEIRA")
            self.assertIn("gestão de suprimentos", str(ws_out["B8"].value).lower())
            self.assertIn("Aula 4", str(ws_out["A11"].value))
            wb_out.close()

    def test_02_excel_tecnico_adaptado_com_cabecalho_mesclado_deslocado(self):
        """Modelo classificado como técnico mas com B5:F5 mesclado (C5 é MergedCell)."""
        with tempfile.TemporaryDirectory() as tmp:
            modelo = os.path.join(tmp, "modelo_tec_adaptado.xlsx")
            saida = os.path.join(tmp, "saida_tec_adaptado.xlsx")

            wb = openpyxl.Workbook()
            ws = wb.active
            # C5 e M5 são MergedCells filhas de B5:F5 e L5:P5
            ws.merge_cells("B5:F5")
            ws.merge_cells("L5:P5")
            ws.merge_cells("B6:F6")
            ws["D11"] = "Matriz de Referência SAEP"
            ws["A12"] = "O que?"
            ws.merge_cells("A12:C13")  # Merge que cruza linha 13
            wb.save(modelo)

            cab = {
                "nome_curso": "Técnico em Logística",
                "unidade_curricular": "Gestão de Suprimentos",
                "codigo_turma": "LOG.1",
                "modalidade": "HABILITAÇÃO TÉCNICA",
                "unidade_escolar": "Instituto Estadual",
                "carga_horaria": 40,
                "horas_por_dia": 4,
                "data_inicio": "2026-03-02",
                "data_fim": "2026-03-20",
                "instrutor": "Ana Paula",
            }
            caminho = gerar_excel_planejamento(cab, _aulas_exemplo(3), caminho_modelo=modelo, caminho_saida=saida)
            self.assertTrue(os.path.exists(caminho))
            wb_out = openpyxl.load_workbook(caminho)
            self.assertEqual(wb_out.active["B5"].value, "Instituto Estadual")
            wb_out.close()

    def test_03_excel_xls_legado_e_matriz_saep_xls(self):
        """Garante suporte a arquivos .xls antigos tanto como modelo quanto como Matriz SAEP em Linux."""
        xls_repo = os.path.join("uploads", "Matriz_SAEP_TECNICO_EM_INFORMATICA_1.xls")
        if os.path.exists(xls_repo):
            itens = ler_matriz_saep(xls_repo)
            self.assertGreater(len(itens), 0)

            with tempfile.TemporaryDirectory() as tmp:
                saida = os.path.join(tmp, "saida_de_xls.xlsx")
                cab = {
                    "nome_curso": "Técnico em Logística",
                    "unidade_curricular": "Gestão de Suprimentos",
                    "carga_horaria": 40,
                }
                caminho = gerar_excel_planejamento(cab, _aulas_exemplo(2), caminho_modelo=xls_repo, caminho_saida=saida)
                self.assertTrue(os.path.exists(caminho))
                self.assertGreater(os.path.getsize(caminho), 1000)

    def test_04_word_modelo_horizontal_mesclado_e_cartao_unico(self):
        """Garante preenchimento seguro de modelos Word com células mescladas e clonagem de cartões."""
        with tempfile.TemporaryDirectory() as tmp:
            # 1) Tabela horizontal com cabeçalho mesclado
            mod_h = os.path.join(tmp, "mod_horizontal.docx")
            out_h = os.path.join(tmp, "out_horizontal.docx")
            doc = Document()
            t_cab = doc.add_table(rows=2, cols=4)
            t_cab.cell(0, 0).merge(t_cab.cell(0, 1))
            t_cab.cell(0, 0).text = "Curso:"
            t_cab.cell(0, 2).merge(t_cab.cell(0, 3))
            t_cab.cell(1, 0).text = "Disciplina:"

            t_aulas = doc.add_table(rows=2, cols=4)
            t_aulas.cell(0, 0).text = "Aula"
            t_aulas.cell(0, 1).text = "Conteúdo"
            t_aulas.cell(0, 2).text = "Metodologia"
            t_aulas.cell(0, 3).text = "Data"
            doc.save(mod_h)

            cab = {
                "nome_curso": "Técnico em Logística",
                "unidade_curricular": "Gestão de Suprimentos",
                "carga_horaria": 40,
            }
            res_h = gerar_docx_planejamento(cab, _aulas_exemplo(3), caminho_modelo=mod_h, caminho_saida=out_h)
            self.assertTrue(os.path.exists(res_h))
            doc_out_h = Document(res_h)
            self.assertEqual(doc_out_h.tables[0].cell(0, 0).text.strip(), "Curso:")
            self.assertIn("TÉCNICO EM LOGÍSTICA", doc_out_h.tables[0].cell(0, 2).text)
            self.assertEqual(len(doc_out_h.tables[1].rows), 4)  # 1 header + 3 aulas

            # 2) Cartão vertical único clonado para 3 aulas
            mod_v = os.path.join(tmp, "mod_vertical.docx")
            out_v = os.path.join(tmp, "out_vertical.docx")
            doc_v = Document()
            t_card = doc_v.add_table(rows=5, cols=2)
            for idx_r, lbl in enumerate(["Capacidade", "Estratégia", "Ambiente", "Recursos", "Critérios"]):
                t_card.cell(idx_r, 0).text = lbl
            doc_v.save(mod_v)

            res_v = gerar_docx_planejamento(cab, _aulas_exemplo(3), caminho_modelo=mod_v, caminho_saida=out_v)
            doc_out_v = Document(res_v)
            self.assertEqual(len(doc_out_v.tables), 3)

    def test_05_rota_planejamento_gestao_suprimentos_modelo_custom_e_corrompido(self):
        """Testa POST /planejamento_docente com modelo válido e com arquivo corrompido (nunca 500)."""
        self._criar_usuario_autenticado()

        # Cria um modelo Excel com células mescladas em memória
        buf_xlsx = io.BytesIO()
        wb = openpyxl.Workbook()
        ws = wb.active
        ws["A1"] = "Curso"
        ws.merge_cells("A1:B1")
        ws.merge_cells("C1:E1")
        ws["A3"] = "Aula"
        ws["B3"] = "Conhecimentos"
        ws["C3"] = "Estratégia"
        ws["D3"] = "Data"
        ws.merge_cells("B4:C4")
        wb.save(buf_xlsx)
        buf_xlsx.seek(0)

        with patch("app.gerar_planejamento_ia", return_value=_aulas_exemplo(3)):
            # 1) Modelo Excel válido com células mescladas + nome com acento ("Gestão de Suprimentos") + data DD/MM/YYYY
            r_ok = self.client.post(
                "/planejamento_docente",
                data={
                    "nome_curso": "Técnico em Logística " + ("A" * 200),
                    "unidade_curricular": "Gestão de Suprimentos",
                    "codigo_turma": "LOG.2026.1",
                    "modalidade": "HABILITAÇÃO TÉCNICA",
                    "unidade_escolar": "Escola Técnica",
                    "tipo_cronograma": "horas",
                    "carga_horaria": "12h",
                    "horas_por_dia": "4",
                    "data_inicio": "02/03/2026",
                    "conteudo_digitado": "Compras e suprimentos\nControle de estoque\nCadeia de suprimentos",
                    "arquivo_modelo": (buf_xlsx, "Modelo Gestão de Suprimentos.xlsx"),
                },
                content_type="multipart/form-data",
            )
            self.assertEqual(r_ok.status_code, 302)
            self.assertIn("/listar_planejamentos?sucesso=1", r_ok.headers["Location"])

            # 2) Arquivo corrompido (.xlsx com bytes inválidos) -> fallback automático sem 500
            r_fallback = self.client.post(
                "/planejamento_docente",
                data={
                    "nome_curso": "Técnico em Logística",
                    "unidade_curricular": "Gestão de Suprimentos",
                    "modalidade": "HABILITAÇÃO TÉCNICA",
                    "tipo_cronograma": "horas",
                    "carga_horaria": "12",
                    "horas_por_dia": "4",
                    "data_inicio": "2026-03-02",
                    "arquivo_modelo": (io.BytesIO(b"NAO_E_UM_EXCEL_VALIDO"), "modelo_quebrado.xlsx"),
                },
                content_type="multipart/form-data",
            )
            self.assertEqual(r_fallback.status_code, 302)
            self.assertIn("/listar_planejamentos?sucesso=1", r_fallback.headers["Location"])

            # Limpa os planejamentos e arquivos temporários gerados
            for resp_loc in (r_ok.headers["Location"], r_fallback.headers["Location"]):
                pid = int(resp_loc.split("download=")[-1])
                self.client.get(f"/deletar_planejamento/{pid}")

            for nome in os.listdir("uploads"):
                if nome.startswith("modelo_custom_"):
                    self.arquivos_limpar.append(os.path.join("uploads", nome))


if __name__ == "__main__":
    unittest.main()
