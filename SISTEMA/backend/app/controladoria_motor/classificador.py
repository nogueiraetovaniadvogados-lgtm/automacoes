"""Classificador de intimacoes — as 5 decisoes, com evidencias e confianca.

1. titularidade  — o prazo e do nosso cliente ou da parte contraria?  (via ADVBOX)
2. ato           — qual ato o despacho exige?                          (catalogo)
3. dias/contagem — quantos dias e contados como?                       (tabela|despacho)
4. trilha        — quem executa: PECA (Advogada) | MEIO (Advogada) | CIENCIA
5. prazos[]      — um ou mais (uma sentenca abre recurso E embargos)

Nada aqui e caixa-preta: cada decisao vem com o gatilho que a produziu. A IA nao
entra neste modulo — ela so le e propoe em outro lugar. Prazo = motor_prazos.

Calibracao de 01/10/2026 (auditoria de 30 publicacoes com a Dra. Advogada, ver
memoria controladoria-esteira-intimacoes): oito regras entraram aqui e no
titularidade.py. As que vivem neste arquivo:
  - o PJe escreve "Agravo(s) de Instrumento" e "contraminutar(em)": o marcador
    entre parenteses quebrava o gatilho e o ato caia em "ciencia";
  - prazo escrito por extenso ("quinze dias") ou como "em 15 dias" nao era lido
    e o SYNC devolvia 5 generico;
  - "cumprimento de sentenca" so e pagamento voluntario quando somos executados;
  - responsavel e o advogado do caso (carteira dedicada em advogados_processos.json;
    parceiro quando o processo e de parceiro), por padrao a revisora (Dra. Advogada);
  - a copia enderecada a parte contraria nunca abre prazo nosso;
  - publicacao com "prazo de N dias" mas sem o teor da decisao nao e ciencia:
    e "abrir os autos".
"""
from __future__ import annotations
import json
import os, re, unicodedata
from pathlib import Path

from . import motor_prazos as MP
from . import titularidade as TT
from . import compromisso as CP

_CATALOGO = json.loads((Path(__file__).parent / 'catalogo_atos.json').read_text('utf-8'))
LIMIAR_CONFIANCA = 0.75   # abaixo disso NAO age (so reporta) — calibrar com a Advogada

# Advogado do caso fora do padrao (carteira dedicada) e processos de parceiro.
try:
    _ADVOGADOS = json.loads((Path(__file__).parent / 'advogados_processos.json').read_text('utf-8'))
except Exception:
    _ADVOGADOS = {}
_CARTEIRA = set(_ADVOGADOS.get('carteira_dedicada') or [])
_CARTEIRA_NOME = os.getenv('ESC_CARTEIRA_DEDICADA_NOME', 'Advogada')
_PARCEIROS = _ADVOGADOS.get('parceiros') or {}

# Marcadores de plural/genero do PJe: "o(s) agravo(s)", "intimado(a)",
# "contraminutar(em)". Sem tirar isso, "agravo(s) de instrumento" nunca casa
# com o gatilho "agravo de instrumento" (erro real: caso C x empresa C, 24/09/2026).
_MARCADOR = re.compile(r'\((?:s|a|as|os|em|m|ns|is|eis|ao|oes)\)')


def _norm(s: str) -> str:
    if not s:
        return ''
    s = unicodedata.normalize('NFKD', str(s)).encode('ascii', 'ignore').decode()
    s = re.sub(r'<[^>]+>', ' ', s)          # tira HTML
    s = _MARCADOR.sub('', s)                # tira "(s)", "(em)", "(a)"
    s = re.sub(r'\s+', ' ', s)
    return s.lower().strip()


def _ramo(tribunal: str) -> str:
    t = (tribunal or '').upper()
    return 'trabalhista' if ('TRT' in t or t.startswith('TST')) else 'civel'


# Numero por extenso, como os despachos escrevem.
_EXTENSO = {
    'um': 1, 'dois': 2, 'tres': 3, 'quatro': 4, 'cinco': 5, 'seis': 6, 'sete': 7,
    'oito': 8, 'nove': 9, 'dez': 10, 'doze': 12, 'quinze': 15, 'vinte': 20,
    'trinta': 30, 'quarenta e cinco': 45, 'sessenta': 60, 'noventa': 90,
}
_NUM = r'(\d{1,3}|' + '|'.join(sorted(_EXTENSO, key=len, reverse=True)) + r')'
# Ordem importa: a forma "prazo ... de N dias" e a mais confiavel; "em N dias"
# vem depois; "N (extenso) dias" por ultimo.
_RE_DIAS = [
    re.compile(r'prazo ?:? ?(?:comum |sucessivo |legal |improrrogavel |de |)(?:de )?' + _NUM + r' ?(?:\([a-z ]+\) ?)?dias?'),
    re.compile(r'no prazo de ' + _NUM + r'\b'),
    re.compile(r'\bem ' + _NUM + r' ?(?:\([a-z ]+\) ?)?dias?\b'),
    re.compile(r'\b' + _NUM + r' ?\([a-z ]+\) ?dias?\b'),
    re.compile(r'\b' + _NUM + r' dias? uteis\b'),
]


def _para_int(tok: str):
    tok = (tok or '').strip()
    if tok.isdigit():
        return int(tok)
    return _EXTENSO.get(tok)


def _dias_do_texto(texto: str) -> int | None:
    """Extrai o prazo do despacho: 'prazo de N dias', 'no prazo de N', 'em N dias',
    'N (extenso) dias', com N em algarismo ou por extenso."""
    t = _norm(texto)
    for rx in _RE_DIAS:
        m = rx.search(t)
        if m:
            n = _para_int(m.group(1))
            if n and 1 <= n <= 90:
                return n
    return None


# Cabecalhos que nao sao o ato. Duas fontes, os dois erros reais:
#
# 1. PJe. Toda intimacao comeca com "Fica V. Sa. intimado para tomar ciencia da
#    Sentenca ID xxx proferida nos autos". A palavra "sentenca" ali e do
#    cabecalho, nao do dispositivo: um despacho que so mandava arquivar um
#    processo ja encerrado virou RECURSO ORDINARIO com prazo de 8 dias.
#
# 2. e-SAJ. A publicacao do TJSP abre com a CLASSE e o ASSUNTO do processo
#    ("- Cumprimento de sentenca - Penhora / Deposito / Avaliacao -"). Uma
#    homologacao de acordo virou IMPUGNACAO AO CUMPRIMENTO, peca de 15 dias,
#    porque "penhora" e o assunto cadastrado ha anos.
#
# Ler o dispositivo, nao a etiqueta.
# "Fica V. Sa." tem pontos: o padrao nao pode ser [^.]. E o TRT6 escreve "ciencia DE
# decisao", nao "da" (os dois erros reais no Jonatas 77502).
_CAB_PJE = re.compile(
    r'fica.{0,140}?tomar ciencia d[aeo].{0,140}?'
    r'(?:proferid[oa] nos autos|consta a seguir:|a seguir:)')
_CAB_SAJ = re.compile(
    r'^(processo [\d.\-]+ (?:\([^)]*\) )?)-[^-]{3,70}-[^-]{3,70}-')

# Publicacao que so tem o cabecalho: o teor da decisao ficou nos autos.
# Em duas passagens: primeiro enderecos (a URL do PJe tem pontos que quebram as
# frases), depois as frases de assinatura, rodape e cabecalho.
_URLS = re.compile(r'https?://\S+|\S+\.(?:jus|gov|com)\.br\S*')
_BOILERPLATE = re.compile(
    r'documento assinado digitalmente.{0,600}?(?:processo judicial eletronico\.?|$)|'
    r'o documento pode ser acessado[^.]{0,200}\.?|'
    r'lei 11\.419/2006\.?|mp n?o? ?2\.200-2/2001|'
    r'intimado ?/ ?citado\s*-?.{0,200}$|prazo ?:? ?(?:de )?\d{1,3} ?(?:\([a-z ]+\) ?)?dias?\.?|'
    r'^poder judiciario.{0,400}?\b(?:intimacao|notificacao)\b|'
    r'destinatario:?\s*[^:]{3,120}|'
    r'(?:reclamante|reclamad[oa]|autor|autora|reu|requerente|requerid[oa]|exequente|executad[oa])\s*:\s*[^:]{3,120}|'
    r'\b(?:cumprse|atord|atsum|rot|airr|ag|ed) [\d.\-]{10,30}|processo (?:pje|n[.o]?)|'
    r'[a-z ]+/[a-z]{2}, \d{1,2} de [a-z]+ de \d{4}\.?|\d{3}-\d/\d{4}|'
    r'cujo dispositivo consta a seguir:?|\b(?:juiz|juiza) do trabalho(?: titular| substitut[oa])?|'
    r'desembargador[a]? do trabalho|diretor[a]? de secretaria|assessor[a]?|servidor[a]?|'
    # TRT6 anexa a 'situacao do cadastro do processo' (partes, CPF/CNPJ, advogados)
    r'situacao do cadastro do processo.{0,900}?(?:-{5,}|$)|advogado\(?s?\)?:?[^:]{0,160}|'
    r'cpf: ?[\d.\-]+|cnpj: ?[\d./\-]+|oab: ?\d+|-{5,}|/[a-z]{2,6}')


def _so_cabecalho(texto_norm):
    """True quando, tirado o cabecalho e o boilerplate, nao sobra dispositivo."""
    t = _CAB_PJE.sub(' ', texto_norm)
    t = _URLS.sub(' ', t)
    t = _BOILERPLATE.sub(' ', t)
    t = re.sub(r'[^a-z]+', ' ', t).strip()
    return len(t) < 80

# Grau pelo cabecalho da publicacao. Caso Romildo (1000101-08.2025.5.06.0016, 05/10/2026):
# acordao da 3a Turma do TRT6 pontuou 'sentenca' e 'recurso ordinario' da ementa e virou
# RECURSO ORDINARIO; o recurso cabivel contra acordao e o de revista (art. 896 CLT), com ED
# em paralelo. O cabecalho (orgao, 'intimadas do acordao', 'PJe 2o grau') diz o grau.
_GRAU2 = re.compile(
    r'tribunal regional do trabalho.{0,120}?\bturma\b|tribunal de justica.{0,160}?\bcamara\b|'
    r'intimad[ao]s? d[oa] acordao|acordao proferido|acordao em \d{2}/\d{2}/\d{4}|'
    r'\b2o? ?grau\b|segundo grau|\brecorrente ?:|\bturma\b.{0,60}?\brelator|'
    # e-SAJ do TJSP (0000781-63.2008.8.26.0318, 12/11/2026): "INTIMACAO DE ACORDAO ... Deram provimento"
    r'intimacao de acordao')
# O campo `orgao` diz o grau melhor que o texto: decisao de vara que transcreve ementa ("9a Camara
# de Direito Privado") ou cita "o v. acordao proferido no agravo" virava 2o grau (23/11/2026).
_ORGAO_2 = re.compile(r'\bcamara\b|\bturma\b|\bsecao\b|\bgrupo\b|desembargador|\bsdi\b|orgao especial')
_ORGAO_1 = re.compile(r'\bvara\b|\bforo\b|\bupj\b|juizado|\bliq ?\d|\bcon ?\d|\bcejusc\b')
_GRAU3 = re.compile(r'tribunal superior do trabalho|superior tribunal de justica|supremo tribunal federal|'
                    r'ministr[oa] relator|\bsdi\b|secao de dissidios')
_SO_NO_1O_GRAU = {'trabalhista': {'RECURSO ORDINARIO': 'RECURSO DE REVISTA'},
                  'civel': {'APELACAO': 'RECURSO ESPECIAL / EXTRAORDINARIO'}}


# Decisao que JULGA embargos de declaracao ja opostos (caso Carla x Notre Dame, 0033845-46,
# 16/11/2026): "CONHECO E REJEITO os embargos de declaracao" batia os gatilhos do ED
# (omissao, contradicao...) e virava "cabem ED" em 5 dias. O cabivel e o recurso contra a
# decisao embargada, com o prazo reiniciado por inteiro (art. 1.026 CPC / 897-A, 3o CLT).
_ED_JULGADOS = re.compile(
    r'\b(?:conheco|nao conheco|rejeito|acolho|desacolho|julgo (?:im)?procedentes?|nego provimento|'
    r'dou (?:parcial )?provimento)\b[^.]{0,60}?\bembargos\b|'
    r'\bembargos\b[^.]{0,60}?\b(?:rejeitados|acolhidos|conhecidos|improcedentes|procedentes|desprovidos|providos)\b')


_ED_MANIFESTAR = re.compile(
    r'\bmanifest\w*(?: ?-?se)?\b[^.]{0,60}?\bsobre (?:os|o recurso de) embargos\b|'
    r'\bembargos de declaracao (?:opostos|interpostos|apresentados)\b[^.]{0,80}?\bmanifest|'
    r'\bcontrarrazo\w* aos embargos\b')


def recurso_apos_ed(ramo: str, grau: int, tipo_ato: str, texto_norm: str) -> str:
    """Recurso contra a decisao embargada, depois que os ED foram julgados."""
    tipo = _norm(tipo_ato)
    execucao = bool(re.search(r'cumprimento (?:provisorio )?de sentenca|execucao', texto_norm))
    if ramo == 'trabalhista':
        if grau >= 2:
            return 'RECURSO DE REVISTA'
        return 'AGRAVO DE PETICAO' if execucao else 'RECURSO ORDINARIO'
    if grau >= 2:
        return 'RECURSO ESPECIAL / EXTRAORDINARIO'
    return 'APELACAO' if 'sentenca' in tipo and not execucao else 'AGRAVO DE INSTRUMENTO'


def grau_do_texto(texto_norm: str, orgao: str = '') -> int:
    """1 = vara; 2 = TRT/TJ (Turma/Camara); 3 = TST/STJ/STF. Pelo texto inteiro, nao pelo corpo.
    Com `orgao` claro (Camara/Turma x Vara/Foro), ele manda sobre o texto."""
    if _GRAU3.search(texto_norm):
        return 3
    o = _norm(orgao)
    if _ORGAO_2.search(o):
        return 2
    if _ORGAO_1.search(o):
        return 1
    if _GRAU2.search(texto_norm):
        return 2
    return 1


# Sigilo: o DJEN nao traz nada alem do aviso.
_SIGILOSO = re.compile(r'processo sigiloso|documento e sigiloso|consulte os autos digitais')


def _corpo(texto_norm):
    """O texto sem o cabecalho de sistema.

    Nunca devolve vazio: se a limpeza comeu quase tudo, volta o original,
    porque classificar sem texto e pior do que classificar com ruido.
    """
    t = _CAB_PJE.sub(' ', texto_norm)
    t = _CAB_SAJ.sub(r'\1', t)
    t = re.sub(r'\s+', ' ', t).strip()
    return t if len(t) >= 40 else texto_norm


# Atos que so existem para quem esta sendo cobrado. Com o cliente no polo
# ATIVO (autor, reclamante, exequente) nao ha contestacao, pagamento voluntario
# nem impugnacao ao cumprimento nossos (casos reais: Benicio Arantes, Doralice,
# Rosemar 0004255-15).
_SO_DO_PASSIVO = {'CONTESTACAO', 'PAGAMENTO VOLUNTARIO (CUMPRIMENTO)',
                  'IMPUGNACAO AO CUMPRIMENTO DE SENTENCA', 'EMBARGOS A EXECUCAO',
                  'PAGAMENTO EM EXECUCAO'}


def _pontua_atos(texto: str, ramo: str, nosso_polo: str | None = None):
    """Para cada ato do ramo, soma 1 + len(gatilho)/40 por gatilho batido.
    Gatilho longo (mais especifico) pesa mais. Retorna lista ordenada."""
    t = _corpo(_norm(texto))
    res = []
    for nome, cfg in _CATALOGO.get(ramo, {}).items():
        score = 0.0
        hits = []
        for g in cfg.get('gatilhos', []):
            gn = _norm(g)
            if gn and gn in t:
                score += 1 + len(gn) / 40.0
                hits.append(g)
        if cfg.get('trilha') == 'CIENCIA':
            score *= 0.6            # ciencia so vence se nada com prazo bater
        if nome in _SO_DO_PASSIVO and nosso_polo == 'ativo':
            score = 0.0
        if score > 0:
            res.append((score, nome, cfg, hits))
    res.sort(key=lambda x: x[0], reverse=True)
    return res


def _lista(v):
    """clientes/adversos chegam como lista ou como JSON em string (advbox_titularidade)."""
    if not v:
        return []
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except Exception:
            return [v]
    return [x for x in (v if isinstance(v, list) else [v]) if x]


def _responsavel(trilha: str, processo: str | None):
    """Quem executa: advogado do caso, nao um nome fixo (regra 5 da calibracao)."""
    parceiro = _PARCEIROS.get(processo or '')
    if parceiro and parceiro.get('modo') == 'encaminhar':
        return 'Advogada', parceiro
    if trilha == 'PECA':
        return (_CARTEIRA_NOME if processo in _CARTEIRA else 'Advogada'), parceiro
    if trilha == 'MEIO':
        return 'Advogada', parceiro
    return None, parceiro


def _audiencia_no_texto(texto, orgao, tnorm, ramo, disp):
    """Despacho composto (a equipe, 06/10/2026): a mesma publicacao recebe a emenda, DESIGNA
    AUDIENCIA e da 5 dias para documentos. O ato principal e o prazo; a audiencia nao pode sumir.
    Devolve {'data','hora','modalidade','especie','local','link'} ou None."""
    gatilhos = (_CATALOGO.get(ramo, {}).get('AUDIENCIA DESIGNADA') or {}).get('gatilhos') or []
    corpo = _corpo(tnorm)
    if not any(_norm(g) in corpo for g in gatilhos):
        return None
    try:
        c = CP.extrair(texto, orgao or '')
    except Exception:
        return None
    if not (c or {}).get('tem') or c.get('tipo') != 'audiencia' or not c.get('data'):
        return None
    if disp and str(c['data']) < str(disp)[:10]:
        return None                      # data no passado: e a audiencia que ja houve, nao a designada
    return {'data': c['data'], 'hora': c.get('hora') or '', 'modalidade': c.get('modalidade') or '',
            'especie': (c.get('especie') or '').lower(), 'local': c.get('local') or '', 'link': c.get('link') or ''}


def classificar(intimacao: dict, titularidade: dict | None = None) -> dict:
    """intimacao: dict do cache (campos: processo, tribunal, orgao, texto/resumo,
    data (disponibilizacao), requer_manifestacao, prazo_dias do SYNC).
    titularidade: opcional {'titularidade','nosso_polo','cliente','parte_adversa','origem',
    'clientes','adversos'}.
    """
    tribunal = intimacao.get('tribunal') or ''
    orgao = intimacao.get('orgao') or intimacao.get('orgao_julgador') or ''
    texto = intimacao.get('texto') or intimacao.get('resumo') or ''
    disp = intimacao.get('data') or intimacao.get('data_disponibilizacao')
    processo = intimacao.get('processo') or ''
    ramo = _ramo(tribunal)
    tnorm = _norm(texto)

    evidencias: list[str] = []
    avisos: list[str] = []

    _tit_pre = titularidade or {}
    nosso_polo = _tit_pre.get('nosso_polo')
    clientes_cad = _lista(_tit_pre.get('clientes')) or _lista(_tit_pre.get('cliente'))
    adversos_cad = _lista(_tit_pre.get('adversos')) or _lista(_tit_pre.get('parte_adversa'))
    # Regra 3 da calibracao: sem polo no cadastro, a propria publicacao diz quem
    # e quem (cabecalho do PJe ou do e-SAJ). Banco do caso D x empresa D e a
    # interdicao do Saulo (01/10/2026) so se resolvem assim.
    polo_inferido = None
    if clientes_cad:
        polo_pub, fonte_polo = TT.polo_pelo_cabecalho(
            texto, clientes_cad, _tit_pre.get('polo_ativo'), _tit_pre.get('polo_passivo'))
        if polo_pub and not nosso_polo:
            nosso_polo = polo_pub
            polo_inferido = 'polo=%s lido da %s (cadastro sem polo)' % (polo_pub, fonte_polo)
        elif polo_pub and nosso_polo and polo_pub != nosso_polo:
            # O cadastro erra o polo em ~22% dos casos (a equipe constava como
            # passivo e e a reclamante). A publicacao e a capa valem mais.
            polo_inferido = ('polo=%s pela %s; o cadastro dizia %s e foi ignorado'
                             % (polo_pub, fonte_polo, nosso_polo))
            nosso_polo = polo_pub
        # Terceiro: o cabecalho lista as partes e o nosso cliente nao e nenhuma
        # delas (BB x Etabel, em que defendemos um terceiro embargante). Afirmar
        # polo aqui e chute; melhor pedir conferencia.
        partes_cab = [p for p in TT.partes_cabecalho_lista(texto) if not TT.eh_iniciais(p)]
        if partes_cab and nosso_polo and not any(
                TT.semelhanca(p, c) >= 0.34 for p in partes_cab for c in clientes_cad):
            avisos.append('Nosso cliente nao aparece entre as partes do cabecalho (%s): '
                          'terceiro? conferir de quem e o prazo' % ' x '.join(partes_cab[:3])[:90])
            nosso_polo = None
            polo_inferido = None
    ranking = _pontua_atos(texto, ramo, nosso_polo)
    if ranking:
        best_score, ato, cfg, hits = ranking[0]
        segundo = ranking[1][0] if len(ranking) > 1 else 0.0
        evidencias.append(f"ato='{ato}' por gatilhos: {', '.join(hits[:4])}")
        gap = best_score - segundo
        confianca = min(0.95, 0.45 + 0.12 * len(hits) + 0.10 * min(gap, 2.0))
    else:
        # nada bateu: fallback seguro
        if intimacao.get('requer_manifestacao'):
            ato = 'MANIFESTACAO GENERICA'
        else:
            ato = 'CIENCIA / MERO EXPEDIENTE'
        cfg = _CATALOGO.get(ramo, {}).get(ato, {})
        confianca = 0.30
        avisos.append('Nenhum gatilho do catalogo bateu — classificado por fallback, conferir')

    # Grau: recurso de sentenca nao cabe contra acordao (caso Romildo). O texto inteiro
    # (com cabecalho) diz o grau; o corpo, usado nos gatilhos, nao.
    grau = grau_do_texto(tnorm, orgao)
    troca_grau = _SO_NO_1O_GRAU.get(ramo, {}).get(ato)
    if grau == 2 and troca_grau and troca_grau in _CATALOGO.get(ramo, {}):
        evidencias.append("2o grau pelo cabecalho (Turma/Camara, 'intimadas do acordao' ou 'PJe 2o grau'): "
                          "a decisao e ACORDAO, nao sentenca; ato '%s' trocado por '%s' (ED em paralelo)" % (ato, troca_grau))
        ato = troca_grau
        cfg = _CATALOGO[ramo][ato]
        confianca = max(confianca, 0.85)
    elif grau == 3 and ato in ('RECURSO ORDINARIO', 'RECURSO DE REVISTA', 'APELACAO'):
        avisos.append('Publicacao de tribunal superior: o catalogo nao decide o recurso cabivel sozinho '
                      '(agravo interno, embargos a SDI, RE): conferir nos autos')

    if ato == 'EMBARGOS DE DECLARACAO' and _ED_JULGADOS.search(_corpo(tnorm)):
        novo = recurso_apos_ed(ramo, grau, intimacao.get('tipo_ato') or '', tnorm)
        if novo in _CATALOGO.get(ramo, {}):
            evidencias.append("decisao que JULGA embargos de declaracao ja opostos: o cabivel e o recurso contra "
                              "a decisao embargada ('%s'), com o prazo reiniciado por inteiro" % novo)
            avisos.append('ED julgados: conferir nos autos qual decisao foi embargada (interlocutoria x sentenca), '
                          'quem opos os embargos e se ha interesse em recorrer')
            ato = novo
            cfg = _CATALOGO[ramo][ato]
            confianca = min(confianca, 0.7)   # abaixo do limiar: uma pessoa confere antes de cadastrar
    elif ato == 'EMBARGOS DE DECLARACAO' and 'MANIFESTACAO SOBRE ED' in _CATALOGO.get(ramo, {}) and (
            any(a == 'MANIFESTACAO SOBRE ED' for _, a, _, _ in ranking) or _ED_MANIFESTAR.search(_corpo(tnorm))):
        # "intime-se a embargada para se manifestar sobre os ED opostos, que apontam omissao..." (23/11/2026):
        # as palavras do ED descrevem os embargos JA opostos; o ato e responder a eles.
        evidencias.append("despacho chama a embargada a se manifestar sobre embargos de declaracao ja opostos: "
                          "'MANIFESTACAO SOBRE ED', nao novos embargos")
        avisos.append('Conferir quem opos os embargos: se fomos nos, a manifestacao e da parte contraria')
        ato = 'MANIFESTACAO SOBRE ED'
        cfg = _CATALOGO[ramo][ato]
        confianca = min(confianca, 0.75)

    dias_despacho = _dias_do_texto(texto)

    # Despacho composto (a equipe, 06/10/2026): audiencia designada E prazo em dias no mesmo
    # texto. O prazo (que vence e preclui) manda na cadeia; a audiencia vira compromisso paralelo
    # (ver _audiencia_no_texto). Sem isto a juntada de 5 dias sumia atras da audiencia.
    if ato == 'AUDIENCIA DESIGNADA' and dias_despacho and ranking:
        outro = next(((sc, a, cf, h) for sc, a, cf, h in ranking
                      if 'AUDIENCIA' not in a and 'PERICIA' not in a and sc > 0), None)
        if outro:
            evidencias.append("despacho composto: audiencia designada e prazo de %d dias no mesmo texto; o prazo manda "
                              "('%s'), a audiencia vira compromisso paralelo" % (dias_despacho, outro[1]))
            _, ato, cfg, hits = outro
            confianca = max(confianca, 0.75)

    # Sigilo: nao ha o que classificar; a providencia e abrir os autos.
    sigiloso = bool(_SIGILOSO.search(tnorm)) and len(_corpo(tnorm)) < 220
    if sigiloso:
        ato = 'PROCESSO SIGILOSO'
        cfg = _CATALOGO.get(ramo, {}).get(ato, {'trilha': 'MEIO', 'fonte_prazo': 'despacho'})
        confianca = 0.9
        evidencias.append("ato='PROCESSO SIGILOSO': o DJEN so traz o aviso de sigilo")

    # Regra 1 da calibracao: publicacao que e so cabecalho, com o teor nos autos.
    #  - "ciencia da SENTENCA/ACORDAO ... consta a seguir:" com dispositivo vazio
    #    (Ariosvaldo 28971): o ato e recursal e o prazo corre do mesmo jeito.
    #  - "ciencia da DECISAO/DESPACHO ... Prazo: N dias" sem teor (Jonatas 77502):
    #    nao e ciencia, e abrir os autos para classificar pelo dispositivo.
    # So vale quando NENHUM gatilho bateu no corpo: "Vistos. Arquive-se." e curto
    # mas tem dispositivo, e continua ciencia.
    teor_fora = False
    if not sigiloso and not ranking and _so_cabecalho(tnorm):
        m_cab = re.search(r'tomar ciencia d[aeo] (sentenca|acordao|decisao|despacho)', tnorm)
        substantivo = m_cab.group(1) if m_cab else ''
        if substantivo == 'sentenca':
            ato = 'RECURSO ORDINARIO' if ramo == 'trabalhista' else 'APELACAO'
        elif substantivo == 'acordao':
            ato = 'RECURSO DE REVISTA' if ramo == 'trabalhista' else 'RECURSO ESPECIAL / EXTRAORDINARIO'
        elif substantivo:
            ato = 'DECISAO SEM TEOR NO DJEN'
            teor_fora = True
        if m_cab or teor_fora:
            cfg = _CATALOGO.get(ramo, {}).get(ato, {'trilha': 'MEIO', 'fonte_prazo': 'despacho'})
            confianca = max(confianca, 0.6)
            evidencias.append("ato='%s': a publicacao so traz o cabecalho (%s) e o teor ficou nos autos"
                              % (ato, substantivo or 'sem dispositivo'))
            if not teor_fora:
                avisos.append('Dispositivo nao veio no DJEN: ler a %s nos autos antes de decidir o recurso' % substantivo)

    trilha = cfg.get('trilha', 'CIENCIA')
    fonte = cfg.get('fonte_prazo', 'despacho')
    fatal = bool(cfg.get('fatal'))
    base_legal = cfg.get('base_legal', '-')
    if cfg.get('observacao'):
        avisos.append(cfg['observacao'])

    # ---- dias (regra de ouro de precedencia: texto > tabela > SYNC) ----
    dias_tabela = cfg.get('dias')
    # O `prazo_dias` do SYNC e generico (quase sempre 5) e serve de apoio, nunca
    # de fonte. Num ato de ciencia ele nao vale nada: prazo generico da
    # plataforma virava vencimento numa publicacao que so avisava a
    # distribuicao do processo.
    _e_ciencia = cfg.get('trilha') == 'CIENCIA'
    if dias_despacho is None and not _e_ciencia and not sigiloso:
        dias_despacho = intimacao.get('prazo_dias')
        if dias_despacho:
            avisos.append('Prazo nao lido no texto; usado o prazo_dias generico do SYNC (%s) — conferir' % dias_despacho)
    if fonte == 'tabela' and dias_tabela:
        dias = dias_tabela
        if dias_despacho and dias_despacho != dias_tabela and fatal:
            avisos.append(f'Texto cita {dias_despacho} dias mas ato fatal com prazo legal -> tabela vence ({dias_tabela})')
    elif fonte == 'audiencia':
        dias = None
        avisos.append('Prazo vinculado a audiencia — conferir data da audiencia')
    elif _e_ciencia:
        # Ciencia nao tem prazo nosso. Se o texto cita dias, o prazo e de outro
        # (Michele regularizar a representacao em 5 dias, 76144) e nao vira
        # vencimento na nossa ficha; fica so o aviso.
        dias = None
        if dias_despacho:
            avisos.append('Texto cita %s dias, mas o ato e de ciencia: o prazo e de outra parte ou de outro ato' % dias_despacho)
    else:  # despacho
        dias = dias_despacho
        if dias is None and not sigiloso:
            avisos.append('Prazo vem do despacho e nao foi encontrado no texto — conferir')

    contagem = cfg.get('contagem', 'uteis')

    # ---- prazos (1..N) ----
    prazos = []
    if dias is not None:
        p = MP.calcular_prazo(disp, dias, contagem, tribunal=tribunal, orgao=orgao)
        p.update({'ato': ato, 'fonte_prazo': fonte, 'fatal': fatal, 'base_legal': base_legal, 'paralelo': False})
        avisos += p.pop('avisos', [])
        prazos.append(p)
    # prazo paralelo (abre_junto): sentenca/acordao recursal abre tambem ED
    for _, aj in _CATALOGO.get('abre_junto', {}).items():
        if isinstance(aj, dict) and aj.get('principal') == ato:
            par_nome = aj['paralelo']
            par_cfg = _CATALOGO.get(ramo, {}).get(par_nome, {})
            if par_cfg.get('dias'):
                pp = MP.calcular_prazo(disp, par_cfg['dias'], par_cfg.get('contagem', 'uteis'),
                                       tribunal=tribunal, orgao=orgao)
                pp.update({'ato': par_nome, 'fonte_prazo': par_cfg.get('fonte_prazo', 'tabela'),
                           'fatal': bool(par_cfg.get('fatal')), 'base_legal': par_cfg.get('base_legal', '-'),
                           'paralelo': True})
                pp.pop('avisos', None)
                prazos.append(pp)
                evidencias.append(f"prazo paralelo '{par_nome}' aberto junto com '{ato}'")
    # despacho composto: audiencia designada junto com outro prazo (a equipe, 06/10/2026)
    audiencia = None
    if 'AUDIENCIA' not in ato and 'PERICIA' not in ato and not _so_cabecalho(tnorm):
        audiencia = _audiencia_no_texto(texto, orgao, tnorm, ramo, disp)
        if audiencia:
            evidencias.append("audiencia%s designada para %s%s lida junto com '%s' (despacho composto)" % (
                (' ' + audiencia['especie']) if audiencia['especie'] else '', audiencia['data'],
                (' ' + audiencia['hora']) if audiencia['hora'] else '', ato))
            prazos.append({'ato': 'AUDIENCIA DESIGNADA', 'paralelo': True, 'compromisso': True,
                           'vencimento': audiencia['data'], 'prazo_fatal_interno': None, 'dias': None,
                           'contagem': None, 'fonte_prazo': 'audiencia', 'fatal': False, 'base_legal': '-',
                           'hora': audiencia['hora'], 'modalidade': audiencia['modalidade'],
                           'especie': audiencia['especie']})

    # ---- titularidade ----
    # O campo do cadastro so diz que atuamos no processo: vale NOSSO em 147 dos
    # 169 registros. Ele nunca respondeu a pergunta que importa, que e de quem e
    # o prazo DESTE despacho. Agora o texto e lido.
    #
    # Precedencia (doc 04, secao 6): ato recursal > comando dirigido a uma parte
    # > padrao NOSSO. O recursal vem primeiro porque, publicada a decisao, o
    # prazo para recorrer corre para nos seja qual for o polo.
    tit = titularidade or {}
    if polo_inferido:
        evidencias.append(polo_inferido)
    polo_do_ato = cfg.get('titularidade_polo')
    if cfg.get('titularidade_padrao') == 'NOSSO':
        titularidade_val = 'NOSSO'
        evidencias.append('titularidade=NOSSO por ato recursal (decisao publicada abre prazo nosso)')
    elif polo_do_ato and nosso_polo:
        # Ato que so existe para um dos polos (emenda e do autor; contestacao,
        # pagamento e impugnacao sao do reu/executado).
        titularidade_val = 'NOSSO' if nosso_polo == polo_do_ato else 'CONTRARIA'
        evidencias.append('titularidade=%s: o ato %s e do polo %s e nos somos o %s'
                          % (titularidade_val, ato, polo_do_ato, nosso_polo))
        if titularidade_val == 'CONTRARIA':
            avisos.append('Prazo dirigido a parte contraria — nao abrir tarefa de prazo nossa')
    else:
        veredito, conf_tit, ev_tit = TT.avaliar_titularidade(_corpo(tnorm), nosso_polo)
        titularidade_val = veredito
        evidencias += ['titularidade=%s: %s' % (veredito, e) for e in ev_tit]
        if veredito == 'CONTRARIA':
            avisos.append('Prazo dirigido a parte contraria — nao abrir tarefa de prazo nossa')
        elif veredito == 'INDEFINIDO':
            avisos.append('Despacho cita os dois polos — conferir de quem e o prazo')
        if not nosso_polo:
            avisos.append('Nosso polo desconhecido no cadastro — titularidade lida so pelo texto')

    # Regra 3 da calibracao: o DJEN manda uma copia por intimado. A copia
    # enderecada a parte contraria nunca abre prazo nosso; se o ato e recursal, o
    # prazo nosso existe, mas esta na NOSSA copia (esta e duplicata).
    copia_de_adverso = False
    destinatarios = TT.destinatario_da_copia(texto)
    # O ADVBOX muitas vezes nao tem a parte contraria cadastrada (Fontanella,
    # Transrima): a capa do SYNC e o cabecalho da publicacao completam a lista.
    adversos_todos = adversos_cad + TT.parte_adversa_pelo_cabecalho(
        texto, nosso_polo, _tit_pre.get('polo_ativo'), _tit_pre.get('polo_passivo'))
    lado = TT.copia_de(destinatarios, clientes_cad, adversos_todos, limiar=0.3)
    if lado == 'adverso':
        copia_de_adverso = True
        if cfg.get('titularidade_padrao') == 'NOSSO' or ato.startswith('CONTRARRAZOES'):
            avisos.append('Copia enderecada a parte contraria (%s): o prazo nosso esta na nossa copia desta decisao'
                          % '; '.join(destinatarios)[:80])
        else:
            titularidade_val = 'CONTRARIA'
            evidencias.append('titularidade=CONTRARIA: copia enderecada a parte contraria (%s)'
                              % '; '.join(destinatarios)[:80])
    elif lado == 'cliente':
        evidencias.append('copia enderecada ao nosso cliente (%s)' % '; '.join(destinatarios)[:80])

    responsavel, parceiro = _responsavel(trilha, processo)
    if parceiro:
        if parceiro.get('modo') == 'encaminhar':
            avisos.append('Processo conduzido por parceiro (%s): a Advogada encaminha a intimacao e confirma a providencia'
                          % parceiro.get('nome'))
        else:
            avisos.append('Cliente de parceria (%s): a peca e nossa, mas o contato com o cliente passa pelo parceiro'
                          % parceiro.get('nome'))
    if processo in _CARTEIRA:
        evidencias.append('responsavel=%s (carteira dedicada)' % _CARTEIRA_NOME)

    acionavel = trilha in ('PECA', 'MEIO')
    return {
        'ato': ato,
        'ramo': ramo,
        'trilha': trilha,
        'fatal': fatal,
        'base_legal': base_legal,
        'titularidade': titularidade_val,
        'nosso_polo': tit.get('nosso_polo'),
        'cliente': tit.get('cliente'),
        'parte_adversa': tit.get('parte_adversa'),
        'responsavel': responsavel,
        'parceiro': parceiro,
        'copia_de_adverso': copia_de_adverso,
        'destinatarios': destinatarios,
        'teor_fora_do_djen': teor_fora,
        'sigiloso': sigiloso,
        'confianca': round(confianca, 2),
        'acao_automatica_permitida': bool(acionavel and confianca >= LIMIAR_CONFIANCA and titularidade_val == 'NOSSO'),
        'prazos': prazos,
        'audiencia': audiencia,
        'evidencias': evidencias,
        'avisos': avisos,
    }


if __name__ == '__main__':   # bateria rapida: python -m app.controladoria_motor.classificador
    casos = [
        ('quinze dias por extenso', 'Digam as partes, em quinze dias, se pretendem a producao de outras provas.', 15),
        ('em 15 dias', 'Indique, em 15 dias, meios efetivos para prosseguimento da execucao.', 15),
        ('oito dias por extenso', 'fica intimado para apresentar os calculos que entender devidos, em 8 dias', 8),
        ('prazo de oito dias', 'Contestar calculos no prazo de oito dias, nos termos do artigo 879, 2o da CLT.', 8),
        ('prazo comum de 05', 'manifestacao no prazo comum de 05 dias, sob pena de preclusao', 5),
        ('prazo de 10 (dez) dias', 'manifestem-se sobre o laudo pericial, no prazo de 10 (dez) dias.', 10),
        ('cinco dias', 'intimem-se as embargadas para se manifestarem no prazo de 5 (cinco) dias.', 5),
        ('sem prazo', 'Processo distribuido para Quarta Turma.', None),
    ]
    ok = 0
    for nome, texto, esperado in casos:
        got = _dias_do_texto(texto)
        bom = got == esperado
        ok += bom
        print('%s  %-28s -> %s (esperado %s)' % ('OK  ' if bom else 'FALHA', nome, got, esperado))
    print('marcador (s):', _norm('Recebo o(s) Agravo(s) de Instrumento, intime(m)-se para contraminutar(em)'))
    # grau pelo cabecalho (caso Romildo, 05/10/2026): acordao de Turma nao abre RO, abre RR + ED
    cleber = ('PODER JUDICIARIO JUSTICA DO TRABALHO TRIBUNAL REGIONAL DO TRABALHO DA 6a REGIAO TERCEIRA TURMA '
              'Relatora: MARIA CLARA ROT 1000101-08.2025.5.06.0016 RECORRENTE: FULANO RECORRIDO: EMPRESA LTDA '
              'INTIMACAO DESTINATARIO: EMPRESA LTDA [Terceira Turma] Ficam as partes intimadas do acordao proferido '
              'no presente processo. Acesso ao sistema PJe-JT - 2o grau. EMENTA: RECURSO ORDINARIO E RECURSO ADESIVO. '
              'Recursos ordinarios interpostos contra sentenca que julgou parcialmente procedente a reclamacao. '
              'RECURSO DAS RECLAMADAS PROVIDO. RECURSO DO RECLAMANTE DESPROVIDO.')
    sent = ('PODER JUDICIARIO JUSTICA DO TRABALHO 16a VARA DO TRABALHO DO RECIFE ATOrd 1000101-08.2025.5.06.0016 '
            'SENTENCA. Julgo procedente em parte a reclamacao e condeno a reclamada ao pagamento de horas extras.')
    for nome, texto, esperado in (('acordao de Turma do TRT -> RR', cleber, 'RECURSO DE REVISTA'),
                                  ('sentenca da Vara -> RO', sent, 'RECURSO ORDINARIO')):
        r = classificar({'processo': '1000101-08.2025.5.06.0016', 'tribunal': 'TRT6', 'texto': texto, 'data': '2026-10-05T03:00:00+00:00'})
        got = r.get('ato')
        bom = got == esperado and (esperado != 'RECURSO DE REVISTA' or any(p.get('ato') == 'EMBARGOS DE DECLARACAO' and p.get('paralelo') for p in r.get('prazos') or []))
        ok += bom
        casos.append((nome, texto, esperado))
        print('%s  %-28s -> %s (esperado %s)' % ('OK  ' if bom else 'FALHA', nome, got, esperado))
    # despacho composto (a equipe 86458, 06/10/2026): emenda recebida + audiencia designada + 5 dias para documentos
    exemplo_despacho = 'PODER JUDICIÁRIO JUSTIÇA DO TRABALHO TRIBUNAL REGIONAL DO TRABALHO DA 2a REGIÃO 1a VARA DO TRABALHO DE SANTOS ATOrd 9431272-85.2026.5.02.6259 RECLAMANTE: FULANA DE TAL EXEMPLO RECLAMADO: RESTAURANTE EXEMPLO LTDA INTIMAÇÃO Fica V. Sa. intimado para tomar ciência do Despacho ID 03ea768 proferido nos autos. CONCLUSÃO Nesta data, faço os autos conclusos ao (à) Exmo. (a) Juiz (íza) do Trabalho. Santos, 05/10/2026 TAINA BERTOLACI DE VALENTIN Estagiário Conhecimento DECISÃO Recebo a petição Id d77d687 como emenda à inicial. Retifique-se o valor da causa, a fim de constar R$ 78.623,52. Designo a audiência Inicial: 01/12/2026 10:05 na modalidade HÍBRIDA na qual partes e advogados deverão comparecer de forma presencial na SALA DE AUDIÊNCIAS da 1a Vara do Trabalho de Santos, situada na Rua Braz Cubas, 158 - 1o andar, Centro, Santos - SP, 11013-162, OU telepresencial, por meio da plataforma ZOOM, no link abaixo. A ausência do reclamante e/ou seu advogado à audiência importa o arquivamento da reclamação, e o não comparecimento do reclamado importa revelia, além de confissão quanto à matéria de fato, nos termos do art. 844, CLT. Ante a conclusão do julgamento da ADC 80, deverá o(a) requerente da gratuidade juntar em 5 dias, sob pena de preclusão e à luz do § 2o do art. 99 do CPC: a) integralidade de sua CTPS digital; b) 3 últimos holerites (se empregado) , sem prejuízo da juntada de documentos que entender pertinente para demonstrar a precariedade financeira, caso possua rendimento(s) superior a faixa limite ou patrimônio(s) suficientes a arcar com as custas e despesas judicias, tudo sob pena de indeferimento do pedido. Recomenda-se a chegada com 30 minutos de antecedência por ser necessária a identificação individualizada para acesso ao Fórum. No momento da audiência, os participantes que comparecerem presencialmente serão apregoados pelo servidor designado para acompanhamento da sessão A audiência é INICIAL, não sendo o momento para produção da prova testemunhal. A defesa deverá ser efetuada via peticionamento eletrônico, atribuindo-lhe ou não sigilo, no sistema Pje, an'  # texto real do DJEN (cache do SYNC, 06/10/2026)
    r = classificar({'processo': '9431272-85.2026.5.02.6259', 'tribunal': 'TRT2', 'orgao': '1a Vara do Trabalho de Santos',
                     'texto': exemplo_despacho, 'data': '2026-10-06T03:00:00+00:00', 'requer_manifestacao': True, 'prazo_dias': 5})
    aud = r.get('audiencia') or {}
    par = [p for p in r.get('prazos') or [] if p.get('compromisso')]
    bom = (r.get('ato') == 'JUNTADA DE DOCUMENTOS' and aud.get('data') == '2026-12-01' and aud.get('hora') == '10:05'
           and len(par) == 1 and par[0]['vencimento'] == '2026-12-01' and [p for p in r['prazos'] if not p.get('paralelo')][0].get('dias') == 5)
    ok += bom
    casos.append(('despacho composto: juntada + audiencia', exemplo_despacho, 'JUNTADA DE DOCUMENTOS + audiencia 01/12'))
    print('%s  %-28s -> %s + audiencia %s %s %s' % ('OK  ' if bom else 'FALHA', 'despacho composto', r.get('ato'), aud.get('data'), aud.get('hora'), aud.get('modalidade')))
    # audiencia que ja passou nao e designacao: texto que so cita a audiencia de ontem nao vira compromisso
    r2 = classificar({'processo': 'x', 'tribunal': 'TRT2', 'texto': 'Junte os documentos em 5 dias. Audiencia inicial realizada em 01/09/2026 10:00.',
                      'data': '2026-10-06T03:00:00+00:00'})
    bom2 = not r2.get('audiencia')
    ok += bom2
    casos.append(('audiencia passada nao e compromisso', '', None))
    print('%s  %-28s -> audiencia=%s' % ('OK  ' if bom2 else 'FALHA', 'audiencia passada', r2.get('audiencia')))
    # ED julgados (caso Carla x Notre Dame, 16/11/2026): nao cabe ED de novo, cabe o recurso da decisao embargada
    ed_jul = ('CUMPRIMENTO PROVISORIO DE SENTENCA No 0033845-46.2025.8.26.0002/SP EXEQUENTE : FULANA ADVOGADO(A) : X '
              'EXECUTADO : EMPRESA S.A. DESPACHO/DECISAO Vistos. Os Embargos devem ser conhecidos, posto que tempestivos. '
              'Contudo, sao improcedentes, eis que nao ha qualquer omissao, obscuridade, contradicao ou erro material na '
              'decisao proferida. Ante o exposto, CONHECO E REJEITO os embargos de declaracao opostos por nao existirem '
              'omissoes, contradicoes, erro material ou obscuridades a serem sanadas (artigo 1022 do NCPC). Int.')
    ed_cab = ('Intime-se a embargada para, querendo, manifestar-se em 5 dias sobre os embargos de declaracao opostos, '
              'que apontam omissao e contradicao na decisao. Int.')
    for nome, it, esperado, conf_max in (
            ('ED julgados no cumprimento -> AI', {'tribunal': 'TJSP', 'texto': ed_jul, 'tipo_ato': 'decisao_interlocutoria'},
             'AGRAVO DE INSTRUMENTO', 0.7),
            ('ED julgados vara trab -> RO', {'tribunal': 'TRT2', 'texto': ed_jul.replace('CUMPRIMENTO PROVISORIO DE SENTENCA', 'ATOrd')},
             'RECURSO ORDINARIO', 0.7),
            ('ED opostos -> manifestacao', {'tribunal': 'TJSP', 'texto': ed_cab, 'tipo_ato': 'despacho'},
             'MANIFESTACAO SOBRE ED', 0.75),
            ('omissao na sentenca -> ED', {'tribunal': 'TJSP', 'orgao': '3a Vara Civel', 'tipo_ato': 'sentenca', 'texto':
             'SENTENCA. Julgo procedente o pedido. Ha erro material e omissao quanto aos honorarios; '
             'cabem embargos de declaracao para corrigir a contradicao e a obscuridade do dispositivo.'},
             'EMBARGOS DE DECLARACAO', 1),
            ('acordao e-SAJ TJSP -> RESP', {'tribunal': 'TJSP', 'orgao': '14a Camara Direito Privado', 'texto':
             'INTIMACAO DE ACORDAO No 0000781-63.2008.8.26.0318 - Apelacao Civel - Leme - Apelante: BANCO - Apelado: '
             'FULANO - Deram provimento ao recurso. V. U. - APELACAO CIVEL. SENTENCA CASSADA. RECURSO PROVIDO.'},
             'RECURSO ESPECIAL / EXTRAORDINARIO', 1)):
        r = classificar(dict(it, processo='x', data='2026-11-16T03:00:00+00:00'))
        if esperado is None:   # so prova que a regra nova nao dispara: nao vira recurso contra a decisao
            bom = r.get('ato') not in ('AGRAVO DE INSTRUMENTO', 'APELACAO') and 'JULGA embargos' not in ' '.join(r['evidencias'])
        else:
            bom = r.get('ato') == esperado and r.get('confianca') <= conf_max
        ok += bom
        casos.append((nome, '', esperado))
        print('%s  %-28s -> %s conf %s (esperado %s)' % ('OK  ' if bom else 'FALHA', nome, r.get('ato'), r.get('confianca'), esperado))
    print('\n%d/%d' % (ok, len(casos)))
