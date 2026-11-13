# -*- coding: utf-8 -*-
"""Motor de peca do escritorio: texto pela IA com o DNA + formatacao no TIMBRADO.docx.

Usado por routers/peca.py (Gerar Peca) e pelas skills (.claude/skills/timbrado/montar_peca.py
chama formatar_no_timbrado). Sem dependencia de pasta externa.

- gerar_texto_peca(): monta o prompt com juridico/DNA_ESCRITORIO.md (formatacao, abertura,
  fecho, teses, assinatura) e chama o Claude; se a Anthropic recusar, cai na IA de reserva.
- formatar_no_timbrado(): abre juridico/modelos/TIMBRADO.docx (logo no cabecalho, endereco no
  rodape) e escreve o corpo no padrao Nogueira & Tovani: Times New Roman 12, recuo de 7 cm,
  titulos em negrito e sublinhado, fecho com data do protocolo eletronico e os dois socios.
"""
import os
import re
from pathlib import Path

AQUI = Path(__file__).resolve().parent
DNA = AQUI / 'DNA_ESCRITORIO.md'
TIMBRADO = Path(os.getenv('ESC_TIMBRADO_DOCX') or (AQUI / 'modelos' / 'TIMBRADO.docx'))
MODELO = os.getenv('ESC_MODELO_PECA', 'claude-opus-5-5')
FONTE = os.getenv('ESC_FONTE_PECA') or 'Times New Roman'
TAMANHO = float(os.getenv('ESC_TAMANHO_PECA') or 12)
RECUO_CM = float(os.getenv('ESC_RECUO_PECA_CM') or 7)


def ler_dna():
    return DNA.read_text(encoding='utf-8') if DNA.exists() else ''


SISTEMA = """Voce e o advogado redator do escritorio. Escreva a peca COMPLETA, pronta para revisao,
seguindo a risca o DNA abaixo (enderecamento, abertura "por seus advogados", topicos DA/DO em
caixa alta, citacao de dispositivo em paragrafo proprio, pedidos em alineas).

Regras inegociaveis:
- nunca invente jurisprudencia: sem precedente que esteja no DNA ou no bloco de jurisprudencia
  verificada, escreva [CONFERIR PRECEDENTE];
- nunca invente dado do cliente ou do processo: use [PREENCHER: campo];
- valores sempre com "R$";
- ementa de jurisprudencia: cada paragrafo da ementa numa linha que comeca com ">" (o sistema
  recua 3 cm e usa fonte 10);
- termine a peca com a linha "Termos em que, pede deferimento." e PARE: a data ("Sao Paulo, data
  do protocolo eletronico.") e o bloco de assinatura dos socios o sistema coloca sozinho;
- devolva SO o texto da peca, um paragrafo por linha, sem markdown (sem #, sem **), sem comentarios.

===== DNA DO ESCRITORIO =====
{dna}
"""


def _chave_anthropic():
    try:
        from dotenv import dotenv_values
        from ..config import ENV_CENTRAL
        v = dotenv_values(str(ENV_CENTRAL)) if ENV_CENTRAL.exists() else {}
    except Exception:
        v = {}
    return (v.get('ANTHROPIC_API_KEY') or os.environ.get('ANTHROPIC_API_KEY') or '').strip()


def montar_prompt(dados_caso, tipo_peca, esqueleto='', jurisprudencia=''):
    partes = ['TIPO DE PECA: %s' % (tipo_peca or '').upper(), '', 'DADOS DO CASO E DOS AUTOS:', dados_caso or '']
    if esqueleto:
        partes += ['', 'ESQUELETO / ORIENTACAO DA ADVOGADA (siga a ordem dos topicos):', esqueleto]
    if jurisprudencia:
        partes += ['', 'JURISPRUDENCIA VERIFICADA (use so esta):', jurisprudencia]
    return '\n'.join(partes)


def gerar_texto_peca(dados_caso, tipo_peca, esqueleto='', jurisprudencia='', incluir_amostras=True,
                     max_tokens=16000):
    """Devolve o texto puro da peca (um paragrafo por linha)."""
    sistema = SISTEMA.format(dna=ler_dna() if incluir_amostras else '')
    msg = montar_prompt(dados_caso, tipo_peca, esqueleto, jurisprudencia)
    chave = _chave_anthropic()
    erro = None
    modo = (os.getenv('ESC_IA_PECA') or ('api' if chave else 'cli')).lower()
    if modo == 'cli':  # Claude Code da assinatura do escritorio (sem credito de API)
        try:
            return limpar(_via_cli(sistema, msg))
        except Exception as e:
            erro = e
    if chave and modo != 'cli':
        try:
            import anthropic
            cli = anthropic.Anthropic(api_key=chave)
            r = cli.messages.create(model=MODELO, max_tokens=max_tokens, system=sistema,
                                    messages=[{'role': 'user', 'content': msg}])
            return limpar(''.join(b.text for b in r.content if getattr(b, 'type', '') == 'text'))
        except Exception as e:  # sem credito, fora do ar: tenta a reserva
            erro = e
    try:
        from .. import ia_reserva
        if ia_reserva.disponivel():
            return limpar(ia_reserva.texto(sistema, [{'role': 'user', 'content': msg}], max_tokens=max_tokens,
                                           modelo=ia_reserva.MODELO_PRO))
    except Exception as e:
        erro = erro or e
    raise RuntimeError('Nenhuma IA disponivel para gerar a peca (%s)' % (erro or 'sem ANTHROPIC_API_KEY/GEMINI_API_KEY'))


def _via_cli(sistema, msg, timeout=None):
    """claude -p com o prompt de sistema; usa a assinatura logada na maquina (CLAUDE_CODE_OAUTH_TOKEN no servidor)."""
    import shutil
    import subprocess
    exe = os.getenv('ESC_CLAUDE_BIN') or shutil.which('claude') or '/usr/local/bin/claude'
    env = dict(os.environ)
    env.pop('ANTHROPIC_API_KEY', None)
    r = subprocess.run([exe, '-p', '--model', os.getenv('ESC_MODELO_PECA_CLI', 'opus'), '--append-system-prompt',
                        sistema], input=msg, capture_output=True, text=True, encoding='utf-8',
                       env=env, timeout=timeout or int(os.getenv('ESC_TIMEOUT_PECA', '900')))
    if r.returncode or not r.stdout.strip():
        raise RuntimeError('claude -p falhou (%s): %s' % (r.returncode, (r.stderr or r.stdout)[-400:]))
    return r.stdout


def limpar(texto):
    t = (texto or '').replace('\r\n', '\n').replace('\r', '\n').replace('\x0b', '\n').replace('\x0c', '\n')
    t = re.sub(r'[\x00-\x08\x0e-\x1f\x7f￾￿]', '', t)  # controle do Word/PDF: o docx nao aceita
    t = re.sub(r'^\s*#+\s*', '', t, flags=re.M)
    t = t.replace('**', '')
    return re.sub(r'\n{3,}', '\n\n', t).strip()


# ---------------------------------------------------------------- formatacao
# Padrao Nogueira & Tovani (modelo "Inicial - Esqueleto Reclamacao Trabalhista"):
#   Times New Roman 12, entrelinha 1,5, espaco depois 0 e UMA linha em branco de verdade entre
#   paragrafos; corpo justificado com recuo de primeira linha de 7 cm; enderecamento em negrito sem
#   recuo; 9 linhas em branco ate o n. do processo / "Para distribuicao" (negrito + sublinhado);
#   nome da acao, PRELIMINAR e MERITO centralizados, negrito + sublinhado; titulos de topico
#   negrito + sublinhado sem recuo; jurisprudencia (linha que comeca com ">") com recuo esquerdo
#   de 3 cm, fonte 10, entrelinha simples; fecho padrao com "data do protocolo eletronico" e os
#   dois socios assinando lado a lado (tabulacao em 7,5 cm).
ADVOGADOS = [('THIAGO TOVANI', 'OAB/SP 299.424'), ('WAGNER NOGUEIRA DA SILVA', 'OAB/SP 258.002')]
CIDADE_FECHO = os.getenv('ESC_CIDADE_ESCRITORIO') or 'São Paulo'
DATA_FECHO = 'data do protocolo eletrônico'
FECHO = ('Termos em que,', 'pede deferimento.')
LINHAS_ATE_PROCESSO = 9
TAB_ASSINATURA_CM = 7.5
RECUO_CITACAO_CM = 3.0

_ENDERECAMENTO = re.compile(r'^(EXCELENT|AO JU[IÍ]ZO|AO EXCELENT|EGR[EÉ]GI[OA] TRIBUNAL|COLEND|'
                            r'AO TRIBUNAL|AO SUPERIOR|MERIT[IÍ]SSIM)', re.I)
_PROCESSO = re.compile(r'^(PROCESSO( N[º°o.]*)?\s*:?\s*\d|AUTOS( N[º°o.]*)?\s*:?\s*\d|PARA DISTRIBUI)', re.I)
_CABECALHO = re.compile(r'^(RECORRENTE|RECORRID[OA]|AGRAVANTE|AGRAVAD[OA]|EMBARGANTE|EMBARGAD[OA]|ORIGEM|'
                        r'PROCESSO N[º°o.]*\s*:|EGR[EÉ]GIA TURMA|COLENDA TURMA|ILUSTRES)', re.I)
_CENTRAL = re.compile(r'^(RECLAMA[CÇ][AÃ]O TRABALHISTA|PRELIMINAR(MENTE|ES)?|(DO )?M[EÉ]RITO|R[EÉ]PLICA|'
                      r'CONTESTA[CÇ][AÃ]O|RAZ[OÕ]ES (DE|DO|FINAIS).*|CONTRARRAZ[OÕ]ES.*|EMBARGOS DE DECLARA[CÇ][AÃ]O|'
                      r'RECURSO (ORDIN[AÁ]RIO|DE REVISTA)|AGRAVO DE (INSTRUMENTO|PETI[CÇ][AÃ]O)|IMPUGNA[CÇ][AÃ]O .*)$', re.I)
_TERMOS = re.compile(r'^(termos em que|nesses termos|nestes termos)', re.I)
_ASSINATURA = re.compile(r'OAB/[A-Z]{2}\s*[\d.]+\s*$')
_PALAVRA_CAIXA_ALTA = r"[A-ZÀ-ÖØ-Þ][A-ZÀ-ÖØ-Þ0-9&.\-/']*(?![a-zß-ÿ])"
_NOMES = re.compile(r'\b%s(?:\s+%s)+' % (_PALAVRA_CAIXA_ALTA, _PALAVRA_CAIXA_ALTA))
_DATA_LOCAL = re.compile(r'^[A-ZÀ-Ú][\wÀ-ú ]+, (\d{1,2} de [a-zç]+ de \d{4}|data do protocolo[\wÀ-ú ]*)\.?$')


def _eh_titulo(p):
    s = p.strip()
    letras = re.sub(r'[^A-Za-zÀ-ú]', '', s)
    return 3 < len(s) <= 140 and letras and s.upper() == s and not s.endswith((';', ',')) \
        and not _ASSINATURA.search(s)


def _tipo(s, anterior):
    if s.startswith('>'):
        return 'cit'
    if anterior is None and _ENDERECAMENTO.match(s):
        return 'end'
    if _PROCESSO.match(s) and len(s) < 90:
        return 'proc'
    if _CABECALHO.match(s) and len(s) < 160:
        return 'cab'
    if _eh_titulo(s):
        return 'central' if _CENTRAL.match(s.rstrip(':. ')) else 'tit'
    if _ASSINATURA.search(s):
        return 'assin'
    if _DATA_LOCAL.match(s):
        return 'data'
    return 'texto'


def formatar_no_timbrado(texto, out_path, modelo=None, padrao='peca'):
    """Grava `texto` no timbrado do escritorio e devolve o caminho.

    padrao='peca' (default): padrao das pecas do escritorio; se o texto trouxer "Termos em que",
    dali para baixo entra o fecho padrao (data do protocolo eletronico + assinatura dos socios).
    padrao='simples': fichas e listas internas (sem recuo de 7 cm, sem linha em branco entre
    paragrafos, sem fecho)."""
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH as AL
    from docx.shared import Cm, Pt
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    base = Path(modelo or TIMBRADO)
    doc = Document(str(base)) if base.exists() else Document()
    corpo = doc.element.body
    for el in list(corpo):  # mantem cabecalho/rodape (sectPr), limpa o corpo do modelo
        if el.tag != qn('w:sectPr'):
            corpo.remove(el)

    def _fonte(rpr, tam):
        rf = rpr.find(qn('w:rFonts'))
        if rf is None:
            rf = OxmlElement('w:rFonts')
            rpr.insert(0, rf)
        for k in ('w:ascii', 'w:hAnsi', 'w:cs', 'w:eastAsia'):
            rf.set(qn(k), FONTE)
        for k in ('w:sz', 'w:szCs'):
            el = rpr.find(qn(k))
            if el is None:
                el = OxmlElement(k)
                rpr.append(el)
            el.set(qn('w:val'), str(int(round(tam * 2))))

    def par(txt='', negrito=False, sublinhado=False, recuo=0.0, esquerda=0.0, alin=AL.JUSTIFY,
            tam=TAMANHO, entrelinha=1.5, tab=None, depois=0, nomes=False):
        """nomes=True: os nomes em CAIXA ALTA (as partes, na abertura) saem em negrito."""
        p = doc.add_paragraph()
        pf = p.paragraph_format
        pf.alignment = alin
        pf.first_line_indent = Cm(recuo)
        if esquerda:
            pf.left_indent = Cm(esquerda)
        pf.line_spacing = entrelinha
        pf.space_before = Pt(0)
        pf.space_after = Pt(depois)
        if tab:
            pf.tab_stops.add_tab_stop(Cm(tab))
        pedacos, pos = [], 0
        for m in (_NOMES.finditer(txt) if nomes else ()):
            pedacos += [(txt[pos:m.start()], negrito), (m.group(0), True)]
            pos = m.end()
        pedacos.append((txt[pos:], negrito))
        for trecho, b in pedacos:
            if not trecho:
                continue
            r = p.add_run(trecho)
            r.bold = b or None
            r.underline = sublinhado or None
            _fonte(r._element.get_or_add_rPr(), tam)
        # a marca de paragrafo tambem leva a fonte: a linha em branco fica com a altura do texto
        ppr = p._p.get_or_add_pPr()
        rpr = OxmlElement('w:rPr')
        ppr.append(rpr)
        _fonte(rpr, tam)
        return p

    def branco(n=1):
        for _ in range(n):
            par()

    linhas = [s.strip() for s in limpar(texto).split('\n') if s.strip()]

    if padrao != 'peca':
        for s in linhas:
            t = _tipo(s, None)
            if t in ('tit', 'central'):
                par(s, negrito=True, depois=6)
            elif t == 'assin':
                par(s, negrito=True, alin=AL.CENTER, depois=6)
            else:
                par(s, depois=6)
        return _salvar(doc, out_path)

    # o que a IA escreveu a partir do "Termos em que" e trocado pelo fecho padrao do escritorio
    corte = next((i for i, s in enumerate(linhas) if _TERMOS.match(s)), None)
    if corte is not None:
        linhas = linhas[:corte]

    R = RECUO_CM
    anterior = None
    abertura = True  # o primeiro paragrafo de texto qualifica as partes: nomes em negrito
    for s in linhas:
        t = _tipo(s, anterior)
        if anterior == 'end':
            branco(LINHAS_ATE_PROCESSO)  # espaco do despacho, entre o enderecamento e o processo
        elif anterior is not None and not (t == anterior and t in ('cit', 'cab', 'assin')):
            branco()
        if t == 'cit':
            par(s.lstrip('> ').strip(), esquerda=RECUO_CITACAO_CM, tam=max(TAMANHO - 2, 8), entrelinha=1.0)
        elif t in ('end', 'cab'):
            par(s, negrito=True)
        elif t in ('proc', 'tit'):
            par(s, negrito=True, sublinhado=True)
        elif t == 'central':
            par(s, negrito=True, sublinhado=True, alin=AL.CENTER)
        elif t == 'assin':
            par(s, negrito=True, alin=AL.CENTER)
        elif anterior == 'central' and s[:1].islower():
            par(s, nomes=True)  # "pelo rito ordinario, em face de..." continua a frase, sem recuo
        else:
            par(s, recuo=R, nomes=abertura)
            abertura = False
        if t in ('tit', 'central') and anterior not in (None, 'end', 'proc'):
            abertura = False
        anterior = t

    if corte is not None:
        branco()
        for s in FECHO:
            par(s, recuo=R)
        branco(2)
        par('%s, %s.' % (CIDADE_FECHO, DATA_FECHO), recuo=R)
        branco(2)
        if len(ADVOGADOS) == 1:
            for s in ADVOGADOS[0]:
                par(s, negrito=True, alin=AL.CENTER)
        else:
            (n1, o1), (n2, o2) = ADVOGADOS[:2]
            par('%s\t%s' % (n1, n2), negrito=True, alin=AL.LEFT, tab=TAB_ASSINATURA_CM)
            par('%s\t%s' % (o1, o2), negrito=True, alin=AL.LEFT, tab=TAB_ASSINATURA_CM)
    return _salvar(doc, out_path)


def _salvar(doc, out_path):
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(out))
    return str(out)


if __name__ == '__main__':  # teste offline: so formatacao
    import tempfile
    exemplo = """EXCELENTÍSSIMO JUÍZO DA 1ª VARA DO TRABALHO DE SÃO PAULO – SP
Processo nº 0000000-00.2026.5.02.0001
FULANO DE TAL, já devidamente qualificado nos autos da reclamação trabalhista que move em face de EMPRESA X LTDA., igualmente qualificada, vem, respeitosamente, por seus advogados, apresentar sua
RÉPLICA
pelos motivos de fato e de direito a seguir expostos:
DO INTERVALO INTRAJORNADA
A Reclamada sustenta que o intervalo era integralmente usufruído.
> RECURSO DE REVISTA. INTERVALO INTRAJORNADA. [CONFERIR PRECEDENTE]
Ao contrário.
DOS PEDIDOS
a) a rejeição das teses defensivas;
b) a procedência integral dos pedidos.
Termos em que, pede deferimento.
São Paulo, 10 de novembro de 2026.
NOME QUALQUER – OAB/SP 000.000"""
    out = formatar_no_timbrado(exemplo, Path(tempfile.gettempdir()) / 'teste_timbrado_esc.docx')
    from docx import Document
    d = Document(out)
    ps = [p for p in d.paragraphs if p.text.strip()]
    txt = [p.text for p in ps]
    assert ps[0].runs[0].bold and not ps[0].paragraph_format.first_line_indent
    assert ps[1].runs[0].bold and ps[1].runs[0].underline                     # processo
    assert ps[2].paragraph_format.first_line_indent.cm > 6.9                  # recuo de 7 cm
    assert ps[3].alignment == 1 and ps[3].runs[0].underline                   # RÉPLICA centralizada
    assert not ps[4].paragraph_format.first_line_indent                       # continuacao sem recuo
    assert ps[5].runs[0].underline and not ps[5].paragraph_format.first_line_indent
    assert ps[7].paragraph_format.left_indent.cm > 2.9                        # jurisprudencia
    assert ps[2].runs[0].font.name == FONTE == 'Times New Roman'
    negritos = [r.text for r in ps[2].runs if r.bold]
    assert negritos == ['FULANO DE TAL', 'EMPRESA X LTDA.'], negritos       # partes em negrito
    assert not any(r.bold for r in ps[6].runs)                                # corpo sem negrito
    assert 'São Paulo, data do protocolo eletrônico.' in txt
    assert 'OAB/SP 000.000' not in ' '.join(txt)
    assert txt[-1] == 'OAB/SP 299.424\tOAB/SP 258.002'
    vazios = [i for i, p in enumerate(d.paragraphs) if not p.text.strip()]
    assert vazios[:LINHAS_ATE_PROCESSO] == list(range(1, LINHAS_ATE_PROCESSO + 1))
    assert d.sections[0].header.part is not None
    print('ok', out, len(ps), 'paragrafos')
