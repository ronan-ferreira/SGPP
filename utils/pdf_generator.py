import os
import re
import html
try:
    import pythoncom
    from docx2pdf import convert
except ImportError:
    pythoncom = None
    convert = None
import docx

from reportlab.lib.pagesizes import A4
from reportlab.platypus import (
    SimpleDocTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
    Image,
    HRFlowable,
)
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib import colors
from reportlab.pdfgen import canvas
from utils.imagem_utils import obter_dimensoes_proporcionais


class PlanoNumberedCanvas(canvas.Canvas):
    """Canvas com numeração de páginas e rodapé institucional dinâmico para planos de aula."""
    def __init__(self, *args, texto_rodape="SGPP — Sistema de Gestão e Planejamento Pedagógico • Plano de Aula", **kwargs):
        super().__init__(*args, **kwargs)
        self._saved_page_states = []
        self.texto_rodape = texto_rodape

    def showPage(self):
        self._saved_page_states.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        num_pages = len(self._saved_page_states)
        for state in self._saved_page_states:
            self.__dict__.update(state)
            self.draw_page_decorations(num_pages)
            canvas.Canvas.showPage(self)
        canvas.Canvas.save(self)

    def draw_page_decorations(self, page_count):
        self.saveState()
        self.setFont("Helvetica", 8)
        self.setFillColor(colors.HexColor("#64748B"))
        self.setStrokeColor(colors.HexColor("#CBD5E1"))
        self.setLineWidth(0.6)
        self.line(36, 36, 559, 36)
        rodape = self.texto_rodape or "SGPP — Sistema de Gestão e Planejamento Pedagógico • Plano de Aula"
        self.drawString(36, 24, rodape[:75])
        self.drawRightString(559, 24, f"Página {self._pageNumber} de {page_count}")
        self.restoreState()


def _formatar_texto_markdown(texto):
    if not texto:
        return ""
    txt = html.escape(texto)
    txt = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", txt)
    txt = re.sub(r"__(.+?)__", r"<b>\1</b>", txt)
    txt = re.sub(r"(?<!\*)\*([^*]+?)\*(?!\*)", r"<i>\1</i>", txt)
    return txt


def gerar_pdf_plano_reportlab(docx_path, pdf_path, titulo=None, subtitulo=None, conteudo=None, logo_path=None, instituicao=None):
    """Converte ou gera o PDF do plano de aula utilizando ReportLab (sem logo fixa de terceiros)."""
    pasta = os.path.dirname(pdf_path)
    if pasta:
        os.makedirs(pasta, exist_ok=True)

    linhas_plano = []
    curso_subtitulo = subtitulo or ""

    if conteudo:
        linhas_plano = conteudo.split("\n")
    elif docx_path and os.path.exists(docx_path):
        try:
            doc_docx = docx.Document(docx_path)
            for p in doc_docx.paragraphs:
                txt = p.text.strip()
                if not txt:
                    continue
                if not curso_subtitulo and (txt.startswith("Curso:") or "plano" in txt.lower()):
                    curso_subtitulo = txt
                linhas_plano.append(txt)
        except Exception as e:
            print(f"Aviso ao ler DOCX do plano: {e}")

    pdf_doc = SimpleDocTemplate(
        pdf_path,
        pagesize=A4,
        leftMargin=36,
        rightMargin=36,
        topMargin=36,
        bottomMargin=48
    )

    styles = getSampleStyleSheet()

    style_titulo = ParagraphStyle(
        "PlanoTitulo",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=15,
        leading=18,
        textColor=colors.HexColor("#004696"),
        alignment=1
    )

    style_subtitulo = ParagraphStyle(
        "PlanoSubtitulo",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=11,
        leading=15,
        textColor=colors.HexColor("#003366"),
        spaceBefore=4,
        spaceAfter=6
    )

    style_dia = ParagraphStyle(
        "PlanoDia",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=12,
        leading=16,
        textColor=colors.HexColor("#FFFFFF"),
        spaceBefore=0,
        spaceAfter=0
    )

    style_secao = ParagraphStyle(
        "PlanoSecao",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=10,
        leading=14,
        textColor=colors.HexColor("#004696"),
        spaceBefore=7,
        spaceAfter=3,
        keepWithNext=True
    )

    style_texto = ParagraphStyle(
        "PlanoTexto",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=9,
        leading=13,
        textColor=colors.HexColor("#1E293B"),
        spaceBefore=1,
        spaceAfter=2
    )

    style_item = ParagraphStyle(
        "PlanoItem",
        parent=style_texto,
        leftIndent=14,
        spaceBefore=1,
        spaceAfter=2
    )

    story = []

    # Cabeçalho com Logo (somente se enviada pelo professor) e Nome da Instituição
    titulo_texto = html.escape(str(titulo or "PLANO DE AULA"))
    instituicao_txt = (instituicao or "").strip()

    header_elements = []
    if instituicao_txt:
        style_inst = ParagraphStyle(
            "PlanoInstituicao",
            parent=styles["Normal"],
            fontName="Helvetica-Bold",
            fontSize=10,
            leading=13,
            textColor=colors.HexColor("#475569"),
            alignment=1,
        )
        header_elements.append(Paragraph(f"<b>{html.escape(instituicao_txt.upper())}</b>", style_inst))
        header_elements.append(Spacer(1, 2))

    header_elements.append(Paragraph(f"<b>{titulo_texto}</b>", style_titulo))

    if logo_path and os.path.exists(logo_path):
        try:
            larg_logo, alt_logo = obter_dimensoes_proporcionais(logo_path, max_w=115, max_h=38)
            img_logo = Image(logo_path, width=larg_logo, height=alt_logo)
            img_logo.hAlign = "CENTER"
            col_logo_w = max(120, larg_logo + 5)
            col_texto_w = 523 - col_logo_w
            t_header = Table([[img_logo, header_elements]], colWidths=[col_logo_w, col_texto_w])
            t_header.setStyle(TableStyle([
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("ALIGN", (1, 0), (1, 0), "CENTER"),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                ("TOPPADDING", (0, 0), (-1, -1), 0),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
            ]))
            story.append(t_header)
        except Exception:
            for el in header_elements:
                story.append(el)
    else:
        for el in header_elements:
            story.append(el)

    story.append(Spacer(1, 4))
    if curso_subtitulo:
        story.append(Paragraph(_formatar_texto_markdown(curso_subtitulo), style_subtitulo))
    story.append(HRFlowable(width="100%", thickness=1.5, color=colors.HexColor("#004696"), spaceBefore=2, spaceAfter=8))

    for linha in linhas_plano:
        l_strip = linha.strip()
        if not l_strip:
            story.append(Spacer(1, 3))
            continue

        l_fmt = _formatar_texto_markdown(l_strip)

        if re.match(r"^DIA\s+\d+", l_strip, re.IGNORECASE):
            # Banner do Dia
            t_dia = Table([[Paragraph(html.escape(l_strip.upper()), style_dia)]], colWidths=[523])
            t_dia.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#004696")),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                ("LEFTPADDING", (0, 0), (-1, -1), 6),
                ("RIGHTPADDING", (0, 0), (-1, -1), 6),
            ]))
            story.append(Spacer(1, 6))
            story.append(t_dia)
            story.append(Spacer(1, 4))
        elif re.match(r"^\d+[\.\:\-–]\s*(Identificação|Objetivos|Conteúdo|Conteudo|Estratégia|Estrategia|Recursos|Avaliação|Avaliacao|Metodologia)", l_strip, re.IGNORECASE):
            story.append(Paragraph(l_fmt, style_secao))
        elif l_strip.startswith("-") or l_strip.startswith("•") or l_strip.startswith("*"):
            item_limpo = l_strip.lstrip("-•* ").strip()
            story.append(Paragraph(f"• {_formatar_texto_markdown(item_limpo)}", style_item))
        else:
            story.append(Paragraph(l_fmt, style_texto))

    rodape_txt = (
        f"{instituicao_txt} • Plano de Aula"
        if instituicao_txt
        else "SGPP — Sistema de Gestão e Planejamento Pedagógico • Plano de Aula"
    )
    pdf_doc.build(story, canvasmaker=lambda *a, **kw: PlanoNumberedCanvas(*a, texto_rodape=rodape_txt, **kw))
    return True


def gerar_pdf_plano(docx_path, pdf_path, titulo=None, subtitulo=None, conteudo=None, logo_path=None, instituicao=None):
    """
    Gera o PDF do plano de aula nativamente via ReportLab com identidade dinâmica do docente.
    Se não for possível, tenta converter via docx2pdf.
    """
    pasta = os.path.dirname(pdf_path)
    if pasta:
        os.makedirs(pasta, exist_ok=True)

    # 1. Geração nativa com ReportLab (rápida, não depende do Microsoft Word)
    try:
        sucesso = gerar_pdf_plano_reportlab(
            docx_path=docx_path,
            pdf_path=pdf_path,
            titulo=titulo,
            subtitulo=subtitulo,
            conteudo=conteudo,
            logo_path=logo_path,
            instituicao=instituicao,
        )
        if sucesso and os.path.exists(pdf_path) and os.path.getsize(pdf_path) > 0:
            print(f"[OK] PDF do plano gerado via ReportLab: {pdf_path}")
            return True
    except Exception as e:
        print(f"Aviso ReportLab plano ({e}). Tentando docx2pdf...")

    # 2. Alternativa via docx2pdf
    if pythoncom and convert and docx_path and os.path.exists(docx_path):
        try:
            pythoncom.CoInitialize()
            convert(docx_path, pdf_path)
            if os.path.exists(pdf_path) and os.path.getsize(pdf_path) > 0:
                print(f"[OK] PDF do plano gerado via docx2pdf: {pdf_path}")
                return True
        except Exception as e:
            print(f"Aviso docx2pdf indisponível para plano ({e}).")
        finally:
            try:
                pythoncom.CoUninitialize()
            except:
                pass

    return os.path.exists(pdf_path) and os.path.getsize(pdf_path) > 0
