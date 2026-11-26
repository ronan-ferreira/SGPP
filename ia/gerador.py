import os
import requests
import hashlib
import threading
import re
import logging
from dotenv import load_dotenv
from ia.ia_client import chamar_ia, obter_config_ia, verificar_ollama_online

# Carrega variáveis de ambiente do .env
load_dotenv()

logger = logging.getLogger("gerador_atividades")

# Cache global thread-safe com Lock
# Chaveado por hash do conteúdo — sem risco de cruzar dados entre usuários
_cache_lock = threading.Lock()
_cache_atividades = {}


def obter_config_ollama():
    """Mantido para compatibilidade retroativa."""
    cfg = obter_config_ia()["ollama"]
    return cfg["url"], cfg["model"], cfg["timeout"]


def extrair_topicos_relevantes(texto_base):
    """
    Extrai tópicos técnicos reais do texto base, ignorando completamente metadados
    administrativos (identificação, curso, carga horária, estratégias, recursos).
    Garante que perguntas geradas (pela IA ou fallback) tenham sentido pedagógico.
    """
    if not texto_base:
        return ["fundamentos técnicos e procedimentos operacionais"]

    topicos = []

    # 1. Se for um plano estruturado com seção "Conteúdo Programático"
    match_cp = re.search(
        r'Conte[uú]do\s+Program[aá]tico:?(.*?)(?=(?:\d+\.\s*)?Estrat[eé]gia|\Z)',
        texto_base,
        re.DOTALL | re.IGNORECASE
    )
    if match_cp:
        for linha in match_cp.group(1).splitlines():
            limpa = re.sub(r'^[\s\-\*•▪▫●○■□►▸\d\.\)]+', '', linha).strip()
            limpa = re.sub(r'\s+\d+$', '', limpa).strip()
            if len(limpa) >= 3:
                topicos.append(limpa)

    # 2. Se não encontrou em Conteúdo Programático, tenta extrair de Objetivos / Capacidades
    if not topicos:
        match_obj = re.search(
            r'Objetivos da Aula:?(.*?)(?=(?:\d+\.\s*)?Conte[uú]do|\Z)',
            texto_base,
            re.DOTALL | re.IGNORECASE
        )
        if match_obj:
            for linha in match_obj.group(1).splitlines():
                limpa = re.sub(
                    r'^(?:Desenvolver a capacidade|Meta de aprendizagem|Objetivo)[\s\:\-]+',
                    '',
                    linha,
                    flags=re.IGNORECASE
                )
                limpa = re.sub(r'^[\s\-\*•▪▫●○■□►▸\d\.\)]+', '', limpa).strip()
                limpa = re.sub(r'\s+\d+$', '', limpa).strip()
                if len(limpa) >= 6:
                    topicos.append(limpa)

    # 3. Filtragem de linhas eliminando cabeçalhos administrativos
    if not topicos:
        linhas = [l.strip() for l in texto_base.splitlines() if l.strip()]
        for l in linhas:
            if re.match(
                r'^(?:\d+[\.\)]\s*)?(?:Identifica|Curso|Unidade Curricular|Carga Hor|Aula\s*/\s*Encontro|N[ií]vel Cognitivo|Taxonomia|Estrat[eé]gia Did|Recursos Did|Abertura|Desenvolvimento|Fechamento|Notebook|Data:|Professor:)',
                l,
                re.IGNORECASE
            ):
                continue
            limpa = re.sub(r'^[\s\-\*•▪▫●○■□►▸\d\.\)]+', '', l).strip()
            limpa = re.sub(r'\s+\d+$', '', limpa).strip()
            if len(limpa) >= 10:
                topicos.append(limpa)

    # 4. Fallback final: quebra por pontuação ignorando termos administrativos
    if not topicos:
        frases = [f.strip() for f in re.split(r'[\.\;\n]+', texto_base) if len(f.strip()) >= 15]
        topicos = [
            re.sub(r'^\d+\s*', '', f).strip()
            for f in frases
            if not re.search(r'Identifica[cç][aã]o|Curso|Carga Hor[aá]ria|Aula|Encontro', f, re.IGNORECASE)
        ]

    return topicos if topicos else ["fundamentos técnicos e procedimentos operacionais"]


def extrair_conteudo_pedagogico(texto):
    """
    Higieniza o conteúdo para o prompt da IA: se for um plano de aula completo,
    extrai apenas os objetivos, o nível cognitivo e o conteúdo programático,
    eliminando metadados de cabeçalho e recursos que poderiam poluir a geração.
    """
    if not texto:
        return ""

    if "Conteudo Programatico" in texto or "Conteúdo Programático" in texto:
        partes = []

        m_bloom = re.search(
            r'N[ií]vel Cognitivo(?:\s*\(Taxonomia de Bloom\))?\s*:\s*([^\n\r]+)',
            texto,
            re.IGNORECASE
        )
        if m_bloom:
            partes.append(f"NÍVEL COGNITIVO ESPERADO (BLOOM): {m_bloom.group(1).strip()}")

        m_obj = re.search(
            r'Objetivos da Aula:?(.*?)(?=(?:\d+\.\s*)?Conte[uú]do|\Z)',
            texto,
            re.DOTALL | re.IGNORECASE
        )
        if m_obj:
            obj_texto = m_obj.group(1).strip()
            if obj_texto:
                partes.append(f"OBJETIVOS E CAPACIDADES:\n{obj_texto}")

        m_cp = re.search(
            r'Conte[uú]do\s+Program[aá]tico:?(.*?)(?=(?:\d+\.\s*)?Estrat[eé]gia|\Z)',
            texto,
            re.DOTALL | re.IGNORECASE
        )
        if m_cp:
            cp_texto = m_cp.group(1).strip()
            if cp_texto:
                partes.append(f"CONTEÚDO PROGRAMÁTICO (TÓPICOS TÉCNICOS):\n{cp_texto}")

        if partes:
            return "\n\n".join(partes)

    linhas = [l.strip() for l in texto.replace("\r", " ").split("\n") if l.strip()]
    linhas_filtradas = [
        l for l in linhas
        if not re.match(
            r'^(?:\d+[\.\)]\s*)?(?:Identifica|Recursos Did|Estrat[eé]gia Did|Carga Hor|Data:|Professor:)',
            l,
            re.IGNORECASE
        )
    ]
    texto_limpo = " ".join(" ".join(linhas_filtradas).split())
    return texto_limpo[:2500]


def limpar_labels_questoes(texto):
    """
    Remove labels redundantes de tipo geradas pela IA como '[Objetiva]', '[Discursiva]'.
    """
    if not texto:
        return texto
    texto = re.sub(
        r'(?i)(Quest[aã]o\s+\d+\s*:\s*)\[(?:Objetiva|Discursiva|Dissertativa|Mista)\]\s*',
        r'\1',
        texto
    )
    texto = re.sub(
        r'(?i)(Quest[aã]o\s+\d+\s*:\s*)\((?:Objetiva|Discursiva|Dissertativa|Mista)\)\s*',
        r'\1',
        texto
    )
    return texto


def preprocessar_texto(texto):
    if not texto:
        return ""
    texto = texto.replace("\n", " ")
    texto = " ".join(texto.split())
    return texto[:2500]


def gerar_hash(texto_pdf, dificuldade, tipo, quantidade):
    base = f"{preprocessar_texto(texto_pdf)}-{dificuldade}-{tipo}-{quantidade}"
    return hashlib.md5(base.encode()).hexdigest()


def gerar_atividade_fallback(texto_base, dificuldade, tipo, quantidade, segmento=""):
    """
    Gera uma atividade formatada e profissional imediatamente caso a IA
    esteja temporariamente indisponível.
    """
    topicos = extrair_topicos_relevantes(texto_base)
    seg_str = str(segmento or "").lower()
    eh_pei = any(k in seg_str for k in ["pei", "individualizado", "especial", "inclusiv"]) or "plano educacional individualizado" in str(texto_base).lower() or "pei" in str(texto_base).lower()

    questoes = []
    for i in range(1, quantidade + 1):
        topico = topicos[(i - 1) % len(topicos)]
        topico_fmt = topico.strip().rstrip(".:;")
        if len(topico_fmt) > 60:
            topico_exibicao = topico_fmt[:57] + "..."
        else:
            topico_exibicao = topico_fmt

        if eh_pei:
            if tipo == "objetiva":
                questoes.append(
                    f"Questão {i} [Apoio Visual: Cartão ilustrado sobre {topico_exibicao}]:\n"
                    f"Com a ajuda do professor e observando a figura, escolha a opção correta sobre {topico_exibicao}:\n"
                    f"A) É um tema importante que estamos aprendendo na aula de hoje.\n"
                    f"B) Não tem relação com o que estamos estudando.\n"
                    f"C) Não devemos prestar atenção neste assunto.\n"
                    f"Resposta correta: A\n"
                    f"Reforço Positivo e Mediação: Parabéns pelo empenho! Use a prancha visual para reforçar o acerto."
                )
            elif tipo == "discursiva":
                questoes.append(
                    f"Questão {i}: Conte ou aponte na prancha visual o que você mais gostou de aprender sobre {topico_exibicao}."
                )
            elif tipo == "pratica":
                questoes.append(
                    f"Questão {i} (Atividade Prática Multissensorial Inclusiva): Explorando {topico_exibicao}\n"
                    f"- Objetivo: Vivenciar o conceito de {topico_exibicao} com apoio de materiais táteis e visuais.\n"
                    f"- Passo 1: Observar e manipular os cartões ilustrados com a rotina da atividade.\n"
                    f"- Passo 2: Participar da dinâmica prática com mediação positiva do professor.\n"
                    f"- Recursos Assistivos: Prancha de rotina visual e material concreto adaptado."
                )
            else:
                if i % 2 == 1:
                    questoes.append(
                        f"Questão {i} [Apoio Visual: Pictograma de {topico_exibicao}]:\n"
                        f"Observe a imagem com atenção e selecione a resposta correta:\n"
                        f"A) Representa o assunto {topico_exibicao} que estudamos.\n"
                        f"B) É algo diferente e que não faz parte da nossa atividade.\n"
                        f"Resposta correta: A\n"
                        f"Reforço Positivo: Excelente resposta! Estimular o estudante com incentivo verbal."
                    )
                else:
                    questoes.append(
                        f"Questão {i} (Vivência Prática Inclusiva): Demonstre através de desenho, recorte ou apontamento como podemos aplicar {topico_exibicao}."
                    )
        else:
            if tipo == "objetiva":
                questoes.append(
                    f"Questão {i}: Com base no estudo sobre {topico_exibicao}, assinale a alternativa correta:\n"
                    f"A) Trata-se de um conceito fundamental aplicado nos procedimentos operacionais da área técnica.\n"
                    f"B) Refere-se a uma abordagem obsoleta desprovida de aplicação prática nas diretrizes vigentes.\n"
                    f"C) Não possui relação com os critérios de conformidade e qualidade recomendados.\n"
                    f"D) Representa uma técnica secundária cuja utilização é desaconselhada nos padrões atuais.\n"
                    f"Resposta correta: A"
                )
            elif tipo == "discursiva":
                questoes.append(
                    f"Questão {i}: Explique a importância e a aplicação prática de {topico_exibicao} no contexto profissional, destacando seus principais benefícios e requisitos técnicos."
                )
            elif tipo == "pratica":
                questoes.append(
                    f"Questão {i} (Atividade Prática Aplicada): Desafio Prático sobre {topico_exibicao}\n"
                    f"- Contexto: Aplicação profissional em ambiente técnico/industrial simulando demanda real do setor.\n"
                    f"- Roteiro de Execução: 1) Planejamento e separação dos insumos/ferramentas; 2) Execução técnica do procedimento conforme normas regulamentadoras; 3) Testes funcionais e inspeção de qualidade.\n"
                    f"- Recursos e Segurança: Utilização de bancada/oficina e uso obrigatório de EPIs adequados.\n"
                    f"- Critérios de Avaliação: Conformidade do produto/processo final, cumprimento do prazo e boas práticas de segurança."
                )
            else:
                # Formato Misto (Objetiva + Discursiva + Prática)
                if i % 3 == 1:
                    questoes.append(
                        f"Questão {i}: Com base no estudo sobre {topico_exibicao}, assinale a alternativa correta:\n"
                        f"A) Trata-se de um conceito fundamental aplicado nos procedimentos operacionais da área técnica.\n"
                        f"B) Refere-se a uma abordagem obsoleta desprovida de aplicação prática nas diretrizes vigentes.\n"
                        f"C) Não possui relação com os critérios de conformidade e qualidade recomendados.\n"
                        f"D) Representa uma técnica secundária cuja utilização é desaconselhada nos padrões atuais.\n"
                        f"Resposta correta: A"
                    )
                elif i % 3 == 2:
                    questoes.append(
                        f"Questão {i}: Explique a importância e a aplicação prática de {topico_exibicao} no contexto profissional, destacando seus principais benefícios e requisitos técnicos."
                    )
                else:
                    questoes.append(
                        f"Questão {i} (Atividade Prática): Desafio Prático Aplicado sobre {topico_exibicao}.\n"
                        f"- Contexto: Simulação de bancada, laboratório ou oficina técnica especializada.\n"
                        f"- Roteiro de Execução: Executar a montagem, configuração ou procedimento operacional seguindo as normas.\n"
                        f"- Critérios de Avaliação: Funcionamento correto, organização, cumprimento das normas técnicas e segurança."
                    )

    return "\n\n".join(questoes)


def gerar_atividade(texto_pdf, dificuldade, tipo="objetiva", quantidade=5, segmento=""):
    quantidade = int(quantidade)
    quantidade = max(1, min(quantidade, 30))

    cfg_geral = obter_config_ia()
    provider_ativo = cfg_geral["provider"]
    modelo_ativo = cfg_geral["gemini"]["model"] if provider_ativo == "gemini" else cfg_geral["ollama"]["model"]

    seg_str = str(segmento or "").lower()
    print(f"[ATIVIDADE] Provedor={provider_ativo} | Modelo={modelo_ativo} | Segmento={seg_str or 'Geral'} | Tipo={tipo} | Dificuldade={dificuldade} | Qtd={quantidade}")

    key = gerar_hash(f"{texto_pdf}-{seg_str}", dificuldade, tipo, quantidade)

    # 1. Consulta ao Cache Thread-safe
    with _cache_lock:
        if key in _cache_atividades:
            print("[CACHE] Hit atividade")
            return _cache_atividades[key]

    # Ajuste de contexto pedagógico (PEI vs BNCC vs Técnico)
    eh_pei = any(k in seg_str for k in ["pei", "individualizado", "especial", "inclusiv"]) or "plano educacional individualizado" in texto_pdf.lower() or "pei" in texto_pdf.lower()
    eh_basica = any(k in seg_str for k in ["fundamental", "medio", "médio", "infantil"])

    # 2. Instruções direcionadas e objetivas
    if tipo == "objetiva":
        if eh_pei:
            instrucao_tipo = (
                f"Crie EXATAMENTE {quantidade} questões OBJETIVAS ADAPTADAS PARA EDUCAÇÃO ESPECIAL INCLUSIVA (PEI / DUA). "
                "DIRETRIZES OBRIGATÓRIAS DE ACESSIBILIDADE E DUA: "
                "(1) Linguagem simples, direta e concisa (Plain Language), sem frases longas ou metáforas complexas; "
                "(2) Cada questão DEVE ter EXATAMENTE 3 alternativas claras (A, B, C), eliminando sobrecarga cognitiva de distratores; "
                "(3) Inclua indicação de apoio visual/pictograma entre colchetes como suporte (ex: [Imagem: prancha de comunicação ou ilustração temática]); "
                "(4) Zero pegadinhas ou termos ambíguos; "
                "(5) Após 'Resposta correta: X', inclua 'Reforço Positivo e Mediação: [orientação acolhedora para o educador]'."
            )
        elif eh_basica:
            instrucao_tipo = (
                f"Crie EXATAMENTE {quantidade} questões OBJETIVAS de múltipla escolha alinhadas às diretrizes da BNCC de nível {dificuldade}. "
                "Cada questão DEVE ter EXATAMENTE 4 ou 5 alternativas (A, B, C, D, E) com vocabulário adequado à faixa etária da série informada. "
                "(1) Enunciados claros, contextualizados e formativos; (2) Sem pegadinhas; (3) Alternativas plausíveis; "
                "(4) Após indicar 'Resposta correta: X', inclua obrigatoriamente 'Justificativa: [explicação didática clara]'."
            )
        else:
            instrucao_tipo = (
                f"Crie EXATAMENTE {quantidade} questões OBJETIVAS de múltipla escolha no PADRÃO SAEP / Técnico de nível {dificuldade}. "
                "Cada questão DEVE ter EXATAMENTE 5 alternativas (A, B, C, D, E). "
                "REGRAS OBRIGATÓRIAS: (1) Nunca use pegadinhas; (2) Nunca induza o aluno ao erro; "
                "(3) As alternativas incorretas devem ser plausíveis mas claramente distinguíveis; "
                "(4) Após indicar 'Resposta correta: X', inclua obrigatoriamente 'Justificativa: [explicação didática clara]'."
            )
        formato = (
            "Questão 1: [Enunciado contextualizado e claro]\n"
            "A) ...\nB) ...\nC) ...\nD) ...\nE) ...\n"
            "Resposta correta: A\n"
            "Justificativa: [Explicação didática da resposta correta]"
        )
        tokens_por_questao = 350
    elif tipo == "discursiva":
        if eh_pei:
            instrucao_tipo = (
                f"Crie EXATAMENTE {quantidade} questões DISCURSIVAS ADAPTADAS PARA O PÚBLICO PEI / EDUCAÇÃO INCLUSIVA. "
                "Enunciados curtos, diretos e significativos para a vida prática do estudante, estimulando expressão verbal, desenho ou relato de experiência."
            )
            formato = "Questão 1: [Pergunta direta e acessível com apoio descritivo]\n\nQuestão 2: [Pergunta com foco prático]"
        else:
            instrucao_tipo = (
                f"Crie EXATAMENTE {quantidade} questões DISCURSIVAS formativas e analíticas de nível {dificuldade} adequadas ao nível dos estudantes."
            )
            formato = "Questão 1: [Enunciado discursivo / reflexivo]\n\nQuestão 2: [Enunciado]"
        tokens_por_questao = 200
    elif tipo == "pratica":
        if eh_pei:
            instrucao_tipo = (
                f"Crie EXATAMENTE {quantidade} ATIVIDADES PRÁTICAS MULTISSENSORIAIS ADAPTADAS (PEI / DUA). "
                "Cada atividade deve priorizar o Desenho Universal para a Aprendizagem (DUA), utilizando materiais manipuláveis, rotina visual TEACCH, "
                "passo a passo simplificado e sugestões de Comunicação Aumentativa e Alternativa (CAA)."
            )
            formato = (
                "Questão 1: [Atividade Multissensorial Inclusiva: Título]\n"
                "Objetivo Adaptado: [Meta de aprendizagem clara e funcional]\n"
                "Passo a Passo com Apoio Visual: [Etapas estruturadas em micro-passos]\n"
                "Tecnologia Assistiva e Recursos: [Pranchas CAA, materiais táteis, recursos digitais]\n"
                "Mediação Pedagógica: [Orientação acolhedora para o professor conduzir a atividade]"
            )
        elif eh_basica:
            instrucao_tipo = (
                f"Crie EXATAMENTE {quantidade} ATIVIDADES PRÁTICAS / DESAFIOS EM SALA de nível {dificuldade} (experimentos simples, dinâmicas ativas, exercícios de aplicação prática no cotidiano). "
                "Cada atividade deve conter: Situação-Problema, Roteiro de Execução Passo a Passo e Critérios de Avaliação Formativa."
            )
            formato = (
                "Questão 1: [Desafio Prático Aplicado: Título do Procedimento]\n"
                "Contexto: [Situação real da prática ou cotidiano]\n"
                "Roteiro de Execução: [Passo a passo com etapas a executar]\n"
                "Recursos Necessários: [Materiais e ferramentas recomendadas]\n"
                "Critérios de Avaliação: [Checklist do que será avaliado no resultado]"
            )
        else:
            instrucao_tipo = (
                f"Crie EXATAMENTE {quantidade} ATIVIDADES PRÁTICAS / DESAFIOS TÉCNICOS APLICADOS de nível {dificuldade} voltados para laboratório, oficina ou estudo de caso de simulação profissional. "
                "Cada atividade DEVE conter: Contextualização do Problema, Roteiro de Execução Passo a Passo, Recursos/Ferramentas necessárias e Critérios de Avaliação."
            )
            formato = (
                "Questão 1: [Desafio Prático Aplicado: Título do Procedimento]\n"
                "Contexto: [Situação real da prática ou cotidiano]\n"
                "Roteiro de Execução: [Passo a passo com etapas a executar]\n"
                "Recursos Necessários: [Materiais e ferramentas recomendadas]\n"
                "Critérios de Avaliação: [Checklist do que será avaliado no resultado]"
            )
        tokens_por_questao = 320
    else:
        # Formato Misto (combina questões Objetivas, Discursivas e Práticas)
        qtd_pratica = max(1, quantidade // 3) if quantidade >= 3 else (1 if quantidade >= 2 else 0)
        qtd_restante = quantidade - qtd_pratica
        qtd_obj = qtd_restante // 2
        qtd_disc = qtd_restante - qtd_obj
        if qtd_obj == 0 and qtd_restante > 0:
            qtd_obj = 1
            qtd_disc = max(0, qtd_restante - 1)

        partes_desc = []
        if eh_pei:
            if qtd_obj > 0:
                partes_desc.append(f"{qtd_obj} OBJETIVAS ADAPTADAS (máximo 3 alternativas A, B, C, linguagem clara e apoio visual)")
            if qtd_disc > 0:
                partes_desc.append(f"{qtd_disc} DISCURSIVAS ACESSÍVEIS (perguntas diretas e funcionais)")
            if qtd_pratica > 0:
                partes_desc.append(f"{qtd_pratica} ATIVIDADE(S) PRÁTICA(S) MULTISSENSORIAL(IS) DUA/TEACCH")
            instrucao_tipo = (
                f"Crie EXATAMENTE {quantidade} questões ADAPTADAS PARA EDUCAÇÃO ESPECIAL (PEI) combinando teoria acessível e prática multissensorial: "
                + ", ".join(partes_desc) + "."
            )
            formato = (
                "Questão 1: [Objetiva acessível com 3 alternativas A, B, C e indicação de apoio visual]\n"
                "A) ...\nB) ...\nC) ...\n"
                "Resposta correta: X\nReforço Positivo e Mediação: [Orientação acolhedora para o educador]\n\n"
                "Questão 2: [Pergunta discursiva direta e acessível]\n\n"
                "Questão 3: [Atividade Multissensorial Inclusiva com DUA, passo a passo e recursos CAA]"
            )
        else:
            if qtd_obj > 0:
                partes_desc.append(f"{qtd_obj} OBJETIVAS (múltipla escolha padrão com A, B, C, D, E — sem pegadinhas, com Resposta correta e Justificativa)")
            if qtd_disc > 0:
                partes_desc.append(f"{qtd_disc} DISCURSIVAS (dissertativas formativas)")
            if qtd_pratica > 0:
                partes_desc.append(f"{qtd_pratica} ATIVIDADE(S) PRÁTICA(S) (desafio aplicado com contexto, passo a passo e critérios de avaliação)")

            instrucao_tipo = (
                f"Crie EXATAMENTE {quantidade} questões de nível {dificuldade} combinando teoria e prática: "
                + ", ".join(partes_desc) + "."
            )
            formato = (
                "Questão 1: [Objetiva com alternativas A, B, C, D, E — sem pegadinhas]\n"
                "A) ...\nB) ...\nC) ...\nD) ...\nE) ...\n"
                "Resposta correta: X\nJustificativa: [Explicação didática da resposta]\n\n"
                "Questão 2: [Discursiva dissertativa]\n\n"
                "Questão 3: [Atividade Prática Aplicada com Contexto, Roteiro e Critérios de Avaliação]"
            )
        tokens_por_questao = 300

    num_predict = min(8192, max(1800, quantidade * tokens_por_questao))
    conteudo_pedagogico = extrair_conteudo_pedagogico(texto_pdf)

    if eh_pei:
        system_prompt = "Você é um especialista mundial em Educação Especial Inclusiva, DUA (Universal Design for Learning) e Tecnologias Assistivas. Crie atividades pedagógicas adaptadas que eliminem barreiras de aprendizagem, com linguagem simples e clara, apoio visual e avaliação formativa positiva."
    else:
        system_prompt = "Você é um professor especialista e coordenador pedagógico responsável por criar atividades avaliativas rigorosas e de alta qualidade técnica de acordo com as diretrizes educacionais."
    prompt = f"""Crie a lista de questões avaliativas abaixo baseando-se estritamente nestes conteúdos técnicos e objetivos pedagógicos:
---
{conteudo_pedagogico}
---

{instrucao_tipo}

REGRAS OBRIGATÓRIAS (PADRÃO SAEP):
1. Comece DIRETAMENTE com "Questão 1:" sem introdução, cumprimentos ou saudações.
2. Numere de 1 até {quantidade}: "Questão 1:", "Questão 2:", etc.
3. Não use formatação markdown pesada ou asteriscos desnecessários (evite **).
4. As questões devem focar EXCLUSIVAMENTE nos conceitos técnicos da disciplina. NUNCA faça perguntas sobre cabeçalhos administrativos, identificação de curso, carga horária ou recursos didáticos.
5. Para questões objetivas (padrão SAEP): use EXATAMENTE 5 alternativas (A, B, C, D, E). NUNCA use pegadinhas. NUNCA induza o aluno ao erro. Todas as alternativas incorretas devem ser plausíveis. Após a resposta correta, inclua sempre a Justificativa explicando o porquê.
6. Siga rigorosamente o formato:
{formato}

Gere as {quantidade} questões agora:"""

    try:
        resposta_ia = chamar_ia(
            prompt=prompt,
            system_prompt=system_prompt,
            max_tokens=num_predict,
            temperature=0.2
        )

        if resposta_ia and len(resposta_ia.strip()) >= 30:
            atividade = limpar_labels_questoes(resposta_ia.strip())
            with _cache_lock:
                _cache_atividades[key] = atividade
            print(f"[OK] Atividade gerada via IA ({len(atividade)} chars)")
            return atividade
    except Exception as e:
        print(f"[AVISO] Exceção ao chamar IA: {e}")

    # Fallback instantâneo
    print("[AVISO] IA indisponível ou resposta incompleta. Retornando atividade estruturada de apoio...")
    fallback = gerar_atividade_fallback(texto_pdf, dificuldade, tipo, quantidade, segmento=segmento)
    with _cache_lock:
        _cache_atividades[key] = fallback
    return fallback
