import os
import re
import math
import zipfile
import unicodedata
import openpyxl
from openpyxl.cell.cell import MergedCell
from openpyxl.styles import Font, Alignment, Border, Side, PatternFill
from openpyxl.utils import get_column_letter, coordinate_to_tuple
from datetime import datetime
from utils.texto_utils import limpar_numeracao_texto, limpar_numeracao_item

# Diretório base do projeto e pasta templates interna do sistema
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PASTA_TEMPLATES = os.path.join(BASE_DIR, "templates")

MODELO_TECNICO      = os.path.join(PASTA_TEMPLATES, "modelo_tecnico.xlsx")
MODELO_QUALIFICACAO = os.path.join(PASTA_TEMPLATES, "modelo_qualificacao.xlsx")
MODELO_LEGADO       = os.path.join(PASTA_TEMPLATES, "modelo_planejamento_docente.xlsx")

from utils.imagem_utils import processar_logo_canvas_excel

# PNG 1x1 transparente usado para limpar a logo fixa do modelo quando o professor não enviou logo própria
_PNG_TRANSPARENTE_1X1 = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
    b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)

# Caminho opcional da logo SENAI (usado apenas se a instituição do docente for explicitamente SENAI)
LOGO_PROJETO = os.path.join(BASE_DIR, "static", "img", "SENAI_logo_2024.png")


def obter_bytes_logo_oficial(logo_custom_path=None, unidade_escolar=""):
    """
    Retorna os bytes da logo institucional do professor ajustada proporcionalmente no canvas.
    Se o professor não enviou logo própria, retorna um PNG transparente 1x1
    (evitando exibir logo do SENAI para professores de outras instituições),
    exceto se a instituição informada contiver explicitamente 'SENAI'.
    """
    if logo_custom_path and os.path.exists(logo_custom_path):
        try:
            bytes_proc = processar_logo_canvas_excel(logo_custom_path, largura_canvas=400, altura_canvas=120)
            if bytes_proc:
                return bytes_proc
            with open(logo_custom_path, "rb") as f:
                return f.read()
        except Exception as e:
            print(f"[AVISO] Falha ao processar canvas da logo customizada: {e}")

    if "SENAI" in str(unidade_escolar or "").upper() and os.path.exists(LOGO_PROJETO):
        try:
            with open(LOGO_PROJETO, "rb") as f:
                return f.read()
        except Exception:
            pass

    return _PNG_TRANSPARENTE_1X1


def garantir_logo_oficial(caminho_xlsx: str, logo_custom_path=None, unidade_escolar=""):
    """
    Insere a logo institucional do professor na planilha Excel ou limpa
    a imagem fixa do template caso o professor ainda não tenha enviado logo.
    """
    logo_bytes = obter_bytes_logo_oficial(logo_custom_path=logo_custom_path, unidade_escolar=unidade_escolar)
    if not logo_bytes or not os.path.exists(caminho_xlsx):
        return

    tmp_path = caminho_xlsx + ".tmp"
    try:
        with zipfile.ZipFile(caminho_xlsx, "r") as zin:
            with zipfile.ZipFile(tmp_path, "w", compression=zipfile.ZIP_DEFLATED) as zout:
                for item in zin.infolist():
                    if item.filename == "xl/media/image1.png":
                        zout.writestr(item, logo_bytes)
                    else:
                        zout.writestr(item, zin.read(item.filename))
        os.replace(tmp_path, caminho_xlsx)
    except Exception as e:
        print(f"[AVISO] Nao foi possivel validar logo no arquivo {caminho_xlsx}: {e}")
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass


def obter_caminho_modelo(tipo: str) -> str:
    """
    Retorna o caminho do modelo oficial (.xlsx) salvo internamente no sistema (templates/).
    Totalmente autocontido e independente do computador ou ambiente em que o sistema roda:
      - 'tecnico'      → templates/modelo_tecnico.xlsx
      - 'qualificacao' → templates/modelo_qualificacao.xlsx
    """
    tipo_norm = "tecnico" if tipo == "tecnico" else "qualificacao"

    if tipo_norm == "tecnico":
        caminho = MODELO_TECNICO
        if not os.path.exists(caminho):
            caminho = os.path.join("templates", "modelo_tecnico.xlsx")
    else:
        caminho = MODELO_QUALIFICACAO
        if not os.path.exists(caminho):
            caminho = os.path.join("templates", "modelo_qualificacao.xlsx")

    if not os.path.exists(caminho):
        if os.path.exists(MODELO_LEGADO):
            print(f"[AVISO] Usando modelo legado de fallback: {MODELO_LEGADO}")
            return MODELO_LEGADO
        raise FileNotFoundError(
            f"Arquivo modelo '{caminho}' não encontrado no sistema. "
            f"Verifique se o arquivo existe na pasta templates/ do projeto."
        )

    return caminho


def detectar_tipo_curso(modalidade: str = "", nome_curso: str = "") -> str:
    """
    Detecta o tipo de curso com base na modalidade ou nome do curso.
    Retorna 'tecnico' ou 'qualificacao'.
      - Técnico / Habilitação Técnica → 'tecnico'
      - Qualificação / Aperfeiçoamento → 'qualificacao'
    """
    m = (modalidade or "").strip().upper()
    n = (nome_curso or "").strip().upper()

    # Prioridade para a modalidade explicitada no formulário
    if any(p in m for p in ["APERFEIÇOAMENTO", "APERFEICOAMENTO", "QUALIFICAÇÃO", "QUALIFICACAO"]):
        return "qualificacao"
    if any(p in m for p in ["TÉCNICO", "TECNICO", "HABILITAÇÃO", "HABILITACAO"]):
        return "tecnico"

    # Se a modalidade não for conclusiva, inspeciona o nome do curso
    if any(p in n for p in ["APERFEIÇOAMENTO", "APERFEICOAMENTO", "QUALIFICAÇÃO", "QUALIFICACAO"]):
        return "qualificacao"
    if any(p in n for p in ["TÉCNICO", "TECNICO", "HABILITAÇÃO", "HABILITACAO"]):
        return "tecnico"

    return "qualificacao"


def normalizar_texto_chave(txt):
    """Remove acentos, pontuações e converte para minúsculo para buscas flexíveis."""
    if not txt:
        return ""
    s = unicodedata.normalize("NFKD", str(txt)).encode("ASCII", "ignore").decode("utf-8")
    return re.sub(r"[^a-zA-Z0-9\s]", " ", s).strip().lower()


def _sanitizar_nome_arquivo(texto, padrao="UC", max_len=60):
    """Converte um texto para nome de arquivo ASCII seguro (compatível com Supabase Storage e HTTP)."""
    base = unicodedata.normalize("NFKD", str(texto or "")).encode("ASCII", "ignore").decode("ASCII")
    limpo = re.sub(r"[^A-Za-z0-9_-]+", "_", base).strip("_-")
    if not limpo:
        limpo = padrao
    return limpo[:max_len]


def _carregar_workbook_excel(caminho_modelo: str, data_only: bool = False):
    """
    Carrega uma planilha Excel (.xlsx, .xlsm ou .xls antigo 97-2003) como openpyxl.Workbook.
    Arquivos .xls são convertidos em memória via xlrd preservando abas, valores e células mescladas.
    """
    ext = os.path.splitext(caminho_modelo)[1].lower()
    if ext == ".xls":
        import xlrd

        try:
            book_xls = xlrd.open_workbook(caminho_modelo, formatting_info=True, ignore_workbook_corruption=True)
        except Exception:
            book_xls = xlrd.open_workbook(caminho_modelo, formatting_info=False, ignore_workbook_corruption=True)

        wb = openpyxl.Workbook()
        # Remove a aba vazia padrão
        wb.remove(wb.active)

        for idx_aba in range(book_xls.nsheets):
            sh = book_xls.sheet_by_index(idx_aba)
            titulo_aba = (sh.name or f"Planilha{idx_aba + 1}")[:31]
            ws = wb.create_sheet(title=titulo_aba)
            for r in range(sh.nrows):
                for c in range(sh.ncols):
                    val = sh.cell_value(r, c)
                    ctype = sh.cell_type(r, c)
                    if ctype == xlrd.XL_CELL_DATE:
                        try:
                            dt_tuple = xlrd.xldate_as_tuple(val, book_xls.datemode)
                            val = datetime(*dt_tuple[:6])
                        except Exception:
                            pass
                    if val != "":
                        ws.cell(row=r + 1, column=c + 1, value=val)

            for rlo, rhi, clo, chi in getattr(sh, "merged_cells", []):
                try:
                    if rhi > rlo and chi > clo:
                        ws.merge_cells(
                            start_row=rlo + 1,
                            start_column=clo + 1,
                            end_row=rhi,
                            end_column=chi,
                        )
                except Exception:
                    pass

        if not wb.sheetnames:
            wb.create_sheet(title="Planejamento")
        return wb

    return openpyxl.load_workbook(caminho_modelo, data_only=data_only)


def _obter_range_mesclado(ws, row: int, col: int):
    """Retorna o CellRange mesclado que contém (row, col), ou None."""
    for rng in ws.merged_cells.ranges:
        if rng.min_row <= row <= rng.max_row and rng.min_col <= col <= rng.max_col:
            return rng
    return None


def _escrever_celula_segura(ws, row_ou_coord, col=None, valor=None):
    """
    Escreve valor em uma célula do openpyxl de forma segura mesmo que a coordenada
    caia em uma MergedCell (redireciona automaticamente para a célula âncora do merge).
    Aceita (_escrever_celula_segura(ws, 'C5', valor=...)) ou (_escrever_celula_segura(ws, r, c, valor)).
    """
    if isinstance(row_ou_coord, str):
        row, col = coordinate_to_tuple(row_ou_coord)
    else:
        row = row_ou_coord

    cell = ws.cell(row=row, column=col)
    if isinstance(cell, MergedCell):
        rng = _obter_range_mesclado(ws, row, col)
        if rng is not None:
            cell = ws.cell(row=rng.min_row, column=rng.min_col)
        else:
            return None
    cell.value = valor
    return cell


def _obter_celula_valor_ao_lado(ws, row: int, col: int):
    """
    Dado um rótulo em (row, col), retorna a célula gravável imediatamente à direita
    do range mesclado do rótulo, resolvendo qualquer MergedCell de destino para sua âncora.
    """
    rng_rotulo = _obter_range_mesclado(ws, row, col)
    ancora_rotulo = (rng_rotulo.min_row, rng_rotulo.min_col) if rng_rotulo else (row, col)
    col_alvo = (rng_rotulo.max_col + 1) if rng_rotulo else (col + 1)

    if col_alvo > max(ws.max_column + 2, 35):
        return None

    cell_alvo = ws.cell(row=row, column=col_alvo)
    if isinstance(cell_alvo, MergedCell):
        rng_alvo = _obter_range_mesclado(ws, row, col_alvo)
        if rng_alvo is None:
            return None
        ancora_alvo = (rng_alvo.min_row, rng_alvo.min_col)
        if ancora_alvo == ancora_rotulo:
            return None
        cell_alvo = ws.cell(row=ancora_alvo[0], column=ancora_alvo[1])

    return cell_alvo


def _remover_merges_a_partir_da_linha(ws, linha_inicio: int):
    """Desmescla todos os intervalos que tocam ou estão abaixo de linha_inicio."""
    ranges_para_remover = [
        rng for rng in list(ws.merged_cells.ranges)
        if rng.max_row >= linha_inicio
    ]
    for rng in ranges_para_remover:
        try:
            ws.merged_cells.remove(rng)
        except Exception:
            pass


def _celula_sem_borda(cell) -> bool:
    """Verifica com segurança se a célula não possui estilo de borda esquerda definido."""
    if not cell or not cell.border or not cell.border.left:
        return True
    return not bool(cell.border.left.style)


def _obter_planilha_ativa_segura(wb):
    """Retorna a planilha ativa (garantindo que seja uma Worksheet e não Chartsheet)."""
    ws = wb.active
    if ws is None or not hasattr(ws, "cell"):
        ws = wb.worksheets[0] if wb.worksheets else wb.create_sheet(title="Planejamento")
    return ws


def obter_situacao_aprendizagem_padrao(aula, dados_cabecalho):
    """
    Retorna a situação de aprendizagem contextualizada. Se for instituição SENAI,
    mantém o link oficial; para qualquer outra instituição, usa texto pedagógico contextualizado.
    """
    sa = aula.get("situacao_aprendizagem")
    if sa and str(sa).strip():
        return str(sa).strip()
    unidade = str(dados_cabecalho.get("unidade_escolar", "")).upper()
    modalidade = str(dados_cabecalho.get("modalidade", "")).upper()
    if "SENAI" in unidade or "SENAI" in modalidade:
        return "https://recursosdidaticos.senai.br"
    return "Situação de aprendizagem contextualizada com resolução de problemas práticos"


def identificar_estrutura_modelo(caminho_modelo: str) -> tuple:
    """
    Inspeciona o arquivo de modelo Excel fornecido e determina se segue
    o layout técnico oficial, o de qualificação ou se é um modelo personalizado.
    Retorna (tipo_estrutura, eh_customizado).
    """
    if not caminho_modelo or not os.path.exists(caminho_modelo):
        return ("qualificacao", False)

    try:
        wb = _carregar_workbook_excel(caminho_modelo, data_only=True)
        ws = _obter_planilha_ativa_segura(wb)

        # Inspeciona cabeçalhos para detectar modelo técnico padrão/oficial
        val_d11 = normalizar_texto_chave(ws.cell(row=11, column=4).value or "")
        val_d12 = normalizar_texto_chave(ws.cell(row=12, column=4).value or "")
        val_e11 = normalizar_texto_chave(ws.cell(row=11, column=5).value or "")

        if "saep" in val_d11 or "saep" in val_d12 or "matriz de referencia" in val_d11 or "matriz de referencia" in val_d12:
            wb.close()
            return ("tecnico", False)

        # Inspeciona modelo de qualificação padrão/oficial
        val_d11_q = normalizar_texto_chave(ws.cell(row=11, column=4).value or "")
        val_e11_q = normalizar_texto_chave(ws.cell(row=11, column=5).value or "")

        if ("como" in val_d11_q or "estrategia" in val_d11_q) and ("onde" in val_e11_q or "ambiente" in val_e11_q):
            wb.close()
            return ("qualificacao", False)

        # Caso contrário, trata como modelo personalizado enviado pelo usuário
        wb.close()
        return ("customizado", True)
    except Exception as e:
        print(f"[AVISO] Erro ao identificar estrutura do modelo: {e}")
        return ("customizado", True)


def calcular_altura_linha(texto_o_que, texto_estrategia="", texto_recursos="", texto_criterios="", texto_instrumentos=""):
    """
    Calcula a altura ideal da linha do Excel baseando-se no número de linhas reais
    e quebras automáticas de texto (wrap text) para garantir que nenhuma informação
    fique cortada ou oculta.
    """
    def estimar_linhas_bloco(texto, largura_chars):
        if not texto:
            return 0
        total = 0
        for linha in str(texto).split("\n"):
            linha_s = linha.strip()
            if not linha_s:
                total += 1
            else:
                total += max(1, math.ceil(len(linha_s) / max(largura_chars, 10)))
        return total

    # Larguras estimadas em caracteres úteis por coluna mesclada:
    # A:C (~50 caracteres)
    # D (~32 caracteres)
    # H:I (~16 caracteres)
    # M:O (~22 caracteres)
    # P (~18 caracteres)
    linhas_a = estimar_linhas_bloco(texto_o_que, 50)
    linhas_d = estimar_linhas_bloco(texto_estrategia, 32)
    linhas_h = estimar_linhas_bloco(texto_recursos, 16)
    linhas_m = estimar_linhas_bloco(texto_criterios, 22)
    linhas_p = estimar_linhas_bloco(texto_instrumentos, 18)

    max_linhas = max(linhas_a, linhas_d, linhas_h, linhas_m, linhas_p, 6)

    # Cada linha consome aproximadamente 16 pontos em Calibri 10, com margem interna
    altura_estimada = max_linhas * 16 + 25

    # Altura base mínima de 115 pt; aumenta dinamicamente conforme a quantidade de conteúdo
    return max(115, altura_estimada)


def gerar_excel_planejamento(
    dados_cabecalho,
    aulas,
    caminho_modelo=None,
    caminho_saida=None
):
    """
    Gera o arquivo Excel do Planejamento Docente a partir do modelo selecionado:
      - Se o usuário forneceu um modelo personalizado: inspeciona e preenche diretamente no modelo dele.
      - Se o usuário NÃO forneceu modelo: seleciona automaticamente o modelo oficial do sistema
        (Técnico ou Qualificação/Aperfeiçoamento) conforme a modalidade e preenche.
    """
    modalidade = dados_cabecalho.get("modalidade", "")
    nome_curso = dados_cabecalho.get("nome_curso", "")

    # 1. Caso o usuário NÃO tenha subido nenhum modelo:
    if not caminho_modelo:
        tipo = detectar_tipo_curso(modalidade, nome_curso)
        caminho_modelo_padrao = obter_caminho_modelo(tipo)
        print(f"[INFO] Nenhum modelo customizado fornecido. Usando modelo padrão do sistema.")
        print(f"[INFO] Modalidade: '{modalidade}' | Curso: '{nome_curso}' => Tipo: '{tipo}'")
        print(f"[INFO] Modelo selecionado: {caminho_modelo_padrao}")
        if tipo == "tecnico":
            return _gerar_excel_tecnico(dados_cabecalho, aulas, caminho_modelo_padrao, caminho_saida, aplicar_logo=True)
        else:
            return _gerar_excel_qualificacao(dados_cabecalho, aulas, caminho_modelo_padrao, caminho_saida, aplicar_logo=True)

    # 2. Caso o usuário TENHA subido um modelo:
    if not os.path.exists(caminho_modelo):
        print(f"[AVISO] Arquivo de modelo customizado '{caminho_modelo}' não encontrado. Usando fallback.")
        tipo = detectar_tipo_curso(modalidade, nome_curso)
        caminho_modelo_padrao = obter_caminho_modelo(tipo)
        if tipo == "tecnico":
            return _gerar_excel_tecnico(dados_cabecalho, aulas, caminho_modelo_padrao, caminho_saida, aplicar_logo=True)
        else:
            return _gerar_excel_qualificacao(dados_cabecalho, aulas, caminho_modelo_padrao, caminho_saida, aplicar_logo=True)

    # Inspeciona a estrutura do modelo enviado
    tipo_estrutura, eh_custom = identificar_estrutura_modelo(caminho_modelo)
    print(f"[INFO] Estrutura do modelo enviado identificada como: '{tipo_estrutura}' (Customizado={eh_custom})")

    if tipo_estrutura == "tecnico":
        # Segue layout técnico, mas preserva a logo/identidade original do arquivo enviado
        return _gerar_excel_tecnico(dados_cabecalho, aulas, caminho_modelo, caminho_saida, aplicar_logo=False)
    elif tipo_estrutura == "qualificacao":
        # Segue layout qualificação, preservando a logo/identidade original
        return _gerar_excel_qualificacao(dados_cabecalho, aulas, caminho_modelo, caminho_saida, aplicar_logo=False)
    else:
        # Preenchimento flexível para modelos personalizados de qualquer instituição de ensino
        return _gerar_excel_customizado(dados_cabecalho, aulas, caminho_modelo, caminho_saida)


def _gerar_excel_customizado(dados_cabecalho, aulas, caminho_modelo, caminho_saida=None):
    """
    Preenche uma planilha de planejamento docente personalizada enviada pelo professor ou instituição,
    detectando dinamicamente os cabeçalhos institucionais e as colunas da tabela de aulas.
    Preserva estilos, fontes, fórmulas, células mescladas e identidades visuais originais.
    """
    wb = _carregar_workbook_excel(caminho_modelo)
    ws = _obter_planilha_ativa_segura(wb)

    # 1. Localiza a linha do cabeçalho da tabela de aulas
    linha_cabecalho = None
    mapeamento_colunas = {}
    maior_score = 0

    palavras_chave_colunas = {
        "conteudo": ["conteudo", "conteudos", "capacidade", "capacidades", "competencia", "competencias", "o que", "tema", "objeto de conhecimento", "conhecimento", "conhecimentos", "descricao", "descricao da aula", "plano de aula", "assunto", "unidade tematica"],
        "estrategia": ["estrategia", "estrategias", "metodologia", "metodologias", "como", "procedimento", "procedimentos", "acao docente", "desenvolvimento", "atividades", "pratica pedagogica"],
        "recursos": ["recurso", "recursos", "recursos didaticos", "material", "materiais", "equipamento", "equipamentos", "ferramentas"],
        "ambiente": ["ambiente", "ambientes", "local", "onde", "espaco", "sala", "laboratorio"],
        "carga_horaria": ["carga horaria", "ch", "horas", "duracao", "tempo"],
        "criterios": ["criterio", "criterios", "criterios de avaliacao", "avaliacao", "desempenho"],
        "instrumentos": ["instrumento", "instrumentos", "instrumentos de avaliacao", "forma de avaliacao"],
        "situacao": ["situacao", "situacao de aprendizagem", "projeto", "desafio", "sa"],
        "data_inicio": ["data inicio", "inicio", "quando", "data"],
        "data_fim": ["data fim", "termino", "fim"],
        "dia": ["aula", "dia", "encontro", "no", "item", "seq"]
    }

    limite_busca_linhas = min(max(ws.max_row, 1) + 1, 40)
    limite_busca_cols = min(max(ws.max_column, 1) + 1, 35)

    for r in range(1, limite_busca_linhas):
        col_map_temp = {}
        score = 0
        for c in range(1, limite_busca_cols):
            val = normalizar_texto_chave(ws.cell(row=r, column=c).value or "")
            if not val:
                continue
            for tipo_col, termos in palavras_chave_colunas.items():
                if any(t in val for t in termos):
                    if tipo_col not in col_map_temp:
                        col_map_temp[tipo_col] = c
                        score += 1
                    break
        if score > maior_score and score >= 2:
            maior_score = score
            linha_cabecalho = r
            mapeamento_colunas = col_map_temp

    print(f"[INFO] Modelo customizado: Linha de cabeçalho detectada = {linha_cabecalho} (Score: {maior_score})")
    print(f"[INFO] Mapeamento de colunas detectado: {mapeamento_colunas}")

    # 2. Preenche os campos institucionais do cabeçalho (linhas acima da tabela de aulas)
    # Usa _obter_celula_valor_ao_lado para lidar com rótulos e campos em células mescladas sem erro read-only
    limite_cabecalho = (linha_cabecalho - 1) if linha_cabecalho else min(12, max(ws.max_row, 1))
    for r in range(1, limite_cabecalho + 1):
        for c in range(1, limite_busca_cols):
            cell_atual = ws.cell(row=r, column=c)
            if isinstance(cell_atual, MergedCell):
                continue
            val = normalizar_texto_chave(cell_atual.value or "")
            if not val or len(val) > 45:
                continue

            valor_inserir = None
            if any(k in val for k in ["curso", "nome do curso"]):
                valor_inserir = str(dados_cabecalho.get("nome_curso", "")).strip().upper()
            elif any(k in val for k in ["unidade curricular", "componente", "disciplina", "uc"]):
                valor_inserir = str(dados_cabecalho.get("unidade_curricular", "")).strip()
            elif "turma" in val and len(val) < 20:
                valor_inserir = str(dados_cabecalho.get("codigo_turma", "")).strip().upper()
            elif any(k in val for k in ["professor", "docente", "instrutor"]):
                valor_inserir = str(dados_cabecalho.get("instrutor", "")).strip().upper()
            elif any(k in val for k in ["unidade escolar", "escola", "instituicao", "centro"]) and dados_cabecalho.get("unidade_escolar"):
                valor_inserir = str(dados_cabecalho["unidade_escolar"]).strip()
            elif any(k in val for k in ["carga horaria total", "ch total", "ch da uc", "carga horaria"]):
                valor_inserir = dados_cabecalho.get("carga_horaria", "")
            elif any(k in val for k in ["periodo", "datas"]):
                d_ini = dados_cabecalho.get("data_inicio", "")
                d_fim = dados_cabecalho.get("data_fim", "")
                if d_ini and d_fim:
                    valor_inserir = f"{d_ini} a {d_fim}"

            if valor_inserir is not None and str(valor_inserir).strip():
                alvo = _obter_celula_valor_ao_lado(ws, r, c)
                if alvo is not None and (not alvo.value or str(alvo.value).strip() in ("", ":", "-")):
                    alvo.value = valor_inserir

    # 3. Preenche as linhas de aulas
    # Avança além de eventual linha de cabeçalho mesclada verticalmente (ex: cabeçalho em 2 linhas)
    if linha_cabecalho:
        max_row_cab = linha_cabecalho
        for rng in ws.merged_cells.ranges:
            if rng.min_row <= linha_cabecalho <= rng.max_row:
                max_row_cab = max(max_row_cab, rng.max_row)
        linha_inicio_dados = max_row_cab + 1
    else:
        linha_inicio_dados = 13

    # Captura padrão de merges horizontais da primeira linha de dados (se existir no modelo)
    merges_horizontais_modelo = []
    for rng in ws.merged_cells.ranges:
        if rng.min_row == linha_inicio_dados and rng.max_row == linha_inicio_dados and rng.max_col > rng.min_col:
            merges_horizontais_modelo.append((rng.min_col, rng.max_col))

    # Remove merges e linhas antigas de exemplo a partir de linha_inicio_dados para evitar MergedCell read-only
    _remover_merges_a_partir_da_linha(ws, linha_inicio_dados)
    if ws.max_row >= linha_inicio_dados:
        ws.delete_rows(linha_inicio_dados, ws.max_row - linha_inicio_dados + 1)

    # Se não mapeou coluna de conteúdo, assume a coluna 1
    if "conteudo" not in mapeamento_colunas:
        mapeamento_colunas["conteudo"] = 1

    # Garante que nenhuma coluna mapeada caia dentro do meio de um merge horizontal do modelo
    if merges_horizontais_modelo:
        for k_col, col_num in list(mapeamento_colunas.items()):
            for c_min, c_max in merges_horizontais_modelo:
                if c_min <= col_num <= c_max:
                    mapeamento_colunas[k_col] = c_min
                    break

    max_col_usada = max([limite_busca_cols - 1] + list(mapeamento_colunas.values()) + [c_max for _, c_max in merges_horizontais_modelo])

    fonte_padrao = Font(name="Calibri", size=10)
    borda_fina = Border(
        left=Side(style="thin", color="CBD5E1"),
        right=Side(style="thin", color="CBD5E1"),
        top=Side(style="thin", color="CBD5E1"),
        bottom=Side(style="thin", color="CBD5E1")
    )
    alin_esquerda = Alignment(horizontal="left", vertical="top", wrap_text=True)
    alin_centro = Alignment(horizontal="center", vertical="center", wrap_text=True)

    for idx, aula in enumerate(aulas):
        r = linha_inicio_dados + idx

        # Prepara textos pedagógicos
        cap = limpar_numeracao_texto(aula.get("capacidade", "")).strip()
        con = limpar_numeracao_texto(aula.get("conhecimento", ""), separador="\n").strip()
        if cap and con:
            texto_conteudo = f"CAPACIDADE:\n{cap}\n\nCONHECIMENTOS:\n{con}"
        else:
            texto_conteudo = cap or con or "Desenvolvimento de competências técnicas"

        if "dia" in mapeamento_colunas:
            _escrever_celula_segura(ws, r, mapeamento_colunas["dia"], f"Aula {idx+1}")

        if "conteudo" in mapeamento_colunas:
            _escrever_celula_segura(ws, r, mapeamento_colunas["conteudo"], texto_conteudo)

        if "estrategia" in mapeamento_colunas:
            _escrever_celula_segura(ws, r, mapeamento_colunas["estrategia"], str(aula.get("estrategia", "")).strip())

        if "ambiente" in mapeamento_colunas:
            _escrever_celula_segura(ws, r, mapeamento_colunas["ambiente"], str(aula.get("ambiente", "Sala / Laboratório")).strip())

        if "recursos" in mapeamento_colunas:
            _escrever_celula_segura(ws, r, mapeamento_colunas["recursos"], str(aula.get("recursos", "Computadores / Recursos Didáticos")).strip())

        if "carga_horaria" in mapeamento_colunas:
            ch_val = str(aula.get("carga_horaria_str") or f"{aula.get('horas_aula', 4)}h")
            _escrever_celula_segura(ws, r, mapeamento_colunas["carga_horaria"], ch_val)

        if "criterios" in mapeamento_colunas:
            _escrever_celula_segura(ws, r, mapeamento_colunas["criterios"], str(aula.get("criterios", "")).strip())

        if "instrumentos" in mapeamento_colunas:
            _escrever_celula_segura(ws, r, mapeamento_colunas["instrumentos"], str(aula.get("instrumentos", "")).strip())

        if "situacao" in mapeamento_colunas:
            sa_texto = obter_situacao_aprendizagem_padrao(aula, dados_cabecalho)
            _escrever_celula_segura(ws, r, mapeamento_colunas["situacao"], sa_texto)

        dt_ini = aula.get("data_inicio")
        dt_fim = aula.get("data_fim", dt_ini)
        if "data_inicio" in mapeamento_colunas:
            val_dt = dt_ini.strftime("%d/%m/%Y") if isinstance(dt_ini, datetime) else (str(dt_ini) if dt_ini else "")
            _escrever_celula_segura(ws, r, mapeamento_colunas["data_inicio"], val_dt)

        if "data_fim" in mapeamento_colunas and mapeamento_colunas["data_fim"] != mapeamento_colunas.get("data_inicio"):
            val_dt2 = dt_fim.strftime("%d/%m/%Y") if isinstance(dt_fim, datetime) else (str(dt_fim) if dt_fim else "")
            _escrever_celula_segura(ws, r, mapeamento_colunas["data_fim"], val_dt2)

        # Aplica estilos e bordas com segurança
        for col_idx in range(1, max_col_usada + 1):
            c_cell = ws.cell(row=r, column=col_idx)
            if not c_cell.font or c_cell.font.name != "Calibri":
                c_cell.font = fonte_padrao
            if _celula_sem_borda(c_cell):
                c_cell.border = borda_fina
            if col_idx == mapeamento_colunas.get("conteudo"):
                c_cell.alignment = alin_esquerda
            elif col_idx in (mapeamento_colunas.get("data_inicio"), mapeamento_colunas.get("data_fim"), mapeamento_colunas.get("carga_horaria"), mapeamento_colunas.get("dia")):
                c_cell.alignment = alin_centro
            else:
                c_cell.alignment = alin_esquerda

        # Reaplica merges horizontais da linha modelo, se houver
        for c_min, c_max in merges_horizontais_modelo:
            try:
                ws.merge_cells(start_row=r, start_column=c_min, end_row=r, end_column=c_max)
            except Exception:
                pass

        ws.row_dimensions[r].height = calcular_altura_linha(
            texto_conteudo,
            str(aula.get("estrategia", "")),
            str(aula.get("recursos", "")),
            str(aula.get("criterios", "")),
            str(aula.get("instrumentos", ""))
        )

    if not caminho_saida:
        os.makedirs("outputs", exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        nome_uc_safe = _sanitizar_nome_arquivo(dados_cabecalho.get("unidade_curricular", "UC"))
        caminho_saida = f"outputs/Planejamento_Docente_Personalizado_{nome_uc_safe}_{timestamp}.xlsx"

    wb.save(caminho_saida)
    wb.close()
    print(f"[OK] Planejamento Docente gerado em modelo personalizado: {caminho_saida}")
    return caminho_saida

def _gerar_excel_qualificacao(dados_cabecalho, aulas, caminho_modelo, caminho_saida, aplicar_logo=True):
    """
    Preenche o modelo de Qualificação/Aperfeiçoamento (.xlsx).
    Layout (colunas):
      A:C  = O que? (Capacidade / Conhecimento)
      D    = Como? (Estratégia de Ensino)
      E:G  = Onde? (Ambientes Pedagógicos)
      H:I  = Recursos Didáticos
      J:L  = Carga Horária
      M:O  = Critérios de Avaliação
      P    = Instrumento de Avaliação
      Q    = Situação de Aprendizagem
      R    = Data Início
      S    = Data Fim
    """
    wb = _carregar_workbook_excel(caminho_modelo)
    ws = _obter_planilha_ativa_segura(wb)

    # ==========================================
    # 1. ATUALIZA O CABEÇALHO (Linhas 5 a 8)
    # ==========================================
    if dados_cabecalho.get("unidade_escolar"):
        _escrever_celula_segura(ws, "C5", valor=str(dados_cabecalho["unidade_escolar"]).strip())

    data_ini = dados_cabecalho.get("data_inicio", "")
    data_fim = dados_cabecalho.get("data_fim", "")
    if isinstance(data_ini, datetime):
        data_ini = data_ini.strftime("%d/%m/%Y")
    if isinstance(data_fim, datetime):
        data_fim = data_fim.strftime("%d/%m/%Y")
    if data_ini and data_fim:
        _escrever_celula_segura(ws, "L5", valor=f"{data_ini} a {data_fim}")
    elif data_ini:
        _escrever_celula_segura(ws, "L5", valor=f"A partir de {data_ini}")

    if dados_cabecalho.get("nome_curso"):
        _escrever_celula_segura(ws, "C6", valor=str(dados_cabecalho["nome_curso"]).strip().upper())

    modalidade = dados_cabecalho.get("modalidade", "QUALIFICAÇÃO PROFISSIONAL")
    _escrever_celula_segura(ws, "L6", valor=f" {modalidade.strip().upper()} ")

    turma = dados_cabecalho.get("codigo_turma", "QUAL.2026.1")
    _escrever_celula_segura(ws, "C7", valor=f" {turma.strip().upper()}")

    if dados_cabecalho.get("carga_horaria"):
        try:
            _escrever_celula_segura(ws, "L7", valor=float(dados_cabecalho["carga_horaria"]))
        except (ValueError, TypeError):
            _escrever_celula_segura(ws, "L7", valor=str(dados_cabecalho["carga_horaria"]))

    if dados_cabecalho.get("unidade_curricular"):
        _escrever_celula_segura(ws, "C8", valor=str(dados_cabecalho["unidade_curricular"]).strip())

    if dados_cabecalho.get("instrutor"):
        _escrever_celula_segura(ws, "L8", valor=str(dados_cabecalho["instrutor"]).strip().upper())

    # ==========================================
    # 2. LIMPAR LINHAS DE DADOS (a partir da linha 13)
    # ==========================================
    _remover_merges_a_partir_da_linha(ws, 13)

    if ws.max_row >= 13:
        ws.delete_rows(13, ws.max_row - 12)

    # Estilos
    fonte_padrao = Font(name="Calibri", size=10, bold=False)
    alin_esquerda_topo = Alignment(horizontal="left", vertical="top", wrap_text=True)
    alin_centro = Alignment(horizontal="center", vertical="center", wrap_text=True)
    alin_data = Alignment(horizontal="center", vertical="center", wrap_text=False)
    borda_fina = Border(
        left=Side(style="thin", color="000000"),
        right=Side(style="thin", color="000000"),
        top=Side(style="thin", color="000000"),
        bottom=Side(style="thin", color="000000")
    )

    # ==========================================
    # 3. PREENCHER AS AULAS
    # ==========================================
    total_aulas = len(aulas)
    linha_inicio_dados = 13

    for idx, aula in enumerate(aulas):
        r = linha_inicio_dados + idx

        cap_limpa = limpar_numeracao_texto(aula.get("capacidade", "")).strip()
        con_limpo = limpar_numeracao_texto(aula.get("conhecimento", ""), separador="\n").strip()
        titulo_cap = " " * 19 + "CAPACIDADE:"
        titulo_con = " " * 17 + "CONHECIMENTOS:"
        texto_o_que = f"{titulo_cap}\n{cap_limpa}\n\n{titulo_con}\n{con_limpo}"

        ws.cell(row=r, column=1, value=texto_o_que)   # A: O que?

        estrategia_txt = str(aula.get("estrategia", "")).strip().upper()
        ws.cell(row=r, column=4, value=estrategia_txt)  # D: Como?

        ws.cell(row=r, column=5, value=str(aula.get("ambiente", "LABORATORIO T.I")).strip().upper())  # E: Onde?

        recursos_txt = str(aula.get("recursos", "NOTEBOOK\nTELA DIGITAL\nCOMPUTADORES")).strip().upper()
        ws.cell(row=r, column=8, value=recursos_txt)  # H: Recursos

        ws.cell(row=r, column=10, value=str(aula.get("carga_horaria_str", "4 HS")).strip().upper())  # J: CH

        criterios_txt = str(aula.get("criterios", "")).strip().upper()
        ws.cell(row=r, column=13, value=criterios_txt)  # M: Critérios

        instrumentos_txt = str(aula.get("instrumentos", "")).strip().upper()
        ws.cell(row=r, column=16, value=instrumentos_txt)  # P: Instrumentos

        ws.cell(row=r, column=17, value=obter_situacao_aprendizagem_padrao(aula, dados_cabecalho))  # Q: SA

        # R e S: Datas
        dt_ini = aula.get("data_inicio")
        dt_fim = aula.get("data_fim", dt_ini)
        cell_r = ws.cell(row=r, column=18)
        cell_s = ws.cell(row=r, column=19)
        if isinstance(dt_ini, datetime):
            cell_r.value = dt_ini
            cell_r.number_format = "DD/MM/YYYY"
        else:
            cell_r.value = str(dt_ini) if dt_ini else ""
        if isinstance(dt_fim, datetime):
            cell_s.value = dt_fim
            cell_s.number_format = "DD/MM/YYYY"
        else:
            cell_s.value = str(dt_fim) if dt_fim else ""

        ws.row_dimensions[r].height = calcular_altura_linha(texto_o_que, estrategia_txt, recursos_txt, criterios_txt, instrumentos_txt)

        for col_idx in range(1, 20):
            c_obj = ws.cell(row=r, column=col_idx)
            c_obj.font = fonte_padrao
            c_obj.border = borda_fina
            if col_idx in (1, 2, 3, 13, 14, 15):
                c_obj.alignment = alin_esquerda_topo
            elif col_idx in (18, 19):
                c_obj.alignment = alin_data
            else:
                c_obj.alignment = alin_centro

        ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=3)    # A:C
        ws.merge_cells(start_row=r, start_column=5, end_row=r, end_column=7)    # E:G
        ws.merge_cells(start_row=r, start_column=8, end_row=r, end_column=9)    # H:I
        ws.merge_cells(start_row=r, start_column=10, end_row=r, end_column=12)  # J:L
        ws.merge_cells(start_row=r, start_column=13, end_row=r, end_column=15)  # M:O

    # Mescla coluna Q (Situação de Aprendizagem) por toda a extensão
    if total_aulas > 1:
        ws.merge_cells(start_row=linha_inicio_dados, start_column=17, end_row=linha_inicio_dados + total_aulas - 1, end_column=17)
        ws.cell(row=linha_inicio_dados, column=17).alignment = alin_centro

    if not caminho_saida:
        os.makedirs("outputs", exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        nome_uc_safe = _sanitizar_nome_arquivo(dados_cabecalho.get("unidade_curricular", "UC"))
        caminho_saida = f"outputs/Planejamento_Docente_Qualificacao_{nome_uc_safe}_{timestamp}.xlsx"

    wb.save(caminho_saida)
    wb.close()
    if aplicar_logo:
        logo_user_path = dados_cabecalho.get("logo_path")
        garantir_logo_oficial(
            caminho_saida,
            logo_custom_path=logo_user_path,
            unidade_escolar=dados_cabecalho.get("unidade_escolar", ""),
        )
    return caminho_saida


def _gerar_excel_tecnico(dados_cabecalho, aulas, caminho_modelo, caminho_saida, aplicar_logo=True):
    """
    Preenche o modelo de Curso Técnico (.xlsx) oficial usando openpyxl.
    Layout (colunas):
      A:C (1:3)   = O que? (Capacidade / Conhecimento)
      D   (4)     = Identificação - MATRIZ DE REFERÊNCIA SAEP
      E   (5)     = Como? (Estratégia de Ensino)
      F:H (6:8)   = Onde? (Ambientes Pedagógicos)
      I:J (9:10)  = Recursos Didáticos
      K:M (11:13) = Carga Horária
      N:P (14:16) = Critérios de Avaliação
      Q   (17)    = Instrumento de Avaliação da Aprendizagem
      R   (18)    = Situação de Aprendizagem
      S   (19)    = Quando - Início
      T   (20)    = Quando - Fim
    """
    wb = _carregar_workbook_excel(caminho_modelo)
    ws = _obter_planilha_ativa_segura(wb)

    # ==========================================
    # 1. ATUALIZA O CABEÇALHO (Linhas 5 a 8)
    # ==========================================
    if dados_cabecalho.get("unidade_escolar"):
        _escrever_celula_segura(ws, "C5", valor=str(dados_cabecalho["unidade_escolar"]).strip())

    data_ini = dados_cabecalho.get("data_inicio", "")
    data_fim = dados_cabecalho.get("data_fim", "")
    if isinstance(data_ini, datetime):
        data_ini = data_ini.strftime("%d/%m/%Y")
    if isinstance(data_fim, datetime):
        data_fim = data_fim.strftime("%d/%m/%Y")
    if data_ini and data_fim:
        _escrever_celula_segura(ws, "M5", valor=f"{data_ini} à {data_fim}")
    elif data_ini:
        _escrever_celula_segura(ws, "M5", valor=f"A partir de {data_ini}")

    if dados_cabecalho.get("nome_curso"):
        _escrever_celula_segura(ws, "C6", valor=str(dados_cabecalho["nome_curso"]).strip().upper())

    modalidade = dados_cabecalho.get("modalidade", "HABILITAÇÃO TÉCNICA")
    _escrever_celula_segura(ws, "M6", valor=f" {modalidade.strip().upper()} ")

    turma = dados_cabecalho.get("codigo_turma", "TEC.2026.1")
    _escrever_celula_segura(ws, "C7", valor=f" {turma.strip().upper()}")

    if dados_cabecalho.get("carga_horaria"):
        try:
            _escrever_celula_segura(ws, "M7", valor=float(dados_cabecalho["carga_horaria"]))
        except (ValueError, TypeError):
            _escrever_celula_segura(ws, "M7", valor=str(dados_cabecalho["carga_horaria"]))

    if dados_cabecalho.get("unidade_curricular"):
        _escrever_celula_segura(ws, "C8", valor=str(dados_cabecalho["unidade_curricular"]).strip())

    if dados_cabecalho.get("instrutor"):
        _escrever_celula_segura(ws, "M8", valor=str(dados_cabecalho["instrutor"]).strip().upper())

    # ==========================================
    # 2. LIMPAR LINHAS DE DADOS DE EXEMPLO (a partir da linha 13)
    # ==========================================
    _remover_merges_a_partir_da_linha(ws, 13)

    if ws.max_row >= 13:
        ws.delete_rows(13, ws.max_row - 12)

    # Estilos
    fonte_padrao = Font(name="Calibri", size=10, bold=False)
    fonte_sa = Font(name="Calibri", size=11, bold=False)
    alin_esquerda_centro = Alignment(horizontal="left", vertical="center", wrap_text=True)
    alin_centro = Alignment(horizontal="center", vertical="center", wrap_text=True)
    alin_data = Alignment(horizontal="center", vertical="center", wrap_text=False)
    borda_fina = Border(
        left=Side(style="thin", color="000000"),
        right=Side(style="thin", color="000000"),
        top=Side(style="thin", color="000000"),
        bottom=Side(style="thin", color="000000")
    )

    # ==========================================
    # 3. PREENCHER AS AULAS
    # ==========================================
    total_aulas = len(aulas)
    linha_inicio_dados = 13

    for idx, aula in enumerate(aulas):
        r = linha_inicio_dados + idx

        cap_limpa = limpar_numeracao_texto(aula.get("capacidade", "")).strip()
        con_limpo = limpar_numeracao_texto(aula.get("conhecimento", ""), separador="\n").strip()
        titulo_cap = " " * 15 + "CAPACIDADE:"
        titulo_con = " " * 13 + "CONHECIMENTOS:"
        texto_o_que = f"{titulo_cap}\n{cap_limpa}\n\n{titulo_con}\n{con_limpo}"

        # A: O que?
        ws.cell(row=r, column=1, value=texto_o_que)

        # D: Matriz de Referência SAEP
        saep_txt = str(aula.get("saep") or "").strip()
        if not saep_txt:
            saep_txt = "Não tem cruzamento"
        ws.cell(row=r, column=4, value=saep_txt)

        # E: Como? Estratégia
        estrategia_txt = str(aula.get("estrategia", "")).strip().upper()
        ws.cell(row=r, column=5, value=estrategia_txt)

        # F: Onde? Ambientes
        ws.cell(row=r, column=6, value=str(aula.get("ambiente", "LABORATORIO T.I")).strip().upper())

        # I: Recursos Didáticos
        recursos_txt = str(aula.get("recursos", "NOTEBOOK\nTELA DIGITAL\nCOMPUTADORES")).strip().upper()
        ws.cell(row=r, column=9, value=recursos_txt)

        # K: Carga Horária
        ws.cell(row=r, column=11, value=str(aula.get("carga_horaria_str", "4 HS")).strip().upper())

        # N: Critérios de Avaliação
        criterios_txt = str(aula.get("criterios", "")).strip().upper()
        ws.cell(row=r, column=14, value=criterios_txt)

        # Q: Instrumento de Avaliação
        instrumentos_txt = str(aula.get("instrumentos", "")).strip().upper()
        ws.cell(row=r, column=17, value=instrumentos_txt)

        # R: Situação de Aprendizagem
        ws.cell(row=r, column=18, value=obter_situacao_aprendizagem_padrao(aula, dados_cabecalho))

        # S e T: Datas
        dt_ini = aula.get("data_inicio")
        dt_fim = aula.get("data_fim", dt_ini)
        cell_s = ws.cell(row=r, column=19)
        cell_t = ws.cell(row=r, column=20)
        if isinstance(dt_ini, datetime):
            cell_s.value = dt_ini
            cell_s.number_format = "DD/MM/YYYY"
        else:
            cell_s.value = str(dt_ini) if dt_ini else ""
        if isinstance(dt_fim, datetime):
            cell_t.value = dt_fim
            cell_t.number_format = "DD/MM/YYYY"
        else:
            cell_t.value = str(dt_fim) if dt_fim else ""

        # Altura da linha
        ws.row_dimensions[r].height = calcular_altura_linha(texto_o_que, estrategia_txt, recursos_txt, criterios_txt, instrumentos_txt)

        # Aplica estilos e bordas
        for col_idx in range(1, 21):
            c_obj = ws.cell(row=r, column=col_idx)
            c_obj.font = fonte_sa if col_idx == 18 else fonte_padrao
            c_obj.border = borda_fina
            if col_idx in (1, 2, 3, 4, 14, 15, 16):
                c_obj.alignment = alin_esquerda_centro
            elif col_idx in (19, 20):
                c_obj.alignment = alin_data
            else:
                c_obj.alignment = alin_centro

        # Mesclagens por linha
        ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=3)    # A:C
        ws.merge_cells(start_row=r, start_column=6, end_row=r, end_column=8)    # F:H
        ws.merge_cells(start_row=r, start_column=9, end_row=r, end_column=10)   # I:J
        ws.merge_cells(start_row=r, start_column=11, end_row=r, end_column=13)  # K:M
        ws.merge_cells(start_row=r, start_column=14, end_row=r, end_column=16)  # N:P

    # Mescla coluna R (Situação de Aprendizagem) verticalmente caso haja mais de 1 aula
    if total_aulas > 1:
        ws.merge_cells(start_row=linha_inicio_dados, start_column=18, end_row=linha_inicio_dados + total_aulas - 1, end_column=18)
        ws.cell(row=linha_inicio_dados, column=18).alignment = alin_centro

    if not caminho_saida:
        os.makedirs("outputs", exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        nome_uc_safe = _sanitizar_nome_arquivo(dados_cabecalho.get("unidade_curricular", "UC"))
        caminho_saida = f"outputs/Planejamento_Docente_Tecnico_{nome_uc_safe}_{timestamp}.xlsx"

    wb.save(caminho_saida)
    wb.close()
    if aplicar_logo:
        logo_user_path = dados_cabecalho.get("logo_path")
        garantir_logo_oficial(
            caminho_saida,
            logo_custom_path=logo_user_path,
            unidade_escolar=dados_cabecalho.get("unidade_escolar", ""),
        )
    return caminho_saida
