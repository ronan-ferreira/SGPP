"""
Módulo de Tratamento e Conformidade de Logomarcas Institucionais
Garante que logotipos de qualquer proporção (quadrados, horizontais, verticais)
sejam renderizados sem deformação, mantendo resolução, nitidez e proporção exata (aspect ratio)
em todos os formatos: Web, Excel (.xlsx), Word (.docx) e PDF.
"""

import io
import os
from PIL import Image as PILImage


def obter_dimensoes_proporcionais(caminho_imagem: str, max_w: float, max_h: float) -> tuple[float, float]:
    """
    Calcula largura e altura preservando rigorosamente o aspect ratio original da imagem,
    garantindo que caiba dentro do retângulo delimitador (max_w, max_h).
    """
    if not caminho_imagem or not os.path.exists(caminho_imagem):
        return max_w, max_h

    try:
        with PILImage.open(caminho_imagem) as img:
            orig_w, orig_h = img.size

        if orig_w <= 0 or orig_h <= 0:
            return max_w, max_h

        # Fator de escala mantendo a proporção exata (aspect ratio)
        escala = min(max_w / orig_w, max_h / orig_h)
        w_final = round(orig_w * escala, 2)
        h_final = round(orig_h * escala, 2)
        return w_final, h_final
    except Exception:
        return max_w, max_h


def processar_logo_canvas_excel(caminho_imagem: str, largura_canvas: int = 400, altura_canvas: int = 120) -> bytes:
    """
    Gera os bytes de um PNG com fundo transparente onde a logo (quadrada, vertical ou horizontal)
    é perfeitamente centralizada e dimensionada na proporção exata sem qualquer esticamento ou distorção.
    Ideal para substituição transparente em modelos de Excel com molduras pré-configuradas.
    """
    if not caminho_imagem or not os.path.exists(caminho_imagem):
        return b""

    try:
        with PILImage.open(caminho_imagem) as img:
            # Converte para RGBA garantindo transparência
            if img.mode != "RGBA":
                img = img.convert("RGBA")

            # Remove bordas transparentes vazias se existirem
            bbox = img.getbbox()
            if bbox:
                img = img.crop(bbox)

            orig_w, orig_h = img.size
            if orig_w <= 0 or orig_h <= 0:
                with open(caminho_imagem, "rb") as f:
                    return f.read()

            # Escala para caber no canvas com margem de segurança
            margem_x = int(largura_canvas * 0.04)
            margem_y = int(altura_canvas * 0.06)
            disp_w = max(10, largura_canvas - (margem_x * 2))
            disp_h = max(10, altura_canvas - (margem_y * 2))

            escala = min(disp_w / orig_w, disp_h / orig_h)
            novo_w = max(1, int(orig_w * escala))
            novo_h = max(1, int(orig_h * escala))

            # Redimensionamento de alta qualidade (Resampling Lanczos)
            resample_filter = getattr(PILImage, "Resampling", PILImage).LANCZOS
            img_redimensionada = img.resize((novo_w, novo_h), resample_filter)

            # Cria o canvas com fundo 100% transparente
            canvas = PILImage.new("RGBA", (largura_canvas, altura_canvas), (255, 255, 255, 0))

            # Posiciona alinhado à esquerda e centralizado verticalmente (estilo cabeçalho institucional)
            pos_x = margem_x
            pos_y = (altura_canvas - novo_h) // 2
            canvas.paste(img_redimensionada, (pos_x, pos_y), img_redimensionada)

            # Exporta bytes em PNG
            out_buffer = io.BytesIO()
            canvas.save(out_buffer, format="PNG", optimize=True)
            return out_buffer.getvalue()
    except Exception as e:
        print(f"[AVISO] Erro ao processar canvas da logo com Pillow: {e}")
        try:
            with open(caminho_imagem, "rb") as f:
                return f.read()
        except Exception:
            return b""


def otimizar_imagem_upload(caminho_arquivo: str) -> bool:
    """
    Otimiza a imagem após o upload, removendo metadados corrompidos,
    garantindo orientação correta (EXIF) e preservando canais de cor e transparência.
    """
    if not caminho_arquivo or not os.path.exists(caminho_arquivo):
        return False

    try:
        with PILImage.open(caminho_arquivo) as img:
            # Garante que a orientação EXIF seja respeitada (caso de fotos tiradas de celular)
            try:
                from PIL import ImageOps
                img = ImageOps.exif_transpose(img)
            except Exception:
                pass

            # Salva de volta otimizado
            ext = os.path.splitext(caminho_arquivo)[1].lower()
            if ext in (".png", ".webp"):
                img.save(caminho_arquivo, optimize=True)
            elif ext in (".jpg", ".jpeg"):
                img = img.convert("RGB")
                img.save(caminho_arquivo, "JPEG", quality=95, optimize=True)
        return True
    except Exception as e:
        print(f"[AVISO] Nao foi possivel reotimizar upload: {e}")
        return False
