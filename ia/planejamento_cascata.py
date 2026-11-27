"""
Módulo de Planejamento Pedagógico em Cascata e Módulo PEI (Educação Especial Inclusiva)
Universal para: Educação Infantil, Ensino Fundamental, Ensino Médio e PEI
Hierarquia: Anual -> Semestral -> Mensal -> Planos de Aula do Mês
Tecnologias Inclusivas Mundiais: DUA (Universal Design for Learning), CAA (Comunicação Aumentativa e Alternativa),
TEACCH, Apoios Visuais, Linguagem Clara (Plain Language) e Tecnologias Assistivas Globais.
"""

import json
import logging
import re
from ia.ia_client import chamar_ia

logger = logging.getLogger("planejamento_cascata")

MESES_LETIVOS_SEMESTRE_1 = [
    {"numero": 2, "nome": "Fevereiro", "semanas": 3, "dias_uteis_aprox": 15},
    {"numero": 3, "nome": "Março", "semanas": 4, "dias_uteis_aprox": 21},
    {"numero": 4, "nome": "Abril", "semanas": 4, "dias_uteis_aprox": 20},
    {"numero": 5, "nome": "Maio", "semanas": 4, "dias_uteis_aprox": 22},
    {"numero": 6, "nome": "Junho", "semanas": 4, "dias_uteis_aprox": 20},
]

MESES_LETIVOS_SEMESTRE_2 = [
    {"numero": 8, "nome": "Agosto", "semanas": 4, "dias_uteis_aprox": 22},
    {"numero": 9, "nome": "Setembro", "semanas": 4, "dias_uteis_aprox": 21},
    {"numero": 10, "nome": "Outubro", "semanas": 4, "dias_uteis_aprox": 21},
    {"numero": 11, "nome": "Novembro", "semanas": 4, "dias_uteis_aprox": 20},
    {"numero": 12, "nome": "Dezembro", "semanas": 3, "dias_uteis_aprox": 14},
]

MESES_LETIVOS_BIMESTRE_1 = [
    {"numero": 2, "nome": "Fevereiro", "semanas": 3, "dias_uteis_aprox": 15},
    {"numero": 3, "nome": "Março", "semanas": 4, "dias_uteis_aprox": 21},
    {"numero": 4, "nome": "Abril", "semanas": 4, "dias_uteis_aprox": 20},
]

MESES_LETIVOS_BIMESTRE_2 = [
    {"numero": 5, "nome": "Maio", "semanas": 4, "dias_uteis_aprox": 22},
    {"numero": 6, "nome": "Junho", "semanas": 4, "dias_uteis_aprox": 20},
]

MESES_LETIVOS_BIMESTRE_3 = [
    {"numero": 8, "nome": "Agosto", "semanas": 4, "dias_uteis_aprox": 22},
    {"numero": 9, "nome": "Setembro", "semanas": 4, "dias_uteis_aprox": 21},
]

MESES_LETIVOS_BIMESTRE_4 = [
    {"numero": 10, "nome": "Outubro", "semanas": 4, "dias_uteis_aprox": 21},
    {"numero": 11, "nome": "Novembro", "semanas": 4, "dias_uteis_aprox": 20},
    {"numero": 12, "nome": "Dezembro", "semanas": 3, "dias_uteis_aprox": 14},
]

MESES_LETIVOS_POR_BIMESTRE = {
    1: MESES_LETIVOS_BIMESTRE_1,
    2: MESES_LETIVOS_BIMESTRE_2,
    3: MESES_LETIVOS_BIMESTRE_3,
    4: MESES_LETIVOS_BIMESTRE_4,
}

TODOS_MESES_LETIVOS = MESES_LETIVOS_SEMESTRE_1 + MESES_LETIVOS_SEMESTRE_2


def obter_meses_por_semestre(semestre: int):
    return MESES_LETIVOS_SEMESTRE_1 if semestre == 1 else MESES_LETIVOS_SEMESTRE_2


def obter_meses_por_bimestre(bimestre: int):
    return MESES_LETIVOS_POR_BIMESTRE.get(bimestre, MESES_LETIVOS_BIMESTRE_1)


def eh_segmento_basica_ou_pei(segmento: str) -> bool:
    seg = (segmento or "").lower()
    return any(k in seg for k in ["infantil", "fundamental", "medio", "médio", "pei", "especial", "individualizado"])


def limpar_simbolos_ia(texto: str) -> str:
    """
    Remove marcações Markdown e símbolos decorativos gerados pela IA
    (**, ##, ###, ---, ___, ***, backticks, etc.), mantendo o texto limpo.
    """
    if not texto:
        return ""
    linhas_limpas = []
    for linha in texto.splitlines():
        s = linha.strip()
        # Ignora linhas puramente separadoras (---, ===, ***, ___)
        if re.fullmatch(r"[-=*_]{3,}", s):
            continue
        # Remove prefixos de título Markdown (#, ##, ###) e blockquote (>)
        s = re.sub(r"^#{1,6}\s*", "", s)
        s = re.sub(r"^>\s*", "", s)
        # Converte marcador de lista "* " ou "• " no início da linha para "- "
        s = re.sub(r"^[*•]\s+", "- ", s)
        # Remove negrito/itálico/código Markdown (**, __, *, `)
        s = s.replace("**", "").replace("__", "").replace("`", "")
        s = re.sub(r"(?<!\w)\*|\*(?!\w)", "", s)
        linhas_limpas.append(s)
    resultado = "\n".join(linhas_limpas)
    resultado = re.sub(r"\n{3,}", "\n\n", resultado)
    return resultado.strip()


# ==============================================================================
# 1. GERAÇÃO DE PLANEJAMENTO ANUAL (EDUCAÇÃO BÁSICA)
# ==============================================================================

def gerar_planejamento_anual_ia(nome_curso: str, componente: str, segmento: str, ementa_digitada: str = "", ano_letivo: str = "2026", docente_responsavel: str = ""):
    """
    Gera o Planejamento Anual completo para Educação Infantil, Fundamental ou Médio (BNCC).
    Retorna texto formatado e estrutura de dados para o desdobramento em semestres.
    """
    linha_docente = f"\n- Docente Responsável: {docente_responsavel}" if docente_responsavel else ""
    instrucao_docente = (
        f'\nNa seção 1 (IDENTIFICAÇÃO E EMENTA ANUAL), informe exatamente "{docente_responsavel}" como Docente Responsável (não invente outro nome nem use placeholders).'
        if docente_responsavel
        else ""
    )

    prompt = f"""Você é um especialista em Gestão Pedagógica e Currículo alinhado à BNCC (Base Nacional Comum Curricular).
Gere um PLANEJAMENTO ANUAL completo e estruturado para:
- Ano/Série/Turma: {nome_curso}
- Componente Curricular: {componente}
- Segmento de Ensino: {segmento}
- Ano Letivo: {ano_letivo}{linha_docente}
- Tópicos/Ementa de Referência: {ementa_digitada or "Desenvolver os conteúdos e competências essenciais conforme a BNCC"}

O Planejamento Anual DEVE conter rigorosamente as seguintes seções:
1. IDENTIFICAÇÃO E EMENTA ANUAL
2. COMPETÊNCIAS GERAIS E ESPECÍFICAS DA BNCC TRABALHADAS NO ANO
3. EIXOS TEMÁTICOS E OBJETOS DE CONHECIMENTO DO 1º BIMESTRE (Fevereiro a Abril)
4. EIXOS TEMÁTICOS E OBJETOS DE CONHECIMENTO DO 2º BIMESTRE (Maio e Junho)
5. EIXOS TEMÁTICOS E OBJETOS DE CONHECIMENTO DO 3º BIMESTRE (Agosto e Setembro)
6. EIXOS TEMÁTICOS E OBJETOS DE CONHECIMENTO DO 4º BIMESTRE (Outubro a Dezembro)
7. METODOLOGIAS ATIVAS E RECURSOS DIDÁTICOS
8. DIRETRIZES DE AVALIAÇÃO FORMATIVA E DIAGNÓSTICA
{instrucao_docente}
Seja detalhado, didático, claro e perfeitamente aplicável ao contexto escolar brasileiro.
NÃO utilize marcações Markdown como **, ##, ###, ---, * ou blocos de código; escreva em texto limpo e organizado."""

    system_prompt = "Você é um consultor pedagógico sênior especialista em BNCC e planejamento escolar."
    texto_gerado = limpar_simbolos_ia(chamar_ia(prompt, system_prompt=system_prompt, max_tokens=2500))

    # Se a IA não responder adequadamente, utiliza fallback estruturado
    if not texto_gerado or len(texto_gerado.strip()) < 100:
        docente_txt = f"\nDocente Responsável: {docente_responsavel}" if docente_responsavel else ""
        texto_gerado = f"""PLANEJAMENTO ANUAL — {ano_letivo}
Componente Curricular: {componente} | Série/Ano: {nome_curso} | Segmento: {segmento}{docente_txt}

1. IDENTIFICAÇÃO E EMENTA ANUAL:
Desenvolvimento integral das competências comunicativas, lógicas, socioemocionais e investigativas em {componente}, com base nas diretrizes da BNCC.

2. COMPETÊNCIAS DA BNCC TRABALHADAS:
- Compreensão crítica e aplicação prática dos conceitos fundamentais de {componente}.
- Utilização de linguagens diversas para expressar ideias e solucionar problemas cotidianos.
- Trabalho colaborativo, pensamento ético e empatia na convivência escolar.

3. ESTRUTURAÇÃO DO 1º BIMESTRE (Fevereiro a Abril):
- Unidade 1: Fundamentos conceituais, nivelamento diagnóstico e introdução aos temas centrais.

4. ESTRUTURAÇÃO DO 2º BIMESTRE (Maio e Junho):
- Unidade 2: Aplicação prática, investigações dirigidas e consolidação das primeiras habilidades essenciais.

5. ESTRUTURAÇÃO DO 3º BIMESTRE (Agosto e Setembro):
- Unidade 3: Aprofundamento conceitual, projetos interdisciplinares e análise crítica.

6. ESTRUTURAÇÃO DO 4º BIMESTRE (Outubro a Dezembro):
- Unidade 4: Síntese dos conhecimentos do ano, autoavaliação e projetos finais.

7. METODOLOGIAS E RECURSOS:
- Metodologias ativas: aprendizagem baseada em problemas, pesquisas orientadas e dinâmicas colaborativas.
- Recursos: livro didático, quadro branco, experimentos simples e projetor multimídia.

8. AVALIAÇÃO FORMATIVA:
- Diagnóstica contínua, participação, trabalhos individuais e em grupos, com foco no avanço do estudante."""

    dados_estruturados = {
        "tipo": "anual",
        "nome_curso": nome_curso,
        "componente": componente,
        "segmento": segmento,
        "ano_letivo": ano_letivo,
        "docente_responsavel": docente_responsavel,
        "bimestres_previstos": [1, 2, 3, 4],
        "resumo_bimestre_1": f"Fundamentos e nivelamento em {componente} (Fev a Abr)",
        "resumo_bimestre_2": f"Consolidação e prática em {componente} (Mai e Jun)",
        "resumo_bimestre_3": f"Aprofundamento e projetos em {componente} (Ago e Set)",
        "resumo_bimestre_4": f"Síntese e avaliação final em {componente} (Out a Dez)",
    }

    return texto_gerado, dados_estruturados


# ==============================================================================
# 2. GERAÇÃO DE PLANEJAMENTO BIMESTRAL (DESDOBRAMENTO DO ANUAL EM 4 BIMESTRES)
# ==============================================================================

def gerar_planejamento_bimestral_ia(planejamento_anual_texto: str, componente: str, nome_curso: str, bimestre: int = 1):
    """
    Desdobra o Planejamento Anual no Planejamento do Bimestre selecionado (1º, 2º, 3º ou 4º Bimestre).
    """
    meses_info = obter_meses_por_bimestre(bimestre)
    nomes_meses_str = ", ".join([m["nome"] for m in meses_info])

    prompt = f"""Você é um coordenador pedagógico especialista em desdobramento curricular e BNCC.
Com base no PLANEJAMENTO ANUAL abaixo, elabore o PLANEJAMENTO DO {bimestre}º BIMESTRE para:
- Ano/Série: {nome_curso}
- Componente Curricular: {componente}
- Bimestre: {bimestre}º Bimestre
- Meses Letivos abrangidos: {nomes_meses_str}

PLANEJAMENTO ANUAL DE REFERÊNCIA:
{planejamento_anual_texto[:3000]}

O Planejamento do {bimestre}º Bimestre DEVE conter:
1. OBJETIVO GERAL DO {bimestre}º BIMESTRE
2. HABILIDADES ESPECÍFICAS DA BNCC TRABALHADAS NESTE BIMESTRE (com códigos de habilidades, ex: EF..., EM... ou objetivos da Educação Infantil)
3. DISTRIBUIÇÃO DOS CONTEÚDOS POR MÊS LETIVO ({nomes_meses_str})
4. ESTRATÉGIAS DIDÁTICAS E METODOLOGIAS ATIVAS DO BIMESTRE
5. INSTRUMENTOS E CRITÉRIOS DE AVALIAÇÃO BIMESTRAL (Autoavaliação, rubricas e avaliações diagnósticas/formativas)

NÃO utilize marcações Markdown como **, ##, ###, ---, * ou blocos de código; escreva em texto limpo."""

    system_prompt = "Você é um gestor escolar especialista em BNCC e planejamento bimestral."
    texto_gerado = limpar_simbolos_ia(chamar_ia(prompt, system_prompt=system_prompt, max_tokens=2200))

    if not texto_gerado or len(texto_gerado.strip()) < 100:
        texto_gerado = f"""PLANEJAMENTO BIMESTRAL — {bimestre}º BIMESTRE
Componente Curricular: {componente} | Série: {nome_curso}
Meses contemplados: {nomes_meses_str}

1. OBJETIVO GERAL DO {bimestre}º BIMESTRE:
Promover o domínio teórico e prático das habilidades essenciais de {componente} previstas para esta etapa do ano letivo.

2. HABILIDADES DA BNCC NESTE BIMESTRE:
- Habilidade 1: Identificar e articular conceitos-chave em situações concretas.
- Habilidade 2: Interpretar textos, tabelas e problemas contextualizados.
- Habilidade 3: Cooperar em produções individuais e coletivas com autonomia e pensamento crítico.

3. DISTRIBUIÇÃO MENSAL:
""" + "\n".join([f"- Mês de {m['nome']}: Metas e conteúdos específicos do período (aprox. {m['semanas']} semanas letivas)." for m in meses_info]) + f"""

4. METODOLOGIAS:
Aulas dialogadas, dinâmicas em grupo, estudos de caso simples e produções textuais/gráficas.

5. AVALIAÇÃO:
Contínua, formativa e cumulativa, valorizando o processo de aprendizagem e a resolução de desafios."""

    dados_estruturados = {
        "tipo": "bimestral",
        "bimestre": bimestre,
        "meses": [m["nome"] for m in meses_info],
        "componente": componente,
        "nome_curso": nome_curso
    }

    return texto_gerado, dados_estruturados


# ==============================================================================
# 2. GERAÇÃO DE PLANEJAMENTO SEMESTRAL (DESDOBRAMENTO DO ANUAL)
# ==============================================================================

def gerar_planejamento_semestral_ia(planejamento_anual_texto: str, componente: str, nome_curso: str, semestre: int = 1):
    """
    Desdobra o Planejamento Anual no Planejamento do Semestre selecionado (1º ou 2º Semestre).
    """
    meses_info = obter_meses_por_semestre(semestre)
    nomes_meses_str = ", ".join([m["nome"] for m in meses_info])

    prompt = f"""Você é um coordenador pedagógico especialista em desdobramento curricular e BNCC.
Com base no PLANEJAMENTO ANUAL abaixo, elabore o PLANEJAMENTO DO {semestre}º SEMESTRE para:
- Ano/Série: {nome_curso}
- Componente Curricular: {componente}
- Semestre: {semestre}º Semestre
- Meses Letivos abrangidos: {nomes_meses_str}

PLANEJAMENTO ANUAL DE REFERÊNCIA:
{planejamento_anual_texto[:3000]}

O Planejamento do {semestre}º Semestre DEVE conter:
1. OBJETIVO GERAL DO {semestre}º SEMESTRE
2. HABILIDADES ESPECÍFICAS DA BNCC TRABALHADAS NESTE SEMESTRE (com códigos de habilidades, ex: EF..., EM... ou objetivos da Educação Infantil)
3. DISTRIBUIÇÃO DOS CONTEÚDOS POR MÊS LETIVO ({nomes_meses_str})
4. ESTRATÉGIAS DIDÁTICAS E METODOLOGIAS ATIVAS DO SEMESTRE
5. INSTRUMENTOS E CRITÉRIOS DE AVALIAÇÃO SEMESTRAL (Autoavaliação, rubricas e provas diagnósticas)

NÃO utilize marcações Markdown como **, ##, ###, ---, * ou blocos de código; escreva em texto limpo."""

    system_prompt = "Você é um gestor escolar especialista em BNCC e planejamento semestral."
    texto_gerado = limpar_simbolos_ia(chamar_ia(prompt, system_prompt=system_prompt, max_tokens=2200))

    if not texto_gerado or len(texto_gerado.strip()) < 100:
        texto_gerado = f"""PLANEJAMENTO SEMESTRAL — {semestre}º SEMESTRE
Componente Curricular: {componente} | Série: {nome_curso}
Meses contemplados: {nomes_meses_str}

1. OBJETIVO GERAL DO {semestre}º SEMESTRE:
Promover o domínio teórico e prático das habilidades essenciais de {componente} previstas para esta etapa do ano letivo.

2. HABILIDADES DA BNCC NESTE SEMESTRE:
- Habilidade 1: Identificar e articular conceitos-chave em situações concretas.
- Habilidade 2: Interpretar textos, tabelas e problemas contextualizados.
- Habilidade 3: Cooperar em produções individuais e coletivas com autonomia e pensamento crítico.

3. DISTRIBUIÇÃO MENSAL:
""" + "\n".join([f"- Mês de {m['nome']}: Metas e conteúdos específicos do período (aprox. {m['semanas']} semanas letivas)." for m in meses_info]) + f"""

4. METODOLOGIAS:
Aulas dialogadas, dinâmicas em grupo, estudos de caso simples e produções textuais/gráficas.

5. AVALIAÇÃO:
Contínua, formativa e cumulativa, valorizando o processo de aprendizagem e a resolução de desafios."""

    dados_estruturados = {
        "tipo": "semestral",
        "semestre": semestre,
        "meses": [m["nome"] for m in meses_info],
        "componente": componente,
        "nome_curso": nome_curso
    }

    return texto_gerado, dados_estruturados


# ==============================================================================
# 3. GERAÇÃO DE PLANEJAMENTO MENSAL (DESDOBRAMENTO DO SEMESTRAL)
# ==============================================================================

def gerar_planejamento_mensal_ia(planejamento_semestral_texto: str, componente: str, nome_curso: str, mes_nome: str, numero_mes: int):
    """
    Gera o Planejamento Mensal detalhado do mês letivo selecionado.
    """
    prompt = f"""Você é um professor e planejador pedagógico da Educação Básica.
Elabore o PLANEJAMENTO MENSAL do mês de {mes_nome.upper()} para:
- Disciplina: {componente}
- Ano/Série: {nome_curso}
- Mês Letivo: {mes_nome} (Mês {numero_mes})

Considere as diretrizes do Planejamento Semestral de origem:
{planejamento_semestral_texto[:2500]}

O Planejamento Mensal de {mes_nome} deve conter:
1. OBJETIVO DE APRENDIZAGEM DO MÊS DE {mes_nome.upper()}
2. DISTRIBUIÇÃO SEMANAL DOS CONTEÚDOS (Semana 1, Semana 2, Semana 3, Semana 4)
3. HABILIDADES DA BNCC PRIORIZADAS NO MÊS
4. RECURSOS DIDÁTICOS E ESPAÇOS UTILIZADOS
5. ATIVIDADE AVALIATIVA / SÍNTESE DO MÊS

NÃO utilize marcações Markdown como **, ##, ###, ---, * ou blocos de código; escreva em texto limpo."""

    system_prompt = "Você é um especialista em planejamento pedagógico mensal e BNCC."
    texto_gerado = limpar_simbolos_ia(chamar_ia(prompt, system_prompt=system_prompt, max_tokens=2000))

    if not texto_gerado or len(texto_gerado.strip()) < 80:
        texto_gerado = f"""PLANEJAMENTO MENSAL — {mes_nome.upper()}
Componente: {componente} | Série: {nome_curso}

1. OBJETIVOS DE APRENDIZAGEM DE {mes_nome.upper()}:
Consolidar as habilidades fundamentais e aprofundar os temas programados para este mês letivo.

2. DISTRIBUIÇÃO SEMANAL DOS CONTEÚDOS:
- Semana 1: Diagnóstico prévio, acolhimento e introdução dos conceitos do mês.
- Semana 2: Desenvolvimento conceitual e exercícios orientados em sala.
- Semana 3: Atividade prática colaborativa e problematização de situações reais.
- Semana 4: Revisão, consolidação das aprendizagens e avaliação formativa do mês.

3. RECURSOS DIDÁTICOS:
Material impresso, livro didático, quadro e recursos audiovisuais.

4. AVALIAÇÃO:
Acompanhamento diário da participação e entrega da atividade síntese ao final do mês."""

    dados_estruturados = {
        "tipo": "mensal",
        "mes_nome": mes_nome,
        "numero_mes": numero_mes,
        "componente": componente,
        "nome_curso": nome_curso
    }

    return texto_gerado, dados_estruturados


# ==============================================================================
# 4. GERAÇÃO DE PLANOS DE AULA DO MÊS (DESDOBRAMENTO OPERACIONAL DO MÊS)
# ==============================================================================

def calcular_aulas_do_mes(dias_semana: str, aulas_por_dia: int, semanas_mes: int = 4):
    """
    Calcula a quantidade de encontros e total de aulas no mês.
    Ex: dias_semana = 'Segunda, Quarta' -> 2 dias/semana -> em 4 semanas = 8 encontros.
    Se aulas_por_dia = 2 -> total = 16 aulas.
    """
    dias_list = [d.strip() for d in dias_semana.split(",") if d.strip()]
    dias_qtd = len(dias_list) if dias_list else 2
    total_encontros = dias_qtd * semanas_mes
    total_aulas = total_encontros * max(1, aulas_por_dia)
    return total_encontros, total_aulas


def _extrair_encontros_json(texto: str, encontros_a_gerar: int):
    """Extrai lista de encontros em JSON da resposta da IA, com fallback seguro."""
    if not texto:
        return []
    limpo = texto.strip()
    limpo = re.sub(r"^```(?:json)?\s*", "", limpo, flags=re.IGNORECASE)
    limpo = re.sub(r"\s*```$", "", limpo)
    inicio = limpo.find("[")
    fim = limpo.rfind("]")
    if inicio != -1 and fim != -1 and fim > inicio:
        try:
            dados = json.loads(limpo[inicio:fim + 1])
            if isinstance(dados, list):
                return [item for item in dados if isinstance(item, dict)][:encontros_a_gerar]
        except Exception:
            pass
    return []


def gerar_planos_aula_do_mes_ia(planejamento_mensal_texto: str, componente: str, nome_curso: str, mes_nome: str,
                                dias_semana: str = "Segunda, Quarta", aulas_por_dia: int = 2, duracao_aula: int = 50,
                                eh_pei: bool = False, dados_pei: dict = None):
    """
    Gera a lista completa de Planos de Aula para cada encontro/aula do mês selecionado,
    incluindo os conhecimentos específicos trabalhados em cada dia.
    """
    semanas = 4
    total_encontros, total_aulas = calcular_aulas_do_mes(dias_semana, aulas_por_dia, semanas_mes=semanas)
    encontros_a_gerar = max(1, min(total_encontros, 12))

    planos_gerados = []

    contexto_inclusivo = ""
    if eh_pei:
        aluno = (dados_pei or {}).get("nome", "Estudante")
        necessidades = (dados_pei or {}).get("necessidades", "Necessidades Educacionais Especiais")
        contexto_inclusivo = f"""
ATENÇÃO - MODALIDADE PEI (Plano Educacional Individualizado):
Estudante: {aluno} | Necessidades: {necessidades}
O plano de cada aula DEVE incorporar tecnologias assistivas mundiais:
- DUA (Universal Design for Learning): múltiplos meios de engajamento, representação e expressão.
- CAA (Comunicação Aumentativa e Alternativa): pranchas visuais, pictogramas ARASAAC.
- Metodologia TEACCH: previsibilidade, passos visuais e controle de estímulos sensoriais.
- Linguagem Clara (Plain Language) e avaliações processuais sem punição por tempo."""

    prompt = f"""Você é um especialista em planejamento didático diário alinhado à BNCC.
Gere o detalhamento de TODOS os {encontros_a_gerar} encontros do MÊS DE {mes_nome.upper()} ({total_aulas} aulas no total de {duracao_aula}min).
Disciplina: {componente} | Série: {nome_curso}
Dias de aula na semana: {dias_semana} | Aulas por dia: {aulas_por_dia}
{contexto_inclusivo}

PLANEJAMENTO MENSAL DE REFERÊNCIA:
{planejamento_mensal_texto[:2500]}

Responda EXCLUSIVAMENTE com um array JSON válido contendo exatamente {encontros_a_gerar} objetos (um para cada encontro, de 1 a {encontros_a_gerar}), cada um com as chaves:
- "dia": número do encontro (1 a {encontros_a_gerar})
- "tema": título curto e específico do tema da aula do dia
- "conhecimentos": lista de 2 a 4 objetos de conhecimento / conteúdos específicos que serão trabalhados neste dia (derivados do planejamento mensal acima)
- "objetivos": lista de 2 objetivos específicos da aula do dia
- "estrategia": texto descrevendo Abertura, Desenvolvimento e Fechamento específicos para o conhecimento do dia
- "recursos": texto com os recursos didáticos{" e tecnologias assistivas DUA/CAA/TEACCH" if eh_pei else ""}
- "avaliacao": texto com a avaliação formativa do dia

NÃO escreva nada fora do array JSON."""

    system_prompt = "Você é um professor experiente gerador de planos de aula estruturados. Responda apenas JSON válido."
    resposta_ia = chamar_ia(prompt, system_prompt=system_prompt, max_tokens=3200)
    encontros_ia = _extrair_encontros_json(resposta_ia, encontros_a_gerar)

    # Constrói os planos individuais para cada encontro
    for dia in range(1, encontros_a_gerar + 1):
        semana_num = ((dia - 1) // max(1, len(dias_semana.split(",")))) + 1
        item_ia = encontros_ia[dia - 1] if dia - 1 < len(encontros_ia) else {}

        tema_dia = str(item_ia.get("tema") or "").strip()
        if not tema_dia:
            tema_dia = f"{componente} — Semana {semana_num} (Encontro {dia})"

        conhecimentos_raw = item_ia.get("conhecimentos")
        if isinstance(conhecimentos_raw, list) and conhecimentos_raw:
            conhecimentos_linhas = "\n".join(f"- {str(c).strip()}" for c in conhecimentos_raw if str(c).strip())
        elif isinstance(conhecimentos_raw, str) and conhecimentos_raw.strip():
            conhecimentos_linhas = f"- {conhecimentos_raw.strip()}"
        else:
            conhecimentos_linhas = f"- Conteúdos e objetos de conhecimento de {componente} previstos para a Semana {semana_num} de {mes_nome}."

        objetivos_raw = item_ia.get("objetivos")
        if isinstance(objetivos_raw, list) and objetivos_raw:
            objetivos_linhas = "\n".join(f"- {str(o).strip()}" for o in objetivos_raw if str(o).strip())
        elif isinstance(objetivos_raw, str) and objetivos_raw.strip():
            objetivos_linhas = f"- {objetivos_raw.strip()}"
        else:
            objetivos_linhas = (
                f"- Desenvolver e aplicar os conceitos fundamentais de {componente} programados para a Semana {semana_num}.\n"
                f"- Estimular o raciocínio crítico, a interação dialógica e a resolução colaborativa de problemas."
            )

        estrategia_txt = str(item_ia.get("estrategia") or "").strip()
        recursos_txt = str(item_ia.get("recursos") or "").strip()
        avaliacao_txt = str(item_ia.get("avaliacao") or "").strip()

        if eh_pei:
            if not estrategia_txt:
                estrategia_txt = (
                    "- Acolhida e Rotina Visual: Apresentação da sequência da aula em prancha de rotina (Passo 1 -> Passo 2 -> Passo 3).\n"
                    "- Desenvolvimento com Apoio Multissensorial: Explicação com material concreto, estímulo visual (cartões com figuras/pictogramas) e comandos curtos e diretos.\n"
                    "- Ação e Expressão: Permitir resposta oral, por apontamento em prancha CAA ou com uso de recursos digitais.\n"
                    "- Encerramento Positivo: Reforço positivo imediato e registro do progresso na ficha de acompanhamento."
                )
            if not recursos_txt:
                recursos_txt = "- Prancha de Comunicação Alternativa (CAA/ARASAAC), recursos manipuláveis, teclado acessível ou leitor digital conforme necessidade."
            if not avaliacao_txt:
                avaliacao_txt = "- Verificação do engajamento e alcance das metas individuais sem sobrecarga sensorial ou pressão de tempo."

            plano_conteudo = f"""PLANO DE AULA PEI — MÊS DE {mes_nome.upper()} (ENCONTRO {dia})
1. IDENTIFICAÇÃO:
- Estudante: {(dados_pei or {}).get('nome', 'Estudante')}
- Componente Curricular: {componente} | Ano/Série: {nome_curso}
- Tema do Dia: {tema_dia}
- Encontro: Dia {dia} do mês de {mes_nome} (Semana {semana_num})
- Carga: {aulas_por_dia} aulas de {duracao_aula} min ({aulas_por_dia * duracao_aula} min)

2. CONHECIMENTOS TRABALHADOS NO DIA (ADAPTADOS):
{conhecimentos_linhas}

3. OBJETIVOS ESPECÍFICOS INDIVIDUALIZADOS (PEI):
{objetivos_linhas}

4. ESTRATÉGIAS DIDÁTICAS INCLUSIVAS (DUA & TEACCH):
{limpar_simbolos_ia(estrategia_txt)}

5. TECNOLOGIAS ASSISTIVAS E RECURSOS:
{limpar_simbolos_ia(recursos_txt)}

6. AVALIAÇÃO PROCESSUAL FORMATIVA:
{limpar_simbolos_ia(avaliacao_txt)}"""
        else:
            if not estrategia_txt:
                estrategia_txt = (
                    "- Abertura (10 min): Acolhimento, contextualização do tema e levantamento de conhecimentos prévios.\n"
                    "- Desenvolvimento (30 min): Exposição dialogada, análise de exemplos práticos e realização de atividade orientada.\n"
                    "- Fechamento (10 min): Síntese das aprendizagens do encontro e esclarecimento de dúvidas."
                )
            if not recursos_txt:
                recursos_txt = "- Livro didático, quadro branco, material impresso e projetor multimídia."
            if not avaliacao_txt:
                avaliacao_txt = "- Observação ativa do engajamento, participação nas discussões e resolução dos exercícios propostos."

            plano_conteudo = f"""PLANO DE AULA — MÊS DE {mes_nome.upper()} (ENCONTRO {dia})
1. IDENTIFICAÇÃO:
- Componente Curricular: {componente} | Ano/Série: {nome_curso}
- Tema do Dia: {tema_dia}
- Encontro: Dia {dia} de {mes_nome} (Semana {semana_num})
- Carga: {aulas_por_dia} aula(s) de {duracao_aula} min ({aulas_por_dia * duracao_aula} min)
- Dias de aula na semana: {dias_semana}

2. CONHECIMENTOS TRABALHADOS NO DIA:
{conhecimentos_linhas}

3. OBJETIVOS DA AULA (BNCC):
{objetivos_linhas}

4. ESTRATÉGIA DIDÁTICA:
{limpar_simbolos_ia(estrategia_txt)}

5. RECURSOS DIDÁTICOS:
{limpar_simbolos_ia(recursos_txt)}

6. AVALIAÇÃO DA APRENDIZAGEM:
{limpar_simbolos_ia(avaliacao_txt)}"""

        planos_gerados.append({
            "dia": dia,
            "semana": semana_num,
            "mes": mes_nome,
            "tema": f"Aula {dia} ({mes_nome}) — {tema_dia}",
            "conteudo": plano_conteudo
        })

    return planos_gerados, total_encontros, total_aulas


# ==============================================================================
# 5. MÓDULO PEI (PLANO EDUCACIONAL INDIVIDUALIZADO) — SEMESTRAL E MENSAL
# ==============================================================================

def gerar_planejamento_pei_semestral_ia(aluno_nome: str, necessidades: str, segmento: str, componente: str,
                                       semestre: int = 1, tecnologias_assistivas: str = "", serie: str = ""):
    """
    Gera o Plano Educacional Individualizado (PEI) Semestral, estruturado com foco em
    metodologias inclusivas globais (DUA, CAA, TEACCH, ABA e Tecnologias Assistivas).
    """
    meses_info = obter_meses_por_semestre(semestre)
    nomes_meses_str = ", ".join([m["nome"] for m in meses_info])

    prompt = f"""Você é uma das maiores autoridades mundiais em Educação Especial Inclusiva, Tecnologias Assistivas e Elaboração de PEI (Plano Educacional / de Ensino Individualizado).
Gere um PEI SEMESTRAL completo, altamente técnico, humanizado e prático para o {semestre}º SEMESTRE ({nomes_meses_str}):

DADOS DO ALUNO E CONTEXTO:
- Nome do Estudante: {aluno_nome}
- Série / Turma: {serie or "Ensino Regular"}
- Componente Curricular: {componente}
- Segmento: {segmento}
- Diagnóstico / Necessidades Educacionais Específicas: {necessidades}
- Tecnologias Assistivas / Recursos prévios indicados: {tecnologias_assistivas or "A definir com base nas melhores tecnologias disponíveis no mundo"}

ESTRUTURE O PEI SEMESTRAL RIGOROSAMENTE NAS SEGUINTES SEÇÕES:
1. PERFIL PEDAGÓGICO, POTENCIALIDADES E BARREIRAS DE APRENDIZAGEM
   (Mapeamento de barreiras cognitivas, sensoriais, comunicativas ou motoras)
2. OBJETIVOS E METAS PRIORITÁRIAS PARA O {semestre}º SEMESTRE
   (Habilidades curriculares adaptadas e competências socioemocionais realistas)
3. DESENHO UNIVERSAL PARA A APRENDIZAGEM (DUA / UDL):
   - Múltiplas formas de Representação (como apresentar os conteúdos)
   - Múltiplas formas de Ação e Expressão (como o aluno demonstra o aprendizado)
   - Múltiplas formas de Engajamento (motivação, autorregulação e interesses)
4. TECNOLOGIAS ASSISTIVAS E METODOLOGIAS INCLUSIVAS GLOBAIS RECOMENDADAS:
   - Comunicação Aumentativa e Alternativa (CAA - Pictogramas ARASAAC, pranchas digitais/físicas, vocalizadores)
   - Estruturação de Rotina e Apoios Visuais (TEACCH, agendas visuais, quebra em micro-passos)
   - Recursos Tecnológicos Digitais (Leitores de tela, audiolivros, teclados adaptados, softwares acessíveis)
5. CRONOGRAMA DE INTERVENÇÃO SEMESTRAL (Metas para {nomes_meses_str})
6. CRITÉRIOS DE AVALIAÇÃO PROCESSUAL E INDICADORES DE EVOLUÇÃO
   (Avaliação qualitativa, sem penalização por tempo ou velocidade motora)

NÃO utilize marcações Markdown como **, ##, ###, ---, * ou blocos de código; escreva em texto limpo."""

    system_prompt = "Você é um especialista internacional em Educação Especial Inclusiva, DUA, CAA e elaboração de PEI."
    texto_gerado = limpar_simbolos_ia(chamar_ia(prompt, system_prompt=system_prompt, max_tokens=2800))

    if not texto_gerado or len(texto_gerado.strip()) < 120:
        texto_gerado = f"""PLANO EDUCACIONAL INDIVIDUALIZADO (PEI) — {semestre}º SEMESTRE
Estudante: {aluno_nome} | Componente: {componente} | Série: {serie}
Necessidades Educacionais Específicas: {necessidades}
Período: {nomes_meses_str}

1. PERFIL PEDAGÓGICO E POTENCIALIDADES:
O estudante demonstra potencial para aprendizagem significativa com apoio pedagógico individualizado, recursos visuais e mediação constante.

2. METAS PRIORITÁRIAS DO {semestre}º SEMESTRE:
- Desenvolver a autonomia e a participação nas atividades de {componente}.
- Ampliar a compreensão dos conceitos essenciais por meio de recursos multissensoriais.
- Estimular a comunicação expressiva e a interação com pares e educadores.

3. ESTRATÉGIAS DE DESENHO UNIVERSAL PARA A APRENDIZAGEM (DUA):
- Representação: Conteúdos apresentados com linguagem clara, esquemas visuais, cores e materiais concretos.
- Ação e Expressão: Aceitação de respostas orais, apontamento, emparelhamento de figuras e digitação assistida.
- Engajamento: Atividades conectadas aos centros de interesse do estudante e intercaladas com momentos de pausa regulatória.

4. TECNOLOGIAS ASSISTIVAS E METODOLOGIAS:
- Prancha de Comunicação Aumentativa e Alternativa (CAA / ARASAAC).
- Estruturação TEACCH com rotina visual do início ao fim de cada encontro.
- Tecnologias digitais de suporte e materiais manipuláveis.

5. CRONOGRAMA DE INTERVENÇÃO:
""" + "\n".join([f"- Mês de {m['nome']}: Metas de curto prazo e adaptações curriculares correspondentes." for m in meses_info]) + """

6. AVALIAÇÃO PROCESSUAL FORMATIVA:
Avaliação contínua descritiva, focada nas aquisições individuais e no respeito ao ritmo do estudante."""

    dados_estruturados = {
        "tipo": "pei_semestral",
        "aluno_nome": aluno_nome,
        "necessidades": necessidades,
        "semestre": semestre,
        "meses": [m["nome"] for m in meses_info],
        "componente": componente,
        "serie": serie,
        "tecnologias_assistivas": tecnologias_assistivas
    }

    return texto_gerado, dados_estruturados


def gerar_planejamento_pei_mensal_ia(pei_semestral_texto: str, aluno_nome: str, necessidades: str,
                                     componente: str, mes_nome: str, numero_mes: int):
    """
    Gera o Plano de Intervenção Mensal do PEI para o mês letivo selecionado.
    """
    prompt = f"""Você é um especialista em Educação Especial e intervenção psicopedagógica.
Elabore o PLANO DE INTERVENÇÃO MENSAL DO PEI para o mês de {mes_nome.upper()}:
- Estudante: {aluno_nome}
- Componente: {componente}
- Mês Letivo: {mes_nome} (Mês {numero_mes})
- Necessidades Educacionais: {necessidades}

Considere as diretrizes do PEI Semestral:
{pei_semestral_texto[:2500]}

O Plano Mensal do PEI deve conter:
1. METAS DE CURTO PRAZO PARA O MÊS DE {mes_nome.upper()}
2. ADAPTAÇÕES CURRICULARES ESPECÍFICAS (redução de enunciados, foco nas habilidades funcionais)
3. RECURSOS DUA E TECNOLOGIAS ASSISTIVAS DO MÊS (materiais táteis, CAA, suportes visuais)
4. DIVISÃO DAS ATIVIDADES POR SEMANAS (Semanas 1 a 4)
5. CRITÉRIOS DE REGISTRO E FEEDBACK POSITIVO

NÃO utilize marcações Markdown como **, ##, ###, ---, * ou blocos de código; escreva em texto limpo."""

    system_prompt = "Você é um especialista em adaptação curricular e intervenção mensal inclusiva para PEI."
    texto_gerado = limpar_simbolos_ia(chamar_ia(prompt, system_prompt=system_prompt, max_tokens=2200))

    if not texto_gerado or len(texto_gerado.strip()) < 100:
        texto_gerado = f"""PLANO DE INTERVENÇÃO MENSAL (PEI) — {mes_nome.upper()}
Estudante: {aluno_nome} | Componente: {componente} | Necessidades: {necessidades}

1. METAS DE CURTO PRAZO PARA {mes_nome.upper()}:
- Conectar o conteúdo de {componente} à rotina do estudante através de recursos visuais e táteis.
- Estimular a conclusão autônoma de pelo menos 2 tarefas curtas por encontro.

2. ADAPTAÇÕES CURRICULARES DO MÊS:
- Enunciados fragmentados em comandos de única ação.
- Eliminação de elementos distratores visuais e auditivos.
- Suporte visual contínuo com cartões de rotina e pictogramas.

3. RECURSOS E TECNOLOGIA ASSISTIVA:
- Prancha de comunicação alternativa temática para a aula.
- Material concreto manipulável e suportes ergonômicos.

4. DISTRIBUIÇÃO SEMANAL:
- Semana 1: Apresentação acolhedora e exploração multissensorial dos conceitos.
- Semana 2: Atividades práticas assistidas com mediação direta.
- Semana 3: Exercícios com aumento progressivo de autonomia do estudante.
- Semana 4: Registro da evolução, portfólio de atividades e celebração das conquistas."""

    dados_estruturados = {
        "tipo": "pei_mensal",
        "aluno_nome": aluno_nome,
        "necessidades": necessidades,
        "mes_nome": mes_nome,
        "numero_mes": numero_mes,
        "componente": componente
    }

    return texto_gerado, dados_estruturados
